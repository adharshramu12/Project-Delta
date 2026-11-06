"""How long `open_app` takes on a clean Windows PC: a benchmark, printed to the job log and job summary.

Written before the faster app opening (batch H) so the same file measures the old path ("before") and the
new one ("after"). It only uses what both have: OpenApp.run, start_apps() and match_start_app().

  lookup_cold   find an app by its Start-menu name with nothing cached (the old path asks PowerShell's
                Get-StartApps every time Delta starts; the new one only on a fresh install or after a change)
  lookup_restart  (new code only) the same lookup after a restart: the index is read from the data folder
  lookup_warm   the same lookup again
  launch        open Notepad (on PATH) until its window is in front
  open_again    open Notepad again while it is already open

Run with DELTA_E2E=1 pytest -m e2e tests/e2e/test_open_app_speed_e2e.py. Each number is the median of a few
rounds. Loose ceilings catch a real regression without failing on a slow runner.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import statistics
import sys
import time
from typing import Any

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="e2e: set DELTA_E2E=1"),
    pytest.mark.skipif(sys.platform != "win32", reason="Windows only"),
]

ROUNDS = 3


def _reset_caches(tmp: Any) -> None:
    """Forget every app list Delta keeps, in memory and on disk (old and new code)."""
    from delta.tools import apps

    if hasattr(apps, "_start_apps"):
        apps._start_apps = None
    reset = getattr(apps, "reset_app_index", None)
    if callable(reset):
        reset(tmp)


def _close_notepads(before: set[int]) -> None:
    import psutil

    for p in psutil.process_iter(["name", "pid"]):
        if (p.info["name"] or "").lower() == "notepad.exe" and p.info["pid"] not in before:
            with contextlib.suppress(Exception):
                p.kill()


def _notepad_pids() -> set[int]:
    import psutil

    return {p.info["pid"] for p in psutil.process_iter(["name", "pid"])
            if (p.info["name"] or "").lower() == "notepad.exe"}


def _write(report: dict[str, Any]) -> None:
    rows = "\n".join(f"| {k} | {v['median_s']:.3f} | {', '.join(f'{x:.3f}' for x in v['rounds'])} |"
                     for k, v in report["timings"].items())
    table = "| step | median s | rounds |\n|---|---|---|\n" + rows
    print("\n" + json.dumps(report, indent=1) + "\n" + table)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## Delta open_app benchmark ({report['code']})\n\n{table}\n")
    if out := os.environ.get("DELTA_APPS_BENCH_OUT"):
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=1)


def test_open_app_benchmark(tmp_path, capsys):
    from delta.tools import apps
    from delta.tools.apps import OpenApp, OpenAppArgs, match_start_app, start_apps

    code = "after" if hasattr(apps, "reset_app_index") else "before"
    tool = OpenApp({}, tmp_path) if code == "after" else OpenApp({})  # type: ignore[call-arg]
    timings: dict[str, list[float]] = {"lookup_cold": [], "lookup_warm": [], "launch": [], "open_again": []}
    if code == "after":
        timings["lookup_restart"] = []
    outputs: list[str] = []
    before = _notepad_pids()
    try:
        for _ in range(ROUNDS):
            _reset_caches(tmp_path)
            t0 = time.perf_counter()
            match_start_app("notepad", start_apps())
            timings["lookup_cold"].append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            match_start_app("notepad", start_apps())
            timings["lookup_warm"].append(time.perf_counter() - t0)
            if code == "after":
                apps.AppIndex(tmp_path).store()  # what the first start leaves in the data folder
                t0 = time.perf_counter()
                match_start_app("notepad", apps.AppIndex(tmp_path).store())  # Delta restarted
                timings["lookup_restart"].append(time.perf_counter() - t0)

            _close_notepads(before)
            time.sleep(0.5)
            t0 = time.perf_counter()
            first = asyncio.run(tool.run(OpenAppArgs(name="notepad")))
            timings["launch"].append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            again = asyncio.run(tool.run(OpenAppArgs(name="notepad")))
            timings["open_again"].append(time.perf_counter() - t0)
            outputs += [first.output, again.output]
            assert first.ok and again.ok, (first.output, again.output)
    finally:
        _close_notepads(before)
    report = {"code": code, "rounds": ROUNDS, "outputs": outputs[:4],
              "timings": {k: {"median_s": statistics.median(v), "rounds": v} for k, v in timings.items()}}
    with capsys.disabled():
        _write(report)
    # Ceilings far above what a healthy runner needs: only a real regression trips them.
    assert report["timings"]["lookup_warm"]["median_s"] < 1.0
    assert report["timings"]["launch"]["median_s"] < 15

