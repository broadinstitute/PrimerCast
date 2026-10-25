"""Tests for the GUI's FASTA upload decoding.

Large genomes can only reach the hosted app compressed -- Cloud Run rejects any
request body over 32 MiB with a 413, and HUMAN.fa is 63 MB raw against 18 MB as
.fa.bz2. So every uploader accepts .bz2/.gz and decompresses on save.
"""

import bz2
import gzip
import sys
from pathlib import Path

import pytest

# gui/ lives at the repo root (not under src/), so make it importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import app as gui_app  # noqa: E402
from gui.app import (  # noqa: E402
    UPLOAD_SIZE_LIMIT_MB,
    _decompress_upload,
    _fasta_suffix,
)

PAYLOAD = b">chr1\nACGTACGT\n"


@pytest.mark.parametrize(
    "name,data,expected_stem",
    [
        ("HUMAN.fa.bz2", bz2.compress(PAYLOAD), "HUMAN"),
        ("HUMAN.fa.gz", gzip.compress(PAYLOAD), "HUMAN"),
        ("GRCh38.fasta.gz", gzip.compress(PAYLOAD), "GRCh38"),
        ("GRCh38.fna.bz2", bz2.compress(PAYLOAD), "GRCh38"),
        # A bare .bz2/.gz with no inner FASTA extension still works.
        ("HUMAN.bz2", bz2.compress(PAYLOAD), "HUMAN"),
        ("HUMAN.gz", gzip.compress(PAYLOAD), "HUMAN"),
    ],
)
def test_compressed_uploads_are_expanded(name, data, expected_stem):
    assert _decompress_upload(name, data) == (expected_stem, PAYLOAD)


@pytest.mark.parametrize("name", ["plain.fa", "plain.fasta", "plain.fna"])
def test_plain_fasta_passes_through(name):
    assert _decompress_upload(name, PAYLOAD) == ("plain", PAYLOAD)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("x.fa", ".fa"), ("x.FA", ".fa"), ("x.Fa", ".fa"),
        ("x.FASTA", ".fasta"), ("x.Fna", ".fna"),
        ("x.txt", None), ("x.fa.bz2", None), ("x", None),
    ],
)
def test_fasta_suffix_normalizes_case(name, expected):
    assert _fasta_suffix(name) == expected


@pytest.mark.parametrize(
    "name,data",
    [
        ("HUMAN.FA.BZ2", bz2.compress(PAYLOAD)),
        ("HUMAN.Fa.Gz", gzip.compress(PAYLOAD)),
        ("HUMAN.FNA", PAYLOAD),
        ("HUMAN.Fasta", PAYLOAD),
    ],
)
def test_extension_matching_is_case_insensitive(name, data):
    assert _decompress_upload(name, data) == ("HUMAN", PAYLOAD)


@pytest.mark.parametrize("name", ["notes.txt", "archive.tar", "primers.csv"])
def test_unsupported_extension_is_rejected(name):
    """None lets the caller report the extension rather than saving junk."""
    assert _decompress_upload(name, PAYLOAD) is None


def test_corrupt_archive_raises_for_the_caller_to_report():
    with pytest.raises((OSError, EOFError, ValueError)):
        _decompress_upload("HUMAN.fa.gz", b"not actually gzip")


class _FakeUpload:
    """Stands in for Streamlit's UploadedFile (name/size/getvalue only)."""

    def __init__(self, name, data):
        self.name = name
        self._data = data
        self.size = len(data)

    def getvalue(self):
        return self._data


class _FakeStreamlit:
    """Minimal st. surface used by _save_uploaded_fastas, outside a runtime."""

    def __init__(self):
        self.session_state = {}
        self.errors = []
        self.successes = []

    def error(self, msg):
        self.errors.append(msg)

    def success(self, msg):
        self.successes.append(msg)


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    """Point the GUI's FASTA directory at a tmp dir with a stubbed st."""
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(gui_app, "FASTA_DIR", tmp_path)
    monkeypatch.setattr(gui_app, "st", fake_st)
    return tmp_path, fake_st


def test_save_normalizes_extension_to_lowercase_fa(upload_dir):
    """The Snakefile only ever reads target_seqs/original/<name>.fa."""
    directory, fake_st = upload_dir
    assert gui_app._save_uploaded_fastas([_FakeUpload("Sample.FASTA", PAYLOAD)])
    assert [p.name for p in directory.iterdir()] == ["Sample.fa"]
    assert fake_st.errors == []


def test_save_expands_compressed_uploads(upload_dir):
    directory, _ = upload_dir
    data = bz2.compress(PAYLOAD)
    assert gui_app._save_uploaded_fastas([_FakeUpload("Sample.fa.bz2", data)])
    assert (directory / "Sample.fa").read_bytes() == PAYLOAD


def test_save_is_idempotent_across_reruns(upload_dir):
    """Streamlit replays the same upload each rerun; only the first writes."""
    directory, _ = upload_dir
    upload = _FakeUpload("Sample.fa", PAYLOAD)
    assert gui_app._save_uploaded_fastas([upload]) is True
    # A caller that reruns on True would loop forever if this returned True.
    assert gui_app._save_uploaded_fastas([upload]) is False


def test_save_replaces_a_same_stem_file_under_another_extension(upload_dir):
    directory, _ = upload_dir
    (directory / "Sample.fna").write_bytes(b">old\nAAAA\n")
    gui_app._save_uploaded_fastas([_FakeUpload("Sample.fa", PAYLOAD)])
    assert [p.name for p in directory.iterdir()] == ["Sample.fa"]


def test_save_reports_a_corrupt_archive_without_writing(upload_dir):
    directory, fake_st = upload_dir
    assert gui_app._save_uploaded_fastas(
        [_FakeUpload("Sample.fa.gz", b"not actually gzip")]) is False
    assert list(directory.iterdir()) == []
    assert len(fake_st.errors) == 1


def test_save_reports_an_unsupported_extension(upload_dir):
    directory, fake_st = upload_dir
    assert gui_app._save_uploaded_fastas(
        [_FakeUpload("notes.txt", PAYLOAD)]) is False
    assert list(directory.iterdir()) == []
    assert "not a FASTA file" in fake_st.errors[0]


def test_listing_and_lookup_find_uppercase_extensions(upload_dir):
    """Hand-placed .FA files must still list and resolve."""
    directory, _ = upload_dir
    (directory / "Legacy.FA").write_bytes(PAYLOAD)
    assert gui_app._available_fasta() == ["Legacy"]
    assert gui_app._fasta_path("Legacy") == directory / "Legacy.FA"


def test_fasta_path_prefers_fa_over_other_extensions(upload_dir):
    directory, _ = upload_dir
    for ext in (".fna", ".fa", ".fasta"):
        (directory / f"Sample{ext}").write_bytes(PAYLOAD)
    assert gui_app._fasta_path("Sample") == directory / "Sample.fa"


def test_fasta_path_returns_none_when_absent(upload_dir):
    assert gui_app._fasta_path("Nope") is None


def test_bundled_human_genome_round_trips():
    """The repo ships HUMAN compressed; it must decode to the real FASTA."""
    src = Path(__file__).resolve().parent.parent / "target_seqs/original/HUMAN.fa.bz2"
    if not src.exists():
        pytest.skip("HUMAN.fa.bz2 not present")
    stem, data = _decompress_upload(src.name, src.read_bytes())
    assert stem == "HUMAN"
    assert data.startswith(b">")
    # This is the whole premise of the compressed-upload path: the bundled
    # genome is over the upload ceiling raw and comfortably under it packed.
    cap = UPLOAD_SIZE_LIMIT_MB * 1024 * 1024
    assert src.stat().st_size < cap < len(data)
