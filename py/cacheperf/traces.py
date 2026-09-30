"""Memory reference trace generation and import.

A trace is an iterable of ``(address, is_write)`` pairs. Generators are lazy so
that a multi-million reference workload never has to exist in memory at once.

On disk a trace is one hex word per line, `$readmemh`-compatible and parsed by
the testbench with a single `$fscanf`: bit 32 is the write flag and bits 31:0
are the byte address.

    00000040      read  0x40
    100000040     write 0x40
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from cacheperf.config import ConfigError, parse_size

logger = logging.getLogger(__name__)

Access = tuple[int, bool]

WRITE_FLAG = 1 << 32


def write_trace(path: str | Path, trace: Iterable[Access]) -> int:
    """Write a trace to disk, returning the number of accesses written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w") as fh:
        for addr, is_write in trace:
            fh.write(f"{addr | WRITE_FLAG if is_write else addr:09x}\n")
            count += 1
    logger.debug("wrote %d accesses to %s", count, path)
    return count


def read_trace(path: str | Path) -> Iterator[Access]:
    with Path(path).open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            word = int(line, 16)
            yield word & 0xFFFFFFFF, bool(word & WRITE_FLAG)


def _writes(rng: random.Random, write_ratio: float) -> bool:
    return write_ratio > 0 and rng.random() < write_ratio


def sequential(
    footprint: int | str = "64k", stride: int = 4, repeats: int = 1,
    write_ratio: float = 0.0, seed: int = 1, base: int = 0,
) -> Iterator[Access]:
    """A linear sweep. Miss count depends only on block size, which makes this
    the workload that isolates the block-size axis."""
    footprint = parse_size(footprint)
    rng = random.Random(seed)
    for _ in range(repeats):
        for offset in range(0, footprint, stride):
            yield base + offset, _writes(rng, write_ratio)


def strided(
    footprint: int | str = "64k", stride: int | str = 64, repeats: int = 1,
    write_ratio: float = 0.0, seed: int = 1, base: int = 0,
) -> Iterator[Access]:
    """Constant stride. As the stride approaches the block size, spatial
    locality collapses and every access costs a fresh block."""
    footprint = parse_size(footprint)
    stride = parse_size(stride)
    rng = random.Random(seed)
    for _ in range(repeats):
        for offset in range(0, footprint, stride):
            yield base + offset, _writes(rng, write_ratio)


def looping(
    working_set: int | str = "8k", accesses: int = 100_000, stride: int = 4,
    write_ratio: float = 0.0, seed: int = 1, base: int = 0,
) -> Iterator[Access]:
    """Repeated sweeps of a fixed working set. Sweeping cache capacity against
    this produces the classic miss-rate knee at the point the set starts to
    fit."""
    working_set = parse_size(working_set)
    rng = random.Random(seed)
    span = max(working_set // stride, 1)
    for i in range(accesses):
        yield base + (i % span) * stride, _writes(rng, write_ratio)


def uniform_random(
    footprint: int | str = "64k", accesses: int = 100_000, granularity: int = 4,
    write_ratio: float = 0.0, seed: int = 1, base: int = 0,
) -> Iterator[Access]:
    """No locality at all. A useful lower bound: any cache doing better than
    this on a real workload is exploiting locality that actually exists."""
    footprint = parse_size(footprint)
    rng = random.Random(seed)
    slots = max(footprint // granularity, 1)
    for _ in range(accesses):
        yield base + rng.randrange(slots) * granularity, _writes(rng, write_ratio)


def pointer_chase(
    footprint: int | str = "64k", accesses: int = 50_000, node_size: int = 64,
    seed: int = 1, base: int = 0,
) -> Iterator[Access]:
    """A random permutation walked in order: dependent accesses with no spatial
    locality, so prefetching and large blocks buy nothing."""
    footprint = parse_size(footprint)
    rng = random.Random(seed)
    nodes = max(footprint // node_size, 1)
    order = list(range(nodes))
    rng.shuffle(order)
    for i in range(accesses):
        yield base + order[i % nodes] * node_size, False


def thrash(
    ways: int = 4, sets: int = 64, block_bytes: int = 64, repeats: int = 200,
    extra: int = 1, base: int = 0,
) -> Iterator[Access]:
    """Adversarial for LRU: cycles one more block than a set can hold, all
    mapped to the same set, so LRU evicts precisely the block needed next and
    never hits. Random replacement does measurably better, which is what makes
    the policy axis visible rather than theoretical."""
    if ways < 1 or sets < 1 or repeats < 1:
        raise ConfigError("thrash needs positive ways, sets and repeats")
    stride = sets * block_bytes
    blocks = [base + i * stride for i in range(ways + extra)]
    for _ in range(repeats):
        for addr in blocks:
            yield addr, False


def tiled_matmul(
    n: int = 64, tile: int = 8, elem_size: int = 8, base: int = 0,
) -> Iterator[Access]:
    """Blocked matrix multiply over three n x n matrices. Exercises block size
    and associativity together: the three streams contend for sets, and the
    tile size decides whether the working set fits."""
    if tile > n:
        raise ConfigError(f"tile {tile} cannot exceed matrix dimension {n}")
    matrix_bytes = n * n * elem_size
    a_base, b_base, c_base = base, base + matrix_bytes, base + 2 * matrix_bytes

    for ii in range(0, n, tile):
        for jj in range(0, n, tile):
            for kk in range(0, n, tile):
                for i in range(ii, min(ii + tile, n)):
                    for j in range(jj, min(jj + tile, n)):
                        c_addr = c_base + (i * n + j) * elem_size
                        yield c_addr, False
                        for k in range(kk, min(kk + tile, n)):
                            yield a_base + (i * n + k) * elem_size, False
                            yield b_base + (k * n + j) * elem_size, False
                        yield c_addr, True


_DINERO_OPS = {"0": False, "1": True, "2": False, "r": False, "w": True, "i": False}


def import_dinero(path: str | Path) -> Iterator[Access]:
    """Dinero IV format: one `<op> <hex address>` pair per line, where op 0 is a
    read, 1 a write and 2 an instruction fetch."""
    with Path(path).open() as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                raise ConfigError(f"{path}:{lineno}: expected '<op> <address>', got {line!r}")
            op = parts[0].lower()
            if op not in _DINERO_OPS:
                raise ConfigError(f"{path}:{lineno}: unknown access type {parts[0]!r}")
            yield int(parts[1], 16), _DINERO_OPS[op]


_LACKEY_RE = re.compile(r"^\s*(?P<op>[ILSM])\s+(?P<addr>[0-9a-fA-F]+),(?P<size>\d+)")


def import_lackey(path: str | Path) -> Iterator[Access]:
    """valgrind --tool=lackey --trace-mem=yes output. An M (modify) is a read
    followed by a write to the same address."""
    with Path(path).open() as fh:
        for line in fh:
            match = _LACKEY_RE.match(line)
            if not match:
                continue
            addr = int(match.group("addr"), 16)
            op = match.group("op")
            if op == "S":
                yield addr, True
            elif op == "M":
                yield addr, False
                yield addr, True
            else:
                yield addr, False


GENERATORS = {
    "sequential": sequential,
    "strided": strided,
    "looping": looping,
    "uniform_random": uniform_random,
    "pointer_chase": pointer_chase,
    "thrash": thrash,
    "tiled_matmul": tiled_matmul,
}

# Byte counts, so these accept the k and M suffixes rather than digits alone.
_SIZE_PARAMS = {"footprint", "working_set", "stride"}

_INT_PARAMS = {
    "repeats", "accesses", "granularity", "node_size", "ways", "sets",
    "block_bytes", "extra", "n", "tile", "elem_size", "seed", "base",
}


def parse_spec(spec: str) -> tuple[str, dict[str, object]]:
    """Parse ``name`` or ``name:key=value,key=value`` into a call.

        >>> parse_spec("looping:working_set=8k,accesses=1000")
        ('looping', {'working_set': 8192, 'accesses': 1000})
    """
    name, _, rest = spec.partition(":")
    name = name.strip()
    if name not in GENERATORS:
        known = ", ".join(sorted(GENERATORS))
        raise ConfigError(f"unknown trace {name!r}; expected one of {known}")

    params: dict[str, object] = {}
    for item in filter(None, (p.strip() for p in rest.split(","))):
        key, sep, value = item.partition("=")
        if not sep:
            raise ConfigError(f"trace parameter {item!r} is not key=value")
        key = key.strip()
        value = value.strip()
        if key == "write_ratio":
            params[key] = float(value)
        elif key in _SIZE_PARAMS:
            params[key] = parse_size(value)
        elif key in _INT_PARAMS:
            try:
                params[key] = int(value)
            except ValueError:
                raise ConfigError(f"trace parameter {key} must be an integer, got {value!r}") from None
        else:
            params[key] = value
    return name, params


def generate(spec: str) -> Iterator[Access]:
    """Build a trace from a spec string such as ``thrash:ways=4,sets=64``."""
    name, params = parse_spec(spec)
    try:
        return GENERATORS[name](**params)
    except TypeError as exc:
        raise ConfigError(f"bad parameters for trace {name!r}: {exc}") from None


def trace_label(spec: str) -> str:
    """Filesystem-safe name for a spec string."""
    return re.sub(r"[^A-Za-z0-9]+", "_", spec).strip("_")
