"""Per-stage diagnostics written beside pipeline outputs.

Stages that finish successfully but produce nothing usable record *why* here, so
the reason survives past the log file and reaches the evaluation report and the
GUI. The GUI discovers these files with ``rglob("*.diagnostics.json")``; the
filename is the stage output with ``.diagnostics.json`` appended, which keeps
sibling stages (``.input``, ``.eval``) from colliding.
"""

import json
from pathlib import Path

# Statuses that mean "ran, but produced no usable result".
EMPTY_STATUSES = ("no_alignments", "no_amplicons", "no_input", "no_candidates")

# Pipeline order, so reasons read cause-first rather than in filename order
# ('.eval' would otherwise sort ahead of the '.input' stage that explains it).
_STAGE_ORDER = ("quick_design", "generate", "prepare_input", "evaluate", "filter")


def diagnostics_path(output_path):
    """Return the diagnostics file belonging to a stage output."""
    return Path(f"{output_path}.diagnostics.json")


def write_diagnostics(output_path, stage, status, reason, counts=None):
    """Record why a stage produced the output it did."""
    data = {"stage": stage, "status": status, "reason": reason}
    if counts:
        data["counts"] = counts
    path = diagnostics_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    return path


def read_diagnostics(directory):
    """Load diagnostics recorded in a directory, sorted by filename.

    Unreadable or malformed files are skipped rather than failing the caller —
    a missing explanation must never break the report that carries it.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    found = []
    for path in sorted(directory.glob("*.diagnostics.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found.append(data)
    return found


def _stage_rank(stage):
    try:
        return _STAGE_ORDER.index(stage)
    except ValueError:
        return len(_STAGE_ORDER)


def collect_reasons(directories):
    """Return reasons from stages that produced no usable result.

    Ordered by pipeline stage so the root cause is read before its downstream
    consequences, and deduplicated so a reason shared by several off-target
    directories is reported once.
    """
    found = []
    for directory in directories:
        for data in read_diagnostics(directory):
            if data.get("status") not in EMPTY_STATUSES and data.get("status") != "error":
                continue
            reason = data.get("reason")
            if reason:
                found.append((_stage_rank(data.get("stage")), reason))

    reasons = []
    for _, reason in sorted(found, key=lambda item: item[0]):
        if reason not in reasons:
            reasons.append(reason)
    return reasons
