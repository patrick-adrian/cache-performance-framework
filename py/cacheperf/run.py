"""Running one built simulation and reading its counters back."""

from __future__ import annotations

import csv
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from cacheperf.build import build, cache_root
from cacheperf.config import CacheGeometry, RunConfig

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 900


class RunError(RuntimeError):
    """A simulation exited non-zero or produced no counters."""


@dataclass(frozen=True)
class RunResult:
    geometry: CacheGeometry
    run: RunConfig
    trace: Path
    counters: dict[str, int] = field(default_factory=dict)
    hits: str | None = None
    """Per-access outcomes as a string of '1' and '0', when a dump was asked for."""

    @property
    def miss_rate(self) -> float:
        total = self.counters["accesses"]
        return self.counters["misses"] / total if total else 0.0

    @property
    def amat(self) -> float:
        total = self.counters["accesses"]
        return self.counters["cycles"] / total if total else 0.0

    def row(self) -> dict[str, float | int | str]:
        return {
            "size_bytes": self.geometry.size_bytes,
            "block_bytes": self.geometry.block_bytes,
            "ways": self.geometry.ways,
            "sets": self.geometry.sets,
            "policy": str(self.run.policy),
            "write_policy": str(self.run.write_policy),
            "write_allocate": int(self.run.write_allocate),
            "miss_penalty": self.run.miss_penalty,
            "hit_latency": self.run.hit_latency,
            "seed": self.run.seed,
            "trace": self.trace.stem,
            **self.counters,
            "miss_rate": round(self.miss_rate, 6),
            "amat": round(self.amat, 6),
        }


def _read_counters(path: Path) -> dict[str, int]:
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    if len(rows) != 1:
        raise RunError(f"expected one row of counters in {path}, found {len(rows)}")
    return {k: int(v) for k, v in rows[0].items()}


def simulate(
    geometry: CacheGeometry,
    run: RunConfig,
    trace: str | Path,
    *,
    dump: bool = False,
    timeout: int = DEFAULT_TIMEOUT_S,
) -> RunResult:
    """Build if needed, run one trace, and return the counters.

    `dump` additionally captures the per-access hit/miss sequence, which is what
    the equivalence check compares against the reference model. It is one byte
    per access on disk, so it is for short traces only.
    """
    trace = Path(trace)
    if not trace.is_file():
        raise RunError(f"no such trace: {trace}")

    binary = build(geometry)
    scratch = cache_root() / "runs"
    scratch.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        csv_path = Path(tmp) / "counters.csv"
        dump_path = Path(tmp) / "hits.txt"
        command = [
            str(binary),
            f"+TRACE={trace}",
            f"+CSV={csv_path}",
            *run.plusargs(),
        ]
        if dump:
            command.append(f"+DUMP={dump_path}")

        logger.debug("running %s", " ".join(command))
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            raise RunError(
                f"{geometry.label}/{run.label} did not finish within {timeout}s"
            ) from None

        if result.returncode != 0:
            raise RunError(
                f"{geometry.label}/{run.label} exited {result.returncode}\n"
                f"{result.stdout}{result.stderr}"
            )
        if not csv_path.is_file():
            raise RunError(f"{geometry.label}/{run.label} wrote no counters")

        counters = _read_counters(csv_path)
        hits = dump_path.read_text() if dump and dump_path.is_file() else None

    return RunResult(geometry=geometry, run=run, trace=trace, counters=counters, hits=hits)
