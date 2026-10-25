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

from gui.app import UPLOAD_SIZE_LIMIT_MB, _decompress_upload  # noqa: E402

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


def test_extension_matching_is_case_insensitive():
    assert _decompress_upload("HUMAN.FA.BZ2", bz2.compress(PAYLOAD)) == (
        "HUMAN", PAYLOAD)
    assert _decompress_upload("Plain.FASTA", PAYLOAD) == ("Plain", PAYLOAD)


@pytest.mark.parametrize("name", ["notes.txt", "archive.tar", "primers.csv"])
def test_unsupported_extension_is_rejected(name):
    """None lets the caller report the extension rather than saving junk."""
    assert _decompress_upload(name, PAYLOAD) is None


def test_corrupt_archive_raises_for_the_caller_to_report():
    with pytest.raises((OSError, EOFError, ValueError)):
        _decompress_upload("HUMAN.fa.gz", b"not actually gzip")


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
