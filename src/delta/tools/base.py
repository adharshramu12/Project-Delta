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

    def scope(self, args: A) -> str:
        """The *kind* of thing acted on, so Delta can learn per kind ("deletes in Downloads"),
        not per exact file: the parent folder of a path, a website's host, or an app name."""
        import os
        from urllib.parse import urlparse

        if paths := [p for p in self.paths(args) if p]:
            full = os.path.normcase(os.path.abspath(os.path.expandvars(os.path.expanduser(paths[0]))))
            return os.path.dirname(full) if os.path.splitext(full)[1] else full
        if domains := self.domains(args):
            d = domains[0]
            return (urlparse(d if "://" in d else f"https://{d}").hostname or d).lower()
        return (self.app(args) or "").lower()

    def summary(self, args: A) -> str:
        """One line shown to the human in approval prompts."""
        return f"{self.name}({args.model_dump_json()})"

    @abstractmethod
    async def run(self, args: A) -> ToolResult: ...

    def spec(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                  "parameters": _compact(self.Args.model_json_schema())}}


def _compact(node: Any) -> Any:
    """Drop JSON-schema keys models do not need ('title' on every field). Tool specs are sent on
    every call, so each token saved here is saved many times per task."""
    if isinstance(node, dict):
        return {k: _compact(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_compact(v) for v in node]
    return node
