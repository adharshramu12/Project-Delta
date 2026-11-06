"""Apps is the one place to connect apps; MCP is an engine detail.

Before 0.2.x the dashboard showed an "mcp" checkbox under Settings > Add-ons (the MCP bridge add-on, which runs the
servers listed under `addons.config.mcp.servers`) next to the Apps page (connected apps, which speak MCP
themselves). People found two switches for one idea confusing. Now:

- Settings > Add-ons no longer lists "mcp".
- Servers under `addons.config.mcp.servers` start by themselves whenever any are configured. They show on the Apps
  page under "Advanced: custom MCP servers", with one on/off switch (`addons.config.mcp.enabled`).
- Migration, once, with no lost settings: a settings file that ticked "mcp" keeps its servers on; one that has
  servers but left "mcp" unticked keeps them off (`enabled: false` is written); "mcp" leaves `addons.enabled`.
"""

from __future__ import annotations

from typing import Any

MCP = "mcp"


def _cfg(addons: Any) -> dict[str, Any]:
    cfg = (getattr(addons, "config", None) or {}).get(MCP) or {}
    return cfg if isinstance(cfg, dict) else {}


def servers(addons: Any) -> dict[str, dict[str, Any]]:
    found = _cfg(addons).get("servers") or {}
    return found if isinstance(found, dict) else {}


def legacy_on(addons: Any) -> bool:
    """Should the MCP bridge run? Only with servers to run; then on unless switched off (or, in a settings file not
    migrated yet, unless "mcp" was left unticked)."""
    if not servers(addons):
        return False
    cfg = _cfg(addons)
    if "enabled" in cfg:
        return bool(cfg["enabled"])
    return True


def effective_addons(addons: Any) -> list[str]:
    """The add-ons to start: the user's list without "mcp", plus "mcp" when its servers should run."""
    names = [a for a in (getattr(addons, "enabled", None) or []) if a != MCP]
    return [*names, MCP] if legacy_on(addons) else names


def migration(addons: Any) -> dict[str, Any] | None:
    """The settings change that moves an old file to the new model, or None when there is nothing to move."""
    enabled = list(getattr(addons, "enabled", None) or [])
    cfg = _cfg(addons)
    ticked = MCP in enabled
    if "enabled" in cfg and not ticked:
        return None  # already migrated
    if not ticked and not servers(addons):
        return None  # nothing configured: servers added later start by themselves
    change: dict[str, Any] = {"config": {MCP: {"enabled": bool(cfg.get("enabled", ticked))}}}
    if ticked:
        change["enabled"] = [a for a in enabled if a != MCP]
    return {"addons": change}


def apply_migration(settings: Any) -> dict[str, Any] | None:
    """Migrate the in-memory settings and the user's settings file. Returns what changed (None: nothing)."""
    change = migration(settings.addons)
    if change is None:
        return None
    from delta.config import write_user_settings

    write_user_settings(settings.data_dir, change)
    add = change["addons"]
    if "enabled" in add:
        settings.addons.enabled = add["enabled"]
    settings.addons.config.setdefault(MCP, {})["enabled"] = add["config"][MCP]["enabled"]
    return change


def describe(addons: Any) -> list[dict[str, Any]]:
    """The servers for the Apps page: name and what runs or is reached (never a secret value)."""
    rows = []
    for name, cfg in servers(addons).items():
        cfg = cfg if isinstance(cfg, dict) else {}
        if "url" in cfg:
            what = str(cfg["url"])
        else:
            what = " ".join([str(cfg.get("command", "")), *[str(a) for a in cfg.get("args", []) or []]]).strip()
        rows.append({"name": str(name), "runs": what[:300], "remote": "url" in cfg})
    return rows
