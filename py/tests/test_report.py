"""Reading a sweep back, and deciding what it has to show."""

import csv

import pytest

from cacheperf.report import (
    ReportError,
    distinct,
    load,
    main,
    plot_axis,
    report,
    table,
    varying_axes,
)


def _write(path, rows):
    fieldnames = list(rows[0])
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(**overrides):
    row = {
        "size_bytes": 4096, "block_bytes": 64, "ways": 4,
        "policy": "lru", "write_policy": "write_back", "trace": "loop",
        "accesses": 1000, "hits": 900, "misses": 100,
        "cycles": 2900, "miss_rate": 0.1, "amat": 2.9,
    }
    row.update(overrides)
    return row


@pytest.fixture
def size_sweep(tmp_path):
    rows = [
        _row(size_bytes=size, miss_rate=rate, amat=1 + 20 * rate)
        for size, rate in zip([1024, 2048, 4096, 8192], [0.4, 0.2, 0.1, 0.02])
    ]
    return _write(tmp_path / "size.csv", rows)


@pytest.fixture
def policy_sweep(tmp_path):
    rows = [
        _row(policy=policy, trace=trace, miss_rate=rate, amat=1 + 20 * rate)
        for policy, trace, rate in [
            ("lru", "thrash", 1.0), ("plru", "thrash", 0.9),
            ("fifo", "thrash", 1.0), ("random", "thrash", 0.3),
            ("lru", "loop", 0.1), ("plru", "loop", 0.11),
            ("fifo", "loop", 0.12), ("random", "loop", 0.13),
        ]
    ]
    return _write(tmp_path / "policy.csv", rows)


class TestLoad:
    def test_numbers_come_back_as_numbers(self, size_sweep):
        row = load(size_sweep)[0]
        assert row["size_bytes"] == 1024
        assert row["miss_rate"] == 0.4
        assert isinstance(row["misses"], int)

    def test_labels_stay_text(self, size_sweep):
        row = load(size_sweep)[0]
        assert row["policy"] == "lru"
        assert row["trace"] == "loop"

    def test_a_missing_file_is_reported(self, tmp_path):
        with pytest.raises(ReportError, match="no such results"):
            load(tmp_path / "absent.csv")

    def test_a_header_with_no_rows_is_reported(self, tmp_path):
        path = tmp_path / "empty.csv"
        path.write_text("size_bytes,miss_rate\n")
        with pytest.raises(ReportError, match="no results"):
            load(path)


class TestAxisDetection:
    def test_only_the_swept_axis_is_reported(self, size_sweep):
        assert varying_axes(load(size_sweep)) == ["size_bytes"]

    def test_a_policy_sweep_reports_policy(self, policy_sweep):
        assert varying_axes(load(policy_sweep)) == ["policy"]

    def test_distinct_values_are_ordered(self, size_sweep):
        assert distinct(load(size_sweep), "size_bytes") == [1024, 2048, 4096, 8192]

    def test_a_single_point_has_nothing_to_show(self, tmp_path):
        path = _write(tmp_path / "one.csv", [_row()])
        assert varying_axes(load(path)) == []
        with pytest.raises(ReportError, match="no axis to plot"):
            report(path, tmp_path / "figs")


class TestTable:
    def test_the_columns_are_the_axis_values(self, size_sweep):
        text = table(load(size_sweep), "size_bytes")
        header = text.splitlines()[0]
        assert header.split()[-4:] == ["1k", "2k", "4k", "8k"]

    def test_each_remaining_axis_becomes_a_row(self, policy_sweep):
        text = table(load(policy_sweep), "policy")
        labels = [line.split()[0] for line in text.splitlines()[2:]]
        assert sorted(labels) == ["loop", "thrash"]

    def test_the_values_are_the_metric(self, size_sweep):
        text = table(load(size_sweep), "size_bytes", "miss_rate")
        assert "0.4000" in text and "0.0200" in text


class TestFigures:
    def test_a_numeric_axis_produces_a_figure(self, size_sweep, tmp_path):
        pytest.importorskip("matplotlib")
        out = plot_axis(load(size_sweep), "size_bytes", "miss_rate", tmp_path / "f.png")
        assert out.is_file() and out.stat().st_size > 0

    def test_a_categorical_axis_produces_a_figure(self, policy_sweep, tmp_path):
        pytest.importorskip("matplotlib")
        out = plot_axis(load(policy_sweep), "policy", "amat", tmp_path / "f.png")
        assert out.is_file() and out.stat().st_size > 0

    def test_one_figure_per_metric_per_swept_axis(self, size_sweep, tmp_path):
        pytest.importorskip("matplotlib")
        written = report(size_sweep, tmp_path / "figs")
        assert sorted(p.name for p in written) == [
            "amat_vs_size_bytes.png",
            "miss_rate_vs_size_bytes.png",
        ]

    def test_two_axes_give_four_figures(self, tmp_path):
        pytest.importorskip("matplotlib")
        rows = [
            _row(size_bytes=size, ways=ways, miss_rate=0.1, amat=3.0)
            for size in (1024, 2048)
            for ways in (1, 4)
        ]
        path = _write(tmp_path / "two.csv", rows)
        assert len(report(path, tmp_path / "figs")) == 4


class TestCommandLine:
    def test_tables_need_no_plotting_library(self, size_sweep, capsys):
        assert main([str(size_sweep), "--no-plots"]) == 0
        assert "cache size" in capsys.readouterr().out

    def test_a_missing_file_exits_non_zero(self, tmp_path, capsys):
        assert main([str(tmp_path / "absent.csv"), "--no-plots"]) == 1
        assert "no such results" in capsys.readouterr().err

    def test_figures_land_in_the_chosen_directory(self, size_sweep, tmp_path):
        pytest.importorskip("matplotlib")
        outdir = tmp_path / "somewhere"
        assert main([str(size_sweep), "-o", str(outdir)]) == 0
        assert sorted(p.name for p in outdir.iterdir()) == [
            "amat_vs_size_bytes.png",
            "miss_rate_vs_size_bytes.png",
        ]
