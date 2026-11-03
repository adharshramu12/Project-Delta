"""Live hard-test runner: plays each scenario in evals/scenarios.yaml through the real agent
(real model, real desktop, the user's own browser) and writes a Markdown report.

    uv run python evals/run_live.py                 # all scenarios
    uv run python evals/run_live.py notebooklm-notebook web-weather

Uses an isolated data dir, so the user's real memory, skills and audit log are untouched.
Keep hands off the mouse/keyboard while it runs: moving the mouse makes Delta stop (by design).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).parent
os.environ.setdefault("DELTA_DATA_DIR", tempfile.mkdtemp(prefix="delta-evals-"))

from delta.bootstrap import build  # noqa: E402
from delta.config import load_settings, save_dir  # noqa: E402
from delta.core.state import TaskRequest  # noqa: E402


@dataclass
class Outcome:
    id: str
    user: str
    task: str
    status: str = "not run"
    passed: bool = False
    seconds: float = 0
    provider: str = ""
    steps: list[str] = field(default_factory=list)
    failed_steps: list[str] = field(default_factory=list)
    approvals: list[str] = field(default_factory=list)
    verify: str = ""
    answer: str = ""
    error: str = ""
    side_effects: str = ""


async def run_one(c: Any, sc: dict[str, Any]) -> Outcome:
    out = Outcome(sc["id"], sc.get("user", ""), sc["task"], side_effects=sc.get("side_effects", ""))
    decision = sc.get("approve", "deny") == "approve"

    async def present(req: Any) -> None:
        out.approvals.append(f"{req.summary} -> {'approved' if decision else 'denied'}")
        c.approvals.resolve(req.id, decision, "eval")

    async def events(event: str, data: dict[str, Any]) -> None:
        if event == "task.step":
            out.steps.append(f"{data['tool']} {json.dumps(data['args'], ensure_ascii=False)[:140]}")
            print(f"   · {out.steps[-1]}", flush=True)
        elif event == "task.verify":
            out.verify = f"{'passed' if data['passed'] else 'failed'}: {data['reason']}"

    c.approvals.add_presenter("eval", present)
    c.events.subscribe(events)
    t = time.perf_counter()
    try:
        r = await asyncio.wait_for(c.submit(TaskRequest(sc["task"], channel="eval")), sc.get("minutes", 5) * 60)
        out.status, out.answer, out.provider = r.status, r.answer, r.provider
        out.failed_steps = [f"{s.tool}: {s.output[:160]}" for s in r.steps if not s.ok]
    except TimeoutError:
        out.status, out.error = "timeout", f"no result within {sc.get('minutes', 5)} min"
    except Exception as e:
        out.status, out.error = "crashed", f"{type(e).__name__}: {e}"
    out.seconds = round(time.perf_counter() - t, 1)
    expect = [w.lower() for w in sc.get("expect", [])]
    out.passed = out.status == "done" and (not expect or any(w in out.answer.lower() for w in expect))
    c.events._handlers.remove(events)
    for name in sc.get("cleanup_files", []):
        target = save_dir(c.settings.files, create=False) / name
        if target.exists():
            target.unlink()
    return out


def report(results: list[Outcome], path: Path) -> None:
    ok = sum(r.passed for r in results)
    lines = [f"# Delta live hard test — {datetime.now():%Y-%m-%d %H:%M}", "",
             f"**{ok}/{len(results)} passed**", "",
             "| Scenario | User | Result | Time | Steps | Approvals |", "|---|---|---|---|---|---|"]
    for r in results:
        mark = "✅" if r.passed else ("⏱" if r.status == "timeout" else "❌")
        lines.append(f"| {r.id} | {r.user} | {mark} {r.status} | {r.seconds:.0f}s | {len(r.steps)} | {len(r.approvals)} |")
    for r in results:
        lines += ["", f"## {r.id} — {'PASS' if r.passed else 'FAIL'}", "", f"**Task:** {r.task}", ""]
        if r.side_effects:
            lines.append(f"**Side effects:** {r.side_effects}")
        lines += [f"**Status:** {r.status} in {r.seconds:.0f}s via {r.provider or '-'}; verify: {r.verify or '-'}"]
        if r.error:
            lines.append(f"**Error:** {r.error}")
        lines += ["", "**Steps:**"] + [f"{i + 1}. `{s}`" for i, s in enumerate(r.steps)]
        if r.failed_steps:
            lines += ["", "**Failed steps:**"] + [f"- {s}" for s in r.failed_steps]
        if r.approvals:
            lines += ["", "**Approvals:**"] + [f"- {a}" for a in r.approvals]
        lines += ["", "**Answer:**", "", "> " + r.answer.replace("\n", "\n> ")[:1500]]
    path.write_text("\n".join(lines), encoding="utf-8")


async def main(only: list[str]) -> None:
    scenarios = yaml.safe_load((HERE / "scenarios.yaml").read_text(encoding="utf-8"))
    if only:
        scenarios = [s for s in scenarios if s["id"] in only]
    s = load_settings()
    s.memory.auto_extract = False
    s.agent.max_steps = 25  # web apps need more steps than simple tasks
    c = build(s)
    results = []
    for sc in scenarios:
        print(f"\n=== {sc['id']} ({sc.get('user', '')}): {sc['task'][:110]}", flush=True)
        res = await run_one(c, sc)
        print(f"-> {'PASS' if res.passed else 'FAIL'} {res.status} in {res.seconds:.0f}s, "
              f"{len(res.steps)} steps, {len(res.approvals)} approvals", flush=True)
        results.append(res)
        await asyncio.sleep(3)
    ub = getattr(c.registry.get("browser_open"), "ub", None)
    if ub and ub.handle:
        import win32con
        import win32gui

        win32gui.PostMessage(ub.handle, win32con.WM_CLOSE, 0, 0)
    for _, closer in c.closers:
        await closer()
    out_dir = HERE / "reports"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"live-{datetime.now():%Y%m%d-%H%M}.md"
    report(results, path)
    print(f"\nREPORT: {path}\nRESULT: {sum(r.passed for r in results)}/{len(results)} passed")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
