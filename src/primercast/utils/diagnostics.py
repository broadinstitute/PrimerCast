"""Per-step filter diagnostics: which filter left a step with no candidates.

The pipeline narrows candidates step by step (generate -> align -> pair ->
score -> filter -> final output). Each filtering step records a *funnel* -- the
number of candidates remaining after every filter it applies -- in
``<step output>.diagnostics.json``. When a step that must produce candidates
ends up with none, it stops the pipeline right there (instead of letting a
later step crash on an empty file) with a message naming the filter that
removed the last candidate and the params that control it.

The GUI and CLI find these files under the run directory with ``explain_run``.

This module depends only on the standard library.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUFFIX = ".diagnostics.json"
# Exit code used when a step stops because a filter removed every candidate.
NO_CANDIDATES_EXIT_CODE = 3

# Human-readable step names, in pipeline order (used to report the earliest
# failing step first).
STAGE_LABELS = {
    "generate": "Primer generation",
    "generate_probe": "Probe generation",
    "quick_design": "Quick design",
    "prepare_input": "Primer binding and pairing",
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
    off-target amplicons is the desired outcome, not a failure.
    """

    stage: str
    target: str = ""
    fatal: bool = True
    unit: str = "candidates"
    steps: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        filter_name: str,
        remaining: int,
        params: dict[str, Any] | None = None,
        breakdown: dict[str, int] | None = None,
        hint: str = "",
    ) -> int:
        """Record how many candidates remain after ``filter_name``.

        ``breakdown`` splits the count (e.g. forward vs reverse primers);
        ``remaining`` should then be the bottleneck (a pair needs both).
        Returns ``remaining`` so calls can be used inline.
        """
        entry: dict[str, Any] = {"filter": filter_name, "remaining": int(remaining)}
        if params:
            entry["params"] = {k: _plain(v) for k, v in params.items()}
        if breakdown:
            entry["breakdown"] = {k: int(v) for k, v in breakdown.items()}
        if hint:
            entry["hint"] = hint
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
        return {
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "target": self.target,
            "fatal": self.fatal,
            "unit": self.unit,
            "empty": self.empty,
            "steps": self.steps,
        }

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


def _plain(value: Any) -> Any:
    """JSON-friendly param value (numpy scalars, integral floats -> int)."""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value
