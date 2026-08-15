"""Application logging and bandwidth history persistence.

Three sinks are produced:

``logs/controller.log``            rotating human readable application log
``logs/bandwidth_history.csv``     one row per bandwidth update (4 fixed columns)
``logs/bandwidth_history.json``    the same history with richer metadata

The CSV schema is intentionally frozen at ``timestamp,bandwidth_mbps,interface,status``
so downstream tooling (``scripts/plot_bandwidth.py``, spreadsheets, pandas) can
rely on it.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Final, Iterable, Literal, Sequence

from .config_loader import LoggingConfig

ROOT_LOGGER_NAME: Final[str] = "rmqnc"

#: Frozen CSV header - do not reorder, external tooling depends on it.
CSV_COLUMNS: Final[tuple[str, ...]] = ("timestamp", "bandwidth_mbps", "interface", "status")

RecordStatus = Literal["applied", "failed", "skipped", "startup", "shutdown", "dry-run"]

_LOG_FORMAT: Final[str] = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_CONSOLE_FORMAT: Final[str] = "%(asctime)s %(levelname)-8s %(message)s"
_DATE_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%S%z"


def utc_now() -> datetime:
    """Timezone-aware current time (local timezone, ISO-8601 friendly)."""
    return datetime.now(timezone.utc).astimezone()


def iso_timestamp(moment: datetime | None = None) -> str:
    """ISO-8601 timestamp with seconds resolution, e.g. ``2026-08-15T12:00:01``."""
    return (moment or utc_now()).strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------- #
# Application logger
# --------------------------------------------------------------------------- #
def setup_logging(config: LoggingConfig, *, verbose: bool = False, quiet: bool = False) -> logging.Logger:
    """Configure and return the root application logger.

    Calling this twice is safe: previously installed handlers are replaced.
    """
    config.app_log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else getattr(logging, config.level, logging.INFO))
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    file_handler = RotatingFileHandler(
        config.app_log_file,
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    file_handler.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)

    if config.console and not quiet:
        console = logging.StreamHandler(stream=sys.stderr)
        console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
        console.setLevel(logging.DEBUG if verbose else getattr(logging, config.level, logging.INFO))
        logger.addHandler(console)

    return logger


def get_logger(name: str = "") -> logging.Logger:
    """Return a child of the application logger."""
    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}" if name else ROOT_LOGGER_NAME)


# --------------------------------------------------------------------------- #
# Bandwidth history
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BandwidthRecord:
    """A single bandwidth decision and its outcome."""

    timestamp: str
    bandwidth_mbps: float
    interface: str
    status: str
    epoch: float = 0.0
    update_index: int = 0
    model: str = ""
    direction: str = "egress"
    apply_duration_ms: float = 0.0
    error: str = ""

    def csv_row(self) -> list[str]:
        """Render the four canonical CSV columns."""
        return [self.timestamp, f"{self.bandwidth_mbps:g}", self.interface, self.status]

    def json_dict(self) -> dict[str, Any]:
        """Full record for the JSON history."""
        return asdict(self)


@dataclass
class BandwidthHistoryLogger:
    """Append bandwidth records to CSV and JSON files.

    The CSV file is appended to and flushed on every record so a hard kill never
    loses more than the in-flight row.  The JSON file is rewritten atomically
    every ``json_flush_every`` records and once more on :meth:`close`.
    """

    config: LoggingConfig
    logger: logging.Logger = field(default_factory=lambda: get_logger("history"))
    records: list[BandwidthRecord] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _csv_handle: Any = field(default=None, repr=False)
    _csv_writer: Any = field(default=None, repr=False)
    _since_flush: int = field(default=0, repr=False)
    _closed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.config.csv_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.json_file.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self.config.csv_file.exists() or self.config.csv_file.stat().st_size == 0
        self._csv_handle = self.config.csv_file.open("a", encoding="utf-8", newline="")
        self._csv_writer = csv.writer(self._csv_handle)
        if write_header:
            self._csv_writer.writerow(CSV_COLUMNS)
            self._csv_handle.flush()

    # -------------------------------------------------------------- recording
    def record(self, record: BandwidthRecord) -> None:
        """Persist a single record."""
        with self._lock:
            if self._closed:
                return
            self.records.append(record)
            try:
                self._csv_writer.writerow(record.csv_row())
                self._csv_handle.flush()
            except OSError as exc:  # pragma: no cover - disk failure
                self.logger.error("cannot append to %s: %s", self.config.csv_file, exc)
            self._since_flush += 1
            if self._since_flush >= self.config.json_flush_every:
                self._write_json_unlocked()

    def log(
        self,
        *,
        bandwidth_mbps: float,
        interface: str,
        status: str,
        update_index: int = 0,
        model: str = "",
        direction: str = "egress",
        apply_duration_ms: float = 0.0,
        error: str = "",
        moment: datetime | None = None,
    ) -> BandwidthRecord:
        """Build and persist a record in one call."""
        now = moment or utc_now()
        record = BandwidthRecord(
            timestamp=iso_timestamp(now),
            bandwidth_mbps=round(float(bandwidth_mbps), 3),
            interface=interface,
            status=status,
            epoch=round(now.timestamp(), 3),
            update_index=update_index,
            model=model,
            direction=direction,
            apply_duration_ms=round(apply_duration_ms, 3),
            error=error,
        )
        self.record(record)
        return record

    # ------------------------------------------------------------------ stats
    def summary(self) -> dict[str, Any]:
        """Aggregate statistics over the records written by this process."""
        applied = [rec.bandwidth_mbps for rec in self.records if rec.status == "applied"]
        if not applied:
            return {"count": 0}
        ordered = sorted(applied)
        count = len(ordered)
        mean = sum(ordered) / count
        variance = sum((value - mean) ** 2 for value in ordered) / count
        return {
            "count": count,
            "min_mbps": ordered[0],
            "max_mbps": ordered[-1],
            "mean_mbps": round(mean, 3),
            "std_mbps": round(variance**0.5, 3),
            "median_mbps": ordered[count // 2],
            "failures": sum(1 for rec in self.records if rec.status == "failed"),
        }

    # ------------------------------------------------------------------ files
    def _write_json_unlocked(self) -> None:
        payload = {
            "schema": 1,
            "generated_at": iso_timestamp(),
            "record_count": len(self.records),
            "summary": self.summary(),
            "records": [rec.json_dict() for rec in self.records],
        }
        try:
            _atomic_write_json(self.config.json_file, payload)
            self._since_flush = 0
        except OSError as exc:  # pragma: no cover - disk failure
            self.logger.error("cannot write %s: %s", self.config.json_file, exc)

    def flush(self) -> None:
        """Force both files to disk."""
        with self._lock:
            if self._csv_handle is not None and not self._csv_handle.closed:
                self._csv_handle.flush()
            self._write_json_unlocked()

    def close(self) -> None:
        """Flush and close all handles.  Safe to call multiple times."""
        with self._lock:
            if self._closed:
                return
            self._write_json_unlocked()
            if self._csv_handle is not None and not self._csv_handle.closed:
                try:
                    self._csv_handle.flush()
                    self._csv_handle.close()
                except OSError:  # pragma: no cover - defensive
                    pass
            self._closed = True

    def __enter__(self) -> "BandwidthHistoryLogger":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Small IO helpers shared by other modules
# --------------------------------------------------------------------------- #
def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON to ``path`` atomically (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(payload, handle, indent=2, sort_keys=False, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover - defensive
            pass
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    """Public wrapper around the atomic JSON writer."""
    _atomic_write_json(path, payload)


def atomic_write_text(path: Path, text: str) -> None:
    """Write text to ``path`` atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:  # pragma: no cover - defensive
            pass
        raise


def read_csv_history(path: Path, limit: int | None = None) -> list[dict[str, str]]:
    """Read the bandwidth CSV history; returns the last ``limit`` rows."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("timestamp")]
    return rows[-limit:] if limit else rows


def format_table(rows: Sequence[Sequence[str]], headers: Iterable[str]) -> str:
    """Render a small fixed-width table (used by the ``status`` command)."""
    header_list = [str(item) for item in headers]
    table = [header_list, *[[str(cell) for cell in row] for row in rows]]
    widths = [max(len(row[index]) for row in table) for index in range(len(header_list))]
    lines = [
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(header_list)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)) for row in table[1:])
    return "\n".join(lines)
