"""Tests for build_output command logic."""

import argparse

import pandas as pd
import pytest

from primercast.commands.build_output import find_valid_probes_offtarget
from primercast.commands.build_output import run as run_build_output


class TestFindValidProbesOfftarget:
    """Tests for find_valid_probes_offtarget."""

    def test_no_off_target_data(self):
        """All probes valid when off-target data is empty."""
        pair_key = ("primer_f", "primer_r")
        probes = ["probe1", "probe2"]
        offtarget_full = [pd.DataFrame()]
        offtarget_mapping = [pd.DataFrame(columns=["probe_name", "probe_seq", "target_id", "start_pos", "orientation"])]
        probe_seqs = {"probe1": "ATCGATCG", "probe2": "GCTAGCTA"}
        buffer = 10

        result = find_valid_probes_offtarget(
            pair_key, probes, offtarget_full, offtarget_mapping, probe_seqs, buffer
        )
        assert sorted(result) == ["probe1", "probe2"]

    def test_probe_not_in_seqs(self):
        """Probe without sequence should be skipped."""
        pair_key = ("primer_f", "primer_r")
        probes = ["probe_missing"]
        offtarget_full = [pd.DataFrame()]
        offtarget_mapping = [pd.DataFrame(columns=["probe_name", "probe_seq", "target_id", "start_pos", "orientation"])]
        probe_seqs = {}
        buffer = 10

        result = find_valid_probes_offtarget(
            pair_key, probes, offtarget_full, offtarget_mapping, probe_seqs, buffer
        )
        assert result == []

    def test_probe_in_off_target_amplicon_excluded(self):
        """Probe within off-target amplicon should be excluded."""
        pair_key = ("primer_f", "primer_r")
        probes = ["probe1"]
        probe_seqs = {"probe1": "ATCGATCG"}  # len 8

        offtarget_full = [pd.DataFrame({
            "pname_f": ["primer_f"],
            "pname_r": ["primer_r"],
            "targets": ["['off_target1']"],
            "starts": ["[100]"],
            "prod_len": [300],
        })]
        offtarget_mapping = [pd.DataFrame({
            "probe_name": ["probe1"],
            "probe_seq": ["ATCGATCG"],
            "target_id": ["off_target1"],
            "start_pos": [150],
            "orientation": ["+"],
        })]
        buffer = 10

        result = find_valid_probes_offtarget(
            pair_key, probes, offtarget_full, offtarget_mapping, probe_seqs, buffer
        )
        assert "probe1" not in result

    def test_probe_not_mapped_to_off_target(self):
        """Probe not mapped to any off-target should be valid."""
        pair_key = ("primer_f", "primer_r")
        probes = ["probe1"]
        probe_seqs = {"probe1": "ATCGATCG"}

        offtarget_full = [pd.DataFrame({
            "pname_f": ["primer_f"],
            "pname_r": ["primer_r"],
            "targets": ["['off_target1']"],
            "starts": ["[100]"],
            "prod_len": [300],
        })]
        # Probe not in the mapping
        offtarget_mapping = [pd.DataFrame({
            "probe_name": ["other_probe"],
            "probe_seq": ["GCTAGCTA"],
            "target_id": ["off_target1"],
            "start_pos": [150],
            "orientation": ["+"],
        })]
        buffer = 10

        result = find_valid_probes_offtarget(
            pair_key, probes, offtarget_full, offtarget_mapping, probe_seqs, buffer
        )
        assert "probe1" in result

    def test_empty_probes_list(self):
        """Empty input probes should return empty list."""
        result = find_valid_probes_offtarget(
            ("f", "r"), [], [pd.DataFrame()],
            [pd.DataFrame(columns=["probe_name", "probe_seq", "target_id", "start_pos", "orientation"])],
            {}, 10
        )
        assert result == []


class TestRunTopPairSelection:
    """build-output keeps the filter step's pairs, then takes the top N by score."""

    N_PAIRS = 6

    @pytest.fixture
    def inputs(self, tmp_path):
        # Eval file in score order (best first), as written by evaluate.
        pairs = [(f"p{i}_f", f"p{i}_r") for i in range(1, self.N_PAIRS + 1)]
        pd.DataFrame({
            "pname_f": [f for f, _ in pairs],
            "pname_r": [r for _, r in pairs],
            "coverage": 0.9, "activity": 0.8,
            "score": [1.0 - i / 10 for i in range(self.N_PAIRS)],
        }).to_csv(tmp_path / "virusA.eval", index=False)
        with open(tmp_path / "virusA_filt.fa", "w") as fh:
            for f, r in pairs:
                fh.write(f">{f}\nACGTACGTACGTACGTACGT\n>{r}\nTTGCATGCATGCATGCAAGG\n")
        (tmp_path / "params.txt").write_text("NUM_TOP_SENSITIVITY = 2\n")
        return tmp_path

    def _args(self, d, **extra):
        values = dict(
            eval_on=str(d / "virusA.eval"), eval_off=[], primers=str(d / "virusA_filt.fa"),
            output=str(d / "virusA_final.csv"), name="virusA",
            param_file=str(d / "params.txt"), probe_mapping_on=None,
            probe_mapping_off=None, probe_seqs=None, ref=None,
        )
        values.update(extra)
        return argparse.Namespace(**values)

    def _final_pairs(self, d):
        final = pd.read_csv(d / "virusA_final.csv")
        return list(zip(final["pname_f"], final["pname_r"]))

    def test_keeps_filtered_pairs_ranked_below_top_n(self, inputs):
        # The filter kept only pairs ranked 4-6 (e.g. the top 3 failed coverage).
        pd.DataFrame({"pname_f": ["p4_f", "p5_f", "p6_f"],
                      "pname_r": ["p4_r", "p5_r", "p6_r"]}).to_csv(
            inputs / "virusA_filt.csv", index=False)

        run_build_output(self._args(inputs))

        # Top NUM_TOP_SENSITIVITY=2 of the filtered pairs, in score order.
        assert self._final_pairs(inputs) == [("p4_f", "p4_r"), ("p5_f", "p5_r")]

    def test_probe_mode_keeps_pairs_with_probes_ranked_below_top_n(self, inputs):
        # Probe-mode filter: only pairs ranked 5-6 have a usable probe.
        pd.DataFrame({"pname_f": ["p5_f", "p6_f"], "pname_r": ["p5_r", "p6_r"],
                      "probe_names": ["probe_a", "probe_b"]}).to_csv(
            inputs / "virusA_filt.csv", index=False)
        (inputs / "probes.fa").write_text(
            ">probe_a\nACGTTGCAACGTTGCAACGTTGCA\n>probe_b\nTGCAACGTTGCAACGTTGCAACGT\n")
        pd.DataFrame({"probe_name": ["probe_a", "probe_b"],
                      "target_id": ["s1", "s1"]}).to_csv(inputs / "probe_on.csv", index=False)

        run_build_output(self._args(inputs, probe_mapping_on=str(inputs / "probe_on.csv"),
                                    probe_seqs=str(inputs / "probes.fa")))

        final = pd.read_csv(inputs / "virusA_final.csv")
        assert list(zip(final["pname_f"], final["pname_r"])) == [("p5_f", "p5_r"), ("p6_f", "p6_r")]
        assert list(final["valid_probes"]) == ["probe_a", "probe_b"]
