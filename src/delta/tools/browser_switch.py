"""Switching browsers without a restart, and never in the middle of a task.

Both browser kits are built at start-up when this PC can run them: the user's own signed-in
Chrome/Edge (user_browser.py, Windows UI Automation) and Delta's private profile (browser.py,
Playwright over CDP). The registry holds one stand-in per tool name (browser_open, browser_click, ...,
solve_captcha) that hands each call to the kit the *current task* uses.

Each task is pinned to a browser when it starts (Orchestrator.run -> BrowserHub.begin_task): the saved
choice, or a choice for this task only ("open Gmail in Chrome", or set_browser(scope="this_task")).
A saved change made while a task runs (dashboard, Telegram /browser, set_browser) applies from the next
task; the running one finishes where it started. The only exception: a task that has not touched a
browser yet picks up a change its own request made ("from now on use Edge and open Gmail").

Safety is unchanged: every call still goes through ToolExecutor (policy, approvals, Remote Guard,
ExfilGuard) with the real tool's own risk rules, password refusals and captcha refusals, because the
stand-in reports the real tool's arguments, risk and checks.
"""

from __future__ import annotations

import contextlib
import re
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from delta.config import write_user_settings
from delta.core.state import CURRENT_REQUEST
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.browser_choice import (
    CHOICES,
    FULL_NAMES,
    BrowserPick,
    Installed,
    describe,
    detect,
    from_config,
    mentions_browser,
    parse_choice,
    parse_engine,
    task_override,
    to_changes,
    unavailable_reason,
)

# Tool names both kits provide, in the order the model sees them; solve_captcha and the extra actions (select,
# check, upload, hover, wait, tabs, dialog: delta.tools.browser_actions) exist only in the private browser.
BROWSER_NAMES = ("browser_open", "browser_search", "browser_read", "browser_click", "browser_fill",
                 "browser_press", "browser_scroll", "browser_back", "browser_select", "browser_check",
                 "browser_upload", "browser_hover", "browser_wait", "browser_tabs", "browser_dialog", "solve_captcha")
SIGNIN_HOLD_S = 30 * 60  # the private browser stays open this long for the user to sign in


@dataclass
class TaskBrowser:
    """The browser one task uses. Fixed when the task starts; `used` once a browser tool ran."""

    pick: BrowserPick
    source: str = "settings"  # settings | this task
    used: bool = False
    note: str = ""  # told to the model once with the first browser result (e.g. Chrome is not installed)


TASK_BROWSER: ContextVar[TaskBrowser | None] = ContextVar("delta_task_browser", default=None)


class BrowserHub:
    def __init__(self, settings: Any, audit: Any = None, detect_fn: Any = detect) -> None:
        self.settings = settings
        self.audit = audit
        self._detect = detect_fn
        self.user_tools: dict[str, Tool] = {}
        self.private_tools: dict[str, Tool] = {}
        self.ub: Any = None  # user_browser.UserBrowser
        self.mgr: Any = None  # browser.BrowserManager
        self._ub_prefer = getattr(settings.browser, "prefer", "auto")
        self._active: dict[str, TaskBrowser] = {}

    # --- set-up --------------------------------------------------------------------------------------
    def add_user(self, tools: list[Tool]) -> None:
        self.user_tools = {t.name: t for t in tools}
        self.ub = next((getattr(t, "ub", None) for t in tools if getattr(t, "ub", None) is not None), None)

    def add_private(self, tools: list[Tool], mgr: Any) -> None:
        self.private_tools = {t.name: t for t in tools}
        self.mgr = mgr
        mgr.engine = getattr(self.settings.browser, "private_engine", "auto")

    def tools(self) -> list[Tool]:
        """What to register: one stand-in per browser tool name, plus set_browser and open_signin_browser."""
        names = [n for n in BROWSER_NAMES if n in self.user_tools or n in self.private_tools]
        out: list[Tool] = [proxy_for(self, n) for n in names]
        if self.user_tools or self.private_tools:
            out.append(SetBrowser(self))
        if self.mgr is not None:
            out.append(OpenSigninBrowser(self))
        return out

    def installed(self) -> Installed:
        return self._detect(desktop=bool(self.user_tools), automation=bool(self.private_tools))

    # --- which browser now -----------------------------------------------------------------------------
    def configured(self) -> BrowserPick:
        return from_config(self.settings.browser)

    def pick_now(self) -> BrowserPick:
        tb = TASK_BROWSER.get()
        return tb.pick if tb else self.configured()

    def kind_now(self) -> str | None:
        """Which kit serves calls now: the pick's kit, or the other one when this PC lacks it."""
        pick = self.pick_now()
        if pick.kind == "user" and self.user_tools:
            return "user"
        if self.private_tools and (pick.kind == "private" or not self.user_tools):
            return "private"
        return "user" if self.user_tools else None

    def target(self, name: str) -> Tool | None:
        kind = self.kind_now()
        kit = self.user_tools if kind == "user" else self.private_tools if kind == "private" else {}
        return kit.get(name)

    # --- task boundaries ---------------------------------------------------------------------------------
    def begin_task(self, task_id: str, text: str = "") -> Token[TaskBrowser | None]:
        """Pin the browser for a task: a choice its request names for this task only, else the saved one."""
        pick, source, note = self.configured(), "settings", ""
        override = task_override(text) if text else None
        if override is not None:
            if override.choice == "private" and not override.engine:
                override = BrowserPick("private", self.settings.browser.private_engine)
            why = unavailable_reason(override.choice, self.installed(), override.engine)
            if why:
                note = (f"The user asked for {override.label} for this task, but it is not available: {why} "
                        f"Using {pick.label} instead; tell the user.")
            else:
                pick, source = override, "this task"
        tb = TaskBrowser(pick, source, note=note)
        self._active[task_id] = tb
        return TASK_BROWSER.set(tb)

    def end_task(self, task_id: str, token: Token[TaskBrowser | None]) -> None:
        self._active.pop(task_id, None)
        with contextlib.suppress(ValueError):
            TASK_BROWSER.reset(token)

    async def prepare(self) -> str:
        """Before a browser call: make the kit's browser the one this task uses. Runs at the first call of
        a task (the same browser for every later call). Returns a note for the model, once."""
        tb = TASK_BROWSER.get()
        pick, kind = self.pick_now(), self.kind_now()
        if tb is not None:
            tb.used = True
        if kind == "user" and self.ub is not None:
            prefer = pick.prefer if pick.kind == "user" else self.settings.browser.prefer
            if prefer != self._ub_prefer:
                self.ub.use(prefer)  # the next action opens a window there
                self._ub_prefer = prefer
        elif kind == "private" and self.mgr is not None:
            engine = (pick.engine if pick.kind == "private" else "") or self.settings.browser.private_engine
            if engine != self.mgr.engine:
                if self.mgr.running:
                    await self.mgr.close()  # only the browser Delta started; a new one opens on first use
                self.mgr.engine = engine
        note = ""
        if tb is not None and tb.note:
            note, tb.note = tb.note, ""
        return note

    def running_elsewhere(self, new: BrowserPick) -> list[TaskBrowser]:
        """Tasks that already use a browser other than `new` (they finish where they started)."""
        mine = TASK_BROWSER.get()
        return [tb for tb in self._active.values() if tb is not mine and tb.used and tb.pick != new]

    def after_saved(self, new: BrowserPick) -> str:
        """What a saved switch means for tasks running now, in plain words ('' when nothing to say)."""
        note = ""
        tb = TASK_BROWSER.get()
        if tb is not None and tb.source == "settings":
            if not tb.used:
                tb.pick = new  # this task has not opened a browser yet: it can use the new one
            elif tb.pick != new:
                note = f" This task keeps using {tb.pick.label}; the switch applies from your next task."
        if self.running_elsewhere(new):
            note += (" A task is using the browser right now, so it finishes there; the switch applies from "
                     "the next task.")
        return note

    def _private_in_use(self) -> bool:
        return any(tb.used and tb.pick.kind == "private" for tb in self._active.values())

    # --- "Sign in to my sites" -------------------------------------------------------------------------
    async def open_signin(self, url: str = "") -> ToolResult:
        """Open Delta's private browser visibly on `url` (or a blank tab) so the user signs in once.
        Delta does not type anything there: the person signs in, and the profile keeps the sign-in."""
        if self.mgr is None:
            return ToolResult(False, "Delta's private browser is not available on this PC (it needs Google Chrome "
                                     "or Microsoft Edge and the browser add-on).")
        try:
            url = clean_url(url)
        except ValueError as e:
            return ToolResult(False, str(e))
        inst = self.installed()
        if not inst.any:
            return ToolResult(False, "Neither Google Chrome nor Microsoft Edge is installed on this PC.")
        engine = self.settings.browser.private_engine
        if engine != self.mgr.engine and not self._private_in_use():
            if self.mgr.running:
                await self.mgr.close()
            self.mgr.engine = engine
        try:
            page = await self.mgr.page()
            blank = page.url in ("", "about:blank") or page.url.startswith(
                ("chrome://new", "edge://new", "chrome-search://", "about:newtab", "edge://newtab"))
            tab = page if blank else await page.context.new_page()
            if url:
                await tab.goto(url, wait_until="domcontentloaded", timeout=30_000)
            with contextlib.suppress(Exception):
                await tab.bring_to_front()
        except Exception as e:
            return ToolResult(False, f"I could not open Delta's private browser: {type(e).__name__}: {str(e)[:160]}")
        self.mgr.hold_until = time.monotonic() + SIGNIN_HOLD_S
        host = (urlparse(url).hostname or "") if url else ""
        if self.audit is not None:
            self.audit.record("browser.signin_opened", host=host, browser=self.mgr.browser_name or engine)
        where = f"on {host}" if host else "on a blank tab"
        name = FULL_NAMES.get(self.mgr.browser_name, "")
        return ToolResult(True, (
            f"Delta's private browser{f' ({name})' if name else ''} is open on your PC {where}. Sign in there "
            "yourself; I never type passwords. Your sign-ins stay saved in that browser, so tasks that use "
            "Delta's private browser can use them. It stays open for 30 minutes."),
            {"verified": True, "url": url, "browser": self.mgr.browser_name, "signin": True})


def clean_url(url: str) -> str:
    """'' (blank tab), or an http(s) address. 'gmail.com' -> 'https://gmail.com'."""
    url = (url or "").strip()
    if not url or url in ("about:blank", "blank"):
        return ""
    if "://" not in url:
        if re.match(r"^[a-z][a-z0-9+.-]*:(?!\d)", url, re.I):  # javascript:, data:, file: and friends
            raise ValueError("Only a normal web address (https://...) can be opened for signing in.")
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only a normal web address (https://...) can be opened for signing in.")
    return url


# --- saving a choice (dashboard, Telegram, chat all come here) ------------------------------------------

def save_choice(settings: Any, audit: Any, choice: str, engine: str | None = None, by: str = "",
                hub: BrowserHub | None = None, inst: Installed | None = None) -> str:
    """Save a browser choice and say, in plain words, what happens now. ValueError: not possible here."""
    picked = parse_choice(choice) or choice
    if picked not in CHOICES:
        raise ValueError(f"I don't know a browser called '{choice}'. Choose: my default browser, Chrome, Edge "
                         "or Delta's private browser.")
    if engine is not None and engine not in ("auto", "chrome", "edge"):
        raise ValueError(f"unknown engine '{engine}'")
    inst = inst or (hub.installed() if hub else detect())
    before = from_config(settings.browser)
    why = unavailable_reason(picked, inst, engine or settings.browser.private_engine)
    if why:
        raise ValueError(why)
    changes = to_changes(picked, engine)
    write_user_settings(settings.data_dir, {"browser": changes})
    for k, v in changes.items():
        setattr(settings.browser, k, v)
    after = from_config(settings.browser)
    if audit is not None:
        audit.record("settings.changed", sections=["browser"], browser=after.choice, engine=after.engine, by=by)
    if after == before:
        msg = f"I'm already using {describe(after, inst)}. Nothing changed."
    else:
        msg = f"Done. From now on I'll use {describe(after, inst)}."
        if after.choice == "private":
            msg += (" To use your accounts there, say \"sign in to my sites\" once (I open it and you sign in; "
                    "I never type passwords).")
    return msg + (hub.after_saved(after) if hub else "")


def save_engine(settings: Any, audit: Any, engine: str, by: str = "", hub: BrowserHub | None = None,
                inst: Installed | None = None) -> str:
    """Which installed browser runs Delta's private browser. The main choice is unchanged."""
    if engine not in ("auto", "chrome", "edge"):
        raise ValueError(f"unknown engine '{engine}'")
    inst = inst or (hub.installed() if hub else detect())
    if engine != "auto" and not inst.has(engine):
        raise ValueError(f"{FULL_NAMES[engine]} is not installed on this PC.")
    write_user_settings(settings.data_dir, {"browser": {"private_engine": engine}})
    settings.browser.private_engine = engine
    if audit is not None:
        audit.record("settings.changed", sections=["browser"], private_engine=engine, by=by)
    runs = FULL_NAMES.get(inst.engine_target(engine) or "", "")
    msg = f"Done. Delta's private browser runs in {runs or engine}."
    now = from_config(settings.browser)
    if now.choice == "private":
        msg += hub.after_saved(now) if hub else ""
    else:
        msg += f" You are using {now.label} right now; this applies when you switch to the private browser."
    return msg


# --- the stand-in tools ------------------------------------------------------------------------------------

class _NoTarget(BaseModel):
    model_config = ConfigDict(extra="allow")


class BrowserProxy(Tool[Any]):
    """One registry entry per browser tool name; each call goes to the kit the current task uses.
    Everything the safety layers read (Args, risk, domains, summary, input checks) is the real tool's."""

    def __init__(self, hub: BrowserHub) -> None:
        self._hub = hub

    def _target(self) -> Tool | None:
        return self._hub.target(self.name)

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("__") or attr == "_hub":
            raise AttributeError(attr)
        hub = self.__dict__.get("_hub")
        target = hub.target(type(self).name) if hub is not None else None
        if target is None:
            fallback = {"Args": _NoTarget, "risk": Risk.ACT,
                        "description": f"{type(self).name} (not available with the browser Delta uses now)"}
            if attr in fallback:
                return fallback[attr]
            raise AttributeError(attr)
        return getattr(target, attr)

    def available(self) -> bool:
        t = self._target()
        return t is not None and t.available()

    def spec(self) -> dict[str, Any]:
        t = self._target()
        return t.spec() if t is not None else super().spec()

    def risk_for(self, args: Any) -> Risk:
        t = self._target()
        return t.risk_for(args) if t is not None else Risk.ACT

    def paths(self, args: Any) -> list[str]:
        t = self._target()
        return t.paths(args) if t is not None else []

    def undoable(self, args: Any) -> bool:
        t = self._target()
        return t.undoable(args) if t is not None else False

    def domains(self, args: Any) -> list[str]:
        t = self._target()
        return t.domains(args) if t is not None else []

    def app(self, args: Any) -> str | None:
        t = self._target()
        return t.app(args) if t is not None else None

    def scope(self, args: Any) -> str:
        t = self._target()
        return t.scope(args) if t is not None else ""

    def summary(self, args: Any) -> str:
        t = self._target()
        return t.summary(args) if t is not None else f"{self.name}()"

    async def run(self, args: Any) -> ToolResult:
        if self._target() is None:
            return ToolResult(False, f"{self.name} works only in Delta's private browser, and this task uses "
                                     f"{self._hub.pick_now().label}. Do it another way or tell the user.")
        note = await self._hub.prepare()
        t = self._target()
        assert t is not None
        result = await t.run(args)
        if note:
            result = ToolResult(result.ok, f"{result.output}\n(Note: {note})", result.evidence)
        return result


_PROXIES: dict[str, type[BrowserProxy]] = {}


def proxy_for(hub: BrowserHub, name: str) -> BrowserProxy:
    cls = _PROXIES.get(name)
    if cls is None:
        cls = _PROXIES[name] = type(f"BrowserProxy_{name}", (BrowserProxy,), {"name": name})
    return cls(hub)


class SetBrowserArgs(BaseModel):
    choice: Literal["default", "chrome", "edge", "private"] = Field(
        description="default = the user's own default browser; chrome / edge = that browser, signed in; "
                    "private = Delta's private browser (background, signed out until the user signs in)")
    scope: Literal["always", "this_task"] = Field(
        "always", description="always = save it (\"from now on use Edge\"); this_task = only for the current "
                              "task (\"do this in Chrome\"), the saved choice stays")
    engine: Literal["auto", "chrome", "edge"] | None = Field(
        None, description="private only: which installed browser runs it (leave empty to keep the saved one)")

    @field_validator("choice", mode="before")
    @classmethod
    def _choice(cls, v: Any) -> Any:
        return parse_choice(str(v)) or v

    @field_validator("engine", mode="before")
    @classmethod
    def _engine(cls, v: Any) -> Any:
        return None if v in (None, "") else (parse_engine(str(v)) or v)


class SetBrowser(Tool[SetBrowserArgs]):
    name = "set_browser"
    description = ("Choose which browser Delta uses: the user's default browser, Chrome, Edge (all signed in) or "
                   "Delta's private browser. scope=always saves it (from the next task; only when the user asked); "
                   "scope=this_task applies to the current task only.")
    risk = Risk.ACT
    Args = SetBrowserArgs

    def __init__(self, hub: BrowserHub) -> None:
        self.hub = hub

    def remote_pin(self, args: SetBrowserArgs) -> str | None:
        """Saving the choice from a phone is a settings change: Remote Guard asks for the PIN."""
        return "change which browser Delta uses" if args.scope == "always" else None

    def summary(self, args: SetBrowserArgs) -> str:
        what = BrowserPick(args.choice, args.engine or "auto").label
        return f"use {what}" + (" for this task" if args.scope == "this_task" else " from now on")

    async def run(self, args: SetBrowserArgs) -> ToolResult:
        hub = self.hub
        inst = hub.installed()
        if args.scope == "this_task":
            tb = TASK_BROWSER.get()
            if tb is None:
                return ToolResult(False, "A browser for one task can only be chosen while a task runs. Use "
                                         "scope=always to save the choice.")
            engine = args.engine or hub.settings.browser.private_engine
            pick = BrowserPick(args.choice, engine if args.choice == "private" else "auto")
            if why := unavailable_reason(pick.choice, inst, pick.engine):
                return ToolResult(True, f"{why} I kept {tb.pick.label} for this task.",
                                  {"verified": True, "changed": False})
            tb.pick, tb.source = pick, "this task"
            return ToolResult(True, f"For this task I'll use {describe(pick, inst)}. Your saved choice stays "
                                    f"{hub.configured().label}.",
                              {"verified": True, "browser": pick.choice, "scope": "this_task"})
        req = CURRENT_REQUEST.get()
        if req is not None and not mentions_browser(req.text):
            return ToolResult(False, "REFUSED: the saved browser changes only when the user's own message asks "
                                     "for it. Use scope=this_task, or ask the user.")
        by = f"{req.channel}:{req.user}" if req is not None else "agent"
        try:
            msg = save_choice(hub.settings, hub.audit, args.choice, args.engine, by=by, hub=hub, inst=inst)
        except ValueError as e:
            return ToolResult(True, f"{e} I kept {hub.configured().label}.", {"verified": True, "changed": False})
        return ToolResult(True, msg, {"verified": True, "browser": hub.configured().choice, "scope": "always"})


class SigninArgs(BaseModel):
    url: str = Field("", description="The site to sign in to, e.g. https://mail.google.com. Empty = a blank tab.")


class OpenSigninBrowser(Tool[SigninArgs]):
    name = "open_signin_browser"
    description = ("Open Delta's private browser visibly on the PC at a site (or a blank tab) so the USER signs in "
                   "once; the sign-in is kept for later tasks. Delta never types passwords there.")
    risk = Risk.ACT
    Args = SigninArgs

    def __init__(self, hub: BrowserHub) -> None:
        self.hub = hub

    def domains(self, args: SigninArgs) -> list[str]:
        return [args.url] if args.url.strip() else []

    async def run(self, args: SigninArgs) -> ToolResult:
        return await self.hub.open_signin(args.url)


__all__ = ["TASK_BROWSER", "BrowserHub", "BrowserProxy", "OpenSigninBrowser", "SetBrowser", "TaskBrowser",
           "clean_url", "save_choice", "save_engine"]
