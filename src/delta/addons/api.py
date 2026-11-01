"""The public add-on SDK. This module is a stability promise to contributors: keep it small,
and never break it without a major version bump.

An add-on is any object with `name`, `version`, `description` and `async setup(ctx)`.
Ship it as a pip package with an entry point:

    [project.entry-points."delta.addons"]
    spotify = "delta_spotify:addon"

or as a folder  %LOCALAPPDATA%/Delta/addons/<name>/addon.py  exposing `addon`.
Nothing loads unless its name is listed under `addons.enabled` in delta.yaml.

Add-ons cannot bypass safety: every tool they register runs through the same
PolicyEngine -> approval -> audit path as built-in tools.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from delta.tools.base import Risk, Tool, ToolResult

if TYPE_CHECKING:
    from delta.core.events import EventBus
    from delta.llm.router import ModelRouter
    from delta.tools.registry import ToolRegistry

SDK_VERSION = "1.0"

__all__ = ["SDK_VERSION", "Addon", "AddonContext", "Risk", "Tool", "ToolResult"]


@runtime_checkable
class Addon(Protocol):
    name: str
    version: str
    description: str

    async def setup(self, ctx: AddonContext) -> None: ...


@dataclass
class AddonContext:
    addon_name: str
    config: dict[str, Any]           # addons.config.<name> from delta.yaml
    data_dir: Path                   # private folder for this add-on
    router: ModelRouter              # ask an LLM (respects the governor and fallbacks)
    events: EventBus
    _registry: ToolRegistry
    _shutdown: list[tuple[str, Callable[[], Awaitable[None]]]] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    def register_tool(self, tool: Tool[Any]) -> None:
        self._registry.register(tool)
        self.tools.append(tool.name)

    def on_shutdown(self, hook: Callable[[], Awaitable[None]]) -> None:
        self._shutdown.append((f"addon:{self.addon_name}", hook))
