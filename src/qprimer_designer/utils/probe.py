"""Probe matching utilities.

Probe-specific functions (display, sliding window match).
Wobble definitions and mismatch counting live in wobble.py.
"""

from .sequences import reverse_complement_dna
from .wobble import (
    WOBBLE_PAIRS,
    WOBBLE_WEIGHT,
    WOBBLE_W,
    WOBBLE_W_PRIMER,
    wobble_mismatch_count,
    wobble_mismatch_count_cols,
    wobble_mismatch_count_gapped,
)

# Watson-Crick pairs for hybridization display
_WC_PAIRS = {('A', 'T'), ('T', 'A'), ('G', 'C'), ('C', 'G')}
# Wobble pairs for hybridization display
_WOBBLE_PAIRS = {(p, t) for p, t in WOBBLE_PAIRS}


def build_match_string(probe_seq, target_complement):
    """Build a pairwise match string for probe-target hybridization display.

    Compares probe sequence against the complement of the target (i.e., the
    strand the probe actually hybridizes with).

    For each position:
      '|' = Watson-Crick pair (A-T, T-A, G-C, C-G)
      '.' = wobble pair (G-T, T-G)
      ' ' = mismatch

    Args:
        probe_seq: Probe sequence (uppercase)
        target_complement: Complement of target sequence (uppercase, same length)

    Returns:
        Match string of same length as inputs.
    """
    chars = []
    for pb, tb in zip(probe_seq, target_complement):
        if (pb, tb) in _WC_PAIRS:
            chars.append('|')
        elif (pb, tb) in _WOBBLE_PAIRS:
            chars.append('.')
        else:
            chars.append(' ')
    return ''.join(chars)


def slide_probe_match(probe_seq, target_seq, max_mismatches, max_indels=0):
    """Slide a probe across a target sequence, checking both orientations.

    Uses wobble-aware mismatch counting at each position. Returns all
    positions where effective_mismatches <= max_mismatches.

    Args:
        probe_seq: Probe sequence (ungapped, uppercase)
        target_seq: Target sequence (ungapped, uppercase)
        max_mismatches: Maximum wobble-weighted mismatches allowed
        max_indels: Maximum indels allowed (always 0 for ungapped comparison)

    Returns:
        list of dicts: {start_pos, orientation, mismatches, indels}
    """
    probe_len = len(probe_seq)
    target_len = len(target_seq)
    if probe_len > target_len:
        return []

    target_upper = target_seq.upper()
    hits = []

    for orientation, seq in [('+', probe_seq.upper()),
                              ('-', reverse_complement_dna(probe_seq).upper())]:
        for i in range(target_len - probe_len + 1):
            window = target_upper[i:i + probe_len]
            mm, indels = wobble_mismatch_count(seq, window)
            if mm <= max_mismatches and indels <= max_indels:
                hits.append({
                    'start_pos': i,
                    'orientation': orientation,
                    'mismatches': round(mm, 2),
                    'indels': indels,
                })

    return hits
