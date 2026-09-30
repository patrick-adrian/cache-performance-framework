import pytest

from cacheperf.config import (
    CacheGeometry,
    ConfigError,
    Policy,
    RunConfig,
    WritePolicy,
    is_power_of_two,
    parse_size,
)


class TestParseSize:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("4096", 4096),
            ("4k", 4096),
            ("4K", 4096),
            ("4kB", 4096),
            ("4KiB", 4096),
            ("32k", 32768),
            ("1M", 1024 ** 2),
            ("  8k  ", 8192),
            (16384, 16384),
        ],
    )
    def test_accepts(self, text, expected):
        assert parse_size(text) == expected

    @pytest.mark.parametrize("text", ["", "abc", "4g", "-4k", "4.5k", "k"])
    def test_rejects(self, text):
        with pytest.raises(ConfigError):
            parse_size(text)

    def test_rejects_zero(self):
        with pytest.raises(ConfigError):
            parse_size(0)


def test_is_power_of_two():
    assert all(is_power_of_two(n) for n in (1, 2, 4, 8, 1024))
    assert not any(is_power_of_two(n) for n in (0, -4, 3, 6, 1000))


class TestGeometryDerivation:
    def test_set_associative(self):
        g = CacheGeometry(size_bytes=32768, block_bytes=64, ways=4)
        assert g.sets == 128
        assert g.num_blocks == 512
        assert g.offset_bits == 6
        assert g.index_bits == 7
        assert g.tag_bits == 32 - 7 - 6
        assert not g.is_direct_mapped
        assert not g.is_fully_associative

    def test_direct_mapped(self):
        g = CacheGeometry(size_bytes=32768, block_bytes=64, ways=1)
        assert g.sets == 512
        assert g.index_bits == 9
        assert g.is_direct_mapped
        assert not g.is_fully_associative

    def test_fully_associative_has_zero_index_bits(self):
        g = CacheGeometry(size_bytes=1024, block_bytes=64, ways=16)
        assert g.sets == 1
        assert g.index_bits == 0
        assert g.tag_bits == 32 - 6
        assert g.is_fully_associative

    def test_single_block_cache(self):
        g = CacheGeometry(size_bytes=64, block_bytes=64, ways=1)
        assert g.sets == 1
        assert g.num_blocks == 1
        assert g.is_direct_mapped and g.is_fully_associative

    def test_fields_partition_the_address(self):
        for size in (1024, 4096, 32768):
            for block in (16, 32, 64):
                for ways in (1, 2, 4, 8):
                    if size % (block * ways):
                        continue
                    g = CacheGeometry(size_bytes=size, block_bytes=block, ways=ways)
                    assert g.tag_bits + g.index_bits + g.offset_bits == g.addr_width


class TestGeometryValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"size_bytes": 3000, "block_bytes": 64, "ways": 1},
            {"size_bytes": 4096, "block_bytes": 48, "ways": 1},
            {"size_bytes": 4096, "block_bytes": 64, "ways": 3},
        ],
    )
    def test_rejects_non_power_of_two(self, kwargs):
        with pytest.raises(ConfigError, match="power of two"):
            CacheGeometry(**kwargs)

    def test_rejects_undersized_block(self):
        with pytest.raises(ConfigError, match="at least"):
            CacheGeometry(size_bytes=4096, block_bytes=2, ways=1)

    def test_rejects_geometry_with_no_whole_sets(self):
        # 64 blocks total, but 128 ways demanded of them.
        with pytest.raises(ConfigError, match="no whole number of sets"):
            CacheGeometry(size_bytes=4096, block_bytes=64, ways=128)

    def test_rejects_address_width_leaving_no_tag(self):
        with pytest.raises(ConfigError, match="no tag bits"):
            CacheGeometry(size_bytes=4096, block_bytes=64, ways=1, addr_width=12)

    @pytest.mark.parametrize("width", [4, 64, 128])
    def test_rejects_implausible_address_width(self, width):
        with pytest.raises(ConfigError, match="addr_width"):
            CacheGeometry(size_bytes=4096, block_bytes=64, ways=1, addr_width=width)


class TestGeometryIdentity:
    def test_label(self):
        assert CacheGeometry(32768, 64, 4).label == "32kB_b64_a4"
        assert CacheGeometry(512, 64, 1).label == "512B_b64_a1"
        assert CacheGeometry(1024 ** 2, 64, 8).label == "1mB_b64_a8"

    def test_equal_geometries_share_a_build_id(self):
        a = CacheGeometry(32768, 64, 4)
        b = CacheGeometry(32768, 64, 4)
        assert a == b
        assert a.build_id("deadbeef") == b.build_id("deadbeef")

    def test_build_id_tracks_geometry(self):
        base = CacheGeometry(32768, 64, 4).build_id("deadbeef")
        assert CacheGeometry(32768, 64, 8).build_id("deadbeef") != base
        assert CacheGeometry(16384, 64, 4).build_id("deadbeef") != base
        assert CacheGeometry(32768, 32, 4).build_id("deadbeef") != base

    def test_build_id_tracks_sources(self):
        g = CacheGeometry(32768, 64, 4)
        assert g.build_id("deadbeef") != g.build_id("cafef00d")

    def test_verilator_params(self):
        params = CacheGeometry(32768, 64, 4).verilator_params()
        assert "-GCACHE_SIZE_BYTES=32768" in params
        assert "-GBLOCK_SIZE_BYTES=64" in params
        assert "-GWAYS=4" in params

    def test_is_hashable_so_builds_can_be_deduplicated(self):
        assert len({CacheGeometry(32768, 64, 4), CacheGeometry(32768, 64, 4)}) == 1


class TestPolicyParsing:
    @pytest.mark.parametrize("text", ["lru", "LRU", " Lru "])
    def test_case_insensitive(self, text):
        assert Policy.parse(text) is Policy.LRU

    def test_rejects_unknown(self):
        with pytest.raises(ConfigError, match="unknown policy"):
            Policy.parse("mru")

    def test_encodings_are_distinct(self):
        assert len({p.value for p in Policy}) == len(Policy)

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("wb", WritePolicy.WRITE_BACK),
            ("write_back", WritePolicy.WRITE_BACK),
            ("write-through", WritePolicy.WRITE_THROUGH),
            ("wt", WritePolicy.WRITE_THROUGH),
        ],
    )
    def test_write_policy(self, text, expected):
        assert WritePolicy.parse(text) is expected


class TestRunConfig:
    def test_plusargs_cover_every_runtime_knob(self):
        args = RunConfig(policy=Policy.PLRU, miss_penalty=50, seed=7).plusargs()
        assert "+POLICY=1" in args
        assert "+MISS_PENALTY=50" in args
        assert "+SEED=7" in args
        assert "+WRITE_ALLOCATE=1" in args

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"miss_penalty": 0},
            {"hit_latency": 0},
            {"seed": -1},
            {"seed": 0},
            {"seed": 2 ** 32},
        ],
    )
    def test_rejects_out_of_range(self, kwargs):
        with pytest.raises(ConfigError):
            RunConfig(**kwargs)

    def test_label_distinguishes_policies(self):
        labels = {RunConfig(policy=p).label for p in Policy}
        assert len(labels) == len(Policy)
