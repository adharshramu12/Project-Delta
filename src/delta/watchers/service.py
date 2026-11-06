"""Watching a portal: sign in with a saved login, read what the user asked for, tell them only what is new.

Each watcher runs on Delta's scheduler (a schedule of kind "watch"), so quiet hours, missed runs after sleep, the
run history and the Telegram / dashboard delivery are the scheduler's own. One run:

1. Opens one tab in Delta's private browser (one browser at a time: runs share Delta's one-heavy-task lock), and
   closes it afterwards; when the browser was not open before the run, it is closed again.
2. Opens each section's page. A sign-in form or a "session expired" page means the site signed Delta out: Delta
   signs in again with the saved login (asking first unless the login is "always allowed"), then reads on.
3. Cuts each page into items with the recipe, with no AI at all: nothing on the portal can steer Delta during a run
   (instructions inside pages are just text in an item). A section whose page no longer looks like the recipe says
   is reported as "couldn't read X"; nothing is invented and its last snapshot is kept.
4. Compares with the last snapshot (sealed with DPAPI), saves the new one, and notifies only new or changed items:
   at once, or in a daily digest. The first run only says what is now being watched.
5. New exams, assignments, deadlines and events are offered as reminders; one becomes a reminder only when the user
   says so (dashboard button, or asking Delta, which goes through the normal approvals).

A site that is down or a sign-in that needs the user (one-time code, captcha) ends the run with a plain message; the
user is told once when a watcher starts failing, not on every run.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.logins.origin import mismatch
from delta.logins.signin import EXPIRED_WORDS, PASSWORD_SEL
from delta.safety.remote_guard import is_remote
from delta.system.logging import log
from delta.watchers.diff import SectionDiff, clean_items, diff, line
from delta.watchers.recipe import EXTRACT_JS, MAX_FIELD, MAX_ITEMS, Recipe, Section, default_recipe, section_url
from delta.watchers.store import DEFAULT_INTERVAL_MINUTES, MIN_INTERVAL_MINUTES, Watcher, WatchStore

_clock = time.monotonic  # module level so tests can drive it without touching asyncio's clock
MANUAL_GAP_S = 10 * 60  # "check now" at most this often per watcher (respect the site)
LINES_PER_SECTION = 8
OFFER_WORDS = re.compile(r"exam|assignment|deadline|due|test|quiz|submission|event|lab|viva|project|seminar", re.I)
NAV_TIMEOUT_MS = 30_000


@dataclass
class RunReport:
    status: str  # done | failed | needs_user | skipped
    text: str = ""  # what to tell the user ('' = nothing new: say nothing)
    summary: str = ""  # one line for the run history
    diffs: list[SectionDiff] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)


def plain(text: str, limit: int = 300) -> str:
    return " ".join((text or "").split())[:limit]


class WatchService:
    def __init__(self, store: WatchStore, logins: Any, hub: Any, scheduler: Any = None, skills: Any = None,
                 audit: Any = None, lock: asyncio.Semaphore | None = None, guard: Any = None,
                 settle: Any = None) -> None:
        self.store = store
        self.logins = logins
        self.hub = hub
        self.scheduler = scheduler
        self.skills = skills
        self.audit = audit
        self.lock = lock or asyncio.Semaphore(1)
        self.guard = guard
        self.settle = settle
        self._manual: dict[str, float] = {}

    # --- creating and removing ---------------------------------------------------------------------------
    def learned_recipe(self, host: str) -> tuple[Recipe | None, int | None]:
        """A recipe learned earlier for this site (a skill named 'Read <host>'), if any and not retired."""
        if self.skills is None:
            return None, None
        for s in self.skills.list(include_retired=False):
            if s.source == "watcher" and s.name == f"Read {host}":
                sections = []
                for row in s.procedure:
                    parts = [p.strip() for p in row.split(" | ")]
                    if len(parts) != 5:
                        continue
                    with contextlib.suppress(Exception):
                        sections.append(Section(name=parts[0], url=parts[1], rows=parts[2],
                                                fields=json.loads(parts[3] or "{}"),
                                                key=[k for k in parts[4].split(",") if k]))
                if sections:
                    return Recipe(sections=sections[:8]), s.id
        return None, None

    def create(self, name: str, login_ref: str, what: list[str], *, interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
               notify: str = "instant", digest_time: str = "18:00", owner_channel: str = "api",
               owner_user: str = "", recipe: Recipe | None = None) -> Watcher:
        login = self.logins.store.get(login_ref)
        if login is None:
            raise ValueError(f"no saved login called '{login_ref}'. Add it first: Settings > Logins on the PC, or "
                             "`delta logins add`.")
        interval = max(MIN_INTERVAL_MINUTES, int(interval_minutes or DEFAULT_INTERVAL_MINUTES))
        learned, skill_id = self.learned_recipe(login.site.host)
        use = recipe or learned or default_recipe(what)
        w = self.store.add(name or f"{login.name}: {', '.join(what)[:50]}", login.id, use, what=", ".join(what),
                           interval_minutes=interval, notify=notify, digest_time=digest_time,
                           owner_channel=owner_channel, owner_user=owner_user)
        if skill_id is not None and recipe is None:
            self.store.update(w.id, skill_id=skill_id)
        self._schedule(w)
        if self.audit is not None:
            self.audit.record("watch.created", watcher=w.id, login=login.id, site=login.origin,
                              every_minutes=interval, notify=notify, channel=owner_channel)
        return self.store.get(w.id) or w

    def _schedule(self, w: Watcher) -> None:
        if self.scheduler is None:
            return
        from delta.scheduler.rules import Rule, iso, now_utc

        start = now_utc() + timedelta(minutes=1)  # the first look soon after it is made
        s = self.scheduler.store.add(f"Watch: {w.name}"[:80], f"watch:{w.id}",
                                     Rule("interval", minutes=w.interval_minutes, anchor=iso(start) or ""),
                                     kind="watch", owner_channel=w.owner_channel, owner_user=w.owner_user)
        fields: dict[str, Any] = {"schedule_id": s.id}
        if w.notify == "digest":
            d = self.scheduler.store.add(f"Digest: {w.name}"[:80], f"digest:{w.id}", Rule("daily", time=w.digest_time),
                                         kind="watch", owner_channel=w.owner_channel, owner_user=w.owner_user)
            fields["digest_schedule_id"] = d.id
        self.store.update(w.id, **fields)
        with contextlib.suppress(Exception):
            self.scheduler.poke()

    def remove(self, ref: str) -> Watcher | None:
        w = self.store.find(ref)
        if w is None:
            return None
        if self.scheduler is not None:
            for sid in (w.schedule_id, w.digest_schedule_id):
                if sid:
                    self.scheduler.store.delete(sid)
        self.store.delete(w.id)
        if self.audit is not None:
            self.audit.record("watch.removed", watcher=w.id)
        return w

    def save_recipe(self, ref: str, recipe: Recipe) -> Watcher:
        """Use this recipe from now on, and keep it as a skill for the site (shown, retired, shared like any)."""
        w = self.store.find(ref)
        if w is None:
            raise ValueError(f"no watcher called '{ref}'")
        login = self.logins.store.get(w.login)
        skill_id = w.skill_id
        if self.skills is not None and login is not None:
            rows = [f"{s.name} | {s.url} | {s.rows} | {json.dumps(s.fields)} | {','.join(s.key)}"
                    for s in recipe.sections]
            name = f"Read {login.site.host}"
            for old in self.skills.list(include_retired=True):  # an older recipe for this site is replaced
                if old.source == "watcher" and old.name == name and old.procedure != rows:
                    self.skills.delete(old.id)
            change = self.skills.learn(name, f"watch {login.name} ({login.site.host}): "
                                       + ", ".join(s.name for s in recipe.sections), rows,
                                       ["watch_portal", f"site:{login.site.host}"], source="watcher")
            skill_id = change.skill.id
        self.store.update(w.id, recipe=recipe, skill_id=skill_id)
        if self.audit is not None:
            self.audit.record("watch.recipe_saved", watcher=w.id, sections=len(recipe.sections), skill=skill_id)
        return self.store.get(w.id) or w

    # --- running -------------------------------------------------------------------------------------------
    async def run_schedule(self, s: Any, channel: str, user: str, trigger: str = "schedule") -> tuple[str, str, str]:
        """Called by the scheduler for a schedule of kind "watch": (status, text to deliver or '', summary)."""
        kind, _, wid = str(s.prompt).partition(":")
        w = self.store.get(wid)
        if w is None:
            return "failed", "", "this watcher was removed"
        if kind == "digest":
            text = self.digest(w)
            return "done", text, "digest sent" if text else "nothing for the digest"
        report = await self.check(w, trigger=trigger, channel=channel, user=user)
        return report.status, report.text, report.summary

    async def check(self, w: Watcher, trigger: str = "manual", channel: str = "", user: str = "",
                    hold_lock: bool = True) -> RunReport:
        """One run. hold_lock=False only from inside a task, which already holds Delta's one-task lock."""
        if trigger == "manual":
            # No entry means never checked. Do not default to 0.0: monotonic() counts from boot, so on a
            # machine up for less than MANUAL_GAP_S the very first manual check would be refused.
            last = self._manual.get(w.id)
            now = _clock()
            if last is not None and now - last < MANUAL_GAP_S:
                wait = int((MANUAL_GAP_S - (now - last)) // 60) + 1
                return RunReport("skipped", f"I checked {w.name} a moment ago; to go easy on the site I'll check "
                                            f"again in about {wait} minutes.", "skipped: checked a moment ago")
            self._manual[w.id] = now
        channel, user = channel or w.owner_channel, user or w.owner_user
        if self.guard is not None and is_remote(channel) and self.guard.locked:
            return RunReport("skipped", "", "remote control is locked")
        login = self.logins.store.get(w.login)
        if login is None:
            return self._finish(w, RunReport("failed", f"Watcher {w.name}: its saved login was removed, so it cannot "
                                                       "sign in. Add the login again or remove the watcher.",
                                             "the saved login was removed"))
        mgr = getattr(self.hub, "mgr", None)
        if mgr is None:
            return self._finish(w, RunReport("failed", "", "Delta's private browser is not available on this PC"))
        inside = CURRENT_REQUEST.get() is not None and not hold_lock
        token = None if inside else CURRENT_REQUEST.set(TaskRequest(f"watcher {w.name}", channel=channel or "api",
                                                                    user=user or "owner"))
        try:
            if hold_lock:
                async with self.lock:  # one heavy task (and one browser) at a time
                    report = await self._run(w, login, mgr)
            else:
                report = await self._run(w, login, mgr)
        except Exception as e:
            log.warning("watch.failed", watcher=w.id, error=type(e).__name__)
            why = plain(str(e).splitlines()[0], 160) if str(e) else ""
            report = RunReport("failed", "", f"failed: {type(e).__name__}: {why}")
        finally:
            if token is not None:
                CURRENT_REQUEST.reset(token)
        return self._finish(w, report)

    async def _run(self, w: Watcher, login: Any, mgr: Any) -> RunReport:
        was_running, prev = mgr.running, mgr.active
        try:
            base = await mgr.page()
        except Exception:  # a browser closed a moment ago may still hold its profile: one more try
            await asyncio.sleep(2)
            base = await mgr.page()
        page = await base.context.new_page()
        mgr.active = page
        try:
            return await self._read_all(w, login, page)
        finally:
            with contextlib.suppress(Exception):
                await page.close()
            mgr.active = prev if prev is not None and not prev.is_closed() else None
            if not was_running:
                await mgr.close()

    async def _settle(self, page: Any) -> None:
        if self.settle is not None:
            await self.settle(page)
        else:
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)

    async def _goto(self, page: Any, url: str) -> str:
        """'' when the page opened; else why not (site down, unreachable)."""
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except Exception as e:
            return f"could not reach the site ({plain(str(e).splitlines()[0] if str(e) else type(e).__name__, 120)})"
        status = int(getattr(resp, "status", 200) or 200) if resp is not None else 200
        if status >= 500:
            return f"the site answered with an error ({status}); it may be down"
        await self._settle(page)
        return ""

    async def _signed_out(self, page: Any, login: Any) -> bool:
        if mismatch(login.site, page.url):
            return True  # sent away from the site (to a login page elsewhere, or an error)
        with contextlib.suppress(Exception):
            loc = page.locator(PASSWORD_SEL)
            for i in range(min(await loc.count(), 4)):
                if await loc.nth(i).is_visible():
                    return True
        with contextlib.suppress(Exception):
            if EXPIRED_WORDS.search(str(await page.inner_text("body"))[:3000]):
                return True
        return False

    async def _sign_in(self, w: Watcher, login: Any, page: Any) -> str:
        """'' when signed in; else why not. Asks first unless the login is always allowed."""
        ok, how = await self.logins.allowed(login, why=f"watcher '{w.name}'")
        if not ok:
            return f"signing in was not allowed ({how})"
        result = await self.logins.sign_in(login.id, page, ask=False)
        return "" if result.ok else plain(result.message, 240)

    async def _read_all(self, w: Watcher, login: Any, page: Any) -> RunReport:
        signed_in = False
        diffs: list[SectionDiff] = []
        unread: list[str] = []
        for section in w.recipe.sections:
            target = section_url(login.site, section.url) if section.url else ""
            if target and (why := await self._goto(page, target)):
                return RunReport("failed", "", why)
            if not target and page.url in ("", "about:blank") and (
                    why := await self._goto(page, login.login_url or str(login.site) + "/")):
                return RunReport("failed", "", why)
            if await self._signed_out(page, login):
                if signed_in:
                    unread.append(f"{section.name} (the site signed Delta out again)")
                    continue
                if why := await self._sign_in(w, login, page):
                    status = "needs_user" if why.startswith(("NEEDS HUMAN", "signing in was not")) else "failed"
                    return RunReport(status, "", f"sign-in: {why}")
                signed_in = True
                if target and (why := await self._goto(page, target)):
                    return RunReport("failed", "", why)
                if await self._signed_out(page, login):
                    return RunReport("failed", "", "signed in, but the site still shows its sign-in page")
            raw: Any = None
            with contextlib.suppress(Exception):
                raw = await asyncio.wait_for(page.evaluate(EXTRACT_JS, {
                    "rows": section.rows, "fields": section.fields, "name": section.name, "limit": MAX_ITEMS,
                    "chars": MAX_FIELD}), 15)
            status = str(raw.get("status")) if isinstance(raw, dict) else "missing"
            if status == "missing":
                unread.append(section.name)
                continue
            items = clean_items(list(raw.get("items") or []))
            before = self.store.last_snapshot(w.id, section.name)
            d = diff(section.name, before, items, section.key)
            self.store.save_snapshot(w.id, section.name, items)
            diffs.append(d)
        return self._report(w, diffs, unread)

    def _report(self, w: Watcher, diffs: list[SectionDiff], unread: list[str]) -> RunReport:
        first = [d for d in diffs if d.first]
        news = [d for d in diffs if d.any]
        lines: list[str] = []
        if first:
            counts = ", ".join(f"{d.name} ({d.total} item{'s' if d.total != 1 else ''})" for d in first)
            lines.append(f"Now watching {w.name}: {counts}. I'll tell you only about new or changed items.")
        offers = 0
        for d in news:
            parts = []
            if d.new:
                parts.append(f"{len(d.new)} new")
            if d.changed:
                parts.append(f"{len(d.changed)} changed")
            lines.append(f"{d.name} ({', '.join(parts)}):")
            shown = [("new", x) for x in d.new] + [("changed", x) for x in d.changed]
            for kind, item in shown[:LINES_PER_SECTION]:
                lines.append(f"- {line(item)}" + (" (changed)" if kind == "changed" else ""))
            if len(shown) > LINES_PER_SECTION:
                lines.append(f"- and {len(shown) - LINES_PER_SECTION} more (see Watchers in the dashboard)")
            for kind, item in shown:
                if w.notify == "digest":
                    self.store.add_pending(w.id, "digest", {"section": d.name, "kind": kind, "line": line(item)})
                if OFFER_WORDS.search(d.name) or OFFER_WORDS.search(line(item)):
                    self.store.add_pending(w.id, "offer", {"section": d.name, "item": item, "line": line(item),
                                                           "watcher": w.name})
                    offers += 1
        if unread:
            lines.append("Couldn't read: " + ", ".join(unread) + ". The page may have changed; I did not guess. Ask me "
                         f"to learn where it is on {w.name} again.")
        if offers:
            lines.append(f"{offers} of these can become reminders: open Watchers in the dashboard, or ask me "
                         "(\"remind me about the DBMS exam\").")
        total_new = sum(len(d.new) + len(d.changed) for d in news)
        summary = (f"{total_new} new or changed" if total_new else "no changes") + (
            f"; couldn't read {', '.join(unread)}" if unread else "")
        status = "done" if diffs or not unread else "failed"
        if w.notify == "digest" and not first:  # new items wait for the daily digest; a problem is told now
            text = next((ln for ln in lines if ln.startswith("Couldn't read")), "")
        else:
            text = "\n".join(lines)
        if text and not first:
            text = f"{w.name}\n{text}"
        return RunReport(status, text, summary, diffs, unread)

    def _finish(self, w: Watcher, report: RunReport) -> RunReport:
        if report.status == "skipped":
            return report
        before = w.last_status
        failing = report.status in ("failed", "needs_user")
        if failing and not report.text and before not in ("failed", "needs_user"):
            # tell the user once when a watcher starts failing, not on every run
            report.text = f"Watcher {w.name} could not check the site: {report.summary}. I'll keep trying on schedule."
        self.store.update(w.id, last_run=datetime.now(UTC).replace(microsecond=0).isoformat(),
                          last_status=report.status, last_message=report.summary[:300])
        if self.skills is not None and w.skill_id:
            with contextlib.suppress(Exception):
                if report.status == "done" and not report.unread:
                    self.skills.record(w.skill_id, True)
                elif report.unread:
                    self.skills.record(w.skill_id, False)
        if self.audit is not None:
            self.audit.record("watch.run", watcher=w.id, status=report.status, summary=report.summary[:200])
        return report

    def digest(self, w: Watcher) -> str:
        items = self.store.pending(w.id, "digest")
        if not items:
            return ""
        lines = [f"Daily digest: {w.name}"]
        for p in items[:30]:
            it = p["item"]
            changed = " (changed)" if it.get("kind") == "changed" else ""
            lines.append(f"- {it.get('section', '')}: {it.get('line', '')}{changed}")
        if len(items) > 30:
            lines.append(f"- and {len(items) - 30} more")
        self.store.clear_pending(w.id, "digest")
        return "\n".join(lines)

    # --- reminders from items, only when the user says so ---------------------------------------------------------
    def offers(self) -> list[dict[str, Any]]:
        return self.store.pending(None, "offer")

    def accept_offer(self, pid: int, when: str, tz: str = "", by: tuple[str, str] = ("api", "")) -> str:
        """Turn an offered item into a one-time reminder at `when` ('YYYY-MM-DDTHH:MM', local). Called only from a
        person's own action (a dashboard button)."""
        if self.scheduler is None:
            raise ValueError("the scheduler is not running")
        offer = next((o for o in self.store.pending(None, "offer") if o["id"] == pid), None)
        if offer is None:
            raise ValueError("that item is no longer offered")
        from delta.scheduler.rules import Rule

        text = f"{offer['item'].get('section', '')}: {offer['item'].get('line', '')}"[:300]
        s = self.scheduler.store.add(text[:80], text, Rule("once", at=when), kind="reminder", tz=tz,
                                     owner_channel=by[0], owner_user=by[1])
        self.store.set_pending_state(pid, "accepted")
        with contextlib.suppress(Exception):
            self.scheduler.poke()
        if self.audit is not None:
            self.audit.record("watch.reminder", offer=pid, schedule=s.id)
        return s.id
