"""Evaluate primer-target pairs using ML models."""

import argparse
import ast
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from Bio import SeqIO
from sklearn.preprocessing import MultiLabelBinarizer
from torch.utils.data import DataLoader

from primercast.models import load_models, PcrDataset, FEATURE_COLUMNS
from primercast.utils import encode_batch_parallel
from primercast.utils.diagnostics import StageDiagnostics, diagnostics_path, format_message

# Per-pair coverage is saved in the diagnostics only for small evaluations
# (evaluate mode); design runs score thousands of pairs.
MAX_DETAIL_PAIRS = 50


def register(subparsers):
    """Register the evaluate subcommand."""
    parser = subparsers.add_parser(
        "evaluate",
        help="Evaluate primer-target matches using ML",
        description="""
Evaluate primer-target pairs using trained ML models (classifier + regressor).
Produces raw per-pair scores, aggregated per-primer-pair scores, and final
ranked table with coverage, activity, and score.
""",
    )
    parser.add_argument("--in", dest="input", required=True, help="Input CSV from prepare-input")
    parser.add_argument("--out", dest="output", required=True, help="Output CSV for evaluation results")
    parser.add_argument("--ref", dest="reference", required=True, help="Reference FASTA")
    parser.add_argument("--reftype", dest="reftype", required=True, choices=["on", "off"], help="on-target or off-target")
    parser.add_argument("--threads", type=int, default=1, help="Number of threads for encoding")
    parser.add_argument(
        "--fail-if-empty", action="store_true",
        help="On-target only: stop (exit 3) with an explanation if no target "
             "sequence is predicted to amplify. Without it, the diagnostics are "
             "only recorded (evaluate mode reports them as a warning).",
    )
    parser.set_defaults(func=run)


def _reach_hint(input_path):
    """Why no target was reached, from prepare-input's diagnostics if it has one."""
    try:
        data = json.loads(diagnostics_path(input_path).read_text())
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and data.get("empty"):
        return f"Cause: {format_message(data)}"
    return ("Primers must align to a target, in the right orientation, "
            "within the amplicon length range.")


def _coverage_diagnostics(args, tnames, reached, clstbl):
    """Target-level funnel for on-target evaluation (None for off-target)."""
    if args.reftype != "on":
        return None
    diag = StageDiagnostics(
        "evaluate", target=Path(args.reference).stem, unit="target sequences",
        fatal=bool(getattr(args, "fail_if_empty", False)),
    )
    diag.record("Target sequences in reference", len(tnames))
    diag.record("Reached by a primer pair (aligned, oriented, amplicon length)",
                len(reached), hint=_reach_hint(args.input))
    active = clstbl > .5 if clstbl is not None else None
    diag.record(
        "Predicted to amplify (classifier > 0.5)",
        int(active.any(axis=0).sum()) if active is not None else 0,
        hint="The model predicts no amplification of any target; mismatches "
             "(especially near the primer 3' ends) are the usual cause.",
    )
    if active is not None and len(active) <= MAX_DETAIL_PAIRS:
        diag.details["pair_coverage"] = {
            f"{f}/{r}": int(n) for (f, r), n in active.sum(axis=1).items()
        }
    return diag


def _finish_diagnostics(diag, output):
    """Save the funnel; warn (non-fatal) or stop (fatal) when coverage is zero."""
    if diag is None:
        return
    if diag.empty and not diag.fatal:
        print(f"WARNING: {diag.message()}", file=sys.stderr, flush=True)
    diag.finish(output)


def run(args):
    """Run the evaluate command."""
    tnames = [s.id for s in SeqIO.parse(args.reference, "fasta")]

    if os.path.getsize(args.input) == 0:
        print(f"No primer pairs to evaluate in {args.input}.")
        open(args.output, "w").close()
        _finish_diagnostics(_coverage_diagnostics(args, tnames, set(), None), args.output)
        sys.exit()

    # Load models
    scaler, classifier, regressor, device = load_models()

    # Set PyTorch threads for CPU inference
    torch.set_num_threads(int(args.threads))

    print(f"Evaluating {args.input} with {device} ({args.threads} threads)...")
    start_time = time.time()

    header_flag = True
    mode = 'w'
    clstbl, regtbl = [], []
    reached = set()

    for i, chunk in enumerate(pd.read_csv(args.input, chunksize=20000)):
        chunk['targets'] = chunk['targets'].apply(ast.literal_eval)
        reached.update(t for targets in chunk['targets'] for t in targets)

        inps_fe = chunk[FEATURE_COLUMNS]
        inps_fe = scaler.transform(inps_fe)
        inps_se = chunk[['pseq_f', 'tseq_f', 'pseq_r', 'tseq_r']]
        inps_se = encode_batch_parallel(inps_se, int(args.threads))
        dataset = PcrDataset(inps_se, inps_fe, np.array([0] * len(chunk)))
        loader = DataLoader(dataset, batch_size=256, shuffle=False)

        predict_cls, predict_reg = [], []
        with torch.no_grad():
            for seq_in, fea_in, _ in loader:
                seq_in = seq_in.to(device).float()
                fea_in = fea_in.to(device).float()
                out_cls = classifier(fea_in, seq_in)
                out_reg = regressor(fea_in, seq_in)
                if len(seq_in) == 1:
                    predict_cls.append(np.array([out_cls.squeeze().detach().cpu().numpy()]))
                    predict_reg.append(np.array([out_reg.squeeze().detach().cpu().numpy()]))
                else:
                    predict_cls.append(out_cls.squeeze().detach().cpu().numpy())
                    predict_reg.append(out_reg.squeeze().detach().cpu().numpy())
            predict_cls = np.concatenate(predict_cls)
            predict_reg = np.round(np.concatenate(predict_reg), decimals=3)

        chunk.loc[:, 'classifier'] = predict_cls
        chunk.loc[:, 'regressor'] = predict_reg
        chunk.to_csv(f'{args.output}.full', mode=mode, header=header_flag, index=False)

        mlb = MultiLabelBinarizer()
        onehot = mlb.fit_transform(chunk['targets'])
        target_cols = list(mlb.classes_)

        for label, l in zip(['classifier', 'regressor'], [clstbl, regtbl]):
            targets_df = pd.DataFrame(onehot, columns=target_cols, index=chunk.index)
            targets_df = targets_df.mul(chunk[label], axis=0)
            evaltbl = pd.concat([chunk[['pname_f', 'pname_r']], targets_df], axis=1)
            agg_dict = {c: "max" for c in evaltbl.columns[2:]}
            evaltbl = evaltbl.groupby(['pname_f', 'pname_r']).agg(agg_dict).reset_index()
            l.append(evaltbl)

        # Periodically consolidate to bound memory
        if len(clstbl) >= 10:
            clstbl = [pd.concat(clstbl, ignore_index=True).groupby(['pname_f', 'pname_r']).agg("max").reset_index()]
            regtbl = [pd.concat(regtbl, ignore_index=True).groupby(['pname_f', 'pname_r']).agg("max").reset_index()]

        header_flag = False
        mode = 'a'

    clstbl = pd.concat(clstbl, ignore_index=True)
    regtbl = pd.concat(regtbl, ignore_index=True).reindex(columns=clstbl.columns)
    agg_dict = {c: "max" for c in regtbl.columns[2:]}
    regtbl = regtbl.reset_index().groupby(['pname_f', 'pname_r']).agg(agg_dict)
    clstbl = clstbl.reset_index().groupby(['pname_f', 'pname_r']).agg(agg_dict)
    clstbl.fillna(0).round(3).to_csv(f'{args.output}.cl')
    regtbl.fillna(0).round(3).to_csv(f'{args.output}.re')

    # Compute coverage and activity
    coverage = (clstbl > .5).sum(axis=1).reset_index(name='coverage')
    if args.reftype == 'on':
        coverage['coverage'] = coverage['coverage'] / len(tnames)
        # Activity = mean(reg) over active targets only
        # (coverage already penalizes missing/inactive targets; no double penalty)
        active_mask = clstbl > .5
        active_reg = regtbl.where(active_mask)
        activity = active_reg.mean(axis=1).reset_index(name='activity')
    else:
        activity = (regtbl * (clstbl > .5)).max(axis=1).reset_index(name='activity')

    res = coverage.merge(activity, on=['pname_f', 'pname_r'])
    res['activity'] = res['activity'].fillna(0)
    res['score'] = res['coverage'] * res['activity']
    res = res.sort_values('score', ascending=False)
    res.round(4).to_csv(args.output, index=False)

    runtime = time.time() - start_time
    print(f"Wrote {len(res)} lines to {args.output} ({runtime:.1f} sec)")

    _finish_diagnostics(_coverage_diagnostics(args, tnames, reached, clstbl), args.output)
