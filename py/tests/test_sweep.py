"""Matrix expansion, trace materialization and result output."""

import shutil

import pytest

from cacheperf import traces
from cacheperf.config import CacheGeometry, ConfigError, Policy, RunConfig, WritePolicy
from cacheperf.sweep import (
    PRESETS,
    Matrix,
    build_matrix,
    main,
    materialize,
    run_sweep,
    summarize,
    write_csv,
)


class TestMatrixExpansion:
    def test_the_default_matrix_is_a_single_point(self):
        points, skipped = Matrix().points()
        assert len(points) == 1
        assert skipped == []

    def test_every_axis_multiplies(self):
        matrix = Matrix(
            sizes=[4096, 8192],
            blocks=[32, 64],
            ways=[1, 4],
            policies=[Policy.LRU, Policy.FIFO],
            trace_specs=["sequential:footprint=1k", "sequential:footprint=2k"],
        )
        points, _ = matrix.points()
        assert len(points) == 2 * 2 * 2 * 2 * 2

    def test_impossible_geometries_are_dropped_not_fatal(self):
        # 1kB of 64B blocks cannot be 32-way: that is half a set.
        matrix = Matrix(sizes=[1024], blocks=[64], ways=[4, 32])
        geometries, skipped = matrix.geometries()

        assert [g.ways for g in geometries] == [4]
        assert len(skipped) == 1
        assert "32-way" in skipped[0]

    def test_an_entirely_impossible_matrix_raises(self):
        matrix = Matrix(sizes=[1024], blocks=[64], ways=[32, 64])
        with pytest.raises(ConfigError, match="no buildable configurations"):
            run_sweep(matrix)

    def test_runtime_axes_cost_no_geometries(self):
        matrix = Matrix(
            policies=list(Policy),
            write_policies=list(WritePolicy),
            miss_penalties=[10, 100],
        )
        geometries, _ = matrix.geometries()
        points, _ = matrix.points()

        assert len(geometries) == 1
        assert len(points) == 16

    def test_runs_are_distinct(self):
        runs = Matrix(policies=list(Policy), hit_latencies=[1, 2]).runs()
        assert len(set(runs)) == len(runs) == 8


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_every_preset_expands_to_something_buildable(name):
    matrix = Matrix(**PRESETS[name])
    points, skipped = matrix.points()

    assert points, f"preset {name} expands to nothing"
    assert not skipped, f"preset {name} contains an impossible geometry: {skipped}"
    for point in points:
        traces.parse_spec(point.trace)


class TestMaterialize:
    def test_a_spec_becomes_a_file(self, tmp_path):
        spec = "sequential:footprint=1k,stride=4"
        paths = materialize([spec], tmp_path)

        assert paths[spec].is_file()
        assert len(list(traces.read_trace(paths[spec]))) == 256

    def test_repeated_specs_are_written_once(self, tmp_path):
        spec = "sequential:footprint=1k"
        first = materialize([spec, spec], tmp_path)[spec]
        stamp = first.stat().st_mtime_ns

        again = materialize([spec], tmp_path)[spec]
        assert again == first
        assert again.stat().st_mtime_ns == stamp

    def test_force_rewrites(self, tmp_path):
        spec = "sequential:footprint=1k"
        path = materialize([spec], tmp_path)[spec]
        content = path.read_text()
        path.write_text("")

        materialize([spec], tmp_path, force=True)
        assert path.read_text() == content

    def test_distinct_specs_get_distinct_files(self, tmp_path):
        specs = ["sequential:footprint=1k", "sequential:footprint=2k"]
        paths = materialize(specs, tmp_path)
        assert len(set(paths.values())) == 2


def _row(**overrides):
    row = {
        "size_bytes": 4096, "block_bytes": 64, "ways": 4, "sets": 16,
        "policy": "lru", "write_policy": "write_back", "write_allocate": 1,
        "miss_penalty": 20, "hit_latency": 1, "seed": 1, "trace": "loop",
        "accesses": 100, "hits": 90, "misses": 10, "cycles": 290,
        "miss_rate": 0.1, "amat": 2.9,
    }
    row.update(overrides)
    return row


class TestOutput:
    def test_csv_round_trips(self, tmp_path):
        import csv

        path = write_csv([_row(), _row(policy="fifo")], tmp_path / "r.csv")
        with path.open(newline="") as fh:
            rows = list(csv.DictReader(fh))

        assert [r["policy"] for r in rows] == ["lru", "fifo"]
        assert rows[0]["misses"] == "10"

    def test_extra_columns_widen_the_header(self, tmp_path):
        rows = [_row(), _row(compulsory=3, capacity=5, conflict=2)]
        path = write_csv(rows, tmp_path / "r.csv")
        header = path.read_text().splitlines()[0]

        assert header.endswith("compulsory,capacity,conflict")

    def test_writing_nothing_is_an_error(self, tmp_path):
        with pytest.raises(ConfigError, match="no results"):
            write_csv([], tmp_path / "r.csv")

    def test_the_summary_leads_with_the_fastest(self):
        text = summarize([_row(amat=9.0, policy="fifo"), _row(amat=1.5, policy="plru")])
        body = text.splitlines()[2:]

        assert "plru" in body[0]
        assert "fifo" in body[1]


class TestCommandLine:
    def test_a_preset_can_be_narrowed(self):
        args = build_matrix(_namespace(preset="ways", ways="2,4"))
        assert args.ways == [2, 4]
        # Anything the flags did not mention still comes from the preset.
        assert args.sizes == PRESETS["ways"]["sizes"]

    def test_sizes_accept_suffixes(self):
        matrix = build_matrix(_namespace(size="1k,2048,1M"))
        assert matrix.sizes == [1024, 2048, 1024 ** 2]

    def test_policies_are_parsed_by_name(self):
        matrix = build_matrix(_namespace(policy="lru,random"))
        assert matrix.policies == [Policy.LRU, Policy.RANDOM]

    def test_write_allocate_takes_words_or_digits(self):
        matrix = build_matrix(_namespace(write_allocate="1,off"))
        assert matrix.write_allocate == [True, False]

    def test_an_unreadable_boolean_is_rejected(self):
        with pytest.raises(ConfigError, match="boolean"):
            build_matrix(_namespace(write_allocate="maybe"))

    def test_dry_run_reports_without_building(self, capsys):
        assert main(["--preset", "policy", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "configurations" in out and "builds" in out

    def test_an_impossible_matrix_exits_non_zero(self, capsys):
        assert main(["--size", "1k", "--block", "64", "--ways", "64"]) == 1


def _namespace(**overrides):
    import argparse

    fields = dict(
        preset=None, size=None, block=None, ways=None, policy=None,
        write_policy=None, write_allocate=None, miss_penalty=None,
        hit_latency=None, seed=None, trace=None, addr_width=32,
    )
    fields.update(overrides)
    return argparse.Namespace(**fields)


@pytest.mark.rtl
@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not on PATH")
def test_a_small_sweep_produces_one_row_per_point(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHEPERF_CACHE", str(tmp_path / "cache"))
    matrix = Matrix(
        sizes=[1024],
        blocks=[32],
        ways=[1, 4],
        policies=[Policy.LRU, Policy.RANDOM],
        trace_specs=["looping:working_set=4k,accesses=2000"],
    )
    rows = run_sweep(matrix)

    assert len(rows) == 4
    assert {r["ways"] for r in rows} == {1, 4}
    for row in rows:
        assert row["accesses"] == 2000
        assert row["hits"] + row["misses"] == 2000


@pytest.mark.rtl
@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not on PATH")
def test_classification_accounts_for_every_miss(tmp_path, monkeypatch):
    monkeypatch.setenv("CACHEPERF_CACHE", str(tmp_path / "cache"))
    matrix = Matrix(
        sizes=[1024], blocks=[32], ways=[4],
        trace_specs=["looping:working_set=4k,accesses=2000"],
    )
    row = run_sweep(matrix, with_classification=True)[0]

    assert row["compulsory"] + row["capacity"] + row["conflict"] == row["misses"]
    assert row["compulsory"] == 4096 // 32
