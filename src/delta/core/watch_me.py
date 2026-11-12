"""Watch me do it: the user does a task by hand in Delta's browser, and Delta saves it as a routine.

    "start recording"                      Delta's own browser opens (or comes to the front) and listens
    (the user clicks, types, picks from lists, moves between pages)
    "stop recording and save as timetable" the clicks and fields become a routine (delta.core.routines)
    "run timetable"                        the same steps run again, with no AI

What is recorded is not a video and not screen positions. A small script in the page tells Delta, for each real
action of the user, what was acted on by NAME: the text of the link or button clicked, the label of the field typed
into and what was typed, the option picked. Those are exactly what the browser tools take, so the routine replays
with the same tools and the same checks as any other.

Never recorded: what is typed into a password, card or one-time-code field (the event carries no value at all, the
field is only noted as skipped). A routine therefore never holds a password; Delta's browser stays signed in between
runs, and a saved login covers a site that signs out.

It works in Delta's own browser only (the one Delta starts and controls). The user's everyday Chrome or Edge is
driven from outside, through Windows, and gives no reliable way to hear each click.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from delta.core.routines import MAX_STEPS, RoutineStore, clean_name, describe
from delta.tools.base import Risk, Tool, ToolResult

HOLD_S = 30 * 60        # Delta's browser is kept open this long while the user records
CAUSED_BY_ACTION_S = 4  # a page change this soon after a click or Enter is that action's result, not a new step
ENTER_CLICK_S = 0.5     # a "click" this soon after Enter is the browser pressing the form's submit button itself
MAX_EVENTS = 400
_BLANK = re.compile(r"^(?:$|about:|chrome://|edge://|chrome-search://|devtools://)", re.I)

# Runs in every page of Delta's browser. It reports only real actions of the person (isTrusted), by name.
RECORDER_JS = r"""
(() => {
  if (window.__deltaRec) return;
  window.__deltaRec = true;
  const send = (e) => { try { window.__deltaRecord(e); } catch (_) {} };
  const text = (s) => String(s || "").replace(/\s+/g, " ").trim().slice(0, 80);
  const label = (el) => {
    const aria = el.getAttribute("aria-label");
    if (text(aria)) return text(aria);
    if (el.labels && el.labels.length) {
      // a label that wraps its field also holds the field's own text (a dropdown's options): leave that out
      const copy = el.labels[0].cloneNode(true);
      copy.querySelectorAll("select,input,textarea,button").forEach((x) => x.remove());
      if (text(copy.textContent)) return text(copy.textContent);
    }
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const t = by.split(/\s+/).map((i) => (document.getElementById(i) || {}).innerText || "").join(" ");
      if (text(t)) return text(t);
    }
    return text(el.placeholder || el.getAttribute("title") || el.name || "");
  };
  const name = (el) => text(el.getAttribute("aria-label") || el.innerText || el.value
                            || el.getAttribute("title") || el.getAttribute("alt") || "");
  const secret = (el) => (el.type || "").toLowerCase() === "password"
    || /password|cc-|one-time-code/.test(String(el.autocomplete || "").toLowerCase());
  const CLICKABLE = 'a,button,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="option"],'
    + 'input[type="submit"],input[type="button"],summary';
  document.addEventListener("click", (ev) => {
    if (!ev.isTrusted || !ev.target.closest) return;
    const el = ev.target.closest(CLICKABLE);
    if (!el) return;
    const n = name(el);
    if (n) send({kind: "click", name: n, url: location.href});
  }, true);
  document.addEventListener("change", (ev) => {
    if (!ev.isTrusted) return;
    const el = ev.target, tag = String(el.tagName || "").toLowerCase();
    if (tag === "select") {
      const o = el.selectedOptions && el.selectedOptions[0];
      send({kind: "select", field: label(el), option: text(o ? o.text : el.value), url: location.href});
      return;
    }
    if (tag !== "input" && tag !== "textarea") return;
    const type = String(el.type || "text").toLowerCase();
    if (["checkbox", "radio", "file", "submit", "button", "hidden", "range", "color"].includes(type)) return;
    const hide = secret(el);
    send({kind: "fill", field: label(el), value: hide ? "" : String(el.value).slice(0, 2000), secret: hide,
          url: location.href});
  }, true);
  document.addEventListener("keydown", (ev) => {
    if (!ev.isTrusted || ev.key !== "Enter") return;
    const el = ev.target;
    if (String(el.tagName || "").toLowerCase() !== "input") return;
    const hide = secret(el);
    send({kind: "enter", field: label(el), value: hide ? "" : String(el.value).slice(0, 2000), secret: hide,
          url: location.href});
  }, true);
})();
"""


@dataclass
class Recording:
    start_url: str = ""
    started: float = field(default_factory=time.monotonic)
    events: list[dict[str, Any]] = field(default_factory=list)


def is_blank(url: str) -> bool:
    return bool(_BLANK.match(url or ""))


def bare(url: str) -> str:
    """The address without what follows ? or #. A form sent with GET puts every field into the address, a
    password included, and Delta never has the password to look for: after one was typed, nothing after the ? of
    any later address is kept."""
    return re.split(r"[?#]", url, maxsplit=1)[0]


def events_to_steps(events: list[dict[str, Any]], start_url: str = "") -> tuple[list[dict[str, Any]], list[str]]:
    """(routine steps, notes for the user) from what the page reported, in order. Pure: no browser needed."""
    steps: list[dict[str, Any]] = []
    notes: list[str] = []

    def note(text: str) -> None:
        if text not in notes:
            notes.append(text)

    def opened(url: str) -> None:
        if not (steps and steps[-1]["tool"] == "browser_open" and steps[-1]["args"]["url"] == url):
            steps.append({"tool": "browser_open", "args": {"url": url}})

    if not is_blank(start_url):
        opened(start_url)
    last_action = last_enter = float("-inf")
    secret_typed = False
    for e in events:
        kind, at = e.get("kind"), float(e.get("t", 0.0))
        if kind == "nav":
            url = str(e.get("url", ""))
            if secret_typed:
                url = bare(url)
            # typed into the address bar or picked from bookmarks: a step of its own. A page change right after a
            # click or Enter is what that action did, and replaying the action brings it again.
            if not is_blank(url) and (not steps or at - last_action > CAUSED_BY_ACTION_S):
                opened(url)
            continue
        if kind == "click":
            name = str(e.get("name", "")).strip()
            if at - last_enter < ENTER_CLICK_S:
                continue  # Enter in a form makes the browser "click" its submit button: one act, already a step
            if name:
                steps.append({"tool": "browser_click", "args": {"text": name}})
                last_action = at
            continue
        if kind in ("fill", "enter"):
            if kind == "enter":
                last_enter = at
            if e.get("secret"):
                secret_typed = True
                note("You typed into a password field. Delta did not record it, and never does. Delta's browser "
                     "stays signed in, so the routine starts after sign-in; if the site asks for the password "
                     "again, the routine stops there.")
                last_action = at
                continue
            label, value = str(e.get("field", "")).strip(), str(e.get("value", ""))
            if not label:
                note("One field has no name on the page, so what you typed there could not be saved as a step.")
                continue
            submit = kind == "enter"
            prev = steps[-1] if steps else None
            if prev and prev["tool"] == "browser_fill" and prev["args"]["field"] == label:
                prev["args"]["value"] = value or prev["args"]["value"]  # the same field again: keep the last text
                prev["args"]["submit"] = prev["args"]["submit"] or submit
            elif value or submit:
                steps.append({"tool": "browser_fill", "args": {"field": label, "value": value, "submit": submit}})
            last_action = at
            continue
        if kind == "select":
            label, option = str(e.get("field", "")).strip(), str(e.get("option", "")).strip()
            if label and option:
                steps.append({"tool": "browser_select", "args": {"label": label, "option": option}})
                last_action = at
            else:
                note("One dropdown has no name on the page, so that choice could not be saved as a step.")
    if len(steps) > MAX_STEPS:
        note(f"You did {len(steps)} things; a routine holds {MAX_STEPS}, so only the first {MAX_STEPS} were kept.")
        steps = steps[:MAX_STEPS]
    return steps, notes


class WatchMe:
    """Holds the one recording in progress and the steps of the last finished one until they are named."""

    def __init__(self, hub: Any, store: RoutineStore) -> None:
        self.hub = hub
        self.store = store
        self.rec: Recording | None = None
        self.pending: tuple[list[dict[str, Any]], list[str]] | None = None
        self._wired: set[int] = set()  # browser contexts that already carry the page script

    @property
    def recording(self) -> bool:
        return self.rec is not None

    def _heard(self, source: Any, event: Any) -> None:
        """Called from the page (through Playwright) for each action of the person."""
        if self.rec is None or not isinstance(event, dict) or len(self.rec.events) >= MAX_EVENTS:
            return
        clean = {k: (str(v)[:2000] if isinstance(v, str) else bool(v)) for k, v in event.items()
                 if k in ("kind", "name", "field", "value", "option", "url", "secret")}
        if clean.get("secret"):
            clean["value"] = ""  # whatever the page sent: a secret field's text is never kept
        clean["t"] = time.monotonic() - self.rec.started
        self.rec.events.append(clean)

    def _moved(self, frame: Any) -> None:
        with contextlib.suppress(Exception):
            if self.rec is not None and frame.parent_frame is None:
                url = str(frame.url)
                if any(e.get("secret") for e in self.rec.events):
                    url = bare(url)  # never keep what a form put after ? once a password was typed
                self.rec.events.append({"kind": "nav", "url": url, "t": time.monotonic() - self.rec.started})

    async def start(self, url: str = "") -> str:
        mgr = getattr(self.hub, "mgr", None)
        if mgr is None:
            raise RuntimeError("Recording needs Delta's own browser, which is not available on this PC (it needs "
                               "Google Chrome or Microsoft Edge).")
        page = await mgr.page()
        ctx = page.context
        if id(ctx) not in self._wired:
            await ctx.expose_binding("__deltaRecord", self._heard)
            await ctx.add_init_script(RECORDER_JS)
            ctx.on("page", lambda p: p.on("framenavigated", self._moved))
            self._wired.add(id(ctx))
        for p in ctx.pages:
            p.on("framenavigated", self._moved)
            with contextlib.suppress(Exception):
                await p.evaluate(RECORDER_JS)  # pages that were open before the script was added
        if url:
            await page.goto(url if "://" in url else f"https://{url}", wait_until="domcontentloaded", timeout=30_000)
            with contextlib.suppress(Exception):
                await page.evaluate(RECORDER_JS)
        with contextlib.suppress(Exception):
            await page.bring_to_front()
        mgr.hold_until = time.monotonic() + HOLD_S
        self.pending = None
        self.rec = Recording(start_url=str(page.url))
        return str(page.url)

    def stop(self) -> tuple[list[dict[str, Any]], list[str]]:
        rec, self.rec = self.rec, None
        if rec is None:
            return [], []
        self.pending = events_to_steps(rec.events, rec.start_url)
        return self.pending

    def cancel(self) -> bool:
        was = self.rec is not None or self.pending is not None
        self.rec = self.pending = None
        return was


class RecordArgs(BaseModel):
    action: str = Field(description="start, stop, save or cancel")
    name: str = Field(default="", description="For stop/save: what to call the routine, e.g. 'timetable'")
    url: str = Field(default="", description="For start: a page to open first (optional)")


class RecordRoutine(Tool[RecordArgs]):
    name = "record_routine"
    description = ("Record a routine by watching the user do a task by hand in Delta's own browser: action=start "
                   "opens the browser and listens; action=stop (with a name) saves the clicks and typing as a "
                   "routine the user can run again with no AI; action=cancel throws the recording away. Passwords "
                   "are never recorded. Only when the user asks to record.")
    risk = Risk.ACT
    Args = RecordArgs

    def __init__(self, watch: WatchMe) -> None:
        self.watch = watch

    def summary(self, args: RecordArgs) -> str:
        return {"start": "open Delta's browser and record what you do there",
                "cancel": "throw the recording away"}.get(args.action, f"save the recording as '{args.name}'")

    def _save(self, name: str) -> ToolResult:
        title = clean_name(name)
        if self.watch.pending is None:
            return ToolResult(False, "There is no finished recording to save. Say \"start recording\" first.")
        steps, notes = self.watch.pending
        tail = ("\n" + "\n".join(notes)) if notes else ""
        if not steps:
            self.watch.pending = None
            return ToolResult(False, "Nothing you did could be saved as a step, so no routine was made. Delta "
                                     "records clicks on links and buttons, typing into named fields and choices "
                                     "in dropdowns, in Delta's own browser." + tail)
        lines = "\n".join(f"{i}. {describe(s)}" for i, s in enumerate(steps, 1))
        if not title:
            return ToolResult(True, f"I recorded {len(steps)} step(s):\n{lines}\nWhat should I call it? Say: save "
                                    "recording as <name>." + tail, {"steps": len(steps), "needs_name": True,
                                                                    "verified": True})
        replaced = self.watch.store.get(title) is not None
        self.watch.store.save(title, "(recorded by hand)", steps, browser="private")
        self.watch.pending = None
        return ToolResult(True, f"Saved the routine '{title}'" + (" (it replaces the old one)" if replaced else "")
                                + f" from what you did. It repeats these {len(steps)} step(s), with no AI:\n{lines}\n"
                                f"Say \"run {title}\" to do it again. Anything that sends or deletes still asks you "
                                "each time." + tail, {"routine": title, "steps": len(steps), "verified": True})

    async def run(self, args: RecordArgs) -> ToolResult:
        action = args.action.strip().lower()
        if action == "start":
            if self.watch.recording:
                return ToolResult(True, "I am already recording. Do the task in Delta's browser, then say: stop "
                                        "recording and save as <name>.", {"recording": True, "verified": True})
            try:
                url = await self.watch.start(args.url.strip())
            except Exception as e:
                return ToolResult(False, f"I could not start recording: {str(e)[:300]}")
            where = "" if is_blank(url) else f" It is on {url}."
            return ToolResult(True, "Recording. Delta's own browser is open and in front." + where + " Do the task "
                                    "there yourself: click, type and pick as usual. I note each step by the name "
                                    "of what you click or fill, never by position, and never what you type into a "
                                    "password field. When you are done, say: stop recording and save as <name>.",
                              {"recording": True, "url": url, "verified": True})
        if action == "cancel":
            return ToolResult(True, "The recording is thrown away; nothing was saved." if self.watch.cancel()
                              else "There was no recording to cancel.", {"verified": True})
        if action in ("stop", "save"):
            if self.watch.recording:
                self.watch.stop()
            return self._save(args.name)
        return ToolResult(False, "action must be start, stop, save or cancel.")


# --- instant commands: the user's own words, no AI --------------------------------------------------------

_START = re.compile(r"^(?:please\s+)?(?:start\s+(?:a\s+)?recording|record\s+(?:a\s+|my\s+)?(?:new\s+)?routine|"
                    r"record\s+(?:what|how)\s+i\s+do|watch\s+me(?:\s+do\s+(?:it|this))?|record\s+me|"
                    r"learn\s+(?:this\s+)?from\s+me)(?:\s+(?:on|at)\s+(?P<url>\S+))?\s*[.!]*$", re.I)
_STOP = re.compile(r"^(?:please\s+)?(?:stop|end|finish)\s+(?:the\s+)?recording"
                   r"(?:\s+and\s+save(?:\s+it)?(?:\s+as)?\s+[\"']?(?P<name>[^\"']{1,60}?)[\"']?)?\s*[.!]*$", re.I)
_SAVE_REC = re.compile(r"^(?:please\s+)?save\s+(?:the\s+|this\s+|my\s+)?recording\s+as\s+[\"']?"
                       r"(?P<name>[^\"']{1,60}?)[\"']?\s*[.!]*$", re.I)
_CANCEL = re.compile(r"^(?:please\s+)?(?:cancel|discard|delete|forget)\s+(?:the\s+|this\s+|my\s+)?recording"
                     r"\s*[.!]*$", re.I)


def record_command(text: str) -> tuple[str, dict[str, str]] | None:
    t = " ".join(text.split())
    if m := _START.match(t):
        return "record_routine", {"action": "start", "url": m["url"] or ""}
    if m := _STOP.match(t):
        return "record_routine", {"action": "stop", "name": clean_name(m["name"] or "")}
    if m := _SAVE_REC.match(t):
        return "record_routine", {"action": "save", "name": clean_name(m["name"])}
    if _CANCEL.match(t):
        return "record_routine", {"action": "cancel"}
    return None
