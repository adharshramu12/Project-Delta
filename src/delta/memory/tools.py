"""Memory exposed to the agent as tools."""

from __future__ import annotations

from pydantic import BaseModel

from delta.memory.store import MemoryStore
from delta.tools.base import Risk, Tool, ToolResult


class RememberArgs(BaseModel):
    fact: str


class Remember(Tool[RememberArgs]):
    name = "remember"
    description = "Save a durable fact or preference about the user to long-term memory."
    risk = Risk.ACT
    Args = RememberArgs

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def run(self, args: RememberArgs) -> ToolResult:
        mem_id = self.store.add(args.fact, "profile", source="agent")
        return ToolResult(True, f"remembered (#{mem_id})", {"memory_id": mem_id})


class RecallArgs(BaseModel):
    query: str


class Recall(Tool[RecallArgs]):
    name = "recall"
    description = "Search long-term memory about the user and past tasks."
    risk = Risk.READ
    Args = RecallArgs

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def run(self, args: RecallArgs) -> ToolResult:
        hits = self.store.search(args.query, 8)
        return ToolResult(True, "\n".join(f"#{m.id} [{m.kind}] {m.content}" for m in hits) or "nothing found")


def memory_tools(store: MemoryStore) -> list[Tool]:
    return [Remember(store), Recall(store)]
