"""Add-ons shipped with Delta. They use the same public SDK as community add-ons."""

from __future__ import annotations

from typing import Any


def builtin_addons() -> dict[str, Any]:
    """Imported lazily so a disabled add-on costs nothing."""

    class _Lazy:
        def __init__(self, module: str) -> None:
            self.module = module

        def load(self) -> Any:
            import importlib

            return importlib.import_module(self.module).addon

    return {"mcp": _Lazy("delta.addons.builtin.mcp_bridge")}
