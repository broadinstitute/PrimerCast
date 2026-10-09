"""Per-step filter diagnostics: which filter left a step with no candidates.

The pipeline narrows candidates step by step (generate -> align -> pair ->
score -> filter -> final output). Each filtering step records a *funnel* -- the
number of candidates remaining after every filter it applies -- in
``<step output>.diagnostics.json``. When a step that must produce candidates
ends up with none, it stops the pipeline right there (instead of letting a
later step crash on an empty file) with a message naming the filter that
removed the last candidate and the params that control it.

The GUI and CLI find these files under the run directory with ``explain_run``.
``run_funnels`` reads every funnel of a run, including successful ones, so the
GUI and CLI can show where filters narrowed the candidates.
Evaluate mode never stops on these: ``run_warnings`` instead reports on-target
evaluations with zero predicted coverage, and rescue re-evaluations (whose
summary is written to ``<eval>.rescue.json``), as warnings.

This module depends only on the standard library.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUFFIX = ".diagnostics.json"
RESCUE_SUFFIX = ".rescue.json"
# Exit code used when a step stops because a filter removed every candidate.
NO_CANDIDATES_EXIT_CODE = 3
# Quick design's per-batch prepare-input funnels; quick design sums them into its own.
_BATCH_FILE = re.compile(r"batch_\d+\.input" + re.escape(SUFFIX) + "$")

# Human-readable step names, in pipeline order (used to report the earliest
# failing step first).
STAGE_LABELS = {
    "generate": "Primer generation",
    "generate_probe": "Probe generation",
    "quick_design": "Quick design",
    "prepare_input": "Primer binding and pairing",
    "evaluate": "ML evaluation",
    "filter": "Primer pair filtering",
    "build_output": "Final output",
}


def diagnostics_path(output_path: str | Path) -> Path:
    """Diagnostics file belonging to a step output."""
    return Path(f"{output_path}{SUFFIX}")


@dataclass
class StageDiagnostics:
    """Funnel of candidate counts for one pipeline step.

    ``fatal`` marks steps whose output must be non-empty for the run to
    continue (e.g. on-target pairing). Off-target steps set it to False: no
    off-target amplicons is the desired outcome, not a failure. They also set
    ``offtarget``, which keeps them out of ``run_funnels``.
    """

    stage: str
    target: str = ""
    fatal: bool = True
    unit: str = "candidates"
    offtarget: bool = False
    steps: list[dict[str, Any]] = field(default_factory=list)
    # Extra step-specific data saved alongside the funnel (e.g. per-pair coverage).
    details: dict[str, Any] = field(default_factory=dict)

    def record(
        self,
        filter_name: str,
        remaining: int,
        params: dict[str, Any] | None = None,
        breakdown: dict[str, int] | None = None,
        hint: str = "",
        unit: str = "",
        summarize: bool = True,
    ) -> int:
        """Record how many candidates remain after ``filter_name``.

        ``breakdown`` splits the count (e.g. forward vs reverse primers);
        ``remaining`` should then be the bottleneck (a pair needs both).
        ``unit`` marks a step that counts something other than the stage's
        unit (e.g. primers in a primer-pair funnel); counts are only compared
        between steps of the same unit. ``summarize=False`` leaves a step out of
        loss summaries: a deliberate top-N cut, or a repeat of an earlier
        step's result.
        Returns ``remaining`` so calls can be used inline.
        """
        entry: dict[str, Any] = {"filter": filter_name, "remaining": int(remaining)}
        if params:
            entry["params"] = {k: _plain(v) for k, v in params.items()}
        if breakdown:
            entry["breakdown"] = {k: int(v) for k, v in breakdown.items()}
        if hint:
            entry["hint"] = hint
        if unit:
            entry["unit"] = unit
        if not summarize:
            entry["summarize"] = False
        self.steps.append(entry)
        return int(remaining)

    def blocking_step(self) -> tuple[int, dict[str, Any]] | None:
        """``(index, step)`` of the first filter that left zero, if any."""
        for i, step in enumerate(self.steps):
            if step["remaining"] == 0:
                return i, step
        return None

    @property
    def empty(self) -> bool:
        return self.blocking_step() is not None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "target": self.target,
            "fatal": self.fatal,
            "unit": self.unit,
            "empty": self.empty,
            "steps": self.steps,
        }
        if self.offtarget:
            data["offtarget"] = True
        if self.details:
            data["details"] = self.details
        return data

    def write(self, output_path: str | Path) -> Path:
        path = diagnostics_path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path

    def message(self) -> str:
        return format_message(self.to_dict())

    def finish(self, output_path: str | Path) -> None:
        """Write the diagnostics; stop the pipeline if a fatal step is empty.

        Exits with ``NO_CANDIDATES_EXIT_CODE`` and prints the explanation to
        stderr, so it also appears in the Snakemake log next to the rule.
        """
        self.write(output_path)
        if self.fatal and self.empty:
            print(f"\nNO CANDIDATES: {self.message()}\n", file=sys.stderr, flush=True)
            sys.exit(NO_CANDIDATES_EXIT_CODE)


def append_step(
    output_path: str | Path,
    filter_name: str,
    remaining: int,
    hint: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Add a later step to an existing diagnostics file.

    Used when a follow-up step revises a count (rescue re-evaluation can
    recover targets), so ``empty`` follows the new last step rather than the
    first zero. Returns the updated data, or None if there is no file.
    """
    path = diagnostics_path(output_path)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    step: dict[str, Any] = {"filter": filter_name, "remaining": int(remaining)}
    if hint:
        step["hint"] = hint
    data.setdefault("steps", []).append(step)
    data["empty"] = int(remaining) == 0
    if details:
        data.setdefault("details", {}).update(details)
    path.write_text(json.dumps(data, indent=2))
    return data


def format_message(data: dict[str, Any]) -> str:
    """One-paragraph explanation of which filter emptied a step."""
    label = data.get("stage_label") or STAGE_LABELS.get(data.get("stage", ""), "")
    target = data.get("target")
    where = f"{label} ({target})" if target else label
    unit = data.get("unit") or "candidates"
    steps = data.get("steps") or []

    for i, step in enumerate(steps):
        if step.get("remaining") != 0:
            continue
        before = steps[i - 1] if i > 0 else None
        if before is None:
            text = f"{where}: no {unit} to start with at '{step['filter']}'."
        else:
            text = (
                f"{where}: no {unit} left after the '{step['filter']}' filter "
                f"({before['remaining']:,} -> 0)."
            )
        if step.get("breakdown"):
            parts = ", ".join(f"{k} {v:,}" for k, v in step["breakdown"].items())
            text += f" Remaining by type: {parts}."
        if step.get("params"):
            params = ", ".join(f"{k}={v}" for k, v in step["params"].items())
            text += f" Controlled by: {params}."
        if step.get("hint"):
            text += f" {step['hint']}"
        return text
    return f"{where}: {unit} remained after every filter."


@dataclass
class Diagnosis:
    """A step that ran out of candidates, as found in a run directory."""

    stage: str
    stage_label: str
    target: str
    fatal: bool
    message: str
    steps: list[dict[str, Any]]
    path: Path


def explain_run(
    run_dir: str | Path,
    include_non_fatal: bool = False,
    since: float | None = None,
) -> list[Diagnosis]:
    """Find steps in a run that ran out of candidates, earliest step first.

    ``since`` (a Unix timestamp) ignores files left by earlier attempts of a
    re-run. Unreadable files are skipped: a missing explanation must never
    break the page that shows it.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return []
    order = list(STAGE_LABELS)
    found: list[Diagnosis] = []
    for path in sorted(run_dir.rglob(f"*{SUFFIX}")):
        try:
            if since is not None and path.stat().st_mtime < since:
                continue
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("empty"):
            continue
        if not data.get("fatal", True) and not include_non_fatal:
            continue
        found.append(Diagnosis(
            stage=data.get("stage", ""),
            stage_label=data.get("stage_label", ""),
            target=data.get("target", ""),
            fatal=bool(data.get("fatal", True)),
            message=format_message(data),
            steps=data.get("steps") or [],
            path=path,
        ))
    found.sort(key=lambda d: order.index(d.stage) if d.stage in order else len(order))
    return found


@dataclass
class RunWarning:
    """Something a successful run's user should know about (evaluate mode)."""

    kind: str  # "no_coverage" or "rescue"
    target: str
    message: str
    steps: list[dict[str, Any]]
    path: Path


def run_warnings(run_dir: str | Path, since: float | None = None) -> list[RunWarning]:
    """Warnings for a finished run: zero on-target coverage, rescue re-evaluation.

    Zero-coverage warnings come from non-fatal ``evaluate`` diagnostics (fatal
    ones stop the run and are reported by ``explain_run``). ``since`` and error
    handling work as in ``explain_run``.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return []
    found: list[RunWarning] = []
    for pattern, kind in ((f"*{SUFFIX}", "no_coverage"), (f"*{RESCUE_SUFFIX}", "rescue")):
        for path in sorted(run_dir.rglob(pattern)):
            try:
                if since is not None and path.stat().st_mtime < since:
                    continue
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            if kind == "no_coverage":
                if data.get("stage") != "evaluate" or data.get("fatal", True) or not data.get("empty"):
                    continue
                message = format_message(data)
            else:
                message = data.get("message", "")
            if message:
                found.append(RunWarning(kind=kind, target=data.get("target", ""),
                                        message=message, steps=data.get("steps") or [],
                                        path=path))
    return found


def step_baselines(steps: list[dict[str, Any]]) -> list[int | None]:
    """For each step, the count of the previous step in the same unit.

    None when there is no earlier step in that unit, or when the count went up:
    a filter only removes candidates, so a larger count is a different set
    (e.g. quick design's probe steps count only the pairs that have a probe).
    """
    last: dict[str, int] = {}
    baselines: list[int | None] = []
    for step in steps:
        unit = step.get("unit", "")
        remaining = step.get("remaining", 0)
        before = last.get(unit)
        baselines.append(before if before is not None and remaining <= before else None)
        last[unit] = remaining
    return baselines


def funnel_losses(steps: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
    """``(step, count before it)`` for filter steps that removed candidates.

    Ordered by the fraction removed, largest first. Steps recorded with
    ``summarize=False`` are left out.
    """
    losses = [
        (step, before)
        for step, before in zip(steps, step_baselines(steps))
        if before and step.get("summarize", True) and step.get("remaining", 0) < before
    ]
    losses.sort(key=lambda sb: (sb[0]["remaining"] / sb[1], -(sb[1] - sb[0]["remaining"])))
    return losses


def format_losses(data: dict[str, Any], max_items: int = 3) -> str:
    """One line naming the filters that removed the most candidates ("" if none)."""
    losses = funnel_losses(data.get("steps") or [])[:max_items]
    if not losses:
        return ""
    label = data.get("stage_label") or STAGE_LABELS.get(data.get("stage", ""), "")
    target = data.get("target")
    where = f"{label} ({target})" if target else label
    parts = [
        f"{step['filter']} {before:,} -> {step['remaining']:,} "
        f"(-{(before - step['remaining']) / before:.0%})"
        for step, before in losses
    ]
    return f"{where}: " + "; ".join(parts)


@dataclass
class Funnel:
    """One step's funnel from a run directory, for display after any run."""

    stage: str
    stage_label: str
    target: str
    unit: str
    steps: list[dict[str, Any]]
    summary: str  # biggest losses, "" if no filter removed anything
    path: Path


def run_funnels(run_dir: str | Path, since: float | None = None) -> list[Funnel]:
    """Every on-target funnel in a run, earliest step first.

    Unlike ``explain_run`` this includes steps that kept candidates, so a
    successful run can still show which filters narrowed the results.
    Off-target funnels and quick design's per-batch funnels are skipped.
    ``since`` and error handling work as in ``explain_run``.
    """
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        return []
    order = list(STAGE_LABELS)
    found: list[Funnel] = []
    for path in sorted(run_dir.rglob(f"*{SUFFIX}")):
        if _BATCH_FILE.search(path.name):
            continue
        try:
            if since is not None and path.stat().st_mtime < since:
                continue
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("offtarget") or not data.get("steps"):
            continue
        stage = data.get("stage", "")
        found.append(Funnel(
            stage=stage,
            stage_label=data.get("stage_label") or STAGE_LABELS.get(stage, stage),
            target=data.get("target", ""),
            unit=data.get("unit") or "candidates",
            steps=data["steps"],
            summary=format_losses(data),
            path=path,
        ))
    found.sort(key=lambda f: (order.index(f.stage) if f.stage in order else len(order),
                              f.target))
    return found


def _plain(value: Any) -> Any:
    """JSON-friendly param value (numpy scalars, integral floats -> int)."""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
