"""Assert one visible outcome and automatic navigation for completed runs."""

import sys

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest


@pytest.fixture(autouse=True)
def restore_main():
    original = sys.modules["__main__"]
    yield
    sys.modules["__main__"] = original


@pytest.mark.parametrize("state,kind", [("running", "info"), ("failed", "error"),
                                       ("stopping", "warning"), ("stopped", "warning"),
                                       ("no_candidates", "warning"), ("dry_run", "success")])
def test_feedback_has_one_banner_and_failure_log_expands(state, kind):
    snapshot = dict(status=state, reason="Current outcome", elapsed=3, rows=[], log="test log")
    app = AppTest.from_string(f"from gui.run_ui import render_feedback\nrender_feedback({snapshot!r})").run()
    assert not app.exception
    assert len(getattr(app, kind)) == 1
    assert sum(len(getattr(app, name)) for name in ("info", "error", "warning", "success")) == 1
    assert app.expander[0].proto.expanded == (state == "failed")


@pytest.mark.parametrize("state,expected_page", [("completed", "results"), ("no_candidates", "results"),
                                                ("failed", "run"), ("stopped", "run"), ("dry_run", "run")])
def test_terminal_state_refreshes_controls_and_opens_correct_results(state, expected_page):
    code = f'''
from pathlib import Path
import streamlit as st
from gui.run_ui import render_live_run
class Runner:
    token = "this-run"
    output_dir = Path("/tmp/new-run")
    def snapshot(self, labels):
        return dict(status={state!r}, reason="Current outcome", elapsed=3, rows=[], log="test log", return_code=0)
st.session_state.setdefault("page", "run")
st.session_state.setdefault("pipeline_running", True)
if "initialized" not in st.session_state:
    st.session_state["results_run_selector"] = "old-run"
    st.session_state["initialized"] = True
if st.session_state["page"] == "run":
    render_live_run(Runner(), [])
else:
    st.write("Results for " + st.session_state["run_id"])
'''
    app = AppTest.from_string(code).run()
    assert not app.exception
    assert not app.session_state["pipeline_running"]
    assert app.session_state["page"] == expected_page
    if expected_page == "results":
        assert app.session_state["run_id"] == "new-run"
        assert "results_run_selector" not in app.session_state
    else:
        # A fragment finishing twice does not cause an infinite rerun loop.
        app.run()
        assert not app.exception


def test_run_page_does_not_mix_configuration_errors_with_active_run(monkeypatch, tmp_path):
    # The complete page must not even run preflight while a process is active.
    code = '''
import streamlit as st
from gui import app
class Runner:
    token = "running-test"
    def snapshot(self, labels):
        return dict(status="running", reason="Running now", elapsed=1, rows=[], log="", return_code=None)
    def stop(self):
        pass
def preflight():
    raise AssertionError("Preflight must not run during an active job")
original_preflight = app._preflight_checks
app._preflight_checks = preflight
st.session_state["pipeline_running"] = True
st.session_state["_pipeline_run"] = Runner()
try:
    app._tab_run()
finally:
    app._preflight_checks = original_preflight
'''
    app = AppTest.from_string(code).run(timeout=15)
    assert not app.exception
    assert len(app.info) == 1
    assert not app.error and not app.warning and not app.success
    assert app.button[0].disabled
    assert not app.button[1].disabled
