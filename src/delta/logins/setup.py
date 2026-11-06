"""Wiring for bootstrap: the login store, the service and its two tools (kept here so the composition root only
gains a couple of lines)."""

from __future__ import annotations

from typing import Any

from delta.logins.service import LoginService
from delta.logins.store import LoginStore
from delta.logins.tools import login_tools


def attach(settings: Any, registry: Any, hub: Any, audit: Any, approvals: Any, solver: Any = None, proof: Any = None,
           channels: dict[str, Any] | None = None) -> LoginService:
    """Register sign_in and list_logins. The caller sets service.guard once the Remote Guard exists."""
    from delta.tools.browser import settle

    service = LoginService(LoginStore(settings.data_dir / "logins"), audit, approvals, captcha_solver=solver,
                           proof=proof, channels=channels, settle=settle)
    for tool in login_tools(service, hub):
        registry.register(tool)
    return service


def attach_flow(orchestrator: Any, service: LoginService, executor: Any, events: Any) -> None:
    """Saved-login requests as a fixed tool flow (delta.logins.flow), and each login's captcha switch as part of the
    captcha allowlist (exact origin only)."""
    from delta.logins.flow import LoginFlow

    orchestrator.login_flow = LoginFlow(service, executor, events)
    if service.solver is not None and hasattr(service.solver, "origin_allowed"):
        service.solver.origin_allowed = service.store.captcha_allowed
