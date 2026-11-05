"""Batch 1 in a real Chrome/Edge: snapshot diffs, event-driven settle, and a before/after benchmark.

The benchmark drives the same pages as test_browser_e2e.py twice per round: "before" with the old
settle (Playwright's networkidle plus a fixed quiet window) and full page listings after every
action, then "after" with this code's defaults. For each it records the steps, the characters the
tools returned, the characters the model would be sent over the task (history compacted the old
way vs the new way; the fixed system prompt and tool specs are left out) and the wall time. The table
is printed to the job log, and to the job summary on GitHub Actions. Run with DELTA_E2E=1 pytest -m e2e.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import statistics
import time
from collections.abc import Callable
from typing import Any

import pytest

from delta.core.orchestrator import CONTEXT_BUDGET_CHARS, compact_history
from delta.tools import browser
from delta.tools.base import ToolResult
from delta.tools.browser import (
    BrowserBack,
    BrowserClick,
    BrowserFill,
    BrowserManager,
    BrowserOpen,
    BrowserPress,
    BrowserRead,
    BrowserScroll,
    ElementRef,
    OpenArgs,
    PressArgs,
    ReadArgs,
    RefClickArgs,
    RefFillArgs,
    ScrollArgs,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]


def ref_of(mgr: BrowserManager, role: str, name: str) -> ElementRef:
    found = [e for e in mgr.refs.values() if e.role == role and e.name == name]
    assert len(found) == 1, (f"expected one {role} '{name}', got {found}; refs read at {mgr.refs_url}, "
                             f"now at {mgr.current_url}: {[e.line() for e in mgr.refs.values()]}")
    return found[0]


def ok(result: ToolResult) -> str:
    assert result.ok, result.output
    return result.output


# --- diffs on a real page ----------------------------------------------------------------------------

async def test_spa_actions_send_diffs_and_every_ref_still_works(mgr: BrowserManager, site: str) -> None:
    full = ok(await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/spa.html")))
    assert full.count("\n\nPage: Tiny SPA") == 1
    load, clear = ref_of(mgr, "button", "Load items"), ref_of(mgr, "button", "Clear list")

    loaded = await BrowserClick(mgr).run(RefClickArgs(ref=load.ref))
    out = ok(loaded)
    assert loaded.evidence["snapshot"] == "diff", out
    assert "Page changes: Tiny SPA" in out
    for name in ("Open report", "Archive thread", "Star message"):
        assert f'+ [{ref_of(mgr, "button", name).ref}] button "{name}"' in out, out
    assert 'button "Load items"' not in out  # unchanged: the model already holds it

    star = ref_of(mgr, "button", "Star message")
    ok(await BrowserClick(mgr).run(RefClickArgs(ref=star.ref)))
    assert await (await mgr.page()).inner_text("#picked") == "picked Star message"

    cleared = await BrowserClick(mgr).run(RefClickArgs(ref=clear.ref))
    # Three buttons gone from a two-button page: the diff is not smaller, so the full listing is sent.
    if cleared.evidence["snapshot"] == "diff":
        assert f'- [{star.ref}] button "Star message"' in ok(cleared), cleared.output
    else:
        assert 'button "Star message"' not in ok(cleared) and 'button "Load items"' in cleared.output
    stale = await BrowserClick(mgr).run(RefClickArgs(ref=star.ref))
    assert not stale.ok and "no longer on the page" in stale.output
    # A ref listed only in the first, full listing still works after several diffs.
    again = ok(await BrowserClick(mgr).run(RefClickArgs(ref=load.ref)))
    assert 'button "Star message"' in again


async def test_navigation_sends_the_full_listing(mgr: BrowserManager, site: str) -> None:
    ok(await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/search.html")))
    box = ref_of(mgr, "searchbox", "Search docs")
    res = await BrowserFill(mgr).run(RefFillArgs(ref=box.ref, value="diffs", submit=True))
    assert res.evidence["snapshot"] == "full" and "\n\nPage: " in ok(res) and "New search" in res.output


# --- benchmark ---------------------------------------------------------------------------------------

async def legacy_settle(page: Any, idle_ms: int = 1500) -> None:
    """settle() as it was before this change (main at 3a179a2), for the "before" runs."""
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=10000)
    deadline = time.monotonic() + idle_ms / 1000
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=idle_ms)
    pending = browser._INFLIGHT.get(page)
    if pending is not None:
        quiet_since: float | None = None
        while (now := time.monotonic()) < deadline:
            if pending:
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= 0.25:
                break
            await asyncio.sleep(0.05)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(page.evaluate(
            "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"), 1.0)


def legacy_compact(messages: list[dict[str, Any]], budget: int, keep_last: int = 2) -> None:
    """compact_history's tool-result trimming as it was before this change."""
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idx[:-keep_last]:
        content = str(messages[i].get("content", ""))
        if len(content) > 500:
            messages[i]["content"] = content[:500] + "\n…[older result trimmed to save space]"

    def size() -> int:
        return sum(len(str(m.get("content") or "")) for m in messages)

    over = size() - budget
    for i in reversed(tool_idx[-keep_last:]):
        if over <= 0:
            break
        content = str(messages[i].get("content", ""))
        keep = max(500, len(content) - over - 100)
        if keep < len(content):
            messages[i]["content"] = content[:keep] + "\n…[trimmed to fit the model's limit]"
            over = size() - budget


async def _arrive(mgr: BrowserManager, pattern: str) -> None:
    """Make sure a navigation landed before the next step (timed): a form submit can commit after
    the action returned, and the agent would then need an extra step to see it."""
    page = await mgr.page()
    await page.wait_for_url(pattern, timeout=10000)


async def _search(mgr: BrowserManager, site: str) -> list[tuple[str, ToolResult]]:
    out = [("browser_open", await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/search.html")))]
    box = ref_of(mgr, "searchbox", "Search docs")
    out.append(("browser_fill", await BrowserFill(mgr).run(RefFillArgs(ref=box.ref, value="element refs",
                                                                        submit=True))))
    await _arrive(mgr, "**/results.html?q=element+refs")
    out.append(("browser_back", await BrowserBack(mgr).run(BrowserBack.Args())))
    await _arrive(mgr, "**/search.html")
    if not any(e.role == "searchbox" for e in mgr.refs.values()):  # listed before the page came back
        out.append(("browser_read", await BrowserRead(mgr).run(ReadArgs())))
    box = ref_of(mgr, "searchbox", "Search docs")
    out.append(("browser_fill", await BrowserFill(mgr).run(RefFillArgs(ref=box.ref, value="pressed enter"))))
    out.append(("browser_press", await BrowserPress(mgr).run(PressArgs(key="Enter"))))
    await _arrive(mgr, "**/results.html?q=pressed+enter")
    out.append(("browser_open", await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/search.html"))))
    out.append(("browser_scroll", await BrowserScroll(mgr).run(ScrollArgs(direction="down", pages=2))))
    return out


async def _login(mgr: BrowserManager, site: str) -> list[tuple[str, ToolResult]]:
    out = [("browser_open", await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/login.html")))]
    user = ref_of(mgr, "textbox", "Username")
    out.append(("browser_fill", await BrowserFill(mgr).run(RefFillArgs(ref=user.ref, value="ada"))))
    signin = ref_of(mgr, "button", "Sign in")
    out.append(("browser_click", await BrowserClick(mgr).run(RefClickArgs(ref=signin.ref))))
    out.append(("browser_click", await BrowserClick(mgr).run(RefClickArgs(text="Sign in"))))
    out.append(("browser_scroll", await BrowserScroll(mgr).run(ScrollArgs(direction="down", pages=1))))
    return out


async def _spa(mgr: BrowserManager, site: str) -> list[tuple[str, ToolResult]]:
    out = [("browser_open", await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/spa.html")))]
    load, clear = ref_of(mgr, "button", "Load items"), ref_of(mgr, "button", "Clear list")
    out.append(("browser_click", await BrowserClick(mgr).run(RefClickArgs(ref=load.ref))))
    star = ref_of(mgr, "button", "Star message")
    out.append(("browser_click", await BrowserClick(mgr).run(RefClickArgs(ref=star.ref))))
    out.append(("browser_click", await BrowserClick(mgr).run(RefClickArgs(ref=clear.ref))))
    out.append(("browser_click", await BrowserClick(mgr).run(RefClickArgs(ref=load.ref))))
    star = ref_of(mgr, "button", "Star message")
    out.append(("browser_click", await BrowserClick(mgr).run(RefClickArgs(ref=star.ref))))
    return out


async def _compose(mgr: BrowserManager, site: str) -> list[tuple[str, ToolResult]]:
    out = [("browser_open", await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/compose.html")))]
    preview = ref_of(mgr, "button", "Preview")
    out.append(("browser_click", await BrowserClick(mgr).run(RefClickArgs(ref=preview.ref))))
    out.append(("browser_scroll", await BrowserScroll(mgr).run(ScrollArgs(direction="down", pages=1))))
    out.append(("browser_scroll", await BrowserScroll(mgr).run(ScrollArgs(direction="up", pages=1))))
    return out


SCENARIOS = {"search": _search, "login": _login, "spa": _spa, "compose": _compose}


def context_chars(results: list[tuple[str, ToolResult]], compact: Callable[..., None]) -> int:
    """Characters of conversation the model is sent over the scenario: after each tool result, the
    history so far (compacted) goes to the model once. The fixed system prompt is left out."""
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "task"}]
    total = 0
    for i, (name, res) in enumerate(results):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "name": name, "content": res.for_llm()})
        compact(msgs, CONTEXT_BUDGET_CHARS)
        total += sum(len(str(m.get("content") or "")) for m in msgs)
    return total


async def _run_mode(mgr: BrowserManager, site: str, mode: str, monkeypatch: pytest.MonkeyPatch
                    ) -> dict[str, dict[str, float]]:
    before = mode == "before"
    monkeypatch.setattr(browser, "settle", legacy_settle if before else _NEW_SETTLE)
    mgr.diff_snapshots = not before
    rows: dict[str, dict[str, float]] = {}
    for name, scenario in SCENARIOS.items():
        start = time.perf_counter()
        try:
            results = await scenario(mgr, site)
        except AssertionError as e:
            raise AssertionError(f"{mode} / {name}: {e}") from e
        took = time.perf_counter() - start
        for _, r in results:
            assert r.ok, (mode, name, r.output)
        tool_chars = sum(len(r.for_llm()) for _, r in results)
        ctx = context_chars(results, legacy_compact if before else compact_history)
        rows[name] = {"steps": len(results), "tool_chars": tool_chars, "context_chars": ctx,
                      "est_tokens": ctx // 4, "seconds": took}
    return rows


_NEW_SETTLE = browser.settle


def _write_reports(table: str, report: dict[str, Any]) -> None:
    """The table into the GitHub job summary, and everything into DELTA_BENCH_OUT when set."""
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Delta browser benchmark\n\nbefore = old settle + full listings; after = this branch\n\n"
                    + table + "\n")
    if out := os.environ.get("DELTA_BENCH_OUT"):
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)


async def test_benchmark_before_and_after(mgr: BrowserManager, site: str, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    ok(await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/search.html")))  # browser start is not timed
    runs: dict[str, list[dict[str, dict[str, float]]]] = {"before": [], "after": []}
    for _ in range(3):  # interleaved rounds, so warm-up and noise hit both sides alike
        for mode in ("before", "after"):
            runs[mode].append(await _run_mode(mgr, site, mode, monkeypatch))
    monkeypatch.setattr(browser, "settle", _NEW_SETTLE)

    def med(mode: str, scen: str, key: str) -> float:
        return statistics.median(r[scen][key] for r in runs[mode])

    lines = ["| scenario | steps | tool chars before -> after | context chars before -> after "
             "(~tokens) | seconds before -> after (median of 3) |", "|---|---|---|---|---|"]
    totals = {k: [0.0, 0.0] for k in ("tool_chars", "context_chars", "seconds")}
    for scen in SCENARIOS:
        for k in totals:
            totals[k][0] += med("before", scen, k)
            totals[k][1] += med("after", scen, k)
        steps = f"{int(med('before', scen, 'steps'))} -> {int(med('after', scen, 'steps'))}"
        lines.append(f"| {scen} | {steps} | {int(med('before', scen, 'tool_chars'))} -> "
                     f"{int(med('after', scen, 'tool_chars'))} | {int(med('before', scen, 'context_chars'))} -> "
                     f"{int(med('after', scen, 'context_chars'))} (~{int(med('before', scen, 'context_chars')) // 4}"
                     f" -> ~{int(med('after', scen, 'context_chars')) // 4}) | "
                     f"{med('before', scen, 'seconds'):.2f} -> {med('after', scen, 'seconds'):.2f} |")
    t = totals
    lines.append(f"| **total** | | {int(t['tool_chars'][0])} -> {int(t['tool_chars'][1])} | "
                 f"{int(t['context_chars'][0])} -> {int(t['context_chars'][1])} | "
                 f"{t['seconds'][0]:.2f} -> {t['seconds'][1]:.2f} |")
    table = "\n".join(lines)
    report = {"runs": runs, "table": table}
    with capsys.disabled():
        print("\n\nDELTA BROWSER BENCHMARK (before = old settle + full listings; after = this branch)\n" + table)
        print("DELTA_BENCH_JSON " + json.dumps({m: [{s: r[s] for s in r} for r in rs] for m, rs in runs.items()}))
    _write_reports(table, report)
    # Characters are deterministic: the new way must never send more. Time is only reported.
    assert t["tool_chars"][1] <= t["tool_chars"][0]
    assert t["context_chars"][1] <= t["context_chars"][0]
