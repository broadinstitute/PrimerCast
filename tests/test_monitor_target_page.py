"""The Monitor "Select Pathogens" page, driven through Streamlit's AppTest.

Manual setup is the main path, with Pathoplexus as a source alongside NCBI.
Pathoplexus is stubbed; nothing here touches the network.
"""

import pytest

from primercast.external import pathoplexus


def _target_page():
    """Script body for AppTest: just the monitor target page."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gui import app as gui_app

    gui_app._page_monitor_target()


@pytest.fixture
def page(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest

    values = {"geoLocCountry": ["Uganda", "Kenya", "missing"],
              "hostNameScientific": ["Homo sapiens"]}
    monkeypatch.setattr(pathoplexus, "field_values", lambda org, field: values[field])
    at = AppTest.from_function(_target_page, default_timeout=60)
    at.run()
    assert not at.exception
    return at


def _choose_pathoplexus(at, organism):
    at.radio(key="monitor_source").set_value("pathoplexus").run()
    at.selectbox(key="monitor_pp_organism").set_value(organism).run()
    assert not at.exception


def test_manual_setup_comes_before_the_spreadsheet(page):
    labels = [e.label for e in page.expander]
    assert page.subheader[0].value == "Set up a target"
    assert "Load from Google Sheets" in labels


def test_ncbi_is_the_default_source(page):
    assert page.radio(key="monitor_source").value == "ncbi"


def test_pathoplexus_target_is_saved_as_a_pathoplexus_row(page):
    _choose_pathoplexus(page, "ebola-bdbv")
    page.multiselect(key="monitor_pp_geoLocCountry_ebola-bdbv").set_value(["Uganda", "Kenya"]).run()
    page.button(key="monitor_pp_use_btn").click().run()

    row = page.session_state["monitor_spreadsheet_data"]["data_rows"][0]
    assert row["source"] == "pathoplexus"
    assert row["pathoplexus_organism"] == "ebola-bdbv"
    assert row["Target name"] == "ebola-bdbv"
    assert row["geographic_location"] == "Uganda, Kenya"


def test_country_picker_offers_only_real_values(page):
    _choose_pathoplexus(page, "ebola-bdbv")
    options = page.multiselect(key="monitor_pp_geoLocCountry_ebola-bdbv").options
    assert options == ["Uganda", "Kenya"]  # "missing" is a placeholder, not a country


def test_segmented_organism_asks_for_a_segment(page):
    _choose_pathoplexus(page, "cchf")
    seg = page.selectbox(key="monitor_pp_segment_cchf")
    assert seg.options == ["L", "M", "S"]
    seg.set_value("M").run()
    assert page.text_input(key="monitor_pp_target_name").value == "cchf_M"

    page.button(key="monitor_pp_use_btn").click().run()
    assert page.session_state["monitor_spreadsheet_data"]["data_rows"][0]["segment"] == "M"


def test_unsegmented_organism_has_no_segment_picker(page):
    _choose_pathoplexus(page, "ebola-zaire")
    assert not [s for s in page.selectbox if s.key.startswith("monitor_pp_segment")]


def test_ncbi_only_fields_are_not_offered_for_pathoplexus(page):
    _choose_pathoplexus(page, "ebola-zaire")
    labels = {w.label for w in [*page.checkbox, *page.selectbox, *page.text_input]}
    assert "RefSeq only" not in labels
    assert "Nucleotide completeness" not in labels
    assert "Subtype" not in labels


def test_picker_falls_back_to_typing_when_pathoplexus_is_down(monkeypatch):
    from streamlit.testing.v1 import AppTest

    def down(org, field):
        raise pathoplexus.PathoplexusError("unreachable")
    monkeypatch.setattr(pathoplexus, "field_values", down)
    at = AppTest.from_function(_target_page, default_timeout=60).run()
    _choose_pathoplexus(at, "marburg")

    assert at.text_input(key="monitor_pp_geoLocCountry_text")


def test_sheet_of_only_example_rows_explains_itself(monkeypatch):
    """Loading used to report "Loaded 0 row(s)" and leave Continue enabled."""
    from streamlit.testing.v1 import AppTest
    from primercast import adapt_cli

    sheet = adapt_cli._rows_to_sheet_csv(
        ["Note", "Target name", "TaxID"], [{"Note": "example", "Target name": "t", "TaxID": "1"}])
    monkeypatch.setattr(adapt_cli, "_download_spreadsheet_csv", lambda sid: sheet)
    import gui.app
    monkeypatch.setattr(gui.app, "_download_spreadsheet_csv", lambda sid: sheet)

    at = AppTest.from_function(_target_page, default_timeout=60).run()
    at.text_input(key="monitor_spreadsheet_url").set_value(
        "https://docs.google.com/spreadsheets/d/abc123/edit").run()
    at.button(key="monitor_load_sheet").click().run()

    assert not at.exception
    assert any("marked 'example' in column A" in w.value for w in at.warning)
    assert "monitor_spreadsheet_data" not in at.session_state
    assert at.button(key="monitor_target_cont").disabled
