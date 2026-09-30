"""Golden reference cache model.

This is the oracle. The RTL is correct exactly insofar as it agrees with this
model on the hit or miss outcome of every access, so where the two could
plausibly differ, the choice made here is the specification and the RTL must
follow it. Two such choices are load-bearing:

Victim selection prefers invalid ways. When a set is not yet full, the lowest
numbered invalid way is filled and the replacement policy is not consulted at
all. Only a full set invokes the policy.

The random policy's LFSR advances once per allocation, never per cycle. A
cycle-driven LFSR would be unreproducible from an untimed model, which would
leave random replacement permanently unverifiable. The RTL therefore has to
clock its LFSR on allocation, and use the same 32-bit polynomial implemented
here, or the two will diverge on the very first eviction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from enum import Enum

from cacheperf.config import CacheGeometry, ConfigError, Policy, RunConfig, WritePolicy

logger = logging.getLogger(__name__)


class MissType(Enum):
    """Hill's three Cs."""

    COMPULSORY = "compulsory"
    CAPACITY = "capacity"
    CONFLICT = "conflict"


# x^32 + x^22 + x^2 + x + 1, maximal length over 32 bits.
_LFSR_MASK = 0xFFFFFFFF


def lfsr_next(state: int) -> int:
    """One step of the Fibonacci LFSR shared with the RTL."""
    feedback = ((state >> 31) ^ (state >> 21) ^ (state >> 1) ^ state) & 1
    return ((state << 1) | feedback) & _LFSR_MASK


class Replacement:
    """Per-set replacement state. `on_hit` and `on_fill` are separate because
    FIFO must ignore hits while LRU must not."""

    def __init__(self, sets: int, ways: int, seed: int) -> None:
        self.sets = sets
        self.ways = ways
        self.seed = seed

    def victim(self, set_idx: int) -> int:
        raise NotImplementedError

    def on_hit(self, set_idx: int, way: int) -> None:
        pass

    def on_fill(self, set_idx: int, way: int) -> None:
        pass


class LRUReplacement(Replacement):
    """Recency lists, most recently used first."""

    def __init__(self, sets: int, ways: int, seed: int) -> None:
        super().__init__(sets, ways, seed)
        self._order = [list(range(ways)) for _ in range(sets)]

    def victim(self, set_idx: int) -> int:
        return self._order[set_idx][-1]

    def _touch(self, set_idx: int, way: int) -> None:
        order = self._order[set_idx]
        order.remove(way)
        order.insert(0, way)

    on_hit = _touch
    on_fill = _touch


class FIFOReplacement(Replacement):
    """Insertion order only; hits do not reorder anything."""

    def __init__(self, sets: int, ways: int, seed: int) -> None:
        super().__init__(sets, ways, seed)
        self._next = [0] * sets

    def victim(self, set_idx: int) -> int:
        return self._next[set_idx]

    def on_fill(self, set_idx: int, way: int) -> None:
        self._next[set_idx] = (self._next[set_idx] + 1) % self.ways


class PLRUReplacement(Replacement):
    """Tree pseudo-LRU: ways-1 bits per set. A bit of 0 sends the search left."""

    def __init__(self, sets: int, ways: int, seed: int) -> None:
        super().__init__(sets, ways, seed)
        if ways & (ways - 1):
            raise ConfigError(f"tree PLRU requires a power-of-two associativity, got {ways}")
        self._bits = [[0] * (ways - 1) for _ in range(sets)]
        self._levels = max(ways.bit_length() - 1, 0)

    def victim(self, set_idx: int) -> int:
        if self.ways == 1:
            return 0
        bits = self._bits[set_idx]
        node = 0
        for _ in range(self._levels):
            node = 2 * node + 1 if bits[node] == 0 else 2 * node + 2
        return node - (self.ways - 1)

    def _touch(self, set_idx: int, way: int) -> None:
        if self.ways == 1:
            return
        bits = self._bits[set_idx]
        node = way + self.ways - 1
        while node > 0:
            parent = (node - 1) // 2
            # Point the parent away from the subtree just used.
            bits[parent] = 1 if node == 2 * parent + 1 else 0
            node = parent

    on_hit = _touch
    on_fill = _touch


class RandomReplacement(Replacement):
    """LFSR victim selection, advanced once per allocation."""

    def __init__(self, sets: int, ways: int, seed: int) -> None:
        super().__init__(sets, ways, seed)
        if seed == 0:
            raise ConfigError("random replacement needs a non-zero LFSR seed")
        self._state = seed

    def victim(self, set_idx: int) -> int:
        self._state = lfsr_next(self._state)
        return self._state & (self.ways - 1)


_POLICIES: dict[Policy, type[Replacement]] = {
    Policy.LRU: LRUReplacement,
    Policy.PLRU: PLRUReplacement,
    Policy.FIFO: FIFOReplacement,
    Policy.RANDOM: RandomReplacement,
}


@dataclass
class Stats:
    reads: int = 0
    writes: int = 0
    read_hits: int = 0
    write_hits: int = 0
    evictions: int = 0
    writebacks: int = 0
    memory_reads: int = 0
    memory_writes: int = 0
    cycles: int = 0
    compulsory: int = 0
    capacity: int = 0
    conflict: int = 0

    @property
    def accesses(self) -> int:
        return self.reads + self.writes

    @property
    def hits(self) -> int:
        return self.read_hits + self.write_hits

    @property
    def misses(self) -> int:
        return self.accesses - self.hits

    @property
    def read_misses(self) -> int:
        return self.reads - self.read_hits

    @property
    def write_misses(self) -> int:
        return self.writes - self.write_hits

    @property
    def miss_rate(self) -> float:
        return self.misses / self.accesses if self.accesses else 0.0

    @property
    def hit_rate(self) -> float:
        return 1.0 - self.miss_rate

    @property
    def amat(self) -> float:
        """Average memory access time in cycles."""
        return self.cycles / self.accesses if self.accesses else 0.0

    def as_row(self) -> dict[str, float | int]:
        return {
            "accesses": self.accesses,
            "reads": self.reads,
            "writes": self.writes,
            "hits": self.hits,
            "misses": self.misses,
            "read_hits": self.read_hits,
            "read_misses": self.read_misses,
            "write_hits": self.write_hits,
            "write_misses": self.write_misses,
            "evictions": self.evictions,
            "writebacks": self.writebacks,
            "memory_reads": self.memory_reads,
            "memory_writes": self.memory_writes,
            "compulsory": self.compulsory,
            "capacity": self.capacity,
            "conflict": self.conflict,
            "cycles": self.cycles,
            "miss_rate": round(self.miss_rate, 6),
            "amat": round(self.amat, 6),
        }


@dataclass
class AccessResult:
    hit: bool
    way: int
    evicted: bool = False
    wrote_back: bool = False
    miss_type: MissType | None = None


@dataclass
class _Line:
    valid: bool = False
    dirty: bool = False
    tag: int = 0


@dataclass
class _Classifier:
    """Hill's three Cs, via a fully-associative LRU shadow of equal capacity.

    A miss is compulsory on first sight of a block, capacity if the ideal
    fully-associative cache would have missed too, and conflict otherwise --
    that is, a miss caused purely by the mapping rather than by size.
    """

    geometry: CacheGeometry
    run: RunConfig
    seen: set[int] = field(default_factory=set)
    shadow: "CacheModel | None" = None

    def __post_init__(self) -> None:
        ideal = CacheGeometry(
            size_bytes=self.geometry.size_bytes,
            block_bytes=self.geometry.block_bytes,
            ways=self.geometry.num_blocks,
            addr_width=self.geometry.addr_width,
        )
        self.shadow = CacheModel(ideal, replace(self.run, policy=Policy.LRU), classify=False)

    def classify(self, addr: int, is_write: bool, block: int) -> MissType:
        first_sight = block not in self.seen
        self.seen.add(block)
        shadow_hit = self.shadow.access(addr, is_write).hit
        if first_sight:
            return MissType.COMPULSORY
        return MissType.CONFLICT if shadow_hit else MissType.CAPACITY

    def note_hit(self, addr: int, is_write: bool, block: int) -> None:
        """The shadow must see every access, not only the ones that miss here."""
        self.seen.add(block)
        self.shadow.access(addr, is_write)


class CacheModel:
    """Functional, untimed cache. Cycle counts are an analytic estimate under a
    blocking single-outstanding-miss model; the RTL is authoritative for timing,
    and equivalence is checked on hit/miss outcomes rather than on cycles."""

    def __init__(
        self,
        geometry: CacheGeometry,
        run: RunConfig | None = None,
        *,
        classify: bool = False,
    ) -> None:
        self.geometry = geometry
        self.run = run or RunConfig()
        self.stats = Stats()

        self._lines = [
            [_Line() for _ in range(geometry.ways)] for _ in range(geometry.sets)
        ]
        self._repl = _POLICIES[self.run.policy](
            geometry.sets, geometry.ways, self.run.seed
        )
        self._classifier = _Classifier(geometry, self.run) if classify else None

        self._index_mask = geometry.sets - 1
        self._write_back = self.run.write_policy is WritePolicy.WRITE_BACK

    def _decode(self, addr: int) -> tuple[int, int, int]:
        block = addr >> self.geometry.offset_bits
        return block, block & self._index_mask, block >> self.geometry.index_bits

    def _find(self, set_idx: int, tag: int) -> int | None:
        for way, line in enumerate(self._lines[set_idx]):
            if line.valid and line.tag == tag:
                return way
        return None

    def _select_victim(self, set_idx: int) -> int:
        for way, line in enumerate(self._lines[set_idx]):
            if not line.valid:
                return way
        return self._repl.victim(set_idx)

    def access(self, addr: int, is_write: bool = False) -> AccessResult:
        if addr < 0 or addr >= 1 << self.geometry.addr_width:
            raise ConfigError(
                f"address {addr:#x} does not fit in {self.geometry.addr_width} bits"
            )

        block, set_idx, tag = self._decode(addr)
        if is_write:
            self.stats.writes += 1
        else:
            self.stats.reads += 1
        self.stats.cycles += self.run.hit_latency

        way = self._find(set_idx, tag)
        if way is not None:
            return self._on_hit(addr, is_write, block, set_idx, way)
        return self._on_miss(addr, is_write, block, set_idx, tag)

    def _on_hit(
        self, addr: int, is_write: bool, block: int, set_idx: int, way: int
    ) -> AccessResult:
        if is_write:
            self.stats.write_hits += 1
            if self._write_back:
                self._lines[set_idx][way].dirty = True
            else:
                self.stats.memory_writes += 1
        else:
            self.stats.read_hits += 1

        self._repl.on_hit(set_idx, way)
        if self._classifier:
            self._classifier.note_hit(addr, is_write, block)
        return AccessResult(hit=True, way=way)

    def _on_miss(
        self, addr: int, is_write: bool, block: int, set_idx: int, tag: int
    ) -> AccessResult:
        miss_type = None
        if self._classifier:
            miss_type = self._classifier.classify(addr, is_write, block)
            setattr(self.stats, miss_type.value, getattr(self.stats, miss_type.value) + 1)

        if is_write and not self.run.write_allocate:
            # Straight to memory; the cache is left untouched.
            self.stats.memory_writes += 1
            self.stats.cycles += self.run.miss_penalty
            return AccessResult(hit=False, way=-1, miss_type=miss_type)

        way = self._select_victim(set_idx)
        line = self._lines[set_idx][way]

        evicted = line.valid
        wrote_back = evicted and line.dirty
        if evicted:
            self.stats.evictions += 1
        if wrote_back:
            self.stats.writebacks += 1
            self.stats.memory_writes += 1
            # Blocking cache: the writeback drains before the fill begins.
            self.stats.cycles += self.run.miss_penalty

        self.stats.memory_reads += 1
        self.stats.cycles += self.run.miss_penalty

        line.valid = True
        line.tag = tag
        line.dirty = bool(is_write and self._write_back)
        if is_write and not self._write_back:
            self.stats.memory_writes += 1

        self._repl.on_fill(set_idx, way)
        return AccessResult(
            hit=False, way=way, evicted=evicted, wrote_back=wrote_back, miss_type=miss_type
        )

    def run_trace(self, trace) -> Stats:
        for addr, is_write in trace:
            self.access(addr, is_write)
        return self.stats

    def hit_vector(self, trace) -> list[bool]:
        """Per-access hit/miss outcomes, the sequence the RTL must reproduce."""
        return [self.access(addr, is_write).hit for addr, is_write in trace]
