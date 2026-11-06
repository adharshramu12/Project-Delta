"""Exact origins for saved logins: where a password may be typed, and nowhere else.

An origin is scheme + host + port, as browsers define it ("https://erp.college.edu"). A saved login is bound to one
origin. Delta types its password only when the page in front of it (and the frame that holds the form) has exactly
that origin. Everything else is refused, with a reason a person can read:

- another host, including lookalikes: erp.college.edu.evil.com, erp-college.edu, xn--rp-college (punycode), a
  different subdomain, an IP address instead of the name;
- an http page when the login was saved for https (a downgrade: anyone on the network could read the password);
- another port;
- a form inside a frame (iframe) from another origin.

Saving an http origin is possible only on purpose (some college portals still use plain http): the person is warned
and must confirm, and then the login is only ever used on that http origin.
"""

from __future__ import annotations

import difflib
import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_PORTS = {"http": 80, "https": 443}


class OriginError(ValueError):
    pass


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int

    def __str__(self) -> str:
        port = "" if DEFAULT_PORTS.get(self.scheme) == self.port else f":{self.port}"
        return f"{self.scheme}://{self.host}{port}"

    @property
    def secure(self) -> bool:
        return self.scheme == "https"


def _host(raw: str) -> str:
    host = (raw or "").strip().lower().rstrip(".")
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii") if not host.isascii() else host
    except UnicodeError:
        return ""
    return host


def parse_origin(url: str) -> Origin:
    """'https://ERP.College.edu/login?x=1' -> Origin('https', 'erp.college.edu', 443). OriginError otherwise."""
    text = (url or "").strip()
    if not text:
        raise OriginError("give the site's address, for example https://erp.college.edu")
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as e:
        raise OriginError(f"'{url}' is not a valid web address") from e
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise OriginError("only http(s) web addresses can hold a login")
    if parts.username or parts.password:
        raise OriginError("the address must not contain a user name or password")
    host = _host(parts.hostname or "")
    if not host or "*" in host or " " in host:
        raise OriginError(f"'{url}' has no usable host name")
    return Origin(scheme, host, port or DEFAULT_PORTS[scheme])


def origin_of(url: str) -> Origin | None:
    try:
        return parse_origin(url) if (url or "").lower().startswith(("http://", "https://")) else None
    except OriginError:
        return None


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def mismatch(saved: Origin, page_url: str) -> str:
    """'' when a password saved for `saved` may be typed on `page_url`; otherwise why not, in plain words."""
    page = origin_of(page_url)
    if page is None:
        return "the page is not a normal web page (http or https), so no saved login is used there"
    if page == saved:
        return ""
    if page.host == saved.host and saved.secure and not page.secure:
        return (f"the page is http, but this login was saved for https ({saved}). Plain http would expose the "
                "password, so Delta refuses (a downgrade)")
    if page.host == saved.host and page.scheme == saved.scheme:
        return f"the page is on port {page.port}, but this login was saved for {saved}"
    if page.host == saved.host:
        return f"the page uses {page.scheme}, but this login was saved for {saved}"
    return f"the page is {page}, not {saved}" + (" (it looks similar: possibly a phishing copy)"
                                                  if lookalike(saved.host, page.host) else "")


def lookalike(saved_host: str, page_host: str) -> bool:
    """A different host that a person could mistake for the saved one (only used to word the warning; any
    different host is refused either way)."""
    if saved_host == page_host:
        return False
    if saved_host in page_host or page_host in saved_host:
        return True
    if page_host.startswith("xn--") or ".xn--" in page_host:
        return True
    a, b = saved_host.split(".")[0], page_host.split(".")[0]
    return difflib.SequenceMatcher(None, saved_host, page_host).ratio() > 0.8 or (
        len(a) > 3 and difflib.SequenceMatcher(None, a, b).ratio() > 0.8)


def is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def same_origin_login_url(saved: Origin, login_url: str) -> str:
    """'' when the optional login page address is on the saved origin; else why not."""
    if not login_url:
        return ""
    page = origin_of(login_url)
    if page is None:
        return "the login page address must start with http:// or https://"
    return "" if page == saved else f"the login page {page} is not on {saved}"
