"""Using a saved login: the permission check, the hand-offs to the user and the audit trail around SignInEngine.

Permission, per login (set only by the user, in Settings > Logins or `delta logins allow`):

  ask      (default) every use asks first, on every approval channel (dashboard, Telegram, terminal). The prompt
           offers "Allow for this task", which covers the rest of that one task only (kept in memory, never saved).
  always   used without asking, on that exact site only.

Approving a sign-in from a phone needs the remote PIN (it is an "external" action, delta.safety.remote_guard), and a
request that came from Telegram can call sign_in only after /unlock (sign_in is a REMOTE_PIN_TOOLS entry). While
remote control is locked nothing remote signs in at all.

Every use is written to the audit log: the login's name, its site, the task and the outcome. Never the user name,
the password, a one-time code or a captcha answer.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from delta.core.state import CURRENT_REQUEST
from delta.logins.signin import SignInEngine, SignInResult
from delta.logins.store import Login, LoginStore
from delta.safety.approvals import ApprovalBroker, ApprovalRequest
from delta.safety.remote_guard import is_remote
from delta.system.logging import log


class LoginService:
    def __init__(self, store: LoginStore, audit: Any, approvals: ApprovalBroker | None,
                 captcha_solver: Any = None, guard: Any = None, proof: Any = None,
                 channels: dict[str, Any] | None = None, settle: Any = None) -> None:
        self.store = store
        self.audit = audit
        self.approvals = approvals
        self.solver = captcha_solver
        self.guard = guard
        self.proof = proof
        self.channels = channels if channels is not None else {}
        self.settle = settle
        self._task_grants: dict[str, set[str]] = {}  # task id -> login ids allowed for the rest of that task
        self.on_used: Callable[[Login, str], None] | None = None

    # --- permission ------------------------------------------------------------------------------------
    def forget_task(self, task_id: str) -> None:
        self._task_grants.pop(task_id, None)

    async def allowed(self, login: Login, task_id: str = "", why: str = "") -> tuple[bool, str]:
        """(may use it now, how it was decided). 'always' needs no answer; otherwise a person decides."""
        req = CURRENT_REQUEST.get()
        if self.guard is not None and req is not None and is_remote(req.channel) and self.guard.locked:
            return False, "remote control is locked"
        if login.permission == "always":
            return True, "always allowed for this site"
        if task_id and login.id in self._task_grants.get(task_id, set()):
            return True, "allowed for this task"
        if self.approvals is None:
            return False, "no one to ask"
        outcome = await self.approvals.request(ApprovalRequest(
            tool="sign_in", risk="external", task_id=task_id, for_task=bool(task_id),
            summary=f"Sign in to {login.name} ({login.origin}) with your saved login" + (f" ({why})" if why else ""),
            reason="saved logins ask before each use (Settings > Logins can change that for this site)"))
        if outcome.approved and outcome.for_task and task_id:
            self._task_grants.setdefault(task_id, set()).add(login.id)
        if not outcome.approved:
            return False, f"not approved ({outcome.by})"
        return True, "allowed for this task" if outcome.for_task else f"approved by {outcome.by}"

    # --- hand-offs (one-time codes, captchas the user solves) -------------------------------------------
    async def handoff(self, kind: str, host: str, text: str) -> bool:
        """Ask the user to finish a step on the PC (Approve = done, Deny or no answer = stop). Waits as long as any
        approval (agent.approval_timeout_s)."""
        if self.approvals is None:
            return False
        req = CURRENT_REQUEST.get()
        await self._send_picture(kind, host)
        outcome = await self.approvals.request(ApprovalRequest(
            tool="sign_in_handoff", risk="act", summary=text, reason=f"{host} needs a person ({kind})",
            task_id=req.id if req else ""))
        self.audit.record("login.handoff", site=host, kind=kind, done=outcome.approved, by=outcome.by)
        return outcome.approved

    async def _send_picture(self, kind: str, host: str) -> None:
        """A picture of the page (password boxes masked) to the phone the request came from, so the user knows what
        is waiting on the PC. The dashboard shows the approval card; the picture is also kept with the run's proof."""
        page = getattr(self, "_page", None)
        if page is None:
            return
        try:
            from delta.logins.signin import mask_secret_fields

            await mask_secret_fields(page)
            png = await page.screenshot(type="jpeg", quality=70, timeout=8000)
        except Exception:
            return
        import base64

        saved = ""
        if self.proof is not None:
            with contextlib.suppress(Exception):
                saved = self.proof.save_image(base64.b64encode(png).decode(), f"{kind}-{host}") or ""
        req = CURRENT_REQUEST.get()
        folder = getattr(self.proof, "dir", None)
        if req is None or not is_remote(req.channel) or not saved or folder is None:
            return
        ch = self.channels.get(req.channel)
        send = getattr(ch, "send_file", None)
        if callable(send):
            from pathlib import Path

            with contextlib.suppress(Exception):
                await send(req.user, Path(folder) / saved, f"{host} is waiting for you on the PC ({kind}).")

    async def _captcha(self, page: Any) -> tuple[bool, str]:
        if self.solver is None:
            return False, "captcha help is not set up"
        from delta.tools.captcha import PlaywrightCaptchaPage

        result = await self.solver.solve(PlaywrightCaptchaPage(page))
        return result.ok, result.output

    # --- the whole thing --------------------------------------------------------------------------------
    async def sign_in(self, ref: str, page: Any, task_id: str = "", why: str = "",
                      ask: bool = True) -> SignInResult:
        """Sign in with the saved login `ref` (its name, id or site) in `page`. Audited, whatever happens."""
        login = self.store.get(ref)
        if login is None:
            names = ", ".join(x.name for x in self.store.list()) or "none saved yet"
            return SignInResult("failed", f"no saved login called '{ref}' (saved: {names}). The user adds logins on "
                                          "the PC: Settings > Logins, or `delta logins add`.")
        if ask:
            ok, how = await self.allowed(login, task_id, why)
            if not ok:
                self._audit(login, task_id, "not_allowed", how)
                return SignInResult("needs_user", f"The user did not allow using the saved login for {login.name} "
                                                  f"({how}). Do not try another way.")
        else:
            how = "already decided"
        self._page = page
        try:
            engine = SignInEngine(self.handoff, self._captcha, self.settle)
            result = await engine.sign_in(page, login)
        except Exception as e:
            log.warning("login.failed", site=login.origin, error=type(e).__name__)
            result = SignInResult("failed", f"signing in to {login.name} failed ({type(e).__name__})")
        finally:
            self._page = None
        self.store.touch(login.id, result.outcome)
        self._audit(login, task_id, result.outcome, how)
        if self.on_used is not None:
            with contextlib.suppress(Exception):
                self.on_used(login, result.outcome)
        return result

    def _audit(self, login: Login, task_id: str, outcome: str, how: str) -> None:
        req = CURRENT_REQUEST.get()
        self.audit.record("login.use", login=login.id, site=login.origin, task=task_id, outcome=outcome,
                          permission=how, channel=req.channel if req else "")
