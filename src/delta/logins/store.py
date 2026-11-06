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

    @property
    def site(self) -> Origin:
        return parse_origin(self.origin)

    def public(self, with_username: bool = True) -> dict[str, Any]:
        """Everything but the password (the model gets with_username=False: only whether there is one)."""
        out: dict[str, Any] = {"id": self.id, "name": self.name, "origin": self.origin, "login_url": self.login_url,
                               "permission": self.permission, "permission_words": PERMISSION_WORDS[self.permission],
                               "has_password": bool(self.password), "last_used": self.last_used,
                               "last_outcome": self.last_outcome, "insecure": not self.site.secure,
                               "own_browser": sorted(self.own_browser)}
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
        return Login(id=lid, name=str(data.get("name") or lid), origin=str(data["origin"]),
                     username=str(data.get("username") or ""), password=str(data.get("password") or ""),
                     login_url=str(data.get("login_url") or ""), permission=perm if perm in PERMISSIONS else "ask",
                     created=str(data.get("created") or ""), last_used=str(data.get("last_used") or ""),
                     last_outcome=str(data.get("last_outcome") or ""), own_browser=own)

    def list(self) -> list[Login]:
        if not self.folder.exists():
            return []
        out = [x for p in sorted(self.folder.glob("*.bin")) if (x := self._load(p.stem)) is not None]
        return sorted(out, key=lambda x: x.name.lower())

    def count(self) -> int:
        """How many logins are saved, without opening any (cheap: asked before every model call)."""
        return len(list(self.folder.glob("*.bin"))) if self.folder.exists() else 0

    def get(self, ref: str) -> Login | None:
        """By id, by name (any case) or by site (host or address)."""
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

    def for_origin(self, url: str) -> builtins.list[Login]:
        page = origin_of(url)
        return [x for x in self.list() if page is not None and x.site == page]

    # --- writing ---------------------------------------------------------------------------------------
    def _save(self, login: Login) -> None:
        data = {"name": login.name, "origin": login.origin, "username": login.username, "password": login.password,
                "login_url": login.login_url, "permission": login.permission, "created": login.created,
                "last_used": login.last_used, "last_outcome": login.last_outcome, "own_browser": login.own_browser}
        try:
            self.box.save(login.id, data)
        except SecretsUnavailableError as e:
            raise VaultMissingError(NOT_SAVED) from e

    def add(self, name: str, url: str, username: str, password: str, *, login_url: str = "",
            permission: str = "ask", allow_http: bool = False, replace: bool = False) -> Login:
        """Save a login bound to the exact origin of `url`. LoginError with a plain reason when it cannot be."""
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
            raise LoginError(f"a login called '{existing.name}' already exists; remove it first or pick another name")
        if existing is None and len(self.list()) >= MAX_LOGINS:
            raise LoginError(f"at most {MAX_LOGINS} logins can be saved")
        login = Login(id=lid, name=name[:60], origin=str(site), username=username.strip(), password=password,
                      login_url=login_url.strip(), permission=permission,
                      created=existing.created if existing else _now())
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
