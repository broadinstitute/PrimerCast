"""Tests for per-step filter diagnostics (primercast.utils.diagnostics)."""

import argparse
import json
import os
from unittest.mock import patch

import pytest

from primercast import adapt_cli
from primercast.commands import filter_primers, generate, quick_design
from primercast.utils.diagnostics import (
    NO_CANDIDATES_EXIT_CODE,
    StageDiagnostics,
    diagnostics_path,
    explain_run,
    format_message,
)


def _no_dimers(pairs):
    return [0.0] * len(pairs)


def _write_diag(path, stage, target, steps, fatal=True):
    diag = StageDiagnostics(stage, target=target, fatal=fatal)
    for name, remaining in steps:
        diag.record(name, remaining)
    return diag.write(path)


# ---------------------------------------------------------------------------
# StageDiagnostics / format_message
# ---------------------------------------------------------------------------

def test_message_names_blocking_filter_with_params_and_breakdown():
    diag = StageDiagnostics("generate", target="virusA")
    diag.record("Tiling", 1234)
    diag.record("Tm / GC", 0, params={"TM_MIN": 55.0, "TM_MAX": 60.5},
                breakdown={"forward": 0, "reverse": 5}, hint="Widen the Tm range.")
    diag.record("Self-dimer dG", 0)

    assert diag.empty
    assert diag.blocking_step()[0] == 1
    assert diag.message() == (
        "Primer generation (virusA): no candidates left after the 'Tm / GC' filter "
        "(1,234 -> 0). Remaining by type: forward 0, reverse 5. "
        "Controlled by: TM_MIN=55, TM_MAX=60.5. Widen the Tm range."
    )


def test_message_when_first_step_is_empty():
    data = StageDiagnostics("filter", unit="primer pairs").to_dict()
    data["steps"] = [{"filter": "Scored primer pairs", "remaining": 0}]
    assert format_message(data) == (
        "Primer pair filtering: no primer pairs to start with at 'Scored primer pairs'."
    )


def test_finish_exits_when_fatal_step_is_empty(tmp_path, capsys):
    out = tmp_path / "x_init.fa"
    diag = StageDiagnostics("generate", target="x")
    diag.record("Tiling", 0)

    with pytest.raises(SystemExit) as exc:
        diag.finish(out)

    assert exc.value.code == NO_CANDIDATES_EXIT_CODE
    assert "NO CANDIDATES: Primer generation (x)" in capsys.readouterr().err
    data = json.loads(diagnostics_path(out).read_text())
    assert data["empty"] and data["stage"] == "generate"


def test_finish_does_not_exit_when_not_empty_or_not_fatal(tmp_path):
    ok = StageDiagnostics("generate")
    ok.record("Tiling", 3)
    ok.finish(tmp_path / "a.fa")

    off_target = StageDiagnostics("prepare_input", fatal=False)
    off_target.record("Amplicon length", 0)
    off_target.finish(tmp_path / "b.input")

    assert diagnostics_path(tmp_path / "a.fa").is_file()
    assert json.loads(diagnostics_path(tmp_path / "b.input").read_text())["empty"]


# ---------------------------------------------------------------------------
# explain_run
# ---------------------------------------------------------------------------

def test_explain_run_orders_by_stage_and_skips_non_fatal_and_bad_files(tmp_path):
    run = tmp_path / "run"
    (run / "_virusA").mkdir(parents=True)
    _write_diag(run / "_virusA" / "virusA_filt.fa", "filter", "virusA", [("Coverage", 0)])
    _write_diag(run / "virusA_init.fa", "generate", "virusA", [("Tiling", 0)])
    _write_diag(run / "ok_init.fa", "generate", "ok", [("Tiling", 4)])
    _write_diag(run / "off.input", "prepare_input", "host", [("Amplicon length", 0)], fatal=False)
    (run / "broken.input.diagnostics.json").write_text("{not json")

    found = explain_run(run)
    assert [(d.stage, d.target) for d in found] == [("generate", "virusA"), ("filter", "virusA")]
    assert found[0].stage_label == "Primer generation"

    with_off = explain_run(run, include_non_fatal=True)
    assert ("prepare_input", "host") in [(d.stage, d.target) for d in with_off]


def test_explain_run_since_ignores_older_files(tmp_path):
    path = _write_diag(tmp_path / "old_init.fa", "generate", "old", [("Tiling", 0)])
    os.utime(path, (1_000, 1_000))
    assert explain_run(tmp_path, since=2_000) == []
    assert len(explain_run(tmp_path)) == 1


def test_explain_run_missing_dir(tmp_path):
    assert explain_run(tmp_path / "missing") == []


# ---------------------------------------------------------------------------
# Commands stop with the blocking filter
# ---------------------------------------------------------------------------

@pytest.fixture
def params_file(tmp_path):
    def _make(**overrides):
        values = {"PRIMER_LEN_MIN": 20, "PRIMER_LEN_MAX": 20, "AMPLEN_MIN": 30,
                  "AMPLEN_MAX": 200, "TM_MIN": 0, "TM_MAX": 100, "GC_MAX": 100,
                  "DG_MIN": -100}
        values.update(overrides)
        path = tmp_path / "params.txt"
        path.write_text("".join(f"{k} = {v}\n" for k, v in values.items()))
        return str(path)
    return _make


@patch("primercast.commands.generate.compute_batch_dimer_dg", side_effect=_no_dimers)
def test_generate_stops_at_tm_gc_filter(mock_dg, tmp_path, params_file):
    target = tmp_path / "virusA.fa"
    target.write_text(">s1\n" + "ATCGATCGTTAGCATGCAAGTCCGATTACGGATCCATGCAAGTTGACCTAGG" * 3 + "\n")
    out = tmp_path / "virusA_init.fa"
    args = argparse.Namespace(target_seqs=str(target), primer_seqs=str(out),
                              param_file=params_file(TM_MIN=0, TM_MAX=0),
                              name="virusA", seed=None)

    with pytest.raises(SystemExit) as exc:
        generate.run(args)

    assert exc.value.code == NO_CANDIDATES_EXIT_CODE
    (diagnosis,) = explain_run(tmp_path)
    blocking = next(s for s in diagnosis.steps if s["remaining"] == 0)
    assert blocking["filter"] == "Tm / GC"
    assert "TM_MIN=0, TM_MAX=0" in diagnosis.message


def test_filter_stops_when_no_scored_pairs(tmp_path, params_file):
    scores = tmp_path / "virusA.eval"
    scores.write_text("")
    out = tmp_path / "virusA_filt.fa"
    args = argparse.Namespace(scores=str(scores), init=str(tmp_path / "virusA_init.fa"),
                              out=str(out), params=params_file(), probe_mapping=None,
                              probe_seqs=None, probe_out=None, probe_pair_csv=None)

    with pytest.raises(SystemExit) as exc:
        filter_primers.run(args)

    assert exc.value.code == NO_CANDIDATES_EXIT_CODE
    (diagnosis,) = explain_run(tmp_path)
    assert diagnosis.stage == "filter" and diagnosis.target == "virusA"
    assert "'Scored primer pairs'" in diagnosis.message


@patch("primercast.commands.quick_design.compute_batch_dimer_dg", side_effect=_no_dimers)
def test_quick_design_primer_extraction_records_funnel(mock_dg):
    seq = "ATCGATCGTTAGCATGCAAGTCCGATTACGGATCCATGCAAGTTGACCTAGGCATGCATGCAAGTC"
    diag = StageDiagnostics("quick_design", target="virusA", unit="primer pairs")

    primers, _ = quick_design.extract_primers_at_positions(
        [seq, seq], [(0, 20, 45, 20, 1.0)], 20,
        min_tm=0, max_tm=0, max_gc=100, min_dg=-100, diag=diag,
    )

    assert primers == []
    assert diag.blocking_step()[1]["filter"] == "Primer Tm / GC"


def test_quick_design_write_output_stops_without_results(tmp_path):
    diag = StageDiagnostics("quick_design", target="virusA", unit="primer pairs")
    diag.record("Top pairs by position score", 12)
    quick_design._record_batch_funnel(
        diag, dict(quick_design._new_batch_funnel(), aligned_f=5, aligned_r=4),
        {"min_amp_len": 60, "max_amp_len": 200},
    )
    args = argparse.Namespace(out=str(tmp_path / "virusA.eval"))

    with pytest.raises(SystemExit) as exc:
        quick_design._write_output(args, [], [], {}, 0.9, 0.9, 1, str(tmp_path), diag=diag)

    assert exc.value.code == NO_CANDIDATES_EXIT_CODE
    (diagnosis,) = explain_run(tmp_path)
    assert "Primer coverage" in diagnosis.message and "(4 -> 0)" in diagnosis.message


def test_adapt_cli_prints_explanation(tmp_path, capsys):
    _write_diag(tmp_path / "virusA_init.fa", "generate", "virusA", [("Tiling", 7), ("Tm / GC", 0)])
    adapt_cli._print_failure_explanation(tmp_path)
    err = capsys.readouterr().err
    assert "a filter removed every candidate" in err
    assert "'Tm / GC' filter (7 -> 0)" in err
