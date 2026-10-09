"""Tests for per-step filter diagnostics (primercast.utils.diagnostics)."""

import argparse
import json
import os
from unittest.mock import patch

import pytest

import pandas as pd

from primercast import adapt_cli
from primercast.commands import (
    evaluate,
    export_report,
    filter_primers,
    generate,
    quick_design,
    rescue_evaluate,
)
from primercast.utils.diagnostics import (
    NO_CANDIDATES_EXIT_CODE,
    RESCUE_SUFFIX,
    StageDiagnostics,
    append_step,
    diagnostics_path,
    explain_run,
    format_losses,
    format_message,
    funnel_losses,
    run_funnels,
    run_warnings,
    step_baselines,
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
# run_funnels: where filters narrowed a run, including successful ones
# ---------------------------------------------------------------------------

def _quick_diag():
    diag = StageDiagnostics("quick_design", target="virusA", unit="primer pairs")
    diag.record("Sequences in alignment", 50, unit="sequences")
    diag.record("Amplicon positions", 100)
    diag.record("Primer Tm / GC", 40)
    diag.record("Top pairs by position score", 10, summarize=False)
    diag.record("Primer alignments", 8, unit="primers")
    diag.record("Primer pairing", 10)
    diag.record("Probe candidates", 2000, unit="probes")
    diag.record("Probe inside amplicon", 5)
    return diag


def test_step_baselines_compare_only_within_a_unit():
    steps = _quick_diag().steps
    assert step_baselines(steps) == [None, None, 100, 40, None, 10, None, 10]


def test_step_baselines_treat_a_larger_count_as_a_different_set():
    steps = [{"filter": "Pairs", "remaining": 6}, {"filter": "With a probe", "remaining": 3},
             {"filter": "All pairs again", "remaining": 6}]
    assert step_baselines(steps) == [None, 6, None]


def test_funnel_losses_skip_unsummarized_steps_and_rank_by_fraction():
    losses = funnel_losses(_quick_diag().steps)
    assert [(step["filter"], before) for step, before in losses] == [
        ("Primer Tm / GC", 100), ("Probe inside amplicon", 10)]
    assert format_losses(_quick_diag().to_dict()) == (
        "Quick design (virusA): Primer Tm / GC 100 -> 40 (-60%); "
        "Probe inside amplicon 10 -> 5 (-50%)")


def test_format_losses_empty_when_nothing_removed():
    diag = StageDiagnostics("filter", target="virusA")
    diag.record("Scored primer pairs", 4)
    diag.record("Coverage", 4)
    assert format_losses(diag.to_dict()) == ""


def test_run_funnels_lists_successful_steps_and_skips_offtarget_and_batches(tmp_path):
    run = tmp_path / "run"
    (run / "_virusA").mkdir(parents=True)
    _quick_diag().write(run / "_virusA" / "virusA.virusA.eval")
    _write_diag(run / "_virusA" / "batch_0.input", "prepare_input", "virusA", [("Amplicon length", 3)])
    _write_diag(run / "virusA_final.csv", "build_output", "virusA", [("On-target", 5), ("Top", 5)])
    _write_diag(run / "virusA_init.fa", "generate", "virusA", [("Tiling", 9), ("Tm / GC", 6)])
    off = StageDiagnostics("prepare_input", target="human", fatal=False, offtarget=True)
    off.record("Amplicon length", 0)
    off.write(run / "pset.human.input")
    (run / "broken.input.diagnostics.json").write_text("{not json")

    funnels = run_funnels(run)
    assert [(f.stage, f.target) for f in funnels] == [
        ("generate", "virusA"), ("quick_design", "virusA"), ("build_output", "virusA")]
    assert funnels[0].summary == "Primer generation (virusA): Tm / GC 9 -> 6 (-33%)"
    assert funnels[2].summary == ""
    assert run_funnels(tmp_path / "missing") == []


def test_run_funnels_since_ignores_older_files(tmp_path):
    path = _write_diag(tmp_path / "old_init.fa", "generate", "old", [("Tiling", 4)])
    os.utime(path, (1_000, 1_000))
    assert run_funnels(tmp_path, since=2_000) == []
    assert len(run_funnels(tmp_path)) == 1


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


# ---------------------------------------------------------------------------
# Evaluate mode: zero-coverage and rescue warnings
# ---------------------------------------------------------------------------

def _eval_args(tmp_path, input_text="", fail_if_empty=False):
    ref = tmp_path / "virusA.fa"
    ref.write_text(">s1\nACGT\n>s2\nACGT\n>s3\nACGT\n")
    inp = tmp_path / "pset.virusA.input"
    inp.write_text(input_text)
    return argparse.Namespace(input=str(inp), output=str(tmp_path / "pset.virusA.eval"),
                              reference=str(ref), reftype="on", threads=1,
                              fail_if_empty=fail_if_empty)


def test_evaluate_without_pairs_warns_with_prepare_input_cause(tmp_path, capsys):
    args = _eval_args(tmp_path)
    _write_diag(args.input, "prepare_input", "virusA",
                [("Primer alignments to virusA", 0)], fatal=False)

    with pytest.raises(SystemExit) as exc:
        evaluate.run(args)

    assert exc.value.code is None  # the run continues to the report
    assert "WARNING: ML evaluation (virusA)" in capsys.readouterr().err
    (warning,) = run_warnings(tmp_path)
    assert warning.kind == "no_coverage"
    assert [s["remaining"] for s in warning.steps] == [3, 0, 0]
    assert "'Reached by a primer pair" in warning.message
    assert "Cause: Primer binding and pairing (virusA)" in warning.message


def test_evaluate_without_pairs_stops_when_fatal(tmp_path):
    with pytest.raises(SystemExit) as exc:
        evaluate.run(_eval_args(tmp_path, fail_if_empty=True))

    assert exc.value.code == NO_CANDIDATES_EXIT_CODE
    assert run_warnings(tmp_path) == []  # reported as a failure instead
    assert explain_run(tmp_path)[0].stage == "evaluate"


def test_coverage_diagnostics_counts_targets_and_pairs(tmp_path):
    args = _eval_args(tmp_path)
    index = pd.MultiIndex.from_tuples([("p1_for", "p1_rev"), ("p2_for", "p2_rev")])
    clstbl = pd.DataFrame({"s1": [0.9, 0.2], "s2": [0.1, 0.3]}, index=index)

    diag = evaluate._coverage_diagnostics(args, ["s1", "s2", "s3"], {"s1", "s2"}, clstbl)

    assert [s["remaining"] for s in diag.steps] == [3, 2, 1]
    assert not diag.empty
    assert diag.details["pair_coverage"] == {"p1_for/p1_rev": 1, "p2_for/p2_rev": 0}
    assert evaluate._coverage_diagnostics(
        argparse.Namespace(**{**vars(args), "reftype": "off"}), [], set(), clstbl) is None


def test_run_warnings_selects_non_fatal_empty_evaluations_and_rescues(tmp_path):
    _write_diag(tmp_path / "a.virusA.eval", "evaluate", "virusA",
                [("Target sequences in reference", 3), ("Predicted to amplify", 0)], fatal=False)
    _write_diag(tmp_path / "b.virusA.eval", "evaluate", "virusA",
                [("Target sequences in reference", 3), ("Predicted to amplify", 2)], fatal=False)
    _write_diag(tmp_path / "c.host.input", "prepare_input", "host", [("Amplicon length", 0)],
                fatal=False)
    (tmp_path / f"a.virusA.eval{RESCUE_SUFFIX}").write_text(
        json.dumps({"target": "virusA", "message": "Rescue re-evaluation was triggered"}))

    found = run_warnings(tmp_path)

    assert [w.kind for w in found] == ["no_coverage", "rescue"]
    assert "(3 -> 0)" in found[0].message
    assert found[1].message == "Rescue re-evaluation was triggered"


def test_append_step_clears_warning_when_rescue_recovers_targets(tmp_path):
    eval_path = tmp_path / "a.virusA.eval"
    _write_diag(eval_path, "evaluate", "virusA",
                [("Target sequences in reference", 3), ("Predicted to amplify", 0)], fatal=False)

    data = append_step(eval_path, "Predicted to amplify after rescue re-evaluation", 2)

    assert data["empty"] is False
    assert data["steps"][-1]["remaining"] == 2
    assert run_warnings(tmp_path) == []
    assert append_step(tmp_path / "missing.eval", "x", 1) is None


def test_rescue_summary_is_written_and_updates_coverage(tmp_path, capsys):
    eval_path = tmp_path / "a.virusA.eval"
    _write_diag(eval_path, "evaluate", "virusA",
                [("Target sequences in reference", 3), ("Predicted to amplify", 0)], fatal=False)
    args = argparse.Namespace(ref=str(tmp_path / "virusA.fa"), report_dir=str(tmp_path))

    with patch.object(rescue_evaluate, "_covered_targets", return_value={"s1", "s2"}):
        rescue_evaluate._write_rescue_summary(eval_path, args, 3, 3, 2, True)

    assert "WARNING: Rescue re-evaluation was triggered" in capsys.readouterr().err
    (warning,) = run_warnings(tmp_path)  # the zero-coverage warning is resolved
    assert warning.kind == "rescue"
    assert "3/3 target sequence(s)" in warning.message
    assert "2 row(s) now pass" in warning.message


def test_rescue_run_removes_stale_summary_when_not_triggered(tmp_path):
    eval_path = tmp_path / "a.virusA.eval"
    stale = tmp_path / f"a.virusA.eval{RESCUE_SUFFIX}"
    stale.write_text("{}")
    args = argparse.Namespace(eval_path=str(eval_path), reftype="on",
                              report_dir=str(tmp_path))

    rescue_evaluate.run(args)  # no .full file -> rescue is skipped

    assert not stale.exists()


@patch("primercast.commands.export_report.compute_dimer_dg", return_value=0.0)
def test_export_report_without_alignment_reports_zero_coverage(mock_dg, tmp_path):
    ref = tmp_path / "virusA.fa"
    ref.write_text(">s1\nACGT\n>s2\nACGT\n")
    primers = tmp_path / "pset.fa"
    primers.write_text(">p1_for\nACGTACGTAC\n>p1_rev\nTTGGCCAATT\n")
    out = tmp_path / "report"
    args = argparse.Namespace(
        on=str(tmp_path / "_virusA" / "pset.virusA.eval"), off=[], off_ref=[], out=str(out),
        names=["p1"], probe_mapping_on=None, probe_mapping_off=[], probe_seqs=None,
        ref=str(ref), primer_fasta=str(primers), probe_max_mismatches=2,
        probe_max_indels=0, mapped_on=None, mapped_off=[],
    )

    export_report.run(args)

    detail = pd.read_excel(out / "p1.xlsx", sheet_name="detail")
    assert list(detail["seq_id"]) == ["s1", "s2"]
    assert set(detail["reason"]) == {"unmapped"} and set(detail["decision"]) == {0}
    summary = pd.read_excel(out / "p1.xlsx", sheet_name="summary", header=None)
    assert "0 / 2" in summary.astype(str).values
    assert "ACGTACGTAC" in summary.astype(str).values


def test_adapt_cli_prints_warnings(tmp_path, capsys):
    (tmp_path / f"a.virusA.eval{RESCUE_SUFFIX}").write_text(
        json.dumps({"message": "Rescue re-evaluation was triggered for virusA"}))
    assert adapt_cli._print_run_warnings(tmp_path) == 1
    err = capsys.readouterr().err
    assert "WARNINGS:" in err and "Rescue re-evaluation was triggered for virusA" in err


def test_adapt_cli_prints_funnel_summary_after_success(tmp_path, capsys):
    def fake_snakemake(cmd, cwd):
        _write_diag(tmp_path / "virusA_filt.fa", "filter", "virusA",
                    [("Scored primer pairs", 10), ("Coverage", 4)])
        return argparse.Namespace(returncode=0)

    with patch.object(adapt_cli.subprocess, "run", side_effect=fake_snakemake):
        assert adapt_cli._run_snakemake(tmp_path, [], cores=1, dry_run=False) == 0
    err = capsys.readouterr().err
    assert "Where filters narrowed the candidates:" in err
    assert "Primer pair filtering (virusA): Coverage 10 -> 4 (-60%)" in err


@pytest.mark.parametrize("warn", [True, False])
def test_adapt_cli_run_with_warnings_is_not_reported_as_clean(tmp_path, capsys, warn):
    def fake_snakemake(cmd, cwd):
        if warn:
            (tmp_path / f"a.virusA.eval{RESCUE_SUFFIX}").write_text(
                json.dumps({"message": "Rescue re-evaluation was triggered for virusA"}))
        return argparse.Namespace(returncode=0)

    with patch.object(adapt_cli.subprocess, "run", side_effect=fake_snakemake):
        rc = adapt_cli._run_snakemake(tmp_path, [], cores=1, dry_run=False)

    assert rc == 0
    err = capsys.readouterr().err
    assert ("Pipeline finished with 1 warning(s)" in err) is warn
