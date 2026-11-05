"""Dry-run the generated Snakefile to check which rules build each output."""

import shutil
import subprocess
from pathlib import Path

import pytest

from primercast.adapt_cli import _build_snakefile

pytestmark = pytest.mark.skipif(
    shutil.which("snakemake") is None, reason="snakemake not installed"
)

PARAMS_TEMPLATE = Path(__file__).parent.parent / "workflows" / "params.txt.template"


def _dry_run(tmp_path, seqs, config):
    (tmp_path / "target_seqs" / "original").mkdir(parents=True)
    fasta = "".join(f">s{i}\n{s}\n" for i, s in enumerate(seqs))
    (tmp_path / "target_seqs" / "original" / "T-1.fa").write_text(fasta)
    # Quick design (multi-sequence) reads an alignment.
    (tmp_path / "target_seqs" / "msa").mkdir()
    (tmp_path / "target_seqs" / "msa" / "T-1.aln").write_text(fasta)
    shutil.copy(PARAMS_TEMPLATE, tmp_path / "params.txt")
    (tmp_path / "Snakefile").write_text(_build_snakefile(["T-1"], [], [], [], "r"))
    result = subprocess.run(
        ["snakemake", "-s", "Snakefile", "--cores", "1", "--dry-run",
         "--config", *config],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


class TestProbeDesignDag:
    def test_single_sequence_filters_with_filter_rule(self, tmp_path):
        # Regression: standard mode has no probe pair assignments, so snakemake
        # used to fall back to prepare_pset_fasta for <target>_filt.fa.
        out = _dry_run(tmp_path, ["ACGT" * 50], ["probe=1"])
        assert "filter_primer_list" in out
        assert "prepare_pset_fasta" not in out

    def test_multi_sequence_uses_quick_design(self, tmp_path):
        out = _dry_run(tmp_path, ["ACGT" * 50, "ACGA" * 50], ["probe=1"])
        assert "quick_design_on_target" in out
        assert "filter_primer_list" in out
