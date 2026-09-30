import pytest

from cacheperf.config import CacheGeometry, ConfigError, Policy, RunConfig, WritePolicy
from cacheperf.model import CacheModel, MissType, lfsr_next

ALL_POLICIES = list(Policy)


def walk(start: int, count: int, stride: int, *, write: bool = False):
    return [(start + i * stride, write) for i in range(count)]


def repeat(addrs, times: int, *, write: bool = False):
    return [(a, write) for _ in range(times) for a in addrs]


class TestSequentialWalk:
    """A linear sweep touches each block exactly once, so the miss count is
    fixed by the block size alone -- associativity and policy cannot change it."""

    @pytest.mark.parametrize("policy", ALL_POLICIES)
    @pytest.mark.parametrize("ways", [1, 2, 4, 8])
    def test_misses_equal_blocks_touched(self, policy, ways):
        geom = CacheGeometry(size_bytes=4096, block_bytes=32, ways=ways)
        model = CacheModel(geom, RunConfig(policy=policy))
        footprint = 8192
        model.run_trace(walk(0, footprint // 4, 4))

        assert model.stats.accesses == footprint // 4
        assert model.stats.misses == footprint // 32

    def test_every_miss_is_compulsory(self):
        geom = CacheGeometry(size_bytes=4096, block_bytes=32, ways=4)
        model = CacheModel(geom, classify=True)
        model.run_trace(walk(0, 2048, 4))

        assert model.stats.compulsory == model.stats.misses
        assert model.stats.capacity == 0
        assert model.stats.conflict == 0


class TestWorkingSetThatFits:
    @pytest.mark.parametrize("policy", ALL_POLICIES)
    def test_only_the_first_pass_misses(self, policy):
        geom = CacheGeometry(size_bytes=1024, block_bytes=32, ways=4)
        blocks = [i * 32 for i in range(16)]
        model = CacheModel(geom, RunConfig(policy=policy))
        model.run_trace(repeat(blocks, 10))

        assert model.stats.misses == len(blocks)
        assert model.stats.hits == len(blocks) * 9


# sets * block_bytes, so every address lands in set 0 of THRASH_GEOM.
_SET_STRIDE = 8 * 32
_THRASH_TRACE = repeat([i * _SET_STRIDE for i in range(5)], 40)


class TestThrashing:
    """One more block than the set has ways, cycled. LRU evicts precisely the
    block about to be used next, so it never hits; random sometimes gets lucky.
    This is the workload that makes the policy axis visible."""

    GEOM = CacheGeometry(size_bytes=1024, block_bytes=32, ways=4)
    TRACE = _THRASH_TRACE

    def test_lru_never_hits(self):
        model = CacheModel(self.GEOM, RunConfig(policy=Policy.LRU))
        model.run_trace(self.TRACE)
        assert model.stats.hits == 0
        assert model.stats.miss_rate == 1.0

    def test_fifo_never_hits(self):
        model = CacheModel(self.GEOM, RunConfig(policy=Policy.FIFO))
        model.run_trace(self.TRACE)
        assert model.stats.hits == 0

    def test_random_does_better_than_lru(self):
        model = CacheModel(self.GEOM, RunConfig(policy=Policy.RANDOM, seed=12345))
        model.run_trace(self.TRACE)
        assert model.stats.hits > 0
        assert model.stats.miss_rate < 1.0

    def test_all_addresses_map_to_one_set(self):
        model = CacheModel(self.GEOM)
        indices = {model._decode(addr)[1] for addr, _ in self.TRACE}
        assert indices == {0}


class TestAssociativityResolvesConflicts:
    """Two blocks that collide in a direct-mapped cache coexist happily once
    there is a second way, which is the definition of a conflict miss."""

    ADDRS = [0, 1024]
    TRACE = repeat(ADDRS, 50)

    def test_direct_mapped_thrashes(self):
        geom = CacheGeometry(size_bytes=1024, block_bytes=32, ways=1)
        model = CacheModel(geom)
        model.run_trace(self.TRACE)
        assert model.stats.misses == len(self.TRACE)

    def test_two_way_fixes_it(self):
        geom = CacheGeometry(size_bytes=1024, block_bytes=32, ways=2)
        model = CacheModel(geom)
        model.run_trace(self.TRACE)
        assert model.stats.misses == len(self.ADDRS)

    def test_classified_as_conflict_not_capacity(self):
        geom = CacheGeometry(size_bytes=1024, block_bytes=32, ways=1)
        model = CacheModel(geom, classify=True)
        model.run_trace(self.TRACE)

        assert model.stats.compulsory == 2
        assert model.stats.conflict == len(self.TRACE) - 2
        assert model.stats.capacity == 0


class TestCapacityMisses:
    """A working set larger than the cache misses even when fully associative,
    so those misses are capacity misses however the cache is organised."""

    def test_oversized_working_set(self):
        geom = CacheGeometry(size_bytes=1024, block_bytes=32, ways=8)
        blocks = [i * 32 for i in range(64)]  # 64 blocks into a 32-block cache
        model = CacheModel(geom, classify=True)
        model.run_trace(repeat(blocks, 5))

        assert model.stats.compulsory == 64
        assert model.stats.capacity > 0


class TestPolicyDivergence:
    """A, B, A, C, A separates recency from insertion order: LRU keeps A because
    it was used again, FIFO discards it because it arrived first."""

    GEOM = CacheGeometry(size_bytes=64, block_bytes=32, ways=2)
    TRACE = [(0, False), (32, False), (0, False), (64, False), (0, False)]

    def test_lru_retains_the_reused_block(self):
        model = CacheModel(self.GEOM, RunConfig(policy=Policy.LRU))
        model.run_trace(self.TRACE)
        assert model.stats.hits == 2

    def test_fifo_evicts_the_reused_block(self):
        model = CacheModel(self.GEOM, RunConfig(policy=Policy.FIFO))
        model.run_trace(self.TRACE)
        assert model.stats.hits == 1


class TestVictimSelection:
    def test_invalid_ways_fill_in_order_before_any_eviction(self):
        geom = CacheGeometry(size_bytes=128, block_bytes=32, ways=4)
        model = CacheModel(geom)
        results = [model.access(i * 128) for i in range(4)]

        assert [r.way for r in results] == [0, 1, 2, 3]
        assert not any(r.evicted for r in results)

    def test_eviction_only_once_the_set_is_full(self):
        geom = CacheGeometry(size_bytes=128, block_bytes=32, ways=4)
        model = CacheModel(geom)
        for i in range(4):
            model.access(i * 128)
        assert model.stats.evictions == 0

        assert model.access(4 * 128).evicted
        assert model.stats.evictions == 1


class TestWritePolicies:
    GEOM = CacheGeometry(size_bytes=64, block_bytes=32, ways=1)

    def test_write_back_defers_the_store_until_eviction(self):
        model = CacheModel(self.GEOM, RunConfig(write_policy=WritePolicy.WRITE_BACK))
        model.access(0, is_write=True)
        assert model.stats.memory_writes == 0

        model.access(64, is_write=True)  # same set, evicts the dirty line
        assert model.stats.writebacks == 1
        assert model.stats.memory_writes == 1

    def test_write_through_stores_immediately_and_never_writes_back(self):
        model = CacheModel(self.GEOM, RunConfig(write_policy=WritePolicy.WRITE_THROUGH))
        model.run_trace([(0, True), (0, True), (64, True)])

        assert model.stats.writebacks == 0
        assert model.stats.memory_writes == model.stats.writes

    def test_no_write_allocate_leaves_the_cache_untouched_on_a_write_miss(self):
        model = CacheModel(self.GEOM, RunConfig(write_allocate=False))
        model.access(0, is_write=True)
        assert model.stats.write_misses == 1

        # Nothing was allocated, so the following read must also miss.
        assert not model.access(0).hit

    def test_write_allocate_fetches_on_a_write_miss(self):
        model = CacheModel(self.GEOM, RunConfig(write_allocate=True))
        model.access(0, is_write=True)
        assert model.access(0).hit


class TestLFSR:
    def test_never_reaches_the_lock_up_state(self):
        state = 1
        for _ in range(10_000):
            state = lfsr_next(state)
            assert state != 0

    def test_stays_within_32_bits(self):
        state = 0xDEADBEEF
        for _ in range(1000):
            state = lfsr_next(state)
            assert 0 < state < 2 ** 32

    def test_does_not_cycle_early(self):
        state = 1
        seen = set()
        for _ in range(5000):
            state = lfsr_next(state)
            seen.add(state)
        assert len(seen) == 5000

    def test_is_deterministic(self):
        def sequence(seed):
            state, out = seed, []
            for _ in range(20):
                state = lfsr_next(state)
                out.append(state)
            return out

        assert sequence(99) == sequence(99)
        assert sequence(99) != sequence(100)

    def test_zero_seed_is_rejected_at_config_time(self):
        with pytest.raises(ConfigError):
            RunConfig(seed=0)


class TestInvariants:
    @pytest.mark.parametrize("policy", ALL_POLICIES)
    @pytest.mark.parametrize("ways", [1, 2, 4])
    def test_hits_and_misses_account_for_every_access(self, policy, ways):
        geom = CacheGeometry(size_bytes=2048, block_bytes=32, ways=ways)
        model = CacheModel(geom, RunConfig(policy=policy), classify=True)
        trace = repeat([i * 64 for i in range(50)], 4, write=False)
        model.run_trace(trace)

        s = model.stats
        assert s.hits + s.misses == s.accesses == len(trace)
        assert s.compulsory + s.capacity + s.conflict == s.misses
        assert s.writebacks <= s.evictions

    @pytest.mark.parametrize("ways", [1, 2, 4, 8])
    def test_compulsory_count_is_independent_of_associativity(self, ways):
        geom = CacheGeometry(size_bytes=1024, block_bytes=32, ways=ways)
        model = CacheModel(geom, classify=True)
        model.run_trace(repeat([i * 32 for i in range(40)], 3))
        assert model.stats.compulsory == 40

    @pytest.mark.parametrize("policy", ALL_POLICIES)
    def test_bigger_caches_do_not_miss_more(self, policy):
        trace = repeat([i * 64 for i in range(96)], 6)
        rates = []
        for size in (1024, 2048, 4096, 8192, 16384):
            geom = CacheGeometry(size_bytes=size, block_bytes=32, ways=4)
            model = CacheModel(geom, RunConfig(policy=policy, seed=7))
            model.run_trace(trace)
            rates.append(model.stats.miss_rate)

        assert rates == sorted(rates, reverse=True)

    def test_fully_associative_is_never_worse_than_direct_mapped(self):
        trace = repeat([i * 256 for i in range(12)], 8)
        direct = CacheModel(CacheGeometry(1024, 32, 1))
        full = CacheModel(CacheGeometry(1024, 32, 32))
        direct.run_trace(trace)
        full.run_trace(trace)

        assert full.stats.misses <= direct.stats.misses


class TestDegenerateGeometries:
    @pytest.mark.parametrize(
        "geom",
        [
            CacheGeometry(32768, 64, 1),   # direct mapped
            CacheGeometry(1024, 64, 16),   # fully associative, zero index bits
            CacheGeometry(64, 64, 1),      # a single block
            CacheGeometry(128, 64, 2),     # a single set, two ways
        ],
        ids=["direct", "fully_assoc", "single_block", "single_set"],
    )
    @pytest.mark.parametrize("policy", ALL_POLICIES)
    def test_runs_without_error(self, geom, policy):
        model = CacheModel(geom, RunConfig(policy=policy))
        model.run_trace(walk(0, 200, 64))
        assert model.stats.accesses == 200
        assert model.stats.misses > 0


class TestAddressValidation:
    def test_rejects_addresses_wider_than_the_configured_bus(self):
        model = CacheModel(CacheGeometry(1024, 32, 2, addr_width=16))
        with pytest.raises(ConfigError, match="does not fit"):
            model.access(1 << 16)


class TestHitVector:
    def test_records_one_outcome_per_access(self):
        geom = CacheGeometry(size_bytes=256, block_bytes=32, ways=2)
        trace = repeat([0, 32, 64], 3)
        vector = CacheModel(geom).hit_vector(trace)

        assert len(vector) == len(trace)
        assert vector[:3] == [False, False, False]
        assert all(vector[3:])
