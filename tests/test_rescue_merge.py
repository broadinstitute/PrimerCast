"""Tests for merging rescue results back into the per-primer-set reports.

The report directory these files live in is what the GUI results page lists,
so the merge must never leave an intermediate name visible there, and must
never rename one report over another.
"""

import pandas as pd
import pytest

from primercast.commands.rescue_evaluate import _merge_final_xlsx

DETAIL_COLS = ["seq_id", "target", "decision", "regressor"]


def _write_report(path, rows):
    """Write a minimal report workbook with the summary/detail sheets."""
    summary = pd.DataFrame([["Sensitivity"], ["cov", 0, 0]])
    detail = pd.DataFrame(rows, columns=DETAIL_COLS)
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="summary", index=False, header=False)
        detail.to_excel(writer, sheet_name="detail", index=False)


@pytest.fixture
def dirs(tmp_path):
    original = tmp_path / "report"
    rescue = original / "rescue"
    rescue.mkdir(parents=True)
    return original, rescue


def test_merge_replaces_report_in_place(dirs):
    original, rescue = dirs
    _write_report(original / "pset.xlsx",
                  [["s1", "t", 0, 0.1], ["s2", "t", 1, 0.9]])
    _write_report(rescue / "pset.xlsx",
                  [["s1", "t", 1, 0.8]])

    assert _merge_final_xlsx(original, rescue) == 1

    detail = pd.read_excel(original / "pset.xlsx", sheet_name="detail")
    rescued = detail.loc[detail["seq_id"] == "s1"].iloc[0]
    assert rescued["decision"] == 1
    assert rescued["regressor"] == pytest.approx(0.8)


def test_merge_leaves_no_intermediate_files_behind(dirs):
    """A `_final.xlsx` stray would show up as a phantom entry in the GUI."""
    original, rescue = dirs
    _write_report(original / "pset.xlsx", [["s1", "t", 0, 0.1]])
    _write_report(rescue / "pset.xlsx", [["s1", "t", 1, 0.8]])

    _merge_final_xlsx(original, rescue)

    assert [p.name for p in sorted(original.iterdir())] == ["pset.xlsx"]


def test_merge_does_not_clobber_a_report_named_final(dirs):
    """`pset_final.xlsx` is a primer set in its own right, not an artifact."""
    original, rescue = dirs
    _write_report(original / "pset.xlsx", [["s1", "t", 0, 0.1]])
    _write_report(original / "pset_final.xlsx", [["s9", "t", 1, 0.5]])
    _write_report(rescue / "pset.xlsx", [["s1", "t", 1, 0.8]])

    _merge_final_xlsx(original, rescue)

    assert (original / "pset_final.xlsx").exists()
    survivor = pd.read_excel(original / "pset_final.xlsx", sheet_name="detail")
    assert survivor["seq_id"].tolist() == ["s9"]


def test_merge_without_improvements_leaves_everything_alone(dirs):
    original, rescue = dirs
    _write_report(original / "pset.xlsx", [["s1", "t", 0, 0.1]])
    _write_report(rescue / "pset.xlsx", [["s1", "t", 0, 0.2]])

    assert _merge_final_xlsx(original, rescue) == 0

    detail = pd.read_excel(original / "pset.xlsx", sheet_name="detail")
    assert detail.iloc[0]["regressor"] == pytest.approx(0.1)
    assert [p.name for p in sorted(original.iterdir())] == ["pset.xlsx"]


def test_merge_removes_the_rescue_directory(dirs):
    original, rescue = dirs
    _write_report(original / "pset.xlsx", [["s1", "t", 0, 0.1]])
    _write_report(rescue / "pset.xlsx", [["s1", "t", 1, 0.8]])

    _merge_final_xlsx(original, rescue)

    assert not rescue.exists()
