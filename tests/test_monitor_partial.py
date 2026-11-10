"""`adapt monitor` when one target's evaluation fails.

A 24k-genome SARS-CoV-2 evaluation was killed for memory. The run still emailed,
still recorded those sequences as seen (so they would never be evaluated), and
still reported plain success.
"""

import argparse
from pathlib import Path

import pytest

from primercast import adapt_cli, monitor_schedule

SHEET = adapt_cli._rows_to_sheet_csv(
    ["Target name", "Forward", "Reverse", "TaxID"],
    [{"Target name": "GOOD", "Forward": "ACGTACGTACGTACGTACGT", "Reverse": "TTTTGGGGCCCCAAAATTTT", "TaxID": "1"},
     {"Target name": "BAD", "Forward": "ACGTACGTACGTACGTACGT", "Reverse": "TTTTGGGGCCCCAAAATTTT", "TaxID": "2"}])


@pytest.fixture
def run(tmp_path, monkeypatch):
    sent = {}
    (tmp_path / "params.txt").write_text("TM_MIN = 55\n")
    (tmp_path / "sheet.csv").write_text(SHEET)

    def fake_fetch(rows, work_dir, date_dir, date_str, dry_run=False):
        out = {}
        for name in ("GOOD", "BAD"):
            (date_dir / f"{name}_accessions.txt").write_text("A.1\n")
            fa = date_dir / f"{name}_new.fa"
            fa.write_text(">A.1\nACGT\n")
            out[name] = {"status": "success", "new_fasta_path": fa, "metadata_path": None,
                         "new_accessions": {"A.1"}, "revised_accessions": set(),
                         "total_count": 1, "new_count": 1, "revised_count": 0}
        return out

    def fake_evaluate(target_name, date_dir, **kw):
        if target_name == "BAD":
            return None, []
        d = date_dir / target_name
        d.mkdir()
        (d / "report.xlsx").write_bytes(b"x")
        return d, []

    monkeypatch.setattr(adapt_cli, "_monitor_fetch", fake_fetch)
    monkeypatch.setattr(adapt_cli, "_monitor_evaluate", fake_evaluate)
    monkeypatch.setattr(adapt_cli, "_read_excel_summary", lambda p: "summary")
    monkeypatch.setattr(adapt_cli, "_email_config", lambda p: ("from@x.org", "pw"))
    monkeypatch.setattr(adapt_cli, "_send_email", lambda **kw: sent.update(kw))
    monkeypatch.setattr(adapt_cli.shutil, "which", lambda name: f"/bin/{name}")

    def go(**over):
        args = argparse.Namespace(
            unschedule=False, schedule=False, params=str(tmp_path / "params.txt"),
            workdir=str(tmp_path / "mon"), runid="r1", csv=str(tmp_path / "sheet.csv"),
            url=None, query_ids=None, email="me@x.org", manage_url=None, cores=1,
            skip_fetch=False, dry_run=False, **over)
        with pytest.raises(SystemExit) as exc:
            adapt_cli.cmd_monitor(args)
        date_dir = next((tmp_path / "mon" / "r1").iterdir())
        return exc.value.code, date_dir, sent
    return go


def test_exits_partial_and_names_the_target(run, capsys):
    code, _, _ = run()
    assert code == monitor_schedule.PARTIAL_EXIT_CODE
    assert "Targets with problems: BAD (evaluate failed)" in capsys.readouterr().out


def test_failed_targets_sequences_stay_unseen(run):
    _, date_dir, _ = run()
    assert not (date_dir / "BAD_accessions.txt").exists()
    assert (date_dir / "GOOD_accessions.txt").exists()


def test_email_still_goes_out_and_says_what_happens_next(run):
    _, _, sent = run()
    assert "will be evaluated again on the next run" in sent["body"]
    assert [p.name for p in sent["attachments"]] == ["GOOD_report.xlsx"]
