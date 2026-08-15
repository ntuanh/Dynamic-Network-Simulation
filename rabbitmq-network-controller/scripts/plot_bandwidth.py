#!/usr/bin/env python3
"""Plot the bandwidth history recorded by the controller.

    python3 scripts/plot_bandwidth.py
    python3 scripts/plot_bandwidth.py --csv logs/bandwidth_history.csv \
        --output results/bandwidth_over_time.png
    python3 scripts/plot_bandwidth.py --config config/config.yaml --theme dark

Produces ``results/bandwidth_over_time.png``: a step chart of the applied rate
over time (the rate is piecewise-constant between updates, so steps -- not a
smooth line -- are the honest rendering) plus the distribution of the applied
values underneath.

Only matplotlib is required; the CSV is parsed with the standard library.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Sequence

import matplotlib

matplotlib.use("Agg")  # headless: works over SSH and in CI
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: Status values that represent a rate that was actually programmed.
APPLIED_STATUSES: Final[frozenset[str]] = frozenset({"applied", "dry-run"})


@dataclass(frozen=True)
class Theme:
    """Colour tokens for one rendering mode."""

    surface: str
    ink: str
    secondary: str
    muted: str
    grid: str
    baseline: str
    series: str
    series_fill: str
    critical: str


LIGHT: Final[Theme] = Theme(
    surface="#fcfcfb",
    ink="#0b0b0b",
    secondary="#52514e",
    muted="#898781",
    grid="#e1e0d9",
    baseline="#c3c2b7",
    series="#2a78d6",
    series_fill="#cde2fb",
    critical="#d03b3b",
)

DARK: Final[Theme] = Theme(
    surface="#1a1a19",
    ink="#ffffff",
    secondary="#c3c2b7",
    muted="#898781",
    grid="#2c2c2a",
    baseline="#383835",
    series="#3987e5",
    series_fill="#184f95",
    critical="#d03b3b",
)


@dataclass(frozen=True)
class Sample:
    """One row of ``logs/bandwidth_history.csv``."""

    moment: datetime | None
    bandwidth_mbps: float
    interface: str
    status: str


def read_samples(path: Path) -> list[Sample]:
    """Parse the CSV history.

    Raises:
        SystemExit: the file is missing or contains no usable rows.
    """
    if not path.exists():
        raise SystemExit(
            f"error: {path} does not exist.\n"
            f"       run the controller first: sudo python3 main.py start --config config/config.yaml"
        )
    samples: list[Sample] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            raw_value = (row.get("bandwidth_mbps") or "").strip()
            if not raw_value:
                continue
            try:
                value = float(raw_value)
            except ValueError:
                continue
            moment: datetime | None
            try:
                moment = datetime.fromisoformat((row.get("timestamp") or "").strip())
            except ValueError:
                moment = None
            samples.append(
                Sample(
                    moment=moment,
                    bandwidth_mbps=value,
                    interface=(row.get("interface") or "").strip(),
                    status=(row.get("status") or "").strip(),
                )
            )
    if not samples:
        raise SystemExit(f"error: {path} contains no bandwidth samples yet")
    return samples


def _statistics(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(values)
    count = len(ordered)
    mean = sum(ordered) / count
    variance = sum((value - mean) ** 2 for value in ordered) / count
    return {
        "count": count,
        "mean": mean,
        "std": variance**0.5,
        "min": ordered[0],
        "max": ordered[-1],
        "p95": ordered[min(count - 1, int(0.95 * count))],
    }


def _style_axes(axes: plt.Axes, theme: Theme, *, x_grid: bool = False) -> None:
    """Recessive chrome: hairline horizontal grid, no top/right spines."""
    axes.set_facecolor(theme.surface)
    axes.grid(axis="y", color=theme.grid, linewidth=0.8, alpha=1.0)
    if x_grid:
        axes.grid(axis="x", color=theme.grid, linewidth=0.8)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        axes.spines[side].set_color(theme.baseline)
        axes.spines[side].set_linewidth(1.0)
    axes.tick_params(colors=theme.muted, labelsize=9, length=4, width=0.8)
    for label in (*axes.get_xticklabels(), *axes.get_yticklabels()):
        label.set_color(theme.secondary)


def plot(
    samples: list[Sample],
    output: Path,
    *,
    theme: Theme,
    title: str,
    dpi: int,
    width: float,
    height: float,
) -> dict[str, float]:
    """Render the figure and return the summary statistics."""
    indexed = list(enumerate(samples))
    applied_pairs = [(index, sample) for index, sample in indexed if sample.status in APPLIED_STATUSES]
    failed_pairs = [(index, sample) for index, sample in indexed if sample.status == "failed"]
    applied = [sample for _index, sample in applied_pairs]
    if not applied:
        raise SystemExit("error: no successfully applied bandwidth values to plot")

    # Fall back to update numbers when any timestamp failed to parse.
    use_time = all(sample.moment is not None for sample in samples)
    x_label = "time" if use_time else "update #"
    x_values: list[object] = [
        sample.moment if use_time else index for index, sample in applied_pairs
    ]
    y_values = [sample.bandwidth_mbps for sample in applied]
    stats = _statistics(y_values)
    interface = next((sample.interface for sample in applied if sample.interface), "?")

    plt.rcParams["font.family"] = ["DejaVu Sans"]
    figure = plt.figure(figsize=(width, height), facecolor=theme.surface, dpi=dpi)
    grid = figure.add_gridspec(2, 1, height_ratios=(3, 1), hspace=0.38)
    timeline = figure.add_subplot(grid[0])
    distribution = figure.add_subplot(grid[1])

    # ---------------------------------------------------------------- timeline
    timeline.fill_between(
        x_values, y_values, step="post", color=theme.series_fill, alpha=0.28, linewidth=0
    )
    timeline.plot(
        x_values,
        y_values,
        drawstyle="steps-post",
        color=theme.series,
        linewidth=1.8 if len(y_values) <= 200 else 1.2,
        solid_capstyle="round",
    )
    timeline.axhline(stats["mean"], color=theme.secondary, linewidth=1.0, linestyle=(0, (4, 3)))
    timeline.annotate(
        f"mean {stats['mean']:.1f} Mbit/s",
        xy=(1.0, stats["mean"]),
        xycoords=("axes fraction", "data"),
        xytext=(-2, 5),
        textcoords="offset points",
        ha="right",
        va="bottom",
        fontsize=9,
        color=theme.secondary,
        bbox={"facecolor": theme.surface, "edgecolor": "none", "pad": 1.5},
        zorder=6,
    )

    if failed_pairs:
        timeline.scatter(
            [sample.moment if use_time else index for index, sample in failed_pairs],
            [sample.bandwidth_mbps for _index, sample in failed_pairs],
            s=34,
            color=theme.critical,
            edgecolor=theme.surface,
            linewidth=1.5,
            zorder=5,
            label=f"failed to apply ({len(failed_pairs)})",
        )
        legend = timeline.legend(
            loc="upper left",
            frameon=True,
            facecolor=theme.surface,
            edgecolor="none",
            framealpha=0.9,
            fontsize=9,
            labelcolor=theme.secondary,
        )
        legend.set_zorder(6)

    timeline.set_ylabel("bandwidth (Mbit/s)", color=theme.secondary, fontsize=10)
    if not use_time:
        timeline.set_xlabel(x_label, color=theme.muted, fontsize=9)
    timeline.set_ylim(0, max(y_values) * 1.18)
    _style_axes(timeline, theme)
    if use_time:
        locator = mdates.AutoDateLocator(minticks=4, maxticks=9)
        timeline.xaxis.set_major_locator(locator)
        timeline.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    span = ""
    if use_time and len(applied) > 1:
        seconds = (applied[-1].moment - applied[0].moment).total_seconds()  # type: ignore[operator]
        span = f" over {seconds / 60:.1f} min" if seconds >= 60 else f" over {seconds:.0f} s"
    figure.suptitle(title, x=0.065, y=0.975, ha="left", fontsize=15, color=theme.ink, weight="bold")
    timeline.set_title(
        f"{stats['count']:.0f} updates on {interface}{span}   ·   "
        f"min {stats['min']:.1f}  ·  mean {stats['mean']:.1f}  ·  p95 {stats['p95']:.1f}  ·  "
        f"max {stats['max']:.1f} Mbit/s  ·  σ {stats['std']:.2f}",
        loc="left",
        fontsize=9.5,
        color=theme.muted,
        pad=12,
    )

    # ------------------------------------------------------------ distribution
    bins = max(10, min(40, int(len(y_values) ** 0.5) + 1))
    distribution.hist(
        y_values,
        bins=bins,
        color=theme.series,
        edgecolor=theme.surface,
        linewidth=1.4,
    )
    distribution.set_xlabel("bandwidth (Mbit/s)", color=theme.secondary, fontsize=10)
    distribution.set_ylabel("updates", color=theme.secondary, fontsize=10)
    distribution.set_title("distribution of applied rates", loc="left", fontsize=9.5, color=theme.muted, pad=8)
    _style_axes(distribution, theme)

    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=dpi, facecolor=theme.surface, bbox_inches="tight")
    plt.close(figure)
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plot bandwidth_history.csv as results/bandwidth_over_time.png"
    )
    parser.add_argument("--csv", type=Path, help="history CSV (default: logs/bandwidth_history.csv)")
    parser.add_argument("--config", type=Path, help="read the CSV path from this YAML configuration")
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "results" / "bandwidth_over_time.png",
        help="output PNG path",
    )
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    parser.add_argument("--title", default="RabbitMQ bandwidth over time")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--width", type=float, default=11.0, help="figure width in inches")
    parser.add_argument("--height", type=float, default=6.5, help="figure height in inches")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    csv_path: Path
    if args.csv:
        csv_path = args.csv
    elif args.config:
        sys.path.insert(0, str(PROJECT_ROOT))
        from controller.config_loader import load_config  # noqa: PLC0415 - optional dependency path

        csv_path = load_config(args.config).logging.csv_file
    else:
        csv_path = PROJECT_ROOT / "logs" / "bandwidth_history.csv"

    samples = read_samples(csv_path)
    theme = DARK if args.theme == "dark" else LIGHT
    stats = plot(
        samples,
        args.output,
        theme=theme,
        title=args.title,
        dpi=args.dpi,
        width=args.width,
        height=args.height,
    )
    print(
        f"wrote {args.output} "
        f"({stats['count']:.0f} samples, mean {stats['mean']:.2f} Mbit/s, "
        f"min {stats['min']:.2f}, max {stats['max']:.2f})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
