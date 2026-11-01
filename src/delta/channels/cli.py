"""Terminal channel. Typing never blocks the agent: a reader thread feeds a queue, so you can
answer approvals (y/n) while a task is running."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from typing import Any

from delta.bootstrap import Container
from delta.core.state import TaskRequest
from delta.safety.approvals import ApprovalRequest

DIM, BOLD, CYAN, YELLOW, GREEN, RED, RESET = (
    "\033[2m", "\033[1m", "\033[36m", "\033[33m", "\033[32m", "\033[31m", "\033[0m")

HELP = """commands:
  /status            health, models, memory size
  /memory            list what Delta remembers about you
  /remember <fact>   add a memory          /forget <id>   delete a memory
  /audit             last audit entries    /help          this help
  /quit              shut Delta down completely"""


class CliChannel:
    def __init__(self, c: Container) -> None:
        self.c = c
        self.lines: asyncio.Queue[str | None] = asyncio.Queue()
        self.tasks: set[asyncio.Task[Any]] = set()

    async def present_approval(self, req: ApprovalRequest) -> None:
        print(f"\n{YELLOW}{BOLD}⚠ Approval needed [{req.id}]{RESET} {req.summary}\n"
              f"{DIM}  risk={req.risk} · {req.reason}{RESET}\n  approve? [y/N] ", end="", flush=True)

    async def on_event(self, event: str, data: dict[str, Any]) -> None:
        if event == "task.plan" and data["plan"].get("needs_tools"):
            steps = data["plan"].get("steps") or []
            print(f"{DIM}plan: " + " → ".join(steps[:6]) + RESET)
        elif event == "task.step":
            args = json.dumps(data["args"], ensure_ascii=False)
            print(f"{DIM}  · {data['tool']} {args[:120]}{RESET}")
        elif event == "task.verify":
            mark = f"{GREEN}✓ verified" if data["passed"] else f"{RED}✗ not verified"
            print(f"{DIM}  {mark}{RESET}{DIM} (attempt {data['attempt']}): {data['reason']}{RESET}")
        elif event == "governor.health":
            print(f"\n{YELLOW}hardware: {data['health']} {', '.join(data['reasons'])}{RESET}")

    def _reader(self, loop: asyncio.AbstractEventLoop) -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(self.lines.put_nowait, line.lstrip("﻿").rstrip("\n"))
        loop.call_soon_threadsafe(self.lines.put_nowait, None)

    async def run(self) -> None:
        self.c.approvals.add_presenter("cli", self.present_approval)
        self.c.events.subscribe(self.on_event)
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, self._reader, loop)
        name = self.c.settings.agent.name
        print(f"{CYAN}{BOLD}Δ {name} is online.{RESET} {DIM}Type a task, /help for commands.{RESET}")
        self._prompt()
        while (line := await self.lines.get()) is not None:
            text = line.strip()
            if not text:
                self._prompt()
                continue
            pending = self.c.approvals.pending()
            if pending and text.lower() in {"y", "yes", "n", "no"}:
                self.c.approvals.resolve(pending[0].id, text.lower().startswith("y"), by="cli")
                continue
            if text.startswith("/"):
                if text in {"/quit", "/exit"}:
                    break
                self._command(text)
                self._prompt()
                continue
            t = asyncio.create_task(self._task(text))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)
        for t in self.tasks:
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _task(self, text: str) -> None:
        result = await self.c.submit(TaskRequest(text, channel="cli"))
        colour = {"done": GREEN, "failed": RED}.get(result.status, YELLOW)
        via = f" via {result.provider}" if result.provider else ""
        print(f"\n{colour}{BOLD}{self.c.settings.agent.name}{RESET}{DIM}{via}{RESET}: {result.answer}")
        self._prompt()

    def _prompt(self) -> None:
        print(f"{CYAN}you ›{RESET} ", end="", flush=True)

    def _command(self, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        mem = self.c.memory
        if cmd == "/status":
            print(json.dumps({"governor": self.c.governor.status(),
                              "providers": self.c.router.usable(),
                              "last_provider": self.c.router.last_provider,
                              "tools": self.c.registry.names(),
                              "memories": len(mem.list())}, indent=1, default=str))
        elif cmd == "/memory":
            items = mem.list()
            print("\n".join(f"#{m.id} [{m.kind}/{m.source}] {m.content}" for m in items) or "(no memories)")
        elif cmd == "/remember" and arg:
            print(f"remembered #{mem.add(arg, 'profile', 'user')}")
        elif cmd == "/forget" and arg.strip().isdigit():
            print("deleted" if mem.delete(int(arg)) else "no such memory")
        elif cmd == "/audit":
            for r in self.c.audit.tail(15):
                print(f"{DIM}{r['ts'][11:19]}{RESET} {r['event']:<10} {json.dumps(r['data'], default=str)[:150]}")
        else:
            print(HELP)
