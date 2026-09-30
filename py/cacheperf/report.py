"""Plots and tables from a sweep's CSV.

Which figures to draw is read off the data rather than asked for: an axis that
holds one value across the whole sweep has nothing to show, so only the axes
that actually vary get a figure, and each remaining axis becomes a separate
line. A sweep of one axis therefore yields one plot per metric, and a sweep of
three yields the same plot broken out by the other two.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

AXES: dict[str, str] = {
    "size_bytes": "cache size (bytes)",
    "block_bytes": "block size (bytes)",
    "ways": "associativity (ways)",
    "policy": "replacement policy",
}

METRICS: dict[str, str] = {
    "miss_rate": "miss rate",
    "amat": "average access time (cycles)",
}

CATEGORICAL = {"policy", "write_policy", "trace"}

_FLOAT_FIELDS = {"miss_rate", "amat"}


class ReportError(RuntimeError):
    """The CSV does not describe a sweep that can be plotted."""


def _pyplot():
    """Imported on demand so the tables work on an install without matplotlib."""
    try:
        import matplotlib
    except ImportError:
        raise ReportError(
            "matplotlib is needed for figures; install it, or use --no-plots"
        ) from None
    matplotlib.use("Agg")  # No display in WSL, and none wanted in a batch sweep.
    import matplotlib.pyplot as plt

    return plt


def load(path: str | Path) -> list[dict]:
    """Read a sweep CSV back, restoring numeric types."""
    path = Path(path)
    if not path.is_file():
        raise ReportError(f"no such results file: {path}")
    with path.open(newline="") as fh:
        rows = [_coerce(row) for row in csv.DictReader(fh)]
    if not rows:
        raise ReportError(f"{path} holds no results")
    return rows


def _coerce(row: dict[str, str]) -> dict:
    out: dict = {}
    for key, value in row.items():
        if key in CATEGORICAL:
            out[key] = value
        elif key in _FLOAT_FIELDS:
            out[key] = float(value)
        else:
            try:
                out[key] = int(value)
            except ValueError:
                try:
                    out[key] = float(value)
                except ValueError:
                    out[key] = value
    return out


def distinct(rows: list[dict], key: str) -> list:
    """Distinct values of one column, ordered for an axis."""
    values = {row[key] for row in rows if key in row}
    return sorted(values, key=lambda v: (isinstance(v, str), v))


def varying_axes(rows: list[dict]) -> list[str]:
    return [axis for axis in AXES if len(distinct(rows, axis)) > 1]


def _series_columns(rows: list[dict], x_axis: str) -> list[str]:
    """Columns that separate one line from another, given the x axis."""
    columns = [a for a in varying_axes(rows) if a != x_axis]
    if len(distinct(rows, "trace")) > 1 or not columns:
        columns.insert(0, "trace")
    return columns


def _series_label(row: dict, columns: list[str]) -> str:
    parts = []
    for column in columns:
        value = row[column]
        if column == "size_bytes":
            parts.append(f"{value // 1024}kB" if value >= 1024 else f"{value}B")
        elif column == "block_bytes":
            parts.append(f"{value}B blocks")
        elif column == "ways":
            parts.append(f"{value}-way")
        else:
            parts.append(str(value))
    return ", ".join(parts)


def _group(rows: list[dict], columns: list[str]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(_series_label(row, columns), []).append(row)
    return grouped


def plot_axis(rows: list[dict], x_axis: str, metric: str, out: Path) -> Path:
    """One figure: `metric` against `x_axis`, one line per remaining axis."""
    plt = _pyplot()
    columns = _series_columns(rows, x_axis)
    grouped = _group(rows, columns)
    categories = distinct(rows, x_axis)

    figure, ax = plt.subplots(figsize=(8, 5))
    if x_axis in CATEGORICAL:
        width = 0.8 / max(len(grouped), 1)
        offsets = range(len(categories))
        for slot, (label, series) in enumerate(sorted(grouped.items())):
            heights = [_mean(series, x_axis, value, metric) for value in categories]
            ax.bar(
                [i + slot * width - 0.4 + width / 2 for i in offsets],
                heights, width=width, label=label,
            )
        ax.set_xticks(list(offsets))
        ax.set_xticklabels([str(c) for c in categories])
    else:
        for label, series in sorted(grouped.items()):
            points = sorted(
                (value, _mean(series, x_axis, value, metric))
                for value in distinct(series, x_axis)
            )
            ax.plot([p[0] for p in points], [p[1] for p in points], marker="o", label=label)
        ax.set_xscale("log", base=2)
        ax.set_xticks(categories)
        ax.set_xticklabels([_short(c) for c in categories])

    ax.set_xlabel(AXES[x_axis])
    ax.set_ylabel(METRICS[metric])
    ax.set_title(f"{METRICS[metric]} against {AXES[x_axis]}")
    ax.grid(True, linestyle=":", alpha=0.6)
    if metric == "miss_rate":
        ax.set_ylim(bottom=0)
    ax.legend(fontsize="small")
    figure.tight_layout()

    out.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(out, dpi=140)
    plt.close(figure)
    return out


def _mean(rows: list[dict], key: str, value, metric: str) -> float:
    """Averages over whatever the caller chose not to separate into series."""
    matching = [row[metric] for row in rows if row[key] == value]
    return sum(matching) / len(matching) if matching else float("nan")


def _short(value) -> str:
    if isinstance(value, int) and value >= 1024 and value % 1024 == 0:
        return f"{value // 1024}k"
    return str(value)


def table(rows: list[dict], x_axis: str, metric: str = "miss_rate") -> str:
    """The same data as the figure, for a terminal or a README."""
    columns = _series_columns(rows, x_axis)
    grouped = _group(rows, columns)
    categories = distinct(rows, x_axis)

    width = max((len(label) for label in grouped), default=8)
    header = f"{AXES[x_axis]:<{width}}" + "".join(f"{_short(c):>12}" for c in categories)
    lines = [header, "-" * len(header)]
    for label, series in sorted(grouped.items()):
        cells = "".join(
            f"{_mean(series, x_axis, value, metric):>12.4f}" for value in categories
        )
        lines.append(f"{label:<{width}}{cells}")
    return "\n".join(lines)


def report(csv_path: str | Path, outdir: str | Path = "figures") -> list[Path]:
    """Every figure the sweep supports. Returns the paths written."""
    rows = load(csv_path)
    axes = varying_axes(rows)
    if not axes:
        raise ReportError(
            "nothing varies across size, block size, associativity or policy; "
            "there is no axis to plot"
        )

    outdir = Path(outdir)
    written = []
    for axis in axes:
        for metric in METRICS:
            written.append(plot_axis(rows, axis, metric, outdir / f"{metric}_vs_{axis}.png"))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cacheperf-report", description="Plot the results of a sweep."
    )
    parser.add_argument("results", help="CSV written by the sweep")
    parser.add_argument("-o", "--outdir", default="figures")
    parser.add_argument("--metric", default="miss_rate", choices=sorted(METRICS))
    parser.add_argument("--no-plots", action="store_true", help="print tables only")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        rows = load(args.results)
        axes = varying_axes(rows)
        if not axes:
            raise ReportError("nothing varies across the four axes; there is nothing to plot")
        for axis in axes:
            print(f"\n{METRICS[args.metric]} against {AXES[axis]}\n")
            print(table(rows, axis, args.metric))
        if not args.no_plots:
            for path in report(args.results, args.outdir):
                print(f"\nwrote {path}")
    except ReportError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
