"""Tests for prepare-input's argument contract.

`prepare_input.run` is called two ways: by argparse from the Snakefile, which
fills in every flag, and directly by quick-design, which hands it a Namespace
built by hand with only the arguments it knows about. A new `store_true` flag
read as a bare `args.<flag>` therefore crashes every multi-sequence target
while the single-sequence path keeps working -- which is exactly what `--exact`
did. These tests pin both the behaviour and the contract.
"""

import argparse
import ast
from pathlib import Path

import pandas as pd
import pytest

from primercast.commands import prepare_input, quick_design

# A 20-mer and its exact alignment line, as sam2pairwise writes them: '|' for a
# match, a space for a mismatch.
FWD = "ACGTACGTACGTACGTACGT"
REV = "TTGCATTGCATTGCATTGCA"
EXACT = "|" * 20
ONE_MM = "|" * 19 + " "


@pytest.fixture
def inputs(tmp_path):
    """Minimal on-target inputs: two forward primers, one reverse.

    F_exact and F_mm both pair with R within the amplicon window, so the only
    thing separating them is whether imperfect alignments are kept.
    """
    ref = tmp_path / "t1.fa"
    ref.write_text(">t1\n" + "ACGT" * 50 + "\n")

    params = tmp_path / "params.txt"
    params.write_text("AMPLEN_MIN = 60\nAMPLEN_MAX = 200\n")

    mapped = tmp_path / "in.mapped"
    mapped.write_text(
        f"F_exact\t0\tt1\t10\t{FWD}\t{FWD}\t{EXACT}\n"
        f"F_mm\t0\tt1\t12\t{FWD}\t{FWD}\t{ONE_MM}\n"
        f"R\t16\tt1\t120\t{REV}\t{REV}\t{EXACT}\n"
    )

    feats = tmp_path / "feat.csv"
    pd.DataFrame(
        {"forrev": ["f", "f", "r"], "len": [20, 20, 20],
         "Tm": [60.0, 60.0, 60.0], "GC": [50.0, 50.0, 50.0]},
        index=["F_exact", "F_mm", "R"],
    ).to_csv(feats)

    return {"mapped": str(mapped), "reference": str(ref),
            "param_file": str(params), "pri_features": str(feats),
            "ml_input": str(tmp_path / "out.input")}


def _quick_design_namespace(inputs, **extra):
    """The Namespace quick-design builds -- required arguments and nothing else."""
    return argparse.Namespace(
        mapped=inputs["mapped"],
        ml_input=inputs["ml_input"],
        reference=inputs["reference"],
        param_file=inputs["param_file"],
        reftype="on",
        pri_features=inputs["pri_features"],
        prev="",
        **extra,
    )


def _pair_names(ml_input):
    out = pd.read_csv(ml_input)
    return sorted(out["pname_f"])


def test_run_accepts_the_namespace_quick_design_builds(inputs):
    """The regression: a bare args.exact raised AttributeError here."""
    prepare_input.run(_quick_design_namespace(inputs))

    # No --exact, so the 1-mismatch alignment is kept: quick design tiles from
    # one representative and aligns against every variant, where a mismatched
    # alignment is real coverage rather than a chance match.
    assert _pair_names(inputs["ml_input"]) == ["F_exact", "F_mm"]


def test_exact_drops_imperfect_alignments(inputs):
    prepare_input.run(_quick_design_namespace(inputs, exact=True))

    assert _pair_names(inputs["ml_input"]) == ["F_exact"]


def _prepare_input_dests():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    prepare_input.register(sub)
    return {a.dest for a in sub.choices["prepare-input"]._actions} - {"help"}


def _quick_design_namespaces():
    """Keyword sets of every argparse.Namespace quick-design feeds prepare-input.

    Identified by `ml_input`, which only prepare-input takes.
    """
    tree = ast.parse(Path(quick_design.__file__).read_text())
    return [
        {kw.arg for kw in node.keywords}
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Namespace"
        and any(kw.arg == "ml_input" for kw in node.keywords)
    ]


def test_quick_design_supplies_every_required_argument():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    prepare_input.register(sub)
    required = {a.dest for a in sub.choices["prepare-input"]._actions if a.required}

    namespaces = _quick_design_namespaces()
    assert len(namespaces) >= 4, "prepare-input call sites not found in quick_design"
    for keys in namespaces:
        assert required <= keys, f"missing {sorted(required - keys)}"


def _bare_args_attrs(tree):
    """Names read as `args.<name>`, which raises when the attribute is absent."""
    return {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name) and node.value.id == "args"
    }


def _defaulted_args_attrs(tree):
    """Names read as `getattr(args, "<name>", <default>)`."""
    return {
        node.args[1].value for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "getattr"
        and len(node.args) == 3
        and isinstance(node.args[0], ast.Name) and node.args[0].id == "args"
        and isinstance(node.args[1], ast.Constant)
    }


def test_arguments_quick_design_omits_are_read_with_a_default():
    """Guards the contract a bare attribute access broke.

    Anything quick-design does not pass has to be read via getattr, or adding a
    flag to the subparser silently breaks every multi-sequence target.
    """
    tree = ast.parse(Path(prepare_input.__file__).read_text())
    bare = _bare_args_attrs(tree)
    defaulted = _defaulted_args_attrs(tree)

    omitted = set()
    for keys in _quick_design_namespaces():
        omitted |= _prepare_input_dests() - keys - {"func"}

    assert omitted, "expected at least one argument quick-design does not pass"
    for dest in sorted(omitted):
        assert dest in defaulted, f"{dest} is not read with a getattr default"
        assert dest not in bare, f"{dest} is also read as a bare args.{dest}"
