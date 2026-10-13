"""Tests for scheduled monitors (primercast.monitor_schedule)."""

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from primercast import monitor_schedule as ms


def _schedule(store=None, **overrides):
    schedule = ms.new_schedule(
        targets=["SC2"],
        primer_sets=["CDC_N1"],
        frequency=overrides.pop("frequency", "monthly"),
        recipients=["a@example.com"],
        params_txt="AMPLEN_MIN = 60\n",
        csv_text="Pathogen,TaxID\nSARS-CoV-2,2697049\n",
        created=overrides.pop("created", date(2026, 1, 10)),
    )
    schedule.update(overrides)
    if store is not None:
        ms.save_schedule(store, schedule)
    return schedule


@pytest.fixture
def store(tmp_path):
    return ms.LocalStore(tmp_path / "store")


# ---------------------------------------------------------------------------
# Due-date logic
# ---------------------------------------------------------------------------
class TestLastOccurrence:
    @pytest.mark.parametrize("freq,day,expected", [
        ("monthly", date(2026, 3, 1), date(2026, 3, 1)),
        ("monthly", date(2026, 3, 31), date(2026, 3, 1)),
        ("biweekly", date(2026, 3, 14), date(2026, 3, 1)),
        ("biweekly", date(2026, 3, 15), date(2026, 3, 15)),
        ("biweekly", date(2026, 3, 30), date(2026, 3, 15)),
        ("quarterly", date(2026, 1, 1), date(2026, 1, 1)),
        ("quarterly", date(2026, 3, 31), date(2026, 1, 1)),
        ("quarterly", date(2026, 6, 2), date(2026, 4, 1)),
        ("quarterly", date(2026, 12, 25), date(2026, 10, 1)),
    ])
    def test_occurrences(self, freq, day, expected):
        assert ms.last_occurrence(freq, day) == expected


class TestIsDue:
    def test_not_due_before_first_date_after_creation(self):
        assert not ms.is_due(_schedule(created=date(2026, 1, 10)), date(2026, 1, 31))

    def test_due_on_first_date_after_creation(self):
        assert ms.is_due(_schedule(created=date(2026, 1, 10)), date(2026, 2, 1))

    def test_missed_date_is_caught_up(self):
        assert ms.is_due(_schedule(created=date(2026, 1, 10)), date(2026, 2, 5))

    def test_not_due_after_run(self):
        s = _schedule(created=date(2026, 1, 10), last_run="2026-02-01")
        assert not ms.is_due(s, date(2026, 2, 20))
        assert ms.is_due(s, date(2026, 3, 1))

    def test_created_on_run_date_waits_for_next(self):
        # The GUI runs the monitor immediately on creation.
        s = _schedule(created=date(2026, 2, 1))
        assert not ms.is_due(s, date(2026, 2, 1))

    def test_stopped_never_due(self):
        assert not ms.is_due(_schedule(status="stopped"), date(2027, 1, 1))


class TestCleanRecipients:
    def test_splits_and_filters(self):
        assert ms.clean_recipients(" a@x.org, bad, b@y.com ,") == ["a@x.org", "b@y.com"]

    def test_caps_count(self):
        many = ",".join(f"u{i}@x.org" for i in range(20))
        assert len(ms.clean_recipients(many)) == ms.MAX_RECIPIENTS


# ---------------------------------------------------------------------------
# Store + schedule CRUD
# ---------------------------------------------------------------------------
class TestSchedules:
    def test_save_load_list(self, store):
        s = _schedule(store)
        assert ms.load_schedule(store, s["id"]) == s
        assert [x["id"] for x in ms.list_schedules(store)] == [s["id"]]

    def test_load_rejects_bad_id(self, store):
        assert ms.load_schedule(store, "../../etc/passwd") is None

    def test_find_by_ref_checks_token(self, store):
        s = _schedule(store)
        assert ms.find_by_ref(store, f"{s['id']}.{s['token']}")["id"] == s["id"]
        assert ms.find_by_ref(store, f"{s['id']}.wrong") is None
        assert ms.find_by_ref(store, s["id"]) is None
        assert ms.find_by_ref(store, "") is None

    def test_manage_url(self):
        s = _schedule()
        assert ms.manage_url("https://x.org/", s) == f"https://x.org/?monitor={s['id']}.{s['token']}"

    def test_create_respects_cap(self, store):
        ms.create_schedule(store, _schedule(), max_active=2)
        ms.create_schedule(store, _schedule(), max_active=2)
        with pytest.raises(ms.ScheduleLimitError):
            ms.create_schedule(store, _schedule(), max_active=2)

    def test_stopped_do_not_count_toward_cap(self, store):
        ms.stop_schedule(store, _schedule(store))
        ms.create_schedule(store, _schedule(), max_active=1)

    def test_unknown_frequency(self):
        with pytest.raises(ValueError):
            _schedule(frequency="daily")


class TestRunData:
    def test_upload_top_level_files_only(self, store, tmp_path):
        date_dir = tmp_path / "work" / "20260201"
        (date_dir / "SC2").mkdir(parents=True)
        (date_dir / "SC2_accessions.txt").write_text("A1\n")
        (date_dir / "SC2_report.xlsx").write_text("x")
        (date_dir / "SC2" / "huge.bt2").write_text("x")
        assert ms.upload_run_dir(store, "abc", date_dir) == 2
        assert store.list("runs/abc/") == [
            "runs/abc/20260201/SC2_accessions.txt",
            "runs/abc/20260201/SC2_report.xlsx",
        ]

    def test_pull_history_only_accessions(self, store, tmp_path):
        store.write_text("runs/abc/20260101/SC2_accessions.txt", "A1\n")
        store.write_text("runs/abc/20260101/SC2_report.xlsx", "x")
        work = tmp_path / "work"
        assert ms.pull_history(store, "abc", work) == 1
        assert (work / "20260101" / "SC2_accessions.txt").read_text() == "A1\n"
        assert not (work / "20260101" / "SC2_report.xlsx").exists()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
class TestDispatch:
    def test_runs_only_due_and_records(self, store):
        due = _schedule(store, created=date(2026, 1, 10))
        not_due = _schedule(store, created=date(2026, 2, 3))
        ran = []

        def runner(store_, schedule, scratch, cores):
            ran.append(schedule["id"])
            assert scratch.is_dir()
            return ms.DispatchResult(schedule["id"], True)

        results = ms.dispatch(store, day=date(2026, 2, 5), runner=runner)
        assert ran == [due["id"]]
        assert [r.ok for r in results] == [True]
        saved = ms.load_schedule(store, due["id"])
        assert saved["last_run"] == "2026-02-05"
        assert saved["history"][-1]["status"] == "ok"
        assert ms.load_schedule(store, not_due["id"])["last_run"] is None

    def test_failure_retried_then_given_up(self, store):
        s = _schedule(store, created=date(2026, 1, 10))
        fail = lambda store_, schedule, scratch, cores: ms.DispatchResult(schedule["id"], False, "boom")
        for day in (2, 3):
            ms.dispatch(store, day=date(2026, 2, day), runner=fail)
            assert ms.is_due(ms.load_schedule(store, s["id"]), date(2026, 2, day + 1))
        ms.dispatch(store, day=date(2026, 2, 4), runner=fail)
        saved = ms.load_schedule(store, s["id"])
        assert saved["last_status"] == "failed"
        assert not ms.is_due(saved, date(2026, 2, 5))

    def test_runner_exception_does_not_block_others(self, store):
        _schedule(store)
        _schedule(store)
        calls = []

        def runner(store_, schedule, scratch, cores):
            calls.append(schedule["id"])
            if len(calls) == 1:
                raise RuntimeError("bad")
            return ms.DispatchResult(schedule["id"], True)

        results = ms.dispatch(store, day=date(2026, 2, 1), runner=runner)
        assert len(calls) == 2
        assert sorted(r.ok for r in results) == [False, True]

    def test_stop_during_run_is_kept(self, store):
        s = _schedule(store)

        def runner(store_, schedule, scratch, cores):
            ms.stop_schedule(store_, ms.load_schedule(store_, schedule["id"]))
            return ms.DispatchResult(schedule["id"], True)

        ms.dispatch(store, day=date(2026, 2, 1), runner=runner)
        assert ms.load_schedule(store, s["id"])["status"] == "stopped"

    def test_dry_run_runs_nothing(self, store):
        _schedule(store)
        runner = lambda *a: pytest.fail("should not run")
        assert ms.dispatch(store, day=date(2026, 2, 1), dry_run=True, runner=runner) == []

    def test_purges_long_stopped(self, store):
        old = _schedule(store, status="stopped", stopped_at="2026-01-01")
        recent = _schedule(store, status="stopped", stopped_at="2026-03-01")
        store.write_text(f"runs/{old['id']}/20251201/x_accessions.txt", "A\n")
        purged = ms.purge_stopped(store, date(2026, 4, 1), after_days=90)
        assert purged == [old["id"]]
        assert ms.load_schedule(store, old["id"]) is None
        assert store.list(f"runs/{old['id']}/") == []
        assert ms.load_schedule(store, recent["id"]) is not None


class TestRunSchedule:
    def test_builds_command_and_uploads(self, store, tmp_path):
        s = _schedule(store, manage_url="https://x.org/?monitor=abc", query_ids=["1"])
        store.write_text(f"runs/{s['id']}/20260101/SC2_accessions.txt", "A1\n")
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        captured = {}

        def fake_run(cmd, capture_output, text):
            captured["cmd"] = cmd
            # Pretend `adapt monitor` produced today's run directory.
            from datetime import datetime
            out = scratch / s["id"] / datetime.now().strftime("%Y%m%d")
            out.mkdir(parents=True)
            (out / "SC2_accessions.txt").write_text("A1\nA2\n")
            assert (scratch / s["id"] / "20260101" / "SC2_accessions.txt").exists()
            return type("P", (), {"returncode": 0, "stdout": "done\n", "stderr": ""})()

        with patch.object(ms.subprocess, "run", fake_run):
            result = ms.run_schedule(store, s, scratch, cores=2)

        assert result.ok
        cmd = captured["cmd"]
        assert cmd[cmd.index("--runid") + 1] == s["id"]
        assert cmd[cmd.index("--email") + 1] == "a@example.com"
        assert cmd[cmd.index("--manage-url") + 1] == "https://x.org/?monitor=abc"
        assert "--csv" in cmd and "--url" not in cmd
        assert cmd[cmd.index("--query-ids") + 1] == "1"
        uploaded = store.list(f"runs/{s['id']}/")
        assert any(k.endswith("monitor.log") for k in uploaded)
        assert len([k for k in uploaded if k.endswith("SC2_accessions.txt")]) == 2

    def test_live_url_preferred(self, store, tmp_path):
        s = _schedule(store, spreadsheet_url="https://docs.google.com/spreadsheets/d/abc/edit")
        captured = {}

        def fake_run(cmd, capture_output, text):
            captured["cmd"] = cmd
            return type("P", (), {"returncode": 1, "stdout": "", "stderr": "err"})()

        with patch.object(ms.subprocess, "run", fake_run):
            result = ms.run_schedule(store, s, tmp_path, cores=1)
        assert not result.ok
        assert "--url" in captured["cmd"] and "--csv" not in captured["cmd"]


def test_get_store_selects_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("PRIMERCAST_MONITOR_BUCKET", raising=False)
    assert isinstance(ms.get_store(tmp_path), ms.LocalStore)
