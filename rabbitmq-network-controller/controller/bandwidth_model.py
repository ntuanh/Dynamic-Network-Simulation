"""Bandwidth generation models.

Every model produces a stream of bandwidth values in Mbit/s.  Models are
deterministic when ``runtime.random_seed`` is set, which makes experiments
reproducible.

Adding a new model is a three line change::

    @register_model
    class MyModel(BandwidthModel):
        name = "my_model"

        def sample(self) -> float:
            return self.rng.betavariate(2, 5) * self.config.max_mbps

Built-in models
---------------
``constant``   fixed ``mean_mbps``
``gaussian``   ``N(mean_mbps, std_mbps)`` (alias: ``normal``)
``uniform``    ``U(low_mbps, high_mbps)`` defaulting to ``U(min_mbps, max_mbps)``
``lognormal``  log-normal with the given mean/std of the underlying normal
``markov``     discrete-state Markov chain over ``markov.states_mbps``
``trace``      replay of a CSV column (real network traces)
``custom``     user supplied Python callable or generator
"""

from __future__ import annotations

import csv
import importlib
import importlib.util
import math
import random
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ClassVar, Iterator, Type

from .config_loader import BandwidthConfig, ConfigError


class BandwidthModelError(RuntimeError):
    """Raised when a bandwidth model cannot be built or produces no value."""


class BandwidthModel(ABC):
    """Base class for all bandwidth generators."""

    #: Registry key used in ``bandwidth.distribution``.
    name: ClassVar[str] = "abstract"
    #: Aliases accepted in the configuration file.
    aliases: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: BandwidthConfig, rng: random.Random) -> None:
        self.config = config
        self.rng = rng
        self.samples_generated: int = 0
        self.setup()

    # ------------------------------------------------------------------ hooks
    def setup(self) -> None:
        """Optional hook executed once at construction time."""

    @abstractmethod
    def sample(self) -> float:
        """Return the next raw (unclamped) bandwidth value in Mbit/s."""

    # ---------------------------------------------------------------- public
    def next_value(self) -> float:
        """Return the next bandwidth value, clamped and rounded."""
        raw = float(self.sample())
        if math.isnan(raw) or math.isinf(raw):
            raise BandwidthModelError(f"model {self.name!r} produced a non-finite value: {raw}")
        clamped = min(max(raw, self.config.min_mbps), self.config.max_mbps)
        self.samples_generated += 1
        return round(clamped, self.config.round_digits)

    def describe(self) -> str:
        """One-line human readable description used in logs and ``status``."""
        return f"{self.name} in [{self.config.min_mbps}, {self.config.max_mbps}] Mbit/s"

    def __iter__(self) -> Iterator[float]:
        while True:
            yield self.next_value()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
MODEL_REGISTRY: dict[str, Type[BandwidthModel]] = {}


def register_model(cls: Type[BandwidthModel]) -> Type[BandwidthModel]:
    """Class decorator registering a model under its name and aliases."""
    for key in (cls.name, *cls.aliases):
        MODEL_REGISTRY[key.lower()] = cls
    return cls


# --------------------------------------------------------------------------- #
# Built-in models
# --------------------------------------------------------------------------- #
@register_model
class ConstantModel(BandwidthModel):
    """Always returns ``mean_mbps`` -- a stable link."""

    name = "constant"
    aliases = ("fixed", "static")

    def sample(self) -> float:
        return self.config.mean_mbps

    def describe(self) -> str:
        return f"constant {self.config.mean_mbps} Mbit/s"


@register_model
class GaussianModel(BandwidthModel):
    """``bandwidth ~ N(mean_mbps, std_mbps)`` clamped to [min, max]."""

    name = "gaussian"
    aliases = ("normal",)

    def sample(self) -> float:
        return self.rng.gauss(self.config.mean_mbps, self.config.std_mbps)

    def describe(self) -> str:
        return (
            f"gaussian mean={self.config.mean_mbps} std={self.config.std_mbps} "
            f"clamped to [{self.config.min_mbps}, {self.config.max_mbps}] Mbit/s"
        )


@register_model
class UniformModel(BandwidthModel):
    """``bandwidth ~ U(low, high)``; defaults to ``U(min_mbps, max_mbps)``."""

    name = "uniform"

    def setup(self) -> None:
        self.low: float = self.config.low_mbps if self.config.low_mbps is not None else self.config.min_mbps
        self.high: float = self.config.high_mbps if self.config.high_mbps is not None else self.config.max_mbps
        if self.low > self.high:
            raise ConfigError(
                f"bandwidth.low_mbps ({self.low}) must be <= bandwidth.high_mbps ({self.high})"
            )

    def sample(self) -> float:
        return self.rng.uniform(self.low, self.high)

    def describe(self) -> str:
        return f"uniform over [{self.low}, {self.high}] Mbit/s"


@register_model
class LogNormalModel(BandwidthModel):
    """Log-normal process; useful for heavy-tailed link capacity models."""

    name = "lognormal"

    def setup(self) -> None:
        mean = max(self.config.mean_mbps, 1e-6)
        std = max(self.config.std_mbps, 1e-6)
        # Method of moments: convert desired mean/std into the underlying normal.
        variance = math.log(1.0 + (std / mean) ** 2)
        self.mu: float = math.log(mean) - variance / 2.0
        self.sigma: float = math.sqrt(variance)

    def sample(self) -> float:
        return self.rng.lognormvariate(self.mu, self.sigma)

    def describe(self) -> str:
        return f"lognormal mu={self.mu:.4f} sigma={self.sigma:.4f} (target mean={self.config.mean_mbps})"


@register_model
class MarkovModel(BandwidthModel):
    """Discrete-state Markov chain over a set of bandwidth levels."""

    name = "markov"

    def setup(self) -> None:
        markov = self.config.markov
        states = markov.states_mbps
        if not states:
            raise ConfigError("bandwidth.markov.states_mbps must not be empty")
        size = len(states)
        transition = markov.transition
        if not transition:
            # Default: uniform transition matrix.
            transition = tuple(tuple(1.0 / size for _ in range(size)) for _ in range(size))
        if len(transition) != size:
            raise ConfigError(
                f"bandwidth.markov.transition must be {size}x{size} to match states_mbps, "
                f"got {len(transition)} rows"
            )
        for index, row in enumerate(transition):
            if len(row) != size:
                raise ConfigError(f"bandwidth.markov.transition row {index} must have {size} entries")
            total = sum(row)
            if not math.isclose(total, 1.0, rel_tol=1e-6, abs_tol=1e-6):
                raise ConfigError(
                    f"bandwidth.markov.transition row {index} sums to {total:.6f}, expected 1.0"
                )
            if any(cell < 0 for cell in row):
                raise ConfigError(f"bandwidth.markov.transition row {index} contains a negative probability")
        if not 0 <= markov.start_state < size:
            raise ConfigError(
                f"bandwidth.markov.start_state must be in [0, {size - 1}], got {markov.start_state}"
            )
        self.states: tuple[float, ...] = states
        self.transition: tuple[tuple[float, ...], ...] = transition
        self.state: int = markov.start_state
        self.jitter: float = markov.jitter_std_mbps
        self._first: bool = True

    def sample(self) -> float:
        if self._first:
            self._first = False
        else:
            row = self.transition[self.state]
            self.state = self.rng.choices(range(len(self.states)), weights=row, k=1)[0]
        value = self.states[self.state]
        if self.jitter > 0:
            value += self.rng.gauss(0.0, self.jitter)
        return value

    def describe(self) -> str:
        return f"markov chain over {list(self.states)} Mbit/s (current state {self.state})"


def _looks_like_header(sample: str) -> bool:
    """True when the first CSV line contains no parseable number.

    ``csv.Sniffer().has_header`` raises on single-column files, which is exactly
    the "one bandwidth value per line" trace format, so this simpler test is used
    instead.
    """
    for line in sample.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for cell in stripped.split(","):
            try:
                float(cell.strip())
            except ValueError:
                continue
            return False  # a numeric cell on the first data line: no header
        return True
    return False


@register_model
class TraceReplayModel(BandwidthModel):
    """Replay bandwidth values recorded in a CSV file (real network traces)."""

    name = "trace"
    aliases = ("replay", "csv")

    def setup(self) -> None:
        trace = self.config.trace
        path = Path(trace.file)
        if not path.exists():
            raise ConfigError(f"bandwidth.trace.file does not exist: {path}")
        values: list[float] = []
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                sample = handle.read(4096)
                handle.seek(0)
                has_header = _looks_like_header(sample)
                if has_header:
                    reader = csv.DictReader(handle)
                    field_names = reader.fieldnames or []
                    column = trace.column if trace.column in field_names else ""
                    if not column:
                        numeric = [name for name in field_names if name and name != "timestamp"]
                        if not numeric:
                            raise ConfigError(f"{path} has no usable numeric column")
                        column = numeric[0]
                    for row_number, row in enumerate(reader, start=2):
                        raw = (row.get(column) or "").strip()
                        if not raw:
                            continue
                        try:
                            values.append(float(raw) * trace.scale)
                        except ValueError as exc:
                            raise ConfigError(
                                f"{path}:{row_number} column {column!r} is not numeric: {raw!r}"
                            ) from exc
                else:
                    for row_number, row in enumerate(csv.reader(handle), start=1):
                        if not row:
                            continue
                        raw = row[0].strip() if len(row) == 1 else row[-1].strip()
                        if not raw or raw.startswith("#"):
                            continue
                        try:
                            values.append(float(raw) * trace.scale)
                        except ValueError as exc:
                            raise ConfigError(f"{path}:{row_number} is not numeric: {raw!r}") from exc
        except OSError as exc:
            raise ConfigError(f"cannot read bandwidth.trace.file {path}: {exc}") from exc

        if not values:
            raise ConfigError(f"bandwidth.trace.file {path} contained no bandwidth values")
        self.values: list[float] = values
        self.index: int = 0
        self.path: Path = path
        self.exhausted: bool = False

    def sample(self) -> float:
        if self.index >= len(self.values):
            if self.config.trace.loop:
                self.index = 0
            else:
                self.exhausted = True
                return self.values[-1]
        value = self.values[self.index]
        self.index += 1
        return value

    def describe(self) -> str:
        mode = "looping" if self.config.trace.loop else "one-shot"
        return f"trace replay of {len(self.values)} samples from {self.path.name} ({mode})"


@register_model
class CustomModel(BandwidthModel):
    """Delegate generation to a user supplied Python module.

    The configured callable may be either:

    * a plain function ``f(config, rng) -> float`` invoked on every tick, or
    * a generator function ``f(config, rng) -> Iterator[float]`` consumed lazily.
    """

    name = "custom"

    def setup(self) -> None:
        custom = self.config.custom
        module = self._import_module(custom.module)
        factory = getattr(module, custom.callable, None)
        if factory is None:
            raise ConfigError(
                f"bandwidth.custom.callable {custom.callable!r} not found in {custom.module!r}"
            )
        if not callable(factory):
            raise ConfigError(f"{custom.module}:{custom.callable} is not callable")
        self.factory: Callable[..., object] = factory
        self.iterator: Iterator[float] | None = None
        produced = factory(self.config, self.rng)
        if hasattr(produced, "__next__"):
            self.iterator = produced  # type: ignore[assignment]
        else:
            self._first_value: float | None = float(produced)  # type: ignore[arg-type]

    @staticmethod
    def _import_module(reference: str):  # type: ignore[no-untyped-def]
        """Import a dotted module path or a standalone ``.py`` file."""
        path = Path(reference).expanduser()
        if path.suffix == ".py":
            if not path.is_absolute():
                path = (Path.cwd() / path).resolve()
            if not path.exists():
                raise ConfigError(f"bandwidth.custom.module file not found: {path}")
            spec = importlib.util.spec_from_file_location(path.stem, path)
            if spec is None or spec.loader is None:
                raise ConfigError(f"cannot import bandwidth.custom.module from {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
        try:
            return importlib.import_module(reference)
        except ImportError as exc:
            raise ConfigError(f"cannot import bandwidth.custom.module {reference!r}: {exc}") from exc

    def sample(self) -> float:
        if self.iterator is not None:
            try:
                return float(next(self.iterator))
            except StopIteration:
                raise BandwidthModelError(
                    f"custom generator {self.config.custom.module}:{self.config.custom.callable} "
                    "stopped producing values"
                ) from None
        first = getattr(self, "_first_value", None)
        if first is not None:
            self._first_value = None
            return first
        return float(self.factory(self.config, self.rng))  # type: ignore[arg-type]

    def describe(self) -> str:
        return f"custom model {self.config.custom.module}:{self.config.custom.callable}"


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelInfo:
    """Metadata about the model that ended up being used."""

    name: str
    description: str
    seed: int | None


def create_model(config: BandwidthConfig, seed: int | None = None) -> BandwidthModel:
    """Instantiate the model selected by ``bandwidth.distribution``.

    Raises:
        ConfigError: unknown distribution or invalid model parameters.
    """
    key = config.distribution.lower()
    model_class = MODEL_REGISTRY.get(key)
    if model_class is None:
        available = ", ".join(sorted(MODEL_REGISTRY))
        raise ConfigError(f"unknown bandwidth.distribution {config.distribution!r}; available: {available}")
    rng = random.Random(seed)
    return model_class(config, rng)


def available_models() -> tuple[str, ...]:
    """Names (and aliases) of all registered models."""
    return tuple(sorted(MODEL_REGISTRY))
