"""Failure taxonomy: one type for every failed step, from the tool's result or the exception it raised.

The type is recorded in the task's event log (step_end data) and chooses the recovery playbook. The checks
are plain text patterns over the messages Delta's own tools, Playwright, Windows and the AI providers
produce, ordered so the most specific match wins. Anything unrecognised is `unknown` (no automatic fix;
the model sees the error as before).
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any


class Failure(StrEnum):
    ELEMENT_NOT_FOUND = "element_not_found"
    STALE_ELEMENT = "stale_element"
    TIMEOUT = "timeout"
    POPUP_OR_DIALOG = "popup_or_dialog"
    SIGN_IN_WALL = "sign_in_wall"
    CAPTCHA = "captcha"
    NAVIGATION_ERROR = "navigation_error"
    APP_NOT_RESPONDING = "app_not_responding"
    APP_CRASHED = "app_crashed"
    PERMISSION_DENIED = "permission_denied"
    FILE_LOCKED = "file_locked"
    MODEL_ERROR = "model_error"
    RATE_LIMIT = "rate_limit"
    BUDGET = "budget"
    UNKNOWN = "unknown"


F = Failure

# Delta's own refusals and the user's own "no". They are decisions, not breakdowns: never recovered, never
# retried, never worked around (the model is told so in the result itself, as before).
_REFUSAL = re.compile(r"^(BLOCKED|REFUSED|The user DENIED|STOPPED: )", re.I)

# Order matters: the first match wins.
PATTERNS: list[tuple[Failure, re.Pattern[str]]] = [
    (F.CAPTCHA, re.compile(r"captcha|hcaptcha|cf-turnstile|\bturnstile\b|verify (that )?you are (a )?human|"
                           r"are you a robot|unusual traffic", re.I)),
    (F.SIGN_IN_WALL, re.compile(r"sign[ -]?in to continue|please (sign|log) ?in|log ?in required|sign[ -]?in required|"
                                r"you need to (sign|log) ?in|you must (sign|log) ?in|session (has )?expired|"
                                r"http (error )?401|401 unauthori[sz]ed|\bunauthori[sz]ed\b", re.I)),
    (F.STALE_ELEMENT, re.compile(r"no longer on the page|stale element|element is not attached|"
                                 r"detached from (the )?(dom|document)|get fresh refs|ref is stale", re.I)),
    (F.POPUP_OR_DIALOG, re.compile(r"intercepts pointer events|another element would receive the click|"
                                   r"modal dialog|dialog (is )?(open|blocking)|cookie (banner|consent|notice)|"
                                   r"overlay (is )?blocking", re.I)),
    (F.FILE_LOCKED, re.compile(r"being used by another process|winerror 32|sharing violation|file is locked|"
                               r"locked by another|resource busy", re.I)),
    (F.PERMISSION_DENIED, re.compile(r"permission denied|access is denied|access denied|winerror 5\b|errno 13|"
                                     r"operation not permitted|requires elevation|winerror 740|"
                                     r"run as administrator", re.I)),
    (F.APP_CRASHED, re.compile(r"has (crashed|stopped working|exited unexpectedly)|"
                               r"target (page, context or browser )?(has been )?closed|"
                               r"browser has (been closed|disconnected)|browser stopped responding|"
                               r"websocket (is not open|closed)|process (has )?exited|no longer running|"
                               r"did not open its remote-control port", re.I)),
    (F.APP_NOT_RESPONDING, re.compile(r"not responding|stopped responding|did not respond|application is busy|"
                                      r"app is busy|page is not readable yet|did not open a window", re.I)),
    (F.NAVIGATION_ERROR, re.compile(r"net::err_|err_name_not_resolved|err_connection|navigation (failed|timeout)|"
                                    r"could not (open|load) (the )?page|name or service not known|"
                                    r"getaddrinfo failed|connection refused|http (error )?5\d\d|"
                                    r"\b(502|503|504) ", re.I)),
    (F.ELEMENT_NOT_FOUND, re.compile(r"nothing visible called|no visible (control|field|element)|"
                                     r"no (text )?field called|nothing called|no element (found|matches)|"
                                     r"could not find the browser's|is not a field you can type in|"
                                     r"no visible window|no browser window open|waiting for (locator|selector)|"
                                     r"element not found|resolved to 0 elements", re.I)),
    (F.TIMEOUT, re.compile(r"timed? ?out|timeout \d+ ?ms exceeded|timeouterror|took longer than", re.I)),
    (F.RATE_LIMIT, re.compile(r"rate.?limit|too many requests|\b429\b|usage limit is reached", re.I)),
]


def is_refusal(output: str, evidence: dict[str, Any] | None = None) -> bool:
    """A policy block, a refused field, the user's "no", or the user taking over the mouse."""
    ev = evidence or {}
    if ev.get("user_denied") or ev.get("needs_pin"):
        return True
    return bool(_REFUSAL.match((output or "").lstrip()))


def classify(tool: str, output: str = "", evidence: dict[str, Any] | None = None,
             exc: BaseException | None = None) -> Failure:
    """The failure type of a failed step (tool name, its result text and evidence, or an exception)."""
    ev = evidence or {}
    watchdog = ev.get("watchdog")
    if watchdog == "browser_dead":
        return F.APP_CRASHED
    if watchdog == "timeout":
        return F.TIMEOUT
    text = output or ""
    if exc is not None:
        text = f"{type(exc).__name__}: {exc} {text}"
        if isinstance(exc, PermissionError):
            return F.FILE_LOCKED if getattr(exc, "winerror", None) == 32 or _locked(text) else F.PERMISSION_DENIED
        if isinstance(exc, TimeoutError):
            return F.TIMEOUT
    if is_refusal(text, ev):
        # Recorded as a permission problem so the log shows why the step did not run; never recovered.
        return F.PERMISSION_DENIED
    for failure, pattern in PATTERNS:
        if pattern.search(text):
            return failure
    return F.UNKNOWN


def _locked(text: str) -> bool:
    return bool(PATTERNS[4][1].search(text))


def classify_llm_error(kind: str, message: str = "", kinds: list[str] | None = None) -> Failure:
    """The failure type of a model call that failed (LLMError.kind and AllProvidersFailedError.kinds)."""
    every = [k for k in (kinds or [kind]) if k]
    real = [k for k in every if k != "unreachable"] or every
    if real and all(k == "rate_limit" for k in real):
        return F.RATE_LIMIT
    if "quota" in real:
        return F.BUDGET
    return F.MODEL_ERROR


_OUTAGE_TEXT = re.compile(r"http 5\d\d|timed out|overloaded|unavailable|bad gateway", re.I)


def is_outage(kind: str, message: str = "") -> bool:
    """A provider failure that says nothing about the request: busy, down, or out of quota. Only these may
    use the opt-in `router.fallbacks`."""
    return kind in ("rate_limit", "unreachable", "quota") or (kind == "error" and bool(_OUTAGE_TEXT.search(message)))
