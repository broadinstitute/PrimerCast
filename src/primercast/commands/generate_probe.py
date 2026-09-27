"""Generate candidate probe sequences from target sequences."""

import argparse
import random
import time
from collections import defaultdict

import pandas as pd
from Bio import SeqIO
from Bio.SeqUtils import gc_fraction

from primercast.utils import get_tm, parse_params, get_probe_params, reverse_complement_dna, has_homopolymer, sanitize_iupac
from primercast.external import compute_batch_dimer_dg
from primercast.utils.diagnostics import StageDiagnostics


def register(subparsers):
    """Register the generate-probe subcommand."""
    parser = subparsers.add_parser(
        "generate-probe",
        help="Generate candidate probe sequences from target sequences",
        description="""
Generate candidate probe sequences from target sequences by tiling across
all possible lengths, then filtering by melting temperature (Tm), GC content,
self-dimer free energy (ΔG), homopolymer runs, and 5' nucleotide constraints.
""",
    )
    parser.add_argument("--in", dest="target_seqs", required=True,
                       help="Input FASTA of target sequences")
    parser.add_argument("--out", dest="probe_seqs", required=True,
                       help="Output FASTA for probes")
    parser.add_argument("--params", dest="param_file", required=True,
                       help="Parameters file (params.txt)")
    parser.add_argument("--name", required=True,
                       help="Name prefix for probe IDs")
    parser.add_argument("--conserved-regions", dest="conserved_regions", default=None,
                       help="TSV of conserved probe regions (from pick-representatives)")
    parser.set_defaults(func=run)


def _position_in_regions(pos, probe_len, regions):
    """Check if a probe at pos with given length falls within any conserved region."""
    for start, end in regions:
        if pos >= start and pos + probe_len <= end:
            return True
    return False


def generate_probes(
    target_seqs,
    len_min: int,
    len_max: int,
    max_tm: float,
    min_tm: float,
    max_gc: float,
    min_dg: float,
    homopolymer_max: int,
    avoid_5prime_G: bool,
    max_num: int,
    conserved_regions=None,
    diag: StageDiagnostics | None = None,
):
    """Generate and filter probe candidates across multiple target sequences.

    Args:
        conserved_regions: Optional list of (start, end) tuples. If provided,
            only probes falling entirely within a conserved region are generated.
        diag: Optional diagnostics; the count left after each filter is recorded.
    """
    # Generate all candidate probes with step=1 from both strands
    probes = {}
    for target_seq in target_seqs:
        seq_len = len(target_seq)

        # Generate from forward strand
        for probe_len in range(len_min, len_max + 1):
            for i in range(0, seq_len - probe_len + 1):
                if conserved_regions and not _position_in_regions(i, probe_len, conserved_regions):
                    continue
                seq = target_seq[i : i + probe_len]
                if "N" not in seq:
                    probes[seq] = i

        # Generate from reverse complement strand
        target_seq_rc = reverse_complement_dna(target_seq)
        for probe_len in range(len_min, len_max + 1):
            for i in range(0, seq_len - probe_len + 1):
                original_pos = seq_len - i - probe_len
                if conserved_regions and not _position_in_regions(original_pos, probe_len, conserved_regions):
                    continue
                seq = target_seq_rc[i : i + probe_len]
                if "N" not in seq:
                    probes[seq] = original_pos

    print(f">> Probes with unique sequence: {len(probes)}")

    # Filter probes
    filtered = {}
    features = defaultdict(dict)

    # First pass: fast filters (5' G, homopolymer, Tm, GC)
    tm_gc_passed = {}
    n_5prime_ok = n_homopolymer_ok = 0
    for pseq in probes:
        if avoid_5prime_G and pseq[0] == 'G':
            continue
        n_5prime_ok += 1
        if has_homopolymer(pseq, homopolymer_max):
            continue
        n_homopolymer_ok += 1

        tm = get_tm(pseq)
        gc = gc_fraction(pseq)

        if min_tm <= tm <= max_tm and gc <= max_gc / 100.0:
            tm_gc_passed[pseq] = (tm, gc)

    # Batch dG computation (single subprocess call)
    if tm_gc_passed:
        pairs = [(pseq, pseq) for pseq in tm_gc_passed]
        dg_values = compute_batch_dimer_dg(pairs)

        for (pseq, _), dG in zip(pairs, dg_values):
            if dG >= min_dg:
                tm, gc = tm_gc_passed[pseq]
                features[pseq]["Tm"] = round(tm, 1)
                features[pseq]["GC"] = round(gc, 2)
                features[pseq]["len"] = len(pseq)
                features[pseq]["position"] = probes[pseq]
                features[pseq]["dG"] = round(dG, 1)
                filtered[pseq] = probes[pseq]

    print(f">> Probes filtered: {len(filtered)}")

    if diag is not None:
        diag.record(
            "Tiling (probe windows without N)", len(probes),
            {"PROBE_LEN_MIN": len_min, "PROBE_LEN_MAX": len_max},
            hint=("Only windows inside the conserved regions are considered."
                  if conserved_regions else ""),
        )
        if avoid_5prime_G:
            diag.record("No 5' G", n_5prime_ok, {"PROBE_AVOID_5PRIME_G": True})
        diag.record("Homopolymer runs", n_homopolymer_ok, {"PROBE_HOMOPOLYMER_MAX": homopolymer_max})
        diag.record("Tm / GC", len(tm_gc_passed),
                    {"PROBE_TM_MIN": min_tm, "PROBE_TM_MAX": max_tm, "PROBE_GC_MAX": max_gc})
        diag.record("Self-dimer dG", len(filtered), {"PROBE_DG_MIN": min_dg})

    # Subsample if needed
    if len(filtered) > max_num:
        selected_seqs = random.sample(list(filtered.keys()), max_num)
        filtered = {seq: filtered[seq] for seq in selected_seqs}
        print(f">> Randomly subsampled to: {len(filtered)}")

    return filtered, features


def run(args):
    """Run the generate-probe command."""
    # Sanitize name for use in FASTA headers (spaces break SAM format)
    args.name = args.name.replace(" ", "_")
    params = parse_params(args.param_file)
    probe_params = get_probe_params(params)

    len_min = probe_params["len_min"]
    len_max = probe_params["len_max"]
    min_tm = probe_params["min_tm"]
    max_tm = probe_params["max_tm"]
    max_gc = probe_params["max_gc"]
    min_dg = probe_params["min_dg"]
    homopolymer_max = probe_params["homopolymer_max"]
    avoid_5prime_G = probe_params["avoid_5prime_g"]
    max_num = probe_params["max_num"]

    target_seqs = [sanitize_iupac(str(s.seq)) for s in SeqIO.parse(args.target_seqs, "fasta")]

    # Load conserved regions if provided
    conserved_regions = None
    if args.conserved_regions:
        import csv
        conserved_regions = []
        with open(args.conserved_regions) as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                conserved_regions.append((int(row['start']), int(row['end'])))
        print(f"Using {len(conserved_regions)} conserved probe region(s)")

    print(f"Generating probes from {args.target_seqs}...")
    print(f"Probe lengths: {len_min}-{len_max}")
    print(f"Tm range: {min_tm}-{max_tm}")
    start_time = time.time()

    diag = StageDiagnostics("generate_probe", target=args.name, unit="probes")
    filtered, features = generate_probes(
        target_seqs, len_min, len_max,
        max_tm, min_tm, max_gc, min_dg,
        homopolymer_max, avoid_5prime_G, max_num,
        conserved_regions=conserved_regions,
        diag=diag,
    )
    diag.finish(args.probe_seqs)

    # Write output FASTA
    probe_list = list(filtered.keys())
    with open(args.probe_seqs, "w") as fout:
        for i, seq in enumerate(probe_list):
            pname = f"{args.name}_{i+1}_probe"
            features[seq]["pname"] = pname
            fout.write(f">{pname}\n{seq}\n")

    # Write features CSV
    if features:
        features_df = pd.DataFrame(features).T
        features_df = features_df.reset_index(names="pseq")[["pname", "pseq", "len", "position", "Tm", "GC", "dG"]]
    else:
        features_df = pd.DataFrame(columns=["pname", "pseq", "len", "position", "Tm", "GC", "dG"])

    fname = args.probe_seqs.replace(".fa", ".feat")
    features_df.to_csv(fname, index=False)

    runtime = time.time() - start_time
    print(f"Wrote {len(probe_list)} probes to {args.probe_seqs} ({runtime:.1f} sec)")
