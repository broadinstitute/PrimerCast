"""Results-page checks against actual final-output schemas and empty runs."""

import json

import pandas as pd
import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from gui.results import coverage_fraction, result_summary


@pytest.fixture(autouse=True)
def restore_main_module():
    # Streamlit's runner installs a script-specific __main__; do not leak it
    # into the model-pickle tests that run later in the same pytest process.
    import sys
    original = sys.modules["__main__"]
    yield
    sys.modules["__main__"] = original


@pytest.mark.parametrize("value,expected", [("7 / 10", 0.7), (0.9, 0.9), ("0 / 5", 0.0),
                                           ("0 / 0", None), ("bad", None), (float("nan"), None)])
def test_coverage_formats(value, expected):
    assert coverage_fraction(value) == expected


def test_final_schema_summary():
    df = pd.DataFrame({"cov_target": ["8 / 10", "9 / 10"], "sco_target": [0.4, 0.7],
                       "valid_probes": ["p1", None], "sco_host": [0, 0], "sco_primer_only": [0.5, 0.8]})
    result = result_summary(df)
    assert result == dict(pairs=2, best_coverage=0.9, best_score=0.7, pairs_with_probe=1, off_panels=1)


@pytest.mark.parametrize("empty", [False, True])
def test_results_page_renders_saved_diagnostics(tmp_path, empty):
    csv = tmp_path / "synthetic_final.csv"
    df = pd.DataFrame({"pname_f": ["f1"], "pname_r": ["r1"], "pseq_f": ["ACGT"],
                       "pseq_r": ["TGCA"], "cov_target": ["9 / 10"], "sco_target": [0.7]})
    df.iloc[:0 if empty else 1].to_csv(csv, index=False)
    (tmp_path / "synthetic_filt.diagnostics.json").write_text(json.dumps({
        "stage": "filter", "status": "no_candidates" if empty else "complete",
        "reason": "All pairs fail the dimer threshold." if empty else "Candidates passed.",
        "counts": {"evaluated_pairs": 10, "selected_pairs": 0 if empty else 1}}))
    app = AppTest.from_string(
        "from pathlib import Path\n"
        "from gui.results import render_run_details, render_design_result\n"
        f"render_run_details(Path({str(tmp_path)!r}))\n"
        f"render_design_result(Path({str(csv)!r}))\n"
    ).run()
    assert not app.exception
    assert app.metric[0].value == ("0" if empty else "1")
    assert app.metric[1].value == ("Unknown" if empty else "90.0%")
    if empty:
        assert any("No valid primer pairs" in x.value for x in app.warning)
    else:
        assert app.selectbox[0].options == ["1. f1 / r1"]


def test_results_page_explains_an_evaluation_that_never_aligned(tmp_path):
    """Alignment-stage diagnostics reach the user, not just the log file."""
    target_dir = tmp_path / "_H1"
    target_dir.mkdir()
    (target_dir / "pset.H1.input.diagnostics.json").write_text(json.dumps({
        "stage": "prepare_input", "status": "no_alignments",
        "reason": "0 of 2 primer(s) aligned to H1. Check that the primers match this reference.",
        "counts": {"primers": 2, "alignments": 0, "pairs": 0}}))
    (tmp_path / "run_status.json").write_text(json.dumps({
        "status": "no_candidates", "return_code": 0,
        "reason": "Evaluation finished without usable results."}))

    app = AppTest.from_string(
        "from pathlib import Path\nfrom gui.results import render_run_details\n"
        f"render_run_details(Path({str(tmp_path)!r}))\n"
    ).run()
    assert not app.exception
    assert any("0 of 2 primer(s) aligned to H1" in x.value for x in app.warning)
    assert any("Alignment and amplicon check · H1" in x.value for x in app.markdown)


def test_blank_legacy_result_is_not_a_read_error(tmp_path):
    csv = tmp_path / "old_final.csv"
    csv.write_text("")
    app = AppTest.from_string(
        "from pathlib import Path\nfrom gui.results import render_design_result\n"
        f"render_design_result(Path({str(csv)!r}))"
    ).run()
    assert not app.exception
    assert not app.error
    assert app.metric[0].value == "0"
