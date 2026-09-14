"""Tests for wobble-aware probe matching utilities."""

import pytest
from qprimer_designer.utils.wobble import (
    WOBBLE_W_SS,
    WOBBLE_W_PRIMER,
    WOBBLE_WEIGHT,
    wobble_mismatch_count,
    wobble_mismatch_count_gapped,
    wobble_mismatch_count_cols,
)
from qprimer_designer.utils.probe import build_match_string, slide_probe_match


class TestWobbleWeights:
    """Verify wobble weight dictionaries."""

    def test_ss_wobble_values(self):
        """WOBBLE_W_SS entries should all equal WOBBLE_WEIGHT."""
        for pair, val in WOBBLE_W_SS.items():
            assert val == WOBBLE_WEIGHT

    def test_primer_wobble_has_wc_pairs(self):
        """WOBBLE_W_PRIMER should have WC pairs (used for hybridization scoring)."""
        assert ('A', 'T') in WOBBLE_W_PRIMER
        assert ('G', 'C') in WOBBLE_W_PRIMER

    def test_ss_wobble_no_wc_pairs(self):
        """WOBBLE_W_SS must NOT have WC complement pairs (same-strand comparison)."""
        assert ('A', 'T') not in WOBBLE_W_SS
        assert ('G', 'C') not in WOBBLE_W_SS
        assert ('C', 'G') not in WOBBLE_W_SS
        assert ('T', 'A') not in WOBBLE_W_SS


class TestWobbleMismatchCount:
    """Tests for ungapped wobble mismatch counting."""

    def test_identical_sequences(self):
        mm, indels = wobble_mismatch_count("ATCGATCG", "ATCGATCG")
        assert mm == 0.0
        assert indels == 0

    def test_full_mismatch(self):
        """Non-wobble substitution = 1.0 penalty each."""
        # A vs C = full mismatch
        mm, indels = wobble_mismatch_count("AAAA", "CCCC")
        assert mm == 4.0
        assert indels == 0

    def test_wobble_ga(self):
        """G->A wobble on same strand = 0.20 penalty (1.0 - 0.80)."""
        mm, indels = wobble_mismatch_count("GATG", "AATG")
        assert mm == pytest.approx(1.0 - WOBBLE_WEIGHT)
        assert indels == 0

    def test_wobble_tc(self):
        """T->C wobble on same strand = 0.20 penalty."""
        mm, indels = wobble_mismatch_count("TATG", "CATG")
        assert mm == pytest.approx(1.0 - WOBBLE_WEIGHT)
        assert indels == 0

    def test_mixed_mismatches(self):
        """Mix of wobble and full mismatches."""
        # pos0: G vs A = wobble penalty
        # pos1: A vs A = 0.0 match
        # pos2: T vs C = wobble penalty
        # pos3: A vs C = 1.0 full mismatch
        expected = 2 * (1.0 - WOBBLE_WEIGHT) + 1.0
        mm, indels = wobble_mismatch_count("GATA", "AACC")
        assert mm == pytest.approx(expected)
        assert indels == 0

    def test_wc_complement_is_full_mismatch(self):
        """G vs C on same strand is NOT a wobble — full mismatch."""
        mm, indels = wobble_mismatch_count("G", "C")
        assert mm == 1.0

    def test_ag_not_wobble(self):
        """A vs G on same strand is NOT a probe wobble — full mismatch."""
        mm, indels = wobble_mismatch_count("A", "G")
        assert mm == 1.0

    def test_single_wobble_penalty_value(self):
        """Verify exact penalty: 1.0 - WOBBLE_WEIGHT."""
        mm, _ = wobble_mismatch_count("G", "A")
        assert mm == pytest.approx(1.0 - WOBBLE_WEIGHT)


class TestWobbleMismatchCountGapped:
    """Tests for MSA-based (gapped) wobble mismatch counting."""

    def test_no_gaps(self):
        probe = "ATCG"
        target_row = "XXATCGXX"
        mm, indels = wobble_mismatch_count_gapped(probe, target_row, 2, 6)
        assert mm == 0.0
        assert indels == 0

    def test_gap_counts_as_indel(self):
        probe = "ATCG"
        target_row = "AT-G"
        mm, indels = wobble_mismatch_count_gapped(probe, target_row, 0, 4)
        assert indels == 1

    def test_n_counts_as_indel(self):
        probe = "ATCG"
        target_row = "ATNG"
        mm, indels = wobble_mismatch_count_gapped(probe, target_row, 0, 4)
        assert indels == 1

    def test_lowercase_target(self):
        """Target can be lowercase; should still match."""
        probe = "ATCG"
        target_row = "atcg"
        mm, indels = wobble_mismatch_count_gapped(probe, target_row, 0, 4)
        assert mm == 0.0
        assert indels == 0

    def test_wobble_in_gapped(self):
        probe = "GATG"
        target_row = "AATG"
        mm, indels = wobble_mismatch_count_gapped(probe, target_row, 0, 4)
        assert mm == pytest.approx(1.0 - WOBBLE_WEIGHT)
        assert indels == 0

    def test_mixed_gaps_and_mismatches(self):
        probe = "GATC"
        target_row = "A-TC"  # pos0: G vs A = wobble, pos1: gap = indel, pos2: T=T, pos3: C=C
        mm, indels = wobble_mismatch_count_gapped(probe, target_row, 0, 4)
        assert mm == pytest.approx(1.0 - WOBBLE_WEIGHT)
        assert indels == 1


class TestWobbleMismatchCountCols:
    """Tests for column-indexed wobble mismatch counting."""

    def test_contiguous_cols(self):
        probe = "ATCG"
        target_row = "ATCGATCG"
        cols = [0, 1, 2, 3]
        mm, indels = wobble_mismatch_count_cols(probe, target_row, cols)
        assert mm == 0.0
        assert indels == 0

    def test_noncontiguous_cols(self):
        probe = "AG"
        target_row = "A-C-G"
        cols = [0, 4]  # skip gap columns
        mm, indels = wobble_mismatch_count_cols(probe, target_row, cols)
        assert mm == 0.0
        assert indels == 0

    def test_gap_at_col(self):
        probe = "AG"
        target_row = "A-G"
        cols = [0, 1]  # col 1 is a gap
        mm, indels = wobble_mismatch_count_cols(probe, target_row, cols)
        assert indels == 1


class TestBuildMatchString:
    """Tests for hybridization match string display."""

    def test_perfect_wc_match(self):
        """All Watson-Crick pairs → all '|'."""
        # probe A vs complement T = A:T WC pair
        result = build_match_string("ATCG", "TAGC")
        assert result == "||||"

    def test_wobble_gt(self):
        """G:T wobble pair → '.'."""
        result = build_match_string("G", "T")
        assert result == "."

    def test_wobble_tg(self):
        """T:G wobble pair → '.'."""
        result = build_match_string("T", "G")
        assert result == "."

    def test_mismatch(self):
        """Non-WC, non-wobble → ' '."""
        result = build_match_string("A", "A")
        assert result == " "

    def test_mixed(self):
        # A:T = WC '|', G:T = wobble '.', C:C = mismatch ' ', T:A = WC '|'
        result = build_match_string("AGCT", "TTCA")
        assert result == "|. |"

    def test_gc_not_wobble_in_display(self):
        """G:C is WC, not wobble. G:G is mismatch."""
        result = build_match_string("GG", "CG")
        assert result == "| "


class TestSlideProbeMatch:
    """Tests for sliding probe across target sequence."""

    def test_exact_match(self):
        probe = "ATCG"
        target = "XXATCGXX"
        hits = slide_probe_match(probe, target, max_mismatches=0)
        assert len(hits) >= 1
        fwd_hits = [h for h in hits if h['orientation'] == '+']
        assert any(h['start_pos'] == 2 and h['mismatches'] == 0.0 for h in fwd_hits)

    def test_no_match(self):
        probe = "AAAA"
        target = "CCCCCCCC"
        hits = slide_probe_match(probe, target, max_mismatches=0)
        assert len(hits) == 0

    def test_reverse_complement_match(self):
        """Probe reverse complement should match."""
        probe = "ATCG"  # RC = CGAT
        target = "XXCGATXX"
        hits = slide_probe_match(probe, target, max_mismatches=0)
        rc_hits = [h for h in hits if h['orientation'] == '-']
        assert len(rc_hits) >= 1
        assert any(h['start_pos'] == 2 for h in rc_hits)

    def test_wobble_within_threshold(self):
        """Wobble mismatch should be found with max_mismatches >= penalty."""
        probe = "GATG"
        target = "AATG"  # G->A = wobble
        penalty = 1.0 - WOBBLE_WEIGHT
        hits = slide_probe_match(probe, target, max_mismatches=penalty)
        fwd_hits = [h for h in hits if h['orientation'] == '+']
        assert len(fwd_hits) == 1
        assert fwd_hits[0]['mismatches'] == pytest.approx(penalty)

    def test_wobble_below_threshold(self):
        """Wobble mismatch should NOT be found with max_mismatches < penalty."""
        probe = "GATG"
        target = "AATG"
        penalty = 1.0 - WOBBLE_WEIGHT
        hits = slide_probe_match(probe, target, max_mismatches=penalty - 0.01)
        fwd_hits = [h for h in hits if h['orientation'] == '+']
        assert len(fwd_hits) == 0

    def test_probe_longer_than_target(self):
        hits = slide_probe_match("ATCGATCG", "ATCG", max_mismatches=0)
        assert hits == []

    def test_multiple_hits(self):
        probe = "AA"
        target = "AAAAA"
        hits = slide_probe_match(probe, target, max_mismatches=0)
        fwd_hits = [h for h in hits if h['orientation'] == '+']
        # Should find AA at positions 0,1,2,3
        assert len(fwd_hits) == 4

    def test_case_insensitive(self):
        hits = slide_probe_match("atcg", "XXATCGXX", max_mismatches=0)
        fwd_hits = [h for h in hits if h['orientation'] == '+']
        assert len(fwd_hits) >= 1
