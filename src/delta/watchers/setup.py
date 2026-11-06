"""Wiring for bootstrap: the watcher store, the service and its tools."""

from __future__ import annotations

from typing import Any

from delta.watchers.service import WatchService
from delta.watchers.store import WatchStore
from delta.watchers.tools import watcher_tools


def attach(settings: Any, registry: Any, logins: Any, hub: Any, scheduler: Any, skills: Any, audit: Any,
           lock: Any) -> WatchService:
    from delta.tools.browser import settle

    service = WatchService(WatchStore(settings.data_dir / "watchers.db"), logins, hub, scheduler, skills, audit,
                           lock, settle=settle)
    for tool in watcher_tools(service):
        registry.register(tool)
    return service
