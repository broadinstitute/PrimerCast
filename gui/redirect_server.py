"""Tiny HTTP server that 301-redirects every request to a new base URL.

Used by the legacy ``qprimer-designer`` Cloud Run service after the web app moved
to ``primercast``: the same container image starts this server instead of
Streamlit when ``QPRIMER_REDIRECT_URL`` is set (see the Dockerfile ``CMD``), so
old links keep working until the legacy service is deleted. The request path and
query string are preserved, e.g. ``/foo?x=1`` -> ``<base>/foo?x=1``.

This module deliberately depends only on the standard library so it starts
instantly and can be unit tested without importing Streamlit / torch.
"""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def build_location(base_url: str, request_path: str) -> str:
    """Join the redirect base URL with the incoming path + query string."""
    path = request_path if request_path.startswith("/") else "/" + request_path
    return base_url.rstrip("/") + path


def make_handler(base_url: str) -> type[BaseHTTPRequestHandler]:
    class RedirectHandler(BaseHTTPRequestHandler):
        def _redirect(self) -> None:
            location = build_location(base_url, self.path)
            body = (
                f'<p>This site has moved to <a href="{location}">{location}</a>.</p>\n'
            ).encode()
            self.send_response(301)
            self.send_header("Location", location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_HEAD = do_POST = _redirect

    return RedirectHandler


def main() -> None:
    base_url = os.environ["QPRIMER_REDIRECT_URL"]
    port = int(os.environ.get("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(base_url))
    print(f"Redirecting all requests on :{port} to {base_url}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
