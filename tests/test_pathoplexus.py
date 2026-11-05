"""Tests for the Pathoplexus (LAPIS) fetch source.

No network: every test stubs ``pathoplexus._request``. The behaviour worth
pinning down is mostly about the API's sharp edges -- per-segment field names,
the two different release dates, and revisions arriving as new accessions --
which are documented in docs/pathoplexus_api_spec.md.
"""

import json
from pathlib import Path

import pytest

from primercast import adapt_cli
from primercast.external import pathoplexus as pp


@pytest.fixture
def fake_api(monkeypatch):
    """Stub LAPIS. Set ``fake_api.accessions``; records are derived from it."""
    class Api:
        accessions: list[str] = []
        calls: list[tuple[str, dict | None]] = []

        def __call__(self, url, body=None, timeout=300):
            self.calls.append((url, body))
            if "fields=accessionVersion&dataFormat=tsv" in url:
                return "accessionVersion\n" + "".join(f"{a}\n" for a in self.accessions)
            asked = body["accessionVersion"]
            if "unalignedNucleotideSequences" in url:
                # Distinct sequences: identical ones get deduplicated away.
                return "".join(f">{a}\n{'ACGT' * (i + 1)}\n" for i, a in enumerate(asked))
            fields = body["fields"]
            return json.dumps({"data": [
                {f: _value(f, a) for f in fields} for a in asked]})

    def _value(field, acc):
        if field == "accessionVersion":
            return acc
        if field.startswith("length"):
            return 18000
        if field == "dataUseTerms":
            return "RESTRICTED" if acc.startswith("R") else "OPEN"
        if field == "dataUseTermsRestrictedUntil":
            return "2027-06-08" if acc.startswith("R") else None
        if field == "dataUseTermsUrl":
            return pp.SEQUENCE_PAGE.replace("seq/{}", "about/terms") if acc.startswith("R") else None
        return f"{field}-val"

    api = Api()
    api.accessions, api.calls = [], []
    monkeypatch.setattr(pp, "_request", api)
    return api


# --- organisms and segments -------------------------------------------------

def test_all_sixteen_organisms_are_enabled():
    assert len(pp.ORGANISMS) == 16
    assert "ebola-bdbv" in pp.ORGANISMS and "dengue" in pp.ORGANISMS


def test_unsegmented_organism_needs_no_segment():
    assert pp.resolve_segment("ebola-zaire", None) is None


def test_segmented_organism_must_name_a_segment():
    """Per-segment FASTA headers are the bare accession, so merging collides."""
    with pytest.raises(pp.PathoplexusError, match="segmented"):
        pp.resolve_segment("cchf", None)


def test_unknown_segment_is_rejected():
    with pytest.raises(pp.PathoplexusError, match="L, M, S"):
        pp.resolve_segment("cchf", "X")


def test_unknown_organism_is_rejected():
    with pytest.raises(pp.PathoplexusError, match="Unknown Pathoplexus organism"):
        pp.resolve_segment("sars-cov-2", None)


# --- filter mapping ---------------------------------------------------------

def test_version_status_is_always_pinned():
    """Superseded versions outnumber current ones; never fetch the history."""
    assert pp.build_filters({}, "ebola-zaire")["versionStatus"] == "LATEST_VERSION"


def test_data_use_terms_is_not_filtered():
    """Restricted records are fetched too -- they are 94% of ebola-bdbv."""
    assert "dataUseTerms" not in pp.build_filters({}, "ebola-bdbv")


def test_collection_date_uses_the_permissive_range_fields():
    """sampleCollectionDate is a string with partial dates, so has no From/To.

    The permissive pairing keeps year-only records, which the strict one drops.
    """
    f = pp.build_filters(
        {"min_collection_date": "2014-01-01", "max_collection_date": "2020-01-01"},
        "ebola-zaire")
    assert f["sampleCollectionDateRangeUpperFrom"] == "2014-01-01"
    assert f["sampleCollectionDateRangeLowerTo"] == "2020-01-01"


def test_release_date_uses_earliest_release_date():
    f = pp.build_filters({"min_release_date": "2024-01-01"}, "ebola-zaire")
    assert f["earliestReleaseDateFrom"] == "2024-01-01"


def test_segmented_organisms_get_per_segment_field_names():
    f = pp.build_filters(
        {"min_seq_length": "11000", "max_ambiguous_chars": "50", "geographic_location": "Turkey"},
        "cchf", "L")
    assert f["length_LFrom"] == "11000"
    assert f["totalAmbiguousNucs_LTo"] == "50"
    assert f["geoLocCountry"] == "Turkey"  # not per-segment


def test_dengue_serotypes_are_not_real_segments():
    """Its "segments" are serotype slots; metadata stays single-valued."""
    assert pp.build_filters({"min_seq_length": "10000"}, "dengue", "DENV-1")["lengthFrom"]


def test_ncbi_only_filters_are_rejected_not_dropped():
    with pytest.raises(pp.PathoplexusError, match="refseq_only"):
        pp.build_filters({"refseq_only": "TRUE"}, "ebola-zaire")


def test_blank_filter_values_are_ignored():
    assert "lengthFrom" not in pp.build_filters({"min_seq_length": ""}, "ebola-zaire")


# --- revisions --------------------------------------------------------------

def test_revision_is_told_apart_from_a_new_sequence():
    new, revised = pp.classify({"A.1", "B.4", "C.1", "D.1"}, {"A.1", "B.3", "C.1"})
    assert new == {"D.1"}
    assert revised == {"B.4"}


def test_first_run_has_no_revisions():
    new, revised = pp.classify({"A.1", "B.1"}, set())
    assert new == {"A.1", "B.1"} and revised == set()


def test_unversioned_accessions_do_not_look_revised():
    """NCBI accessions share this code path and may carry no version."""
    new, revised = pp.classify({"AF086833", "AF272001"}, {"AF086833"})
    assert new == {"AF272001"} and revised == set()


# --- fetching ---------------------------------------------------------------

def test_only_unseen_accessions_are_downloaded(tmp_path, fake_api):
    """The whole point of the two-phase fetch."""
    fake_api.accessions = ["A.1", "B.1", "C.1"]

    result = pp.fetch("ebola-zaire", {}, tmp_path, known_accessions={"A.1"})

    assert result["new_accessions"] == {"B.1", "C.1"}
    downloaded = [b["accessionVersion"] for u, b in fake_api.calls
                  if b and "unalignedNucleotideSequences" in u]
    assert downloaded == [["B.1", "C.1"]]


def test_nothing_new_downloads_nothing(tmp_path, fake_api):
    fake_api.accessions = ["A.1"]

    result = pp.fetch("ebola-zaire", {}, tmp_path, known_accessions={"A.1"})

    assert result["fasta_path"] is None and result["metadata_path"] is None
    assert result["all_accessions"] == {"A.1"}


def test_metadata_columns_match_what_the_report_expects(tmp_path, fake_api):
    fake_api.accessions = ["A.1"]

    result = pp.fetch("ebola-zaire", {}, tmp_path)

    header = result["metadata_path"].read_text().splitlines()[0]
    for col in ("accession", "Length", "Geographic Region", "Release date"):
        assert col in header


def test_restricted_terms_columns_are_carried_through(tmp_path, fake_api):
    """Required when passing restricted data on -- must survive the fetch."""
    fake_api.accessions = ["R.1"]

    result = pp.fetch("ebola-bdbv", {}, tmp_path)

    header = result["metadata_path"].read_text().splitlines()[0]
    for col in ("dataUseTerms", "dataUseTermsRestrictedUntil", "dataUseTermsUrl"):
        assert col in header


def test_every_sequence_gets_a_pathoplexus_link(tmp_path, fake_api):
    """Also required: displayed records need a direct link to their source page."""
    fake_api.accessions = ["R.1"]

    result = pp.fetch("ebola-bdbv", {}, tmp_path)

    assert "https://pathoplexus.org/seq/R.1" in result["metadata_path"].read_text()


def test_per_segment_metadata_is_normalised_back(tmp_path, fake_api):
    """cchf reports ``length_L``; the report table looks for ``Length``."""
    fake_api.accessions = ["A.1"]

    result = pp.fetch("cchf", {}, tmp_path, segment="L")

    header = result["metadata_path"].read_text().splitlines()[0]
    assert "Length" in header and "length_L" not in header


def test_segment_is_requested_from_the_segment_endpoint(tmp_path, fake_api):
    fake_api.accessions = ["A.1"]

    pp.fetch("cchf", {}, tmp_path, segment="M")

    assert any(u.endswith("unalignedNucleotideSequences/M") for u, _ in fake_api.calls)


def test_large_fetches_are_batched(tmp_path, fake_api, monkeypatch):
    monkeypatch.setattr(pp, "_BATCH", 2)
    fake_api.accessions = [f"A{i}.1" for i in range(5)]

    pp.fetch("ebola-zaire", {}, tmp_path)

    seq_calls = [b for u, b in fake_api.calls if b and "unalignedNucleotide" in u]
    assert [len(b["accessionVersion"]) for b in seq_calls] == [2, 2, 1]


def test_bad_filter_key_surfaces_the_valid_key_list(monkeypatch, tmp_path):
    """LAPIS answers a 400 with every valid key; that is the useful part."""
    import urllib.error

    def boom(*a, **kw):
        raise urllib.error.HTTPError(
            "u", 400, "Bad Request", {},
            __import__("io").BytesIO(json.dumps(
                {"error": {"detail": "'nope' is not a valid key. Valid keys are: accession"}}
            ).encode()))

    monkeypatch.setattr(pp.urllib.request, "urlopen", boom)
    with pytest.raises(pp.PathoplexusError, match="Valid keys are"):
        pp.list_accessions("ebola-zaire", {})


# --- monitor integration ----------------------------------------------------

def test_source_defaults_to_ncbi():
    assert adapt_cli._fetch_source({}) == "ncbi"
    assert adapt_cli._fetch_source({"source": "Pathoplexus"}) == "pathoplexus"


def test_unknown_source_is_rejected():
    with pytest.raises(ValueError, match="unknown source"):
        adapt_cli._fetch_source({"source": "gisaid"})


def _monitor_row(**extra):
    return {"Target name": "T", "source": "pathoplexus",
            "pathoplexus_organism": "ebola-zaire", **extra}


def test_monitor_routes_to_pathoplexus_without_calling_gget(tmp_path, fake_api, monkeypatch):
    def no_subprocess(*a, **kw):
        raise AssertionError("gget must not run for a Pathoplexus row")
    monkeypatch.setattr(adapt_cli.subprocess, "run", no_subprocess)
    fake_api.accessions = ["A.1", "B.1"]
    date_dir = tmp_path / "20261103"; date_dir.mkdir()

    result = adapt_cli._monitor_fetch([_monitor_row()], tmp_path, date_dir, "20261103")["T"]

    assert result["status"] == "success"
    assert result["source"] == "pathoplexus"
    assert result["new_count"] == 2


def test_monitor_reports_revisions_separately(tmp_path, fake_api):
    fake_api.accessions = ["A.1", "B.2", "C.1"]
    prev = tmp_path / "20261101"; prev.mkdir()
    (prev / "T_accessions.txt").write_text("A.1\nB.1\n")
    date_dir = tmp_path / "20261103"; date_dir.mkdir()

    result = adapt_cli._monitor_fetch([_monitor_row()], tmp_path, date_dir, "20261103")["T"]

    assert result["new_accessions"] == {"C.1"}
    assert result["revised_accessions"] == {"B.2"}


def test_revised_sequences_are_still_evaluated(tmp_path, fake_api):
    """Splitting them in the report must not drop them from the analysis."""
    fake_api.accessions = ["A.2"]
    prev = tmp_path / "20261101"; prev.mkdir()
    (prev / "T_accessions.txt").write_text("A.1\n")
    date_dir = tmp_path / "20261103"; date_dir.mkdir()

    result = adapt_cli._monitor_fetch([_monitor_row()], tmp_path, date_dir, "20261103")["T"]

    assert result["revised_accessions"] == {"A.2"}
    assert ">A.2" in result["new_fasta_path"].read_text()


def test_accession_history_covers_everything_matching(tmp_path, fake_api):
    """Not just the newly downloaded ones, or they re-download every run."""
    fake_api.accessions = ["A.1", "B.1", "C.1"]
    prev = tmp_path / "20261101"; prev.mkdir()
    (prev / "T_accessions.txt").write_text("A.1\n")
    date_dir = tmp_path / "20261103"; date_dir.mkdir()

    adapt_cli._monitor_fetch([_monitor_row()], tmp_path, date_dir, "20261103")

    history = (date_dir / "T_accessions.txt").read_text().split()
    assert sorted(history) == ["A.1", "B.1", "C.1"]


def test_missing_organism_is_an_error_not_a_crash(tmp_path, fake_api):
    date_dir = tmp_path / "20261103"; date_dir.mkdir()
    row = {"Target name": "T", "source": "pathoplexus"}

    result = adapt_cli._monitor_fetch([row], tmp_path, date_dir, "20261103")["T"]

    assert result["status"] == "error"


def test_first_fetch_matching_nothing_is_flagged(tmp_path, fake_api):
    fake_api.accessions = []
    date_dir = tmp_path / "20261103"; date_dir.mkdir()

    result = adapt_cli._monitor_fetch([_monitor_row()], tmp_path, date_dir, "20261103")["T"]

    assert result["status"] == "no_output"


# --- data use notice --------------------------------------------------------

def _meta(tmp_path, rows):
    import csv
    path = tmp_path / "metadata.csv"
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


def test_notice_appears_only_for_restricted_records(tmp_path):
    path = _meta(tmp_path, [
        {"accession": "A.1", "dataUseTerms": "OPEN",
         "dataUseTermsRestrictedUntil": "", "dataUseTermsUrl": ""}])

    assert adapt_cli._data_use_notice({"A.1"}, path) == ""


def test_notice_states_the_terms_and_expiry(tmp_path):
    path = _meta(tmp_path, [
        {"accession": "R.1", "dataUseTerms": "RESTRICTED",
         "dataUseTermsRestrictedUntil": "2027-06-08", "dataUseTermsUrl": "https://x/terms"}])

    notice = adapt_cli._data_use_notice({"R.1"}, path)

    assert "Restricted-Use Data" in notice
    assert "2027-06-08" in notice
    assert "https://x/terms" in notice


def test_notice_gives_a_span_for_many_expiry_dates(tmp_path):
    """A real batch spans dozens; listing them all is unreadable."""
    path = _meta(tmp_path, [
        {"accession": f"R.{i}", "dataUseTerms": "RESTRICTED",
         "dataUseTermsRestrictedUntil": d, "dataUseTermsUrl": "https://x/terms"}
        for i, d in enumerate(["2027-01-01", "2027-06-08", "2027-12-31"])])

    notice = adapt_cli._data_use_notice({"R.0", "R.1", "R.2"}, path)

    assert "between 2027-01-01 and 2027-12-31" in notice


def test_ncbi_metadata_has_no_notice(tmp_path):
    """NCBI metadata has no dataUseTerms column at all."""
    path = _meta(tmp_path, [{"accession": "AF086833", "Length": "18959"}])

    assert adapt_cli._data_use_notice({"AF086833"}, path) == ""


def test_manual_setup_segment_column_is_honoured(tmp_path, fake_api):
    """The GUI's one-row mastersheet writes "segment", not "Gene Segment"."""
    fake_api.accessions = ["A.1"]
    date_dir = tmp_path / "20261103"; date_dir.mkdir()
    row = {"Target name": "T", "source": "pathoplexus",
           "pathoplexus_organism": "cchf", "segment": "S"}

    adapt_cli._monitor_fetch([row], tmp_path, date_dir, "20261103")

    assert any(u.endswith("unalignedNucleotideSequences/S") for u, _ in fake_api.calls)


# --- exact-match filters ----------------------------------------------------

def test_every_organism_has_a_display_name():
    assert pp.ORGANISM_NAMES.keys() == pp.ORGANISMS.keys()


def test_several_countries_become_an_or_filter():
    f = pp.build_filters({"geographic_location": "Uganda, Kenya"}, "ebola-bdbv")
    assert f["geoLocCountry"] == ["Uganda", "Kenya"]


def test_or_filter_is_sent_as_repeated_params(fake_api):
    pp.list_accessions("ebola-bdbv", {"geoLocCountry": ["Uganda", "Kenya"]})
    url = fake_api.calls[0][0]
    assert "geoLocCountry=Uganda&geoLocCountry=Kenya" in url


def test_country_names_with_spaces_are_encoded(fake_api):
    pp.list_accessions("ebola-bdbv", {"geoLocCountry": "Democratic Republic of the Congo"})
    assert "geoLocCountry=Democratic%20Republic%20of%20the%20Congo" in fake_api.calls[0][0]


def _values_api(monkeypatch, accessions, values):
    """Stub where the ID pull returns ``accessions`` and ``aggregated`` ``values``."""
    def request(url, body=None, timeout=300):
        if "aggregated" in url:
            field = url.rsplit("fields=", 1)[1]
            return json.dumps({"data": [{field: v, "count": 10 - i}
                                        for i, v in enumerate(values)]})
        return "accessionVersion\n" + "".join(f"{a}\n" for a in accessions)
    monkeypatch.setattr(pp, "_request", request)


def test_misspelt_country_explains_the_empty_result(tmp_path, monkeypatch):
    """Typed by hand in a sheet; LAPIS is case-sensitive."""
    _values_api(monkeypatch, [], ["Uganda", "Kenya"])
    with pytest.raises(pp.PathoplexusError, match="did you mean 'Uganda'"):
        pp.fetch("ebola-bdbv", {"geographic_location": "uganda"}, tmp_path)


def test_unknown_host_lists_the_real_values(tmp_path, monkeypatch):
    _values_api(monkeypatch, [], ["Homo sapiens"])
    with pytest.raises(pp.PathoplexusError, match="Homo sapiens"):
        pp.fetch("ebola-bdbv", {"host": "human"}, tmp_path)


def test_valid_values_that_match_nothing_are_not_an_error(tmp_path, monkeypatch):
    """Real countries with no sequences yet: just an empty fetch."""
    _values_api(monkeypatch, [], ["Uganda"])
    result = pp.fetch("ebola-bdbv", {"geographic_location": "Uganda"}, tmp_path)
    assert result["all_accessions"] == set()


def test_field_values_are_most_common_first_without_blanks(monkeypatch):
    monkeypatch.setattr(pp, "_request", lambda url, body=None, timeout=300: json.dumps(
        {"data": [{"geoLocCountry": "Kenya", "count": 2}, {"geoLocCountry": None, "count": 50},
                  {"geoLocCountry": "Uganda", "count": 9}]}))
    assert pp.field_values("ebola-bdbv", "geoLocCountry") == ["Uganda", "Kenya"]


# --- manual setup round trip --------------------------------------------------

def test_manual_pathoplexus_row_survives_the_one_row_sheet(tmp_path, fake_api):
    """The GUI saves a manual target as a mastersheet; the run reads it back."""
    row = {"Target name": "bdbv", "source": "pathoplexus",
           "pathoplexus_organism": "ebola-bdbv", "geographic_location": "Uganda"}
    csv_text = adapt_cli._rows_to_sheet_csv(adapt_cli.MANUAL_SHEET_HEADERS, [row])
    _, rows = adapt_cli._load_spreadsheet(csv_text)
    fake_api.accessions = ["A.1"]
    date_dir = tmp_path / "20261105"; date_dir.mkdir()

    result = adapt_cli._monitor_fetch(rows, tmp_path, date_dir, "20261105")["bdbv"]

    assert result["source"] == "pathoplexus"
    assert "geoLocCountry=Uganda" in fake_api.calls[0][0]


def test_spreadsheet_keeps_pathoplexus_rows_without_a_taxid():
    """The loader used to drop any row with no TaxID."""
    headers = ["Target name", "source", "pathoplexus_organism", "TaxID", "host"]
    csv_text = adapt_cli._rows_to_sheet_csv(headers, [
        {"Target name": "bdbv", "source": "pathoplexus", "pathoplexus_organism": "ebola-bdbv"},
        {"Target name": "blank"},
    ])
    _, rows = adapt_cli._load_spreadsheet(csv_text)
    assert [r["Target name"] for r in rows] == ["bdbv"]


def test_header_whitespace_is_stripped():
    """The published template had " subtype_filter" with a leading space."""
    csv_text = adapt_cli._rows_to_sheet_csv(
        ["Target name", "TaxID", " subtype_filter"],
        [{"Target name": "flu", "TaxID": "11320", " subtype_filter": "H5N1"}])
    _, rows = adapt_cli._load_spreadsheet(csv_text)
    assert rows[0]["subtype_filter"] == "H5N1"
    assert " subtype_filter" not in rows[0]


@pytest.mark.parametrize("col", ["has_proteins", "min_gene_count", "max_protein_count"])
def test_ncbi_count_filters_are_rejected_too(col):
    with pytest.raises(pp.PathoplexusError, match=col):
        pp.build_filters({col: "3"}, "ebola-zaire")
