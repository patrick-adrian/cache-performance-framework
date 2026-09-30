"""Verilator builds, cached by geometry and source content.

Geometry is elaboration-time, so each distinct geometry needs its own binary.
The cache key is the geometry plus a digest of every RTL and testbench source,
which means editing the RTL invalidates every build while sweeping policies or
traces invalidates none.

Build output lives under ``~/.cache/cache-perf`` on the native filesystem rather
than beside the sources: a C++ build is thousands of small files, and that is
the access pattern the /mnt/c 9p mount is worst at.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from cacheperf.config import CacheGeometry, ConfigError

logger = logging.getLogger(__name__)

TOP_MODULE = "tb_cache"
BINARY_NAME = "Vtb_cache"


class BuildError(RuntimeError):
    """Verilator refused to build a configuration."""


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def source_files() -> list[Path]:
    """RTL first, package first of all: Verilator needs the package defined
    before the modules that import it."""
    rtl = repo_root() / "rtl"
    ordered = [
        rtl / "cache_pkg.sv",
        rtl / "repl_lru.sv",
        rtl / "repl_plru.sv",
        rtl / "repl_fifo.sv",
        rtl / "repl_random.sv",
        rtl / "replacement_unit.sv",
        rtl / "mem_stub.sv",
        rtl / "cache.sv",
        repo_root() / "tb" / f"{TOP_MODULE}.sv",
    ]
    missing = [p for p in ordered if not p.is_file()]
    if missing:
        raise BuildError("missing sources: " + ", ".join(str(p) for p in missing))
    return ordered


def cache_root() -> Path:
    override = os.environ.get("CACHEPERF_CACHE")
    root = Path(override) if override else Path.home() / ".cache" / "cache-perf"
    root.mkdir(parents=True, exist_ok=True)
    return root


@lru_cache(maxsize=1)
def source_digest() -> str:
    """Content hash of every source, so a stale binary can never be reused."""
    digest = hashlib.sha256()
    for path in source_files():
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def build_dir(geometry: CacheGeometry) -> Path:
    return cache_root() / "build" / f"{geometry.label}_{geometry.build_id(source_digest())}"


def binary_path(geometry: CacheGeometry) -> Path:
    return build_dir(geometry) / "obj_dir" / BINARY_NAME


def verilator_command(geometry: CacheGeometry, *, jobs: int) -> list[str]:
    target = build_dir(geometry)
    return [
        "verilator",
        "--binary",
        "--timescale", "1ns/1ps",
        "--top-module", TOP_MODULE,
        "--Mdir", str(target / "obj_dir"),
        "-o", BINARY_NAME,
        "--build-jobs", str(jobs),
        *geometry.verilator_params(),
        *(str(p) for p in source_files()),
    ]


def build(geometry: CacheGeometry, *, force: bool = False, jobs: int = 1) -> Path:
    """Build one geometry, or return the cached binary. Returns its path."""
    binary = binary_path(geometry)
    if binary.is_file() and not force:
        logger.debug("cached build for %s", geometry.label)
        return binary

    if shutil.which("verilator") is None:
        raise BuildError("verilator is not on PATH; run scripts/setup_wsl.sh")

    target = build_dir(geometry)
    if force and target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    command = verilator_command(geometry, jobs=jobs)
    logger.info("building %s", geometry.describe())
    result = subprocess.run(command, capture_output=True, text=True, cwd=repo_root())
    (target / "verilator.log").write_text(result.stdout + result.stderr)

    if result.returncode != 0 or not binary.is_file():
        raise BuildError(
            f"verilator failed for {geometry.label} (exit {result.returncode})\n"
            f"{' '.join(command)}\n{result.stdout}{result.stderr}"
        )
    return binary


def build_all(
    geometries, *, force: bool = False, workers: int | None = None
) -> dict[CacheGeometry, Path]:
    """Build a set of geometries in parallel.

    Verilator's own build parallelism is held to one job per geometry: with more
    geometries than cores, fanning out across configurations keeps every core
    busy without the oversubscription of nesting the two.
    """
    unique = list(dict.fromkeys(geometries))
    if not unique:
        return {}

    cores = os.cpu_count() or 1
    workers = workers or min(len(unique), cores)
    per_build_jobs = max(cores // workers, 1)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            g: pool.submit(build, g, force=force, jobs=per_build_jobs) for g in unique
        }
        return {g: f.result() for g, f in futures.items()}


def clean() -> None:
    target = cache_root() / "build"
    if target.exists():
        shutil.rmtree(target)
        logger.info("removed %s", target)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build one cache geometry.")
    parser.add_argument("--size", default="4k")
    parser.add_argument("--block", default="64")
    parser.add_argument("--ways", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.clean:
        clean()
        return 0

    from cacheperf.config import parse_size

    try:
        geometry = CacheGeometry(parse_size(args.size), parse_size(args.block), args.ways)
        print(build(geometry, force=args.force, jobs=os.cpu_count() or 1))
    except (ConfigError, BuildError) as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
