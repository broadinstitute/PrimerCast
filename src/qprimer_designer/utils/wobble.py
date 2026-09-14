"""Wobble base-pair definitions and mismatch counting.

Wobble pairs are defined once in hybridization coordinates (base1, base2 on
opposing strands).  All scoring dicts are derived from this single source.

Used by both primer design (quick_design.py) and probe matching (probe.py).
"""

_COMPLEMENT = {'A': 'T', 'T': 'A', 'G': 'C', 'C': 'G'}

# ── Canonical wobble pair definitions (hybridization coordinates) ──
# Each entry is (base_on_primer/probe, base_on_template) on opposing strands.
WOBBLE_PAIRS = {('G', 'T'), ('T', 'G'), ('T', 'T'), ('C', 'T'), ('T', 'C')}

# Scoring weights
#   WC = 1.0, wobble = 0.5, other mismatch = 0.0, gap = -0.5
WOBBLE_WEIGHT = 0.80
GAP_WEIGHT = -0.50

# ── Derived scoring dicts ──────────────────────────────────────────

# Base-selection weights (hybridization coordinates).
# Used by _best_primer_base in quick_design.py.
_WC_PAIRS = {('A', 'T'), ('T', 'A'), ('G', 'C'), ('C', 'G')}
WOBBLE_W_PRIMER = {pair: 1.0 for pair in _WC_PAIRS}
WOBBLE_W_PRIMER.update({pair: WOBBLE_WEIGHT for pair in WOBBLE_PAIRS})

# Same-strand mismatch weights.
# Converts hybridization (primer, template) → same-strand (primer, complement(template)).
WOBBLE_W_SS = {
    (p, _COMPLEMENT[t]): WOBBLE_WEIGHT for p, t in WOBBLE_PAIRS
}

# Default alias — mismatch counting functions use this.
WOBBLE_W = WOBBLE_W_SS


# ── Mismatch counting functions ───────────────────────────────────

def wobble_mismatch_count(probe_seq, target_seq):
    """Count wobble-weighted mismatches between two ungapped sequences.

    Identical bases contribute 0.0, wobble pairs contribute (1 - weight),
    all other mismatches contribute 1.0.

    Args:
        probe_seq: Probe sequence (ungapped, uppercase)
        target_seq: Target sequence (ungapped, uppercase, same length as probe)

    Returns:
        (effective_mismatches: float, indels: int)
        indels is always 0 for ungapped comparison.
    """
    mm = 0.0
    for pb, tb in zip(probe_seq, target_seq):
        if pb == tb:
            continue
        w = WOBBLE_W.get((pb, tb), 0.0)
        if w == 0.0:
            mm += 1.0
        else:
            mm += 1.0 - w
    return mm, 0


def wobble_mismatch_count_gapped(probe_seq, target_row, msa_start, msa_end):
    """Count wobble-weighted mismatches from an MSA window.

    Extracts target_row[msa_start:msa_end], counts gap/N positions as indels,
    and applies wobble-weighted mismatch scoring on aligned (non-gap) positions.

    The probe_seq should be in sense orientation (matching the MSA strand).

    Args:
        probe_seq: Probe consensus in sense orientation (ungapped)
        target_row: Full MSA row string for one sequence
        msa_start: Start column in MSA (inclusive)
        msa_end: End column in MSA (exclusive)

    Returns:
        (effective_mismatches: float, n_indels: int)
    """
    window = target_row[msa_start:msa_end]
    mm = 0.0
    n_indels = 0
    for pb, tb in zip(probe_seq, window):
        if tb == '-' or tb == 'N' or tb == 'n':
            n_indels += 1
            continue
        tb_upper = tb.upper()
        if pb == tb_upper:
            continue
        w = WOBBLE_W.get((pb, tb_upper), 0.0)
        if w == 0.0:
            mm += 1.0
        else:
            mm += 1.0 - w
    return mm, n_indels


def wobble_mismatch_count_cols(probe_seq, target_row, msa_cols):
    """Count wobble-weighted mismatches at specific MSA columns.

    Like wobble_mismatch_count_gapped but uses explicit column indices
    instead of a contiguous [start:end] slice. This handles probes built
    from gap-trimmed consensus where the columns may not be contiguous.

    Args:
        probe_seq: Probe consensus in sense orientation (ungapped, len = len(msa_cols))
        target_row: Full MSA row string for one sequence
        msa_cols: List/array of MSA column indices to compare

    Returns:
        (effective_mismatches: float, n_indels: int)
    """
    mm = 0.0
    n_indels = 0
    for pb, col in zip(probe_seq, msa_cols):
        tb = target_row[col]
        if tb == '-' or tb == 'N' or tb == 'n':
            n_indels += 1
            continue
        tb_upper = tb.upper()
        if pb == tb_upper:
            continue
        w = WOBBLE_W.get((pb, tb_upper), 0.0)
        if w == 0.0:
            mm += 1.0
        else:
            mm += 1.0 - w
    return mm, n_indels
