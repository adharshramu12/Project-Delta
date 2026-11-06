"""Browser choice everywhere: one four-way choice (My default browser, Chrome, Edge, Delta's private
browser) mapped onto the old mode/prefer keys, installed-browser detection, per-task pinning (a switch
never lands mid-task), set_browser / open_signin_browser and their Remote Guard rules, the settings API,
Telegram /browser and old settings files. Fakes only: no real browser, no Windows APIs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fakes import FakeProvider, call, js, text
from pydantic import BaseModel

from delta.config import (
    AgentConfig,
    Boundaries,
    BrowserConfig,
    FilesConfig,
    MemoryConfig,
    Settings,
    WriterConfig,
    load_settings,
)
from delta.core.events import EventBus
from delta.core.intents import match_intent
from delta.core.orchestrator import Orchestrator
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.llm.router import ModelRouter
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine, Verdict
from delta.safety.remote_guard import RemoteGuard
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.browser_choice import (
    BrowserPick,
    Installed,
    detect,
    engine_options,
    from_config,
    mentions_browser,
    options,
    parse_choice,
    persistent_request,
    signin_request,
    task_override,
    to_changes,
    unavailable_reason,
)
from delta.tools.browser_switch import TASK_BROWSER, BrowserHub, clean_url, save_choice
from delta.tools.registry import ToolExecutor, ToolRegistry

BOTH = Installed(chrome=True, edge=True, progid="MSEdgeHTM", desktop=True, automation=True)


# --- the choice model -------------------------------------------------------------------------------------

@pytest.mark.parametrize(("choice", "engine", "keys"), [
    ("default", None, {"mode": "your-browser", "prefer": "auto"}),
    ("chrome", None, {"mode": "your-browser", "prefer": "chrome"}),
    ("edge", None, {"mode": "your-browser", "prefer": "edge"}),
    ("private", None, {"mode": "delta-profile"}),
    ("private", "edge", {"mode": "delta-profile", "private_engine": "edge"}),
])
def test_mapping_choice_to_keys_and_back(choice, engine, keys):
    assert to_changes(choice, engine) == keys
    cfg = BrowserConfig().model_copy(update=keys)
    back = from_config(cfg)
    assert back.choice == choice and back.engine == (engine or "auto")


def test_old_keys_map_to_the_four_choices():
    assert from_config(BrowserConfig()) == BrowserPick("default", "auto")  # the shipped default
    assert from_config(BrowserConfig(prefer="edge")).choice == "edge"
    # an old delta-profile config with prefer set: still the private browser, engine unchanged (auto)
    assert from_config(BrowserConfig(mode="delta-profile", prefer="edge")) == BrowserPick("private", "auto")
    # choosing the private browser keeps prefer, so switching back restores the signed-in pick
    assert "prefer" not in to_changes("private")
    with pytest.raises(ValueError):
        to_changes("firefox")


def test_words_people_use_map_to_choices():
    assert parse_choice("Microsoft Edge") == "edge" and parse_choice("google chrome") == "chrome"
    assert parse_choice("Delta’s private browser") == "private" and parse_choice("delta-profile") == "private"
    assert parse_choice("my default browser") == "default" and parse_choice("my normal browser") == "default"
    assert parse_choice("firefox") is None


def test_old_settings_file_loads_unchanged(tmp_path, monkeypatch):
    cfg, data = tmp_path / "config", tmp_path / "data"
    cfg.mkdir()
    data.mkdir()
    (cfg / "delta.yaml").write_text("agent:\n  name: Delta\n", encoding="utf-8")
    # what an older Delta wrote: no private_engine key
    (data / "settings.yaml").write_text(yaml.safe_dump({"browser": {"mode": "delta-profile", "prefer": "chrome"}}),
                                        encoding="utf-8")
    monkeypatch.setenv("DELTA_DATA_DIR", str(data))
    s = load_settings(cfg)
    assert s.browser.mode == "delta-profile" and s.browser.prefer == "chrome" and s.browser.private_engine == "auto"
    assert from_config(s.browser) == BrowserPick("private", "auto")


# --- what is installed ------------------------------------------------------------------------------------

def test_detection_with_fakes():
    only_chrome = detect(exists=lambda p: p.lower().endswith("chrome.exe"), progid=lambda: "FirefoxURL-308046B0AF",
                         desktop=True, automation=True)
    assert only_chrome.chrome and not only_chrome.edge and only_chrome.windows_default == "Firefox"
    opts = {o["id"]: o for o in options(only_chrome, BrowserPick())}
    assert opts["chrome"]["available"] and opts["default"]["available"] and opts["private"]["available"]
    assert not opts["edge"]["available"] and "Microsoft Edge is not installed" in opts["edge"]["reason"]
    assert "Firefox" in opts["default"]["uses"] and "Google Chrome" in opts["default"]["uses"]
    engines = {e["id"]: e for e in engine_options(only_chrome, BrowserPick("private"))}
    assert engines["auto"]["available"] and engines["chrome"]["available"] and not engines["edge"]["available"]

    nothing = detect(exists=lambda p: False, progid=lambda: "", desktop=True, automation=True)
    assert not any(o["available"] for o in options(nothing, BrowserPick()))
    assert all("Neither" in o["reason"] for o in options(nothing, BrowserPick()) if o["id"] != "edge"
               and o["id"] != "chrome")

    no_desktop = Installed(chrome=True, edge=True, desktop=False, automation=True)
    opts = {o["id"]: o for o in options(no_desktop, BrowserPick())}
    assert opts["private"]["available"] and not opts["default"]["available"]
    assert "desktop control" in opts["edge"]["reason"]
    assert "Playwright" in unavailable_reason("private", Installed(chrome=True, automation=False))


def test_my_default_browser_follows_windows_default():
    assert Installed(chrome=True, edge=True, progid="MSEdgeHTM").default_target() == "edge"
    assert Installed(chrome=True, edge=True, progid="ChromeHTML").default_target() == "chrome"
    assert Installed(chrome=False, edge=True, progid="ChromeHTML").default_target() == "edge"  # uninstalled
    assert Installed(chrome=True, edge=True).engine_target("auto") == "chrome"  # what Delta always did
    assert Installed(chrome=False, edge=True).engine_target("chrome") == "edge"


# --- natural language ---------------------------------------------------------------------------------------

@pytest.mark.parametrize(("said", "choice"), [
    ("use Edge", "edge"), ("use edge from now on", "edge"), ("From now on, use Chrome.", "chrome"),
    ("switch to Delta's private browser", "private"), ("use my default browser", "default"),
    ("please set the browser to microsoft edge", "edge"), ("always use delta's private browser", "private"),
])
def test_lasting_switches_are_recognised(said, choice):
    assert persistent_request(said) == choice
    assert task_override(said) is None  # a lasting switch is never a one-task choice
    intent = match_intent(said, {"set_browser"})
    assert intent is not None and intent.tool == "set_browser"
    assert intent.args == {"choice": choice, "scope": "always"}


@pytest.mark.parametrize("said", ["switch to chrome", "open chrome", "what is the edge of a cube",
                                  "install chrome on my laptop", "use edge and open gmail"])
def test_not_lasting_switches(said):
    assert persistent_request(said) is None


@pytest.mark.parametrize(("said", "choice"), [
    ("open gmail in Chrome", "chrome"), ("do this in Edge", "edge"),
    ("search for flights using delta's private browser", "private"),
    ("check my attendance with my default browser", "default"), ("use chrome to open youtube", "chrome"),
])
def test_one_task_choices(said, choice):
    pick = task_override(said)
    assert pick is not None and pick.choice == choice


def test_signin_requests():
    assert signin_request("sign in to my sites") == {"url": ""}
    assert signin_request("let me log in to gmail.com in delta's private browser") == {"url": "https://gmail.com"}
    assert signin_request("open delta's private browser") == {"url": ""}
    assert signin_request("open chrome") is None
    assert match_intent("sign in to my sites", {"open_signin_browser"}).tool == "open_signin_browser"
    assert mentions_browser("from now on use Edge") and not mentions_browser("delete my downloads")


def test_clean_url():
    assert clean_url("") == "" and clean_url("mail.google.com") == "https://mail.google.com"
    with pytest.raises(ValueError):
        clean_url("file:///C:/secrets.txt")
    with pytest.raises(ValueError):
        clean_url("javascript:alert(1)")


# --- the switch: fake kits ----------------------------------------------------------------------------------

class UArgs(BaseModel):
    text: str = ""


class PArgs(BaseModel):
    ref: int | None = None
    text: str = ""


class FakeUB:
    def __init__(self) -> None:
        self.uses: list[str] = []

    def use(self, prefer: str) -> None:
        self.uses.append(prefer)


class FakeMgr:
    def __init__(self) -> None:
        self.engine = "auto"
        self.running = False
        self.closed = 0
        self.browser_name = "chrome"
        self.hold_until = 0.0
        self.opened: list[str] = []

    async def close(self) -> None:
        self.closed += 1
        self.running = False

    async def page(self) -> Any:
        self.running = True
        mgr = self

        class Tab:
            url = "about:blank"

            async def goto(self, url: str, **kw: Any) -> None:
                mgr.opened.append(url)

            async def bring_to_front(self) -> None:
                pass

        return Tab()


def kit_tool(kind: str, name: str, calls: list, args: type[BaseModel], risk: Risk = Risk.ACT, **extra: Any) -> Tool:
    attrs = {"name": name, "description": f"{kind} {name}", "risk": risk, "Args": args}

    async def run(self, a):
        calls.append((kind, name))
        return ToolResult(True, f"{kind}:{name}", {"url": "https://example.com", "title": "Example"})

    attrs["run"] = run
    attrs.update(extra)
    return type(f"{kind}_{name}", (Tool,), attrs)()


NAMES = ["browser_open", "browser_read", "browser_click"]


def make_hub(tmp_path: Path, inst: Installed = BOTH, settings: Settings | None = None):
    settings = settings or Settings(data_dir=tmp_path)
    events: list = []
    audit = SimpleNamespace(record=lambda e, **k: events.append((e, k)))
    hub = BrowserHub(settings, audit, detect_fn=lambda desktop=True, automation=True: inst)
    calls: list = []
    ub, mgr = FakeUB(), FakeMgr()
    user = [kit_tool("user", n, calls, UArgs, ub=ub) for n in NAMES]
    private = [kit_tool("private", n, calls, PArgs, mgr=mgr) for n in NAMES]
    private.append(kit_tool("private", "solve_captcha", calls, PArgs, mgr=mgr))
    hub.add_user(user)
    hub.add_private(private, mgr)
    return hub, calls, ub, mgr, events


def test_one_registry_entry_per_tool_and_it_reports_the_real_tool(tmp_path):
    hub, _, ub, mgr, _ = make_hub(tmp_path)
    reg = ToolRegistry(hub.tools())
    assert set(reg.names()) == {*NAMES, "solve_captcha", "set_browser", "open_signin_browser"}
    click = reg.get("browser_click")
    assert click.Args is UArgs and click.ub is ub  # the user's own browser by default
    assert click.spec()["function"]["description"] == "user browser_click"
    assert "solve_captcha" not in {s["function"]["name"] for s in reg.specs()}  # only in the private browser
    hub.settings.browser.mode = "delta-profile"
    assert click.Args is PArgs and click.mgr is mgr
    assert "solve_captcha" in {s["function"]["name"] for s in reg.specs()}


async def test_policy_still_sees_the_real_risk(tmp_path):
    hub, calls, *_ = make_hub(tmp_path)
    risky = kit_tool("user", "browser_click", calls, UArgs, risk=Risk.EXTERNAL)
    hub.user_tools["browser_click"] = risky
    proxy = {t.name: t for t in hub.tools()}["browser_click"]
    d = PolicyEngine(Boundaries()).evaluate(proxy, UArgs(text="Send"))
    assert d.verdict == Verdict.ASK and d.risk == Risk.EXTERNAL


async def test_a_task_keeps_its_browser_when_the_setting_changes_mid_task(tmp_path):
    hub, calls, *_ = make_hub(tmp_path)
    proxy = {t.name: t for t in hub.tools()}["browser_open"]
    token = hub.begin_task("t1", "open example.com")
    await proxy.run(UArgs())
    assert calls[-1] == ("user", "browser_open")
    # the user switches to the private browser from the dashboard while t1 runs
    msg = save_choice(hub.settings, hub.audit, "private", by="dashboard", hub=hub)
    assert "This task keeps using My default browser" in msg
    await proxy.run(UArgs())
    assert calls[-1] == ("user", "browser_open")  # never mid-task
    hub.end_task("t1", token)
    token = hub.begin_task("t2", "open example.com")
    await proxy.run(PArgs())
    assert calls[-1] == ("private", "browser_open")  # the next task uses it
    hub.end_task("t2", token)
    assert TASK_BROWSER.get() is None


async def test_switch_from_another_place_while_a_task_runs_says_so(tmp_path):
    hub, *_ = make_hub(tmp_path)
    proxy = {t.name: t for t in hub.tools()}["browser_open"]

    async def task() -> None:
        token = hub.begin_task("t1", "open example.com")
        await proxy.run(UArgs())
        await asyncio.sleep(0.05)
        hub.end_task("t1", token)

    running = asyncio.create_task(task())
    await asyncio.sleep(0.01)
    msg = save_choice(hub.settings, hub.audit, "edge", by="telegram:5", hub=hub)  # outside that task
    assert "finishes there; the switch applies from the next task" in msg
    await running


async def test_one_task_choice_from_the_request_leaves_the_setting_alone(tmp_path):
    hub, calls, ub, _, _ = make_hub(tmp_path)
    proxy = {t.name: t for t in hub.tools()}["browser_open"]
    hub.settings.browser.mode = "delta-profile"
    token = hub.begin_task("t1", "open gmail in Chrome")
    await proxy.run(UArgs())
    assert calls[-1] == ("user", "browser_open") and ub.uses == ["chrome"]
    hub.end_task("t1", token)
    assert hub.settings.browser.mode == "delta-profile" and hub.settings.browser.prefer == "auto"
    token = hub.begin_task("t2", "open gmail")
    await proxy.run(PArgs())
    assert calls[-1] == ("private", "browser_open")
    hub.end_task("t2", token)


async def test_a_one_task_choice_that_is_not_installed_is_reported(tmp_path):
    hub, *_ = make_hub(tmp_path, inst=Installed(chrome=False, edge=True, desktop=True, automation=True))
    proxy = {t.name: t for t in hub.tools()}["browser_open"]
    token = hub.begin_task("t1", "open gmail in Chrome")
    out = await proxy.run(UArgs())
    assert "Google Chrome is not installed" in out.output and "tell the user" in out.output
    assert "not installed" not in (await proxy.run(UArgs())).output  # said once
    hub.end_task("t1", token)


async def test_private_engine_changes_only_between_tasks(tmp_path):
    hub, _, _, mgr, _ = make_hub(tmp_path)
    hub.settings.browser.mode = "delta-profile"
    proxy = {t.name: t for t in hub.tools()}["browser_open"]
    mgr.running = True
    hub.settings.browser.private_engine = "edge"
    token = hub.begin_task("t1", "open example.com")
    await proxy.run(PArgs())
    assert mgr.engine == "edge" and mgr.closed == 1  # the old engine's browser (Delta's own) was closed first
    hub.end_task("t1", token)


async def test_captcha_is_not_offered_with_the_users_own_browser(tmp_path):
    hub, *_ = make_hub(tmp_path)
    reg = ToolRegistry(hub.tools())
    cap = reg.get("solve_captcha")
    assert not cap.available()
    out = await cap.run(cap.Args())
    assert not out.ok and "only in Delta's private browser" in out.output


# --- set_browser, open_signin_browser and Remote Guard ---------------------------------------------------

def guard(tmp_path: Path) -> RemoteGuard:
    store: dict = {}
    g = RemoteGuard(tmp_path / "g", SimpleNamespace(record=lambda e, **k: None), alert=lambda t, x: None,
                    get_secret=store.get, set_secret=store.__setitem__)
    g.set_pin("4821", by="dashboard")
    return g


def executor(tmp_path: Path, hub: BrowserHub, g: RemoteGuard | None) -> ToolExecutor:
    ex = ToolExecutor(ToolRegistry(hub.tools()), PolicyEngine(Boundaries()), ApprovalBroker(timeout_s=0.05),
                      AuditLog(tmp_path / "audit.jsonl"))
    ex.guard = g
    return ex


async def run_as(ex: ToolExecutor, hub: BrowserHub, channel: str, text_: str, tool: str, args: dict,
                 user: str = "5") -> ToolResult:
    tok = CURRENT_REQUEST.set(TaskRequest(text_, channel=channel, user=user))
    pin = hub.begin_task("t1", text_)
    try:
        return await ex.execute(tool, args, "t1")
    finally:
        hub.end_task("t1", pin)
        CURRENT_REQUEST.reset(tok)


async def test_saved_switch_from_chat_on_the_pc(tmp_path):
    hub, *_ = make_hub(tmp_path)
    ex = executor(tmp_path, hub, guard(tmp_path))
    r = await run_as(ex, hub, "dashboard", "use Edge from now on", "set_browser", {"choice": "Edge"})
    assert r.ok and r.evidence["verified"] and "From now on I'll use Microsoft Edge" in r.output
    assert hub.settings.browser.prefer == "edge"
    saved = yaml.safe_load((tmp_path / "settings.yaml").read_text())
    assert saved["browser"] == {"mode": "your-browser", "prefer": "edge"}


async def test_saved_switch_from_a_phone_needs_the_pin(tmp_path):
    hub, *_ = make_hub(tmp_path)
    g = guard(tmp_path)
    ex = executor(tmp_path, hub, g)
    r = await run_as(ex, hub, "telegram", "use Edge from now on", "set_browser", {"choice": "edge"})
    assert not r.ok and r.evidence.get("needs_pin") and hub.settings.browser.prefer == "auto"
    # a choice for this task only is not a settings change: no PIN
    r = await run_as(ex, hub, "telegram", "do this in Edge", "set_browser", {"choice": "edge", "scope": "this_task"})
    assert r.ok and "For this task" in r.output and hub.settings.browser.prefer == "auto"
    assert g.try_pin("5", "4821")[0]
    r = await run_as(ex, hub, "telegram", "use Edge from now on", "set_browser", {"choice": "edge"})
    assert r.ok and hub.settings.browser.prefer == "edge"
    g.lock("test", by="test")
    r = await run_as(ex, hub, "telegram", "do this in Chrome", "set_browser",
                     {"choice": "chrome", "scope": "this_task"})
    assert not r.ok and "locked" in r.output


async def test_a_page_cannot_change_the_saved_browser(tmp_path):
    """The saved choice changes only when the user's own message talks about browsers."""
    hub, *_ = make_hub(tmp_path)
    ex = executor(tmp_path, hub, None)
    r = await run_as(ex, hub, "dashboard", "summarise this article", "set_browser",
                     {"choice": "private", "scope": "always"})
    assert not r.ok and "REFUSED" in r.output and hub.settings.browser.mode == "your-browser"


async def test_switching_to_a_browser_that_is_not_installed_keeps_the_old_one(tmp_path):
    hub, *_ = make_hub(tmp_path, inst=Installed(chrome=False, edge=True, desktop=True, automation=True))
    ex = executor(tmp_path, hub, None)
    r = await run_as(ex, hub, "dashboard", "use chrome from now on", "set_browser", {"choice": "chrome"})
    assert r.ok and not r.evidence["changed"] and "Google Chrome is not installed" in r.output
    assert hub.settings.browser.prefer == "auto"


async def test_signin_opens_the_private_browser_and_needs_the_pin_remotely(tmp_path):
    hub, _, _, mgr, events = make_hub(tmp_path)
    g = guard(tmp_path)
    ex = executor(tmp_path, hub, g)
    r = await run_as(ex, hub, "telegram", "sign in to my sites", "open_signin_browser", {"url": "gmail.com"})
    assert not r.ok and r.evidence.get("needs_pin") and mgr.opened == []
    r = await run_as(ex, hub, "dashboard", "sign in to my sites", "open_signin_browser", {"url": "gmail.com"})
    assert r.ok and mgr.opened == ["https://gmail.com"] and "never type passwords" in r.output
    assert mgr.hold_until > 0 and any(e == "browser.signin_opened" for e, _ in events)
    r = await run_as(ex, hub, "dashboard", "sign in", "open_signin_browser", {"url": "file:///C:/x"})
    assert not r.ok


async def test_task_pin_from_orchestrator_and_fast_path(tmp_path, monkeypatch):
    """Through the real agent loop: 'use Edge from now on' needs no AI call; 'open example.com in
    Chrome' runs in Chrome without touching the saved choice."""
    settings = Settings(agent=AgentConfig(max_attempts=1, max_steps=4), memory=MemoryConfig(auto_extract=False),
                        data_dir=tmp_path, files=FilesConfig(save_dir=str(tmp_path / "files")))
    monkeypatch.setattr("delta.core.orchestrator.known_folder", lambda name: tmp_path)  # no Windows registry here
    hub, calls, ub, _, _ = make_hub(tmp_path, settings=settings)
    queues = {"plan": [js({"needs_tools": True, "goal": "open", "tools": ["browser_open"],
                           "success_criteria": ["example.com is open"]})],
              "chat": [call("browser_open", text="https://example.com"), text("DONE: opened")],
              "verify": [js({"passed": True, "reason": "ok"})]}
    router = ModelRouter({"fake": FakeProvider("fake", handler=lambda task, m: queues[task].pop(0))},
                         {"plan": ["fake"], "chat": ["fake"], "verify": ["fake"]})
    audit = AuditLog(tmp_path / "audit.jsonl")
    ex = ToolExecutor(ToolRegistry(hub.tools()), PolicyEngine(Boundaries()), ApprovalBroker(timeout_s=1), audit)
    orch = Orchestrator(settings, router, ex, None, None, EventBus(), audit)
    orch.browsers = hub

    r = await orch.run(TaskRequest("use Edge from now on", channel="dashboard"))
    assert r.status == "done" and r.fast and "Microsoft Edge" in r.answer
    assert settings.browser.prefer == "edge" and len(queues["plan"]) == 1  # no planner call

    settings.browser.mode = "delta-profile"
    r = await orch.run(TaskRequest("open example.com in Chrome", channel="dashboard"))
    await orch.drain()
    assert ("user", "browser_open") in calls and ub.uses[-1] == "chrome"
    assert settings.browser.mode == "delta-profile" and TASK_BROWSER.get() is None


# --- settings API (back-compat) ----------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from delta.api import settings_routes

    monkeypatch.setattr(settings_routes, "get_secret", lambda n: None)
    monkeypatch.setattr("delta.tools.user_browser.default_browser", lambda prefer="auto": ("edge", "x", "msedge.exe"))
    settings = Settings(data_dir=tmp_path, files=FilesConfig(save_dir=str(tmp_path / "files")),
                        writer=WriterConfig(output_dir=str(tmp_path / "docs")))
    hub, *_ = make_hub(tmp_path, settings=settings)
    events: list = []
    executor = SimpleNamespace(policy=None)
    c = SimpleNamespace(settings=settings, browsers=hub, registry=ToolRegistry(hub.tools()),
                        orchestrator=SimpleNamespace(executor=executor),
                        audit=SimpleNamespace(record=lambda e, **k: events.append((e, k))))
    hub.audit = c.audit
    app = FastAPI()
    settings_routes.register(app, c, lambda: None)  # type: ignore[arg-type]
    with TestClient(app) as tc:
        yield tc, c, events


def test_settings_api_shows_the_four_choices(api):
    tc, _, _ = api
    b = tc.get("/api/settings").json()["browser"]
    assert b["mode"] == "your-browser" and b["prefer"] == "auto" and b["using"] == "edge"  # old fields stay
    assert [o["id"] for o in b["options"]] == ["default", "chrome", "edge", "private"]
    assert b["choice"] == "default" and b["engine"] == "auto" and b["signin_available"] is True


def test_settings_api_old_and_new_ways_to_switch(api):
    tc, c, events = api
    r = tc.put("/api/settings", json={"browser": {"mode": "delta-profile"}}).json()  # an older dashboard
    assert r["ok"] and r["restart_needed"] is False and c.settings.browser.mode == "delta-profile"
    r = tc.put("/api/settings", json={"browser": {"choice": "edge", "orange_cursor": False}}).json()
    assert r["ok"] and "Microsoft Edge" in r["browser_note"]
    assert c.settings.browser.mode == "your-browser" and c.settings.browser.prefer == "edge"
    assert c.settings.browser.orange_cursor is False
    assert any(e == "settings.changed" and k.get("by") == "dashboard" for e, k in events)
    r = tc.put("/api/settings", json={"browser": {"choice": "private", "engine": "edge"}}).json()
    assert c.settings.browser.mode == "delta-profile" and c.settings.browser.private_engine == "edge"
    assert c.settings.browser.prefer == "edge"  # kept for switching back
    unchanged = tc.put("/api/settings", json={"browser": {"choice": "private", "engine": "edge"}}).json()
    assert "browser_note" not in unchanged
    assert tc.put("/api/settings", json={"browser": {"choice": "firefox"}}).status_code == 422


def test_settings_api_refuses_a_missing_browser(api, tmp_path):
    tc, c, _ = api
    c.browsers._detect = lambda desktop=True, automation=True: Installed(chrome=False, edge=True, desktop=True,
                                                                        automation=True)
    r = tc.put("/api/settings", json={"browser": {"choice": "chrome"}})
    assert r.status_code == 400 and "Google Chrome is not installed" in r.json()["detail"]
    assert c.settings.browser.prefer == "auto"


def test_signin_endpoint(api):
    tc, c, _ = api
    r = tc.post("/api/browser/signin", json={"url": "github.com"}).json()
    assert r["ok"] and c.browsers.mgr.opened == ["https://github.com"]


# --- Telegram /browser ---------------------------------------------------------------------------------------

def telegram(tmp_path: Path, g: RemoteGuard | None):
    pytest.importorskip("telegram")
    from delta.channels.telegram import TelegramChannel

    settings = Settings(data_dir=tmp_path, files=FilesConfig(save_dir=str(tmp_path / "files")))
    hub, *_ = make_hub(tmp_path, settings=settings,
                       inst=Installed(chrome=False, edge=True, desktop=True, automation=True))
    events: list = []
    c = SimpleNamespace(settings=settings, browsers=hub, guard=g,
                        audit=SimpleNamespace(record=lambda e, **k: events.append((e, k))))
    hub.audit = c.audit
    ch = TelegramChannel(c, "123456:TEST-TOKEN", ["5"])  # type: ignore[arg-type]
    return ch, c, events


class Msg:
    def __init__(self, text_: str = "") -> None:
        self.text = text_
        self.replies: list[tuple[str, Any]] = []
        self.chat = SimpleNamespace(send_message=self._send)

    async def reply_text(self, text_: str, **kw: Any) -> None:
        self.replies.append((text_, kw.get("reply_markup")))

    async def _send(self, text_: str, **kw: Any) -> None:
        self.replies.append((text_, None))

    async def delete(self) -> None:
        pass


def upd(text_: str = "") -> SimpleNamespace:
    return SimpleNamespace(effective_user=SimpleNamespace(id=5), message=Msg(text_))


def buttons(markup: Any) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def test_telegram_browser_menu_and_pin(tmp_path, monkeypatch):
    monkeypatch.setattr("delta.system.notify.notify", lambda *a, **k: None)
    g = guard(tmp_path)
    ch, c, events = telegram(tmp_path, g)
    u = upd("/browser")
    await ch._browser_cmd(u, SimpleNamespace(args=[]))
    text_, kb = u.message.replies[0]
    assert "Browser now: <b>My default browser</b>" in text_ and "Google Chrome is not installed" in text_
    labels = dict((d, t) for t, d in buttons(kb))
    assert labels["ctl:browser:default"].startswith("✅") and "not available" in labels["ctl:browser:chrome"]
    assert "ctl:browser:private" in labels and "ctl:signin:" in labels

    said: list[str] = []

    async def say(t: str, **kw: Any) -> None:
        said.append(t)

    await ch._apply(say, "browser", "edge", 5)  # no PIN yet: asked for, nothing changed
    assert "Enter your Delta PIN" in said[-1] and c.settings.browser.prefer == "auto"
    pin = upd("4821")
    assert await ch._take_pin(pin, "5")  # the PIN runs the waiting switch
    assert c.settings.browser.prefer == "edge"
    assert any("From now on I'll use Microsoft Edge" in t for t, _ in pin.message.replies) or \
        any("Microsoft Edge" in t for t in said)
    assert any(e == "settings.changed" and k.get("by") == "telegram:5" for e, k in events)

    await ch._apply(say, "browser", "chrome", 5)  # unlocked, but not installed
    assert said[-1].startswith("✗") and "not installed" in said[-1] and c.settings.browser.prefer == "edge"

    g.lock("test", by="test")
    await ch._apply(say, "browser", "default", 5)
    assert "locked" in said[-1] and c.settings.browser.prefer == "edge"
    locked = upd("/browser")
    await ch._browser_cmd(locked, SimpleNamespace(args=[]))
    assert "locked" in locked.message.replies[0][0]


async def test_telegram_signin_needs_the_pin(tmp_path, monkeypatch):
    monkeypatch.setattr("delta.system.notify.notify", lambda *a, **k: None)
    g = guard(tmp_path)
    ch, c, _ = telegram(tmp_path, g)
    u = upd("/signin gmail.com")
    await ch._signin_cmd(u, SimpleNamespace(args=["gmail.com"]))
    assert "PIN" in u.message.replies[0][0] and c.browsers.mgr.opened == []
    pin = upd("4821")
    await ch._take_pin(pin, "5")
    assert c.browsers.mgr.opened == ["https://gmail.com"]
    assert any("is open on your PC" in t for t, _ in [*u.message.replies, *pin.message.replies])


def test_help_lists_the_browser_commands():
    pytest.importorskip("telegram")
    from delta.channels.telegram import HELP

    assert "/browser" in HELP and "/signin" in HELP
