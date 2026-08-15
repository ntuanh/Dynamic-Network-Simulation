"""Example custom bandwidth generator.

Selected from a configuration file with::

    bandwidth:
      distribution: "custom"
      custom:
        module: "scripts/custom_model_example.py"
        callable: "generate"

Two shapes are supported by the loader:

* ``f(config, rng) -> float``            called once per update interval;
* ``f(config, rng) -> Iterator[float]``  a generator, consumed lazily (used here).

``config`` is the :class:`controller.config_loader.BandwidthConfig` dataclass, so
``mean_mbps``, ``std_mbps``, ``min_mbps``, ``max_mbps`` and
``update_interval_sec`` are all available.  ``rng`` is a seeded
:class:`random.Random`, which keeps runs reproducible.

Values returned here are clamped to ``[min_mbps, max_mbps]`` by the framework, so
a model never has to worry about producing an illegal rate.
"""

from __future__ import annotations

import math
import random
from typing import Iterator

from controller.config_loader import BandwidthConfig


def generate(config: BandwidthConfig, rng: random.Random) -> Iterator[float]:
    """A diurnal sine wave plus an AR(1) random walk and occasional dropouts.

    The pattern is deliberately non-Gaussian so that plots make the difference
    obvious: a slow 5-minute cycle, correlated noise, and a 1% chance per tick
    of a short "congestion event" that halves the available bandwidth.
    """
    period_sec = 300.0
    amplitude = config.std_mbps * 2.0
    walk = 0.0
    dropout_ticks = 0
    tick = 0

    while True:
        phase = 2.0 * math.pi * (tick * config.update_interval_sec) / period_sec
        # AR(1): today's noise remembers 80% of yesterday's.
        walk = 0.8 * walk + rng.gauss(0.0, config.std_mbps * 0.4)
        value = config.mean_mbps + amplitude * math.sin(phase) + walk

        if dropout_ticks > 0:
            dropout_ticks -= 1
            value *= 0.5
        elif rng.random() < 0.01:
            dropout_ticks = rng.randint(3, 10)

        tick += 1
        yield value


def constant_with_steps(config: BandwidthConfig, rng: random.Random) -> float:
    """Alternative entry point: a staircase that steps every 30 calls."""
    step = getattr(constant_with_steps, "_step", 0)
    setattr(constant_with_steps, "_step", step + 1)
    level = (step // 30) % 4
    return config.min_mbps + level * (config.max_mbps - config.min_mbps) / 4.0
