"""Discovers and starts enabled add-ons. A broken add-on is logged and skipped; it never
stops Delta from starting."""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from delta.addons.api import Addon, AddonContext
from delta.system.logging import log

SETUP_TIMEOUT_S = 20


@dataclass
class AddonInfo:
    name: str
    version: str
    description: str
    source: str
    status: str  # running | failed | not found
    tools: list[str]
    error: str = ""


def _from_entry_points() -> dict[str, Any]:
    found = {}
    for ep in entry_points(group="delta.addons"):
        found[ep.name] = ep
    return found


def _load_folder(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"delta_addon_{path.parent.name}", path)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.addon


def discover(addon_dir: Path) -> dict[str, tuple[str, Any]]:
    """name -> (source, loader). Loaders are not called here (no add-on code runs)."""
    out: dict[str, tuple[str, Any]] = {}
    for name, ep in _from_entry_points().items():
        out[name] = ("package", ep.load)
    if addon_dir.is_dir():
        for f in sorted(addon_dir.glob("*/addon.py")):
            out.setdefault(f.parent.name, ("folder", lambda f=f: _load_folder(f)))
    return out


async def start_addons(enabled: list[str], configs: dict[str, dict[str, Any]], builtin: dict[str, Any],
                       make_ctx: Any, addon_dir: Path) -> tuple[list[AddonInfo], list[AddonContext]]:
    available = discover(addon_dir)
    infos: list[AddonInfo] = []
    contexts: list[AddonContext] = []
    for name in enabled:
        if name in builtin:
            source, load = "built-in", builtin[name].load
        elif name in available:
            source, load = available[name]
        else:
            infos.append(AddonInfo(name, "", "", "", "not found", [], "not installed"))
            continue
        ctx: AddonContext = make_ctx(name, configs.get(name, {}))
        try:
            addon = load()
            if not isinstance(addon, Addon):
                raise TypeError("object does not implement the Addon protocol (name, version, description, setup)")
            await asyncio.wait_for(addon.setup(ctx), SETUP_TIMEOUT_S)
        except Exception as e:
            log.error("addon.failed", addon=name, error=str(e))
            infos.append(AddonInfo(name, "", "", source, "failed", ctx.tools, f"{type(e).__name__}: {e}"))
            continue
        contexts.append(ctx)
        infos.append(AddonInfo(name, addon.version, addon.description, source, "running", ctx.tools))
        log.info("addon.started", addon=name, tools=ctx.tools)
    return infos, contexts
