"""The dashboard's static files: served with the right types, a strict Content-Security-Policy, nothing loaded from
other sites (it must work offline), no HTML built from text, and every element the scripts look up exists."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from engine_helpers import no_registry
from fastapi.testclient import TestClient

from delta.api.server import ASSETS, CSP, STATIC, create_app, load_token
from delta.bootstrap import build
from delta.config import Settings

HTML = (STATIC / "index.html").read_text(encoding="utf-8")
SCRIPTS = {p.name: p.read_text(encoding="utf-8") for p in STATIC.glob("*.js")}
STYLES = {p.name: p.read_text(encoding="utf-8") for p in STATIC.glob("*.css")}
TEXT_FILES = {"index.html": HTML, **SCRIPTS, **STYLES}
VIEWS = ["chat", "tasks", "apps", "schedules", "documents", "memory", "settings"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    no_registry(monkeypatch, tmp_path)
    c = build(Settings(data_dir=tmp_path))
    with TestClient(create_app(c, load_token(tmp_path))) as tc:
        yield tc


class Tags(HTMLParser):
    """Every start tag of index.html with its attributes, read by the standard HTML parser."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.script_text: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._in_script = tag == "script"

    def handle_endtag(self, tag: str) -> None:
        self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.script_text.append(data)


def parsed() -> Tags:
    t = Tags()
    t.feed(HTML)
    return t


def referenced() -> list[str]:
    out = []
    for tag, attrs in parsed().tags:
        url = attrs.get("src") if tag == "script" else attrs.get("href") if tag == "link" else None
        if url and url.startswith("/"):
            out.append(url)
    return out


def test_every_file_the_page_loads_is_served_with_its_type(client):
    paths = referenced()
    assert "/ui/core.js" in paths and "/ui/app.css" in paths and "/tasks.js" in paths
    for path in paths:
        r = client.get(path)
        assert r.status_code == 200, path
        kind = r.headers["content-type"]
        if path.endswith(".js"):
            assert kind.startswith("text/javascript"), (path, kind)
        elif path.endswith(".css"):
            assert kind.startswith("text/css"), (path, kind)
    # nothing is served that the page does not need, and no request reaches another file
    for bad in ("/ui/index.html", "/ui/delta.ico", "/ui/server.py", "/ui/..%2Fserver.py", "/ui/%2e%2e%2fserver.py",
                "/ui/nope.js"):
        assert client.get(bad).status_code == 404, bad
    assert set(ASSETS) == {p.name for p in STATIC.iterdir() if p.suffix in (".js", ".css")}


def test_the_page_has_a_strict_content_security_policy(client):
    r = client.get("/")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    assert csp == CSP
    rules = dict(part.strip().split(" ", 1) for part in csp.split(";"))
    assert rules["script-src"] == "'self'" and rules["default-src"] == "'self'" and rules["style-src"] == "'self'"
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp and "http" not in csp
    assert rules["frame-ancestors"] == "'none'" and rules["base-uri"] == "'none'"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_no_inline_script_style_or_event_handler():
    # The policy above would block them; this says why a page would break instead of failing quietly.
    t = parsed()
    for tag, attrs in t.tags:
        assert tag != "style", "a <style> element"
        assert tag != "script" or attrs.get("src"), "an inline <script>"
        assert "style" not in attrs, f"a style attribute on <{tag}>"
        assert not [a for a in attrs if a.startswith("on")], f"an event handler attribute on <{tag}>"
    assert not t.script_text


def test_nothing_is_loaded_from_another_site():
    """Works offline on a fresh PC: no CDN, web font, remote image or script. Links people open themselves
    (Telegram's BotFather) and the SVG namespace are the only addresses allowed."""
    allowed = ("https://t.me/BotFather", "http://www.w3.org/2000/svg", "http://127.0.0.1:")
    for name, text in TEXT_FILES.items():
        for url in re.findall(r"(?:https?:)?//[\w.-]+\.[a-z]{2,}[^\s\"'`)<]*", text):
            if url.startswith("//"):
                continue  # a comment, not an address
            assert url.startswith(allowed), f"{name}: {url}"
        if name.endswith(".css"):
            assert "@import" not in text and "url(" not in text, name
    for tag, attrs in parsed().tags:
        for key in ("src", "href"):
            url = attrs.get(key) or ""
            assert tag == "a" or not re.match(r"(?:https?:)?//", url), f"<{tag} {key}={url}>"


def test_no_html_is_built_from_text():
    """Text from the user, the AI, web pages and apps is set with textContent only."""
    banned = re.compile(r"\.innerHTML\b|\.outerHTML\b|insertAdjacentHTML|document\.write|\beval\(|new Function\(|"
                        r"setTimeout\(\s*['\"`]|setInterval\(\s*['\"`]|\.setAttribute\(\s*['\"](?:on\w+|style)['\"]")
    for name, text in SCRIPTS.items():
        assert not banned.search(text), f"{name}: {banned.search(text).group(0)}"  # type: ignore[union-attr]


def test_every_element_a_script_looks_up_exists():
    ids = set(re.findall(r'\bid="([\w-]+)"', HTML))
    for text in SCRIPTS.values():
        ids |= set(re.findall(r'\bid: "([\w-]+)"', text))
    for name, text in SCRIPTS.items():
        for wanted in re.findall(r'(?:\$|getElementById)\("([\w-]+)"\)', text):
            assert wanted in ids, f"{name} looks up #{wanted}, which is not on the page"


def test_every_view_has_a_nav_item_and_a_section():
    for v in VIEWS:
        assert f'data-go="{v}"' in HTML and f'data-view="{v}"' in HTML, v
        assert f"views.{v} =" in "".join(SCRIPTS.values()), v


def test_plain_copy_no_em_dashes_or_emoji():
    emoji = re.compile("[\u2600-\u27bf\U0001f300-\U0001faff\u2b50\u2705\u274c]")
    for name, text in TEXT_FILES.items():
        assert "\u2014" not in text, f"{name} has an em dash"
        assert not emoji.search(text), f"{name}: {emoji.search(text).group(0)!r}"  # type: ignore[union-attr]


def test_the_payload_stays_small():
    """Low-end laptops: the dashboard's own code and styles (icons excluded) stay small, with no framework.
    Raised from 215 KB to 221 KB for Settings > Logins and watchers (logins.js and its card, about 6.3 KB after
    deduplication), then to 222 KB for Stop / Stop all on running tasks in the Tasks view (about 0.9 KB), then to
    224 KB for email approvals (Approve / Edit / Cancel with the full draft) and Apps > Advanced: custom MCP servers
    (the "mcp" add-on moved out of Settings), about 1.6 KB together. With both changes the dashboard ships about
    223.1 KB (223,111 bytes). Then to 226 KB for saved logins: the per-login "simple text captcha" switch on the add
    form and each card, Replace for a name saved before, the no-password warning, and the captcha picture shown with
    a sign-in's answer in Chat (about 2.2 KB; the dashboard then ships about 225.3 KB). Then to 229 KB for "Start Delta
    when I sign in to Windows": the switch in Settings > Setup and shutdown and its offer on the setup wizard's
    Telegram step (about 3 KB; the dashboard then ships about 228.4 KB). Lazy loading would not lower this total: it
    counts every file the dashboard ships."""
    total = sum(len(t.encode("utf-8")) for t in TEXT_FILES.values())
    assert total < 229_000, total
    assert not list(STATIC.glob("*.map")) and not list(Path(STATIC).glob("node_modules"))


def test_the_theme_follows_windows_and_respects_motion_and_contrast_settings():
    tokens = STYLES["tokens.css"]
    assert '[data-theme="dark"]' in tokens and "--accent:" in tokens
    assert "prefers-color-scheme: dark" in SCRIPTS["theme.js"]
    css = STYLES["app.css"]
    assert "prefers-reduced-motion" in css and "forced-colors: active" in css and ":focus-visible" in css


def _tokens(selector: str) -> dict[str, str]:
    css = STYLES["tokens.css"]
    start = css.index(selector + " {")
    return dict(re.findall(r"--([\w-]+):\s*(#[0-9a-f]{6})", css[start:css.index("}", start)]))


def _contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_text_colors_meet_wcag_aa(theme):
    light = _tokens(":root")
    t = light if theme == "light" else light | _tokens('[data-theme="dark"]')
    pairs = [("text", "surface"), ("text-2", "surface"), ("text-3", "surface"), ("text-3", "bg"),
             ("text-3", "surface-2"), ("text-2", "surface-3"), ("accent-text", "surface"),
             ("accent-text", "accent-soft"), ("accent-fg", "accent"), ("accent-fg", "accent-hover"),
             ("ok", "ok-soft"), ("warn", "warn-soft"), ("danger", "danger-soft"), ("danger", "surface"),
             ("text-3", "warn-soft")]
    for fg, bg in pairs:
        assert _contrast(t[fg], t[bg]) >= 4.5, f"{theme}: {fg} on {bg} is {_contrast(t[fg], t[bg]):.2f}"


def test_the_page_itself_never_scrolls():
    """Only the panes inside scroll. The shell clips (overflow:clip cannot be scrolled by scrollIntoView, hidden can)
    and contains its absolutely placed children (visually hidden labels), so nothing makes the page taller than the
    window.
    The real-browser check is tests/e2e/test_dashboard_e2e.py::test_dashboard_fills_the_window."""
    css = STYLES["app.css"]
    shell = re.search(r"^\.app\{([^}]*)\}", css, re.M)
    assert shell, "the .app shell rule"
    rules = shell.group(1)
    assert "overflow:clip" in rules and "position:relative" in rules and "height:100vh" in rules, rules
    assert "body{" in css and "overflow:hidden" in re.search(r"^body\{([^}]*)\}", css, re.M).group(1)  # type: ignore[union-attr]
