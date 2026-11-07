"""Token use per task type, measured with the scripted model on Delta's real wiring (bootstrap.build).

    uv run python evals/token_bench.py              a Markdown table
    uv run python evals/token_bench.py --json       the same numbers as JSON

No keys, no network, no cost: every model call goes to a recording scripted model that answers from a fixed
script. What is measured is what Delta would SEND (system prompt, tool descriptions, history, tool results), as
estimated input tokens (characters / 4, delta.llm.usage.estimate_tokens), per model job (plan, chat, verify).
"Stable prefix" is how many characters at the start of the planner and actor requests stay byte-identical
between two tasks a minute apart: that part is what a provider's automatic prompt cache can reuse.

Task types (the same five every run):
  simple_chat   a question the planner answers itself ("what is the capital of France?")
  open_app      "open notepad" (the app launch is simulated; Delta's own open_app code runs)
  open_app_planned  the same, worded so it goes through the planner and the agent loop
  browser_read  read a figure off a web page (Delta's fake browser: real tool code, a tiny in-memory page)
  multi_step    write a file, check it, report its size
  try_again     "try again" after a failed turn in the same chat
Real-model quality is measured by `delta eval run --model <provider>`; this only counts what is sent.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

TASKS: dict[str, dict[str, Any]] = {
    "simple_chat": {
        "request": "what is the capital of France?",
        "script": {"plan": [{"needs_tools": False, "answer": "Paris."}]},
    },
    "open_app": {
        "request": "open notepad",
        "script": {
            "plan": [{"needs_tools": True, "goal": "open Notepad", "tools": ["open_app"],
                      "success_criteria": ["Notepad's window is open"]}],
            "chat": [{"call": {"tool": "open_app", "args": {"name": "notepad"}}},
                     {"text": "DONE: Notepad is open."}],
        },
    },
    "open_app_planned": {  # worded so no instant command matches: the planner and the agent loop run
        "request": "could you bring up notepad for me so I can jot something down",
        "script": {
            "plan": [{"needs_tools": True, "goal": "open Notepad", "tools": ["open_app"],
                      "success_criteria": ["Notepad's window is open"]}],
            "chat": [{"call": {"tool": "open_app", "args": {"name": "notepad"}}},
                     {"text": "DONE: Notepad is open."}],
        },
    },
    "browser_read": {
        "request": "what is the temperature on {site}/weather.html ?",
        "script": {
            "plan": [{"needs_tools": True, "goal": "read the temperature", "tools": ["browser_open"],
                      "success_criteria": ["the temperature on the page is reported"]}],
            "chat": [{"call": {"tool": "browser_open", "args": {"url": "{site}/weather.html"}}},
                     {"text": "DONE: It is 31 °C in Vijayawada."}],
        },
    },
    "multi_step": {
        "request": "make a file notes.txt in {sandbox} that says hello, then tell me its size",
        "script": {
            "plan": [{"needs_tools": True, "goal": "write notes.txt and report its size",
                      "tools": ["write_file", "path_info"],
                      "success_criteria": ["{sandbox}/notes.txt exists and contains 'hello'",
                                           "its size is reported"]}],
            "chat": [{"call": {"tool": "write_file", "args": {"path": "{sandbox}/notes.txt", "content": "hello"}}},
                     {"call": {"tool": "path_info", "args": {"path": "{sandbox}/notes.txt"}}},
                     {"text": "DONE: notes.txt is saved; it is 5 bytes."}],
        },
    },
    "try_again": {
        "request": "try again",
        "history": [("user", "make a file notes.txt in {sandbox} that says hello"),
                    ("assistant", "I could not save notes.txt: the folder was busy.")],
        "script": {
            "plan": [{"needs_tools": True, "goal": "write notes.txt", "tools": ["write_file"],
                      "success_criteria": ["{sandbox}/notes.txt exists and contains 'hello'"]}],
            "chat": [{"call": {"tool": "write_file", "args": {"path": "{sandbox}/notes.txt", "content": "hello"}}},
                     {"text": "DONE: notes.txt is saved."}],
        },
    },
}


def _fill(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str):
        for k, v in values.items():
            value = value.replace("{" + k + "}", v)
        return value
    if isinstance(value, list):
        return [_fill(v, values) for v in value]
    if isinstance(value, tuple):
        return tuple(_fill(v, values) for v in value)
    if isinstance(value, dict):
        return {k: _fill(v, values) for k, v in value.items()}
    return value


class Recorder:
    """The scripted model, plus a record of every request it was sent."""

    def __init__(self, script: dict[str, list[Any]]) -> None:
        from delta.evals.scripted import ScriptedProvider

        self.inner = ScriptedProvider(script)
        self.name = "scripted"
        self.local = False
        self.sent: list[dict[str, Any]] = []

    def supports(self, task: str) -> bool:
        return self.inner.supports(task)

    async def unload(self) -> None:
        return None

    async def complete(self, task: str, messages: list[Any], tools: list[dict] | None,
                       json_mode: bool = False) -> Any:
        from delta.llm.usage import estimate_tokens

        system = next((str(m.get("content")) for m in messages if m.get("role") == "system"), "")
        self.sent.append({"job": task, "tokens": estimate_tokens(messages, tools), "tools": len(tools or []),
                          "tool_chars": len(json.dumps(tools)) if tools else 0, "system": system,
                          "prefix": json.dumps(tools or [], sort_keys=True) + system})
        return await self.inner.complete(task, messages, tools, json_mode)


def _fake_launch(monkeypatch: Any) -> None:
    """open_app on any PC: Delta's own OpenApp.run, with the launch and the window it opens simulated."""
    from delta.tools import apps

    def plan(self: Any, args: Any) -> Any:
        key = args.name.strip().lower()
        return key, [key], [apps.Launch("exe", lambda: None, f"{key}.exe")], ""

    monkeypatch(apps.OpenApp, "plan", plan)
    monkeypatch(apps, "find_open_window", lambda names, exe="": None)
    monkeypatch(apps, "_windows", lambda: {})
    monkeypatch(apps, "wait_for_app_window", lambda before, hints, seconds=0: "Untitled - Notepad")


async def run_one(name: str, spec: dict[str, Any], base: Path, now: datetime) -> list[dict[str, Any]]:
    from delta.bootstrap import build
    from delta.config import AgentConfig, Boundaries, Settings
    from delta.core import orchestrator as orch_mod
    from delta.core.state import TaskRequest
    from delta.evals.fakebrowser import FakeBrowserManager, fake_browser_tools

    sandbox = base / f"{name}-{now:%H%M}"
    data = base / f"{name}-{now:%H%M}-data"
    sandbox.mkdir(parents=True)
    data.mkdir(parents=True)
    values = {"sandbox": sandbox.as_posix(), "site": "http://127.0.0.1:9/site"}
    settings = Settings(data_dir=data, agent=AgentConfig(max_steps=12, max_attempts=2, auto_observe=False),
                        boundaries=Boundaries(allowed_paths=[str(sandbox)], autonomy="trusted"))  # type: ignore[arg-type]
    c = build(settings)
    rec = Recorder(_fill(spec["script"], values))
    router = c.router
    router.providers = {"scripted": rec}  # type: ignore[dict-item]
    router.routes = {j: ["scripted"] for j in ("plan", "chat", "verify", "vision", "helper")}
    router.preferred = "scripted"
    from delta.llm.router import CircuitBreaker

    router.breakers = {"scripted": CircuitBreaker()}
    router.fallbacks = []
    registry = c.registry
    fake = FakeBrowserManager(sandbox / "page_state.json", {})
    for tool in fake_browser_tools(fake):
        registry.unregister(tool.name)
        registry.register(tool)

    class FixedNow(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:  # type: ignore[override]
            return now

    saved: list[tuple[Any, str, Any]] = []

    def patch(obj: Any, attr: str, value: Any) -> None:
        saved.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, value)

    patch(orch_mod, "datetime", FixedNow)
    _fake_launch(patch)
    req = TaskRequest(_fill(spec["request"], values), channel="api")
    if spec.get("history"):
        req.history = "\n".join(f"{who}: {text}" for who, text in _fill(spec["history"], values))
    try:
        result = await c.orchestrator.run(req)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(c.orchestrator.drain(), 10)
    finally:
        for obj, attr, value in reversed(saved):
            setattr(obj, attr, value)
        _close_stores(c)
    for s in rec.sent:
        s["status"] = result.status
    return rec.sent


def _close_stores(c: Any) -> None:
    """Close every SQLite file build() opened, as Delta's own shutdown does (Container.start, close_state).
    Windows refuses to delete a file that is still open, so a store left open breaks the temp-folder cleanup."""
    orch = c.orchestrator
    engine = getattr(orch, "engine", None)
    for store in (c.memory, c.chats, getattr(orch, "skills", None), getattr(orch, "learning", None),
                  getattr(c.scheduler, "store", None), getattr(c.watchers, "store", None),
                  getattr(engine, "store", None)):
        if store is not None:
            with contextlib.suppress(Exception):
                store.close()


def _common(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b, strict=False):
        if x != y:
            break
        n += 1
    return n


async def measure(base: Path | None = None) -> dict[str, Any]:
    if base is not None:
        return await _measure(base)
    # Windows keeps a just-closed SQLite file locked for a moment: never fail the run over removing a temp folder
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        return await _measure(Path(tmp))


async def _measure(base: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    t0 = datetime(2026, 11, 7, 9, 30)
    for name, spec in TASKS.items():
        first = await run_one(name, spec, base, t0)
        second = await run_one(name, spec, base, t0 + timedelta(minutes=1))
        jobs: dict[str, dict[str, int]] = {}
        for s in first:
            j = jobs.setdefault(s["job"], {"calls": 0, "tokens": 0})
            j["calls"] += 1
            j["tokens"] += s["tokens"]
        prefix: dict[str, int] = {}
        for job in ("plan", "chat"):
            a = next((s["prefix"] for s in first if s["job"] == job), "")
            b = next((s["prefix"] for s in second if s["job"] == job), "")
            if a and b:
                prefix[job] = _common(a, b)
                prefix[job + "_total"] = len(a)
        out[name] = {"status": first[0]["status"] if first else "done (no model call)",
                     "calls": len(first), "tokens": sum(s["tokens"] for s in first), "jobs": jobs,
                     "stable_prefix": prefix,
                     "tools_sent_max": max((s["tools"] for s in first), default=0)}
    return out


def table(data: dict[str, Any]) -> str:
    lines = ["| task | status | model calls | est. input tokens | plan | chat | verify | tools sent | "
             "stable prefix plan / chat (chars) |", "|---|---|---|---|---|---|---|---|---|"]
    for name, r in data.items():
        j = r["jobs"]

        def cell(job: str, j: dict[str, Any] = j) -> str:
            return f"{j[job]['tokens']:,} ({j[job]['calls']})" if job in j else "-"

        p = r["stable_prefix"]
        pre = " / ".join(f"{p[k]:,} of {p[k + '_total']:,}" if k in p else "-" for k in ("plan", "chat"))
        lines.append(f"| {name} | {r['status']} | {r['calls']} | {r['tokens']:,} | {cell('plan')} | {cell('chat')} | "
                     f"{cell('verify')} | {r['tools_sent_max']} | {pre} |")
    total = sum(r["tokens"] for r in data.values())
    lines.append(f"| **total** | | {sum(r['calls'] for r in data.values())} | {total:,} | | | | | |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    import io

    with contextlib.redirect_stdout(io.StringIO()):  # Delta's own log lines: only the table is printed
        data = asyncio.run(measure())
    print(json.dumps(data, indent=1) if args.json else table(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
