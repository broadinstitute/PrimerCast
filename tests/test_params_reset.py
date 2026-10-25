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


def test_every_parameter_widget_key_uses_the_swept_prefix():
    """Guards the prefix contract the reset relies on.

    A control added with some other key would silently stop resetting.
    """
    import re

    source = Path(gui_app.__file__).read_text()
    body = source.split("def _tab_parameters(")[1].split("\ndef ")[0]
    keys = re.findall(r"""key=["']([^"']+)["']""", body)

    assert keys, "no keyed widgets found on the parameters page"
    assert [k for k in keys if not k.startswith(PARAM_WIDGET_KEY_PREFIX)] == []
