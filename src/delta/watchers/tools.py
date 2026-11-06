"""The model's side of watchers: set one up, list them, check one now, and learn where things are on a site.

watch_portal and save_watch_recipe change what runs later, unattended, so they are WRITE actions (asked at the
default permission level) and from a phone they need the PIN (Remote Guard). What a watcher reads from a site is
outside content: check_watcher and inspect_portal mark the task as having read it (taint), so a page that says
"send this somewhere" cannot make Delta do it without the user's yes.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from delta.core.state import CURRENT_REQUEST
from delta.tools.base import Risk, Tool, ToolResult
from delta.watchers.diff import clean_items, line
from delta.watchers.recipe import EXTRACT_JS, INSPECT_JS, MAX_FIELD, Recipe, Section, section_url
from delta.watchers.service import WatchService
from delta.watchers.store import MIN_INTERVAL_MINUTES


class WatchArgs(BaseModel):
    login: str = Field(description="The saved login to sign in with (list_logins)")
    what: list[str] = Field(min_length=1, max_length=8, description="What to check, in the user's words, e.g. "
                            "['timetable', 'upcoming exams', 'notifications']")
    every_hours: float = Field(default=6, ge=1, le=168, description="How often (at most hourly; default 6)")
    notify: Literal["instant", "digest"] = Field(default="instant", description="instant = tell the user as soon "
                                                 "as something new shows; digest = one message a day")
    digest_time: str = Field(default="18:00", pattern=r"^\d{2}:\d{2}$")
    name: str = Field(default="", max_length=80)


class WatchPortal(Tool[WatchArgs]):
    name = "watch_portal"
    description = ("Watch a site the user has a saved login for (a college ERP or LMS...): every few hours Delta signs "
                   "in, reads what they asked for and tells them only what is new or changed. Ask the user first "
                   "what to check and how often if they did not say.")
    risk = Risk.WRITE
    Args = WatchArgs

    def __init__(self, service: WatchService) -> None:
        self.service = service

    def available(self) -> bool:
        return self.service.logins.store.count() > 0

    def summary(self, args: WatchArgs) -> str:
        return (f"watch {args.login} for {', '.join(args.what)} every {args.every_hours:g} h "
                f"({'daily digest' if args.notify == 'digest' else 'tell you at once'})")

    async def run(self, args: WatchArgs) -> ToolResult:
        req = CURRENT_REQUEST.get()
        try:
            w = self.service.create(args.name, args.login, args.what,
                                    interval_minutes=max(MIN_INTERVAL_MINUTES, int(args.every_hours * 60)),
                                    notify=args.notify, digest_time=args.digest_time,
                                    owner_channel=req.channel if req else "api", owner_user=req.user if req else "")
        except ValueError as e:
            return ToolResult(False, str(e))
        login = self.service.logins.store.get(w.login)
        perm = (" It will ask before each sign-in (change that in Settings > Logins)."
                if login is not None and login.permission == "ask" else "")
        return ToolResult(True, (f"Watching {w.name}: {w.what}, every {w.interval_minutes // 60} h, "
                                 f"{'daily digest at ' + w.digest_time if w.notify == 'digest' else 'told at once'}. "
                                 f"The first check runs in a minute.{perm}"),
                          {"verified": True, "watcher": w.id})


class NoArgs(BaseModel):
    pass


class ListWatchers(Tool[NoArgs]):
    name = "list_watchers"
    description = "List the sites Delta watches (what, how often, last check)."
    risk = Risk.READ
    Args = NoArgs

    def __init__(self, service: WatchService) -> None:
        self.service = service

    def available(self) -> bool:
        return bool(self.service.store.list()) or self.service.logins.store.count() > 0

    async def run(self, args: NoArgs) -> ToolResult:
        items = self.service.store.list()
        if not items:
            return ToolResult(True, "No watchers yet.", {"count": 0})
        rows = [f"- {w.name} [{w.id}]: {w.what or ', '.join(s.name for s in w.recipe.sections)}; every "
                f"{w.interval_minutes // 60} h; last check: {w.last_status or 'not yet'} {w.last_message}".rstrip()
                for w in items]
        return ToolResult(True, "\n".join(rows), {"count": len(items)})


class RefArgs(BaseModel):
    watcher: str = Field(description="The watcher's name or id (list_watchers)")


class CheckWatcher(Tool[RefArgs]):
    name = "check_watcher"
    description = "Check a watched site now and report what is new (at most every 10 minutes per watcher)."
    risk = Risk.ACT
    Args = RefArgs
    untrusted_output = True

    def __init__(self, service: WatchService) -> None:
        self.service = service

    def available(self) -> bool:
        return bool(self.service.store.list())

    async def run(self, args: RefArgs) -> ToolResult:
        w = self.service.store.find(args.watcher)
        if w is None:
            return ToolResult(False, f"no watcher called '{args.watcher}'")
        req = CURRENT_REQUEST.get()
        report = await self.service.check(w, trigger="manual", channel=req.channel if req else "",
                                          user=req.user if req else "", hold_lock=False)
        text = report.text or f"{w.name}: {report.summary}"
        return ToolResult(report.status in ("done", "skipped"), text, {"watcher": w.id, "status": report.status})


class InspectArgs(BaseModel):
    pass


class InspectPortal(Tool[InspectArgs]):
    name = "inspect_portal"
    description = ("On the page open in Delta's private browser, list the tables and lists that can be watched, "
                   "with a CSS selector for each rows and the page's own links. Use to learn a watcher recipe.")
    risk = Risk.READ
    Args = InspectArgs
    untrusted_output = True

    def __init__(self, service: WatchService) -> None:
        self.service = service

    def available(self) -> bool:
        return bool(self.service.store.list()) and getattr(self.service.hub, "mgr", None) is not None

    async def run(self, args: InspectArgs) -> ToolResult:
        mgr = getattr(self.service.hub, "mgr", None)
        if mgr is None or not mgr.running:
            return ToolResult(False, "Delta's private browser is not open; sign_in to the site first")
        page = await mgr.page()
        try:
            found = await page.evaluate(INSPECT_JS)
        except Exception as e:
            return ToolResult(False, f"could not read the page ({type(e).__name__})")
        rows = [f"Page: {page.url}"]
        for p in (found or {}).get("parts", [])[:12]:
            cols = f" columns: {', '.join(p.get('columns') or [])}" if p.get("columns") else ""
            rows.append(f"- {p.get('kind')} \"{p.get('heading', '')}\": rows=\"{p.get('rows')}\" "
                        f"({p.get('count')} rows){cols}; first: {str(p.get('sample', ''))[:80]}")
        links = [f"{x.get('text', '')[:30]} -> {x.get('path', '')[:80]}" for x in (found or {}).get("links", [])[:25]]
        if links:
            rows.append("Links on this site: " + "; ".join(links))
        return ToolResult(True, "\n".join(rows), {"url": page.url})


class RecipeArgs(BaseModel):
    watcher: str = Field(description="The watcher's name or id")
    sections: list[Section] = Field(min_length=1, max_length=8, description="One per thing to watch: name (the "
                                    "user's word), url (a path on the site), rows (CSS selector for one item each, "
                                    "from inspect_portal; empty = automatic), optional fields and key")

    @field_validator("sections")
    @classmethod
    def _names(cls, v: list[Section]) -> list[Section]:
        if len({s.name.lower() for s in v}) != len(v):
            raise ValueError("each section needs its own name")
        return v


class SaveWatchRecipe(Tool[RecipeArgs]):
    name = "save_watch_recipe"
    description = ("Save where each watched thing is on the site (a recipe), after checking it on the live pages in "
                   "Delta's private browser (sign_in first). Kept as a skill for that site.")
    risk = Risk.WRITE
    Args = RecipeArgs

    def __init__(self, service: WatchService) -> None:
        self.service = service

    def available(self) -> bool:
        return bool(self.service.store.list())

    def summary(self, args: RecipeArgs) -> str:
        return f"save where to read {', '.join(s.name for s in args.sections)} for watcher {args.watcher}"

    async def run(self, args: RecipeArgs) -> ToolResult:
        w = self.service.store.find(args.watcher)
        if w is None:
            return ToolResult(False, f"no watcher called '{args.watcher}'")
        login = self.service.logins.store.get(w.login)
        mgr = getattr(self.service.hub, "mgr", None)
        if login is None or mgr is None:
            return ToolResult(False, "the watcher's login or Delta's private browser is missing")
        page = await mgr.page()
        report, bad = [], []
        for s in args.sections:
            try:
                url = section_url(login.site, s.url) if s.url else page.url
            except ValueError as e:
                return ToolResult(False, str(e))
            try:
                if url != page.url:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    await self.service._settle(page)
                raw = await page.evaluate(EXTRACT_JS, {"rows": s.rows, "fields": s.fields, "name": s.name,
                                                       "limit": 50, "chars": MAX_FIELD})
            except Exception as e:
                return ToolResult(False, f"could not open {url} ({type(e).__name__})")
            status = str((raw or {}).get("status", "missing"))
            if status == "missing":
                bad.append(s.name)
                continue
            items = clean_items(list((raw or {}).get("items") or []))
            sample = "; ".join(line(x, 80) for x in items[:2])
            report.append(f"- {s.name}: {len(items)} item(s){': ' + sample if sample else ' (empty now)'}")
        if bad:
            return ToolResult(False, f"not saved: could not read {', '.join(bad)} with that recipe (or the site "
                                     "signed Delta out: sign_in first). Use inspect_portal on the right page.")
        self.service.save_recipe(w.id, Recipe(sections=args.sections))
        return ToolResult(True, "Saved. The watcher now reads:\n" + "\n".join(report), {"verified": True})


def watcher_tools(service: WatchService) -> list[Tool[Any]]:
    return [WatchPortal(service), ListWatchers(service), CheckWatcher(service), InspectPortal(service),
            SaveWatchRecipe(service)]
