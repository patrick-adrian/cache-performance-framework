"""RTL against the reference model, per access.

This is the correctness gate for the framework: a miss rate the RTL and the
model disagree about is a miss rate nobody should believe. The comparison is the
full hit/miss sequence rather than the totals, because two caches can reach the
same totals by different routes and only one of them is right.
"""

import itertools
import shutil

import pytest

from cacheperf import traces
from cacheperf.build import build_all
from cacheperf.config import CacheGeometry, Policy, RunConfig, WritePolicy
from cacheperf.model import CacheModel
from cacheperf.run import simulate

pytestmark = [
    pytest.mark.rtl,
    pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not on PATH"),
]

GEOMETRIES = [
    CacheGeometry(1024, 32, 1),    # direct mapped
    CacheGeometry(1024, 32, 4),    # set associative
    CacheGeometry(1024, 64, 16),   # fully associative: no index bits at all
    CacheGeometry(64, 64, 1),      # a single block
]

TRACES = {
    "mixed": lambda: traces.uniform_random(
        footprint=4096, accesses=600, write_ratio=0.3, seed=4
    ),
    "loop": lambda: traces.looping(
        working_set=2048, accesses=600, stride=16, write_ratio=0.2
    ),
    "thrash": lambda: traces.thrash(ways=4, sets=8, block_bytes=32, repeats=60),
}


@pytest.fixture(scope="module")
def built():
    """One Verilator build per geometry, shared by every case in this module."""
    build_all(GEOMETRIES)


@pytest.fixture(scope="module")
def trace_files(tmp_path_factory):
    directory = tmp_path_factory.mktemp("traces")
    files = {}
    for name, make in TRACES.items():
        path = directory / f"{name}.hex"
        traces.write_trace(path, make())
        files[name] = path
    return files


def golden(geometry, run, name):
    model = CacheModel(geometry, run)
    sequence = "".join("1" if h else "0" for h in model.hit_vector(TRACES[name]()))
    return model, sequence


COUNTERS = (
    "accesses", "reads", "writes", "hits", "misses",
    "read_hits", "read_misses", "write_hits", "write_misses",
    "evictions", "writebacks", "memory_reads", "memory_writes", "cycles",
)


@pytest.mark.parametrize("name", sorted(TRACES))
@pytest.mark.parametrize("policy", list(Policy))
@pytest.mark.parametrize("geometry", GEOMETRIES, ids=lambda g: g.label)
def test_hit_sequence_matches_the_model(built, trace_files, geometry, policy, name):
    run = RunConfig(policy=policy, seed=7)
    result = simulate(geometry, run, trace_files[name], dump=True)
    _, expected = golden(geometry, run, name)

    assert len(result.hits) == len(expected)
    index = next(
        (i for i, (a, b) in enumerate(zip(result.hits, expected)) if a != b), None
    )
    assert index is None, f"first divergence at access {index}"


@pytest.mark.parametrize("name", sorted(TRACES))
@pytest.mark.parametrize("policy", list(Policy))
@pytest.mark.parametrize("geometry", GEOMETRIES, ids=lambda g: g.label)
def test_counters_match_the_model(built, trace_files, geometry, policy, name):
    run = RunConfig(policy=policy, seed=7)
    result = simulate(geometry, run, trace_files[name])
    model, _ = golden(geometry, run, name)
    row = model.stats.as_row()

    assert {k: result.counters[k] for k in COUNTERS} == {k: row[k] for k in COUNTERS}


WRITE_MODES = [
    (WritePolicy.WRITE_BACK, True),
    (WritePolicy.WRITE_BACK, False),
    (WritePolicy.WRITE_THROUGH, True),
    (WritePolicy.WRITE_THROUGH, False),
]


@pytest.mark.parametrize("write_policy,write_allocate", WRITE_MODES)
def test_write_handling_matches_the_model(built, trace_files, write_policy, write_allocate):
    geometry = CacheGeometry(1024, 32, 4)
    run = RunConfig(write_policy=write_policy, write_allocate=write_allocate)
    result = simulate(geometry, run, trace_files["mixed"], dump=True)
    model, expected = golden(geometry, run, "mixed")

    assert result.hits == expected
    assert {k: result.counters[k] for k in COUNTERS} == {
        k: model.stats.as_row()[k] for k in COUNTERS
    }


@pytest.mark.parametrize("hit_latency,miss_penalty", [(1, 1), (1, 20), (2, 7), (4, 3)])
def test_timing_parameters_match_the_model(built, trace_files, hit_latency, miss_penalty):
    geometry = CacheGeometry(1024, 32, 4)
    run = RunConfig(hit_latency=hit_latency, miss_penalty=miss_penalty)
    result = simulate(geometry, run, trace_files["loop"])
    model, _ = golden(geometry, run, "loop")

    assert result.counters["cycles"] == model.stats.as_row()["cycles"]


class TestCycleAccounting:
    """Checked against the arithmetic directly rather than against the model, so
    that a shared misconception could not satisfy both."""

    GEOM = CacheGeometry(64, 64, 1)  # one block, so every new address evicts

    def test_a_hit_costs_the_hit_latency(self, built, tmp_path):
        path = tmp_path / "t.hex"
        traces.write_trace(path, [(0, False), (0, False), (0, False)])
        run = RunConfig(hit_latency=3, miss_penalty=10)
        result = simulate(self.GEOM, run, path)

        # One compulsory miss at 3 + 10, then two hits at 3 each.
        assert result.counters["cycles"] == 13 + 3 + 3

    def test_a_clean_miss_costs_one_penalty(self, built, tmp_path):
        path = tmp_path / "t.hex"
        traces.write_trace(path, [(0, False), (64, False), (128, False)])
        run = RunConfig(hit_latency=1, miss_penalty=20)
        result = simulate(self.GEOM, run, path)

        assert result.counters["misses"] == 3
        assert result.counters["writebacks"] == 0
        assert result.counters["cycles"] == 3 * (1 + 20)

    def test_a_dirty_miss_costs_two(self, built, tmp_path):
        path = tmp_path / "t.hex"
        traces.write_trace(path, [(0, True), (64, True)])
        run = RunConfig(hit_latency=1, miss_penalty=20)
        result = simulate(self.GEOM, run, path)

        # The first write allocates cleanly; the second must drain it first.
        assert result.counters["writebacks"] == 1
        assert result.counters["cycles"] == (1 + 20) + (1 + 40)

    def test_write_around_does_not_disturb_the_cache(self, built, tmp_path):
        path = tmp_path / "t.hex"
        traces.write_trace(path, [(0, False), (64, True), (0, False)])
        run = RunConfig(hit_latency=1, miss_penalty=20, write_allocate=False)
        result = simulate(self.GEOM, run, path)

        # The write misses and goes straight to memory, so the read still hits.
        assert result.counters["hits"] == 1
        assert result.counters["memory_reads"] == 1
        assert result.counters["memory_writes"] == 1
        assert result.counters["cycles"] == (1 + 20) + (1 + 20) + 1


def test_random_policy_is_reproducible(built, trace_files):
    geometry = CacheGeometry(1024, 32, 4)
    run = RunConfig(policy=Policy.RANDOM, seed=12345)
    first = simulate(geometry, run, trace_files["thrash"], dump=True)
    second = simulate(geometry, run, trace_files["thrash"], dump=True)

    assert first.counters == second.counters
    assert first.hits == second.hits


def test_random_policy_tracks_its_seed(built, trace_files):
    geometry = CacheGeometry(1024, 32, 4)
    outcomes = {
        simulate(
            geometry, RunConfig(policy=Policy.RANDOM, seed=s), trace_files["thrash"]
        ).counters["hits"]
        for s in (1, 12345, 0xDEADBEEF)
    }
    assert len(outcomes) > 1


def test_policies_diverge_on_the_thrash_trace(built, trace_files):
    """If every policy scores the same here, the runtime mux is not selecting."""
    geometry = CacheGeometry(1024, 32, 4)
    hits = {
        policy: simulate(
            geometry, RunConfig(policy=policy, seed=7), trace_files["thrash"]
        ).counters["hits"]
        for policy in Policy
    }

    assert hits[Policy.LRU] == 0
    assert hits[Policy.FIFO] == 0
    assert hits[Policy.RANDOM] > 0


@pytest.mark.parametrize(
    "geometry,policy",
    list(itertools.product(GEOMETRIES, list(Policy))),
    ids=lambda x: x.label if isinstance(x, CacheGeometry) else str(x),
)
def test_every_access_is_accounted_for(built, trace_files, geometry, policy):
    result = simulate(geometry, RunConfig(policy=policy), trace_files["mixed"])
    c = result.counters

    assert c["hits"] + c["misses"] == c["accesses"]
    assert c["reads"] + c["writes"] == c["accesses"]
    assert c["writebacks"] <= c["evictions"]
    assert c["memory_reads"] <= c["misses"]
