"""Tests for the configuration page's "Reset to defaults" button.

Streamlit gives a keyed widget's stored value precedence over its ``value=``
argument, so clearing ``st.session_state.params`` alone leaves every control
showing -- and writing back -- the edited value. The reset must drop the
widget keys too.
"""

import sys
from pathlib import Path

import pytest

# gui/ lives at the repo root (not under src/), so make it importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import app as gui_app  # noqa: E402
from gui.app import DEFAULT_PARAMS, PARAM_WIDGET_KEY_PREFIX  # noqa: E402


class _FakeSessionState(dict):
    """dict with attribute access, like Streamlit's SessionStateProxy."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name, value):
        self[name] = value


class _FakeStreamlit:
    def __init__(self):
        self.session_state = _FakeSessionState()


@pytest.fixture
def fake_st(monkeypatch):
    stub = _FakeStreamlit()
    monkeypatch.setattr(gui_app, "st", stub)
    return stub


def test_reset_restores_default_values(fake_st):
    fake_st.session_state.params = dict(DEFAULT_PARAMS, TM_MIN=42, GC_MAX=99)

    gui_app._reset_params_to_defaults()

    assert fake_st.session_state.params == DEFAULT_PARAMS


def test_reset_clears_widget_keys_so_controls_redraw_at_defaults(fake_st):
    """The actual bug: params were reset but the widgets kept their own state."""
    fake_st.session_state.params = dict(DEFAULT_PARAMS)
    fake_st.session_state["p_tm_min"] = 42
    fake_st.session_state["p_probe_len_max"] = 99

    gui_app._reset_params_to_defaults()

    leftover = [k for k in fake_st.session_state
                if k.startswith(PARAM_WIDGET_KEY_PREFIX)]
    assert leftover == []


def test_reset_leaves_unrelated_session_state_alone(fake_st):
    """Targets, run state and the like must survive a parameter reset."""
    fake_st.session_state.params = dict(DEFAULT_PARAMS)
    fake_st.session_state["p_tm_min"] = 42
    fake_st.session_state["targets"] = ["virusA"]
    fake_st.session_state["page"] = "configure"
    # Nothing else may be swept up by the prefix match.
    fake_st.session_state["_params_reset_done"] = True
    fake_st.session_state["pset_path"] = "/tmp/x.fa"

    gui_app._reset_params_to_defaults()

    assert fake_st.session_state["targets"] == ["virusA"]
    assert fake_st.session_state["page"] == "configure"
    assert fake_st.session_state["_params_reset_done"] is True
    assert fake_st.session_state["pset_path"] == "/tmp/x.fa"


def test_reset_returns_a_copy_not_the_defaults_dict(fake_st):
    """Editing a parameter after a reset must not mutate DEFAULT_PARAMS."""
    fake_st.session_state.params = {}

    gui_app._reset_params_to_defaults()
    fake_st.session_state.params["TM_MIN"] = 1

    assert DEFAULT_PARAMS["TM_MIN"] != 1


def _params_page():
    """Script body for AppTest: just the parameters page."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gui import app as gui_app

    gui_app._tab_parameters()


def test_reset_rebuilds_the_controls_in_the_browser():
    """End-to-end: the element id must change, not just the stored value.

    st.number_input derives its element id from the key alone, so before the
    nonce the id was identical across a reset. The server reported the default
    while the mounted control still showed the edited number -- and posted it
    back on the next interaction. Asserting on the value alone passes either
    way, so this pins the id.
    """
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_function(_params_page, default_timeout=60).run()
    assert not at.exception
    before_id = at.number_input(key="p_tm_min_0").proto.id

    at.number_input(key="p_tm_min_0").set_value(99.0).run()
    assert at.session_state["params"]["TM_MIN"] == 99.0

    at.button[0].click().run()
    widget = at.number_input(key="p_tm_min_1")
    assert widget.proto.id != before_id, "element id unchanged — browser keeps the old value"
    assert widget.value == DEFAULT_PARAMS["TM_MIN"]
    assert at.session_state["params"]["TM_MIN"] == DEFAULT_PARAMS["TM_MIN"]


def test_every_parameter_widget_key_goes_through_pkey():
    """Guards both contracts the reset relies on.

    A control keyed with a bare string keeps its element id across a reset, so
    the browser never rebuilds it and goes on showing the edited number; one
    keyed outside the prefix is never swept from session_state.
    """
    import re

    source = Path(gui_app.__file__).read_text()
    body = source.split("def _tab_parameters(")[1].split("\ndef ")[0]

    bare = re.findall(r"""key=["']([^"']+)["']""", body)
    assert bare == [], f"keyed without _pkey(): {bare}"

    keys = re.findall(r"""key=_pkey\(["']([^"']+)["']\)""", body)
    assert keys, "no keyed widgets found on the parameters page"
    assert [k for k in keys if not k.startswith(PARAM_WIDGET_KEY_PREFIX)] == []


def test_pkey_changes_when_the_nonce_is_bumped(fake_st):
    """The element id has to move, or the browser keeps the old control."""
    fake_st.session_state.params = dict(DEFAULT_PARAMS)
    before = gui_app._pkey("p_tm_min")

    gui_app._reset_params_to_defaults()
    after = gui_app._pkey("p_tm_min")

    assert before != after
    assert after.startswith(PARAM_WIDGET_KEY_PREFIX)


def test_repeated_resets_keep_producing_fresh_keys(fake_st):
    fake_st.session_state.params = dict(DEFAULT_PARAMS)
    seen = set()
    for _ in range(3):
        gui_app._reset_params_to_defaults()
        seen.add(gui_app._pkey("p_tm_min"))

    assert len(seen) == 3
