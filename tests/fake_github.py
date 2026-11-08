"""A stand-in for the parts of GitHub Delta's update check uses: releases, commit lookup by name, and raw files.

Used two ways: as an httpx.MockTransport (unit tests) and as a real HTTP server on 127.0.0.1 (the dashboard e2e test,
through DELTA_UPDATE_API / DELTA_UPDATE_RAW). It records every request so tests can check what Delta sent."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

REPO = "adharshramu12/Project-Delta"


def pyproject(version: str) -> str:
    return f'[project]\nname = "delta-agent"\nversion = "{version}"\n'


LOCK = 'version = 1\nrequires-python = ">=3.12"\n\n[[package]]\nname = "delta-agent"\nsource = { editable = "." }\n'


def sha_for(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()


@dataclass
class FakeGitHub:
    repo: str = REPO
    releases: list[dict[str, Any]] = field(default_factory=list)
    main_version: str = "0.0.1"
    changelog: str = ""
    files: dict[str, dict[str, str]] = field(default_factory=dict)  # commit -> {path: text}
    refs: dict[str, str] = field(default_factory=dict)  # tag or branch -> commit
    rate_limited: bool = False
    requests: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)

    def release(self, tag: str, body: str = "", prerelease: bool = False, draft: bool = False,
                version: str | None = None) -> str:
        """Add a release whose tag points at a commit with that version's files. Returns the commit."""
        sha = sha_for(tag)
        self.refs[tag] = sha
        self.files[sha] = {"pyproject.toml": pyproject(version or tag.lstrip("v")), "uv.lock": LOCK}
        self.releases.insert(0, {"tag_name": tag, "name": tag, "body": body, "draft": draft, "prerelease": prerelease,
                                 "html_url": f"https://github.com/{self.repo}/releases/tag/{tag}"})
        return sha

    def set_main(self, version: str, changelog: str = "") -> str:
        sha = sha_for("main-" + version)
        self.main_version, self.changelog = version, changelog
        self.refs["main"] = sha
        self.files[sha] = {"pyproject.toml": pyproject(version), "uv.lock": LOCK}
        return sha

    # -- one request: (status, headers, body)
    def answer(self, method: str, path: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        self.requests.append((method, path, headers))
        if method != "GET":
            return 405, {}, b""
        if self.rate_limited:
            limited = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}
            return 403, limited, b'{"message": "API rate limit exceeded"}'
        api = f"/repos/{self.repo}"
        if path == f"{api}/releases/latest":
            stable = [r for r in self.releases if not r["draft"] and not r["prerelease"]]
            return self._json(stable[0], headers) if stable else (404, {}, b'{"message": "Not Found"}')
        if path.startswith(f"{api}/releases?"):
            return self._json(self.releases, headers)
        if path.startswith(f"{api}/commits/"):
            ref = path[len(f"{api}/commits/"):]
            sha = self.refs.get(ref)
            return (200, {}, sha.encode()) if sha else (422, {}, b'{"message": "No commit found"}')
        raw = f"/{self.repo}/"
        if path.startswith(raw):
            ref, _, name = path[len(raw):].partition("/")
            if ref == "main":
                text = {"pyproject.toml": pyproject(self.main_version), "CHANGELOG.md": self.changelog}.get(name)
            else:
                text = self.files.get(ref, {}).get(name)
            return (200, {}, text.encode()) if text is not None else (404, {}, b"404: Not Found")
        return 404, {}, b'{"message": "Not Found"}'

    def _json(self, data: Any, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(data).encode()
        etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        if headers.get("if-none-match") == etag:
            return 304, {"etag": etag}, b""
        return 200, {"etag": etag, "content-type": "application/json"}, body

    # -- as an httpx transport
    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.raw_path.decode()
            sent = {k.lower(): v for k, v in request.headers.items()}
            status, headers, body = self.answer(request.method, path, sent)
            return httpx.Response(status, headers=headers, content=body)

        return httpx.MockTransport(handle)

    # -- as a server on 127.0.0.1
    def serve(self) -> tuple[ThreadingHTTPServer, str]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                status, headers, body = fake.answer("GET", self.path, {k.lower(): v for k, v in self.headers.items()})
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"
