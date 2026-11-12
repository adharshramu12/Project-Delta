"""Routines: do a task once, save it under a name, run it again with no AI.

    "open my portal and go to the timetable"   (Delta does it the normal way, with the AI)
    "save this as timetable"                   (the steps that worked are kept, exactly as they ran)
    "run timetable"                            (those steps run back to back: no planner, no tokens)

Why this exists next to learned skills (delta.core.replay): there the recipe is written by a model after the task
and then validated, and on a real PC not one skill ever got a recipe, so nothing was ever replayed. Here the steps
are taken straight from the run, in code, and the user decides what is saved and when it runs.

What keeps a replay honest:
- Only steps that succeeded are kept, in order. Steps that only looked (reading a page, listing windows) are left
  out. A run that aimed by screen position or by looking at the screen cannot be saved: positions change.
- A browser click or fill made by element number is saved by the element's own name (the number is only valid for
  one page listing). If the name is not known, the run cannot be saved.
- Every step still goes through the ToolExecutor, so the user's approval rules, Remote Guard and the browser tools'
  own checks apply on every run exactly as when the AI drives. A step that sends, pays or deletes asks each time.
- The first step that fails stops the routine and says which step and why. Nothing is retried blindly and the
  remaining steps do not run.

Routines live in routines.db in Delta's data folder: the name, the steps (tool and arguments as they ran) and a
count of runs. The arguments are stored as they were, so a routine should not be made from a task whose steps
contain something private; saved logins are safe, because sign_in takes the login's name, never the password.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from delta.core.replay import NOT_REPLAYABLE, OBSERVERS
from delta.core.state import CURRENT_REQUEST, StepRecord
from delta.tools.base import Risk, Tool, ToolResult

MAX_STEPS = 20
MAX_NAME = 40
# Steps that only gather information add nothing to a replay; these are on top of replay.OBSERVERS.
LOOKERS = OBSERVERS | {"web_search", "fetch_page", "web_answer", "browser_tabs", "list_running_apps", "list_logins",
                       "find_app_tool", "search_files", "read_attachment", "look_at_attachment", "browser_wait",
                       "list_routines", "list_schedules", "list_watchers", "take_screenshot"}
# Never part of a routine: a routine does not run or change routines, and a captcha is new every time.
NEVER = frozenset({"save_routine", "run_routine", "delete_routine", "solve_captcha", "remember", "set_browser"})
_NAME_OK = re.compile(r"[^\w '&+-]+", re.UNICODE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS routines(
  name TEXT PRIMARY KEY,       -- lower-case key
  title TEXT NOT NULL,         -- as the user wrote it
  request TEXT NOT NULL,       -- the request the steps came from
  steps TEXT NOT NULL,         -- JSON [{"tool": ..., "args": {...}}]
  created REAL NOT NULL,
  runs INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  last_run REAL
);
"""


def clean_name(name: str) -> str:
    """The name as stored: plain words, one space between them, at most MAX_NAME characters."""
    return " ".join(_NAME_OK.sub(" ", name).split())[:MAX_NAME].strip()


@dataclass
class Routine:
    name: str
    title: str
    request: str
    steps: list[dict[str, Any]]
    runs: int = 0
    failures: int = 0
    last_run: float | None = None


class RoutineStore:
    def __init__(self, path: Path) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()

    def close(self) -> None:
        self._db.close()

    def save(self, title: str, request: str, steps: list[dict[str, Any]]) -> Routine:
        title = clean_name(title)
        if not title:
            raise ValueError("a routine needs a name")
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO routines(name, title, request, steps, created) VALUES (?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET title=excluded.title, request=excluded.request, "
                "steps=excluded.steps, created=excluded.created, runs=0, failures=0, last_run=NULL",
                (title.lower(), title, request[:500], json.dumps(steps), time.time()))
        return Routine(title.lower(), title, request[:500], steps)

    def get(self, name: str) -> Routine | None:
        with self._lock:
            row = self._db.execute("SELECT name, title, request, steps, runs, failures, last_run FROM routines "
                                   "WHERE name=?", (clean_name(name).lower(),)).fetchone()
        return Routine(row[0], row[1], row[2], json.loads(row[3]), row[4], row[5], row[6]) if row else None

    def list(self) -> list[Routine]:
        with self._lock:
            rows = self._db.execute("SELECT name, title, request, steps, runs, failures, last_run FROM routines "
                                    "ORDER BY title COLLATE NOCASE").fetchall()
        return [Routine(r[0], r[1], r[2], json.loads(r[3]), r[4], r[5], r[6]) for r in rows]

    def names(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT name FROM routines")}

    def delete(self, name: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM routines WHERE name=?", (clean_name(name).lower(),)).rowcount > 0

    def ran(self, name: str, ok: bool) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE routines SET runs=runs+1, failures=failures+?, last_run=? WHERE name=?",
                             (0 if ok else 1, time.time(), name))


# --- from a finished run to replayable steps ------------------------------------------------------------


def build_steps(steps: list[StepRecord]) -> tuple[list[dict[str, Any]], str]:
    """(steps to replay, "") or ([], why this run cannot be saved). Decided in code from what really ran."""
    out: list[dict[str, Any]] = []
    for s in steps:
        if not s.ok or s.tool in LOOKERS:
            continue
        if s.tool in NEVER:
            continue
        if s.tool in NOT_REPLAYABLE:
            return [], (f"the task used '{s.tool}', which works by screen position or by looking at the screen. "
                        "That cannot be repeated exactly, so this task cannot be saved as a routine.")
        args = dict(s.args or {})
        if s.tool in ("browser_click", "browser_fill") and args.get("ref") is not None:
            # An element number is only valid for one page listing: keep the element's own name instead.
            target = str((s.evidence or {}).get("target") or "").strip()
            if not target:
                return [], ("one browser step used an element number and its name was not recorded, so the step "
                            "cannot be found again. Do the task once more and save it then.")
            args.pop("ref", None)
            args["text" if s.tool == "browser_click" else "field"] = target
        out.append({"tool": s.tool, "args": args})
    if not out:
        return [], "the last task had no steps that can be repeated (it only looked things up or answered)."
    if len(out) > MAX_STEPS:
        return [], f"the task had {len(out)} steps; a routine holds at most {MAX_STEPS}. Save a shorter task."
    return out, ""


def describe(step: dict[str, Any]) -> str:
    """One step in plain words, for the user to read before trusting a routine."""
    tool, args = step["tool"], step.get("args", {})
    a = {k: str(v) for k, v in args.items()}
    known = {
        "browser_open": lambda: f"open {a.get('url', '')}",
        "browser_click": lambda: f"click '{a.get('text', '')}'",
        "browser_fill": lambda: (f"type into '{a.get('field', '')}'"
                                 + (" and press Enter" if args.get("submit") else "")),
        "browser_search": lambda: f"search for '{a.get('query', '')}'",
        "browser_press": lambda: f"press {a.get('keys', a.get('key', ''))}",
        "sign_in": lambda: f"sign in with the saved login '{a.get('login', a.get('name', ''))}'",
        "open_app": lambda: f"open {a.get('name', '')}",
        "type_text": lambda: "type text",
        "press_keys": lambda: f"press {a.get('keys', '')}",
        "send_message": lambda: f"send a message to {a.get('to', '')} on {a.get('app', '')} (asks first)",
    }
    return known[tool]() if tool in known else tool.replace("_", " ")


# --- tools ----------------------------------------------------------------------------------------------


class SaveArgs(BaseModel):
    name: str = Field(description="What the user wants to call it, e.g. 'timetable'")


class NameArgs(BaseModel):
    name: str = Field(description="The routine's name")


class NoArgs(BaseModel):
    pass


class _RoutineTool:
    def __init__(self, store: RoutineStore) -> None:
        self.store = store
        self.executor: Any = None                # the ToolExecutor, set at start-up
        self.last_done: Callable[[str], tuple[str, list[StepRecord]] | None] = lambda chat: None


class SaveRoutine(_RoutineTool, Tool[SaveArgs]):
    name = "save_routine"
    description = ("Save the task that just finished in this chat as a named routine, so the user can run the same "
                   "steps again later with no AI ('save this as timetable'). Only when the user asks for it.")
    risk = Risk.ACT
    Args = SaveArgs

    def summary(self, args: SaveArgs) -> str:
        return f"save the last task as the routine '{clean_name(args.name)}'"

    async def run(self, args: SaveArgs) -> ToolResult:
        title = clean_name(args.name)
        if not title:
            return ToolResult(False, "Give the routine a name, for example: save this as timetable.")
        req = CURRENT_REQUEST.get()
        last = self.last_done(req.chat_id if req else "")
        if last is None:
            return ToolResult(False, "There is no finished task in this chat to save. Do the task first, and when "
                                     "it has worked, say: save this as <name>.")
        request, ran = last
        steps, why = build_steps(ran)
        if not steps:
            return ToolResult(False, f"I could not save that as a routine: {why}")
        replaced = self.store.get(title) is not None
        self.store.save(title, request, steps)
        lines = "\n".join(f"{i}. {describe(s)}" for i, s in enumerate(steps, 1))
        return ToolResult(True, f"Saved the routine '{title}'" + (" (it replaces the old one)" if replaced else "")
                                + f". It repeats these {len(steps)} step(s) exactly, with no AI:\n{lines}\n"
                                f"Say \"run {title}\" to do it again. Anything that sends or deletes still asks "
                                "you each time.", {"routine": title, "steps": len(steps), "verified": True})


class RunRoutine(_RoutineTool, Tool[NameArgs]):
    name = "run_routine"
    description = ("Run a routine the user saved earlier ('run timetable'): its recorded steps run back to back with "
                   "no AI. Each step still follows the user's approval rules.")
    risk = Risk.ACT
    Args = NameArgs

    def summary(self, args: NameArgs) -> str:
        return f"run the routine '{clean_name(args.name)}'"

    async def run(self, args: NameArgs) -> ToolResult:
        routine = self.store.get(args.name)
        if routine is None:
            names = ", ".join(r.title for r in self.store.list()) or "none saved yet"
            return ToolResult(False, f"There is no routine called '{clean_name(args.name)}'. Saved routines: {names}.")
        if self.executor is None:
            return ToolResult(False, "Routines cannot run right now (Delta is still starting).")
        req = CURRENT_REQUEST.get()
        total = len(routine.steps)
        last: ToolResult | None = None
        for i, step in enumerate(routine.steps, 1):
            if step["tool"] in NEVER or step["tool"] in NOT_REPLAYABLE:  # an edited or old store: never trust it
                self.store.ran(routine.name, ok=False)
                return ToolResult(False, f"Step {i} of '{routine.title}' is not allowed in a routine; nothing "
                                         "more ran.")
            last = await self.executor.execute(step["tool"], dict(step.get("args", {})), task_id=req.id if req else "")
            if not last.ok:
                self.store.ran(routine.name, ok=False)
                done = f"Steps 1 to {i - 1} were done. " if i > 1 else "Nothing was done. "
                denied = bool(last.evidence.get("user_denied"))
                why = "you said no to it" if denied else last.output.strip()[:300]
                return ToolResult(False, f"The routine '{routine.title}' stopped at step {i} of {total} "
                                         f"({describe(step)}): {why}\n{done}The remaining steps did not run.",
                                  {"routine": routine.title, "failed_step": i, "user_denied": denied})
        self.store.ran(routine.name, ok=True)
        tail = (last.output.strip().split("\n")[0][:200] if last else "")
        return ToolResult(True, f"Ran the routine '{routine.title}': all {total} step(s) worked."
                                + (f" Last step: {tail}" if tail else ""),
                          {"routine": routine.title, "steps": total, "verified": True})


class ListRoutines(_RoutineTool, Tool[NoArgs]):
    name = "list_routines"
    description = "List the routines the user has saved, with their steps in plain words."
    risk = Risk.READ
    Args = NoArgs

    async def run(self, args: NoArgs) -> ToolResult:
        routines = self.store.list()
        if not routines:
            return ToolResult(True, "No routines saved yet. Do a task, and when it has worked say: save this as "
                                    "<name>. Then \"run <name>\" repeats it with no AI.", {"verified": True})
        blocks = []
        for r in routines:
            steps = "; ".join(describe(s) for s in r.steps)
            record = f"run {r.runs} time(s)" + (f", {r.failures} stopped early" if r.failures else "")
            blocks.append(f"- {r.title} ({record}): {steps}")
        return ToolResult(True, "Your routines:\n" + "\n".join(blocks), {"count": len(routines), "verified": True})


class DeleteRoutine(_RoutineTool, Tool[NameArgs]):
    name = "delete_routine"
    description = "Delete a saved routine by name. Only when the user asks for it."
    risk = Risk.ACT
    Args = NameArgs

    def summary(self, args: NameArgs) -> str:
        return f"delete the routine '{clean_name(args.name)}'"

    async def run(self, args: NameArgs) -> ToolResult:
        if self.store.delete(args.name):
            return ToolResult(True, f"Deleted the routine '{clean_name(args.name)}'.", {"verified": True})
        return ToolResult(False, f"There is no routine called '{clean_name(args.name)}'.")


def routine_tools(store: RoutineStore) -> list[Any]:
    return [SaveRoutine(store), RunRoutine(store), ListRoutines(store), DeleteRoutine(store)]


# --- instant commands -------------------------------------------------------------------------------------

_SAVE = re.compile(r"^(?:please\s+)?(?:save|keep|store)\s+(?:this|that|it|the\s+last\s+task)\s+"
                   r"(?:task\s+)?(?:as|under)\s+(?:a\s+|the\s+|my\s+)?(?:routine\s+)?(?:called\s+|named\s+)?"
                   r"[\"']?(?P<name>[^\"']{1,60}?)[\"']?(?:\s+routine)?\s*[.!]*$", re.I)
_RUN = re.compile(r"^(?:please\s+)?(?:run|do|start|replay|repeat)\s+(?:the\s+|my\s+)?(?:routine\s+)?[\"']?"
                  r"(?P<name>[^\"']{1,60}?)[\"']?(?:\s+routine)?(?:\s+(?:again|now|please))?\s*[.!]*$", re.I)
_LIST = re.compile(r"^(?:please\s+)?(?:show|list|what\s+are)?\s*(?:me\s+)?(?:all\s+)?my\s+(?:saved\s+)?"
                   r"routines\s*[.?!]*$"
                   r"|^(?:list|show)\s+(?:the\s+)?routines\s*[.?!]*$", re.I)
_DELETE = re.compile(r"^(?:please\s+)?(?:delete|remove|forget)\s+(?:the\s+|my\s+)?routine\s+[\"']?"
                     r"(?P<name>[^\"']{1,60}?)[\"']?\s*[.!]*$", re.I)

# Set at start-up: the names of the saved routines (lower case). "run X" is an instant command only for a name
# that exists, so "run a virus scan" still goes to the planner.
names_now: Callable[[], set[str]] = set


def routine_command(text: str) -> tuple[str, dict[str, str]] | None:
    """(tool, args) for the four routine commands, or None. No AI: the user's own words decide."""
    t = " ".join(text.split())
    if m := _SAVE.match(t):
        name = clean_name(m["name"])
        return ("save_routine", {"name": name}) if name else None
    if _LIST.match(t):
        return "list_routines", {}
    if m := _DELETE.match(t):
        return "delete_routine", {"name": clean_name(m["name"])}
    if m := _RUN.match(t):
        name = clean_name(m["name"])
        if name.lower() in names_now():
            return "run_routine", {"name": name}
    if clean_name(t).lower() in names_now():  # just the routine's name: "timetable"
        return "run_routine", {"name": clean_name(t)}
    return None
