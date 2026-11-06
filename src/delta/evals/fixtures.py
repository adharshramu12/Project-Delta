"""A local web site for browser evals: static pages from evals/fixtures/site on 127.0.0.1, plus two
endpoints that record what a task did, so a checker can read the page state afterwards.

  GET /submit?<fields>      a form submission: fields are recorded under "submit.<field>"
  GET /api/mark?<k>=<v>     a page action (a button's fetch): recorded as k = v
  GET /api/state            everything recorded, as JSON

Nothing here reaches the internet; the port is picked free each run.
"""

from __future__ import annotations

import json
import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

TYPES = {".html": "text/html; charset=utf-8", ".json": "application/json", ".css": "text/css",
         ".js": "text/javascript", ".txt": "text/plain; charset=utf-8", ".png": "image/png"}


class _Handler(SimpleHTTPRequestHandler):
    state: dict[str, str]
    lock: threading.Lock

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path in ("/submit", "/api/mark"):
            prefix = "submit." if parts.path == "/submit" else ""
            with self.lock:
                for k, v in parse_qsl(parts.query, keep_blank_values=True):
                    self.state[prefix + k] = v
            body = (b"<!doctype html><title>Thanks</title><h1>Thank you</h1><p>Your request was received.</p>"
                    if prefix else b'{"ok": true}')
            self._send(body, "text/html" if prefix else "application/json")
            return
        if parts.path == "/api/state":
            with self.lock:
                self._send(json.dumps(self.state).encode(), "application/json")
            return
        super().do_GET()

    def guess_type(self, path: str | os.PathLike[str]) -> str:
        # fixed types: no lookup in the Windows registry, the same answer on every PC
        return TYPES.get(Path(path).suffix.lower(), "application/octet-stream")

    def _send(self, body: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class FixtureSite:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state: dict[str, str] = {}
        self._lock = threading.Lock()
        handler = type("Handler", (_Handler,), {"state": self.state, "lock": self._lock})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), partial(handler, directory=str(root)))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FixtureSite:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    def reset(self) -> None:
        with self._lock:
            self.state.clear()

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self.state)
