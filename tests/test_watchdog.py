"""The tool watchdog: time limits, whole process trees ended on a timeout or Stop (audit M5), only the
processes a call started, and one restart of a dead Delta browser."""

import asyncio
import subprocess
import sys
import time
import uuid

import psutil
import pytest
from pydantic import BaseModel

from delta.config import EngineConfig
from delta.engine.watchdog import Watchdog
from delta.system import proctree
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.power import PythonArgs, RunPython


class NoArgs(BaseModel):
    pass


class Sleepy(Tool[NoArgs]):
    """Starts a helper program that starts its own child, then hangs."""
    name = "sleepy"
    description = "test"
    risk = Risk.WRITE
    Args = NoArgs

    def __init__(self, marker: str) -> None:
        self.marker = marker

    async def run(self, args: NoArgs) -> ToolResult:
        code = (f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', "
                f"'import time; time.sleep(60)', '{self.marker}-child']); time.sleep(60)")
        proc = subprocess.Popen([sys.executable, "-c", code, f"{self.marker}-parent"])  # noqa: ASYNC220
        proctree.track(proc.pid)
        await asyncio.sleep(60)
        return ToolResult(True, "never")


def alive(marker: str) -> list[int]:
    found = []
    for p in psutil.process_iter(["cmdline"]):
        try:
            if any(marker in part for part in p.info["cmdline"] or []):
                found.append(p.pid)
        except psutil.Error:
            continue
    return found


def wait_gone(marker: str, seconds: float = 8) -> list[int]:
    deadline = time.monotonic() + seconds
    while (left := alive(marker)) and time.monotonic() < deadline:
        time.sleep(0.2)
    return left


def wait_started(marker: str, n: int, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while len(alive(marker)) < n and time.monotonic() < deadline:
        time.sleep(0.1)


async def test_timeout_ends_the_whole_tree():
    marker = f"wdtest{uuid.uuid4().hex[:8]}"
    dog = Watchdog(EngineConfig(tool_timeouts={"sleepy": 3}))
    result = await dog.run(Sleepy(marker), NoArgs(), "t1")
    assert not result.ok and "watchdog" in result.output and result.evidence["watchdog"] == "timeout"
    assert result.evidence["killed"] >= 2  # the helper and the child it started
    assert wait_gone(marker) == []


async def test_stop_ends_the_whole_tree():
    marker = f"wdstop{uuid.uuid4().hex[:8]}"
    dog = Watchdog(EngineConfig(default_tool_timeout_s=0))
    task = asyncio.create_task(dog.run(Sleepy(marker), NoArgs(), "t2"))
    await asyncio.to_thread(wait_started, marker, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wait_gone(marker) == []


async def test_processes_delta_did_not_start_for_the_call_are_left_alone():
    marker = f"wdother{uuid.uuid4().hex[:8]}"
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", marker])  # noqa: ASYNC220
    try:
        dog = Watchdog(EngineConfig(tool_timeouts={"sleepy": 2}))
        await dog.run(Sleepy(f"{marker}x"), NoArgs(), "t3")
        assert other.poll() is None  # still running
        assert proctree.kill_tree(other.pid, create_time=1.0) == 0  # pid/start time mismatch: untouched
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


async def test_run_python_timeout_ends_what_the_script_started():
    marker = f"wdpy{uuid.uuid4().hex[:8]}"
    code = (f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', "
            f"'{marker}'])\ntime.sleep(60)\n")
    result = await RunPython().run(PythonArgs(code=code, timeout_s=3))
    assert not result.ok and "timed out" in result.output
    assert wait_gone(marker) == []


def test_limits():
    dog = Watchdog(EngineConfig(default_tool_timeout_s=600, tool_timeouts={"web_search": 10}))

    class T:
        def __init__(self, name):
            self.name = name

    assert dog.limit_for(T("web_search"), None) == 10
    assert dog.limit_for(T("browser_click"), None) == 180
    assert dog.limit_for(T("run_python"), PythonArgs(code="", timeout_s=500)) == 600
    assert dog.limit_for(T("make_document"), None) == 600
    short = Watchdog(EngineConfig(default_tool_timeout_s=120))
    assert short.limit_for(T("run_python"), PythonArgs(code="", timeout_s=500)) == 530  # never cuts it short
    assert Watchdog(EngineConfig(default_tool_timeout_s=0)).limit_for(T("x"), None) is None


async def test_a_tools_own_timeout_error_is_not_the_watchdogs():
    class Own(Tool[NoArgs]):
        name, description, risk, Args = "own", "t", Risk.READ, NoArgs

        async def run(self, args):
            raise TimeoutError("the site took too long")

    with pytest.raises(TimeoutError):
        await Watchdog(EngineConfig()).run(Own(), NoArgs(), "t4")


class FakeBrowser:
    def __init__(self):
        self.closed = self.opened = 0

    async def page(self):
        self.opened += 1

    async def close(self):
        self.closed += 1


class DeadBrowserTool(Tool[NoArgs]):
    name, description, risk, Args = "browser_click", "t", Risk.ACT, NoArgs

    def __init__(self, mgr):
        self.mgr = mgr

    async def run(self, args):
        raise RuntimeError("Target page, context or browser has been closed")


async def test_a_dead_delta_browser_is_restarted_once_per_task():
    mgr = FakeBrowser()
    dog = Watchdog(EngineConfig())
    first = await dog.run(DeadBrowserTool(mgr), NoArgs(), "t5")
    assert not first.ok and "restarted the Delta browser once" in first.output and first.evidence["watchdog"]
    second = await dog.run(DeadBrowserTool(mgr), NoArgs(), "t5")
    assert "already restarted" in second.output
    assert (mgr.closed, mgr.opened) == (1, 1)
    dog.forget("t5")
    await dog.run(DeadBrowserTool(mgr), NoArgs(), "t5")
    assert mgr.closed == 2
