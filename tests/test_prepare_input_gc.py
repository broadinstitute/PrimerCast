"""Amplicon GC/Tm in prepare-input.

GC used to come from per-target prefix-sum arrays at 8 bytes per base, which ran
a 24k-genome SARS-CoV-2 monitor out of memory. It is now counted on the
amplicon; these pin the numbers it must keep producing.
"""

import argparse

import pandas as pd
import pytest

from primercast.commands import prepare_input as pi


def _run(tmp_path, seq, f_start, r_start, r_len=20):
    (tmp_path / "ref.fa").write_text(f">T0\n{seq}\n")
    (tmp_path / "feat.csv").write_text(",forrev,len,Tm,GC\nF,f,20,58,50\nR,r,20,58,50\n")
    (tmp_path / "params.txt").write_text("AMPLEN_MIN = 20\nAMPLEN_MAX = 400\n")
    p, m = "A" * 20, "|" * 20
    r_p = "A" * r_len
    (tmp_path / "mapped.tsv").write_text(
        f"F\t0\tT0\t{f_start}\t{p}\t{p}\t{m}\n"
        f"R\t16\tT0\t{r_start}\t{r_p}\t{r_p}\t{'|' * r_len}\n")
    out = tmp_path / "out.csv"
    pi.run(argparse.Namespace(
        mapped=str(tmp_path / "mapped.tsv"), ml_input=str(out),
        reference=str(tmp_path / "ref.fa"), param_file=str(tmp_path / "params.txt"),
        reftype="on", pri_features=str(tmp_path / "feat.csv"), prev=""))
    return pd.read_csv(out).iloc[0]


def test_tm_uses_the_amplicons_gc_case_insensitively(tmp_path):
    # 10 bases before, then a 70 bp amplicon: 30 G/C (half lowercase), 40 A/T.
    amplicon = "GGGGGcccccGGGGGcccccGGGGGccccc" + "aaaaaTTTTT" * 4
    seq = "TTTTTTTTTT" + amplicon + "TTTTTTTTTT"
    row = _run(tmp_path, seq, f_start=11, r_start=61)

    assert row["prod_len"] == 70
    assert row["prod_Tm"] == round(pi._gc_tm(30, 70), 1)


def test_amplicon_running_off_the_end_counts_only_real_bases(tmp_path):
    """The reverse alignment ends past the sequence; GC stops at the last base."""
    seq = "A" * 40 + "G" * 20          # 60 bp
    row = _run(tmp_path, seq, f_start=1, r_start=50, r_len=20)   # nominal end 69

    assert row["prod_len"] == 69
    assert row["prod_Tm"] == round(pi._gc_tm(20, 69), 1)


def test_no_per_base_arrays_are_built():
    """Guard against bringing back the per-target prefix sums."""
    import inspect
    assert "cumsum" not in inspect.getsource(pi.run)
