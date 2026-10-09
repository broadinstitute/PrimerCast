"""Tests for quick_design amplicon position scoring."""

import random

from primercast.commands.quick_design import score_amplicon_positions


def _msa(n_seqs=10, length=800, conserved=(0, 250), seed=0):
    """MSA whose `conserved` block is identical; elsewhere one sequence varies every 5th column."""
    rng = random.Random(seed)
    base = [rng.choice("ACGT") for _ in range(length)]
    seqs = [list(base) for _ in range(n_seqs)]
    for col in range(length):
        if not conserved[0] <= col < conserved[1] and col % 5 == 0:
            seqs[0][col] = rng.choice([b for b in "ACGT" if b != base[col]])
    return ["".join(s) for s in seqs]


def _score(msa, **kwargs):
    defaults = dict(primer_len_min=19, primer_len_max=21, min_amp_len=60, max_amp_len=200,
                    min_gc=0.0, max_gc=1.0, top_n=20)
    defaults.update(kwargs)
    positions, _, _ = score_amplicon_positions(msa, **defaults)
    return positions


def test_positions_have_distinct_spaced_fwd_starts():
    positions = _score(_msa(), min_gap=10)
    starts = [p[0] for p in positions]
    assert len(positions) == 20
    assert len(set(starts)) == len(starts)
    assert all(abs(a - b) >= 10 for i, a in enumerate(starts) for b in starts[i + 1:])


def test_positions_spread_beyond_most_conserved_region():
    """The top positions must not all sit in the best-conserved block (H1 regression)."""
    positions = _score(_msa(conserved=(0, 250)), top_n=40)
    assert max(p[0] for p in positions) >= 250


def test_positions_respect_amplicon_range_and_sort_order():
    positions = _score(_msa())
    for fwd_start, fwd_len, rev_start, rev_len, _ in positions:
        assert 19 <= fwd_len <= 21 and 19 <= rev_len <= 21
        assert 60 <= rev_start + rev_len - fwd_start <= 200
    scores = [p[4] for p in positions]
    assert scores == sorted(scores, reverse=True)


def test_top_n_zero_is_unlimited():
    assert len(_score(_msa(), top_n=0)) > len(_score(_msa(), top_n=20))


def test_min_interior_leaves_room_between_primers():
    positions = _score(_msa(), min_interior=31)
    assert positions
    for fwd_start, fwd_len, rev_start, _, _ in positions:
        assert rev_start - (fwd_start + fwd_len) >= 31
