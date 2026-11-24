"""Live preview of a Build project (milestone 3): the page the project makes, shown in the Build page.

Two kinds, one at a time per project:
- static: for a plain HTML/CSS/JS project, Delta serves the project folder itself, read-only, on 127.0.0.1 and a free
  port. It serves only files inside the project (the same jail as the coding tools), never secret files or .git, and
  only answers GET and HEAD. No approval is needed: nothing runs and nothing leaves the PC.
- server: the project's own dev server (`npm run dev`, `python app.py`, ...), started in the project folder like
  run_command (the user's OK first, Delta's keys left out of its environment), but kept running. Delta reads its
  output for the address it prints; only an address on this PC (localhost, 127.0.0.1, [::1], 0.0.0.0) is accepted.

The Build page shows the address in a sandboxed frame (the dashboard allows frames from this PC only). A preview is
stopped with Stop preview, when another preview starts, when the project is removed, and when Delta shuts down.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from delta.studio.jail import JailError, check_file
from delta.system import proctree
from delta.system.logging import log
from delta.tools.shell import scrubbed_env

# What the static preview serves, by extension; anything else is sent as plain bytes (never run by the browser).
TYPES = {".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
         ".json": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8",
         ".md": "text/plain; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
         ".webp": "image/webp", ".ico": "image/x-icon", ".avif": "image/avif", ".woff": "font/woff",
         ".woff2": "font/woff2",
         ".ttf": "font/ttf", ".otf": "font/otf", ".mp3": "audio/mpeg", ".wav": "audio/wav", ".mp4": "video/mp4",
         ".webm": "video/webm", ".pdf": "application/pdf", ".wasm": "application/wasm", ".xml": "application/xml",
         ".map": "application/json; charset=utf-8"}
START_WAIT_S = 90       # how long a dev server gets to print its address and answer
LOG_LINES = 300
LOCAL_URL = re.compile(r"https?://(localhost|127\.0\.0\.1|\[::1\]|0\.0\.0\.0):(\d{2,5})[^\s\"'<>]*", re.I)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _static_handler(root: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "DeltaPreview"
        sys_version = ""

        def _file(self) -> tuple[Path, str] | None:
            rel = unquote(urlsplit(self.path).path).lstrip("/") or "index.html"
            try:
                path, rel = check_file(root, rel)
            except JailError:
                return None
            if path.is_dir():
                path = path / "index.html"
                try:
                    path, rel = check_file(root, path.relative_to(root).as_posix())
                except (JailError, ValueError):
                    return None
            return (path, rel) if path.is_file() else None

        def _send(self, body: bool) -> None:
            found = self._file()
            if found is None:
                self.send_error(404, "Not found in this project")
                return
            data = found[0].read_bytes()
            kind = TYPES.get(found[0].suffix.lower(), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if body:
                self.wfile.write(data)

        def do_GET(self) -> None:
            self._send(True)

        def do_HEAD(self) -> None:
            self._send(False)

        def log_message(self, *args: Any) -> None:  # requests are not worth Delta's log
            pass

    return Handler


@dataclass
class Preview:
    kind: str                 # static | server
    url: str
    command: str = ""
    started: float = field(default_factory=time.time)
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    _httpd: ThreadingHTTPServer | None = None
    _proc: asyncio.subprocess.Process | None = None
    _reader: asyncio.Task[None] | None = None

    @property
    def alive(self) -> bool:
        if self._httpd is not None:
            return True
        return self._proc is not None and self._proc.returncode is None

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "url": self.url, "command": self.command, "started": self.started,
                "alive": self.alive}

    def tail(self, lines: int = 60) -> str:
        return "\n".join(list(self.logs)[-lines:])

    async def stop(self) -> None:
        if self._httpd is not None:
            httpd, self._httpd = self._httpd, None
            await asyncio.to_thread(httpd.shutdown)
            httpd.server_close()
        if self._proc is not None:
            proc, self._proc = self._proc, None
            if proc.returncode is None:
                await asyncio.to_thread(proctree.kill_process, proc)
        if self._reader is not None:
            self._reader.cancel()


def start_static(root: Path) -> Preview:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _static_handler(root))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="delta-preview", daemon=True).start()
    port = httpd.server_address[1]
    return Preview("static", f"http://127.0.0.1:{port}/", _httpd=httpd)


def local_url(line: str) -> str:
    """The first address on this PC that a dev server printed, made safe for the frame; "" when none."""
    m = LOCAL_URL.search(ANSI.sub("", line))
    if not m:
        return ""
    host = "localhost" if m.group(1).lower() in ("localhost", "0.0.0.0", "[::1]") else "127.0.0.1"
    port = int(m.group(2))
    if not 1024 <= port <= 65535:
        return ""
    return f"http://{host}:{port}/"


async def start_server(command: str, root: Path, wait_s: float = START_WAIT_S) -> Preview:
    """Start the project's dev server and wait until it prints an address on this PC that answers."""
    if sys.platform == "win32":
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        argv = ["/bin/sh", "-c", command]
    proc = await asyncio.create_subprocess_exec(*argv, cwd=str(root), stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                                                env=scrubbed_env())
    proctree.track(getattr(proc, "pid", None))
    pv = Preview("server", "", command=command, _proc=proc)
    found: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def read() -> None:
        assert proc.stdout is not None
        while line := await proc.stdout.readline():
            text = ANSI.sub("", line.decode("utf-8", errors="replace")).rstrip()
            pv.logs.append(text)
            if not found.done() and (url := local_url(text)):
                found.set_result(url)
        if not found.done():
            found.set_result("")

    pv._reader = asyncio.create_task(read(), name="delta-preview-log")
    try:
        url = await asyncio.wait_for(asyncio.shield(found), wait_s)
    except TimeoutError:
        url = ""
    except asyncio.CancelledError:
        await pv.stop()
        raise
    if url and await answers(url):
        pv.url = url
        return pv
    await pv.stop()
    why = ("it stopped" if proc.returncode is not None else
           f"no address on this PC appeared within {wait_s:.0f}s" if not url else f"{url} did not answer")
    raise RuntimeError(f"The dev server did not start ({why}). Its last output:\n{pv.tail(40)}")


async def answers(url: str, tries: int = 20) -> bool:
    import httpx

    async with httpx.AsyncClient(timeout=3, follow_redirects=True) as client:
        for _ in range(tries):
            with contextlib.suppress(httpx.HTTPError):
                if (await client.get(url)).status_code < 500:
                    return True
            await asyncio.sleep(0.5)
    return False


async def page_check(url: str) -> str:
    """What the preview serves at its address, for the agent: the status, the title and the start of the text."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url)
    except httpx.HTTPError as e:
        return f"ERROR: the preview did not answer ({type(e).__name__})."
    text = r.text
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    plain = " ".join(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", text, flags=re.S | re.I).split())
    return (f"HTTP {r.status_code}, title: {title.group(1).strip()[:120] if title else '(none)'}\n"
            f"Text on the page: {plain[:1500] or '(no text: the page may be drawn by JavaScript)'}")


def stop_all(previews: dict[str, Preview]) -> None:
    """At shutdown: stop every preview (no event loop needed)."""
    for pv in list(previews.values()):
        try:
            if pv._httpd is not None:
                pv._httpd.shutdown()
                pv._httpd.server_close()
            if pv._proc is not None and pv._proc.returncode is None:
                proctree.kill_process(pv._proc)
        except Exception:
            log.exception("studio.preview_stop_failed")
    previews.clear()
