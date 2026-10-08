"""Tests for gui.error_report (stdlib-only; no Streamlit, GCS mocked)."""

import json
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

# gui/ lives at the repo root (not under src/), so make it importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import error_report  # noqa: E402
from gui.error_report import (  # noqa: E402
    build_bundle,
    clean_contact_email,
    object_name,
    resolve_input_files,
    snapshot_state,
    submit_report,
)


def _read_bundle(path: Path) -> tuple[dict, set[str]]:
    with tarfile.open(path, "r:gz") as tar:
        names = set(tar.getnames())
        report = json.load(tar.extractfile("report.json"))
    return report, names


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "run1"
    (d / "sub").mkdir(parents=True)
    (d / "virusA_final.csv").write_text("a,b\n1,2\n")
    (d / "pipeline.log").write_text("rule make_MSA:\nError\n")
    (d / "sub" / "index.1.bt2").write_bytes(b"x" * 10)
    return d


@pytest.fixture
def target_seqs(tmp_path):
    ts = tmp_path / "target_seqs"
    (ts / "original").mkdir(parents=True)
    (ts / "msa").mkdir()
    (ts / "original" / "virusA.fa").write_text(">a\nACGT\n")
    (ts / "original" / "virusB.fasta").write_text(">b\nACGT\n")
    (ts / "msa" / "virusA.aln").write_text(">a\nACGT\n")
    (ts / "original" / "unrelated.fa").write_text(">u\nACGT\n")
    return ts


def test_resolve_input_files_picks_named_targets_only(target_seqs, tmp_path):
    files = resolve_input_files(["virusA", "virusB", "virusA", "../etc", "missing"], target_seqs)
    arcnames = sorted(a for _, a in files)
    assert arcnames == [
        "inputs/msa/virusA.aln",
        "inputs/original/virusA.fa",
        "inputs/original/virusB.fasta",
    ]


def test_resolve_input_files_extra_must_be_inside_allowed_dir(target_seqs, tmp_path):
    allowed = tmp_path / "evaluate"
    allowed.mkdir()
    pset = allowed / "pset.fa"
    pset.write_text(">p\nACGT\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")

    files = resolve_input_files([], target_seqs, [str(pset), str(outside), ""], allowed)
    assert [a for _, a in files] == ["inputs/extra/pset.fa"]


def test_build_bundle_contents_and_manifest(run_dir, target_seqs, tmp_path):
    inputs = resolve_input_files(["virusA"], target_seqs)
    out = tmp_path / "bundle.tar.gz"

    included, skipped = build_bundle(out, {"run_id": "run1"}, run_dir, inputs)

    report, names = _read_bundle(out)
    assert "run/virusA_final.csv" in names
    assert "run/pipeline.log" in names
    assert "inputs/original/virusA.fa" in names
    assert "run/sub/index.1.bt2" not in names
    assert (included, skipped) == (4, 1)
    skipped_entries = [m for m in report["manifest"] if "skipped" in m]
    assert [m["path"] for m in skipped_entries] == ["run/sub/index.1.bt2"]
    assert report["run_id"] == "run1"


def test_build_bundle_does_not_follow_symlinks(run_dir, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    (run_dir / "link.txt").symlink_to(secret)

    out = tmp_path / "bundle.tar.gz"
    build_bundle(out, {}, run_dir)
    _, names = _read_bundle(out)
    assert "run/link.txt" not in names


def test_build_bundle_enforces_size_caps(run_dir, tmp_path):
    (run_dir / "big.bin").write_bytes(b"x" * 500)
    out = tmp_path / "bundle.tar.gz"

    build_bundle(out, {}, run_dir, max_total_bytes=10_000, max_file_bytes=100)
    report, names = _read_bundle(out)
    assert "run/big.bin" not in names
    big = next(m for m in report["manifest"] if m["path"] == "run/big.bin")
    assert "larger than" in big["skipped"]

    out2 = tmp_path / "bundle2.tar.gz"
    build_bundle(out2, {}, run_dir, max_total_bytes=30, max_file_bytes=10_000)
    report2, _ = _read_bundle(out2)
    assert any("cap" in m.get("skipped", "") for m in report2["manifest"])


def test_build_bundle_without_run_dir(tmp_path):
    out = tmp_path / "bundle.tar.gz"
    assert build_bundle(out, {"x": 1}, tmp_path / "missing") == (0, 0)
    report, names = _read_bundle(out)
    assert names == {"report.json"}
    assert report["manifest"] == []


def test_snapshot_state_drops_secrets_and_handles_unserializable():
    class Uploaded:
        def __repr__(self):
            return "<UploadedFile>"

    state = {
        "targets": ["virusA"],
        "completed": {"b", "a"},
        "upload": Uploaded(),
        "EMAIL_PASSWORD": "hunter2",
        "api_token": "abc",
        "long": "x" * 20_000,
    }
    snap = snapshot_state(state)
    assert snap["targets"] == ["virusA"]
    assert snap["completed"] == ["a", "b"]
    assert snap["upload"] == "<UploadedFile>"
    assert "EMAIL_PASSWORD" not in snap and "api_token" not in snap
    assert snap["long"].endswith("<truncated>")


@pytest.mark.parametrize(
    "value, expected",
    [("  me@broadinstitute.org ", "me@broadinstitute.org"), ("not-an-email", ""), ("", "")],
)
def test_clean_contact_email(value, expected):
    assert clean_contact_email(value) == expected


def test_object_name():
    when = datetime(2026, 9, 27, tzinfo=timezone.utc)
    assert object_name("staging/my-branch/", "run 1/../x", "abc", when) == (
        "staging/my-branch/20260927/run_1_.._x-abc.tar.gz"
    )
    assert object_name("", "r", "abc", when) == "20260927/r-abc.tar.gz"


def test_submit_report_local_save_and_log(run_dir, tmp_path, capsys):
    result = submit_report(
        report={"context": "run_page", "workflow": "design",
                "pipeline": {"return_code": 1}, "user_message": "broke"},
        run_id="run1",
        run_dir=run_dir,
        input_files=[],
        bucket=None,
        prefix="local",
        local_dir=tmp_path / "reports",
    )
    assert result.local_path is not None and result.local_path.is_file()
    assert result.location == str(result.local_path)
    report, _ = _read_bundle(result.local_path)
    assert report["report_id"] == result.report_id

    log = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert log["event"] == error_report.LOG_EVENT
    assert log["run_id"] == "run1"
    assert log["return_code"] == 1
    assert log["user_message"] == "broke"


def test_submit_report_uploads_to_bucket(run_dir, tmp_path, monkeypatch, capsys):
    uploaded = {}

    def fake_upload(bundle_path, bucket, name):
        uploaded["exists"] = Path(bundle_path).is_file()
        uploaded["name"] = name
        return f"gs://{bucket}/{name}"

    monkeypatch.setattr(error_report, "upload_bundle", fake_upload)
    result = submit_report(
        report={}, run_id="run1", run_dir=run_dir, input_files=[],
        bucket="reports-bucket", prefix="prod", local_dir=tmp_path / "unused",
    )
    assert uploaded["exists"]
    assert uploaded["name"].startswith("prod/")
    assert result.location == f"gs://reports-bucket/{uploaded['name']}"
    assert result.local_path is None
    assert not (tmp_path / "unused").exists()
