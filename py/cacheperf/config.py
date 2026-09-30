"""Cache geometry and run configuration.

Geometry -- capacity, block size, associativity, address width -- fixes array
dimensions and address field widths, so it must be elaboration-time and each
distinct combination needs its own Verilator build. Everything else is a runtime
plusarg on an already-built binary and deliberately lives in a separate type, so
that sweeping policies or miss penalties costs no builds at all.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum


class ConfigError(ValueError):
    """A configuration that cannot be built or does not describe a real cache."""


class Policy(Enum):
    """Replacement policy. Values are the encoding passed to the RTL, so this
    enum is the single definition shared by both languages."""

    LRU = 0
    PLRU = 1
    FIFO = 2
    RANDOM = 3

    @classmethod
    def parse(cls, text: str) -> "Policy":
        try:
            return cls[str(text).strip().upper()]
        except KeyError:
            names = ", ".join(p.name.lower() for p in cls)
            raise ConfigError(f"unknown policy {text!r}; expected one of {names}") from None

    def __str__(self) -> str:
        return self.name.lower()


class WritePolicy(Enum):
    WRITE_BACK = 0
    WRITE_THROUGH = 1

    @classmethod
    def parse(cls, text: str) -> "WritePolicy":
        key = str(text).strip().upper().replace("-", "_")
        aliases = {"WB": "WRITE_BACK", "WT": "WRITE_THROUGH"}
        key = aliases.get(key, key)
        try:
            return cls[key]
        except KeyError:
            raise ConfigError(
                f"unknown write policy {text!r}; expected write_back or write_through"
            ) from None

    def __str__(self) -> str:
        return self.name.lower()


_SIZE_RE = re.compile(r"^(\d+)\s*([kKmM]?)[iI]?[bB]?$")
_SUFFIX = {"": 1, "k": 1024, "m": 1024 ** 2}


def parse_size(text: str | int) -> int:
    """Accept 4096, '4096', '4k', '4KiB', '1M' and return a byte count."""
    if isinstance(text, int):
        value = text
    else:
        match = _SIZE_RE.match(text.strip())
        if not match:
            raise ConfigError(f"cannot parse size {text!r}; expected e.g. 4096, 4k, 1M")
        value = int(match.group(1)) * _SUFFIX[match.group(2).lower()]
    if value <= 0:
        raise ConfigError(f"size must be positive, got {value}")
    return value


def is_power_of_two(n: int) -> bool:
    return n > 0 and n & (n - 1) == 0


def _require_power_of_two(name: str, value: int) -> None:
    if not is_power_of_two(value):
        raise ConfigError(f"{name} must be a power of two, got {value}")


@dataclass(frozen=True)
class CacheGeometry:
    """Build-time cache shape. Two geometries that compare equal share a binary."""

    size_bytes: int
    block_bytes: int
    ways: int
    addr_width: int = 32

    MIN_BLOCK_BYTES = 4
    # Bounded by the trace word rather than by the cache: a trace line packs the
    # address into the low bits and the write flag into bit 32, so a wider
    # address would need a wider trace format first.
    MAX_ADDR_WIDTH = 32

    def __post_init__(self) -> None:
        _require_power_of_two("size_bytes", self.size_bytes)
        _require_power_of_two("block_bytes", self.block_bytes)
        _require_power_of_two("ways", self.ways)

        if self.block_bytes < self.MIN_BLOCK_BYTES:
            raise ConfigError(
                f"block_bytes must be at least {self.MIN_BLOCK_BYTES}, got {self.block_bytes}"
            )
        if not 8 <= self.addr_width <= self.MAX_ADDR_WIDTH:
            raise ConfigError(
                f"addr_width must be between 8 and {self.MAX_ADDR_WIDTH}, got {self.addr_width}"
            )

        way_bytes = self.block_bytes * self.ways
        if self.size_bytes % way_bytes:
            raise ConfigError(
                f"size_bytes {self.size_bytes} is not divisible by "
                f"block_bytes * ways ({self.block_bytes} * {self.ways} = {way_bytes}); "
                "this geometry has no whole number of sets"
            )
        if self.tag_bits < 1:
            raise ConfigError(
                f"addr_width {self.addr_width} leaves no tag bits for this geometry "
                f"({self.offset_bits} offset + {self.index_bits} index)"
            )

    @property
    def sets(self) -> int:
        return self.size_bytes // (self.block_bytes * self.ways)

    @property
    def num_blocks(self) -> int:
        return self.size_bytes // self.block_bytes

    @property
    def offset_bits(self) -> int:
        return self.block_bytes.bit_length() - 1

    @property
    def index_bits(self) -> int:
        return self.sets.bit_length() - 1

    @property
    def tag_bits(self) -> int:
        return self.addr_width - self.index_bits - self.offset_bits

    @property
    def is_direct_mapped(self) -> bool:
        return self.ways == 1

    @property
    def is_fully_associative(self) -> bool:
        return self.sets == 1

    @property
    def label(self) -> str:
        """Filesystem-safe identifier, e.g. '32kB_b64_a4'."""
        if self.size_bytes >= 1024 ** 2 and self.size_bytes % 1024 ** 2 == 0:
            size = f"{self.size_bytes // 1024 ** 2}mB"
        elif self.size_bytes >= 1024 and self.size_bytes % 1024 == 0:
            size = f"{self.size_bytes // 1024}kB"
        else:
            size = f"{self.size_bytes}B"
        return f"{size}_b{self.block_bytes}_a{self.ways}"

    def key(self) -> str:
        """Canonical string identifying everything that affects the build."""
        return (
            f"size={self.size_bytes},block={self.block_bytes},"
            f"ways={self.ways},addr={self.addr_width}"
        )

    def build_id(self, source_digest: str) -> str:
        """Build cache key: geometry plus a digest of the RTL sources."""
        payload = f"{self.key()}|{source_digest}".encode()
        return hashlib.sha256(payload).hexdigest()[:12]

    def verilator_params(self) -> list[str]:
        return [
            f"-GCACHE_SIZE_BYTES={self.size_bytes}",
            f"-GBLOCK_SIZE_BYTES={self.block_bytes}",
            f"-GWAYS={self.ways}",
            f"-GADDR_WIDTH={self.addr_width}",
        ]

    def describe(self) -> str:
        kind = (
            "direct-mapped" if self.is_direct_mapped
            else "fully associative" if self.is_fully_associative
            else f"{self.ways}-way"
        )
        return (
            f"{self.label}: {kind}, {self.sets} sets x {self.ways} ways x "
            f"{self.block_bytes}B = {self.size_bytes}B, "
            f"tag/index/offset = {self.tag_bits}/{self.index_bits}/{self.offset_bits}"
        )


@dataclass(frozen=True)
class RunConfig:
    """Runtime knobs. None of these affect the build, so a single binary serves
    every combination of them."""

    policy: Policy = Policy.LRU
    write_policy: WritePolicy = WritePolicy.WRITE_BACK
    write_allocate: bool = True
    miss_penalty: int = 20
    hit_latency: int = 1
    seed: int = 1

    def __post_init__(self) -> None:
        if self.miss_penalty < 1:
            raise ConfigError(f"miss_penalty must be at least 1, got {self.miss_penalty}")
        if self.hit_latency < 1:
            raise ConfigError(f"hit_latency must be at least 1, got {self.hit_latency}")
        # Zero is excluded: it is the LFSR's lock-up state, from which random
        # replacement would return way 0 forever.
        if not 1 <= self.seed < 2 ** 32:
            raise ConfigError(f"seed must be a non-zero 32-bit value, got {self.seed}")

    @property
    def label(self) -> str:
        allocate = "wa" if self.write_allocate else "nwa"
        wp = "wb" if self.write_policy is WritePolicy.WRITE_BACK else "wt"
        return f"{self.policy}_{wp}_{allocate}_mp{self.miss_penalty}"

    def plusargs(self) -> list[str]:
        return [
            f"+POLICY={self.policy.value}",
            f"+WRITE_POLICY={self.write_policy.value}",
            f"+WRITE_ALLOCATE={int(self.write_allocate)}",
            f"+MISS_PENALTY={self.miss_penalty}",
            f"+HIT_LATENCY={self.hit_latency}",
            f"+SEED={self.seed}",
        ]


@dataclass(frozen=True)
class SweepPoint:
    """One measured point: a geometry, a runtime configuration, and a trace."""

    geometry: CacheGeometry
    run: RunConfig
    trace: str

    @property
    def label(self) -> str:
        return f"{self.geometry.label}_{self.run.label}_{self.trace}"
