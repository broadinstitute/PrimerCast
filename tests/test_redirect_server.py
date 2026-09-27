"""Tests for gui.redirect_server (stdlib-only; no Streamlit)."""

import http.client
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

# gui/ lives at the repo root (not under src/), so make it importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gui.redirect_server import build_location, make_handler  # noqa: E402

BASE = "https://primercast.sabeti.broadinstitute.org"


@pytest.mark.parametrize(
    "base, path, expected",
    [
        (BASE, "/", BASE + "/"),
        (BASE + "/", "/", BASE + "/"),
        (BASE, "/results?run=abc&x=1", BASE + "/results?run=abc&x=1"),
        (BASE, "foo", BASE + "/foo"),
    ],
)
def test_build_location(base, path, expected):
    assert build_location(base, path) == expected


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(BASE))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.mark.parametrize("method", ["GET", "HEAD", "POST"])
def test_server_redirects_with_path_and_query(server, method):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
    conn.request(method, "/some/page?q=1")
    resp = conn.getresponse()
    body = resp.read()
    conn.close()

    assert resp.status == 301
    assert resp.getheader("Location") == BASE + "/some/page?q=1"
    if method == "HEAD":
        assert body == b""
    else:
        assert b"has moved" in body
