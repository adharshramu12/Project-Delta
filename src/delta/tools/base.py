"""Tool contract. Each tool declares its risk and what it touches, so the PolicyEngine can
decide before anything runs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel


class Risk(StrEnum):
    READ = "read"                # look only: list files, read, search, screenshot
    ACT = "act"                  # visible but non-destructive: open app, click, type
    WRITE = "write"              # create or modify data
    DESTRUCTIVE = "destructive"  # delete, overwrite, kill
    EXTERNAL = "external"        # sends something outside the machine (messages, forms, uploads)
    SYSTEM = "system"            # OS/security settings, credentials, money: hard-blocked


@dataclass
class ToolResult:
    ok: bool
    output: str
    evidence: dict[str, Any] = field(default_factory=dict)  # facts the verifier can check

    def for_llm(self, limit: int = 6000) -> str:
        text = self.output if len(self.output) <= limit else self.output[:limit] + "\n…[truncated]"
        return ("OK: " if self.ok else "ERROR: ") + text


class Tool[A: BaseModel](ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    risk: ClassVar[Risk]
    Args: type[A]

    def risk_for(self, args: A) -> Risk:
        return self.risk

    def paths(self, args: A) -> list[str]:
        return []

    def domains(self, args: A) -> list[str]:
        return []

    def app(self, args: A) -> str | None:
        return None

    def summary(self, args: A) -> str:
        """One line shown to the human in approval prompts."""
        return f"{self.name}({args.model_dump_json()})"

    @abstractmethod
    async def run(self, args: A) -> ToolResult: ...

    def spec(self) -> dict[str, Any]:
        schema = self.Args.model_json_schema()
        schema.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                  "parameters": schema}}
