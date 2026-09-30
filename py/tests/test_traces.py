import pytest

from cacheperf.config import CacheGeometry, ConfigError, Policy, RunConfig
from cacheperf.model import CacheModel
from cacheperf import traces


class TestRoundTrip:
    def test_write_then_read_preserves_accesses(self, tmp_path):
        original = [(0x40, False), (0x80, True), (0xDEADBEE0, True), (0, False)]
        path = tmp_path / "t.hex"

        assert traces.write_trace(path, original) == len(original)
        assert list(traces.read_trace(path)) == original

    def test_write_flag_is_bit_32(self, tmp_path):
        path = tmp_path / "t.hex"
        traces.write_trace(path, [(0x40, False), (0x40, True)])
        words = [int(line, 16) for line in path.read_text().split()]

        assert words == [0x40, 0x100000040]

    def test_blank_lines_are_ignored(self, tmp_path):
        path = tmp_path / "t.hex"
        path.write_text("00000040\n\n00000080\n\n")
        assert list(traces.read_trace(path)) == [(0x40, False), (0x80, False)]


class TestSequential:
    def test_covers_the_footprint_once(self):
        trace = list(traces.sequential(footprint=1024, stride=4))
        assert len(trace) == 256
        assert trace[0][0] == 0
        assert trace[-1][0] == 1020

    def test_repeats_multiply_accesses_not_footprint(self):
        trace = list(traces.sequential(footprint=1024, stride=4, repeats=3))
        assert len(trace) == 768
        assert len({a for a, _ in trace}) == 256

    def test_write_ratio_is_honoured(self):
        trace = list(traces.sequential(footprint=16384, stride=4, write_ratio=0.5, seed=3))
        writes = sum(1 for _, w in trace if w)
        assert 0.45 < writes / len(trace) < 0.55

    def test_misses_are_fixed_by_block_size(self):
        """The property that makes this trace worth having."""
        trace = list(traces.sequential(footprint=8192, stride=4))
        for ways in (1, 2, 4):
            model = CacheModel(CacheGeometry(16384, 64, ways))
            model.run_trace(trace)
            assert model.stats.misses == 8192 // 64


class TestStrided:
    @pytest.mark.parametrize("stride,expected", [(4, 256), (16, 64), (64, 16)])
    def test_access_count_follows_stride(self, stride, expected):
        assert len(list(traces.strided(footprint=1024, stride=stride))) == expected

    def test_stride_beyond_the_block_defeats_spatial_locality(self):
        """One block per access once the stride reaches the block size."""
        trace = list(traces.strided(footprint=16384, stride=64))
        model = CacheModel(CacheGeometry(32768, 64, 4))
        model.run_trace(trace)
        assert model.stats.misses == len(trace)


class TestLooping:
    def test_access_count_is_exact(self):
        assert len(list(traces.looping(working_set=1024, accesses=5000))) == 5000

    def test_stays_within_the_working_set(self):
        trace = list(traces.looping(working_set=1024, accesses=5000))
        assert max(a for a, _ in trace) < 1024

    def test_produces_a_knee_as_capacity_grows(self):
        trace = list(traces.looping(working_set=4096, accesses=20000, stride=32))
        rates = []
        for size in (512, 1024, 2048, 4096, 8192):
            model = CacheModel(CacheGeometry(size, 32, 4))
            model.run_trace(trace)
            rates.append(model.stats.miss_rate)

        assert rates[0] > 0.5
        assert rates[-1] < 0.05
        assert rates == sorted(rates, reverse=True)


class TestUniformRandom:
    def test_access_count_and_bounds(self):
        trace = list(traces.uniform_random(footprint=4096, accesses=2000, seed=5))
        assert len(trace) == 2000
        assert all(0 <= a < 4096 for a, _ in trace)

    def test_is_reproducible(self):
        a = list(traces.uniform_random(footprint=4096, accesses=500, seed=42))
        b = list(traces.uniform_random(footprint=4096, accesses=500, seed=42))
        c = list(traces.uniform_random(footprint=4096, accesses=500, seed=43))
        assert a == b and a != c


class TestPointerChase:
    def test_visits_every_node_before_repeating(self):
        trace = list(traces.pointer_chase(footprint=1024, accesses=16, node_size=64))
        assert len({a for a, _ in trace}) == 16

    def test_has_no_spatial_locality_to_exploit(self):
        """Bigger blocks should not help a random permutation of whole blocks."""
        trace = list(traces.pointer_chase(footprint=65536, accesses=4000, node_size=64, seed=2))
        model = CacheModel(CacheGeometry(4096, 64, 2))
        model.run_trace(trace)
        assert model.stats.miss_rate > 0.7


class TestThrash:
    def test_cycles_one_more_block_than_the_set_holds(self):
        trace = list(traces.thrash(ways=4, sets=8, block_bytes=32, repeats=10))
        addrs = {a for a, _ in trace}
        assert len(addrs) == 5
        assert len(trace) == 50

    def test_every_address_maps_to_the_same_set(self):
        geom = CacheGeometry(1024, 32, 4)
        trace = list(traces.thrash(ways=4, sets=geom.sets, block_bytes=32, repeats=4))
        model = CacheModel(geom)
        assert {model._decode(a)[1] for a, _ in trace} == {0}

    def test_separates_lru_from_random(self):
        """The reason this trace exists."""
        geom = CacheGeometry(1024, 32, 4)
        trace = list(traces.thrash(ways=4, sets=geom.sets, block_bytes=32, repeats=100))

        lru = CacheModel(geom, RunConfig(policy=Policy.LRU))
        rnd = CacheModel(geom, RunConfig(policy=Policy.RANDOM, seed=99))
        lru.run_trace(trace)
        rnd.run_trace(trace)

        assert lru.stats.miss_rate == 1.0
        assert rnd.stats.miss_rate < 0.8

    def test_rejects_nonsense_parameters(self):
        with pytest.raises(ConfigError):
            list(traces.thrash(ways=0))


class TestTiledMatmul:
    def test_access_count_matches_the_loop_nest(self):
        n, tile = 16, 4
        trace = list(traces.tiled_matmul(n=n, tile=tile))
        # Per (i, j, kk) tile iteration: one C read, one C write, and a read of
        # A and B for each k in the tile.
        inner = (n // tile) * n * n
        assert len(trace) == inner * (2 + 2 * tile)

    def test_touches_three_distinct_matrices(self):
        trace = list(traces.tiled_matmul(n=8, tile=4, elem_size=8))
        matrix_bytes = 8 * 8 * 8
        regions = {a // matrix_bytes for a, _ in trace}
        assert regions == {0, 1, 2}

    def test_writes_only_to_the_output_matrix(self):
        n, elem = 8, 8
        trace = list(traces.tiled_matmul(n=n, tile=4, elem_size=elem))
        c_base = 2 * n * n * elem
        assert all(a >= c_base for a, w in trace if w)

    def test_tiling_improves_the_miss_rate(self):
        geom = CacheGeometry(4096, 64, 4)
        untiled = CacheModel(geom)
        tiled = CacheModel(geom)
        untiled.run_trace(traces.tiled_matmul(n=48, tile=48))
        tiled.run_trace(traces.tiled_matmul(n=48, tile=8))

        assert tiled.stats.miss_rate < untiled.stats.miss_rate

    def test_rejects_tile_larger_than_matrix(self):
        with pytest.raises(ConfigError):
            list(traces.tiled_matmul(n=8, tile=16))


class TestImporters:
    def test_dinero(self, tmp_path):
        path = tmp_path / "t.din"
        path.write_text("# comment\n0 deadbeef\n1 cafe0000\n2 1000\n\n")
        assert list(traces.import_dinero(path)) == [
            (0xDEADBEEF, False),
            (0xCAFE0000, True),
            (0x1000, False),
        ]

    def test_dinero_rejects_unknown_op(self, tmp_path):
        path = tmp_path / "t.din"
        path.write_text("9 deadbeef\n")
        with pytest.raises(ConfigError, match="unknown access type"):
            list(traces.import_dinero(path))

    def test_dinero_rejects_malformed_line(self, tmp_path):
        path = tmp_path / "t.din"
        path.write_text("0\n")
        with pytest.raises(ConfigError, match="expected"):
            list(traces.import_dinero(path))

    def test_lackey_expands_modify_into_read_then_write(self, tmp_path):
        path = tmp_path / "t.lackey"
        path.write_text(
            "==1234== Command: ./a.out\n"
            " I  04001000,5\n"
            " L  1ffefff8,8\n"
            " S  1ffefff0,8\n"
            " M  1ffeffe8,4\n"
        )
        assert list(traces.import_lackey(path)) == [
            (0x04001000, False),
            (0x1FFEFFF8, False),
            (0x1FFEFFF0, True),
            (0x1FFEFFE8, False),
            (0x1FFEFFE8, True),
        ]


class TestSpecParsing:
    def test_bare_name(self):
        assert traces.parse_spec("sequential") == ("sequential", {})

    def test_typed_parameters(self):
        name, params = traces.parse_spec(
            "looping:working_set=8k,accesses=1000,write_ratio=0.25"
        )
        assert name == "looping"
        assert params == {"working_set": 8192, "accesses": 1000, "write_ratio": 0.25}

    def test_byte_counts_accept_suffixes(self):
        _, params = traces.parse_spec("strided:footprint=128k,stride=8k,repeats=2")
        assert params == {"footprint": 131072, "stride": 8192, "repeats": 2}

    def test_rejects_an_unreadable_size(self):
        with pytest.raises(ConfigError, match="cannot parse size"):
            traces.parse_spec("sequential:footprint=lots")

    def test_generate_builds_a_usable_trace(self):
        trace = list(traces.generate("thrash:ways=2,sets=4,block_bytes=32,repeats=3"))
        assert len(trace) == 9

    def test_rejects_unknown_generator(self):
        with pytest.raises(ConfigError, match="unknown trace"):
            traces.parse_spec("nonesuch")

    def test_rejects_bare_parameter(self):
        with pytest.raises(ConfigError, match="key=value"):
            traces.parse_spec("sequential:footprint")

    def test_rejects_non_integer_where_integer_required(self):
        with pytest.raises(ConfigError, match="must be an integer"):
            traces.parse_spec("sequential:repeats=many")

    def test_rejects_unknown_parameter_for_generator(self):
        with pytest.raises(ConfigError, match="bad parameters"):
            traces.generate("sequential:nonsense=1")

    @pytest.mark.parametrize(
        "spec,expected",
        [
            ("sequential", "sequential"),
            ("thrash:ways=4,sets=64", "thrash_ways_4_sets_64"),
            ("looping:working_set=8k", "looping_working_set_8k"),
        ],
    )
    def test_label_is_filesystem_safe(self, spec, expected):
        assert traces.trace_label(spec) == expected


class TestEveryGeneratorIsUsable:
    @pytest.mark.parametrize("name", sorted(traces.GENERATORS))
    def test_defaults_produce_a_non_empty_trace(self, name):
        trace = traces.GENERATORS[name]()
        first = next(iter(trace))
        assert isinstance(first[0], int) and isinstance(first[1], bool)
