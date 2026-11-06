"""Building the connected apps pieces from Settings (used by bootstrap, the CLI and the evals)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from delta.config import REPO_ROOT, Settings
from delta.connectors.catalog import Catalog, load_catalog
from delta.connectors.manager import AppManager
from delta.connectors.manager import Settings as ManagerSettings


def catalog_path(settings: Settings) -> Path:
    if settings.connectors.catalog:
        return Path(settings.connectors.catalog).expanduser()
    own = settings.config_dir / "mcp_catalog.yaml"
    return own if own.exists() else REPO_ROOT / "config" / "mcp_catalog.yaml"


def manager_settings(settings: Settings) -> ManagerSettings:
    c = settings.connectors
    return ManagerSettings(call_timeout_s=c.call_timeout_s, connect_timeout_s=c.connect_timeout_s,
                           read_retries=c.read_retries, breaker_failures=c.breaker_failures,
                           breaker_cooldown_s=c.breaker_cooldown_s, sign_in_timeout_s=c.sign_in_timeout_s,
                           idle_close_s=c.idle_close_s)


def build_manager(settings: Settings, audit: Any = None, catalog: Catalog | None = None, **kw: Any) -> AppManager:
    return AppManager(settings.data_dir, catalog or load_catalog(catalog_path(settings)), manager_settings(settings),
                      audit=audit, **kw)


def attach(settings: Settings, registry: Any, audit: Any = None) -> Any:
    """The manager plus the agent's app tools (AppTools), or None when connectors.enabled is false."""
    if not settings.connectors.enabled:
        return None
    from delta.connectors.tools import AppTools

    return AppTools(build_manager(settings, audit), registry)
