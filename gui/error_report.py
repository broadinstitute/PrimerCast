"""Bundle a pipeline run into an error report and send it to the team.

The Streamlit app's "Report a problem" button calls ``submit_report``, which:

1. builds ``<run_id>-<report_id>.tar.gz`` containing ``report.json`` (the run
   state, a snapshot of the session, the user's message and a manifest), every
   file under ``runs/<run_id>/`` and the input FASTAs/MSAs the run used;
2. uploads it to the GCS bucket named by ``QPRIMER_REPORT_BUCKET`` (under
   ``QPRIMER_REPORT_PREFIX``), or, when no bucket is configured (local dev),
   saves it under a local directory so the user can download it instead;
3. prints one structured JSON log line (``event = "error_report_submitted"``).
   On Cloud Run that becomes a Cloud Logging entry, and a log-based alert (see
   terraform/error_reports.tf) emails the team.

Only files inside the run directory and the named inputs are ever bundled, and
size caps keep a public user from turning this into a bulk-upload endpoint.

Apart from the optional ``google-cloud-storage`` import in ``upload_bundle``,
this module depends only on the standard library so it can be unit tested
without importing Streamlit / torch.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

LOG_EVENT = "error_report_submitted"

MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_MESSAGE_CHARS = 5000
MAX_STATE_VALUE_CHARS = 10_000
# Large, regenerable artifacts: listed in the manifest but not bundled.
SKIP_SUFFIXES = (".bt2", ".bt2l")
FASTA_SUFFIXES = (".fa", ".fasta", ".fna")
_SECRET_KEY_RE = re.compile(r"password|secret|token|credential", re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class ReportResult:
    report_id: str
    location: str  # gs:// URI, or local file path when no bucket is configured
    local_path: Path | None  # set only for local saves (offered as a download)
    included_files: int
    skipped_files: int


def new_report_id() -> str:
    return uuid.uuid4().hex[:12]


def clean_contact_email(value: str) -> str:
    """Return a trimmed email address, or "" if blank / not email-shaped."""
    value = (value or "").strip()[:254]
    return value if _EMAIL_RE.match(value) else ""


def snapshot_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """JSON-safe copy of the Streamlit session state.

    Secret-looking keys are dropped; values that aren't JSON-serializable (e.g.
    UploadedFile objects) are replaced by a truncated ``repr``.
    """
    snapshot: dict[str, Any] = {}
    for key in sorted(state.keys(), key=str):
        key_str = str(key)
        if _SECRET_KEY_RE.search(key_str):
            continue
        value = state[key]
        if isinstance(value, (set, frozenset)):
            value = sorted(value, key=str)
        try:
            encoded = json.dumps(value, default=_json_default)
        except (TypeError, ValueError):
            snapshot[key_str] = repr(value)[:MAX_STATE_VALUE_CHARS]
            continue
        if len(encoded) > MAX_STATE_VALUE_CHARS:
            snapshot[key_str] = encoded[:MAX_STATE_VALUE_CHARS] + "...<truncated>"
        else:
            snapshot[key_str] = json.loads(encoded)
    return snapshot


def resolve_input_files(
    target_names: Iterable[str],
    target_seqs_dir: Path,
    extra_files: Iterable[str | Path] = (),
    extra_allowed_dir: Path | None = None,
) -> list[tuple[Path, str]]:
    """Map a run's target names (and e.g. an uploaded primer set) to files.

    Returns ``(path, archive_name)`` pairs. Target names are file stems under
    ``target_seqs/original`` (FASTA) and ``target_seqs/msa`` (``.aln``); names
    containing path separators are ignored. ``extra_files`` are only included
    when they resolve inside ``extra_allowed_dir``.
    """
    found: dict[Path, str] = {}
    for name in dict.fromkeys(target_names):
        if not name or "/" in name or "\\" in name or name.startswith("."):
            continue
        for suffix in FASTA_SUFFIXES:
            path = target_seqs_dir / "original" / f"{name}{suffix}"
            if path.is_file():
                found.setdefault(path, f"inputs/original/{path.name}")
        aln = target_seqs_dir / "msa" / f"{name}.aln"
        if aln.is_file():
            found.setdefault(aln, f"inputs/msa/{aln.name}")

    if extra_allowed_dir is not None:
        allowed = extra_allowed_dir.resolve()
        for extra in extra_files:
            if not extra:
                continue
            path = Path(extra).resolve()
            if path.is_file() and path.is_relative_to(allowed):
                found.setdefault(path, f"inputs/extra/{path.name}")
    return list(found.items())


def build_bundle(
    out_path: Path,
    report: dict[str, Any],
    run_dir: Path | None,
    input_files: Iterable[tuple[Path, str]] = (),
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> tuple[int, int]:
    """Write the ``.tar.gz`` bundle; return ``(included, skipped)`` file counts.

    ``report`` is written as ``report.json`` with a ``manifest`` key added that
    lists every file considered and why any were skipped.
    """
    candidates: list[tuple[Path, str]] = []
    if run_dir is not None and run_dir.is_dir():
        for path in sorted(run_dir.rglob("*")):
            # Never follow symlinks: only real files inside the run dir.
            if path.is_symlink() or not path.is_file():
                continue
            candidates.append((path, f"run/{path.relative_to(run_dir).as_posix()}"))
    candidates.extend(input_files)

    manifest: list[dict[str, Any]] = []
    total = 0
    included = skipped = 0
    to_add: list[tuple[Path, str]] = []
    for path, arcname in candidates:
        size = path.stat().st_size
        entry: dict[str, Any] = {"path": arcname, "bytes": size}
        if path.suffix in SKIP_SUFFIXES:
            entry["skipped"] = "regenerable index file"
        elif size > max_file_bytes:
            entry["skipped"] = f"file larger than {max_file_bytes} bytes"
        elif total + size > max_total_bytes:
            entry["skipped"] = f"bundle size cap ({max_total_bytes} bytes) reached"
        else:
            total += size
            to_add.append((path, arcname))
        if "skipped" in entry:
            skipped += 1
        else:
            included += 1
        manifest.append(entry)

    report = {**report, "manifest": manifest}
    report_bytes = json.dumps(report, indent=2, default=_json_default).encode()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz") as tar:
        info = tarfile.TarInfo("report.json")
        info.size = len(report_bytes)
        info.mtime = int(datetime.now(timezone.utc).timestamp())
        tar.addfile(info, io.BytesIO(report_bytes))
        for path, arcname in to_add:
            tar.add(path, arcname=arcname, recursive=False)
    return included, skipped


def object_name(prefix: str, run_id: str, report_id: str, when: datetime) -> str:
    safe_run = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)[:80] or "run"
    parts = [p for p in prefix.strip("/").split("/") if p]
    parts += [when.strftime("%Y%m%d"), f"{safe_run}-{report_id}.tar.gz"]
    return "/".join(parts)


def upload_bundle(bundle_path: Path, bucket: str, name: str) -> str:
    """Upload to GCS using the ambient credentials (Cloud Run runtime SA)."""
    from google.cloud import storage

    blob = storage.Client().bucket(bucket).blob(name)
    blob.upload_from_filename(str(bundle_path), content_type="application/gzip")
    return f"gs://{bucket}/{name}"


def log_submission(fields: Mapping[str, Any]) -> None:
    """Emit the structured log line the Cloud Logging alert matches on."""
    entry = {
        "severity": "WARNING",
        "message": f"Error report submitted for run {fields.get('run_id', '')}",
        "event": LOG_EVENT,
        **fields,
    }
    print(json.dumps(entry, default=_json_default), flush=True)


def submit_report(
    *,
    report: dict[str, Any],
    run_id: str,
    run_dir: Path | None,
    input_files: Iterable[tuple[Path, str]],
    bucket: str | None,
    prefix: str,
    local_dir: Path,
) -> ReportResult:
    """Build the bundle, upload or save it, and log the submission."""
    report_id = report.get("report_id") or new_report_id()
    report = {**report, "report_id": report_id}
    now = datetime.now(timezone.utc)
    name = object_name(prefix, run_id, report_id, now)

    if bucket:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / Path(name).name
            included, skipped = build_bundle(bundle, report, run_dir, input_files)
            location = upload_bundle(bundle, bucket, name)
        local_path = None
    else:
        local_path = local_dir / name
        included, skipped = build_bundle(local_path, report, run_dir, input_files)
        location = str(local_path)

    log_submission({
        "report_id": report_id,
        "run_id": run_id,
        "location": location,
        "context": report.get("context", ""),
        "workflow": report.get("workflow", ""),
        "return_code": report.get("pipeline", {}).get("return_code"),
        "contact_email": report.get("contact_email", ""),
        "user_message": (report.get("user_message") or "")[:500],
    })
    return ReportResult(report_id, location, local_path, included, skipped)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"{type(obj).__name__} is not JSON serializable")

