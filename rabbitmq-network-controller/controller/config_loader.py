"""Typed configuration loading and validation.

The YAML file is parsed into frozen dataclasses so the rest of the application
works with validated, immutable, fully typed objects instead of nested dicts.

Relative paths inside the configuration are resolved against the *project root*
(the directory containing ``main.py``), so the application behaves identically
regardless of the working directory it was launched from -- important because
``sudo`` is often invoked from elsewhere.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Iterable, Mapping, Sequence

import yaml

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: Distribution names understood by :mod:`controller.bandwidth_model`.
KNOWN_DISTRIBUTIONS: Final[tuple[str, ...]] = (
    "constant",
    "gaussian",
    "normal",
    "uniform",
    "lognormal",
    "markov",
    "trace",
    "custom",
)

KNOWN_SHAPERS: Final[tuple[str, ...]] = ("htb", "tbf")
KNOWN_CLASSIFIERS: Final[tuple[str, ...]] = ("fwmark", "u32")


class ConfigError(ValueError):
    """Raised when the configuration file is missing, malformed or invalid."""


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NetworkConfig:
    """Host networking parameters."""

    interface: str = "auto"
    #: Also shape traffic *entering* the host by redirecting it to an IFB device.
    shape_ingress: bool = False
    #: Name of the intermediate functional block device used for ingress shaping.
    ifb_device: str = "ifb-rmq0"
    #: ``fwmark`` (iptables/nftables marking, recommended) or ``u32`` (pure tc).
    classification: str = "fwmark"
    #: ``htb`` (hierarchical token bucket) or ``tbf`` (token bucket filter).
    shaper: str = "htb"
    #: Firewall mark applied to RabbitMQ packets.
    fwmark: int = 0x2A
    #: Mask so that only our bits of ``skb->mark`` are touched.
    fwmask: int = 0xFF
    #: Also install IPv6 rules (ip6tables + ``protocol ipv6`` tc filters).
    enable_ipv6: bool = True
    #: Ceiling for *unshaped* traffic, in Mbit/s. ``0`` means auto-detect.
    unshaped_ceil_mbps: float = 0.0
    #: Queue latency budget used by tbf and htb burst computation.
    latency_ms: float = 50.0
    #: Leaf qdisc placed under the shaped class (``fq_codel``, ``pfifo``, ``sfq``).
    leaf_qdisc: str = "fq_codel"
    #: Refuse to start when a foreign root qdisc is already installed.
    protect_existing_qdisc: bool = True


@dataclass(frozen=True)
class RabbitMQConfig:
    """How RabbitMQ traffic is recognised on the wire."""

    ports: tuple[int, ...] = (5672, 15672)
    #: ``both`` (source or destination port), ``dst`` or ``src``.
    match_direction: str = "both"

    def __post_init__(self) -> None:
        if not self.ports:
            raise ConfigError("rabbitmq.ports must contain at least one TCP port")
        for port in self.ports:
            if not 1 <= port <= 65535:
                raise ConfigError(f"rabbitmq.ports contains an invalid TCP port: {port}")
        if self.match_direction not in ("both", "dst", "src"):
            raise ConfigError("rabbitmq.match_direction must be one of: both, dst, src")


@dataclass(frozen=True)
class NetemConfig:
    """Optional impairment applied on top of the bandwidth limit."""

    enabled: bool = False
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_pct: float = 0.0
    duplicate_pct: float = 0.0
    reorder_pct: float = 0.0
    distribution: str = ""  # e.g. "normal", "pareto" - only used with jitter

    @property
    def active(self) -> bool:
        """True when netem would actually change anything."""
        return self.enabled and any(
            value > 0
            for value in (self.delay_ms, self.jitter_ms, self.loss_pct, self.duplicate_pct, self.reorder_pct)
        )

    def tc_args(self) -> list[str]:
        """Render the netem parameters as ``tc`` arguments."""
        args: list[str] = []
        if self.delay_ms > 0 or self.jitter_ms > 0:
            args += ["delay", f"{self.delay_ms}ms"]
            if self.jitter_ms > 0:
                args.append(f"{self.jitter_ms}ms")
                if self.distribution:
                    args += ["distribution", self.distribution]
        if self.loss_pct > 0:
            args += ["loss", f"{self.loss_pct}%"]
        if self.duplicate_pct > 0:
            args += ["duplicate", f"{self.duplicate_pct}%"]
        if self.reorder_pct > 0:
            args += ["reorder", f"{self.reorder_pct}%"]
        return args


@dataclass(frozen=True)
class MarkovConfig:
    """Discrete-state Markov chain bandwidth model."""

    states_mbps: tuple[float, ...] = ()
    transition: tuple[tuple[float, ...], ...] = ()
    start_state: int = 0
    jitter_std_mbps: float = 0.0


@dataclass(frozen=True)
class TraceConfig:
    """CSV trace replay model."""

    file: str = ""
    column: str = "bandwidth_mbps"
    loop: bool = True
    scale: float = 1.0


@dataclass(frozen=True)
class CustomModelConfig:
    """User supplied Python bandwidth generator."""

    module: str = ""  # dotted path or filesystem path to a .py file
    callable: str = "generate"


@dataclass(frozen=True)
class BandwidthConfig:
    """Bandwidth process definition."""

    distribution: str = "gaussian"
    mean_mbps: float = 20.0
    std_mbps: float = 5.0
    min_mbps: float = 1.0
    max_mbps: float = 100.0
    update_interval_sec: float = 1.0
    #: Bounds for the ``uniform`` distribution (default: [min, max]).
    low_mbps: float | None = None
    high_mbps: float | None = None
    #: Round generated values to this many decimals before applying them.
    round_digits: int = 3
    markov: MarkovConfig = field(default_factory=MarkovConfig)
    trace: TraceConfig = field(default_factory=TraceConfig)
    custom: CustomModelConfig = field(default_factory=CustomModelConfig)


@dataclass(frozen=True)
class LoggingConfig:
    """Log destinations."""

    csv_file: Path = PROJECT_ROOT / "logs" / "bandwidth_history.csv"
    json_file: Path = PROJECT_ROOT / "logs" / "bandwidth_history.json"
    app_log_file: Path = PROJECT_ROOT / "logs" / "controller.log"
    restoration_report_file: Path = PROJECT_ROOT / "logs" / "restoration_report.json"
    level: str = "INFO"
    console: bool = True
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5
    #: Rewrite the JSON history every N records (it is always flushed on exit).
    json_flush_every: int = 10


@dataclass(frozen=True)
class RuntimeConfig:
    """Process lifetime and on-disk state."""

    #: ``0`` or ``null`` means "run until stopped".
    duration_sec: float = 3600.0
    random_seed: int | None = 42
    backup_dir: Path = PROJECT_ROOT / "runtime_backup"
    state_file: Path = PROJECT_ROOT / "runtime_backup" / "state.json"
    pid_file: Path = PROJECT_ROOT / "runtime_backup" / "controller.pid"
    journal_file: Path = PROJECT_ROOT / "runtime_backup" / "rollback.json"
    #: Verify that the network was restored to its pre-run state on shutdown.
    verify_restore: bool = True
    #: Seconds allowed for the whole teardown sequence.
    restore_timeout_sec: float = 30.0


@dataclass(frozen=True)
class AppConfig:
    """Fully validated application configuration."""

    network: NetworkConfig = field(default_factory=NetworkConfig)
    rabbitmq: RabbitMQConfig = field(default_factory=RabbitMQConfig)
    bandwidth: BandwidthConfig = field(default_factory=BandwidthConfig)
    netem: NetemConfig = field(default_factory=NetemConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    source_path: Path | None = None
    warnings: tuple[str, ...] = ()

    def with_interface(self, interface: str) -> "AppConfig":
        """Return a copy with the network interface replaced (used by ``auto``)."""
        return dataclasses.replace(self, network=dataclasses.replace(self.network, interface=interface))

    def ensure_directories(self) -> None:
        """Create every directory the application writes into."""
        for path in (
            self.logging.csv_file,
            self.logging.json_file,
            self.logging.app_log_file,
            self.logging.restoration_report_file,
            self.runtime.state_file,
            self.runtime.pid_file,
            self.runtime.journal_file,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime.backup_dir.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _resolve_path(value: str | Path, *, base: Path = PROJECT_ROOT) -> Path:
    """Resolve ``value`` against the project root when it is relative."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path)


def _section(data: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Return a mapping section, tolerating ``None`` and missing keys."""
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"configuration section {key!r} must be a mapping, got {type(value).__name__}")
    return dict(value)


def _unknown_keys(section: Mapping[str, Any], known: Iterable[str], prefix: str) -> list[str]:
    return [f"unknown configuration key: {prefix}.{key}" for key in section if key not in set(known)]


def _as_float(section: Mapping[str, Any], key: str, default: float, prefix: str) -> float:
    value = section.get(key, default)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{prefix}.{key} must be a number, got {value!r}") from exc


def _as_int(section: Mapping[str, Any], key: str, default: int, prefix: str) -> int:
    value = section.get(key, default)
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return int(value, 0)  # supports "0x2a"
        except ValueError as exc:
            raise ConfigError(f"{prefix}.{key} must be an integer, got {value!r}") from exc
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{prefix}.{key} must be an integer, got {value!r}") from exc


def _as_bool(section: Mapping[str, Any], key: str, default: bool, prefix: str) -> bool:
    value = section.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    if value is None:
        return default
    raise ConfigError(f"{prefix}.{key} must be a boolean, got {value!r}")


def _as_str(section: Mapping[str, Any], key: str, default: str, prefix: str) -> str:
    value = section.get(key, default)
    if value is None:
        return default
    if not isinstance(value, (str, int, float)):
        raise ConfigError(f"{prefix}.{key} must be a string, got {type(value).__name__}")
    return str(value).strip()


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #
def _build_network(data: Mapping[str, Any], warnings: list[str]) -> NetworkConfig:
    prefix = "network"
    known = (
        "interface",
        "shape_ingress",
        "ifb_device",
        "classification",
        "shaper",
        "fwmark",
        "fwmask",
        "enable_ipv6",
        "unshaped_ceil_mbps",
        "latency_ms",
        "leaf_qdisc",
        "protect_existing_qdisc",
    )
    warnings.extend(_unknown_keys(data, known, prefix))
    defaults = NetworkConfig()

    classification = _as_str(data, "classification", defaults.classification, prefix).lower()
    if classification not in KNOWN_CLASSIFIERS:
        raise ConfigError(
            f"network.classification must be one of {KNOWN_CLASSIFIERS}, got {classification!r}"
        )
    shaper = _as_str(data, "shaper", defaults.shaper, prefix).lower()
    if shaper not in KNOWN_SHAPERS:
        raise ConfigError(f"network.shaper must be one of {KNOWN_SHAPERS}, got {shaper!r}")

    interface = _as_str(data, "interface", defaults.interface, prefix)
    if not interface:
        raise ConfigError("network.interface must not be empty (use 'auto' to detect it)")
    if len(interface) > 15:
        raise ConfigError(f"network.interface {interface!r} exceeds the 15 character Linux limit")

    ifb_device = _as_str(data, "ifb_device", defaults.ifb_device, prefix)
    if len(ifb_device) > 15:
        raise ConfigError(f"network.ifb_device {ifb_device!r} exceeds the 15 character Linux limit")

    fwmark = _as_int(data, "fwmark", defaults.fwmark, prefix)
    fwmask = _as_int(data, "fwmask", defaults.fwmask, prefix)
    if not 0 < fwmark <= 0xFFFFFFFF:
        raise ConfigError("network.fwmark must be a positive 32-bit integer")
    if not 0 < fwmask <= 0xFFFFFFFF:
        raise ConfigError("network.fwmask must be a positive 32-bit integer")
    if fwmark & fwmask != fwmark:
        raise ConfigError(
            f"network.fwmark (0x{fwmark:x}) has bits outside network.fwmask (0x{fwmask:x}); "
            "widen the mask or lower the mark"
        )

    return NetworkConfig(
        interface=interface,
        shape_ingress=_as_bool(data, "shape_ingress", defaults.shape_ingress, prefix),
        ifb_device=ifb_device,
        classification=classification,
        shaper=shaper,
        fwmark=fwmark,
        fwmask=fwmask,
        enable_ipv6=_as_bool(data, "enable_ipv6", defaults.enable_ipv6, prefix),
        unshaped_ceil_mbps=_as_float(data, "unshaped_ceil_mbps", defaults.unshaped_ceil_mbps, prefix),
        latency_ms=_as_float(data, "latency_ms", defaults.latency_ms, prefix),
        leaf_qdisc=_as_str(data, "leaf_qdisc", defaults.leaf_qdisc, prefix),
        protect_existing_qdisc=_as_bool(data, "protect_existing_qdisc", defaults.protect_existing_qdisc, prefix),
    )


def _build_rabbitmq(data: Mapping[str, Any], warnings: list[str]) -> RabbitMQConfig:
    prefix = "rabbitmq"
    warnings.extend(_unknown_keys(data, ("ports", "match_direction"), prefix))
    defaults = RabbitMQConfig()
    raw_ports = data.get("ports", list(defaults.ports))
    if isinstance(raw_ports, (int, str)):
        raw_ports = [raw_ports]
    if not isinstance(raw_ports, Sequence):
        raise ConfigError("rabbitmq.ports must be a list of TCP port numbers")
    ports: list[int] = []
    for item in raw_ports:
        try:
            port = int(item)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"rabbitmq.ports contains a non-numeric entry: {item!r}") from exc
        if port not in ports:
            ports.append(port)
    return RabbitMQConfig(
        ports=tuple(ports),
        match_direction=_as_str(data, "match_direction", defaults.match_direction, prefix).lower(),
    )


def _build_markov(data: Mapping[str, Any], warnings: list[str]) -> MarkovConfig:
    prefix = "bandwidth.markov"
    if not data:
        return MarkovConfig()
    warnings.extend(_unknown_keys(data, ("states_mbps", "transition", "start_state", "jitter_std_mbps"), prefix))
    states_raw = data.get("states_mbps") or ()
    if not isinstance(states_raw, Sequence) or isinstance(states_raw, str):
        raise ConfigError("bandwidth.markov.states_mbps must be a list of numbers")
    states = tuple(float(value) for value in states_raw)

    transition_raw = data.get("transition") or ()
    if not isinstance(transition_raw, Sequence) or isinstance(transition_raw, str):
        raise ConfigError("bandwidth.markov.transition must be a list of rows")
    transition = tuple(tuple(float(cell) for cell in row) for row in transition_raw)
    return MarkovConfig(
        states_mbps=states,
        transition=transition,
        start_state=_as_int(data, "start_state", 0, prefix),
        jitter_std_mbps=_as_float(data, "jitter_std_mbps", 0.0, prefix),
    )


def _build_trace(data: Mapping[str, Any], warnings: list[str]) -> TraceConfig:
    prefix = "bandwidth.trace"
    if not data:
        return TraceConfig()
    warnings.extend(_unknown_keys(data, ("file", "column", "loop", "scale"), prefix))
    defaults = TraceConfig()
    file_value = _as_str(data, "file", defaults.file, prefix)
    return TraceConfig(
        file=str(_resolve_path(file_value)) if file_value else "",
        column=_as_str(data, "column", defaults.column, prefix),
        loop=_as_bool(data, "loop", defaults.loop, prefix),
        scale=_as_float(data, "scale", defaults.scale, prefix),
    )


def _build_custom(data: Mapping[str, Any], warnings: list[str]) -> CustomModelConfig:
    prefix = "bandwidth.custom"
    if not data:
        return CustomModelConfig()
    warnings.extend(_unknown_keys(data, ("module", "callable"), prefix))
    defaults = CustomModelConfig()
    return CustomModelConfig(
        module=_as_str(data, "module", defaults.module, prefix),
        callable=_as_str(data, "callable", defaults.callable, prefix),
    )


def _build_bandwidth(data: Mapping[str, Any], warnings: list[str]) -> BandwidthConfig:
    prefix = "bandwidth"
    known = (
        "distribution",
        "mean_mbps",
        "std_mbps",
        "min_mbps",
        "max_mbps",
        "update_interval_sec",
        "low_mbps",
        "high_mbps",
        "round_digits",
        "markov",
        "trace",
        "custom",
    )
    warnings.extend(_unknown_keys(data, known, prefix))
    defaults = BandwidthConfig()

    distribution = _as_str(data, "distribution", defaults.distribution, prefix).lower()
    if distribution not in KNOWN_DISTRIBUTIONS:
        raise ConfigError(
            f"bandwidth.distribution must be one of {KNOWN_DISTRIBUTIONS}, got {distribution!r}"
        )

    min_mbps = _as_float(data, "min_mbps", defaults.min_mbps, prefix)
    max_mbps = _as_float(data, "max_mbps", defaults.max_mbps, prefix)
    mean_mbps = _as_float(data, "mean_mbps", defaults.mean_mbps, prefix)
    std_mbps = _as_float(data, "std_mbps", defaults.std_mbps, prefix)
    interval = _as_float(data, "update_interval_sec", defaults.update_interval_sec, prefix)

    if min_mbps <= 0:
        raise ConfigError("bandwidth.min_mbps must be greater than 0 (tc cannot shape to 0 bit/s)")
    if max_mbps < min_mbps:
        raise ConfigError(f"bandwidth.max_mbps ({max_mbps}) must be >= bandwidth.min_mbps ({min_mbps})")
    if std_mbps < 0:
        raise ConfigError("bandwidth.std_mbps must be >= 0")
    if interval <= 0:
        raise ConfigError("bandwidth.update_interval_sec must be greater than 0")
    if interval < 0.1:
        warnings.append(
            f"bandwidth.update_interval_sec={interval}s is very aggressive; "
            "each update issues a netlink call, values below 0.1s are discouraged"
        )
    if not min_mbps <= mean_mbps <= max_mbps:
        warnings.append(
            f"bandwidth.mean_mbps ({mean_mbps}) lies outside [min_mbps, max_mbps] "
            f"([{min_mbps}, {max_mbps}]); every sample will be clamped"
        )

    low = data.get("low_mbps")
    high = data.get("high_mbps")
    bandwidth = BandwidthConfig(
        distribution=distribution,
        mean_mbps=mean_mbps,
        std_mbps=std_mbps,
        min_mbps=min_mbps,
        max_mbps=max_mbps,
        update_interval_sec=interval,
        low_mbps=None if low is None else float(low),
        high_mbps=None if high is None else float(high),
        round_digits=_as_int(data, "round_digits", defaults.round_digits, prefix),
        markov=_build_markov(_section(data, "markov"), warnings),
        trace=_build_trace(_section(data, "trace"), warnings),
        custom=_build_custom(_section(data, "custom"), warnings),
    )

    if distribution == "markov" and not bandwidth.markov.states_mbps:
        raise ConfigError("bandwidth.distribution='markov' requires bandwidth.markov.states_mbps")
    if distribution == "trace" and not bandwidth.trace.file:
        raise ConfigError("bandwidth.distribution='trace' requires bandwidth.trace.file")
    if distribution == "custom" and not bandwidth.custom.module:
        raise ConfigError("bandwidth.distribution='custom' requires bandwidth.custom.module")
    return bandwidth


def _build_netem(data: Mapping[str, Any], warnings: list[str]) -> NetemConfig:
    prefix = "netem"
    if not data:
        return NetemConfig()
    known = ("enabled", "delay_ms", "jitter_ms", "loss_pct", "duplicate_pct", "reorder_pct", "distribution")
    warnings.extend(_unknown_keys(data, known, prefix))
    defaults = NetemConfig()
    return NetemConfig(
        enabled=_as_bool(data, "enabled", defaults.enabled, prefix),
        delay_ms=_as_float(data, "delay_ms", defaults.delay_ms, prefix),
        jitter_ms=_as_float(data, "jitter_ms", defaults.jitter_ms, prefix),
        loss_pct=_as_float(data, "loss_pct", defaults.loss_pct, prefix),
        duplicate_pct=_as_float(data, "duplicate_pct", defaults.duplicate_pct, prefix),
        reorder_pct=_as_float(data, "reorder_pct", defaults.reorder_pct, prefix),
        distribution=_as_str(data, "distribution", defaults.distribution, prefix),
    )


def _build_logging(data: Mapping[str, Any], warnings: list[str]) -> LoggingConfig:
    prefix = "logging"
    known = (
        "csv_file",
        "json_file",
        "app_log_file",
        "restoration_report_file",
        "level",
        "console",
        "max_bytes",
        "backup_count",
        "json_flush_every",
    )
    warnings.extend(_unknown_keys(data, known, prefix))
    defaults = LoggingConfig()
    level = _as_str(data, "level", defaults.level, prefix).upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ConfigError(f"logging.level must be a standard log level, got {level!r}")
    return LoggingConfig(
        csv_file=_resolve_path(_as_str(data, "csv_file", str(defaults.csv_file), prefix)),
        json_file=_resolve_path(_as_str(data, "json_file", str(defaults.json_file), prefix)),
        app_log_file=_resolve_path(_as_str(data, "app_log_file", str(defaults.app_log_file), prefix)),
        restoration_report_file=_resolve_path(
            _as_str(data, "restoration_report_file", str(defaults.restoration_report_file), prefix)
        ),
        level=level,
        console=_as_bool(data, "console", defaults.console, prefix),
        max_bytes=_as_int(data, "max_bytes", defaults.max_bytes, prefix),
        backup_count=_as_int(data, "backup_count", defaults.backup_count, prefix),
        json_flush_every=max(1, _as_int(data, "json_flush_every", defaults.json_flush_every, prefix)),
    )


def _build_runtime(data: Mapping[str, Any], warnings: list[str]) -> RuntimeConfig:
    prefix = "runtime"
    known = (
        "duration_sec",
        "random_seed",
        "backup_dir",
        "state_file",
        "pid_file",
        "journal_file",
        "verify_restore",
        "restore_timeout_sec",
    )
    warnings.extend(_unknown_keys(data, known, prefix))
    defaults = RuntimeConfig()
    seed_raw = data.get("random_seed", defaults.random_seed)
    seed = None if seed_raw is None else int(seed_raw)
    backup_dir = _resolve_path(_as_str(data, "backup_dir", str(defaults.backup_dir), prefix))
    return RuntimeConfig(
        duration_sec=_as_float(data, "duration_sec", defaults.duration_sec, prefix),
        random_seed=seed,
        backup_dir=backup_dir,
        state_file=_resolve_path(_as_str(data, "state_file", str(backup_dir / "state.json"), prefix)),
        pid_file=_resolve_path(_as_str(data, "pid_file", str(backup_dir / "controller.pid"), prefix)),
        journal_file=_resolve_path(_as_str(data, "journal_file", str(backup_dir / "rollback.json"), prefix)),
        verify_restore=_as_bool(data, "verify_restore", defaults.verify_restore, prefix),
        restore_timeout_sec=_as_float(data, "restore_timeout_sec", defaults.restore_timeout_sec, prefix),
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def load_config(path: str | Path) -> AppConfig:
    """Load, validate and return the application configuration.

    Raises:
        ConfigError: the file is missing, is not valid YAML, or a value is invalid.
    """
    config_path = Path(path).expanduser()
    if not config_path.is_absolute():
        candidate = Path.cwd() / config_path
        config_path = candidate if candidate.exists() else PROJECT_ROOT / config_path
    if not config_path.exists():
        raise ConfigError(
            f"configuration file not found: {config_path}\n"
            f"       copy the shipped example first: cp example_config.yaml config/config.yaml"
        )
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {config_path}: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")

    warnings: list[str] = []
    warnings.extend(
        _unknown_keys(raw, ("network", "rabbitmq", "bandwidth", "netem", "logging", "runtime"), "<root>")
    )

    config = AppConfig(
        network=_build_network(_section(raw, "network"), warnings),
        rabbitmq=_build_rabbitmq(_section(raw, "rabbitmq"), warnings),
        bandwidth=_build_bandwidth(_section(raw, "bandwidth"), warnings),
        netem=_build_netem(_section(raw, "netem"), warnings),
        logging=_build_logging(_section(raw, "logging"), warnings),
        runtime=_build_runtime(_section(raw, "runtime"), warnings),
        source_path=config_path,
        warnings=tuple(warnings),
    )
    return config


def config_to_dict(config: AppConfig) -> dict[str, Any]:
    """Serialise a configuration back into plain JSON-compatible types."""

    def convert(value: Any) -> Any:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {key: convert(item) for key, item in dataclasses.asdict(value).items()}
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value

    return convert(config)  # type: ignore[return-value]
