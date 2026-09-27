"""Regression cases built from synthetic sequences and injected tool failures."""

import json
from types import SimpleNamespace
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from qprimer_designer.commands import (
    build_output, evaluate, export_report, filter_primers, generate, prepare_input,
    quick_design,
)
from qprimer_designer.utils import write_diagnostics


@pytest.fixture
def design_case(tmp_path):
    params = tmp_path / "params.txt"
    params.write_text("NUM_TOP_SENSITIVITY=1\nDG_MIN=-6\nCOVERAGE_MIN=0.5\n")
    init = tmp_path / "init.fa"
    init.write_text(">f1\nACGTACGTACGTACGTACGT\n>r1\nTGCATGCATGCATGCATGCA\n"
                    ">f2\nAACCAACCAACCAACCAACC\n>r2\nTTGGTTGGTTGGTTGGTTGG\n")
    scores = tmp_path / "scores.eval"
    pd.DataFrame([
        ["f1", "r1", 1.0, 1.0, 1.0],
        ["f2", "r2", 0.9, 1.0, 0.9],
    ], columns=["pname_f", "pname_r", "coverage", "activity", "score"]).to_csv(scores, index=False)
    return SimpleNamespace(params=str(params), init=str(init), scores=str(scores),
                           out=str(tmp_path / "filt.fa"), probe_out=None,
                           probe_mapping=None, probe_seqs=None, probe_pair_csv=None)


def build_args(case):
    return SimpleNamespace(param_file=case.params, eval_on=case.scores, eval_off=[],
                           primers=case.out, output=str(Path(case.out).with_name("test_final.csv")),
                           name="synthetic", probe_mapping_on=None, probe_mapping_off=None,
                           probe_seqs=None, ref=None)


def add_probe(case):
    root = Path(case.out).parent
    case.probe_mapping = str(root / "probe.csv")
    case.probe_seqs = str(root / "probe.fa")
    case.probe_out = str(root / "filt_probes.csv")
    case.probe_pair_csv = str(root / "assignments.csv")
    Path(case.probe_seqs).write_text(">p1\nACACACACACACACACACACACAC\n")
    pd.DataFrame([["t1", "p1", 30, 0, 0]],
                 columns=["target_id", "probe_name", "start_pos", "mismatches", "indels"]).to_csv(case.probe_mapping, index=False)
    pd.DataFrame([["f1", "r1", "p1"], ["f2", "r2", "p1"]],
                 columns=["pname_f", "pname_r", "probe_name"]).to_csv(case.probe_pair_csv, index=False)
    Path(case.scores + ".quick").touch()


def test_filter_searches_beyond_rejected_top_pair(design_case, monkeypatch):
    monkeypatch.setattr(filter_primers, "compute_batch_dimer_dg",
                        lambda pairs: [-10.0 if f.startswith("ACGT") else -2.0 for f, r in pairs])
    filter_primers.run(design_case)
    result = pd.read_csv(Path(design_case.out).with_suffix(".csv"))
    assert result.pname_f.tolist() == ["f2"]


def test_final_output_keeps_valid_lower_ranked_pair(design_case):
    pd.read_csv(design_case.scores).iloc[1:].to_csv(Path(design_case.out).with_suffix(".csv"), index=False)
    Path(design_case.out).write_text(Path(design_case.init).read_text())
    args = build_args(design_case)
    build_output.run(args)
    assert pd.read_csv(args.output).pname_f.tolist() == ["f2"]


@pytest.mark.parametrize("content", ["", "pname_f,pname_r,coverage,activity,score\n"])
def test_empty_filter_produces_all_declared_outputs(design_case, content):
    add_probe(design_case)
    Path(design_case.scores).write_text(content)
    filter_primers.run(design_case)
    assert pd.read_csv(Path(design_case.out).with_suffix(".csv")).empty
    assert pd.read_csv(design_case.probe_out).empty
    assert Path(design_case.probe_out).with_suffix(".fa").read_text() == ""


@pytest.mark.parametrize("content", ["", "pname_f,pname_r,coverage,activity,score\n"])
def test_final_output_handles_no_evaluated_pairs(design_case, content):
    Path(design_case.scores).write_text(content)
    Path(design_case.out).write_text("")
    Path(design_case.out).with_suffix(".csv").write_text(content)
    args = build_args(design_case)
    build_output.run(args)
    assert pd.read_csv(args.output).empty


def test_tool_failure_is_not_a_biological_rejection(design_case, monkeypatch):
    def fail(pairs):
        raise RuntimeError("RNAduplex crashed")
    monkeypatch.setattr(filter_primers, "compute_batch_dimer_dg", fail)
    with pytest.raises(RuntimeError, match="RNAduplex"):
        filter_primers.run(design_case)


def test_probe_mode_also_checks_primer_primer_dimer(design_case, monkeypatch):
    add_probe(design_case)
    monkeypatch.setattr(filter_primers, "compute_batch_dimer_dg",
                        lambda pairs: [-10.0 if f.startswith("ACGT") else -2.0 for f, r in pairs])
    filter_primers.run(design_case)
    assert pd.read_csv(Path(design_case.out).with_suffix(".csv")).pname_f.tolist() == ["f2"]


def test_empty_probe_assignments_do_not_authorize_all_probes(design_case, monkeypatch):
    add_probe(design_case)
    Path(design_case.probe_pair_csv).write_text("pname_f,pname_r,probe_name\n")
    monkeypatch.setattr(filter_primers, "compute_batch_dimer_dg", lambda pairs: [-2.0] * len(pairs))
    filter_primers.run(design_case)
    assert pd.read_csv(Path(design_case.out).with_suffix(".csv")).empty


@pytest.mark.parametrize("length", [60, 61, 79, 80])
def test_short_but_feasible_templates_are_tiled(length):
    import random
    rng = random.Random(101)
    template = "".join(rng.choices("ACGT", k=length))
    forwards, reverses = generate.generate_primers_single(template, 1, 20, 20, 60)
    assert template[:20] in forwards
    assert generate.reverse_complement_dna(template[-20:]) in reverses
    assert generate.count_primer_pairs(forwards, reverses, 60, 200) > 0


@pytest.mark.parametrize("coverage,dg", [(0.0, -2.0), (1.0, -20.0)])
def test_quick_output_all_rejected_clears_stale_outputs(tmp_path, monkeypatch, coverage, dg):
    args = SimpleNamespace(out=str(tmp_path / "scores.eval"), init_fa=str(tmp_path / "init.fa"),
                           init_feat=str(tmp_path / "init.feat"), probe_fa=str(tmp_path / "probe.fa"),
                           probe_feat=str(tmp_path / "probe.feat"), probe_pair_csv=str(tmp_path / "assign.csv"))
    for path in vars(args).values():
        Path(path).write_text("stale result\n")
    rows = pd.DataFrame([["f1", "r1", coverage, 1.0, coverage]],
                        columns=["pname_f", "pname_r", "coverage", "activity", "score"])
    monkeypatch.setattr(quick_design, "compute_batch_dimer_dg", lambda pairs: [dg] * len(pairs))
    quick_design._write_output(args, [rows], [], {"f1": "ACGT", "r1": "TGCA"},
                              1.0, 1.0, 1, tmp_path, min_dg=-6)
    assert pd.read_csv(args.out).empty
    assert Path(args.init_fa).read_text() == ""
    assert pd.read_csv(args.init_feat).empty
    assert pd.read_csv(args.probe_pair_csv).empty


def test_probe_can_be_shared_by_alternative_pairs(design_case):
    add_probe(design_case)
    Path(design_case.params).write_text("NUM_TOP_SENSITIVITY=2\n")
    filt = pd.read_csv(design_case.scores)
    filt["probe_names"] = "p1"
    filt.to_csv(Path(design_case.out).with_suffix(".csv"), index=False)
    Path(design_case.out).write_text(Path(design_case.init).read_text())
    args = build_args(design_case)
    args.probe_mapping_on = design_case.probe_mapping
    args.probe_seqs = design_case.probe_seqs
    build_output.run(args)
    assert pd.read_csv(args.output).valid_probes.tolist() == ["p1", "p1"]


def test_generation_features_match_emitted_ids_with_shared_sequences(tmp_path, monkeypatch):
    from Bio import SeqIO
    sequence = "ACGT" * 5
    other = "AGCT" * 5
    features = {seq: dict(len=20, Tm=60, GC=0.5, dG=-2) for seq in (sequence, other)}
    monkeypatch.setattr(generate, "generate_primers_multi",
                        lambda *a: ({sequence: 0, other: 1}, {sequence: 100, other: 101}, features))
    inp = tmp_path / "in.fa"
    inp.write_text(">synthetic\n" + "ACGT" * 30 + "\n")
    params = tmp_path / "params.txt"
    params.write_text("MAX_PRIMER_CANDIDATES=2\n")
    out = tmp_path / "init.fa"
    generate.run(SimpleNamespace(name="synthetic", param_file=str(params), target_seqs=str(inp),
                                 primer_seqs=str(out), seed=42))
    records = list(SeqIO.parse(out, "fasta"))
    table = pd.read_csv(out.with_suffix(".feat"))
    assert len(table) == len(records) == 2
    assert set(table.pname) == {rec.id for rec in records}
    assert table.pname.notna().all()


@pytest.mark.parametrize("probe", [False, True])
def test_dimer_failure_records_error_and_clears_outputs(design_case, monkeypatch, probe):
    import json
    if probe:
        add_probe(design_case)
    Path(design_case.out).write_text(">stale\nACGT\n")
    def fail(pairs):
        raise RuntimeError("RNAduplex crashed")
    monkeypatch.setattr(filter_primers, "compute_batch_dimer_dg", fail)
    with pytest.raises(RuntimeError):
        filter_primers.run(design_case)
    assert Path(design_case.out).read_text() == ""
    diagnostic = json.loads(Path(design_case.out).with_suffix(".diagnostics.json").read_text())
    assert diagnostic["status"] == "error"


def test_probe_primer_failure_is_not_silently_accepted(design_case, monkeypatch):
    add_probe(design_case)
    def fail_probe_only(pairs):
        if pairs[0][0].startswith("ACAC"):
            raise RuntimeError("RNAduplex probe check failed")
        return [-2.0] * len(pairs)
    monkeypatch.setattr(filter_primers, "compute_batch_dimer_dg", fail_probe_only)
    with pytest.raises(RuntimeError, match="probe check"):
        filter_primers.run(design_case)


def test_dimer_boundary_is_inclusive(design_case, monkeypatch):
    monkeypatch.setattr(filter_primers, "compute_batch_dimer_dg", lambda pairs: [-6.0] * len(pairs))
    filter_primers.run(design_case)
    assert len(pd.read_csv(Path(design_case.out).with_suffix(".csv"))) == 1


def test_probe_positions_use_actual_reverse_primer():
    pairs = pd.DataFrame({"pname_f": ["f1", "f1"], "pname_r": ["r1", "r2"]})
    positions = {"f1": (0, 20), "r1": (40, 20), "r2": (80, 20)}
    probes = [dict(msa_start=25, msa_end=49), dict(msa_start=90, msa_end=114)]
    result = quick_design._probe_indices_by_pair(pairs, probes, positions)
    assert result == {("f1", "r2"): {0}}


def test_probe_mode_scores_only_positions_with_space_for_probe():
    import random
    rng = random.Random(17)
    sequence = "".join(rng.choices("ACGT", k=100))
    scored, _, _ = quick_design.score_amplicon_positions(
        [sequence, sequence], 20, 22, 60, 90, min_gc=0, max_gc=1,
        min_primer_score=0, top_n=50, min_insert_len=24,
    )
    assert scored
    assert all(rev_start - (fwd_start + fwd_len) >= 24
               for fwd_start, fwd_len, rev_start, rev_len, score in scored)


def test_no_probe_filters_still_write_declared_quick_outputs(tmp_path):
    args = SimpleNamespace(out=str(tmp_path / "eval"), init_fa=str(tmp_path / "init.fa"),
                           probe_csv=str(tmp_path / "probe.csv"), probe_pair_csv=str(tmp_path / "assign.csv"))
    quick_design._write_empty_output(args, "No probes passed filters.")
    assert pd.read_csv(args.probe_csv).empty
    assert pd.read_csv(args.probe_pair_csv).empty


def prepare_input_case(tmp_path, mapped_text):
    """A prepare-input invocation over a two-primer set against one reference."""
    mapped = tmp_path / "pset.H1.mapped"
    mapped.write_text(mapped_text)
    ref = tmp_path / "H1.fa"
    ref.write_text(">seq1\n" + "ACGT" * 60 + "\n")
    features = tmp_path / "pset.feat"
    pd.DataFrame([["pset_for", "ACGTACGTACGTACGTACGT", "f", 20, 57.3, 0.5, -4.4],
                  ["pset_rev", "TGCATGCATGCATGCATGCA", "r", 20, 57.3, 0.5, -6.4]],
                 columns=["pname", "pseq", "forrev", "len", "Tm", "GC", "dG"]).to_csv(features, index=False)
    params = tmp_path / "params.txt"
    params.write_text("AMPLEN_MIN=60\nAMPLEN_MAX=200\nOFFLEN_MIN=50\nOFFLEN_MAX=5000\n")
    return SimpleNamespace(mapped=str(mapped), ml_input=str(tmp_path / "pset.H1.input"),
                           reference=str(ref), param_file=str(params), reftype="on",
                           pri_features=str(features), prev="")


def test_no_alignments_records_the_cause_instead_of_exiting_silently(tmp_path):
    args = prepare_input_case(tmp_path, "")
    with pytest.raises(SystemExit):
        prepare_input.run(args)

    assert Path(args.ml_input).exists() and Path(args.ml_input).stat().st_size == 0
    data = json.loads(Path(f"{args.ml_input}.diagnostics.json").read_text())
    assert data["stage"] == "prepare_input"
    assert data["status"] == "no_alignments"
    assert "0 of 2 primer(s) aligned to H1" in data["reason"]
    assert data["counts"] == {"primers": 2, "alignments": 0, "pairs": 0}


def test_alignments_without_a_valid_amplicon_are_distinguished(tmp_path):
    # Both primers align, but on the same strand at the same spot, so no
    # amplicon of any length can form.
    row = "pset_for\t0\tseq1\t1\tACGTACGTACGTACGTACGT\tACGTACGTACGTACGTACGT\t||||||||||||||||||||"
    args = prepare_input_case(tmp_path, row + "\n")
    prepare_input.run(args)

    data = json.loads(Path(f"{args.ml_input}.diagnostics.json").read_text())
    assert data["status"] == "no_amplicons"
    assert "AMPLEN_MIN=60-AMPLEN_MAX=200" in data["reason"]
    assert data["counts"]["alignments"] == 1


def test_empty_model_input_records_that_no_scores_were_produced(tmp_path, monkeypatch):
    inp = tmp_path / "pset.H1.input"
    inp.write_text("")
    ref = tmp_path / "H1.fa"
    ref.write_text(">seq1\nACGT\n")
    out = tmp_path / "pset.H1.eval"
    # Model loading is irrelevant to the empty-input path but happens before it.
    monkeypatch.setattr(evaluate, "load_models", lambda: (None, None, None, "cpu"))

    with pytest.raises(SystemExit):
        evaluate.run(SimpleNamespace(input=str(inp), output=str(out),
                                     reference=str(ref), reftype="on", threads=1))

    data = json.loads(Path(f"{out}.diagnostics.json").read_text())
    assert data["stage"] == "evaluate"
    assert data["status"] == "no_input"
    assert "H1" in data["reason"]
    assert not Path(f"{out}.full").exists()


def test_export_report_carries_upstream_reasons_into_the_report(tmp_path):
    """The report explains the root cause, not just the missing '.full' file."""
    eval_dir = tmp_path / "_H1"
    eval_dir.mkdir()
    eval_on = eval_dir / "pset.H1.eval"
    eval_on.write_text("")
    write_diagnostics(str(eval_on), stage="evaluate", status="no_input",
                      reason="No candidate amplicons to score against H1.")
    write_diagnostics(str(eval_dir / "pset.H1.input"), stage="prepare_input",
                      status="no_alignments",
                      reason="0 of 2 primer(s) aligned to H1.")
    fasta = tmp_path / "pset.fa"
    fasta.write_text(">pset_for\nACGTACGTACGTACGTACGT\n>pset_rev\nTGCATGCATGCATGCATGCA\n")
    outdir = tmp_path / "report"

    export_report.run(SimpleNamespace(
        on=str(eval_on), off=[], off_ref=[], out=str(outdir), names=["pset"],
        probe_mapping_on=None, probe_mapping_off=[], probe_seqs=None, ref=None,
        probe_max_mismatches=2, probe_max_indels=0, mapped_on=None, mapped_off=[],
        pset_fasta=str(fasta)))

    report = outdir / "pset.xlsx"
    assert report.exists(), "A fully failed evaluation must still produce a report"

    workbook = load_workbook(report)
    text = [row[0] for row in workbook["summary"].iter_rows(values_only=True) if row[0]]
    workbook.close()
    # Warnings read cause-first: alignment, then scoring, then this command's
    # own downstream symptoms.
    start = text.index("Warnings")
    assert text[start + 1:start + 3] == [
        "0 of 2 primer(s) aligned to H1.",
        "No candidate amplicons to score against H1.",
    ]
    assert text[start + 3].startswith("on-target eval failed for pset")
    assert text[start + 4] == "No evaluation results for primer set pset."
