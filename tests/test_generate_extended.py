"""Extended tests for generate command logic."""

from random import Random
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest
from Bio.SeqUtils import gc_fraction

from primercast.commands import generate, prepare_features
from primercast.commands.generate import (
    generate_primers_single,
    generate_primers_multi,
    count_primer_pairs,
)


class TestGeneratePrimersMulti:
    """Tests for generate_primers_multi (with mocked dG)."""

    @patch("primercast.commands.generate.compute_batch_dimer_dg", side_effect=lambda pairs: [0.0] * len(pairs))
    def test_basic_multi(self, mock_dg):
        """Test multi-target primer generation."""
        targets = ["ATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCG"]
        for_filt, rev_filt, features = generate_primers_multi(
            targets, step=1, min_pri_len=20, max_pri_len=20,
            min_amp_len=30, max_amp_len=200,
            max_tm=100, min_tm=0, max_gc=100, min_dg=-100,
        )
        assert len(for_filt) > 0
        assert len(rev_filt) > 0
        # Each filtered primer should have features
        for seq in for_filt:
            assert "Tm" in features[seq]
            assert "GC" in features[seq]
            assert "len" in features[seq]
            assert "dG" in features[seq]

    @patch("primercast.commands.generate.compute_batch_dimer_dg", side_effect=lambda pairs: [-100.0] * len(pairs))
    def test_dg_filter_removes_all(self, mock_dg):
        """All primers filtered when dG is below threshold."""
        targets = ["ATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCG"]
        for_filt, rev_filt, features = generate_primers_multi(
            targets, step=1, min_pri_len=20, max_pri_len=20,
            min_amp_len=30, max_amp_len=200,
            max_tm=100, min_tm=0, max_gc=100, min_dg=-6,
        )
        assert len(for_filt) == 0
        assert len(rev_filt) == 0

    @patch("primercast.commands.generate.compute_batch_dimer_dg", side_effect=lambda pairs: [0.0] * len(pairs))
    def test_tm_filter(self, mock_dg):
        """Very narrow Tm range should filter out most primers."""
        targets = ["ATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCG"]
        for_filt, rev_filt, features = generate_primers_multi(
            targets, step=1, min_pri_len=20, max_pri_len=20,
            min_amp_len=30, max_amp_len=200,
            max_tm=0, min_tm=0,  # Impossible range
            max_gc=100, min_dg=-100,
        )
        # With Tm range [0, 0], almost no 20-mers will pass
        assert len(for_filt) == 0

    @patch("primercast.commands.generate.compute_batch_dimer_dg", side_effect=lambda pairs: [0.0] * len(pairs))
    def test_multiple_targets(self, mock_dg):
        """Test with multiple target sequences."""
        targets = [
            "ATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCGATCG",
            "GCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTAGCTA",
        ]
        for_filt, rev_filt, features = generate_primers_multi(
            targets, step=1, min_pri_len=20, max_pri_len=20,
            min_amp_len=30, max_amp_len=200,
            max_tm=100, min_tm=0, max_gc=100, min_dg=-100,
        )
        assert len(for_filt) > 0


@pytest.mark.parametrize("primer_length", [19, 20, 21])
def test_generated_features_match_evaluate_preprocessing(tmp_path, monkeypatch, primer_length):
    """Re-evaluating emitted primers must preserve every stored feature."""
    for command in (generate, prepare_features):
        monkeypatch.setattr(command, "compute_batch_dimer_dg",
                            lambda pairs: [-2.345] * len(pairs))

    target = tmp_path / "target.fa"
    target.write_text(">synthetic\n" + "".join(Random(42).choices("ACGT", k=120)) + "\n")
    params = tmp_path / "params.txt"
    params.write_text(
        f"PRIMER_LEN_MIN={primer_length}\nPRIMER_LEN_MAX={primer_length}\n"
        "TM_MIN=0\nTM_MAX=100\nGC_MAX=100\nDG_MIN=-10\n"
        "AMPLEN_MIN=60\nAMPLEN_MAX=200\nMAX_PRIMER_CANDIDATES=1000\n"
    )
    primers = tmp_path / "generated.fa"
    generate.run(SimpleNamespace(
        name="synthetic", param_file=str(params), target_seqs=str(target),
        primer_seqs=str(primers), seed=42,
    ))
    evaluated_features = tmp_path / "evaluated.feat"
    prepare_features.run(SimpleNamespace(fa=str(primers), out=str(evaluated_features)))

    generated = pd.read_csv(primers.with_suffix(".feat"))
    evaluated = pd.read_csv(evaluated_features)
    assert not generated.empty
    pd.testing.assert_frame_equal(generated, evaluated, check_exact=True)
    assert generated["GC"].tolist() == pytest.approx(
        [gc_fraction(seq) for seq in generated["pseq"]], abs=1e-15, rel=0,
    )


class TestCountPrimerPairsExtended:
    """Extended tests for count_primer_pairs."""

    def test_multiple_valid_pairs(self):
        fors = {"A": 0, "B": 10, "C": 20}
        revs = {"X": 100, "Y": 150}
        count = count_primer_pairs(fors, revs, min_amp_len=60, max_amp_len=200)
        assert count > 0

    def test_max_amp_len_constraint(self):
        """Amplicons exceeding max_amp_len should not be counted."""
        fors = {"A": 0}
        revs = {"B": 500}
        count = count_primer_pairs(fors, revs, min_amp_len=60, max_amp_len=200)
        assert count == 0
