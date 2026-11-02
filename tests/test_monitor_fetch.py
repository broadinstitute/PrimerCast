"""Tests for the monitor's incremental fetch.

A repeat run only downloads what NCBI released since the previous one. It used
to narrow the *collection* date as well, which dropped nearly every new
sequence (they are deposited long after collection) -- gget then wrote no FASTA
and the report said "Fetch failed".
"""

from pathlib import Path

import pytest

from primercast import adapt_cli

TARGET = "Ebola"
ROW = {"Target name": TARGET, "TaxID": "3044781"}


def _arg(cmd, flag):
    """Value following ``flag`` in a gget command, or None."""
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


@pytest.fixture
def fake_gget(monkeypatch):
    """Stand-in for the gget subprocess.

    Writes ``fake_gget.accessions`` as a FASTA into the --out directory, or
    nothing when the list is empty -- which is what gget does when no record
    passes its filters. Every command run is kept in ``fake_gget.calls``.
    """
    class Gget:
        accessions: list[str] = []
        calls: list[list[str]] = []

        def __call__(self, cmd, check=False, **kwargs):
            self.calls.append(cmd)
            if self.accessions:
                out = Path(_arg(cmd, "--out"))
                # Distinct sequences: the monitor drops identical ones.
                (out / "seqs.fasta").write_text("".join(
                    f">{a} x\n{'ACGT' * (i + 1)}\n" for i, a in enumerate(self.accessions)))

    gget = Gget()
    gget.calls = []
    monkeypatch.setattr(adapt_cli.subprocess, "run", gget)
    return gget


def _previous_fetch(work_dir, date_str, accessions):
    prev = work_dir / date_str
    prev.mkdir(parents=True)
    (prev / f"{TARGET}_accessions.txt").write_text("\n".join(accessions) + "\n")


def _fetch(work_dir, date_str, row=ROW):
    date_dir = work_dir / date_str
    date_dir.mkdir(parents=True, exist_ok=True)
    return adapt_cli._monitor_fetch([dict(row)], work_dir, date_dir, date_str)[TARGET]


# --- window narrowing -------------------------------------------------------

def test_repeat_run_narrows_release_date_only():
    row = {"TaxID": "1"}
    assert adapt_cli._narrow_to_new_releases(row, "20261029") == "2026-10-22"
    assert row["min_release_date"] == "2026-10-22"
    assert "min_collection_date" not in row


def test_users_collection_date_is_left_as_set():
    row = {"TaxID": "1", "min_collection_date": "2014-01-01"}
    adapt_cli._narrow_to_new_releases(row, "20261029")
    assert row["min_collection_date"] == "2014-01-01"


def test_later_user_release_date_wins():
    row = {"TaxID": "1", "min_release_date": "2026-10-28"}
    assert adapt_cli._narrow_to_new_releases(row, "20261029") == "2026-10-28"


def test_unparseable_previous_date_leaves_row_alone():
    row = {"TaxID": "1"}
    assert adapt_cli._narrow_to_new_releases(row, "latest") is None
    assert row == {"TaxID": "1"}


def test_gget_is_not_given_a_collection_date_on_repeat_runs(tmp_path, fake_gget):
    _previous_fetch(tmp_path, "20261029", ["A1"])
    fake_gget.accessions = ["A1", "B2"]

    _fetch(tmp_path, "20261102")

    cmd = fake_gget.calls[0]
    assert _arg(cmd, "--min_release_date") == "2026-10-22"
    assert "--min_collection_date" not in cmd


# --- empty fetches ----------------------------------------------------------

def test_quiet_repeat_run_is_zero_new_not_a_failure(tmp_path, fake_gget):
    _previous_fetch(tmp_path, "20261029", ["A1", "B2"])
    fake_gget.accessions = []

    result = _fetch(tmp_path, "20261102")

    assert result["status"] == "success"
    assert result["new_count"] == 0
    assert result["new_accessions"] == set()


def test_quiet_repeat_run_carries_the_accession_history_forward(tmp_path, fake_gget):
    """So the next run narrows from today and does not re-report A1/B2."""
    _previous_fetch(tmp_path, "20261029", ["A1", "B2"])
    fake_gget.accessions = []
    _fetch(tmp_path, "20261102")

    fake_gget.accessions = ["A1", "C3"]
    result = _fetch(tmp_path, "20261103")

    assert _arg(fake_gget.calls[-1], "--min_release_date") == "2026-10-26"  # 20261102 - 7 days
    assert result["new_accessions"] == {"C3"}


def test_first_run_matching_nothing_is_still_flagged(tmp_path, fake_gget):
    fake_gget.accessions = []

    result = _fetch(tmp_path, "20261029")

    assert result["status"] == "no_output"
    assert not (tmp_path / "20261029" / f"{TARGET}_accessions.txt").exists()


def test_new_sequences_are_found_on_a_repeat_run(tmp_path, fake_gget):
    _previous_fetch(tmp_path, "20261029", ["A1"])
    fake_gget.accessions = ["A1", "B2"]

    result = _fetch(tmp_path, "20261102")

    assert result["status"] == "success"
    assert result["new_accessions"] == {"B2"}
    assert result["total_count"] == 2


# --- GUI copy ----------------------------------------------------------------

def test_gui_fetch_uses_the_shared_helpers():
    """The GUI runs its own copy of the fetch loop; keep it on the same rules."""
    source = (Path(__file__).resolve().parent.parent / "gui" / "app.py").read_text()
    assert "_narrow_to_new_releases(row," in source
    assert "_no_new_releases_result(" in source
    assert '("min_release_date", "min_collection_date")' not in source
