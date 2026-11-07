"""The saved-login fixes from a real user's report (a college ERP with a simple text captcha, on Windows 11, Codex as
the AI): which login a request means (and why not), exact error messages that never claim a missing password when
one is saved, Replace updates in place, the per-login captcha switch (exact origin only), a rejected captcha told
apart from a rejected password, the captcha picture parked for the user's reply, the own-browser captcha path, the
no-AI LoginFlow ("log in to my ERP", "try again", the captcha reply) and the planner never answering "I can't perform
desktop actions". The real-browser side is tests/e2e/test_logins_captcha_e2e.py.

Everything here is made up: no real user name, student id, password or captcha answer."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeProvider, js

from delta.config import AgentConfig, Boundaries, MemoryConfig, Settings
from delta.core.events import EventBus
from delta.core.orchestrator import Orchestrator, claims_no_tools
from delta.core.planner import cannot_act, make_plan
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.evals.standin_vault import STANDIN
from delta.llm.router import ModelRouter
from delta.logins import service as service_mod
from delta.logins.flow import LoginFlow, captcha_text
from delta.logins.origin import mismatch, parse_origin
from delta.logins.own_browser import FieldRef, OwnBrowserSignIn, TabBinding, classify
from delta.logins.service import LoginService
from delta.logins.signin import OTP_SEL, captcha_rejected
from delta.logins.store import LoginError, LoginStore
from delta.logins.tools import ListLogins, ListLoginsArgs
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.tools.base import ToolResult
from delta.tools.captcha import CaptchaSolver
from delta.tools.registry import ToolExecutor, ToolRegistry

SECRET = "Made-Up-Pw-3141!"
USER = "student01"
SITE = "https://newerp.fixtureuniversity.in"
ANSWER = "K7Q2"


def store(tmp_path: Path) -> LoginStore:
    return LoginStore(tmp_path / "logins", STANDIN.seal, STANDIN.unseal)


# --- which login a request means -------------------------------------------------------------------------------

def test_vague_names_find_the_one_login_they_mean(tmp_path):
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, SECRET)
    s.add("Gmail", "https://mail.google.com", "me", "x")
    for ref in ("fuerp", "FUERP", "Fixture University ERP", "my fixture university erp portal",
                "newerp.fixtureuniversity.in", "https://newerp.fixtureuniversity.in/", SITE + "/index.php?r=site/login",
                "https://newerp.fixtureuniversity.in:443/login", "https://www.newerp.fixtureuniversity.in"):
        login, why = s.resolve(ref)
        assert login is not None and login.name == "fuerp", (ref, why)
    login, why = s.resolve("my bank")
    assert login is None and "no saved login matches 'my bank'" in why and "'fuerp'" in why and "Gmail" in why


def test_finding_is_loose_but_typing_needs_the_exact_origin():
    saved = parse_origin(SITE)
    assert mismatch(saved, SITE + "/") == "" and mismatch(saved, SITE + "/index.php?r=site%2Flogin") == ""
    assert mismatch(saved, "https://newerp.fixtureuniversity.in:443/x") == ""  # the default port is the same origin
    for other in ("http://newerp.fixtureuniversity.in/", "https://www.newerp.fixtureuniversity.in/",
                  "https://newerp.fixtureuniversity.in:8443/", "https://erp.fixtureuniversity.in/"):
        assert mismatch(saved, other), other  # found by resolve(), never typed into


def test_two_logins_for_one_site_are_never_guessed_between(tmp_path):
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, SECRET)
    s.add("FU ERP old", SITE, USER, "")  # saved once with only a user name
    login, why = s.resolve(SITE)
    assert login is None
    assert "2 saved logins are for newerp.fixtureuniversity.in" in why
    assert "'fuerp' (https://newerp.fixtureuniversity.in, password saved)" in why
    assert "'FU ERP old' (https://newerp.fixtureuniversity.in, NO password saved)" in why
    assert s.resolve("fuerp")[0].password == SECRET  # the exact name still works


def test_strict_get_for_changes_never_takes_a_vague_match(tmp_path):
    s = store(tmp_path)
    s.add("Old ERP", "https://old.example.edu", USER, SECRET)
    assert s.get("ERP") is None and s.remove("ERP") is None  # never removes "Old ERP" by a loose word
    with pytest.raises(LoginError):
        s.set_simple_captcha("ERP", True)


def test_replace_updates_in_place_and_keeps_the_captcha_switch(tmp_path):
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, "", simple_captcha=True)
    with pytest.raises(LoginError, match="Replace"):
        s.add("fuerp", SITE, USER, SECRET)
    s.add("fuerp", SITE, USER, SECRET, replace=True)
    items = s.list()
    assert len(items) == 1 and items[0].password == SECRET and items[0].simple_captcha  # one login, updated
    s.add("fuerp", "https://other.example.edu", USER, SECRET, replace=True)
    assert not s.get("fuerp").simple_captcha  # another site: the switch does not follow


def test_captcha_switch_is_exact_origin_only(tmp_path):
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, SECRET)
    assert not s.captcha_allowed(SITE + "/")
    assert s.set_simple_captcha("fuerp", True).simple_captcha
    assert s.captcha_allowed(SITE + "/index.php?r=site/login")
    for other in ("http://newerp.fixtureuniversity.in/", "https://evil.newerp.fixtureuniversity.in/",
                  "https://newerp.fixtureuniversity.in.evil.test/", "https://newerp.fixtureuniversity.in:8443/"):
        assert not s.captcha_allowed(other), other
    pub = s.get("fuerp").public(with_username=False)
    assert pub["simple_captcha"] is True and pub["has_password"] is True and SECRET not in json.dumps(pub)


def test_card_status_is_plain_words(tmp_path):
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, SECRET)
    s.touch("fuerp", "needs_user")
    assert s.get("fuerp").public()["last_outcome_words"] == "waited for you (captcha, code or permission)"


async def test_list_logins_says_whether_a_password_is_saved(tmp_path):
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, SECRET)
    s.add("Library", "https://lib.example.edu", USER, "")
    out = (await ListLogins(LoginService(s, AuditLog(tmp_path / "a.jsonl"), None)).run(ListLoginsArgs())).output
    assert "fuerp: https://newerp.fixtureuniversity.in (ask every time; password saved)" in out
    assert "Library: https://lib.example.edu (ask every time; NO password saved" in out
    assert SECRET not in out and USER not in out


# --- a rejected captcha is not a rejected password ------------------------------------------------------------

@pytest.mark.parametrize("text, captcha", [
    ("Login\nThe verification code is incorrect.\nEnter Username", True),
    ("Invalid captcha. Please enter the characters again.", True),
    ("Verification Code cannot be blank.", False),  # not an error word Delta knows: never re-submits on a guess
    ("Incorrect username or password.", False),
    ("Invalid password. The verification code is incorrect.", False),  # the password too: never retried
    ("Welcome, student", False),
])
def test_captcha_rejected(text, captcha):
    assert captcha_rejected(text) is captcha


def test_a_captcha_box_is_never_an_otp_box():
    assert all(":not([placeholder*=captcha i])" in part for part in OTP_SEL.split(", "))
    assert classify("Enter verification Code", False, "loginFormCaptcha") == "captcha"  # its HTML id says so
    assert classify("Enter verification Code", False) == "otp"  # no id: a same-form code (see below)
    assert classify("Enter MFA Code", False, "loginFormQRCodeID") == "otp"
    assert classify("Enter Username *", False, "loginFormUserNameID") == "user"
    pw, code = FieldRef("password", SITE + "/"), FieldRef("otp", SITE + "/")
    assert OwnBrowserSignIn._captcha_box([FieldRef("user", SITE + "/"), pw, code]) is code
    assert OwnBrowserSignIn._captcha_box([code]) is None  # alone, after the password: a real one-time code


# --- the own browser: captcha read here, or parked for the user's reply -------------------------------------------

@dataclass
class Page:
    url: str
    boxes: list[tuple[str, str]] = field(default_factory=list)
    text: str = "Login"


@dataclass
class CaptchaBrowser:
    """A fake own-browser window on an ERP-like page: user name, password, and a captcha box with a picture."""

    page: Page
    answer: str = ANSWER
    browser_key: str = "chrome"
    browser_label: str = "Google Chrome"
    values: dict[str, str] = field(default_factory=dict)
    posts: list[dict[str, str]] = field(default_factory=list)
    tab: str = "tab0"
    pictures: int = 0

    async def ensure_window(self, url: str) -> bool:
        return True

    async def binding(self) -> TabBinding | None:
        return TabBinding("w1", self.tab)

    async def top_url(self) -> str:
        return self.page.url

    async def navigate(self, url: str) -> None:
        self.page = form()

    async def fields(self) -> list[FieldRef]:
        return [FieldRef(kind, where, kind, kind) for kind, where in self.page.boxes]

    async def frame_url(self, f: FieldRef) -> str:
        return self.page.url

    async def set_value(self, f: FieldRef, value: str, secret: bool) -> None:
        self.values[f.kind] = value

    async def value_length(self, f: FieldRef) -> int | None:
        return len(self.values.get(f.kind, ""))

    async def submit(self, f: FieldRef) -> None:
        sent = dict(self.values)
        self.posts.append({k: ("<set>" if k == "password" else v) for k, v in sent.items()})
        self.values = {"user": sent.get("user", "")}  # the site clears the password and the captcha
        if sent.get("captcha", sent.get("otp", "")).upper() != self.answer:
            self.page = form("The verification code is incorrect.")
        elif sent.get("password") != SECRET:
            self.page = form("Incorrect username or password.")
        else:
            self.page = Page(SITE + "/index.php?r=site/home", [], "Welcome student")

    async def text(self) -> str:
        return self.page.text

    async def challenge(self) -> str | None:
        return None

    async def settle(self) -> None:
        return None

    def remember_secret_box(self, f: FieldRef) -> None:
        return None

    async def captcha_image(self, f: FieldRef) -> bytes | None:
        self.pictures += 1
        return b"\xff\xd8fake-jpeg-%d" % self.pictures

    async def refresh_captcha(self, f: FieldRef) -> bool:
        return True

    async def observe(self) -> str:
        return self.page.text


def form(error: str = "", code_kind: str = "captcha") -> Page:
    where = SITE + "/"
    return Page(where, [("user", where), ("password", where), (code_kind, where)], f"Login {error}".strip())


def solver(tmp_path: Path, reads: list[str], allowed: bool = True) -> CaptchaSolver:
    queue = list(reads)

    async def ocr(jpeg: bytes) -> str:
        return queue.pop(0) if queue else ""

    async def nobody(host: str, saved: str) -> bool:
        raise AssertionError("a sign-in never uses the solver's on-PC hand-off")

    s = CaptchaSolver(allowed_sites=lambda: [], vision=None, ask_human=nobody, audit=AuditLog(tmp_path / "c.jsonl"),
                      ocr=ocr)
    s.origin_allowed = (lambda url: url.startswith(SITE)) if allowed else (lambda url: False)
    return s


def service(tmp_path: Path, reads: list[str] | None = None, allowed: bool = True, captcha_switch: bool = True,
            password: str = SECRET) -> LoginService:
    s = store(tmp_path)
    s.add("fuerp", SITE, USER, password, permission="always", simple_captcha=captcha_switch)
    s.allow_own_browser("fuerp", "chrome")
    return LoginService(s, AuditLog(tmp_path / "audit.jsonl"), None,
                        captcha_solver=solver(tmp_path, reads or [], allowed) if reads is not None else None)


def as_chat(chat: str = "c1") -> Any:
    return CURRENT_REQUEST.set(TaskRequest("sign in", channel="api", chat_id=chat))


async def test_own_browser_reads_the_captcha_and_retries_a_rejected_one(tmp_path):
    svc = service(tmp_path, reads=["WRONG", ANSWER])
    b = CaptchaBrowser(form())
    token = as_chat()
    try:
        r = await svc.sign_in_own("fuerp", b)
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok, r.message
    assert [p.get("captcha") for p in b.posts] == ["WRONG", ANSWER] and all(p["password"] == "<set>" for p in b.posts)


async def test_own_browser_same_form_code_box_is_read_as_the_captcha(tmp_path):
    svc = service(tmp_path, reads=[ANSWER])
    b = CaptchaBrowser(form(code_kind="otp"))  # "Enter verification Code" without a telling id
    token = as_chat()
    try:
        r = await svc.sign_in_own("fuerp", b)
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok, r.message
    assert b.posts == [{"user": USER, "password": "<set>", "otp": ANSWER}]  # never submitted empty


async def test_switch_off_parks_the_form_and_sends_the_picture(tmp_path):
    svc = service(tmp_path, reads=[ANSWER], allowed=False, captcha_switch=False)
    b = CaptchaBrowser(form())
    token = as_chat()
    try:
        r = await svc.sign_in_own("fuerp", b)
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.outcome == "needs_user" and r.captcha_image and b.posts == []
    assert "Reply with just the characters" in r.message and "open browser" not in r.message
    assert "turn on 'This site has a simple text captcha'" in r.message
    assert svc.waiting("c1") is not None and svc.waiting("someone-else") is None
    assert svc.last_image["c1"] == r.captcha_image
    # the user's reply finishes it: the same tab, re-checked, one submit, checked
    done = await svc.answer_captcha("c1", ANSWER)
    assert done.ok, done.message
    assert b.posts == [{"user": USER, "password": "<set>", "captcha": ANSWER}]
    assert svc.waiting("c1") is None
    log = (tmp_path / "audit.jsonl").read_text()
    assert ANSWER not in log and SECRET not in log and "human_reply" in log


async def test_a_wrong_reply_sends_the_new_picture_then_stops(tmp_path):
    svc = service(tmp_path, reads=None, captcha_switch=False)  # no solver at all: straight to the user
    b = CaptchaBrowser(form())
    token = as_chat()
    try:
        r = await svc.sign_in_own("fuerp", b)
        assert r.outcome == "needs_user" and r.captcha_image
        for _ in range(service_mod.HUMAN_TRIES - 1):
            again = await svc.answer_captcha("c1", "NOPE")
            assert again.outcome == "needs_user" and again.captcha_image and svc.waiting("c1") is not None
        last = await svc.answer_captcha("c1", "NOPE")
    finally:
        CURRENT_REQUEST.reset(token)
    assert last.outcome == "needs_user" and "rejected the captcha 3 times" in last.message
    assert svc.waiting("c1") is None and len(b.posts) == service_mod.HUMAN_TRIES


async def test_a_parked_sign_in_expires_and_a_tab_switch_stops_it(tmp_path):
    svc = service(tmp_path, reads=None, captcha_switch=False)
    b = CaptchaBrowser(form())
    token = as_chat()
    try:
        await svc.sign_in_own("fuerp", b)
        b.tab = "tab1"  # the user switched tabs meanwhile
        stopped = await svc.answer_captcha("c1", ANSWER)
        assert stopped.outcome == "aborted" and b.posts == []
        b.tab = "tab0"
        await svc.sign_in_own("fuerp", b)
        svc.clock = lambda: 1e12  # long after
        gone = await svc.answer_captcha("c1", ANSWER)
    finally:
        CURRENT_REQUEST.reset(token)
    assert gone.outcome == "failed" and "expires" in gone.message and b.posts == []


async def test_messages_name_the_missing_field_and_never_invent_one(tmp_path):
    svc = service(tmp_path, reads=None, password="")
    b = CaptchaBrowser(form())
    r = await svc.sign_in_own("fuerp", b)
    assert r.outcome == "no_form" and "'fuerp' has no password saved (only a user name)" in r.message
    assert "Settings > Logins (Replace)" in r.message and b.values == {}
    svc2 = service(tmp_path / "2", reads=None)
    only_user = CaptchaBrowser(Page(SITE + "/", [("user", SITE + "/")]))

    async def stays(f: FieldRef) -> None:  # the next page never shows a password box
        only_user.posts.append({})

    only_user.submit = stays  # type: ignore[method-assign]
    r2 = await svc2.sign_in_own("fuerp", only_user)  # a user-name-only first page, password saved
    assert "no password saved" not in r2.message and "no password box came" in r2.message


# --- the flow: no AI for a plain sign-in ------------------------------------------------------------------------

class FakeExecutor:
    def __init__(self, svc: LoginService, browser: CaptchaBrowser) -> None:
        self.svc, self.b, self.calls = svc, browser, 0

        class Reg:
            @staticmethod
            def get(name: str) -> Any:
                return type("T", (), {"available": lambda self: True})() if name == "sign_in" else None

        self.registry = Reg()

    async def execute(self, name: str, args: dict[str, Any], task_id: str = "") -> ToolResult:
        self.calls += 1
        r = await self.svc.sign_in_own(args["site"], self.b, task_id)
        return ToolResult(r.ok, r.message, {"outcome": r.outcome})


@pytest.mark.parametrize("text, taken", [
    ("Login my fixture university erp portal", True),
    ("log in to my FU ERP", True),
    ("log in to my XQ ERP", False),  # "xq" is not part of the login's name or site
    ("Sign in to fuerp", True),
    ("please sign into newerp.fixtureuniversity.in", True),
    ("login to my fixture university erp and check my timetable", False),  # more than a sign-in: the planner
    ("what is a login page?", False),
    ("sign in to my bank", False),
])
def test_which_requests_the_flow_takes(tmp_path, text, taken):
    svc = service(tmp_path, reads=None)
    flow = LoginFlow(svc, FakeExecutor(svc, CaptchaBrowser(form())))
    assert (flow.wanted_login(text) is not None) is taken


def test_captcha_text():
    assert captcha_text("K7Q2") == ANSWER and captcha_text(" k 7 q 2 ") == "k7q2" and captcha_text("'K7Q2'") == ANSWER
    for no in ("hi", "try again now", "the code is K7Q2", "x" * 20, ""):
        assert captcha_text(no) is None


async def test_flow_parks_then_the_reply_signs_in_and_try_again_reruns(tmp_path):
    svc = service(tmp_path, reads=None, captcha_switch=False)
    b = CaptchaBrowser(form())
    ex = FakeExecutor(svc, b)
    flow = LoginFlow(svc, ex)

    async def say(text: str) -> Any:
        req = TaskRequest(text, channel="api", chat_id="c1")
        token = CURRENT_REQUEST.set(req)
        try:
            return await flow.handle(req)
        finally:
            CURRENT_REQUEST.reset(token)

    first = await say("Login my fixture university erp portal")
    assert first.status == "needs_user" and first.image and first.fast and ex.calls == 1
    assert "Reply with just the characters" in first.answer and "CAPTCHA:" not in first.answer
    assert flow.is_captcha_answer(TaskRequest(ANSWER, channel="api", chat_id="c1"))
    assert not flow.is_captcha_answer(TaskRequest(ANSWER, channel="api", chat_id="other-chat"))
    assert await say("what's the weather") is None  # anything else goes on as normal
    done = await say(ANSWER)
    assert done.status == "done" and done.answer.startswith("Signed in to fuerp") and ex.calls == 1
    # a failed sign-in, then "done try again now": the same sign-in again, no planner
    svc.store.add("fuerp", SITE, USER, "Wrong-Pw-1", replace=True, permission="always")
    svc.store.allow_own_browser("fuerp", "chrome")
    b.page = form()
    failed = await say("sign in to fuerp")
    assert failed.status == "needs_user" and ex.calls == 2
    svc.cancel("c1")
    assert (await say("ok")) is None  # not a retry phrase
    again = await say("done try again now")
    assert again is not None and ex.calls == 3


async def test_flow_cancel(tmp_path):
    svc = service(tmp_path, reads=None, captcha_switch=False)
    flow = LoginFlow(svc, FakeExecutor(svc, CaptchaBrowser(form())))
    req = TaskRequest("sign in to fuerp", channel="api", chat_id="c1")
    token = CURRENT_REQUEST.set(req)
    try:
        await flow.handle(req)
        r = await flow.handle(TaskRequest("cancel", channel="api", chat_id="c1"))
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.status == "done" and "stopped signing in to fuerp" in r.answer and svc.waiting("c1") is None


async def test_orchestrator_signs_in_with_zero_ai_calls_and_never_audits_the_answer(tmp_path):
    def no_model(task: str, messages: Any) -> Any:
        raise AssertionError(f"no AI call expected ({task})")

    settings = Settings(agent=AgentConfig(max_attempts=1, max_steps=3), memory=MemoryConfig(auto_extract=False),
                        data_dir=tmp_path)
    router = ModelRouter({"fake": FakeProvider("fake", handler=no_model)}, {"plan": ["fake"], "chat": ["fake"]})
    audit = AuditLog(tmp_path / "audit.jsonl")
    executor = ToolExecutor(ToolRegistry([]), PolicyEngine(Boundaries()), None, audit)
    orch = Orchestrator(settings, router, executor, None, None, EventBus(), audit)
    svc = service(tmp_path, reads=None, captcha_switch=False)
    b = CaptchaBrowser(form())
    orch.login_flow = LoginFlow(svc, FakeExecutor(svc, b), orch.events)
    asked = await orch.run(TaskRequest("Login my fixture university erp portal", channel="api", chat_id="c9"))
    assert asked.status == "needs_user" and asked.image and asked.tokens.get("calls", 0) == 0
    done = await orch.run(TaskRequest(ANSWER, channel="api", chat_id="c9"))
    assert done.status == "done" and done.tokens.get("calls", 0) == 0
    log = (tmp_path / "audit.jsonl").read_text()
    assert ANSWER not in log and "(captcha answer)" in log and SECRET not in log


# --- the planner never answers "I can't perform desktop actions" -------------------------------------------------

def test_cannot_act_and_claims_no_tools():
    said = "I can't perform desktop actions in this interface."
    assert cannot_act(said) and claims_no_tools(said)
    for fine in ("The capital of France is Paris.", "You can't use a sandbox for that in Python.",
                 "I can't tell the future, but here is a guess.", "I don't have feelings, but thanks!"):
        assert not cannot_act(fine), fine


async def test_planner_reply_that_it_cannot_act_becomes_a_tool_plan():
    router = ModelRouter({"fake": FakeProvider("fake", [js({"needs_tools": False,
                                                            "answer": "I can't perform desktop actions in this "
                                                                      "interface."})])}, {"plan": ["fake"]})
    plan = await make_plan(router, "open my portal and read the notices", ["browser_open", "sign_in"], "", "Delta")
    assert plan.needs_tools and plan.answer == "" and plan.success_criteria


async def test_planner_still_answers_real_questions_directly():
    router = ModelRouter({"fake": FakeProvider("fake", [js({"needs_tools": False, "answer": "4"})])},
                         {"plan": ["fake"]})
    plan = await make_plan(router, "what is 2+2", [], "", "Delta")
    assert not plan.needs_tools and plan.answer == "4"



# --- the dashboard routes: Replace, the no-password warning, the captcha switch -------------------------------------

def test_routes_replace_warn_and_captcha_switch(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from delta.api.logins_routes import register

    svc = LoginService(store(tmp_path), AuditLog(tmp_path / "a.jsonl"), None)
    app = FastAPI()
    c = SimpleNamespace(logins=svc, watchers=None, audit=AuditLog(tmp_path / "api.jsonl"),
                        browsers=SimpleNamespace(mgr=None), settings=SimpleNamespace())
    register(app, c, lambda: None)
    client = TestClient(app)
    first = client.post("/api/logins", json={"name": "fuerp", "site": SITE, "username": USER, "password": ""})
    assert first.status_code == 200 and "No password was saved" in first.json()["message"]
    again = client.post("/api/logins", json={"name": "fuerp", "site": SITE, "username": USER, "password": SECRET})
    assert again.status_code == 400 and "Replace" in again.json()["detail"]
    done = client.post("/api/logins", json={"name": "fuerp", "site": SITE, "username": USER, "password": SECRET,
                                            "replace": True})
    assert done.json()["message"].startswith("Updated fuerp") and "No password" not in done.json()["message"]
    listing = client.get("/api/logins").json()["logins"]
    assert len(listing) == 1 and listing[0]["has_password"] and not listing[0]["simple_captcha"]
    on = client.put("/api/logins/fuerp/captcha", json={"on": True}).json()
    assert on["login"]["simple_captcha"] and svc.store.captcha_allowed(SITE + "/")
    assert client.put("/api/logins/nope/captcha", json={"on": True}).status_code == 400
    assert SECRET not in (tmp_path / "api.jsonl").read_text() and SECRET not in client.get("/api/logins").text
