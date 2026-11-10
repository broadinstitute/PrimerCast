"""Fetch virus sequences from Pathoplexus via its LAPIS API.

Pathoplexus (https://pathoplexus.org) is an open sequence database. Its query
layer is LAPIS, one base URL per organism, needing no authentication::

    https://lapis.pathoplexus.org/{organism}/sample/{endpoint}

Unlike ``gget virus`` this is plain HTTP, so there is no subprocess and no conda
package. See ``docs/pathoplexus_api_spec.md`` for the full API notes.

The fetch is deliberately two-phase: an ID-only pull of everything matching the
filters, then sequence and metadata downloads for only the accessions we have not
seen before. Pathoplexus's one range-filterable release date
(``earliestReleaseDate``) is the date a sequence first went public *anywhere* --
1993 for GenBank-mirrored records -- not the date it reached Pathoplexus, so
narrowing the window by date would silently miss newly ingested old sequences.
Diffing accessions sidesteps that, and costs one cheap ID-only request.
"""

import csv
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LAPIS_BASE = "https://lapis.pathoplexus.org"

# Public page for one sequence. Required next to any displayed restricted record;
# the version-less form 302-redirects, so always emit the full accessionVersion.
SEQUENCE_PAGE = "https://pathoplexus.org/seq/{}"

# Organism path segment -> nucleotide segment names. Verified 2026-11-03;
# ``referenceGenome`` is authoritative if this ever drifts. "main" means
# unsegmented. Dengue's names are serotypes, not genome segments: a record still
# carries one sequence, in whichever serotype slot it matched.
ORGANISMS: dict[str, tuple[str, ...]] = {
    "andv": ("L", "M", "S"),
    "cchf": ("L", "M", "S"),
    "chikungunya": ("main",),
    "dengue": ("DENV-1", "DENV-2", "DENV-3", "DENV-4"),
    "ebola-bdbv": ("main",),
    "ebola-sudan": ("main",),
    "ebola-zaire": ("main",),
    "hmpv": ("main",),
    "marburg": ("main",),
    "measles": ("main",),
    "mpox": ("main",),
    "rsv-a": ("main",),
    "rsv-b": ("main",),
    "west-nile": ("main",),
    "yellow-fever": ("main",),
    "zika": ("main",),
}

# Organism -> display name, for pickers and reports.
ORGANISM_NAMES = {
    "andv": "Andes virus (hantavirus)",
    "cchf": "Crimean-Congo hemorrhagic fever virus",
    "chikungunya": "Chikungunya virus",
    "dengue": "Dengue virus",
    "ebola-bdbv": "Bundibugyo ebolavirus",
    "ebola-sudan": "Sudan ebolavirus",
    "ebola-zaire": "Zaire ebolavirus",
    "hmpv": "Human metapneumovirus",
    "marburg": "Marburg virus",
    "measles": "Measles virus",
    "mpox": "Mpox virus",
    "rsv-a": "Respiratory syncytial virus A",
    "rsv-b": "Respiratory syncytial virus B",
    "west-nile": "West Nile virus",
    "yellow-fever": "Yellow fever virus",
    "zika": "Zika virus",
}

# Organisms whose genome really is multi-segment. LAPIS splits their per-segment
# metadata into one field per segment (``length_L``, ``insdcAccessionBase_M``),
# where every other organism keeps the plain name -- including dengue, whose
# "segments" are serotype slots on what is still a single sequence.
SEGMENTED_METADATA = frozenset({"andv", "cchf"})

# Field names that take a ``_{segment}`` suffix on those organisms.
PER_SEGMENT_FIELDS = frozenset({
    "length", "totalAmbiguousNucs", "insdcAccessionBase", "insdcAccessionFull",
    "insdcVersion", "completeness", "lineage", "ncbiUpdateDate",
})

# Mastersheet column -> LAPIS filter key.
#
# Collection dates go through LAPIS's derived range fields, because
# ``sampleCollectionDate`` is a string holding whatever precision the submitter
# gave ("1976", "2002-12") and so has no range filters of its own. The pairing
# below is the permissive reading -- the record *might* satisfy the bound. The
# strict reading (Lower for min, Upper for max) would silently discard every
# year-only record, and those are disproportionately the old reference isolates
# a primer panel most wants to cover.
FILTER_MAP = {
    "min_release_date": "earliestReleaseDateFrom",
    "max_release_date": "earliestReleaseDateTo",
    "min_collection_date": "sampleCollectionDateRangeUpperFrom",
    "max_collection_date": "sampleCollectionDateRangeLowerTo",
    "min_seq_length": "lengthFrom",
    "max_seq_length": "lengthTo",
    "max_ambiguous_chars": "totalAmbiguousNucsTo",
    "geographic_location": "geoLocCountry",
    "host": "hostNameScientific",
}

# String filters LAPIS matches exactly and case-sensitively ("uganda" and
# "human" both match nothing). A sheet cell may list several, comma-separated;
# repeated values are OR'd.
EXACT_MATCH_FILTERS = frozenset({"geoLocCountry", "hostNameScientific"})

# gget-only columns with no Pathoplexus equivalent. Rejected rather than dropped
# silently: a row asking for RefSeq-only would otherwise quietly return everything.
UNSUPPORTED_FILTERS = frozenset({
    "refseq_only", "annotated", "lab_passaged", "is_sars_cov2",
    "is_alphainfluenza", "genbank_metadata", "proteins_complete",
    "nuc_completeness", "lineage", "submitter_country", "has_proteins",
    "min_gene_count", "max_gene_count", "min_protein_count", "max_protein_count",
    "min_mature_peptide_count", "max_mature_peptide_count",
})

# Metadata pulled for new sequences. The three dataUseTerms columns are not
# optional: the restricted-data terms require them to be kept intact when the
# data is passed on.
METADATA_FIELDS = [
    "accessionVersion", "length", "geoLocCountry", "geoLocAdmin1",
    "sampleCollectionDate", "earliestReleaseDate", "releasedDate",
    "hostNameScientific", "insdcAccessionBase", "versionStatus",
    "dataUseTerms", "dataUseTermsRestrictedUntil", "dataUseTermsUrl",
]

# LAPIS name -> column name the monitor's report table expects.
COLUMN_RENAMES = {
    "accessionVersion": "accession",
    "length": "Length",
    "geoLocCountry": "Geographic Region",
    "earliestReleaseDate": "Release date",
}

# Accessions per POST body, to keep requests a sane size.
_BATCH = 500


class PathoplexusError(RuntimeError):
    """A Pathoplexus request failed, or was built wrong."""


def _endpoint(organism: str, path: str) -> str:
    return f"{LAPIS_BASE}/{organism}/sample/{path}"


def _request(url: str, body: dict | None = None, timeout: int = 300) -> str:
    """GET ``url``, or POST ``body`` to it, returning the response text.

    LAPIS answers an unknown filter key with a 400 whose payload lists every
    valid key; surface that rather than a bare HTTP error.
    """
    req = urllib.request.Request(url)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail)["error"]["detail"]
        except Exception:
            pass
        raise PathoplexusError(f"Pathoplexus returned HTTP {e.code} for {url}\n{detail[:800]}")
    except urllib.error.URLError as e:
        raise PathoplexusError(f"Could not reach Pathoplexus ({url}): {e.reason}")


def resolve_segment(organism: str, segment: str | None) -> str | None:
    """Validate ``organism``/``segment``, returning the segment to request.

    None for unsegmented organisms. Segmented ones must name a segment: their
    per-segment FASTA headers are the bare accession, so merging segments would
    produce duplicate IDs.
    """
    if organism not in ORGANISMS:
        raise PathoplexusError(
            f"Unknown Pathoplexus organism {organism!r}. "
            f"Valid: {', '.join(sorted(ORGANISMS))}")

    segments = ORGANISMS[organism]
    if segments == ("main",):
        return None
    if not segment:
        raise PathoplexusError(
            f"{organism} is segmented; set 'Gene Segment' to one of "
            f"{', '.join(segments)} (one target row per segment)")
    if segment not in segments:
        raise PathoplexusError(
            f"{organism} has no segment {segment!r}. Valid: {', '.join(segments)}")
    return segment


def _field(name: str, organism: str, segment: str | None) -> str:
    """LAPIS name for metadata field ``name``, per-segment where that applies."""
    if segment and organism in SEGMENTED_METADATA and name in PER_SEGMENT_FIELDS:
        return f"{name}_{segment}"
    return name


def _suffixed_filter(key: str, organism: str, segment: str | None) -> str:
    """Same, for a filter key, which may carry a ``From``/``To`` suffix."""
    for bound in ("From", "To"):
        if key.endswith(bound):
            return _field(key[:-len(bound)], organism, segment) + bound
    return _field(key, organism, segment)


def build_filters(
    row: dict, organism: str, segment: str | None = None
) -> dict[str, str | list[str]]:
    """Translate mastersheet columns into LAPIS filter params.

    Always restricts to current versions: every revision is a separate record in
    Pathoplexus, and for ebola-zaire the superseded ones outnumber the current by
    more than two to one. ``dataUseTerms`` is deliberately not filtered -- see
    the spec; restricted records are fetchable, with display obligations attached.
    """
    filters = {"versionStatus": "LATEST_VERSION"}

    for col, value in row.items():
        if value is None or str(value).strip() == "":
            continue
        if col in UNSUPPORTED_FILTERS:
            raise PathoplexusError(
                f"'{col}' is an NCBI-only filter with no Pathoplexus equivalent; "
                f"remove it from this row or fetch the target from NCBI instead")
        if col in FILTER_MAP:
            key = _suffixed_filter(FILTER_MAP[col], organism, segment)
            if key in EXACT_MATCH_FILTERS:
                values = [v.strip() for v in str(value).split(",") if v.strip()]
                filters[key] = values[0] if len(values) == 1 else values
            else:
                filters[key] = str(value).strip()

    return filters


def list_accessions(organism: str, filters: dict) -> set[str]:
    """Every accessionVersion currently matching ``filters`` (the ID-only pull)."""
    params = urllib.parse.urlencode(filters, doseq=True, quote_via=urllib.parse.quote)
    url = _endpoint(organism, f"details?{params}&fields=accessionVersion&dataFormat=tsv")
    lines = _request(url).splitlines()
    # First line is the header.
    return {ln.strip() for ln in lines[1:] if ln.strip()}


def field_values(organism: str, field: str) -> list[str]:
    """Distinct values of ``field`` among current records, most common first.

    For the exact-match filters, so a picker can offer only values that exist.
    """
    url = _endpoint(organism, f"aggregated?versionStatus=LATEST_VERSION&fields={field}")
    rows = json.loads(_request(url))["data"]
    rows = [r for r in rows if r.get(field)]
    rows.sort(key=lambda r: -r["count"])
    return [r[field] for r in rows]


def _check_exact_values(organism: str, filters: dict) -> None:
    """Explain an empty result caused by a misspelt exact-match value.

    Spreadsheet rows are typed by hand, and "uganda" or "human" silently match
    nothing. Raises with the closest real value where one exists.
    """
    for key in EXACT_MATCH_FILTERS & filters.keys():
        wanted = filters[key] if isinstance(filters[key], list) else [filters[key]]
        known = field_values(organism, key)
        by_lower = {k.lower(): k for k in known}
        bad = [w for w in wanted if w not in known]
        if not bad:
            continue
        hints = [f"'{w}' (did you mean '{by_lower[w.lower()]}'?)" if w.lower() in by_lower
                 else f"'{w}'" for w in bad]
        sample = ", ".join(known[:8])
        raise PathoplexusError(
            f"No {organism} records have {key} {', '.join(hints)}. Matching is exact "
            f"and case-sensitive. Most common values: {sample}")


def base_accession(accession_version: str) -> str:
    """``PP_000LAXN.4`` -> ``PP_000LAXN``."""
    return accession_version.rsplit(".", 1)[0]


def classify(current: set[str], known: set[str]) -> tuple[set[str], set[str]]:
    """Split what we have not seen into (genuinely new, revised).

    Revisions mint a new accessionVersion, so both arrive as unseen IDs. They are
    told apart by the version-stripped accession. Both get re-analysed -- a
    revision can change the sequence, and a panel validated against v3 is not
    necessarily valid against v4 -- but they are reported separately, because
    Pathoplexus revisions come overwhelmingly from bulk pipeline re-ingests (97%
    of current ebola-zaire records are revisions, arriving in clusters of
    hundreds). Lumped together, one re-ingest day would look like an outbreak.
    """
    unseen = current - known
    known_bases = {base_accession(a) for a in known}
    revised = {a for a in unseen if base_accession(a) in known_bases}
    return unseen - revised, revised


def _batched(items: list[str]):
    for i in range(0, len(items), _BATCH):
        yield items[i:i + _BATCH]


def download_sequences(
    organism: str, accessions: set[str], out_path: Path, segment: str | None = None
) -> int:
    """Write FASTA for ``accessions``. Returns the number of records written."""
    path = "unalignedNucleotideSequences" + (f"/{segment}" if segment else "")
    url = _endpoint(organism, path)
    written = 0

    with open(out_path, "w") as fh:
        for batch in _batched(sorted(accessions)):
            text = _request(url, {"accessionVersion": batch})
            if not text.strip():
                continue
            # LAPIS omits records with no sequence for this segment, so the
            # response can be shorter than the batch.
            written += text.count(">")
            fh.write(text if text.endswith("\n") else text + "\n")

    return written


def download_metadata(
    organism: str, accessions: set[str], out_path: Path, segment: str | None = None
) -> int:
    """Write a metadata CSV for ``accessions``. Returns the number of rows.

    Column names follow what the monitor report expects (``accession``,
    ``Length``, ``Geographic Region``, ``Release date``), with the per-segment
    LAPIS names normalised back to the plain ones. The dataUseTerms columns are
    carried through verbatim and a per-sequence Pathoplexus link is added, both
    required when passing restricted records on.
    """
    # Requested LAPIS field -> output column.
    columns = {_field(f, organism, segment): COLUMN_RENAMES.get(f, f)
               for f in METADATA_FIELDS}

    url = _endpoint(organism, "details")
    rows = []
    for batch in _batched(sorted(accessions)):
        payload = {"accessionVersion": batch, "fields": list(columns)}
        rows.extend(json.loads(_request(url, payload))["data"])

    with open(out_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[*columns.values(), "Pathoplexus URL"])
        writer.writeheader()
        for row in rows:
            out = {columns[k]: v for k, v in row.items() if k in columns}
            out["Pathoplexus URL"] = SEQUENCE_PAGE.format(row["accessionVersion"])
            writer.writerow(out)

    return len(rows)


def fetch(
    organism: str,
    row: dict,
    out_dir: Path,
    known_accessions: set[str] | None = None,
    segment: str | None = None,
) -> dict:
    """Fetch what is new since ``known_accessions``.

    Writes ``sequences.fasta`` and ``metadata.csv`` into ``out_dir`` covering the
    new and revised records, and returns a dict with the accession sets. With
    ``known_accessions`` empty or None this is a first fetch and everything
    counts as new.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    segment = resolve_segment(organism, segment)
    known = known_accessions or set()

    filters = build_filters(row, organism, segment)
    current = list_accessions(organism, filters)
    if not current:
        _check_exact_values(organism, filters)
    new, revised = classify(current, known)
    to_download = new | revised

    fasta_path = meta_path = None
    if to_download:
        fasta_path = out_dir / "sequences.fasta"
        if not download_sequences(organism, to_download, fasta_path, segment):
            # Every match lacked a sequence for this segment.
            fasta_path.unlink()
            fasta_path = None
        meta_path = out_dir / "metadata.csv"
        download_metadata(organism, to_download, meta_path, segment)

    return {
        "organism": organism,
        "segment": segment,
        "all_accessions": current,
        "new_accessions": new,
        "revised_accessions": revised,
        "fasta_path": fasta_path,
        "metadata_path": meta_path,
    }


def main(argv: list[str] | None = None) -> None:
    """``python -m primercast.external.pathoplexus <organism> [--k=v ...]``"""
    import argparse

    p = argparse.ArgumentParser(description="Fetch sequences from Pathoplexus.")
    p.add_argument("organism", choices=sorted(ORGANISMS))
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--segment")
    for col in FILTER_MAP:
        p.add_argument(f"--{col}")
    args = p.parse_args(argv)

    row = {c: getattr(args, c) for c in FILTER_MAP if getattr(args, c)}
    try:
        result = fetch(args.organism, row, args.out, segment=args.segment)
    except PathoplexusError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"{len(result['all_accessions'])} sequences match; "
          f"{len(result['new_accessions'])} new, "
          f"{len(result['revised_accessions'])} revised")


if __name__ == "__main__":
    main()
