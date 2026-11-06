"""Lessons from self-heal: a recovery that worked on a site becomes a lesson candidate.

A lesson is written only in Delta's own fixed words, chosen by the failure type, plus the site's host name
(letters, digits, dots and dashes only). No text from the page, the model or a file goes into it, so a lesson
can never carry an instruction someone planted. It is still untrusted when the task read outside content and
the site cannot be traced to the user's request (the host could have come from a link on a page): untrusted
lessons are kept for the record and are never promoted.

A lesson is used (added to the "standing instructions" block of the prompt) only once the promotion gate
(delta.learning.gate) has seen it work in enough real runs or pass a scripted eval. The same recovery
failing on that site counts against it, and repeated failures retire it.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from delta.learning.gate import GateChange, PromotionGate
from delta.learning.store import LearningStore
from delta.memory.provenance import TRUSTED, UNTRUSTED, origin_of_taint, traced_to_user
from delta.selfheal.taxonomy import Failure

F = Failure
LESSON_TEXT = {
    F.STALE_ELEMENT: "On {site}, elements move after the page changes: re-read the page and act by name, "
                     "not by number.",
    F.ELEMENT_NOT_FOUND: "On {site}, controls are not always found by number: re-read the page and act by name.",
    F.TIMEOUT: "On {site}, pages can be slow: let the page settle before clicking again.",
    F.POPUP_OR_DIALOG: "On {site}, a banner can block clicks: close it with a reject or close button, never accept.",
}
_HOST = re.compile(r"^[a-z0-9.-]{3,80}$")


def site_of(steps: list[Any], url: str = "") -> str:
    """The host the task was on (from the tools' own evidence), cleaned to a safe word, or ''."""
    for raw in [url, *(str(getattr(s, "evidence", {}).get("url") or "") for s in reversed(steps))]:
        host = (urlparse(raw).hostname or "").lower().removeprefix("www.") if raw else ""
        if host and _HOST.match(host):
            return host
    return ""


class LessonBook:
    def __init__(self, store: LearningStore, gate: PromotionGate) -> None:
        self.store = store
        self.gate = gate

    def note(self, task_id: str, request: str, failure: Failure, site: str, recovered: bool,
             taint_source: str | None) -> GateChange | None:
        """A self-heal outcome on `site`. Recovered: create the lesson candidate or count a run for it.
        Not recovered: count a failure against an existing lesson for the same site and failure."""
        if failure not in LESSON_TEXT or not site or not self.gate.cfg.lessons or not self.gate.cfg.enabled:
            return None
        key = f"{failure}|{site}"
        existing = self.store.find("lesson", key)
        if existing is not None:
            if existing.status in ("candidate", "active"):
                return self.gate.record_run(f"L{existing.id}", recovered, task_id)
            return None  # rejected or retired: never re-learned
        if not recovered:
            return None
        name = site.split(".")[-2] if site.count(".") >= 1 else site
        trusted = (not taint_source or site in request.lower()
                   or (bool(re.search(r"[a-z]{3}", name)) and traced_to_user(name, request)))
        trust = TRUSTED if trusted else UNTRUSTED
        item = self.store.add("lesson", key, LESSON_TEXT[failure].format(site=site), status="candidate",
                              trust=trust, origin=origin_of_taint(taint_source) if not trusted else "agent_note",
                              task_id=task_id)
        what = "untrusted (the site came from outside content), kept for the record only" if not trusted else \
            "it will be used once it has worked a few more times"
        line = f"Learned a lesson candidate L{item.id} for {site}: {what}."
        self.gate._history(f"L{item.id}", "candidate", task_id, line)
        # The run that taught it is its first piece of evidence (and may promote it when the gate asks for 1).
        return self.gate.record_run(f"L{item.id}", True, task_id) or GateChange(f"L{item.id}", "candidate", line)
