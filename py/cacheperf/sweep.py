"""Expanding a configuration matrix into measured points.

The matrix is deliberately split the way the build is: every geometry in it
costs one Verilator build, every other axis costs nothing but a plusarg. So the
sweep builds the distinct geometries once, in parallel, and then runs every
configuration against those binaries.

Combinations that describe no real cache -- a capacity that leaves a fractional
number of sets, say -- are dropped with a note rather than aborting the sweep,
because a rectangular matrix over the four axes always contains some.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from cacheperf import traces
from cacheperf.build import BuildError, build_all, cache_root
from cacheperf.config import (
    CacheGeometry,
    ConfigError,
    Policy,
    RunConfig,
    SweepPoint,
    WritePolicy,
    parse_size,
)
from cacheperf.model import CacheModel
from cacheperf.run import DEFAULT_TIMEOUT_S, RunError, simulate

logger = logging.getLogger(__name__)

CLASSIFICATION = ("compulsory", "capacity", "conflict")


@dataclass
class Matrix:
    """The cross product to measure. Each field is one axis."""

    sizes: list[int] = field(default_factory=lambda: [4096])
    blocks: list[int] = field(default_factory=lambda: [64])
    ways: list[int] = field(default_factory=lambda: [4])
    policies: list[Policy] = field(default_factory=lambda: [Policy.LRU])
    write_policies: list[WritePolicy] = field(
        default_factory=lambda: [WritePolicy.WRITE_BACK]
    )
    write_allocate: list[bool] = field(default_factory=lambda: [True])
    miss_penalties: list[int] = field(default_factory=lambda: [20])
    hit_latencies: list[int] = field(default_factory=lambda: [1])
    seeds: list[int] = field(default_factory=lambda: [1])
    trace_specs: list[str] = field(default_factory=lambda: ["looping:working_set=8k"])
    addr_width: int = 32

    def geometries(self) -> tuple[list[CacheGeometry], list[str]]:
        found: list[CacheGeometry] = []
        skipped: list[str] = []
        for size, block, ways in itertools.product(self.sizes, self.blocks, self.ways):
            try:
                found.append(CacheGeometry(size, block, ways, self.addr_width))
            except ConfigError as exc:
                skipped.append(f"{size}B/{block}B/{ways}-way: {exc}")
        return found, skipped

    def runs(self) -> list[RunConfig]:
        return [
            RunConfig(
                policy=policy,
                write_policy=write_policy,
                write_allocate=allocate,
                miss_penalty=penalty,
                hit_latency=latency,
                seed=seed,
            )
            for policy, write_policy, allocate, penalty, latency, seed in itertools.product(
                self.policies,
                self.write_policies,
                self.write_allocate,
                self.miss_penalties,
                self.hit_latencies,
                self.seeds,
            )
        ]

    def points(self) -> tuple[list[SweepPoint], list[str]]:
        geometries, skipped = self.geometries()
        points = [
            SweepPoint(geometry=g, run=r, trace=t)
            for g, r, t in itertools.product(geometries, self.runs(), self.trace_specs)
        ]
        return points, skipped


PRESETS: dict[str, dict] = {
    "size": dict(
        sizes=[1024 * n for n in (1, 2, 4, 8, 16, 32, 64, 128, 256)],
        trace_specs=[
            "looping:working_set=32k,accesses=200000,write_ratio=0.2",
            "uniform_random:footprint=64k,accesses=200000,write_ratio=0.2",
        ],
    ),
    "block": dict(
        sizes=[16 * 1024],
        blocks=[8, 16, 32, 64, 128, 256],
        trace_specs=[
            "sequential:footprint=256k,repeats=4",
            "pointer_chase:footprint=256k,accesses=50000",
        ],
    ),
    "ways": dict(
        sizes=[8 * 1024],
        ways=[1, 2, 4, 8, 16],
        trace_specs=[
            "strided:footprint=128k,stride=8k,repeats=500",
            "tiled_matmul:n=64,tile=8",
        ],
    ),
    "policy": dict(
        sizes=[8 * 1024],
        ways=[4, 8],
        policies=list(Policy),
        trace_specs=[
            "thrash:ways=8,sets=16,block_bytes=64,repeats=2000",
            "looping:working_set=32k,accesses=200000",
        ],
    ),
}


def trace_dir() -> Path:
    directory = cache_root() / "traces"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def materialize(specs, directory: Path | None = None, *, force: bool = False) -> dict[str, Path]:
    """Write each trace spec to disk once and return spec -> path.

    Generators are deterministic given their spec, so an existing file for a
    spec is that spec's trace and regenerating it would only cost time.
    """
    directory = directory or trace_dir()
    paths: dict[str, Path] = {}
    for spec in dict.fromkeys(specs):
        path = directory / f"{traces.trace_label(spec)}.hex"
        if force or not path.is_file():
            count = traces.write_trace(path, traces.generate(spec))
            logger.info("generated %s (%d accesses)", path.name, count)
        paths[spec] = path
    return paths


def classify(point: SweepPoint, trace_path: Path) -> dict[str, int]:
    """The three Cs, which only the reference model can attribute."""
    model = CacheModel(point.geometry, point.run, classify=True)
    stats = model.run_trace(traces.read_trace(trace_path))
    row = stats.as_row()
    return {name: row[name] for name in CLASSIFICATION}


def measure(
    point: SweepPoint,
    trace_path: Path,
    *,
    with_classification: bool = False,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> dict:
    result = simulate(point.geometry, point.run, trace_path, timeout=timeout)
    row = result.row()
    if with_classification:
        extra = classify(point, trace_path)
        if sum(extra.values()) != row["misses"]:
            logger.warning(
                "%s: model counted %d misses, RTL counted %d",
                point.label, sum(extra.values()), row["misses"],
            )
        row.update(extra)
    return row


def run_sweep(
    matrix: Matrix,
    *,
    workers: int | None = None,
    force_build: bool = False,
    with_classification: bool = False,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> list[dict]:
    """Build every geometry, then measure every point. Returns CSV-ready rows."""
    points, skipped = matrix.points()
    for note in skipped:
        logger.warning("skipping %s", note)
    if not points:
        raise ConfigError("the matrix expands to no buildable configurations")

    trace_paths = materialize(matrix.trace_specs)
    geometries = list(dict.fromkeys(p.geometry for p in points))

    logger.info("building %d geometr%s", len(geometries), "y" if len(geometries) == 1 else "ies")
    build_all(geometries, force=force_build)

    workers = workers or min(len(points), os.cpu_count() or 1)
    logger.info("running %d configurations on %d workers", len(points), workers)

    rows: list[dict] = []
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            (
                point,
                pool.submit(
                    measure,
                    point,
                    trace_paths[point.trace],
                    with_classification=with_classification,
                    timeout=timeout,
                ),
            )
            for point in points
        ]
        for done, (point, future) in enumerate(futures, 1):
            try:
                rows.append(future.result())
            except RunError as exc:
                failures += 1
                logger.error("%s failed: %s", point.label, exc)
            if done % 25 == 0 or done == len(futures):
                logger.info("  %d/%d", done, len(futures))

    if failures:
        logger.warning("%d of %d configurations failed", failures, len(points))
    return rows


def write_csv(rows: list[dict], path: str | Path) -> Path:
    if not rows:
        raise ConfigError("no results to write")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames: list[str] = []
    for row in rows:
        for name in row:
            if name not in fieldnames:
                fieldnames.append(name)

    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def summarize(rows: list[dict], limit: int = 10) -> str:
    """The best few configurations by average access time, as plain text."""
    ordered = sorted(rows, key=lambda r: r["amat"])[:limit]
    header = f"{'geometry':>18}  {'policy':>7}  {'trace':<28}  {'miss%':>7}  {'AMAT':>7}"
    lines = [header, "-" * len(header)]
    for row in ordered:
        geometry = f"{row['size_bytes']}B/{row['block_bytes']}B/{row['ways']}-way"
        lines.append(
            f"{geometry:>18}  {row['policy']:>7}  {row['trace'][:28]:<28}  "
            f"{row['miss_rate'] * 100:7.3f}  {row['amat']:7.3f}"
        )
    return "\n".join(lines)


def _int_list(text: str) -> list[int]:
    return [int(part, 0) for part in text.split(",") if part.strip()]


def _size_list(text: str) -> list[int]:
    return [parse_size(part) for part in text.split(",") if part.strip()]


def _bool_list(text: str) -> list[bool]:
    truthy = {"1", "true", "yes", "on"}
    falsy = {"0", "false", "no", "off"}
    values = []
    for part in text.split(","):
        key = part.strip().lower()
        if key in truthy:
            values.append(True)
        elif key in falsy:
            values.append(False)
        elif key:
            raise ConfigError(f"cannot read {part!r} as a boolean")
    return values


def build_matrix(args: argparse.Namespace) -> Matrix:
    """Preset first, explicit flags on top of it."""
    fields = dict(PRESETS.get(args.preset, {})) if args.preset else {}

    explicit = {
        "sizes": _size_list(args.size) if args.size else None,
        "blocks": _size_list(args.block) if args.block else None,
        "ways": _int_list(args.ways) if args.ways else None,
        "policies": [Policy.parse(p) for p in args.policy.split(",")] if args.policy else None,
        "write_policies": (
            [WritePolicy.parse(p) for p in args.write_policy.split(",")]
            if args.write_policy else None
        ),
        "write_allocate": _bool_list(args.write_allocate) if args.write_allocate else None,
        "miss_penalties": _int_list(args.miss_penalty) if args.miss_penalty else None,
        "hit_latencies": _int_list(args.hit_latency) if args.hit_latency else None,
        "seeds": _int_list(args.seed) if args.seed else None,
        "trace_specs": list(args.trace) if args.trace else None,
        "addr_width": args.addr_width,
    }
    fields.update({k: v for k, v in explicit.items() if v is not None})
    return Matrix(**fields)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cacheperf",
        description="Sweep cache configurations and measure each one.",
        epilog="Lists are comma separated; --trace may be repeated.",
    )
    parser.add_argument("--preset", choices=sorted(PRESETS), help="a ready-made single-axis sweep")
    parser.add_argument("--size", help="capacities, e.g. 1k,4k,16k")
    parser.add_argument("--block", help="block sizes in bytes, e.g. 16,32,64")
    parser.add_argument("--ways", help="associativities, e.g. 1,2,4,8")
    parser.add_argument("--policy", help="lru,plru,fifo,random")
    parser.add_argument("--write-policy", help="write_back,write_through")
    parser.add_argument("--write-allocate", help="1,0")
    parser.add_argument("--miss-penalty", help="cycles, e.g. 20,100")
    parser.add_argument("--hit-latency", help="cycles, e.g. 1,2")
    parser.add_argument("--seed", help="LFSR seeds for random replacement")
    parser.add_argument("--addr-width", type=int, default=32)
    parser.add_argument("--trace", action="append", help="trace spec; repeatable")
    parser.add_argument("-o", "--out", default="results.csv")
    parser.add_argument("-j", "--jobs", type=int, help="concurrent simulations")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--classify", action="store_true", help="add the three Cs")
    parser.add_argument("--force-build", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="list the matrix and stop")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO, format="%(message)s"
    )

    try:
        matrix = build_matrix(args)
        points, skipped = matrix.points()
        for note in skipped:
            logger.warning("skipping %s", note)

        if args.dry_run:
            geometries = list(dict.fromkeys(p.geometry for p in points))
            print(f"{len(points)} configurations, {len(geometries)} builds")
            for geometry in geometries:
                print(f"  {geometry.describe()}")
            return 0

        rows = run_sweep(
            matrix,
            workers=args.jobs,
            force_build=args.force_build,
            with_classification=args.classify,
            timeout=args.timeout,
        )
        path = write_csv(rows, args.out)
    except (ConfigError, BuildError, RunError) as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"\n{len(rows)} results -> {path}\n")
    print(summarize(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
