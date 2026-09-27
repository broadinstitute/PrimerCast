"""Execution state and UI refresh regressions; only harmless Python subprocesses."""

import json
import os
from pathlib import Path
import sys
import time

import pytest

from gui.pipeline_runner import JobProgress, PipelineRun


def make_run(tmp_path, code, **kwargs):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return PipelineRun([sys.executable, "-u", "-c", code], scratch, tmp_path / "results", os.environ,
                       workflow=kwargs.pop("workflow", "evaluate"), **kwargs)


def finish(run):
    run._thread.join(timeout=8)
    assert not run._thread.is_alive(), "Runner did not complete"
    return run.snapshot()


def test_parallel_jobs_complete_only_on_confirmation():
    progress = JobProgress()
    for line in ["localrule align:", "    jobid: 1", "localrule align:", "    jobid: 2",
                 "Finished jobid: 1 (Rule: align)", "Error in rule align:"]:
        progress.feed(line)
    assert progress.jobs["1"]["finished"]
    assert not progress.jobs["2"]["finished"]
    assert progress.rows({}, "failed") == [{"Stage": "Align", "Progress": "1 job(s) finished; failed"}]


def test_rule_transition_does_not_imply_success():
    progress = JobProgress()
    for line in ["rule generate:", "jobid: 1", "rule evaluate:", "jobid: 2"]:
        progress.feed(line)
    assert not any(job["finished"] for job in progress.jobs.values())


def test_silent_run_does_not_block_snapshot(tmp_path):
    # Silent, but a real evaluate run still leaves a report behind.
    report = tmp_path / "results" / "pset.xlsx"
    run = make_run(tmp_path, f"import time, pathlib; time.sleep(2); "
                             f"pathlib.Path({str(report)!r}).write_bytes(b'')")
    run.start()
    before = time.monotonic()
    snapshot = run.snapshot()
    assert time.monotonic() - before < 0.5
    assert snapshot["status"] == "running"
    assert snapshot["log"] == ""
    assert finish(run)["status"] == "completed"


def test_failure_is_persisted_with_log_and_no_false_finished_job(tmp_path):
    run = make_run(tmp_path, "print('localrule evaluate:'); print('jobid: 1'); print('tool failed'); raise SystemExit(2)")
    run.start()
    snapshot = finish(run)
    assert snapshot["status"] == "failed"
    assert "tool failed" in snapshot["log"]
    assert "finished" not in snapshot["rows"][0]["Progress"]
    assert json.loads((run.output_dir / "run_status.json").read_text())["status"] == "failed"
    assert not run.cwd.exists()


def test_launch_failure_resets_state(tmp_path):
    run = make_run(tmp_path, "")
    run.command = [str(tmp_path / "missing-executable")]
    run.start()
    snapshot = finish(run)
    assert snapshot["status"] == "failed"
    assert "Could not execute" in snapshot["reason"]
    assert not run.cwd.exists()


def test_stop_uses_private_process_group(tmp_path):
    run = make_run(tmp_path, "import time; time.sleep(30)")
    run.start()
    deadline = time.monotonic() + 3
    while run._process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert run._process is not None
    assert os.getpgid(run._process.pid) == run._process.pid
    assert os.getpgid(run._process.pid) != os.getpgrp()
    run.stop()
    run.stop()  # Repeated clicks must not signal another process.
    assert finish(run)["status"] == "stopped"


@pytest.mark.parametrize("rows,state", [("", "no_candidates"), ("f,r\n", "completed")])
def test_completed_design_classifies_empty_and_nonempty_tables(tmp_path, rows, state):
    output = tmp_path / "results" / "synthetic_final.csv"
    code = f"from pathlib import Path; Path({str(output)!r}).write_text({'pname_f,pname_r' + chr(10) + rows!r})"
    run = make_run(tmp_path, code, workflow="design")
    run.start()
    assert finish(run)["status"] == state


def test_evaluate_without_a_report_is_not_reported_as_ready(tmp_path):
    run = make_run(tmp_path, "pass")
    run.start()
    snapshot = finish(run)
    assert snapshot["status"] == "failed"
    assert "no evaluation report" in snapshot["reason"]


def test_evaluate_with_an_empty_report_surfaces_the_recorded_cause(tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    (results / "_H1").mkdir()
    (results / "_H1" / "pset.H1.input.diagnostics.json").write_text(json.dumps({
        "stage": "prepare_input", "status": "no_alignments",
        "reason": "0 of 2 primer(s) aligned to H1."}))
    report = results / "pset.xlsx"
    code = f"from pathlib import Path; Path({str(report)!r}).write_bytes(b'')"
    run = make_run(tmp_path, code)
    run.start()
    snapshot = finish(run)
    assert snapshot["status"] == "no_candidates"
    assert "0 of 2 primer(s) aligned to H1." in snapshot["reason"]


def test_dry_run_does_not_claim_designs_exist(tmp_path):
    run = make_run(tmp_path, "", workflow="design", dry_run=True)
    run.start()
    snapshot = finish(run)
    assert snapshot["status"] == "dry_run"
    assert "No new primer designs" in snapshot["reason"]


def test_start_is_idempotent(tmp_path):
    run = make_run(tmp_path, "print('started once')")
    run.start()
    worker = run._thread
    run.start()
    assert run._thread is worker
    assert finish(run)["log"].count("started once") == 1
