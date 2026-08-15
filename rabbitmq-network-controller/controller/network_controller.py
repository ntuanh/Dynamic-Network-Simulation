"""Linux traffic-control front-end.

Responsibilities
----------------
* capture a complete snapshot of the current shaping / marking state,
* install a shaping hierarchy that affects **only** RabbitMQ traffic,
* update the RabbitMQ rate at runtime without touching anything else,
* remove every rule it created and restore the previous state,
* verify that the restoration actually succeeded.

Design
------
Two shaper backends are supported:

``htb`` (default, recommended)
    ``root htb`` with two leaf classes: ``1:10`` carries RabbitMQ at the
    dynamic rate, ``1:99`` is the HTB *default* class and runs at line rate, so
    SSH / git / Docker / apt traffic is never delayed.

``tbf``
    ``root prio bands 4``; the default ``priomap`` only ever uses bands 1..3, so
    band ``1:4`` is reachable exclusively through our filters.  A ``tbf`` qdisc
    on that band enforces the RabbitMQ rate.

Traffic is selected either through netfilter marks (``fwmark`` classification,
``iptables -t mangle``) or directly with ``u32`` port matches.  Marks are applied
with ``--set-xmark MARK/MASK`` so other users of ``skb->mark`` are preserved.

Ingress (traffic *arriving* at the host) can optionally be shaped by redirecting
only RabbitMQ packets onto an IFB device.  ``tc`` ingress hooks run before
netfilter, so the ingress path always classifies with ``u32``.

Every mutating command pushes an undo command onto a persistent
:class:`RollbackJournal`, so even a ``SIGKILL`` leaves a machine-readable recipe
for ``main.py restore``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Iterable, Sequence

from .config_loader import AppConfig, config_to_dict
from .logger import atomic_write_json, atomic_write_text, get_logger, iso_timestamp
from .shell import CommandResult, CommandRunner, ToolNotFoundError

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
ROOT_HANDLE: Final[str] = "1:"
SHAPED_CLASSID_HTB: Final[str] = "1:10"
DEFAULT_CLASSID_HTB: Final[str] = "1:99"
SHAPED_LEAF_HANDLE: Final[str] = "10:"
DEFAULT_LEAF_HANDLE: Final[str] = "99:"
SHAPED_BAND_TBF: Final[str] = "1:4"
TBF_HANDLE: Final[str] = "40:"
TBF_NETEM_HANDLE: Final[str] = "41:"
INGRESS_HANDLE: Final[str] = "ffff:"

FILTER_PRIO_V4: Final[int] = 10
FILTER_PRIO_V6: Final[int] = 11
INGRESS_PRIO_V4: Final[int] = 30
INGRESS_PRIO_V6: Final[int] = 31

MANGLE_CHAIN: Final[str] = "RMQNC"

#: Root qdiscs that are kernel defaults and may safely be replaced.
REPLACEABLE_QDISCS: Final[frozenset[str]] = frozenset(
    {"pfifo_fast", "noqueue", "mq", "fq_codel", "fq", "pfifo", "bfifo", "clsact"}
)

#: Error fragments that mean "the object was already gone" during teardown.
_ALREADY_GONE: Final[tuple[str, ...]] = (
    "no such file or directory",
    "no such process",
    "cannot find device",
    "does not exist",
    "invalid argument",  # tc qdisc del on a device with no root qdisc
    "no chain/target/match by that name",
    "chain already exists",
    "not found",
)

#: Minimum rate tc can meaningfully enforce.
MIN_RATE_MBPS: Final[float] = 0.008
#: Fallback ceiling for unshaped traffic when the link speed is unknown.
FALLBACK_CEIL_MBPS: Final[float] = 10_000.0

_QDISC_LINE = re.compile(r"^qdisc\s+(?P<kind>\S+)\s+(?P<handle>\S+)\s+(?P<parent>root|parent\s+\S+|ingress)")


class NetworkError(RuntimeError):
    """Raised for unrecoverable traffic-control problems."""


# --------------------------------------------------------------------------- #
# Rollback journal
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RollbackOp:
    """A single undo command."""

    description: str
    argv: tuple[str, ...]
    ignore: tuple[str, ...] = _ALREADY_GONE

    def to_dict(self) -> dict[str, Any]:
        return {"description": self.description, "argv": list(self.argv), "ignore": list(self.ignore)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RollbackOp":
        return cls(
            description=str(data.get("description", "")),
            argv=tuple(str(part) for part in data.get("argv", ())),
            ignore=tuple(str(part) for part in data.get("ignore", _ALREADY_GONE)),
        )


@dataclass
class RollbackJournal:
    """Persistent LIFO stack of undo commands.

    The journal is written to disk after every push, so a hard crash (SIGKILL,
    power loss, OOM killer) still leaves an exact recipe for undoing everything
    this process did.  ``main.py restore`` replays it.
    """

    path: Path
    ops: list[RollbackOp] = field(default_factory=list)
    logger: logging.Logger = field(default_factory=lambda: get_logger("journal"))

    def push(self, description: str, argv: Sequence[str], ignore: Sequence[str] = _ALREADY_GONE) -> None:
        """Record an undo command and persist the journal immediately."""
        self.ops.append(RollbackOp(description, tuple(str(part) for part in argv), tuple(ignore)))
        self.save()

    def save(self) -> None:
        payload = {
            "schema": 1,
            "updated_at": iso_timestamp(),
            "ops": [op.to_dict() for op in self.ops],
        }
        try:
            atomic_write_json(self.path, payload)
        except OSError as exc:  # pragma: no cover - disk failure
            self.logger.error("cannot persist rollback journal %s: %s", self.path, exc)

    def clear(self) -> None:
        """Empty the journal (called once restoration succeeded)."""
        self.ops.clear()
        self.save()

    @classmethod
    def load(cls, path: Path, logger: logging.Logger | None = None) -> "RollbackJournal":
        """Load a journal from disk; returns an empty journal when absent."""
        journal = cls(path=path, logger=logger or get_logger("journal"))
        if not path.exists():
            return journal
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            journal.logger.warning("cannot read rollback journal %s: %s", path, exc)
            return journal
        journal.ops = [RollbackOp.from_dict(item) for item in payload.get("ops", [])]
        return journal

    def __len__(self) -> int:
        return len(self.ops)


# --------------------------------------------------------------------------- #
# Snapshots and restoration reports
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NetworkSnapshot:
    """Everything needed to describe (and compare) the shaping state."""

    interface: str
    captured_at: str
    qdisc: str = ""
    classes: str = ""
    filters: str = ""
    ingress_filters: str = ""
    iptables_mangle: str = ""
    ip6tables_mangle: str = ""
    nft_ruleset: str = ""
    links: str = ""
    root_qdisc_kind: str = ""
    root_qdisc_line: str = ""
    has_ingress_qdisc: bool = False

    # ---------------------------------------------------------------- helpers
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, directory: Path) -> None:
        """Write the snapshot as JSON plus one plain-text file per section."""
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_json(directory / "snapshot.json", self.to_dict())
        sections = {
            "tc_qdisc.txt": self.qdisc,
            "tc_class.txt": self.classes,
            "tc_filter.txt": self.filters,
            "tc_filter_ingress.txt": self.ingress_filters,
            "iptables_mangle.rules": self.iptables_mangle,
            "ip6tables_mangle.rules": self.ip6tables_mangle,
            "nft_ruleset.txt": self.nft_ruleset,
            "ip_link.txt": self.links,
        }
        for name, content in sections.items():
            atomic_write_text(directory / name, content if content.endswith("\n") else content + "\n")

    @classmethod
    def load(cls, directory: Path) -> "NetworkSnapshot | None":
        """Load a snapshot previously written by :meth:`save`."""
        path = directory / "snapshot.json"
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in payload.items() if key in known})


@dataclass(frozen=True)
class RestorationCheck:
    """Result of a single post-teardown verification."""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RestorationReport:
    """Outcome of the full teardown + verification sequence."""

    reason: str
    interface: str
    started_at: str
    finished_at: str = ""
    ops_executed: int = 0
    errors: list[str] = field(default_factory=list)
    checks: list[RestorationCheck] = field(default_factory=list)
    verified: bool = False

    @property
    def success(self) -> bool:
        """True when no error occurred and every verification passed."""
        return not self.errors and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "interface": self.interface,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ops_executed": self.ops_executed,
            "success": self.success,
            "verified": self.verified,
            "errors": list(self.errors),
            "checks": [check.to_dict() for check in self.checks],
        }

    def render(self) -> str:
        """Human readable multi-line summary."""
        status = "SUCCESS" if self.success else "FAILED"
        lines = [
            f"Restoration: {status} (reason: {self.reason})",
            f"  interface     : {self.interface}",
            f"  undo commands : {self.ops_executed}",
        ]
        for check in self.checks:
            mark = "ok  " if check.passed else "FAIL"
            lines.append(f"  [{mark}] {check.name}" + (f" - {check.detail}" if check.detail else ""))
        for error in self.errors:
            lines.append(f"  [ERR ] {error}")
        if not self.success:
            lines.append(
                "  manual recovery: sudo tc qdisc del dev "
                f"{self.interface} root ; sudo tc qdisc del dev {self.interface} ingress"
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def detect_default_interface(runner: CommandRunner) -> str:
    """Return the interface carrying the default route.

    Raises:
        NetworkError: no default route could be found.
    """
    output = runner.capture(["ip", "-o", "route", "show", "default"])
    match = re.search(r"\bdev\s+(\S+)", output)
    if match:
        return match.group(1)
    output = runner.capture(["ip", "-o", "-6", "route", "show", "default"])
    match = re.search(r"\bdev\s+(\S+)", output)
    if match:
        return match.group(1)
    raise NetworkError(
        "cannot auto-detect the network interface (no default route). "
        "Set network.interface explicitly in the configuration file; "
        "list candidates with: ip -brief link show"
    )


def interface_exists(interface: str) -> bool:
    """True when ``/sys/class/net/<interface>`` exists."""
    return Path("/sys/class/net", interface).exists()


def link_speed_mbps(interface: str) -> float | None:
    """Read the NIC link speed in Mbit/s (``None`` for virtual devices)."""
    try:
        raw = Path("/sys/class/net", interface, "speed").read_text(encoding="utf-8").strip()
        speed = float(raw)
    except (OSError, ValueError):
        return None
    return speed if speed > 0 else None


def rate_arg(mbps: float) -> str:
    """Render a Mbit/s value as a ``tc`` rate argument."""
    return f"{max(mbps, MIN_RATE_MBPS):.3f}mbit"


def burst_bytes(mbps: float, *, hz: int = 250, minimum: int = 1600, maximum: int = 1 << 20) -> int:
    """Token bucket depth: one timer tick worth of traffic, clamped to [1.6 KB, 1 MB]."""
    return max(minimum, min(int(mbps * 1_000_000 / 8 / hz), maximum))


def quantum_bytes(mbps: float, *, mtu: int = 1514, maximum: int = 60_000) -> int:
    """HTB quantum: ~1 ms of traffic, never below one MTU (kernel requirement).

    The textbook value is ``rate / r2q``; for multi-megabit rates that exceeds
    the 200000 byte limit the kernel warns about, so a millisecond of traffic is
    used instead.  Quantum only governs how many bytes a class may dequeue per
    round, and our two classes never compete for the same bandwidth.
    """
    return max(mtu, min(int(mbps * 1_000_000 / 8 / 1000), maximum))


def normalize_tc(text: str) -> list[str]:
    """Normalise ``tc`` output for comparison (drops volatile counters)."""
    lines = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line or line.startswith("<"):
            continue
        line = re.sub(r"\b(Sent|bytes|pkt|dropped|overlimits|requeues|backlog)\b.*$", "", line).strip()
        if line:
            lines.append(line)
    return sorted(lines)


def normalize_iptables(text: str) -> list[str]:
    """Normalise ``iptables-save`` output (drops comments and counters)."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("<"):
            continue
        line = re.sub(r"\[\d+:\d+\]", "[0:0]", line)
        lines.append(line)
    return sorted(lines)


def unified_diff(before: Sequence[str], after: Sequence[str], limit: int = 8) -> str:
    """Compact description of the difference between two normalised outputs."""
    missing = [line for line in before if line not in after]
    added = [line for line in after if line not in before]
    parts: list[str] = []
    if missing:
        parts.append("missing: " + " | ".join(missing[:limit]))
    if added:
        parts.append("unexpected: " + " | ".join(added[:limit]))
    return "; ".join(parts)


def _chunk(values: Sequence[int], size: int) -> Iterable[tuple[int, ...]]:
    for start in range(0, len(values), size):
        yield tuple(values[start : start + size])


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #
class NetworkController:
    """Create, update and remove RabbitMQ traffic shaping rules."""

    def __init__(
        self,
        config: AppConfig,
        runner: CommandRunner,
        journal: RollbackJournal,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.journal = journal
        self.logger = logger or get_logger("network")
        self.interface: str = config.network.interface
        self.ifb_device: str = config.network.ifb_device
        self.baseline: NetworkSnapshot | None = None
        self.backup_dir: Path | None = None
        self.ingress_active: bool = False
        self.classification: str = config.network.classification
        self.current_mbps: float | None = None
        self.applied: bool = False
        self._fw_mask_supported: bool = True
        self._unshaped_ceil: float = FALLBACK_CEIL_MBPS

    # ------------------------------------------------------------------ setup
    def preflight(self, *, force: bool = False) -> list[str]:
        """Validate the environment before touching anything.

        Returns a list of non-fatal warnings.

        Raises:
            NetworkError: the host cannot support the requested configuration.
        """
        warnings: list[str] = []

        for tool in ("tc", "ip"):
            if self.runner.has(tool):
                continue
            if not self.runner.dry_run:
                raise ToolNotFoundError(tool)
            warnings.append(f"{tool} is not installed - dry-run only prints the commands it would run")

        if self.interface == "auto":
            self.interface = detect_default_interface(self.runner)
            self.logger.info("auto-detected network interface: %s", self.interface)
        if not self.runner.dry_run and not interface_exists(self.interface):
            available = sorted(path.name for path in Path("/sys/class/net").glob("*")) or ["<none>"]
            raise NetworkError(
                f"network interface {self.interface!r} does not exist. "
                f"Available interfaces: {', '.join(available)}"
            )

        if self.classification == "fwmark" and not self.runner.has("iptables") and not self.runner.dry_run:
            warnings.append(
                "iptables is not installed; falling back to classification='u32' "
                "(pure tc port matching, no packet marking)"
            )
            self.classification = "u32"

        speed = self.config.network.unshaped_ceil_mbps or link_speed_mbps(self.interface) or 0.0
        self._unshaped_ceil = speed if speed > 0 else FALLBACK_CEIL_MBPS
        if speed <= 0:
            warnings.append(
                f"link speed of {self.interface} is unknown (virtual device?); "
                f"unshaped traffic is capped at {FALLBACK_CEIL_MBPS:.0f} Mbit/s - "
                "set network.unshaped_ceil_mbps if your link is faster"
            )
        if self._unshaped_ceil < self.config.bandwidth.max_mbps:
            warnings.append(
                f"bandwidth.max_mbps ({self.config.bandwidth.max_mbps}) exceeds the unshaped ceiling "
                f"({self._unshaped_ceil} Mbit/s); RabbitMQ can never exceed the physical link"
            )

        # This snapshot doubles as the restoration baseline.
        snapshot = self.capture_snapshot()
        self.baseline = snapshot
        if snapshot.root_qdisc_kind and snapshot.root_qdisc_kind not in REPLACEABLE_QDISCS:
            message = (
                f"{self.interface} already has a non-default root qdisc "
                f"({snapshot.root_qdisc_line.strip()}). Replacing it would disturb existing shaping."
            )
            if self.config.network.protect_existing_qdisc and not force:
                raise NetworkError(
                    message
                    + "\n       Re-run with --force to take it over anyway, or set "
                    "network.protect_existing_qdisc: false"
                )
            warnings.append(message + " (proceeding because --force was given)")

        if snapshot.has_ingress_qdisc and self.config.network.shape_ingress:
            message = f"{self.interface} already has an ingress/clsact qdisc installed"
            if not force:
                raise NetworkError(
                    message
                    + ". Another tool (tc, cilium, tcpdump-based tooling) may own it; "
                    "re-run with --force or disable network.shape_ingress"
                )
            warnings.append(message + " (proceeding because --force was given)")

        return warnings

    # --------------------------------------------------------------- snapshot
    def capture_snapshot(self) -> NetworkSnapshot:
        """Read the current shaping / marking state of the host."""
        capture = self.runner.capture
        qdisc = capture(["tc", "qdisc", "show", "dev", self.interface])
        root_kind, root_line = "", ""
        has_ingress = False
        for line in qdisc.splitlines():
            match = _QDISC_LINE.match(line.strip())
            if not match:
                continue
            if match.group("parent") == "root" and not root_kind:
                root_kind, root_line = match.group("kind"), line
            if match.group("parent") == "ingress" or match.group("kind") in ("ingress", "clsact"):
                has_ingress = True

        iptables_mangle = capture(["iptables-save", "-t", "mangle"]) if self.runner.has("iptables-save") else ""
        ip6tables_mangle = (
            capture(["ip6tables-save", "-t", "mangle"]) if self.runner.has("ip6tables-save") else ""
        )
        nft = capture(["nft", "list", "ruleset"]) if self.runner.has("nft") else ""

        return NetworkSnapshot(
            interface=self.interface,
            captured_at=iso_timestamp(),
            qdisc=qdisc,
            classes=capture(["tc", "class", "show", "dev", self.interface]),
            filters=capture(["tc", "filter", "show", "dev", self.interface]),
            ingress_filters=capture(["tc", "filter", "show", "dev", self.interface, "parent", INGRESS_HANDLE]),
            iptables_mangle=iptables_mangle,
            ip6tables_mangle=ip6tables_mangle,
            nft_ruleset=nft,
            links=capture(["ip", "-o", "link", "show"]),
            root_qdisc_kind=root_kind,
            root_qdisc_line=root_line,
            has_ingress_qdisc=has_ingress,
        )

    def save_backup(self) -> Path:
        """Persist the pre-run snapshot under ``runtime_backup/``."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = self.config.runtime.backup_dir / f"backup-{stamp}"
        snapshot = self.baseline or self.capture_snapshot()
        self.baseline = snapshot
        snapshot.save(directory)
        atomic_write_json(directory / "config.json", config_to_dict(self.config))
        latest = self.config.runtime.backup_dir / "latest"
        try:
            if latest.is_symlink() or latest.exists():
                if latest.is_dir() and not latest.is_symlink():
                    shutil.rmtree(latest)
                else:
                    latest.unlink()
            latest.symlink_to(directory.name, target_is_directory=True)
        except (OSError, NotImplementedError):
            # Filesystems without symlink support: copy instead.
            try:
                shutil.copytree(directory, latest, dirs_exist_ok=True)
            except OSError as exc:  # pragma: no cover - defensive
                self.logger.warning("cannot materialise %s: %s", latest, exc)
        self.backup_dir = directory
        self.logger.info("network state backed up to %s", directory)
        return directory

    # ------------------------------------------------------------------ rules
    def setup(self, initial_mbps: float) -> None:
        """Install the full shaping hierarchy and apply the first rate."""
        if self.baseline is None:
            self.baseline = self.capture_snapshot()

        self.logger.info(
            "installing shaping on %s (shaper=%s, classification=%s, ports=%s, ingress=%s)",
            self.interface,
            self.config.network.shaper,
            self.classification,
            ",".join(str(port) for port in self.config.rabbitmq.ports),
            self.config.network.shape_ingress,
        )

        # The original root qdisc is re-installed last during teardown.
        self._push_original_qdisc_restore()

        self._build_shaper(self.interface, classified=True, mbps=initial_mbps)

        if self.classification == "fwmark":
            self._setup_marking()
            self._install_fw_filters(self.interface)
        else:
            self._install_u32_filters(self.interface)

        if self.config.network.shape_ingress:
            self._setup_ingress(initial_mbps)

        self.applied = True
        self.current_mbps = initial_mbps
        self.logger.info("shaping active: RabbitMQ traffic limited to %.3f Mbit/s", initial_mbps)

    # ................................................................. shaper
    def _build_shaper(self, device: str, *, classified: bool, mbps: float) -> None:
        """Create the qdisc/class hierarchy on ``device``.

        Args:
            device: interface or IFB device.
            classified: the device carries mixed traffic and therefore needs a
                default (unshaped) class plus filters.  ``False`` means every
                packet on the device is already known to be RabbitMQ traffic.
        """
        if self.config.network.shaper == "htb":
            self._build_htb(device, classified=classified, mbps=mbps)
        else:
            self._build_tbf(device, classified=classified, mbps=mbps)

    def _build_htb(self, device: str, *, classified: bool, mbps: float) -> None:
        default_class = "99" if classified else "10"
        self._tc(
            ["qdisc", "add", "dev", device, "root", "handle", ROOT_HANDLE, "htb", "default", default_class],
            undo=("remove root qdisc", ["tc", "qdisc", "del", "dev", device, "root"]),
            hint=f"failed to install the root htb qdisc on {device}",
        )
        # RabbitMQ class.
        self._tc(
            [
                "class", "add", "dev", device, "parent", ROOT_HANDLE, "classid", SHAPED_CLASSID_HTB,
                "htb", "rate", rate_arg(mbps), "ceil", rate_arg(mbps),
                "burst", f"{burst_bytes(mbps)}b", "cburst", f"{burst_bytes(mbps)}b",
                "quantum", str(quantum_bytes(mbps)), "prio", "1",
            ]
        )
        self._add_leaf_qdisc(device, parent=SHAPED_CLASSID_HTB, handle=SHAPED_LEAF_HANDLE)

        if classified:
            ceil = self._unshaped_ceil
            self._tc(
                [
                    "class", "add", "dev", device, "parent", ROOT_HANDLE, "classid", DEFAULT_CLASSID_HTB,
                    "htb", "rate", rate_arg(ceil), "ceil", rate_arg(ceil),
                    "burst", f"{burst_bytes(ceil)}b", "quantum", str(quantum_bytes(ceil)), "prio", "0",
                ]
            )
            self._tc(
                ["qdisc", "add", "dev", device, "parent", DEFAULT_CLASSID_HTB, "handle",
                 DEFAULT_LEAF_HANDLE, self.config.network.leaf_qdisc],
                check=False,
            )

    def _build_tbf(self, device: str, *, classified: bool, mbps: float) -> None:
        latency = f"{self.config.network.latency_ms:g}ms"
        if classified:
            # prio's default priomap only ever selects bands 1..3, so band 4 is
            # reachable exclusively through our filters.
            self._tc(
                ["qdisc", "add", "dev", device, "root", "handle", ROOT_HANDLE, "prio", "bands", "4"],
                undo=("remove root qdisc", ["tc", "qdisc", "del", "dev", device, "root"]),
                hint=f"failed to install the root prio qdisc on {device}",
            )
            self._tc(
                ["qdisc", "add", "dev", device, "parent", SHAPED_BAND_TBF, "handle", TBF_HANDLE,
                 "tbf", "rate", rate_arg(mbps), "burst", f"{burst_bytes(mbps)}b", "latency", latency]
            )
            if self.config.netem.active:
                self._tc(
                    ["qdisc", "add", "dev", device, "parent", f"{TBF_HANDLE}1", "handle", TBF_NETEM_HANDLE,
                     "netem", *self.config.netem.tc_args()],
                    check=False,
                )
        else:
            self._tc(
                ["qdisc", "add", "dev", device, "root", "handle", ROOT_HANDLE, "tbf",
                 "rate", rate_arg(mbps), "burst", f"{burst_bytes(mbps)}b", "latency", latency],
                undo=("remove root qdisc", ["tc", "qdisc", "del", "dev", device, "root"]),
                hint=f"failed to install the root tbf qdisc on {device}",
            )
            if self.config.netem.active:
                self._tc(
                    ["qdisc", "add", "dev", device, "parent", f"{ROOT_HANDLE}1", "handle", TBF_NETEM_HANDLE,
                     "netem", *self.config.netem.tc_args()],
                    check=False,
                )

    def _add_leaf_qdisc(self, device: str, *, parent: str, handle: str) -> None:
        """Attach netem (when configured) or the configured leaf qdisc."""
        if self.config.netem.active:
            self._tc(
                ["qdisc", "add", "dev", device, "parent", parent, "handle", handle,
                 "netem", *self.config.netem.tc_args()],
                check=False,
            )
        else:
            self._tc(
                ["qdisc", "add", "dev", device, "parent", parent, "handle", handle,
                 self.config.network.leaf_qdisc],
                check=False,
            )

    # ................................................................ filters
    def _shaped_flowid(self) -> str:
        return SHAPED_CLASSID_HTB if self.config.network.shaper == "htb" else SHAPED_BAND_TBF

    def _install_fw_filters(self, device: str) -> None:
        """Route marked packets into the shaped class."""
        mark = self.config.network.fwmark
        mask = self.config.network.fwmask
        flowid = self._shaped_flowid()
        protocols = ["ip"] + (["ipv6"] if self.config.network.enable_ipv6 else [])
        for protocol, prio in zip(protocols, (FILTER_PRIO_V4, FILTER_PRIO_V6)):
            argv = [
                "filter", "add", "dev", device, "parent", ROOT_HANDLE,
                "protocol", protocol, "prio", str(prio),
                "handle", f"0x{mark:x}/0x{mask:x}", "fw", "flowid", flowid,
            ]
            result = self._tc(argv, check=False)
            if not result.ok:
                # Older iproute2 builds do not accept a mask in the fw handle.
                self._fw_mask_supported = False
                fallback = list(argv)
                fallback[fallback.index("handle") + 1] = f"0x{mark:x}"
                self._tc(fallback, hint="failed to install the fw classifier")
                self.logger.warning(
                    "iproute2 does not support masked fw handles; using an exact mark match "
                    "(0x%x). Other software writing to skb->mark may break classification.",
                    mark,
                )

    def _install_u32_filters(self, device: str, *, parent: str = ROOT_HANDLE) -> None:
        """Match RabbitMQ TCP ports directly with u32 (no netfilter involved).

        One filter per (address family, direction, port) combination.  IPv6
        selectors are best-effort: some iproute2 builds lack ``ip6 protocol``.
        """
        flowid = self._shaped_flowid()
        installed = 0
        for protocol, prio, match_proto in self._u32_protocols():
            for selector in self._port_selectors():
                for port in self.config.rabbitmq.ports:
                    result = self._tc(
                        [
                            "filter", "add", "dev", device, "parent", parent,
                            "protocol", protocol, "prio", str(prio), "u32",
                            "match", match_proto, "protocol", "6", "0xff",
                            "match", match_proto, selector, str(port), "0xffff",
                            "flowid", flowid,
                        ],
                        check=protocol == "ip",
                    )
                    installed += 1 if result.ok else 0
        if installed == 0:
            raise NetworkError(
                f"no u32 filter could be installed on {device}; RabbitMQ traffic would not be shaped"
            )

    def _u32_protocols(self) -> list[tuple[str, int, str]]:
        protocols = [("ip", FILTER_PRIO_V4, "ip")]
        if self.config.network.enable_ipv6:
            protocols.append(("ipv6", FILTER_PRIO_V6, "ip6"))
        return protocols

    def _port_selectors(self) -> list[str]:
        """u32 port selectors implied by ``rabbitmq.match_direction``."""
        direction = self.config.rabbitmq.match_direction
        if direction == "dst":
            return ["dport"]
        if direction == "src":
            return ["sport"]
        return ["dport", "sport"]

    # ................................................................ marking
    def _setup_marking(self) -> None:
        """Create the mangle chain that marks RabbitMQ packets."""
        binaries = [("iptables", "iptables-save")]
        if self.config.network.enable_ipv6 and self.runner.has("ip6tables"):
            binaries.append(("ip6tables", "ip6tables-save"))

        mark = self.config.network.fwmark
        mask = self.config.network.fwmask
        xmark = f"0x{mark:x}/0x{mask:x}"

        for binary, _ in binaries:
            self._run(
                [binary, "-t", "mangle", "-N", MANGLE_CHAIN],
                undo=(f"delete {binary} chain {MANGLE_CHAIN}", [binary, "-t", "mangle", "-X", MANGLE_CHAIN]),
                check=False,
                hint=f"cannot create the {binary} mangle chain {MANGLE_CHAIN}",
            )
            self.journal.push(f"flush {binary} chain {MANGLE_CHAIN}", [binary, "-t", "mangle", "-F", MANGLE_CHAIN])
            # Start from a clean chain in case a previous run left rules behind.
            self._run([binary, "-t", "mangle", "-F", MANGLE_CHAIN], check=False)

            for ports in _chunk(self.config.rabbitmq.ports, 15):
                port_list = ",".join(str(port) for port in ports)
                for flag in self._multiport_flags():
                    self._run(
                        [
                            binary, "-t", "mangle", "-A", MANGLE_CHAIN,
                            "-p", "tcp", "-m", "multiport", flag, port_list,
                            "-j", "MARK", "--set-xmark", xmark,
                        ],
                        hint=f"cannot add the {binary} marking rule for ports {port_list}",
                    )

            self._run(
                [binary, "-t", "mangle", "-I", "POSTROUTING", "1", "-o", self.interface, "-j", MANGLE_CHAIN],
                undo=(
                    f"remove {binary} POSTROUTING jump",
                    [binary, "-t", "mangle", "-D", "POSTROUTING", "-o", self.interface, "-j", MANGLE_CHAIN],
                ),
                hint=f"cannot hook {MANGLE_CHAIN} into the {binary} mangle POSTROUTING chain",
            )

    def _multiport_flags(self) -> tuple[str, ...]:
        direction = self.config.rabbitmq.match_direction
        if direction == "dst":
            return ("--dports",)
        if direction == "src":
            return ("--sports",)
        return ("--dports", "--sports")

    # ................................................................ ingress
    def _setup_ingress(self, mbps: float) -> None:
        """Redirect RabbitMQ ingress traffic onto an IFB device and shape it."""
        if not self._ensure_ifb_device():
            self.logger.warning(
                "ingress shaping disabled: the ifb kernel module is unavailable. "
                "Only outbound RabbitMQ traffic will be limited."
            )
            return

        self._build_shaper(self.ifb_device, classified=False, mbps=mbps)

        self._tc(
            ["qdisc", "add", "dev", self.interface, "handle", INGRESS_HANDLE, "ingress"],
            undo=("remove ingress qdisc", ["tc", "qdisc", "del", "dev", self.interface, "ingress"]),
            hint="cannot install the ingress qdisc",
        )
        # tc ingress runs before netfilter, so marks are not available here:
        # classify with u32 and mirror only RabbitMQ packets to the IFB device.
        redirected = 0
        for protocol, _prio, match_proto in self._u32_protocols():
            for selector in self._port_selectors():
                for port in self.config.rabbitmq.ports:
                    result = self._tc(
                        [
                            "filter", "add", "dev", self.interface, "parent", INGRESS_HANDLE,
                            "protocol", protocol,
                            "prio", str(INGRESS_PRIO_V4 if protocol == "ip" else INGRESS_PRIO_V6),
                            "u32",
                            "match", match_proto, "protocol", "6", "0xff",
                            "match", match_proto, selector, str(port), "0xffff",
                            "action", "mirred", "egress", "redirect", "dev", self.ifb_device,
                        ],
                        check=False,
                    )
                    redirected += 1 if result.ok else 0
        if redirected == 0 and not self.runner.dry_run:
            raise NetworkError(
                "ingress redirection filters could not be installed; "
                "check that the 'act_mirred' kernel module is available (modprobe act_mirred)"
            )
        self.ingress_active = True
        self.logger.info("ingress shaping active via %s", self.ifb_device)

    def _ensure_ifb_device(self) -> bool:
        """Create and bring up the IFB device; returns False when unsupported."""
        if not self.runner.dry_run and interface_exists(self.ifb_device):
            self.logger.warning(
                "%s already exists and was not created by this run; reusing it without ownership",
                self.ifb_device,
            )
        else:
            result = self._run(
                ["ip", "link", "add", self.ifb_device, "type", "ifb"],
                undo=(f"delete {self.ifb_device}", ["ip", "link", "del", self.ifb_device]),
                check=False,
            )
            if not result.ok:
                self._run(["modprobe", "ifb", "numifbs=0"], check=False)
                result = self._run(
                    ["ip", "link", "add", self.ifb_device, "type", "ifb"],
                    undo=(f"delete {self.ifb_device}", ["ip", "link", "del", self.ifb_device]),
                    check=False,
                )
                if not result.ok:
                    return False
        up = self._run(["ip", "link", "set", "dev", self.ifb_device, "up"], check=False)
        return up.ok

    # --------------------------------------------------------------- updating
    def apply_bandwidth(self, mbps: float) -> None:
        """Change the RabbitMQ rate limit.

        Raises:
            CommandError: the ``tc`` update failed.
        """
        mbps = max(mbps, MIN_RATE_MBPS)
        self._change_rate(self.interface, classified=True, mbps=mbps)
        if self.ingress_active:
            self._change_rate(self.ifb_device, classified=False, mbps=mbps)
        self.current_mbps = mbps

    def _change_rate(self, device: str, *, classified: bool, mbps: float) -> None:
        latency = f"{self.config.network.latency_ms:g}ms"
        if self.config.network.shaper == "htb":
            argv = [
                "class", "change", "dev", device, "parent", ROOT_HANDLE, "classid", SHAPED_CLASSID_HTB,
                "htb", "rate", rate_arg(mbps), "ceil", rate_arg(mbps),
                "burst", f"{burst_bytes(mbps)}b", "cburst", f"{burst_bytes(mbps)}b",
                "quantum", str(quantum_bytes(mbps)), "prio", "1",
            ]
        elif classified:
            argv = [
                "qdisc", "change", "dev", device, "parent", SHAPED_BAND_TBF, "handle", TBF_HANDLE,
                "tbf", "rate", rate_arg(mbps), "burst", f"{burst_bytes(mbps)}b", "latency", latency,
            ]
        else:
            argv = [
                "qdisc", "change", "dev", device, "root", "handle", ROOT_HANDLE,
                "tbf", "rate", rate_arg(mbps), "burst", f"{burst_bytes(mbps)}b", "latency", latency,
            ]
        self._tc(argv, hint=f"cannot update the RabbitMQ rate on {device}")

    # --------------------------------------------------------------- teardown
    def teardown(self, reason: str = "shutdown") -> RestorationReport:
        """Undo every change, then verify that the host is back to normal."""
        report = RestorationReport(reason=reason, interface=self.interface, started_at=iso_timestamp())
        deadline = time.monotonic() + self.config.runtime.restore_timeout_sec

        if not self.journal.ops and not self.applied:
            report.checks = [RestorationCheck("nothing to restore", True, "no shaping rules were installed")]
            report.finished_at = iso_timestamp()
            report.verified = True
            return report

        for op in reversed(self.journal.ops):
            if time.monotonic() > deadline:
                report.errors.append(
                    f"restore timeout ({self.config.runtime.restore_timeout_sec}s) exceeded; "
                    f"{op.description!r} and any remaining undo steps were not executed"
                )
                break
            try:
                result = self.runner.run(op.argv, check=False, ignore=op.ignore, timeout=10.0)
                report.ops_executed += 1
                if not result.ok:
                    report.errors.append(f"{op.description}: {result.stderr.strip() or 'unknown error'}")
                else:
                    self.logger.debug("undo ok: %s", op.description)
            except (ToolNotFoundError, OSError) as exc:  # pragma: no cover - defensive
                report.errors.append(f"{op.description}: {exc}")

        self.applied = False
        self.ingress_active = False

        if self.config.runtime.verify_restore:
            report.checks = self.verify_restoration()
            report.verified = True

        report.finished_at = iso_timestamp()
        if report.success:
            self.journal.clear()
        else:
            self.logger.error("restoration incomplete; rollback journal kept at %s", self.journal.path)
        return report

    def sweep(self) -> list[str]:
        """Best-effort removal of anything this project could have left behind.

        Used by ``main.py restore --force``; safe to run when nothing is installed.
        """
        errors: list[str] = []
        commands: list[tuple[str, list[str]]] = [
            ("remove root qdisc", ["tc", "qdisc", "del", "dev", self.interface, "root"]),
            ("remove ingress qdisc", ["tc", "qdisc", "del", "dev", self.interface, "ingress"]),
        ]
        for binary in ("iptables", "ip6tables"):
            if not self.runner.has(binary):
                continue
            # Repeated --force starts can stack several identical jumps.
            for _attempt in range(5):
                result = self.runner.run(
                    [binary, "-t", "mangle", "-D", "POSTROUTING", "-o", self.interface, "-j", MANGLE_CHAIN],
                    check=False,
                    ignore=_ALREADY_GONE,
                    timeout=10.0,
                )
                if result.returncode != 0:
                    break
            commands += [
                (f"flush {binary} chain", [binary, "-t", "mangle", "-F", MANGLE_CHAIN]),
                (f"delete {binary} chain", [binary, "-t", "mangle", "-X", MANGLE_CHAIN]),
            ]
        if not self.runner.dry_run and interface_exists(self.ifb_device):
            commands.append((f"delete {self.ifb_device}", ["ip", "link", "del", self.ifb_device]))

        for description, argv in commands:
            try:
                result = self.runner.run(argv, check=False, ignore=_ALREADY_GONE, timeout=10.0)
                if not result.ok:
                    errors.append(f"{description}: {result.stderr.strip() or 'unknown error'}")
            except (ToolNotFoundError, OSError) as exc:  # pragma: no cover - defensive
                errors.append(f"{description}: {exc}")
        return errors

    # ----------------------------------------------------------- verification
    def verify_restoration(self) -> list[RestorationCheck]:
        """Compare the live state against the pre-run baseline."""
        checks: list[RestorationCheck] = []
        current = self.capture_snapshot()
        baseline = self.baseline

        if baseline is None:
            # No pre-run snapshot (e.g. `restore` after a reboot): fall back to
            # checking that none of *our* objects are left behind.
            checks.append(
                RestorationCheck(
                    "baseline snapshot",
                    True,
                    "not available - only residue checks were performed",
                )
            )
            checks.extend(self._residue_checks(current))
            return checks

        for name, before, after, normalizer in (
            ("qdisc state", baseline.qdisc, current.qdisc, normalize_tc),
            ("class state", baseline.classes, current.classes, normalize_tc),
            ("filter state", baseline.filters, current.filters, normalize_tc),
            ("ingress filters", baseline.ingress_filters, current.ingress_filters, normalize_tc),
            ("iptables mangle", baseline.iptables_mangle, current.iptables_mangle, normalize_iptables),
            ("ip6tables mangle", baseline.ip6tables_mangle, current.ip6tables_mangle, normalize_iptables),
        ):
            before_lines, after_lines = normalizer(before), normalizer(after)
            passed = before_lines == after_lines
            detail = "" if passed else unified_diff(before_lines, after_lines)
            checks.append(RestorationCheck(name, passed, detail))

        chain_present = MANGLE_CHAIN in current.iptables_mangle or MANGLE_CHAIN in current.ip6tables_mangle
        checks.append(
            RestorationCheck(
                f"{MANGLE_CHAIN} chain removed",
                not chain_present,
                "" if not chain_present else f"the {MANGLE_CHAIN} chain still exists in the mangle table",
            )
        )

        ifb_present = (not self.runner.dry_run) and interface_exists(self.ifb_device)
        checks.append(
            RestorationCheck(
                "ifb device removed",
                not ifb_present,
                "" if not ifb_present else f"{self.ifb_device} still exists",
            )
        )
        return checks

    def _residue_checks(self, current: NetworkSnapshot) -> list[RestorationCheck]:
        """Verification used when no pre-run baseline is available."""
        chain_present = MANGLE_CHAIN in current.iptables_mangle or MANGLE_CHAIN in current.ip6tables_mangle
        ifb_present = (not self.runner.dry_run) and interface_exists(self.ifb_device)
        our_qdisc = any(
            line.strip().startswith(("qdisc htb 1:", "qdisc prio 1:")) for line in current.qdisc.splitlines()
        )
        ingress_present = current.has_ingress_qdisc
        return [
            RestorationCheck(
                f"{MANGLE_CHAIN} chain removed",
                not chain_present,
                "" if not chain_present else f"the {MANGLE_CHAIN} chain still exists",
            ),
            RestorationCheck(
                "shaping qdisc removed",
                not our_qdisc,
                "" if not our_qdisc else f"an htb/prio root qdisc is still attached to {self.interface}",
            ),
            RestorationCheck(
                "ingress qdisc removed",
                not ingress_present,
                "" if not ingress_present else f"an ingress qdisc is still attached to {self.interface}",
            ),
            RestorationCheck(
                "ifb device removed",
                not ifb_present,
                "" if not ifb_present else f"{self.ifb_device} still exists",
            ),
        ]

    # ------------------------------------------------------------- inspection
    def describe_rules(self) -> dict[str, str]:
        """Return the live tc / netfilter state for the ``status`` command."""
        capture = self.runner.capture
        rules = {
            f"tc qdisc show dev {self.interface}": capture(["tc", "qdisc", "show", "dev", self.interface]),
            f"tc class show dev {self.interface}": capture(["tc", "class", "show", "dev", self.interface]),
            f"tc filter show dev {self.interface}": capture(["tc", "filter", "show", "dev", self.interface]),
        }
        if self.runner.has("iptables"):
            rules["iptables -t mangle -S " + MANGLE_CHAIN] = capture(
                ["iptables", "-t", "mangle", "-S", MANGLE_CHAIN]
            )
        if not self.runner.dry_run and interface_exists(self.ifb_device):
            rules[f"tc qdisc show dev {self.ifb_device}"] = capture(
                ["tc", "qdisc", "show", "dev", self.ifb_device]
            )
        return rules

    # ---------------------------------------------------------------- private
    def _push_original_qdisc_restore(self) -> None:
        """Queue re-installation of a non-default root qdisc (executed last)."""
        baseline = self.baseline
        if baseline is None or not baseline.root_qdisc_kind:
            return
        if baseline.root_qdisc_kind in REPLACEABLE_QDISCS:
            # The kernel restores these automatically once our root qdisc is
            # deleted, so re-adding them would fail with "file exists".
            return
        argv = self._reconstruct_qdisc_add(baseline.root_qdisc_line)
        if argv:
            self.journal.push(f"re-install original root qdisc ({baseline.root_qdisc_kind})", argv)
            self.logger.info("original root qdisc %s will be re-installed on exit", baseline.root_qdisc_kind)

    def _reconstruct_qdisc_add(self, line: str) -> list[str] | None:
        """Build a ``tc qdisc add`` command from a ``tc qdisc show`` line."""
        parts = line.split()
        if len(parts) < 4 or parts[0] != "qdisc":
            return None
        kind, handle = parts[1], parts[2]
        try:
            root_index = parts.index("root")
        except ValueError:
            return None
        params: list[str] = []
        skip_next = False
        for token in parts[root_index + 1 :]:
            if skip_next:
                skip_next = False
                continue
            if token in ("refcnt", "direct_packets_stat"):
                skip_next = True
                continue
            params.append(token)
        return ["tc", "qdisc", "add", "dev", self.interface, "root", "handle", handle, kind, *params]

    def _tc(
        self,
        args: Sequence[str],
        *,
        undo: tuple[str, Sequence[str]] | None = None,
        check: bool = True,
        hint: str | None = None,
    ) -> CommandResult:
        return self._run(["tc", *args], undo=undo, check=check, hint=hint)

    def _run(
        self,
        argv: Sequence[str],
        *,
        undo: tuple[str, Sequence[str]] | None = None,
        check: bool = True,
        hint: str | None = None,
    ) -> CommandResult:
        """Execute a mutating command and register its undo command first.

        The undo command is journalled *before* execution so a crash between the
        two never leaves an unrecorded rule behind (undoing something that was
        never created is harmless: the failure patterns are tolerated).
        """
        if undo is not None:
            self.journal.push(undo[0], undo[1])
        return self.runner.run(argv, check=check, hint=hint)
