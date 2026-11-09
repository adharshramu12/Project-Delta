"""Saved logins in the user's OWN browser (delta.logins.own_browser): the same origin rules as the private browser,
the one-time consent per site and browser, tab binding (a tab switch or navigation mid-fill stops it), the fill check
and the password never leaving the form. The browser is a fake driver here; tests/e2e/test_logins_own_browser_e2e.py
runs the same engine in a real Chrome/Edge."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.evals.standin_vault import STANDIN
from delta.logins import mask
from delta.logins.own_browser import FieldRef, OwnBrowserSignIn, TabBinding, classify
from delta.logins.service import OWN_CONSENT_TOOL, LoginService
from delta.logins.store import LoginStore
from delta.logins.tools import SignIn, SignInArgs
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.remote_guard import RemoteGuard
from delta.tools.base import Risk

SECRET = "Own-Canary-Pw-4417!"
USER = "student01"
SITE = "https://erp.college.edu"


# --- a fake own browser -------------------------------------------------------------------------------------------

@dataclass
class Page:
    url: str
    boxes: list[tuple[str, str]] = field(default_factory=list)  # (kind, frame url)
    text: str = ""


@dataclass
class FakeBrowser:
    """Delta's window in the user's browser: tabs of pages; the active tab is what UI Automation would see."""

    tabs: list[Page]
    active: int = 0
    window: str = "w1"
    browser_key: str = "chrome"
    browser_label: str = "Google Chrome"
    values: dict[int, str] = field(default_factory=dict)  # id(box ref) -> what is in the box
    submits: int = 0
    navigations: list[str] = field(default_factory=list)
    secret_boxes: int = 0
    # hooks for the hard cases
    after_set: Any = None  # (browser, field, value) -> None: the user acts while Delta fills
    keeps: Any = None  # (field, value) -> what the box really keeps (an extension may change it)
    on_submit: Any = None  # (browser) -> None: what the site does with the form
    routes: dict[str, Page] = field(default_factory=dict)  # navigate(url) -> page

    @property
    def page(self) -> Page:
        return self.tabs[self.active]

    async def ensure_window(self, url: str) -> bool:
        return True

    async def binding(self) -> TabBinding | None:
        return None if self.window == "" else TabBinding(self.window, f"tab{self.active}")

    async def top_url(self) -> str:
        return self.page.url

    async def navigate(self, url: str) -> None:
        self.navigations.append(url)
        self.tabs[self.active] = self.routes.get(url, Page(url))

    async def fields(self) -> list[FieldRef]:
        return [FieldRef(kind, where, (id(self.page), i), kind) for i, (kind, where) in enumerate(self.page.boxes)]

    async def frame_url(self, f: FieldRef) -> str:
        page_id, i = f.handle
        if page_id != id(self.page) or i >= len(self.page.boxes):
            return ""
        return self.page.boxes[i][1]

    async def set_value(self, f: FieldRef, value: str, secret: bool) -> None:
        kept = self.keeps(f, value) if self.keeps else value
        self.values[hash(f.handle)] = kept
        if self.after_set:
            self.after_set(self, f, value)

    async def value_length(self, f: FieldRef) -> int | None:
        v = self.values.get(hash(f.handle))
        return None if v is None else len(v)

    async def submit(self, f: FieldRef) -> None:
        self.submits += 1
        if self.on_submit:
            self.on_submit(self)

    async def text(self) -> str:
        return self.page.text

    async def challenge(self) -> str | None:
        return "reCAPTCHA" if "recaptcha" in self.page.text.lower() else None

    async def settle(self) -> None:
        return None

    def remember_secret_box(self, f: FieldRef) -> None:
        self.secret_boxes += 1

    async def observe(self) -> str:
        return f"{self.page.text} ({self.page.url})"

    def typed(self) -> list[str]:
        return [v for v in self.values.values() if v]


def login_page(url: str = SITE + "/login", frame: str | None = None) -> Page:
    where = frame or url
    return Page(url, [("user", where), ("password", where)], "Sign in - Fixture College ERP")


def signs_in(b: FakeBrowser) -> None:
    b.tabs[b.active] = Page(SITE + "/dashboard", [], "Welcome student")


def rejects(b: FakeBrowser) -> None:
    b.tabs[b.active] = Page(SITE + "/login", [("user", SITE + "/login"), ("password", SITE + "/login")],
                            "Invalid user name or password")


def browser(page: Page | None = None, **kw: Any) -> FakeBrowser:
    b = FakeBrowser([page or login_page()], **kw)
    if b.on_submit is None:
        b.on_submit = signs_in
    return b


# --- service fixtures ----------------------------------------------------------------------------------------------

class Approver:
    def __init__(self, broker: ApprovalBroker, answers: list[bool]) -> None:
        self.broker, self.answers, self.asked = broker, list(answers), []
        broker.add_presenter("test", self.present)

    async def present(self, req: Any) -> None:
        self.asked.append(req)
        approved = self.answers.pop(0) if self.answers else False
        asyncio.get_running_loop().call_soon(self.broker.answer, req.id, approved, "dashboard", False)


@pytest.fixture
def svc(tmp_path: Path) -> tuple[LoginService, ApprovalBroker, Path]:
    s = LoginStore(tmp_path / "logins", STANDIN.seal, STANDIN.unseal)
    s.add("College ERP", SITE, USER, SECRET, login_url=SITE + "/login", permission="always")
    broker = ApprovalBroker(timeout_s=5)
    return LoginService(s, AuditLog(tmp_path / "audit.jsonl"), broker), broker, tmp_path


async def engine(b: FakeBrowser, svc: LoginService, answers: list[bool] | None = None) -> Any:
    async def handoff(kind: str, host: str, text: str) -> bool:
        return bool(answers.pop(0)) if answers else False

    return await OwnBrowserSignIn(handoff, settle_s=1).sign_in(b, svc.store.get("College ERP"))


# --- consent -------------------------------------------------------------------------------------------------------

async def test_first_fill_in_own_browser_asks_once_even_for_always(svc):
    service, broker, _ = svc
    who = Approver(broker, [True])
    b = browser()
    result = await service.sign_in_own("College ERP", b, task_id="t1")
    assert result.ok and result.outcome == "signed_in", result.message
    assert "in your Google Chrome" in result.message and b.submits == 1
    assert len(who.asked) == 1
    ask = who.asked[0]
    assert ask.tool == OWN_CONSENT_TOOL and ask.risk == "external" and not ask.for_task
    assert "Google Chrome" in ask.summary and "College ERP" in ask.summary and SITE in ask.summary
    assert SECRET not in ask.summary and USER not in ask.summary
    assert service.store.get("College ERP").own_browser.keys() == {"chrome"}
    # remembered: the next time an "always" login is used without a question
    again = await service.sign_in_own("College ERP", browser(), task_id="t2")
    assert again.ok and len(who.asked) == 1


async def test_no_consent_types_nothing(svc):
    service, broker, tmp = svc
    who = Approver(broker, [False])
    b = browser()
    result = await service.sign_in_own("College ERP", b, task_id="t1")
    assert result.outcome == "needs_user" and "own Google Chrome" in result.message
    assert b.typed() == [] and b.submits == 0 and len(who.asked) == 1
    assert service.store.get("College ERP").own_browser == {}
    uses = [json.loads(x) for x in (tmp / "audit.jsonl").read_text("utf-8").splitlines() if '"login.use"' in x]
    assert uses[-1]["data"]["outcome"] == "not_allowed" and uses[-1]["data"]["browser"] == "own:chrome"


async def test_consent_is_per_browser_and_ask_every_time_withdraws_it(svc):
    service, broker, _ = svc
    who = Approver(broker, [True, True, True])
    assert (await service.sign_in_own("College ERP", browser(), task_id="t1")).ok
    edge = browser(browser_key="edge", browser_label="Microsoft Edge")
    assert (await service.sign_in_own("College ERP", edge, task_id="t2")).ok
    assert [a.tool for a in who.asked] == [OWN_CONSENT_TOOL, OWN_CONSENT_TOOL]
    assert "Microsoft Edge" in who.asked[1].summary
    service.store.set_permission("College ERP", "ask")  # "Ask every time" withdraws the consent
    assert service.store.get("College ERP").own_browser == {}


async def test_ask_every_time_still_asks_after_the_consent(svc):
    service, broker, _ = svc
    service.store.set_permission("College ERP", "ask")
    who = Approver(broker, [True, True])
    assert (await service.sign_in_own("College ERP", browser(), task_id="t1")).ok  # the consent covers this use
    assert (await service.sign_in_own("College ERP", browser(), task_id="t2")).ok  # the login's own question
    assert [a.tool for a in who.asked] == [OWN_CONSENT_TOOL, "sign_in"]


async def test_locked_remote_control_asks_nothing_and_types_nothing(svc, tmp_path):
    service, broker, _ = svc
    who = Approver(broker, [True])
    secrets: dict[str, str] = {}
    guard = RemoteGuard(tmp_path, AuditLog(tmp_path / "g.jsonl"), get_secret=secrets.get,
                        set_secret=secrets.__setitem__)
    guard.lock("test", by="dashboard")
    service.guard = guard
    b = browser()
    token = CURRENT_REQUEST.set(TaskRequest("check erp", channel="telegram", user="5"))
    try:
        result = await service.sign_in_own("College ERP", b, task_id="t1")
    finally:
        CURRENT_REQUEST.reset(token)
    assert not result.ok and who.asked == [] and b.typed() == []


# --- the same origin rules ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("url, word", [
    ("https://erp.college.edu.evil.test/login", "phishing"),
    ("https://erp-college.edu/login", "phishing"),
    ("https://xn--rp-college-u9a.edu/login", "phishing"),
    ("http://erp.college.edu/login", "downgrade"),
    ("https://erp.college.edu:8443/login", "port"),
])
async def test_other_origins_get_nothing(svc, url, word):
    service, _, _ = svc
    b = browser(login_page(url))
    result = await engine(b, service)
    assert result.outcome == "refused" and word in result.message, result.message
    assert "did not type anything" in result.message
    assert b.typed() == [] and b.submits == 0


async def test_form_in_a_cross_origin_frame_is_refused(svc):
    service, _, _ = svc
    b = browser(login_page(SITE + "/frame", frame="https://login.other-site.test/form"))
    result = await engine(b, service)
    assert result.outcome == "refused" and "frame from another site" in result.message
    assert b.typed() == [] and b.submits == 0


async def test_an_unreadable_frame_address_is_refused(svc):
    service, _, _ = svc
    b = browser(Page(SITE + "/login", [("user", SITE + "/login"), ("password", SITE + "/login")]))

    async def unreadable(f: FieldRef) -> str:
        return ""

    b.frame_url = unreadable  # type: ignore[method-assign]
    result = await engine(b, service)
    assert result.outcome == "refused" and "could not confirm" in result.message and b.typed() == []


async def test_opens_the_saved_login_page_when_elsewhere(svc):
    service, _, _ = svc
    b = browser(Page("https://news.example/", [], "news"))
    b.routes[SITE + "/login"] = login_page()
    result = await engine(b, service)
    assert result.ok and b.navigations == [SITE + "/login"]


async def test_a_redirect_to_another_site_is_refused(svc):
    service, _, _ = svc
    b = browser(Page("https://news.example/", [], "news"))
    b.routes[SITE + "/login"] = login_page("https://sso.elsewhere.test/login")
    result = await engine(b, service)
    assert result.outcome == "refused" and "ended up somewhere else" in result.message and b.typed() == []


# --- own-browser risks ----------------------------------------------------------------------------------------------

async def test_switching_tabs_mid_fill_aborts_and_clears(svc):
    service, _, _ = svc

    def user_switches(b: FakeBrowser, f: FieldRef, value: str) -> None:
        if f.kind == "user":
            b.tabs.append(Page("https://mail.example/", [], "inbox"))
            b.active = 1  # the user clicked another tab while Delta was filling

    b = browser(after_set=user_switches)
    result = await engine(b, service)
    assert result.outcome == "aborted" and "switched to another tab" in result.message
    assert SECRET not in b.values.values() and b.typed() == [] and b.submits == 0  # cleared, never submitted


async def test_navigation_mid_fill_stops_before_the_password(svc):
    service, _, _ = svc

    def page_moves(b: FakeBrowser, f: FieldRef, value: str) -> None:
        if f.kind == "user":
            b.tabs[b.active] = login_page("https://erp.college.edu.evil.test/login")

    b = browser(after_set=page_moves)
    result = await engine(b, service)
    assert result.outcome == "refused" and SECRET not in b.values.values() and b.submits == 0


async def test_window_closed_mid_fill_aborts(svc):
    service, _, _ = svc

    def closes(b: FakeBrowser, f: FieldRef, value: str) -> None:
        b.window = ""

    b = browser(after_set=closes)
    result = await engine(b, service)
    assert result.outcome == "aborted" and b.submits == 0


async def test_extension_that_changes_the_box_is_reported_once(svc):
    service, _, _ = svc
    b = browser(keeps=lambda f, v: "" if f.kind == "password" else v)  # e.g. a password manager clears it
    result = await engine(b, service)
    assert result.outcome == "failed" and "did not keep" in result.message and "will not try again" in result.message
    assert b.submits == 0 and SECRET not in b.values.values()


async def test_wrong_password_is_reported_and_not_retried(svc):
    service, _, _ = svc
    b = browser(on_submit=rejects)
    result = await engine(b, service)
    assert result.outcome == "wrong_password" and b.submits == 1


async def test_captcha_on_the_form_is_the_users_to_submit(svc):
    service, _, _ = svc
    page = login_page()
    page.boxes.append(("captcha", SITE + "/login"))
    b = browser(page)
    result = await engine(b, service, answers=[False])
    assert result.outcome == "needs_user" and b.submits == 0  # Delta never submits a captcha form itself


async def test_robot_checks_are_handed_over(svc):
    service, _, _ = svc
    b = browser(Page(SITE + "/login", [("user", SITE + "/login"), ("password", SITE + "/login")], "reCAPTCHA"))
    result = await engine(b, service, answers=[False])
    assert result.outcome == "needs_user" and b.typed() == []


async def test_already_signed_in_types_nothing(svc):
    service, _, _ = svc
    b = browser(Page(SITE + "/dashboard", [], "Welcome"))
    b.routes[SITE + "/login"] = Page(SITE + "/dashboard", [], "Welcome")
    result = await engine(b, service)
    assert result.outcome == "already_signed_in" and b.typed() == []


async def test_password_boxes_are_remembered_for_masking(svc):
    service, _, _ = svc
    b = browser()
    assert (await engine(b, service)).ok and b.secret_boxes == 1


# --- the password goes nowhere --------------------------------------------------------------------------------------

async def test_password_is_nowhere_but_the_box(svc, capsys):
    service, broker, tmp = svc
    who = Approver(broker, [True])
    outcomes = []
    for kw in ({}, {"on_submit": rejects}, {"keeps": lambda f, v: v[:-1]}):
        outcomes.append(await service.sign_in_own("College ERP", browser(**kw), task_id="t"))
    texts = [r.message for r in outcomes] + [a.summary + a.reason for a in who.asked]
    assert all(SECRET not in t and USER not in t for t in texts)
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    for f in tmp.rglob("*"):
        if f.is_file():
            assert SECRET.encode() not in f.read_bytes(), f"password in {f.name}"


def test_no_log_or_audit_line_names_a_secret():
    """A grep over the own-browser code: no log, audit or result text is built from the password or user name."""
    src = Path(__file__).resolve().parents[1] / "src" / "delta" / "logins"
    for name in ("own_browser.py", "own_uia.py", "service.py", "mask.py"):
        for line in (src / name).read_text("utf-8").splitlines():
            if any(k in line for k in ("log.", "audit.record", "SignInResult(", "ToolResult(")):
                assert "login.password" not in line and "login.username" not in line, f"{name}: {line.strip()}"
                assert "{value" not in line, f"{name}: {line.strip()}"


# --- the tool: whichever browser the task uses, never switching it ------------------------------------------------

class Hub:
    def __init__(self, kind: str, driver: Any = None, mgr: Any = None) -> None:
        self.kind, self.driver, self.mgr, self.ub = kind, driver, mgr, object() if driver else None
        self.settings = None

    def kind_now(self) -> str:
        return self.kind

    def own_signin_driver(self) -> Any:
        return self.driver

    async def prepare(self) -> str:
        return ""


async def test_sign_in_tool_uses_the_users_own_browser_and_keeps_the_choice(svc):
    from delta.tools.browser_choice import BrowserPick
    from delta.tools.browser_switch import TASK_BROWSER, TaskBrowser

    service, broker, _ = svc
    Approver(broker, [True])
    b = browser()
    tool = SignIn(service, Hub("user", b))
    assert tool.available()
    tb = TaskBrowser(BrowserPick("default"))
    token = TASK_BROWSER.set(tb)
    try:
        out = await tool.run(SignInArgs(site="College ERP"))
    finally:
        TASK_BROWSER.reset(token)
    assert out.ok, out.output
    assert out.evidence["browser"] == "own:chrome" and tb.pick.choice == "default" and tb.used
    assert SECRET not in out.output and "Welcome student" in out.output


async def test_sign_in_tool_in_the_private_browser_is_unchanged(svc):
    service, _, _ = svc

    class Mgr:
        async def page(self) -> Any:
            raise RuntimeError("no browser here")

    tool = SignIn(service, Hub("private", None, Mgr()))
    out = await tool.run(SignInArgs(site="College ERP"))
    assert not out.ok and "private browser" in out.output


# --- store and masking ---------------------------------------------------------------------------------------------

def test_old_logins_load_without_the_consent_field(tmp_path):
    s = LoginStore(tmp_path / "logins", STANDIN.seal, STANDIN.unseal)
    s.box.save("old", {"name": "Old ERP", "origin": SITE, "username": USER, "password": SECRET})
    old = s.get("Old ERP")
    assert old is not None and old.own_browser == {} and old.public()["own_browser"] == []
    s.allow_own_browser("old", "edge")
    assert s.get("Old ERP").own_browser.keys() == {"edge"}


def test_masking_paints_the_remembered_box():
    from PIL import Image

    mask.forget_all()
    try:
        mask.remember(42, (110, 220, 210, 240), (100, 200))  # a box at window (10, 20)-(110, 40)
        img = Image.new("RGB", (300, 200), (255, 255, 255))
        assert mask.mask_window(img, 42, rect=lambda h: (100, 200, 400, 400)) == 1
        assert img.getpixel((50, 30)) == (0, 0, 0) and img.getpixel((200, 150)) == (255, 255, 255)
        screen = Image.new("RGB", (800, 600), (255, 255, 255))
        assert mask.mask_screen(screen, (0, 0), rect=lambda h: (100, 200, 400, 400)) == 1
        assert screen.getpixel((150, 230)) == (0, 0, 0)
        assert mask.mask_window(Image.new("RGB", (10, 10)), 42, rect=lambda h: None) == 0  # window gone
    finally:
        mask.forget_all()


def test_field_kinds_from_names():
    assert classify("Password", True) == "password"
    assert classify("Roll number / user name", False) == "user"
    assert classify("Enter the one-time code", False) == "otp"
    assert classify("Enter the characters", False) == "captcha"
    assert classify("Search", False) == ""


# --- the extra browser tools exist in both browsers -----------------------------------------------------------------

EXTRA = ("browser_select", "browser_check", "browser_upload", "browser_hover", "browser_wait", "browser_tabs",
         "browser_dialog", "browser_menu")


class FakeCtl:
    def acting(self) -> Any:
        import contextlib

        return contextlib.nullcontext()

    async def call(self, fn: Any, *a: Any) -> Any:
        return fn(*a)

    def call_now(self, fn: Any, *a: Any, timeout: float = 2.0) -> Any:
        return fn(*a)


class FakeUserBrowser:
    def __init__(self) -> None:
        self.ctl, self.current_url, self.name, self.handle = FakeCtl(), "https://erp.college.edu/", "chrome", 1

    def _window(self) -> Any:
        return None


def test_extra_tools_exist_for_the_users_own_browser_with_the_same_args():
    from delta.tools import browser_actions as private
    from delta.tools.browser_switch import BROWSER_NAMES
    from delta.tools.user_browser_actions import user_browser_more_tools

    tools = {t.name: t for t in user_browser_more_tools(FakeUserBrowser())}  # type: ignore[arg-type]
    assert set(tools) == set(EXTRA) and set(EXTRA) <= set(BROWSER_NAMES)
    same = {"browser_select": private.SelectArgs, "browser_check": private.CheckArgs,
            "browser_upload": private.UploadArgs, "browser_wait": private.WaitArgs, "browser_tabs": private.TabsArgs,
            "browser_dialog": private.DialogArgs, "browser_menu": private.MenuArgs}
    for name, args in same.items():
        assert tools[name].Args is args
    assert tools["browser_upload"].risk == Risk.EXTERNAL and tools["browser_upload"].paths(
        private.UploadArgs(label="Choose file", path="C:/x.pdf")) == ["C:/x.pdf"]


async def test_extra_tools_in_the_users_browser_explain_what_differs():
    from delta.tools import browser_actions as private
    from delta.tools.user_browser_actions import user_browser_more_tools

    tools = {t.name: t for t in user_browser_more_tools(FakeUserBrowser())}  # type: ignore[arg-type]
    by_ref = await tools["browser_check"].run(private.CheckArgs(ref=7))
    assert not by_ref.ok and "label" in by_ref.output
    element = await tools["browser_wait"].run(private.WaitArgs(until="element", ref=3))
    assert not element.ok and "until=text" in element.output
    no_window = await tools["browser_select"].run(private.SelectArgs(label="Semester", option="5"))
    assert not no_window.ok and "browser_open" in no_window.output
    assert tools["browser_tabs"].risk_for(private.TabsArgs(action="list")) == Risk.READ
    assert tools["browser_dialog"].risk_for(private.DialogArgs(accept=False)) == Risk.ACT


def test_the_stand_in_sends_extra_tools_to_the_users_browser(tmp_path):
    from delta.config import Settings
    from delta.tools.browser_choice import Installed
    from delta.tools.browser_switch import BrowserHub
    from delta.tools.user_browser_actions import user_browser_more_tools

    inst = Installed(chrome=True, edge=True, desktop=True, automation=True)
    hub = BrowserHub(Settings(data_dir=tmp_path), None, detect_fn=lambda desktop=True, automation=True: inst)
    hub.add_user(user_browser_more_tools(FakeUserBrowser()))  # type: ignore[arg-type]
    proxies = {t.name: t for t in hub.tools()}
    assert hub.kind_now() == "user"  # the default choice is the user's own browser, unchanged
    assert proxies["browser_select"].available() and "user's browser" in proxies["browser_select"].description
