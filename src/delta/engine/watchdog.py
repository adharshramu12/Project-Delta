"""The tool watchdog: no single tool call can hang a task forever or leave programs running behind it.

Around every tool call (ToolExecutor._run):
- a time limit per tool (EngineConfig.default_tool_timeout_s / tool_timeouts; a tool's own timeout_s
  argument is honoured plus a grace period). Past it, the call is stopped and the step fails with a
  clear reason the model and the user can read;
- on that timeout, or when the user presses Stop, every process the call started (delta.system.proctree)
  is ended together with its children. Nothing else is touched: not the user's own browser, not an app
  the user opened, not a process Delta did not start for this call;
- a Delta-profile browser (its own Chrome/Edge over CDP) that died or stopped answering is restarted
  once per task, and the step is reported failed (it is never silently re-run: it may have had effects).

A blocking call running in a worker thread cannot be interrupted by Python; after a timeout its result is
ignored and the task moves on. Processes are what hold real resources, and those are ended.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from delta.config import EngineConfig
from delta.system import proctree
from delta.system.logging import log
from delta.tools.base import ToolResult

# Sensible limits for tools that should never take minutes. Everything else: default_tool_timeout_s.
DEFAULT_LIMITS: dict[str, float] = {
    "web_search": 90, "fetch_page": 90, "read_file": 60, "list_dir": 60, "path_info": 30, "recall": 30,
    "media_status": 60, "list_running_apps": 60, "list_windows": 60, "read_window": 90,
}
BROWSER_LIMIT_S = 180  # one browser action (open, click, fill, read) including page loads
OWN_TIMEOUT_GRACE_S = 30  # on top of a tool's own timeout_s, so the tool reports its own timeout first
DEAD_BROWSER = re.compile(
    r"(target (page, context or browser )?(has been )?closed|browser has (been closed|disconnected)|"
    r"connection closed|websocket (is not open|closed)|not connected|econnrefused|"
    r"did not open its remote-control port|browser closed)", re.I)


def _browser_of(tool: Any) -> Any:
    """Delta's own browser manager (delta-profile mode), if this tool drives it. Never the user's browser."""
    mgr = getattr(tool, "mgr", None)
    if mgr is None:
        return None
    return mgr if callable(getattr(mgr, "page", None)) and callable(getattr(mgr, "close", None)) else None


class Watchdog:
    def __init__(self, cfg: EngineConfig | None = None, audit: Any = None) -> None:
        self.cfg = cfg or EngineConfig()
        self.audit = audit
        self._restarted: set[str] = set()  # tasks whose browser was already restarted once

    def limit_for(self, tool: Any, args: Any) -> float | None:
        name = str(getattr(tool, "name", ""))
        if name in self.cfg.tool_timeouts:
            limit = float(self.cfg.tool_timeouts[name])
        elif name in DEFAULT_LIMITS:
            limit = DEFAULT_LIMITS[name]
        elif name.startswith("browser_"):
            limit = BROWSER_LIMIT_S
        else:
            limit = float(self.cfg.default_tool_timeout_s)
        if limit <= 0:
            return None
        own = getattr(args, "timeout_s", None)
        if isinstance(own, int | float) and own > 0:
            limit = max(limit, float(own) + OWN_TIMEOUT_GRACE_S)
        return limit

    def forget(self, task_id: str) -> None:
        self._restarted.discard(task_id)

    async def run(self, tool: Any, args: Any, task_id: str = "") -> ToolResult:
        name = str(getattr(tool, "name", "tool"))
        limit = self.limit_for(tool, args)
        token, scope = proctree.begin_scope()
        try:
            try:
                async with asyncio.timeout(limit) as window:
                    result: ToolResult = await tool.run(args)
            except TimeoutError:
                if not window.expired():
                    raise  # the tool's own timeout error, not ours: handled like any tool error
                killed = await asyncio.to_thread(proctree.kill_scope, scope) if scope else 0
                note = await self._recover_browser(tool, task_id, f"{name} did not answer within {limit:g}s")
                self._audit("watchdog.timeout", task=task_id, tool=name, limit_s=limit, killed=killed)
                extra = f" Ended {killed} process(es) it had started." if killed else ""
                return ToolResult(False, f"STOPPED by the watchdog: {name} took longer than {limit:g} seconds."
                                         f"{extra}{note} Check the current state before trying anything again.",
                                  {"watchdog": "timeout", "limit_s": limit, "killed": killed})
            except asyncio.CancelledError:  # Stop pressed: nothing this call started keeps running
                if scope:
                    await asyncio.shield(asyncio.to_thread(proctree.kill_scope, scope))
                raise
            except Exception as e:
                if _browser_of(tool) is not None and DEAD_BROWSER.search(str(e)):
                    note = await self._recover_browser(tool, task_id, str(e)[:120])
                    return ToolResult(False, f"The Delta browser stopped responding ({type(e).__name__}).{note} "
                                             "This step did not finish; check the page before trying again.",
                                      {"watchdog": "browser_dead"})
                raise
            if not result.ok and _browser_of(tool) is not None and DEAD_BROWSER.search(result.output or ""):
                note = await self._recover_browser(tool, task_id, result.output[:120])
                result = ToolResult(False, f"{result.output}\nThe Delta browser stopped responding.{note} This step "
                                           "did not finish; check the page before trying again.",
                                    {**result.evidence, "watchdog": "browser_dead"})
            return result
        finally:
            proctree.end_scope(token)

    async def _recover_browser(self, tool: Any, task_id: str, why: str) -> str:
        """Restart Delta's own browser once per task. Returns a sentence for the step's result."""
        mgr = _browser_of(tool)
        if mgr is None:
            return ""
        key = task_id or "-"
        if key in self._restarted:
            return " The browser was already restarted once in this task, so I did not restart it again."
        self._restarted.add(key)
        try:
            await asyncio.wait_for(mgr.close(), 20)  # ends only the browser Delta itself started
            await asyncio.wait_for(mgr.page(), 60)
        except Exception as e:
            log.warning("watchdog.browser_restart_failed", task=task_id, error=str(e))
            self._audit("watchdog.browser_restart", task=task_id, ok=False, why=why)
            return " Restarting the Delta browser failed."
        self._audit("watchdog.browser_restart", task=task_id, ok=True, why=why)
        return " I restarted the Delta browser once."

    def _audit(self, event: str, **data: Any) -> None:
        if self.audit is not None:
            try:
                self.audit.record(event, **data)
            except Exception:
                log.exception("watchdog.audit_failed")
