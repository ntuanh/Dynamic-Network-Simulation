"""Runtime state tracking and the ``status`` report.

The running controller keeps a small JSON document under
``runtime_backup/state.json`` describing what it is doing.  ``main.py status``
reads that document, checks whether the process is still alive, and combines it
with the live ``tc``/``iptables`` output.

A PID file with an exclusive ``flock`` guarantees a single controller instance
per host: two processes shaping the same interface would corrupt each other's
rules and each other's rollback journal.
"""

from __future__ import annotations

import errno
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from .logger import atomic_write_json, iso_timestamp, read_csv_history

try:  # pragma: no cover - Linux only
    import fcntl
except ImportError:  # pragma: no cover - development on non-Linux hosts
    fcntl = None  # type: ignore[assignment]

#: Lifecycle phases written to the state file.
PHASE_STARTING: Final[str] = "starting"
PHASE_RUNNING: Final[str] = "running"
PHASE_STOPPING: Final[str] = "stopping"
PHASE_RESTORED: Final[str] = "restored"
PHASE_FAILED: Final[str] = "failed"


@dataclass
class AppState:
    """Serialisable snapshot of what the controller is doing."""

    pid: int = 0
    phase: str = PHASE_STARTING
    started_at: str = ""
    started_epoch: float = 0.0
    config_path: str = ""
    interface: str = ""
    ifb_device: str = ""
    shaper: str = ""
    classification: str = ""
    shape_ingress: bool = False
    ports: list[int] = field(default_factory=list)
    model: str = ""
    duration_sec: float = 0.0
    update_interval_sec: float = 0.0
    current_bandwidth_mbps: float | None = None
    last_update_at: str = ""
    updates_applied: int = 0
    update_failures: int = 0
    dry_run: bool = False
    backup_dir: str = ""
    journal_file: str = ""
    restoration: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AppState":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in data.items() if key in known})

    @property
    def uptime_sec(self) -> float:
        """Seconds since the controller started (0 when unknown)."""
        return max(0.0, time.time() - self.started_epoch) if self.started_epoch else 0.0


@dataclass
class StateStore:
    """Atomic reader/writer for ``runtime_backup/state.json``."""

    path: Path

    def save(self, state: AppState) -> None:
        payload = state.to_dict()
        payload["updated_at"] = iso_timestamp()
        atomic_write_json(self.path, payload)

    def load(self) -> AppState | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return AppState.from_dict(payload)

    def update(self, **fields: Any) -> AppState | None:
        """Patch individual fields of the persisted state."""
        state = self.load()
        if state is None:
            return None
        for key, value in fields.items():
            if hasattr(state, key):
                setattr(state, key, value)
        self.save(state)
        return state

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - defensive
            pass


# --------------------------------------------------------------------------- #
# Process helpers
# --------------------------------------------------------------------------- #
def process_alive(pid: int) -> bool:
    """True when a process with ``pid`` exists and is visible to us."""
    if pid <= 0:
        return False
    if Path("/proc").exists():
        return Path("/proc", str(pid)).exists()
    try:  # pragma: no cover - non-Linux fallback
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def process_command(pid: int) -> str:
    """Best-effort command line of ``pid`` (used to detect stale PID files)."""
    try:
        raw = Path("/proc", str(pid), "cmdline").read_bytes()
    except OSError:
        return ""
    return " ".join(part for part in raw.decode(errors="replace").split("\0") if part)


class PidFileError(RuntimeError):
    """Raised when the PID file is already held by a live controller."""


@dataclass
class PidFile:
    """Exclusive, ``flock``-protected PID file."""

    path: Path
    _handle: Any = field(default=None, repr=False)

    def acquire(self) -> None:
        """Take the lock.

        Raises:
            PidFileError: another controller instance already holds it.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                handle.seek(0)
                existing = handle.read().strip() or "?"
                handle.close()
                raise PidFileError(
                    f"another controller instance is already running (pid {existing}, "
                    f"lock: {self.path}): {exc}\n"
                    f"       stop it first:  sudo python main.py stop"
                ) from exc
        else:  # pragma: no cover - non-Linux development fallback
            handle.seek(0)
            existing = handle.read().strip()
            if existing.isdigit() and process_alive(int(existing)):
                handle.close()
                raise PidFileError(f"another controller instance is already running (pid {existing})")
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle

    def release(self) -> None:
        """Release the lock and remove the file.  Safe to call twice."""
        if self._handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
        except OSError:  # pragma: no cover - defensive
            pass
        finally:
            self._handle = None
            try:
                self.path.unlink()
            except OSError:
                pass

    @staticmethod
    def read(path: Path) -> int:
        """Return the PID stored in ``path`` (0 when missing or unreadable)."""
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            return 0
        return int(raw) if raw.isdigit() else 0


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def format_duration(seconds: float) -> str:
    """``3725`` -> ``1h 02m 05s``."""
    seconds = int(max(0.0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _section(title: str) -> str:
    return f"\n\033[1m{title}\033[0m" if _colour_enabled() else f"\n{title}"


def _colour_enabled() -> bool:
    import sys

    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def render_status(
    state: AppState | None,
    *,
    alive: bool,
    rules: Mapping[str, str] | None = None,
    history_path: Path | None = None,
    history_rows: int = 5,
    journal_ops: int = 0,
) -> str:
    """Render the full ``status`` report."""
    lines: list[str] = ["RabbitMQ Network Controller - status"]

    if state is None:
        lines += [
            "",
            "  controller     : NOT RUNNING (no state file found)",
            "  bandwidth      : n/a",
            "  restoration    : nothing to restore",
        ]
    else:
        if alive:
            running = f"RUNNING (pid {state.pid})"
        elif state.phase == PHASE_RESTORED:
            running = f"STOPPED (pid {state.pid} exited, network restored)"
        else:
            running = f"NOT RUNNING (pid {state.pid} is gone, phase={state.phase})"
        bandwidth = (
            f"{state.current_bandwidth_mbps:.3f} Mbit/s" if state.current_bandwidth_mbps else "n/a"
        )
        lines += [
            "",
            f"  controller     : {running}",
            f"  phase          : {state.phase}",
            f"  uptime         : {format_duration(state.uptime_sec) if alive else 'n/a'}",
            f"  started at     : {state.started_at}",
            f"  config         : {state.config_path}",
            f"  interface      : {state.interface}"
            + (f" (+ ingress via {state.ifb_device})" if state.shape_ingress else ""),
            f"  shaper         : {state.shaper} / classification: {state.classification}",
            f"  rabbitmq ports : {', '.join(str(port) for port in state.ports)}",
            f"  model          : {state.model}",
            f"  bandwidth      : {bandwidth}",
            f"  last update    : {state.last_update_at or 'n/a'}"
            f" ({state.updates_applied} applied, {state.update_failures} failed)",
            f"  dry run        : {'yes' if state.dry_run else 'no'}",
            f"  backup         : {state.backup_dir or 'n/a'}",
        ]

        restoration = state.restoration
        if restoration:
            verdict = "SUCCESS" if restoration.get("success") else "FAILED"
            lines.append(f"  restoration    : {verdict} at {restoration.get('finished_at', '?')}")
            for check in restoration.get("checks", []):
                mark = "ok" if check.get("passed") else "FAIL"
                detail = check.get("detail") or ""
                lines.append(f"      [{mark:>4}] {check.get('name')}" + (f" - {detail}" if detail else ""))
            for error in restoration.get("errors", []):
                lines.append(f"      [ ERR] {error}")
        elif alive:
            lines.append("  restoration    : pending (rules are active)")
        else:
            lines.append(
                "  restoration    : UNKNOWN - the process exited without reporting. "
                "Run: sudo python main.py restore"
            )

    if journal_ops:
        lines.append(f"  pending undo   : {journal_ops} command(s) recorded in the rollback journal")

    if rules:
        lines.append(_section("Live traffic-control rules"))
        for title, output in rules.items():
            lines.append(f"  $ {title}")
            body = output.strip() or "<empty>"
            lines.extend(f"      {line}" for line in body.splitlines())

    if history_path is not None:
        rows = read_csv_history(history_path, limit=history_rows)
        lines.append(_section(f"Last {len(rows)} bandwidth samples ({history_path})"))
        if not rows:
            lines.append("  <no samples recorded yet>")
        else:
            for row in rows:
                lines.append(
                    f"  {row.get('timestamp', '?'):<21} "
                    f"{row.get('bandwidth_mbps', '?'):>10} Mbit/s  "
                    f"{row.get('interface', '?'):<10} {row.get('status', '?')}"
                )
    return "\n".join(lines) + "\n"


def summarize_history(rows: Sequence[Mapping[str, str]]) -> dict[str, float]:
    """Min/mean/max over CSV history rows (used by ``status --json``)."""
    values: list[float] = []
    for row in rows:
        try:
            values.append(float(row["bandwidth_mbps"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not values:
        return {}
    return {
        "count": float(len(values)),
        "min_mbps": min(values),
        "max_mbps": max(values),
        "mean_mbps": round(sum(values) / len(values), 3),
    }
