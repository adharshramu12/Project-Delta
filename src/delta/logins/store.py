"""Saved site logins, sealed with Windows DPAPI (the same SecretBox the connected apps use).

One sealed file per login in <data dir>/logins/<id>.bin holding the site's exact origin, the optional login page
address, the user name, the password, the permission and when it was last used. Delta's data folder is protected (no
tool can read it) and a sealed file opens only for the same Windows account on the same PC. Without a working DPAPI
saving is refused: a password is never written in plain text.

The password is read only by delta.logins.signin at the moment it is typed into a page whose origin matches. It is
never part of what `public()` returns, never in a repr, a log line, the audit log, a chat or the model's context.
"""

from __future__ import annotations

import builtins
import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from delta.connectors.secrets import SecretBox, SecretsUnavailableError
from delta.logins.origin import Origin, OriginError, is_loopback_host, origin_of, parse_origin, same_origin_login_url

OWN_BROWSERS = ("chrome", "edge")  # the user's own browsers Delta can fill a login in (one-time consent each)
PERMISSIONS = ("ask", "always")  # "allow for this task" is chosen at the prompt and kept in memory only
PERMISSION_WORDS = {"ask": "Ask every time", "always": "Always allow for this site"}
MAX_LOGINS = 50
CAPTCHA_CHOICES = ("on", "off")  # the user's own captcha choice for a login; anything else = never chosen (default)
# Logins saved before the captcha default changed carry only "simple_captcha" (off by default then). Read once, they
# become: on -> "on" (the user turned it on), off -> "off" when the user turned it off (the audit log's
# login.captcha_setting from Settings > Logins, or the file itself for `delta logins captcha NAME --off`, which #40 did
# not audit: see LoginStore._switched_off_in_file), otherwise "" so the new default (on with a password) applies.
CAPTCHA_SCHEMA = 2
# When the per-login captcha switch first shipped (#40, merged 2026-11-07 06:23:46 IST). A login file written after this
# by #40's own code carries "simple_captcha"; one written before it does not carry the key at all.
SWITCH_SHIPPED = datetime(2026, 11, 7, 0, 53, 46, tzinfo=UTC)
WRITE_SLACK_S = 5.0  # a file write within this of a stamp the file carries is the write that set the stamp
# What the dashboard and the CLI show for a login's last outcome (delta.logins.signin.OUTCOMES), in plain words.
OUTCOME_WORDS = {"signed_in": "signed in", "already_signed_in": "already signed in",
                 "wrong_password": "the site said the password is wrong", "refused": "refused (not the saved site)",
                 "needs_user": "waited for you (captcha, code or permission)", "no_form": "no sign-in form found",
                 "failed": "did not finish", "aborted": "stopped (you took over)", "not_allowed": "not allowed"}
# Words that say nothing about which login is meant ("my example university erp portal" -> example, university, erp).
_FILLER = {"my", "the", "a", "an", "to", "into", "in", "on", "of", "for", "portal", "site", "website", "page",
           "account", "login", "log", "sign", "signin", "web", "online", "please", "college", "student"}
_HOST_NOISE = {"www", "com", "in", "org", "net", "edu", "ac", "co", "io", "uk", "gov", "new", "app"}
NOT_SAVED = ("Delta could not encrypt this login with Windows DPAPI, so it was not saved. Delta never stores "
             "passwords unencrypted. Repair Delta by running the installer again or `delta update`.")


class LoginError(ValueError):
    """A login that cannot be saved or found, with a plain reason."""


class VaultMissingError(LoginError):
    """Windows DPAPI is not available: nothing is saved (never plain text)."""


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s[:40]


@dataclass
class Login:
    id: str
    name: str
    origin: str
    username: str = ""
    password: str = field(default="", repr=False)
    login_url: str = ""
    permission: str = "ask"
    created: str = ""
    last_used: str = ""
    last_outcome: str = ""
    # The one-time consent to fill this login in the user's OWN browser (not Delta's private one): browser key
    # (chrome | edge) -> when the user gave it. Asked once per site and browser, even with "always" (service.py).
    own_browser: dict[str, str] = field(default_factory=dict)
    # "This site has a simple text captcha, let Delta read it" (Settings > Logins). The user's own choice for this
    # login: "on", "off", or "" (never chosen: the default applies, ON when a password is saved, owner decision
    # 2026-11-07). When on, a simple image captcha on THIS exact origin is read automatically (Windows OCR first,
    # delta.tools.captcha), like a host allowed with `delta captcha allow`. reCAPTCHA, hCaptcha, Turnstile and other
    # managed challenges are still handed over, never solved.
    captcha_choice: str = ""

    @property
    def simple_captcha(self) -> bool:
        """Delta reads this login's simple captcha: the user said so, or they never chose and a password is saved."""
        if self.captcha_choice in CAPTCHA_CHOICES:
            return self.captcha_choice == "on"
        return bool(self.password)

    @property
    def site(self) -> Origin:
        return parse_origin(self.origin)

    def public(self, with_username: bool = True) -> dict[str, Any]:
        """Everything but the password (the model gets with_username=False: only whether there is one)."""
        out: dict[str, Any] = {"id": self.id, "name": self.name, "origin": self.origin, "login_url": self.login_url,
                               "permission": self.permission, "permission_words": PERMISSION_WORDS[self.permission],
                               "has_password": bool(self.password), "last_used": self.last_used,
                               "last_outcome": self.last_outcome,
                               "last_outcome_words": OUTCOME_WORDS.get(self.last_outcome, self.last_outcome),
                               "insecure": not self.site.secure, "own_browser": sorted(self.own_browser),
                               "simple_captcha": self.simple_captcha,
                               "captcha_default": self.captcha_choice not in CAPTCHA_CHOICES}
        if with_username:
            out["username"] = self.username
        else:
            out["has_username"] = bool(self.username)
        return out

    def __str__(self) -> str:  # never the password
        return f"{self.name} ({self.origin})"


class LoginStore:
    def __init__(self, folder: Path, seal: Callable[[str], str] | None = None,
                 unseal: Callable[[str], str] | None = None) -> None:
        self.box = SecretBox(folder, seal, unseal)
        self.folder = folder
        self.audit_path: Path | None = None  # where the user's past captcha choices are (default: next to logins/)
        self._turned_off: set[str] | None = None

    def available(self) -> bool:
        return self.box.available()

    # --- reading ---------------------------------------------------------------------------------------
    def _load(self, lid: str) -> Login | None:
        data = self.box.load(lid)
        if not data or not data.get("origin"):
            return None
        perm = str(data.get("permission") or "ask")
        raw_own = data.get("own_browser")
        own = ({str(k): str(v) for k, v in raw_own.items() if k in OWN_BROWSERS} if isinstance(raw_own, dict)
               else {})
        login = Login(id=lid, name=str(data.get("name") or lid), origin=str(data["origin"]),
                      username=str(data.get("username") or ""), password=str(data.get("password") or ""),
                      login_url=str(data.get("login_url") or ""), permission=perm if perm in PERMISSIONS else "ask",
                      created=str(data.get("created") or ""), last_used=str(data.get("last_used") or ""),
                      last_outcome=str(data.get("last_outcome") or ""), own_browser=own)
        if int(data.get("captcha_schema") or 0) >= CAPTCHA_SCHEMA:
            choice = str(data.get("captcha_choice") or "")
            login.captcha_choice = choice if choice in CAPTCHA_CHOICES else ""
        else:  # saved before the default changed: migrated once, then written back with the new schema
            login.captcha_choice = ("on" if data.get("simple_captcha") is True else
                                    "off" if lid in self._explicit_off() or self._switched_off_in_file(lid, data)
                                    else "")
            with contextlib.suppress(Exception):
                self._save(login)
        return login

    def _switched_off_in_file(self, lid: str, data: dict[str, Any]) -> bool:
        """A #40-era login file whose stored flag says off and whose last write was the user's own switch, not a save
        Delta stamped: `delta logins captcha NAME --off` (#40 did not audit it) leaves exactly that. The file was
        written after the switch shipped, carries "simple_captcha": false, and its last write time matches none of
        the stamps every other #40 write leaves (created on a new login, last_used on a sign-in, an own-browser
        consent). The other unstamped #40 writes (the permission switch, a Replace) also read as off here: the safe
        side, the user gets the captcha picture and can turn reading back on with one switch."""
        if data.get("simple_captcha") is not False:
            return False
        try:
            written = datetime.fromtimestamp((self.folder / f"{lid}.bin").stat().st_mtime, UTC)
        except (OSError, ValueError):
            return False
        if written < SWITCH_SHIPPED:
            return False
        raw = data.get("own_browser")
        own: dict[str, Any] = raw if isinstance(raw, dict) else {}
        for stamp in (data.get("created"), data.get("last_used"), *own.values()):
            with contextlib.suppress(TypeError, ValueError):
                when = datetime.fromisoformat(str(stamp))
                when = when if when.tzinfo else when.replace(tzinfo=UTC)
                if abs((written - when).total_seconds()) <= WRITE_SLACK_S:
                    return False
        return True

    def _explicit_off(self) -> set[str]:
        """Logins the user turned the captcha switch off for in Settings > Logins (the audit log's
        login.captcha_setting with on=false as the last word for that login). Read once, only for a migration."""
        if self._turned_off is not None:
            return self._turned_off
        import json

        last: dict[str, bool] = {}
        path = self.audit_path or self.folder.parent / "audit.jsonl"
        with contextlib.suppress(Exception), path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if "login.captcha_setting" not in line:
                    continue
                with contextlib.suppress(ValueError):
                    rec = json.loads(line)
                    data = rec.get("data") or {}
                    if rec.get("event") == "login.captcha_setting" and data.get("login"):
                        last[str(data["login"])] = bool(data.get("on"))
        self._turned_off = {lid for lid, on in last.items() if not on}
        return self._turned_off

    def list(self) -> list[Login]:
        if not self.folder.exists():
            return []
        out = [x for p in sorted(self.folder.glob("*.bin")) if (x := self._load(p.stem)) is not None]
        return sorted(out, key=lambda x: x.name.lower())

    def count(self) -> int:
        """How many logins are saved, without opening any (cheap: asked before every model call)."""
        return len(list(self.folder.glob("*.bin"))) if self.folder.exists() else 0

    def get(self, ref: str) -> Login | None:
        """By id, by name (any case) or by site (host or address). Exact only: remove, permission and the captcha
        switch use it, so a vague name never changes the wrong login (sign-ins use resolve())."""
        ref = (ref or "").strip()
        if not ref:
            return None
        items = self.list()
        low = ref.lower()
        for x in items:
            if x.id == slug(ref) or x.name.lower() == low:
                return x
        page = origin_of(ref if "://" in ref else "https://" + ref)
        if page is not None:
            same = [x for x in items if x.site.host == page.host]
            if len(same) == 1:
                return same[0]
        return None

    def resolve(self, ref: str) -> tuple[Login | None, str]:
        """(the one login `ref` means, '') or (None, why not in plain words that name the saved logins).

        Tried in order, the first that finds exactly one wins: the id or the exact name; the site, from a host or
        any address on it (path, query, trailing slash, the default port and a leading "www." do not matter for
        FINDING the login; typing still needs the exact saved origin, delta.logins.origin); then the words, so
        "Example University ERP" finds a login named "euerp" for https://newerp.example-university.edu. Two or more
        matches are never guessed between."""
        ref = (ref or "").strip()
        items = self.list()
        if not ref:
            return None, "say which saved login to use" + _saved(items)
        low = ref.lower()
        for x in items:
            if x.id == slug(ref) or x.name.lower() == low:
                return x, ""
        page = origin_of(ref if "://" in ref else "https://" + ref) if " " not in ref else None
        if page is not None:
            bare = _bare(page.host)
            same = [x for x in items if _bare(x.site.host) == bare]
            if len(same) == 1:
                return same[0], ""
            if len(same) > 1:
                return None, (f"{len(same)} saved logins are for {page.host}: "
                              + "; ".join(_describe(x) for x in same) + ". Say which one by its name.")
        words = [w for w in re.findall(r"[a-z0-9]+", low) if w not in _FILLER]
        if words:
            hits = [x for x in items if _words_match(words, x)]
            if len(hits) == 1:
                return hits[0], ""
            if len(hits) > 1:
                return None, (f"'{ref}' fits {len(hits)} saved logins: " + "; ".join(_describe(x) for x in hits)
                              + ". Say which one by its name.")
        return None, f"no saved login matches '{ref}'" + _saved(items)

    def captcha_allowed(self, url: str) -> bool:
        """A saved login for exactly this page's origin has "let Delta read the simple captcha" switched on."""
        page = origin_of(url)
        return page is not None and any(x.simple_captcha and x.site == page for x in self.list())

    def for_origin(self, url: str) -> builtins.list[Login]:
        page = origin_of(url)
        return [x for x in self.list() if page is not None and x.site == page]

    # --- writing ---------------------------------------------------------------------------------------
    def _save(self, login: Login) -> None:
        data = {"name": login.name, "origin": login.origin, "username": login.username, "password": login.password,
                "login_url": login.login_url, "permission": login.permission, "created": login.created,
                "last_used": login.last_used, "last_outcome": login.last_outcome, "own_browser": login.own_browser,
                "simple_captcha": login.simple_captcha, "captcha_choice": login.captcha_choice,
                "captcha_schema": CAPTCHA_SCHEMA}
        try:
            self.box.save(login.id, data)
        except SecretsUnavailableError as e:
            raise VaultMissingError(NOT_SAVED) from e

    def add(self, name: str, url: str, username: str, password: str, *, login_url: str = "",
            permission: str = "ask", allow_http: bool = False, replace: bool = False,
            simple_captcha: bool | None = None) -> Login:
        """Save a login bound to the exact origin of `url`. LoginError with a plain reason when it cannot be.
        replace=True updates the login with the same name in place (never a second copy). simple_captcha None keeps
        a replaced login's own captcha choice (same site only) or, for a new login, leaves it to the default: ON when
        a password is saved. True / False is the user's explicit choice."""
        if not self.box.available():
            raise VaultMissingError(NOT_SAVED)
        name = (name or "").strip()
        lid = slug(name)
        if not lid:
            raise LoginError("give the login a short name, for example 'College ERP'")
        try:
            site = parse_origin(url)
        except OriginError as e:
            raise LoginError(str(e)) from e
        if not site.secure and not allow_http and not is_loopback_host(site.host):
            raise LoginError(f"{site} uses plain http: anyone on the same network could read the password. Use the "
                             "https address if the site has one, or confirm you want http anyway (--allow-http, or "
                             "tick 'plain http' in the dashboard).")
        if why := same_origin_login_url(site, login_url.strip()):
            raise LoginError(why)
        if not password and not username:
            raise LoginError("give a user name, a password or both")
        if permission not in PERMISSIONS:
            raise LoginError("permission must be 'ask' or 'always'")
        existing = self._load(lid)
        if existing is not None and not replace:
            raise LoginError(f"a login called '{existing.name}' already exists. Save it again with Replace to update "
                             "its password, or pick another name")
        if existing is None and len(self.list()) >= MAX_LOGINS:
            raise LoginError(f"at most {MAX_LOGINS} logins can be saved")
        keep = existing is not None and existing.origin == str(site)
        choice = ("on" if simple_captcha else "off") if simple_captcha is not None else (
            existing.captcha_choice if keep and existing else "")
        login = Login(id=lid, name=name[:60], origin=str(site), username=username.strip(), password=password,
                      login_url=login_url.strip(), permission=permission,
                      created=existing.created if existing else _now(), captcha_choice=choice)
        self._save(login)
        return login

    def remove(self, ref: str) -> Login | None:
        login = self.get(ref)
        if login is None:
            return None
        self.box.delete(login.id)  # overwritten, then deleted
        return login

    def set_permission(self, ref: str, permission: str) -> Login:
        if permission not in PERMISSIONS:
            raise LoginError("permission must be 'ask' or 'always'")
        login = self.get(ref)
        if login is None:
            raise LoginError(f"no saved login called '{ref}'")
        login.permission = permission
        if permission == "ask":
            login.own_browser = {}  # "Ask every time" also withdraws the own-browser consent (docs/LOGINS.md)
        self._save(login)
        return login

    def set_simple_captcha(self, ref: str, on: bool) -> Login:
        """The user's switch "This site has a simple text captcha, let Delta read it" for one login (its exact
        origin only), kept as their explicit choice. Only the dashboard and the CLI call this; no tool can."""
        login = self.get(ref)
        if login is None:
            raise LoginError(f"no saved login called '{ref}'")
        login.captcha_choice = "on" if on else "off"
        self._save(login)
        return login

    def allow_own_browser(self, lid: str, browser: str) -> Login | None:
        """Record the user's one-time consent to fill this login in their own `browser` (chrome | edge)."""
        if browser not in OWN_BROWSERS:
            raise LoginError(f"unknown browser '{browser}'")
        login = self._load(lid)
        if login is None:
            return None
        login.own_browser[browser] = _now()
        self._save(login)
        return login

    def touch(self, lid: str, outcome: str) -> None:
        login = self._load(lid)
        if login is None:
            return
        login.last_used, login.last_outcome = _now(), outcome[:40]
        with contextlib.suppress(LoginError):
            self._save(login)


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _describe(x: Login) -> str:
    return f"'{x.name}' ({x.origin}, {'password saved' if x.password else 'NO password saved'})"


def _saved(items: builtins.list[Login]) -> str:
    return (". Saved logins: " + "; ".join(_describe(x) for x in items)) if items else (
        ". No logins are saved yet: the user adds one on the PC in Settings > Logins (or `delta logins add`)")


def _words_match(words: builtins.list[str], x: Login) -> bool:
    """Every telling word of the request is part of the login's name or its host ("example", "university" and "erp" are
    all inside "euerp newerp.example-university.edu"), and at least one word is more than a host's noise."""
    hay = f"{x.name.lower()} {x.site.host}"
    host_words = set(re.findall(r"[a-z0-9]+", x.site.host)) - _HOST_NOISE
    telling = [w for w in words if len(w) >= 2]
    if not telling or not all(w in hay for w in telling):
        return False
    return any(w in x.name.lower() or any(w in h for h in host_words) for w in telling)
