"""Persistent monitor schedules and the daily dispatcher.

Schedules live in a store: a GCS bucket on Cloud Run (``PRIMERCAST_MONITOR_BUCKET``
under ``PRIMERCAST_MONITOR_PREFIX``), or a local directory otherwise. Layout under
the store root::

    schedules/<id>.json        schedule config + run status
    runs/<id>/<YYYYMMDD>/...   per-run outputs (accession lists, reports, log)

``adapt monitor-dispatch`` runs once a day (a Cloud Run Job triggered by Cloud
Scheduler on Cloud Run; a cron line locally). For every due schedule it pulls the
schedule's earlier accession lists, runs ``adapt monitor``, uploads the new run
directory and records the result. The accession lists are what make monitoring
incremental ("new" = not in the previous fetch), so they must persist between runs.

There are no user accounts: each schedule carries a random token, and the manage
link ``<public url>/?monitor=<id>.<token>`` (shown on creation and included in
every report email) is the only way to view or stop it.

Apart from the optional ``google-cloud-storage`` import in ``GCSStore``, this
module depends only on the standard library.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

FREQUENCIES = ("biweekly", "monthly", "quarterly")
# Run history entries kept per schedule.
MAX_HISTORY = 24
# Daily attempts per scheduled date before a failing run waits for the next date.
MAX_ATTEMPTS = 3
DEFAULT_MAX_ACTIVE = int(os.environ.get("PRIMERCAST_MONITOR_MAX_SCHEDULES", "50"))
DEFAULT_PURGE_AFTER_DAYS = 90
MAX_RECIPIENTS = 10
_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
_ID_RE = re.compile(r"^[0-9a-f]{12}$")


class ScheduleLimitError(Exception):
    """Raised when creating a schedule would exceed the active-schedule cap."""


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------

class LocalStore:
    """Store backed by a local directory (local dev / CLI)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def __repr__(self) -> str:
        return f"LocalStore({self.root})"

    def _path(self, key: str) -> Path:
        return self.root / key

    def read_text(self, key: str) -> str | None:
        path = self._path(key)
        return path.read_text() if path.is_file() else None

    def write_text(self, key: str, text: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix)
        if not base.is_dir():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file())

    def download(self, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self._path(key), dest)

    def upload(self, src: Path, key: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, path)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> None:
        shutil.rmtree(self._path(prefix), ignore_errors=True)


class GCSStore:
    """Store backed by a GCS bucket, with every key under ``prefix``."""

    def __init__(self, bucket: str, prefix: str = ""):
        from google.cloud import storage

        self.bucket_name = bucket
        self.prefix = prefix.strip("/")
        self._bucket = storage.Client().bucket(bucket)

    def __repr__(self) -> str:
        return f"GCSStore(gs://{self.bucket_name}/{self.prefix})"

    def _name(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def read_text(self, key: str) -> str | None:
        from google.api_core.exceptions import NotFound

        try:
            return self._bucket.blob(self._name(key)).download_as_text()
        except NotFound:
            return None

    def write_text(self, key: str, text: str) -> None:
        self._bucket.blob(self._name(key)).upload_from_string(text)

    def list(self, prefix: str) -> list[str]:
        strip = len(self.prefix) + 1 if self.prefix else 0
        return sorted(
            blob.name[strip:]
            for blob in self._bucket.client.list_blobs(self._bucket, prefix=self._name(prefix))
        )

    def download(self, key: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._bucket.blob(self._name(key)).download_to_filename(str(dest))

    def upload(self, src: Path, key: str) -> None:
        self._bucket.blob(self._name(key)).upload_from_filename(str(src))

    def delete(self, key: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self._bucket.blob(self._name(key)).delete()
        except NotFound:
            pass

    def delete_prefix(self, prefix: str) -> None:
        for key in self.list(prefix):
            self.delete(key)


def get_store(local_root: Path):
    """GCS store if ``PRIMERCAST_MONITOR_BUCKET`` is set, else a local one at ``local_root``."""
    bucket = os.environ.get("PRIMERCAST_MONITOR_BUCKET", "").strip()
    if bucket:
        return GCSStore(bucket, os.environ.get("PRIMERCAST_MONITOR_PREFIX", "").strip())
    return LocalStore(local_root)


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

def today() -> date:
    """Today's date in the monitor time zone (matches the Cloud Scheduler zone)."""
    tz = os.environ.get("PRIMERCAST_MONITOR_TZ", "America/New_York")
    return datetime.now(ZoneInfo(tz)).date()


def last_occurrence(frequency: str, day: date) -> date:
    """Most recent scheduled run date on or before ``day``.

    biweekly = 1st and 15th of each month, monthly = 1st, quarterly = 1st of
    Jan/Apr/Jul/Oct (the same calendar the cron schedules used).
    """
    if frequency == "biweekly":
        return day.replace(day=15) if day.day >= 15 else day.replace(day=1)
    if frequency == "quarterly":
        return date(day.year, day.month - (day.month - 1) % 3, 1)
    return day.replace(day=1)


def is_due(schedule: dict, day: date) -> bool:
    """True if the schedule is active and has missed a scheduled date.

    The baseline is the last recorded run, or the creation date (the GUI runs the
    monitor once right away), so a missed day is caught up on the next dispatch.
    """
    if schedule.get("status") != "active":
        return False
    baseline = date.fromisoformat(schedule.get("last_run") or schedule["created"])
    return last_occurrence(schedule["frequency"], day) > baseline


def clean_recipients(value: str) -> list[str]:
    """Split a comma-separated recipient string, keeping email-shaped addresses."""
    found = [e.strip() for e in (value or "").split(",") if e.strip()]
    return [e for e in found if _EMAIL_RE.match(e)][:MAX_RECIPIENTS]


def new_schedule(
    *,
    targets: list[str],
    primer_sets: list[str],
    frequency: str,
    recipients: list[str],
    params_txt: str,
    csv_text: str,
    spreadsheet_url: str = "",
    query_ids: list[str] | None = None,
    created: date | None = None,
) -> dict:
    """Build a new active schedule (not yet saved)."""
    if frequency not in FREQUENCIES:
        raise ValueError(f"Unknown frequency: {frequency}")
    return {
        "id": uuid.uuid4().hex[:12],
        "token": secrets.token_urlsafe(18),
        "status": "active",
        "created": (created or today()).isoformat(),
        "frequency": frequency,
        "recipients": recipients,
        "targets": targets,
        "primer_sets": primer_sets,
        "spreadsheet_url": spreadsheet_url,
        "query_ids": query_ids or [],
        "csv_text": csv_text,
        "params_txt": params_txt,
        "manage_url": "",
        "last_run": None,
        "last_status": None,
        "stopped_at": None,
        "history": [],
    }


def manage_url(base_url: str, schedule: dict) -> str:
    return f"{base_url.rstrip('/')}/?monitor={schedule['id']}.{schedule['token']}"


def _key(schedule_id: str) -> str:
    return f"schedules/{schedule_id}.json"


def save_schedule(store, schedule: dict) -> None:
    store.write_text(_key(schedule["id"]), json.dumps(schedule, indent=2))


def load_schedule(store, schedule_id: str) -> dict | None:
    if not _ID_RE.match(schedule_id or ""):
        return None
    text = store.read_text(_key(schedule_id))
    return json.loads(text) if text else None


def list_schedules(store) -> list[dict]:
    schedules = []
    for key in store.list("schedules/"):
        if key.endswith(".json"):
            text = store.read_text(key)
            if text:
                schedules.append(json.loads(text))
    return schedules


def create_schedule(store, schedule: dict, max_active: int = DEFAULT_MAX_ACTIVE) -> None:
    """Save a new schedule, refusing once ``max_active`` schedules are active."""
    active = sum(1 for s in list_schedules(store) if s.get("status") == "active")
    if active >= max_active:
        raise ScheduleLimitError(
            f"The maximum number of active monitors ({max_active}) has been reached."
        )
    save_schedule(store, schedule)


def find_by_ref(store, ref: str) -> dict | None:
    """Resolve a manage-link reference ``<id>.<token>`` to its schedule."""
    schedule_id, _, token = (ref or "").partition(".")
    schedule = load_schedule(store, schedule_id)
    if schedule and token and hmac.compare_digest(schedule["token"], token):
        return schedule
    return None


def stop_schedule(store, schedule: dict) -> None:
    schedule["status"] = "stopped"
    schedule["stopped_at"] = today().isoformat()
    save_schedule(store, schedule)


# ---------------------------------------------------------------------------
# Run data
# ---------------------------------------------------------------------------

def upload_run_dir(store, schedule_id: str, date_dir: Path) -> int:
    """Upload the top-level files of a monitor date directory; returns the count.

    Subdirectories (the per-target evaluate working dirs, with snakemake
    intermediates and bowtie2 indexes) are scratch and not kept.
    """
    count = 0
    for path in sorted(Path(date_dir).iterdir()):
        if path.is_file():
            store.upload(path, f"runs/{schedule_id}/{date_dir.name}/{path.name}")
            count += 1
    return count


def pull_history(store, schedule_id: str, work_dir: Path) -> int:
    """Download earlier accession lists into ``work_dir/<YYYYMMDD>/``; returns the count."""
    prefix = f"runs/{schedule_id}/"
    count = 0
    for key in store.list(prefix):
        rel = key[len(prefix):]
        if rel.endswith("_accessions.txt") and rel.count("/") == 1:
            store.download(key, Path(work_dir) / rel)
            count += 1
    return count


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

@dataclass
class DispatchResult:
    schedule_id: str
    ok: bool
    detail: str = ""


def _adapt_command() -> list[str]:
    adapt = shutil.which("adapt")
    return [adapt] if adapt else [sys.executable, "-m", "primercast.adapt_cli"]


def run_schedule(store, schedule: dict, scratch: Path, cores: int) -> DispatchResult:
    """Run one schedule's monitor in ``scratch`` and upload its outputs."""
    sid = schedule["id"]
    work_dir = scratch / sid
    work_dir.mkdir(parents=True, exist_ok=True)
    pull_history(store, sid, work_dir)

    params = scratch / f"{sid}.params.txt"
    params.write_text(schedule["params_txt"])
    cmd = _adapt_command() + [
        "monitor",
        "--params", str(params),
        "--workdir", str(scratch),
        "--runid", sid,
        "--cores", str(cores),
        "--email", ",".join(schedule["recipients"]),
    ]
    if schedule.get("manage_url"):
        cmd += ["--manage-url", schedule["manage_url"]]
    if schedule.get("spreadsheet_url"):
        # Live sheet: edits (new targets/primers) are picked up on the next run.
        cmd += ["--url", schedule["spreadsheet_url"]]
    else:
        sheet = scratch / f"{sid}.mastersheet.csv"
        sheet.write_text(schedule["csv_text"], encoding="utf-8")
        cmd += ["--csv", str(sheet)]
    if schedule.get("query_ids"):
        cmd += ["--query-ids", *schedule["query_ids"]]

    print(f"[{sid}] {' '.join(cmd[:2])} ...", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    log = proc.stdout + ("\n--- stderr ---\n" + proc.stderr if proc.stderr else "")
    print(log, flush=True)

    # Same date stamp `adapt monitor` gives its run directory.
    date_dir = work_dir / datetime.now().strftime("%Y%m%d")
    if date_dir.is_dir():
        (date_dir / "monitor.log").write_text(log)
        upload_run_dir(store, sid, date_dir)

    if proc.returncode != 0:
        return DispatchResult(sid, False, f"adapt monitor exited with {proc.returncode}")
    return DispatchResult(sid, True)


def _record(store, schedule_id: str, day: date, result: DispatchResult) -> None:
    # Re-read so a stop made while the monitor ran isn't overwritten.
    schedule = load_schedule(store, schedule_id)
    if schedule is None:
        return
    status = "ok" if result.ok else "failed"
    schedule["last_status"] = status
    schedule["history"] = (schedule.get("history", []) + [
        {"date": day.isoformat(), "status": status, "detail": result.detail}
    ])[-MAX_HISTORY:]
    # A failed run stays due (retried at the next daily dispatch) up to MAX_ATTEMPTS.
    since = last_occurrence(schedule["frequency"], day).isoformat()
    failures = sum(1 for h in schedule["history"]
                   if h["status"] == "failed" and h["date"] >= since)
    if result.ok or failures >= MAX_ATTEMPTS:
        schedule["last_run"] = day.isoformat()
    save_schedule(store, schedule)


def purge_stopped(store, day: date, after_days: int = DEFAULT_PURGE_AFTER_DAYS) -> list[str]:
    """Delete schedules (and their run data) stopped more than ``after_days`` ago."""
    purged = []
    for schedule in list_schedules(store):
        stopped = schedule.get("stopped_at")
        if schedule.get("status") == "stopped" and stopped and \
                date.fromisoformat(stopped) + timedelta(days=after_days) <= day:
            store.delete_prefix(f"runs/{schedule['id']}/")
            store.delete(_key(schedule["id"]))
            purged.append(schedule["id"])
    return purged


def dispatch(
    store,
    day: date | None = None,
    cores: int = 1,
    purge_after_days: int = DEFAULT_PURGE_AFTER_DAYS,
    dry_run: bool = False,
    runner: Callable[..., DispatchResult] = run_schedule,
) -> list[DispatchResult]:
    """Run every due schedule, then purge long-stopped ones."""
    day = day or today()
    due = [s for s in list_schedules(store) if is_due(s, day)]
    print(f"{day}: {len(due)} due schedule(s) in {store!r}", flush=True)

    results = []
    if dry_run:
        for s in due:
            print(f"  would run {s['id']} ({s['frequency']}, targets: {', '.join(s['targets'])})")
        return results

    for schedule in due:
        with tempfile.TemporaryDirectory(prefix=f"monitor-{schedule['id']}-") as scratch:
            try:
                result = runner(store, schedule, Path(scratch), cores)
            except Exception as e:  # one bad schedule must not block the rest
                result = DispatchResult(schedule["id"], False, f"{type(e).__name__}: {e}")
        print(f"[{result.schedule_id}] {'ok' if result.ok else 'FAILED'} {result.detail}",
              flush=True)
        _record(store, schedule["id"], day, result)
        results.append(result)

    for sid in purge_stopped(store, day, purge_after_days):
        print(f"Purged stopped schedule {sid}", flush=True)
    return results
