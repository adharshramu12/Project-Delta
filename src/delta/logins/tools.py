"""The model's side of saved logins: sign_in(site) and list_logins. Neither ever shows a password or a user name.

sign_in works in Delta's private browser (the one Delta controls in the background, where it can check the page's
exact origin and its frames before typing). When the task was using the user's own browser, sign_in moves the rest
of THIS task to the private browser and says so; the saved browser choice does not change.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from delta.core.state import CURRENT_REQUEST
from delta.logins.service import LoginService
from delta.tools.base import Risk, Tool, ToolResult


class SignInArgs(BaseModel):
    site: str = Field(description="The saved login's name or site, e.g. 'College ERP' or erp.college.edu "
                                  "(list_logins shows them)")


class SignIn(Tool[SignInArgs]):
    name = "sign_in"
    description = ("Sign in to a site with the user's SAVED login: Delta opens the site's sign-in page in its private "
                   "browser and types the user name and password itself (you never see them), only on the exact "
                   "saved site. Use when a page needs the user to be signed in. Never type passwords yourself.")
    risk = Risk.ACT  # each login's own permission decides (LoginService.allowed): ask every time by default
    Args = SignInArgs
    untrusted_output = True  # the page after signing in is outside content

    def __init__(self, service: LoginService, hub: Any) -> None:
        self.service = service
        self.hub = hub

    def available(self) -> bool:
        return getattr(self.hub, "mgr", None) is not None and self.service.store.count() > 0

    def domains(self, args: SignInArgs) -> list[str]:
        login = self.service.store.get(args.site)
        return [login.origin] if login else []

    def summary(self, args: SignInArgs) -> str:
        login = self.service.store.get(args.site)
        return f"sign in to {login.name} ({login.origin}) with the saved login" if login else f"sign in to {args.site}"

    async def run(self, args: SignInArgs) -> ToolResult:
        from delta.tools.browser import _observed, settle
        from delta.tools.browser_choice import BrowserPick
        from delta.tools.browser_switch import TASK_BROWSER

        mgr = getattr(self.hub, "mgr", None)
        if mgr is None:
            return ToolResult(False, "sign_in needs Delta's private browser, which is not available on this PC "
                                     "(it needs Google Chrome or Microsoft Edge).")
        note = ""
        tb = TASK_BROWSER.get()
        if tb is not None and tb.pick.kind != "private":
            tb.pick, tb.source = BrowserPick("private", self.hub.settings.browser.private_engine), "this task"
            note = (" (Signed in in Delta's private browser; the rest of this task uses it. Your saved browser choice "
                    "is unchanged.)")
        if tb is not None:
            tb.used = True
        req = CURRENT_REQUEST.get()
        try:
            page = await mgr.page()
        except Exception as e:
            return ToolResult(False, f"could not start Delta's private browser ({type(e).__name__})")
        self.service.settle = settle
        result = await self.service.sign_in(args.site, page, task_id=req.id if req else "")
        evidence = {"site": args.site, "outcome": result.outcome}
        if not result.ok:
            return ToolResult(False, result.message + note, evidence)
        observed = await _observed(page, mgr, result.message + note, force_full=True)
        observed.evidence.update(evidence)
        return observed


class ListLoginsArgs(BaseModel):
    pass


class ListLogins(Tool[ListLoginsArgs]):
    name = "list_logins"
    description = ("List the user's saved site logins (name, site, permission). Never shows user names or "
                   "passwords. Use sign_in with one of them.")
    risk = Risk.READ
    Args = ListLoginsArgs

    def __init__(self, service: LoginService) -> None:
        self.service = service

    def available(self) -> bool:
        return self.service.store.count() > 0

    async def run(self, args: ListLoginsArgs) -> ToolResult:
        items = self.service.store.list()
        if not items:
            return ToolResult(True, "No saved logins. The user adds them on the PC: Settings > Logins, or "
                                    "`delta logins add`. Passwords are never given in chat.", {"count": 0})
        rows = [f"- {x.name}: {x.origin} ({x.public()['permission_words'].lower()})" for x in items]
        return ToolResult(True, "\n".join(rows), {"count": len(items)})


def login_tools(service: LoginService, hub: Any) -> list[Tool[Any]]:
    return [SignIn(service, hub), ListLogins(service)]
