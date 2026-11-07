"""Is there a newer Delta? Asked of GitHub, never more than needed, and safe on any network.

Where the answer comes from:
  1. GitHub Releases of the official repository: the latest release (drafts and pre-releases are never offered,
     unless the person chose the beta channel, which also counts pre-releases). Its notes become the highlights.
  2. No release yet: the `version` in pyproject.toml on the main branch (raw.githubusercontent.com), with the
     matching section of CHANGELOG.md as the highlights.

What is sent: plain HTTPS GETs with a `Delta/<version>` User-Agent. No id, no usage data, nothing about this PC.
Proxies set for Windows or in HTTPS_PROXY are used. GitHub's answers are cached with their ETag (an unchanged
answer costs nothing against GitHub's rate limit); a rate limit or an offline PC backs off instead of retrying.

Installing is never done here. `resolve()` only works out exactly what an update would install: the commit the
release tag points to, and the SHA-256 of that commit's pyproject.toml and uv.lock, which the installer checks
against the files it downloads (scripts/install.ps1 -ExpectSha256) before it changes anything.
"""

from __future__ import annotations

import contextlib
import html
import json
import os
import random
import re
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from delta import __version__
from delta.config import Settings
from delta.system.logging import log
from delta.updates.version import is_newer, parse

OFFICIAL_REPO = "adharshramu12/Project-Delta"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
STATE_FILE = "updates.json"
TIMEOUT = httpx.Timeout(10.0, connect=6.0)
MAX_BYTES = 3_000_000  # uv.lock is about 130 KB; anything near this is not a file Delta asked for
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FORCE_GAP_S = 60  # "Check now" pressed again within a minute answers from the last check
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def user_agent() -> str:
    return f"Delta/{__version__}"


def repo() -> str:
    """The official repository. DELTA_REPO (a fork, owner/name) is honoured, as the installer honours it."""
    env = os.environ.get("DELTA_REPO", "").strip()
    return env if REPO_RE.match(env) and ".." not in env else OFFICIAL_REPO


def _test_base(name: str, default: str) -> str:
    """DELTA_UPDATE_API / DELTA_UPDATE_RAW point the checker at a stand-in GitHub on this PC (the tests use it). Only
    an address on this PC is accepted, so the setting cannot send Delta to some other site."""
    env = os.environ.get(name, "").strip().rstrip("/")
    if env:
        parts = urlsplit(env)
        if parts.scheme in ("http", "https") and parts.hostname in _LOOPBACK and not parts.query:
            return env
        log.warning("updates.ignored_override", name=name)
    return default


def api_base() -> str:
    return _test_base("DELTA_UPDATE_API", API)


def raw_base() -> str:
    return _test_base("DELTA_UPDATE_RAW", RAW)


def disabled_by_env() -> bool:
    """DELTA_NO_UPDATE_CHECK=1: never ask GitHub (the test suite, locked-down PCs)."""
    return os.environ.get("DELTA_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes")


class UpdateError(Exception):
    """Why a check or an update plan failed, in words for people. kind: offline | rate_limited | bad_answer |
    integrity. retry_at: when GitHub said to come back (a rate limit)."""

    def __init__(self, message: str, kind: str = "offline", retry_at: float = 0.0) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_at = retry_at


@dataclass
class Release:
    version: str
    tag: str = ""          # the release's tag (v0.3.0); empty when it came from main's pyproject.toml
    source: str = "release"  # release | main
    notes: str = ""        # plain text, cleaned (sanitize_notes)
    highlights: list[str] = field(default_factory=list)
    url: str = ""          # the release page on github.com, when there is one
    prerelease: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Release | None:
        if not isinstance(d, dict) or parse(str(d.get("version", ""))) is None:
            return None
        return cls(version=str(d["version"]), tag=str(d.get("tag", "")), source=str(d.get("source", "release")),
                   notes=str(d.get("notes", ""))[:4000],
                   highlights=[str(x)[:200] for x in d.get("highlights", []) if isinstance(x, str)][:6],
                   url=str(d.get("url", "")), prerelease=bool(d.get("prerelease", False)))


@dataclass
class Target:
    """Exactly what an update installs: one commit of one repository, and the hashes of the two files that decide
    every package installed (uv.lock pins each package with its own hash)."""
    repo: str
    sha: str
    version: str
    pyproject_sha256: str
    lock_sha256: str

    def expect_arg(self) -> str:
        return f"pyproject.toml={self.pyproject_sha256};uv.lock={self.lock_sha256}"


# ---- release notes as plain text ---------------------------------------------------------------------------------
_TAG = re.compile(r"<[^>]{0,500}>")
_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]\n]{1,300})\]\([^)\n]*\)")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")


def sanitize_notes(text: str, limit: int = 4000) -> str:
    """Release notes or a CHANGELOG section as plain text: no HTML, no pictures, link text without the address, no
    control or direction-changing characters. The dashboard sets it as text (never HTML) as well."""
    t = (text or "")[:20000].replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = _TAG.sub("", t)
    t = html.unescape(t)
    t = _TAG.sub("", t)  # escaped tags that unescaping just turned back into tags
    t = _IMG.sub("", t)
    t = _LINK.sub(r"\1", t)
    t = _CTRL.sub("", t)
    t = t.replace("\u2014", " - ").replace("\u2013", "-")
    lines = []
    for line in t.split("\n"):
        line = re.sub(r"^#{1,6}\s*", "", line.rstrip())
        line = line.replace("**", "").replace("__", "").replace("`", "")
        lines.append(line)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out[:limit].rstrip()


def highlights_of(notes: str, count: int = 4) -> list[str]:
    """The first few bullet points (or lines) of cleaned notes, each short enough for a banner."""
    lines = [ln for ln in notes.split("\n") if ln.strip()]
    bullets = [_BULLET.sub("", ln).strip() for ln in lines if _BULLET.match(ln) and not ln.startswith("    ")]
    picked = bullets or [ln.strip() for ln in lines if not ln.strip().endswith(":")]
    out = []
    for ln in picked[:count]:
        ln = re.sub(r"\s+", " ", ln)
        out.append(ln if len(ln) <= 160 else ln[:157].rstrip() + "...")
    return out


def changelog_section(changelog: str, version: str) -> str:
    """The `## <version>` section of a Keep a Changelog file, without its heading."""
    m = re.search(rf"(?m)^##\s+\[?v?{re.escape(version)}\]?(?:\s.*)?$", changelog)
    if not m:
        return ""
    rest = changelog[m.end():]
    nxt = re.search(r"(?m)^##\s", rest)
    return rest[:nxt.start()] if nxt else rest


def pyproject_version(text: str) -> str:
    try:
        v = str(tomllib.loads(text)["project"]["version"])
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as e:
        raise UpdateError("GitHub's pyproject.toml has no version Delta can read.", "bad_answer") from e
    if parse(v) is None:
        raise UpdateError(f"GitHub's pyproject.toml has a version Delta cannot read ({v[:40]}).", "bad_answer")
    return v


# ---- the checker -------------------------------------------------------------------------------------------------
class UpdateChecker:
    """Checks, remembers the answer in <data>/updates.json, and plans an update. Thread-safe enough for Delta: one
    background loop and the dashboard's buttons, each call reads and writes the small state file whole."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None,
                 clock: Any = time.time) -> None:
        self.settings = settings
        self.transport = transport
        self.clock = clock
        self.path = settings.data_dir / STATE_FILE

    # -- state
    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, state: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix="updates.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, self.path)

    def update_state(self, **changes: Any) -> dict[str, Any]:
        state = self.load()
        state.update(changes)
        self.save(state)
        return state

    @property
    def enabled(self) -> bool:
        return self.settings.updates.check and not disabled_by_env()

    # -- HTTP
    def _client(self) -> httpx.Client:
        # trust_env: HTTPS_PROXY and the proxy set in Windows (urllib reads it from the registry) are used. A stand-in
        # on this PC (the tests) is asked directly: a proxy must not answer for it.
        local = urlsplit(api_base()).hostname in _LOOPBACK
        return httpx.Client(timeout=TIMEOUT, transport=self.transport, follow_redirects=True, trust_env=not local,
                            headers={"User-Agent": user_agent()})

    def _allowed_host(self, url: httpx.URL) -> bool:
        allowed = {urlsplit(api_base()).hostname, urlsplit(raw_base()).hostname}
        local = url.host in _LOOPBACK and url.scheme in ("http", "https")
        return (url.scheme == "https" or local) and url.host in allowed

    def get(self, url: str, accept: str = "application/vnd.github+json", cache: dict[str, Any] | None = None
            ) -> tuple[int, bytes]:
        """One GET. 304 answers from `cache` (url -> [etag, text]). Rate limits and network trouble raise
        UpdateError; any other status is returned for the caller to judge."""
        headers = {"Accept": accept}
        if accept.startswith("application/vnd.github"):
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        cached = (cache or {}).get(url)
        if cached and isinstance(cached, list) and len(cached) == 2:
            headers["If-None-Match"] = str(cached[0])
        try:
            with self._client() as client:
                r = client.get(url, headers=headers)
                body = r.read()
        except httpx.TimeoutException as e:
            raise UpdateError("GitHub did not answer in time.", "offline") from e
        except (httpx.HTTPError, OSError, ValueError) as e:
            raise UpdateError(f"Could not reach GitHub ({type(e).__name__}). This PC may be offline.", "offline") from e
        if not self._allowed_host(r.url):
            raise UpdateError("GitHub sent Delta to another site. Nothing was used from it.", "bad_answer")
        if len(body) > MAX_BYTES:
            raise UpdateError("GitHub's answer was far too large. Nothing was used from it.", "bad_answer")
        if r.status_code == 304 and cached:
            return 200, str(cached[1]).encode("utf-8")
        if r.status_code == 429 or (r.status_code == 403 and (r.headers.get("x-ratelimit-remaining") == "0"
                                                              or b"rate limit" in body.lower())):
            raise UpdateError("GitHub asked Delta to wait before checking again (rate limit).", "rate_limited",
                              self._retry_at(r.headers))
        if r.status_code >= 500:
            raise UpdateError(f"GitHub had a problem ({r.status_code}). Delta will try again later.", "offline")
        if cache is not None and r.status_code == 200 and (etag := r.headers.get("etag")):
            cache[url] = [etag, body.decode("utf-8", "replace")]
        return r.status_code, body

    def _retry_at(self, headers: httpx.Headers) -> float:
        now = self.clock()
        wait = 3600.0
        with contextlib.suppress(ValueError, TypeError):
            if headers.get("retry-after"):
                wait = float(headers["retry-after"])
            elif headers.get("x-ratelimit-reset"):
                wait = float(headers["x-ratelimit-reset"]) - now
        return now + min(max(wait, 600.0), 86400.0)  # at least 10 minutes, at most a day

    def _json(self, url: str, cache: dict[str, Any]) -> tuple[int, Any]:
        status, body = self.get(url, cache=cache)
        if status != 200:
            return status, None
        try:
            return status, json.loads(body)
        except ValueError as e:
            raise UpdateError("GitHub's answer was not readable.", "bad_answer") from e

    def _text(self, url: str) -> str:
        status, body = self.get(url, accept="text/plain")
        if status != 200:
            raise UpdateError(f"GitHub did not have {url.rsplit('/', 1)[-1]} ({status}).", "bad_answer")
        return body.decode("utf-8", "replace")

    # -- what is the newest version
    def latest(self, cache: dict[str, Any] | None = None) -> Release:
        cache = {} if cache is None else cache
        rp, api = repo(), api_base()
        if self.settings.updates.channel == "beta":
            status, data = self._json(f"{api}/repos/{rp}/releases?per_page=10", cache)
            if status == 200 and isinstance(data, list):
                found = [r for r in (self._release(x, beta=True) for x in data) if r is not None]
                if found:
                    return max(found, key=lambda r: parse(r.version).key())  # type: ignore[union-attr]
            elif status not in (200, 404):
                raise UpdateError(f"GitHub answered {status} for the releases.", "bad_answer")
        else:
            status, data = self._json(f"{api}/repos/{rp}/releases/latest", cache)
            if status == 200:
                rel = self._release(data, beta=False)
                if rel is not None:
                    return rel
            elif status != 404:  # 404: this repository has no release yet
                raise UpdateError(f"GitHub answered {status} for the latest release.", "bad_answer")
        return self._from_main()

    def _release(self, data: Any, beta: bool) -> Release | None:
        if not isinstance(data, dict) or data.get("draft"):
            return None
        tag = str(data.get("tag_name") or "")
        v = parse(tag)
        if v is None or len(tag) > 64:
            return None
        if (data.get("prerelease") or v.prerelease) and not beta:
            return None
        notes = sanitize_notes(str(data.get("body") or ""))
        url = str(data.get("html_url") or "")
        if not re.fullmatch(rf"https://github\.com/{re.escape(repo())}/releases/tag/[\w.+-]+", url):
            url = ""
        return Release(version=str(v), tag=tag, source="release", notes=notes, highlights=highlights_of(notes),
                       url=url, prerelease=bool(data.get("prerelease") or v.prerelease))

    def _from_main(self) -> Release:
        base = f"{raw_base()}/{repo()}/main"
        version = pyproject_version(self._text(f"{base}/pyproject.toml"))
        notes = ""
        if is_newer(version, __version__):  # the notes are only worth a download when they will be shown
            with contextlib.suppress(UpdateError):
                notes = sanitize_notes(changelog_section(self._text(f"{base}/CHANGELOG.md"), version))
        return Release(version=version, source="main", notes=notes, highlights=highlights_of(notes))

    # -- the scheduled check
    def due(self) -> float:
        """Seconds until the next check is due (0 = now)."""
        state = self.load()
        return max(0.0, max(float(state.get("next_at", 0)), float(state.get("rate_limited_until", 0))) - self.clock())

    def check(self, force: bool = False) -> dict[str, Any]:
        """Ask GitHub when a check is due (or `force`, at most once a minute), remember the answer, return view()."""
        if not self.enabled:
            return self.view()
        state = self.load()
        now = self.clock()
        if now < float(state.get("rate_limited_until", 0)):
            return self.view()
        if not force and now < float(state.get("next_at", 0)):
            return self.view()
        if force and now - float(state.get("checked_at", 0)) < FORCE_GAP_S and not state.get("error"):
            return self.view()
        etags = state.get("etags")
        cache: dict[str, Any] = etags if isinstance(etags, dict) else {}
        interval = self.settings.updates.interval_hours * 3600
        try:
            rel = self.latest(cache)
            state.update(latest=asdict(rel), failures=0, error="", rate_limited_until=0,
                         next_at=now + interval * random.uniform(0.9, 1.1))  # nosec B311: jitter, not security
            log.info("updates.checked", latest=rel.version, source=rel.source, newer=is_newer(rel.version, __version__))
        except UpdateError as e:
            failures = int(state.get("failures", 0)) + 1
            backoff = min(interval, 1800.0 * 2 ** min(failures - 1, 8)) * random.uniform(0.8, 1.2)  # nosec B311
            state.update(failures=failures, error=str(e), next_at=max(now + backoff, e.retry_at))
            if e.retry_at:
                state["rate_limited_until"] = e.retry_at
            log.info("updates.check_failed", kind=e.kind, failures=failures)
        state["etags"] = {k: v for k, v in list(cache.items())[-6:]}  # a few answers, not a growing file
        state["checked_at"] = now
        self.save(state)
        return self.view()

    def view(self) -> dict[str, Any]:
        """What the dashboard, Telegram and `delta doctor` show."""
        state = self.load()
        now = self.clock()
        rel = Release.from_dict(state.get("latest"))
        newer = bool(rel and is_newer(rel.version, __version__))
        whats_new = state.get("whats_new") if isinstance(state.get("whats_new"), dict) else None
        if whats_new and str(whats_new.get("version")) != __version__:
            whats_new = None  # the update did not happen (yet), or this is an even newer version
        return {
            "current": __version__,
            "enabled": self.enabled,
            "channel": self.settings.updates.channel,
            "telegram": self.settings.updates.telegram,
            "checked_at": state.get("checked_at") or None,
            "error": state.get("error", "") if self.enabled else "",
            "rate_limited_until": state.get("rate_limited_until") or None,
            "latest": rel.version if rel else None,
            "source": rel.source if rel else None,
            "newer": newer,
            "notes": rel.notes if rel and newer else "",
            "highlights": rel.highlights if rel and newer else [],
            "url": rel.url if rel and newer else "",
            "prerelease": bool(rel and rel.prerelease),
            "skipped": state.get("skipped", ""),
            "show_banner": bool(newer and rel and self.enabled and state.get("skipped") != rel.version
                                and now >= float(state.get("later_until", 0))),
            "whats_new": whats_new,
        }

    def latest_release(self) -> Release | None:
        return Release.from_dict(self.load().get("latest"))

    # -- planning an update (never installs)
    def resolve(self, rel: Release) -> Target:
        """The commit the release tag (or main, before any release) points to, and its two files' hashes. The
        version in that commit's pyproject.toml must be the one offered, so a moved tag or a changed main branch is
        caught before anything is downloaded for real."""
        ref = rel.tag if rel.source == "release" and rel.tag else "main"
        target = resolve_ref(self, ref)
        if rel.source == "release" and parse(target.version) != parse(rel.version):
            raise UpdateError(f"The release says {rel.version} but its files say {target.version}. Nothing was "
                              "installed.", "integrity")
        if rel.source == "main" and not is_newer(target.version, __version__):
            raise UpdateError("The main branch changed and is no longer newer than this Delta.", "integrity")
        return target


def resolve_ref(checker: UpdateChecker, ref: str) -> Target:
    """A branch, tag or full commit id of repo() as a Target pinned to one commit."""
    rp = repo()
    ref = ref.strip()
    if SHA_RE.match(ref.lower()):
        sha = ref.lower()
    else:
        if not re.fullmatch(r"[\w./+-]{1,100}", ref) or ".." in ref:
            raise UpdateError("That version name is not one Delta can look up.", "bad_answer")
        status, body = checker.get(f"{api_base()}/repos/{rp}/commits/{quote(ref, safe='')}",
                                   accept="application/vnd.github.sha")
        sha = body.decode("ascii", "replace").strip().lower()
        if status == 404 or status == 422:
            raise UpdateError(f"GitHub has no version called {ref} in {rp}.", "bad_answer")
        if status != 200 or not SHA_RE.match(sha):
            raise UpdateError(f"GitHub did not say which commit {ref} is ({status}).", "bad_answer")
    base = f"{raw_base()}/{rp}/{sha}"
    status, py = checker.get(f"{base}/pyproject.toml", accept="text/plain")
    status2, lock = checker.get(f"{base}/uv.lock", accept="text/plain")
    if status != 200 or status2 != 200:
        raise UpdateError(f"GitHub did not have Delta's files for commit {sha[:12]}.", "bad_answer")
    version = pyproject_version(py.decode("utf-8", "replace"))
    try:
        locked = tomllib.loads(lock.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise UpdateError("uv.lock from GitHub is not a lock file. Nothing was installed.", "integrity") from e
    if not any(isinstance(p, dict) and p.get("name") == "delta-agent" for p in locked.get("package", [])):
        raise UpdateError("uv.lock from GitHub is not Delta's lock file. Nothing was installed.", "integrity")
    return Target(repo=rp, sha=sha, version=version, pyproject_sha256=sha256(py).hexdigest(),
                  lock_sha256=sha256(lock).hexdigest())


def state_path(data_dir: Path) -> Path:
    return data_dir / STATE_FILE
