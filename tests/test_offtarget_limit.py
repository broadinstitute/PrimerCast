"""Tests for the cap on how many off-target sequences one run may evaluate.

The limit is shared by the cross-reactivity and host multiselects, because the
Snakefile runs them as a single list (``OFF_TARGETS = CROSS + HOST``). Each is
guarded by an ``on_change`` callback that rolls back the pick that went over.
"""

import re
import sys
from pathlib import Path

import pytest

# gui/ lives at the repo root (not under src/), so make it importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui import app as gui_app  # noqa: E402
from gui.app import MAX_OFFTARGET_FILES, OFFTARGET_SELECT_KEYS  # noqa: E402


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


def _names(n, prefix="f"):
    return [f"{prefix}{i}" for i in range(n)]


def test_selection_under_the_cap_is_left_alone(fake_st):
    picked = _names(MAX_OFFTARGET_FILES - 1)
    fake_st.session_state["cross_select"] = list(picked)
    fake_st.session_state["host_select"] = []

    gui_app._enforce_offtarget_limit("cross_select")

    assert fake_st.session_state["cross_select"] == picked
    assert "_offtarget_limit_hit" not in fake_st.session_state


def test_selection_exactly_at_the_cap_is_allowed(fake_st):
    """The cap is a maximum, not a ceiling the last pick trips over."""
    picked = _names(MAX_OFFTARGET_FILES)
    fake_st.session_state["cross_select"] = list(picked)
    fake_st.session_state["host_select"] = []

    gui_app._enforce_offtarget_limit("cross_select")

    assert fake_st.session_state["cross_select"] == picked
    assert "_offtarget_limit_hit" not in fake_st.session_state


def test_one_over_the_cap_drops_the_new_pick_and_flags_the_dialog(fake_st):
    picked = _names(MAX_OFFTARGET_FILES + 1)
    fake_st.session_state["cross_select"] = list(picked)
    fake_st.session_state["host_select"] = []

    gui_app._enforce_offtarget_limit("cross_select")

    # multiselect appends, so the rejected entry is the one just added.
    assert fake_st.session_state["cross_select"] == picked[:MAX_OFFTARGET_FILES]
    assert fake_st.session_state["_offtarget_limit_hit"] is True


def test_cross_and_host_share_one_budget(fake_st):
    """A host pick is rejected by cross-reactivity sequences already chosen."""
    fake_st.session_state["cross_select"] = _names(MAX_OFFTARGET_FILES, "c")
    fake_st.session_state["host_select"] = ["HUMAN"]

    gui_app._enforce_offtarget_limit("host_select")

    assert fake_st.session_state["host_select"] == []
    assert fake_st.session_state["_offtarget_limit_hit"] is True


def test_partial_budget_keeps_what_fits(fake_st):
    """Adding three with two slots left keeps two, not zero and not three."""
    fake_st.session_state["cross_select"] = _names(MAX_OFFTARGET_FILES - 2, "c")
    fake_st.session_state["host_select"] = ["h0", "h1", "h2"]

    gui_app._enforce_offtarget_limit("host_select")

    assert fake_st.session_state["host_select"] == ["h0", "h1"]
    assert fake_st.session_state["_offtarget_limit_hit"] is True


def test_guard_does_not_disturb_the_other_multiselect(fake_st):
    over = _names(MAX_OFFTARGET_FILES + 3, "c")
    fake_st.session_state["cross_select"] = over
    fake_st.session_state["host_select"] = ["HUMAN"]

    gui_app._enforce_offtarget_limit("cross_select")

    assert fake_st.session_state["host_select"] == ["HUMAN"]
    assert len(fake_st.session_state["cross_select"]) == MAX_OFFTARGET_FILES - 1


def test_guard_tolerates_an_unset_counterpart(fake_st):
    """The host multiselect does not exist until that section renders."""
    fake_st.session_state["cross_select"] = _names(2)

    gui_app._enforce_offtarget_limit("cross_select")

    assert fake_st.session_state["cross_select"] == _names(2)


def test_both_offtarget_multiselects_are_wired_to_the_guard():
    """Guards the contract: an unwired multiselect would bypass the cap."""
    source = Path(gui_app.__file__).read_text()
    body = source.split("def _page_select_offtarget(")[1].split("\ndef ")[0]

    for key in OFFTARGET_SELECT_KEYS:
        block = body.split(f'key="{key}"')
        assert len(block) == 2, f"{key} is not a keyed widget on this page"
        # on_change/args sit in the same st.multiselect(...) call as the key,
        # which ends at the closing paren on its own line.
        call = block[1].split("\n        )")[0]
        assert "on_change=_enforce_offtarget_limit" in call, key
        assert f'args=("{key}",)' in call, key


def test_continue_is_blocked_while_the_selection_is_over_the_cap():
    """The restore path seeds the multiselects without firing on_change."""
    source = Path(gui_app.__file__).read_text()
    body = source.split("def _page_select_offtarget(")[1].split("\ndef ")[0]

    assert re.search(r"over_limit\s*=\s*n_offtarget\s*>\s*MAX_OFFTARGET_FILES", body)
    cont = body.split('"Continue →"')[1].split(")")[0]
    assert "disabled=over_limit" in cont
