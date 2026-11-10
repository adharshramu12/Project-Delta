"""Answer grounding: every concrete claim in Delta's final answer must be backed by something a tool really
returned in this task (closes audit item M12).

Before a tool task is reported as done, the answer is scanned for claims Delta could have got wrong:

  path     a file or folder ("saved C:\\Users\\a\\notes.txt", "report.pdf")
  number   a figure ("26°C", "Rs 1,499", "45%"); compared by value, so 70,06,652 = 7,006,652
  url      a link or a site named as a source ("bbc.com", "https://...")
  quote    text in quotation marks ("the page says 'Mark done'")
  action   "I sent / saved / deleted / moved / scheduled / installed ..." (not when negated)

Each claim is matched against the task's evidence: the full output, evidence and arguments of every tool call
that worked (delta.core.observations), plus the user's own request (a number the user gave is not a claim
Delta has to prove). No AI call is made, so grounding costs no tokens.

Claims nothing backs are "unverified". Delta may then run at most `grounding.max_checks` extra READ-ONLY checks
(path_info for a file; a focused browser_read of the page the task was on for a number or a quote), only when
the policy allows them without asking. A check that proves a claim wrong (the file is not there) makes the
claim "contradicted": the attempt fails like a failed verification and the agent is told what was wrong.
Claims that stay unverified are listed to the user ("I could not confirm these from what I saw in this task")
in report mode, or fail the attempt in strict mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PATH, NUMBER, URL, QUOTE, ACTION = "path", "number", "url", "quote", "action"

_WIN_PATH = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s\"'<>|*?,;]+")
_POSIX_PATH = re.compile(r"(?<![\w:/])/(?:[\w.\-]+/)+[\w.\-]+\.\w{1,5}\b")
_FILE_EXT = ("txt|md|pdf|docx?|xlsx?|csv|json|py|png|jpe?g|gif|zip|pptx?|html?|log|ya?ml|ini|cfg|xml|mp3|mp4|wav|"
             "tex|rtf|odt|bat|ps1|js|ts|css|sql|db")
_BARE_FILE = re.compile(rf"(?<![\w\\/.-])[\w-]+(?:[.\-][\w-]+)*\.(?:{_FILE_EXT})\b", re.I)
_URL = re.compile(r"https?://[^\s)>\]\"'`]+", re.I)
_DOMAIN = re.compile(r"(?<![@\w.-])(?:[a-z0-9-]+\.)+(?:com|org|net|in|io|ai|dev|app|gov|edu|co|uk|info|news|tv)\b",
                     re.I)
_QUOTES = [re.compile(r'"([^"\n]{4,200})"'), re.compile(r"“([^”\n]{4,200})”"),
           re.compile(r"(?<![\w])'([^'\n]{4,200})'(?![\w])")]
_NUMBER = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?(?![\w])")
_UNIT_AFTER = re.compile(r"^\s?(%|°|º|degrees?\b|deg\b|[kKmMgG]?[bB]\b|kg\b|km\b|cm\b|mm\b|ms\b|mb\b|gb\b|rs\b|inr\b|"
                         r"usd\b|lakh|crore|million|billion|k\b)", re.I)
_CURRENCY_BEFORE = re.compile(r"(₹|\$|€|£|rs\.?|inr|usd)\s?$", re.I)
_ACTION = re.compile(r"\b(sent|emailed|messaged|saved|created|wrote|written|deleted|removed|recycled|moved|renamed|"
                     r"installed|uploaded|downloaded|scheduled|booked|played|posted|published|submitted|ordered)\b",
                     re.I)
_NEGATION = re.compile(r"(not|n't|never|unable|couldn|cannot|can't|failed|without|no)\W+(\w+\W+){0,3}$", re.I)

# Which tools back which action words. A tool whose name contains the word's stem counts too, and tools that
# can do anything (shell, code, clicks, MCP tools) back every action word: grounding then relies on the
# verifier and on the file checks for those.
ACTION_TOOLS: dict[str, set[str]] = {
    "sent": {"send_message", "send_file", "chat_for_me", "start_meeting", "schedule_meeting",
             "alert_emergency_contacts"},
    "saved": {"write_file", "edit_file", "make_document", "make_presentation", "make_spreadsheet", "write_post",
              "take_screenshot", "camera_photo", "remember", "schedule_task"},
    "deleted": {"delete_path", "delete_schedule", "edit_meeting"},
    "moved": {"move_path"},
    "scheduled": {"schedule_meeting", "schedule_task", "edit_meeting"},
    "played": {"play_youtube", "media_control"},
    "posted": {"prepare_post"},
}
_SYNONYM = {"emailed": "sent", "messaged": "sent", "created": "saved", "wrote": "saved", "written": "saved",
            "removed": "deleted", "recycled": "deleted", "renamed": "moved", "booked": "scheduled",
            "published": "posted", "submitted": "posted"}
_STEM = {"sent": "send", "saved": "write", "deleted": "delete", "moved": "move", "scheduled": "schedule",
         "played": "play", "posted": "post", "installed": "install", "uploaded": "upload", "downloaded": "download",
         "ordered": "order"}
GENERIC_ACTORS = {"run_shell", "run_python", "do_actions", "click_element", "click_at", "type_text", "press_keys",
                  "set_field", "browser_click", "browser_fill", "browser_press", "click_mark", "click_text"}


@dataclass
class Claim:
    kind: str
    text: str
    value: str = ""
    grounded: bool = False
    contradicted: bool = False
    note: str = ""


@dataclass
class GroundingReport:
    claims: list[Claim] = field(default_factory=list)
    checks: int = 0

    @property
    def unverified(self) -> list[Claim]:
        return [c for c in self.claims if not c.grounded and not c.contradicted]

    @property
    def contradicted(self) -> list[Claim]:
        return [c for c in self.claims if c.contradicted]

    @property
    def grounded(self) -> list[Claim]:
        return [c for c in self.claims if c.grounded]

    def footnote(self) -> str:
        """What the user reads under the answer when some claims could not be confirmed."""
        if not self.unverified:
            return ""
        items = ", ".join(_show(c) for c in self.unverified[:6])
        more = f" and {len(self.unverified) - 6} more" if len(self.unverified) > 6 else ""
        return f"Not confirmed: I could not confirm these from what I saw in this task: {items}{more}."

    def failure(self, strict: bool) -> str:
        """Why the attempt fails ('' when it does not)."""
        wrong = [f"{_show(c)} ({c.note})" if c.note else _show(c) for c in self.contradicted]
        if strict:
            wrong += [_show(c) for c in self.unverified]
        if not wrong:
            return ""
        return "the answer states things this task did not show or that are not true: " + "; ".join(wrong[:6])

    def summary(self) -> str:
        """One timeline line."""
        parts = [f"Checked {len(self.claims)} claim(s) in the answer: {len(self.grounded)} seen in this task"]
        if self.unverified:
            parts.append(f"{len(self.unverified)} not confirmed ({', '.join(_show(c) for c in self.unverified[:3])})")
        if self.contradicted:
            parts.append(f"{len(self.contradicted)} wrong ({', '.join(_show(c) for c in self.contradicted[:3])})")
        if self.checks:
            parts.append(f"{self.checks} extra read-only check(s)")
        return ", ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {"claims": len(self.claims), "grounded": len(self.grounded),
                "unverified": [c.text for c in self.unverified], "contradicted": [c.text for c in self.contradicted],
                "checks": self.checks}


def _show(c: Claim) -> str:
    return f"'{c.text}'" if c.kind != ACTION else f"that I {c.text} it"


def number_value(raw: str) -> str:
    """'70,06,652' / '7,006,652' / '7006652.0' -> '7006652'."""
    try:
        v = float(raw.replace(",", ""))
    except ValueError:
        return raw
    return str(int(v)) if v == int(v) else f"{v:g}"


def numbers_in(text: str) -> set[str]:
    return {number_value(m.group(0)) for m in _NUMBER.finditer(text)}


def _norm_path(p: str) -> str:
    return p.strip().rstrip(".,;:)").replace("/", "\\").lower()


def _basename(p: str) -> str:
    return re.split(r"[\\/]", p.rstrip("\\/"))[-1].lower()


def _host(url: str) -> str:
    h = re.sub(r"^https?://", "", url.lower()).split("/")[0].split(":")[0]
    return h.removeprefix("www.")


def extract_claims(answer: str) -> list[Claim]:
    """The concrete claims of an answer, in order, each once."""
    text = answer or ""
    claims: list[Claim] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, raw: str, value: str = "") -> None:
        key = (kind, (value or raw).lower())
        if key not in seen:
            seen.add(key)
            claims.append(Claim(kind, raw, value or raw))

    masked = text
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(".,;:)")
        add(URL, url, _host(url))
        masked = masked.replace(m.group(0), " ")
    for pat in (_WIN_PATH, _POSIX_PATH):
        for m in pat.finditer(masked):
            raw = m.group(0).rstrip(".,;:)")
            add(PATH, raw, _norm_path(raw))
            masked = masked.replace(m.group(0), " ")
    for m in _BARE_FILE.finditer(masked):
        add(PATH, m.group(0), m.group(0).lower())
        masked = masked.replace(m.group(0), " ")
    for m in _DOMAIN.finditer(masked):
        add(URL, m.group(0), _host(m.group(0)))
        masked = masked.replace(m.group(0), " ")
    for pat in _QUOTES:
        for m in pat.finditer(masked):
            inner = m.group(1).strip()
            if re.search(r"[A-Za-z]{2}", inner):
                add(QUOTE, inner, " ".join(inner.lower().split()))
    for m in _NUMBER.finditer(masked):
        raw = m.group(0)
        after = masked[m.end():m.end() + 10]
        before = masked[max(0, m.start() - 5):m.start()]
        if re.match(r"^[:/\-]\d", after) or re.search(r"\d[:/\-]$", before):
            continue  # a time or a date: compared as text by the quote rule if quoted, otherwise left alone
        value = number_value(raw)
        unit = bool(_UNIT_AFTER.match(after)) or bool(_CURRENCY_BEFORE.search(before))
        try:
            small = float(value) < 10
        except ValueError:
            small = False
        if small and not unit:
            continue  # "2 files", "step 1": too common to be worth a claim
        add(NUMBER, raw + (after[:2].strip() if after[:1] in "%°" else ""), value)
    for m in _ACTION.finditer(text):
        before = text[max(0, m.start() - 40):m.start()]
        if _NEGATION.search(before):
            continue
        word = m.group(1).lower()
        add(ACTION, word, _SYNONYM.get(word, word))
    return claims


def ground(claims: list[Claim], corpus: str, request: str, ok_tools: set[str], known: str = "",
           missing: set[str] | None = None) -> None:
    """Mark each claim grounded when the task's evidence (or the user's request) backs it. `missing`: paths a
    check in this task already found not to exist (normalised: backslashes, lower case)."""
    hay = f"{corpus}\n{known}".lower()
    hay_paths = hay.replace("\\\\", "\\").replace("/", "\\")
    hay_words = " ".join(re.sub(r"\s+", " ", hay).split())
    req = request.lower()
    nums = numbers_in(corpus + "\n" + known) | numbers_in(request)
    for c in claims:
        if c.grounded or c.contradicted:
            continue
        if c.kind == PATH:
            if missing and any(m == c.value or (len(c.value) > 3 and m.endswith("\\" + c.value)) for m in missing):
                c.contradicted, c.note = True, "it does not exist"
                continue
            base = _basename(c.value)
            c.grounded = c.value in hay_paths or (bool(base) and base in hay_paths) or c.value in req
        elif c.kind == NUMBER:
            c.grounded = c.value in nums
        elif c.kind == URL:
            c.grounded = bool(c.value) and (c.value in hay or c.value in req)
        elif c.kind == QUOTE:
            c.grounded = c.value in hay_words or c.value in " ".join(req.split())
        elif c.kind == ACTION:
            c.grounded = action_backed(c.value, ok_tools)


def action_backed(word: str, ok_tools: set[str]) -> bool:
    if ok_tools & GENERIC_ACTORS:
        return True
    if any(t.startswith("mcp") or "__" in t for t in ok_tools):
        return True  # an MCP or add-on tool: Delta cannot tell what it did
    if ok_tools & ACTION_TOOLS.get(word, set()):
        return True
    stem = _STEM.get(word, word[:4])
    return any(stem in t for t in ok_tools)


def check_for(claim: Claim, failed_args: list[dict[str, Any]], last_page: bool) -> tuple[str, dict[str, Any]] | None:
    """The one read-only call that could confirm (or disprove) an unverified claim, if there is one."""
    if claim.kind == PATH:
        raw = claim.text
        if not re.match(r"^[A-Za-z]:[\\/]|^/", raw):  # a bare name: the full path a step tried to use
            name = raw.lower()
            raw = next((str(v) for a in failed_args for v in a.values()
                        if isinstance(v, str) and _basename(v) == name), "")
            if not raw:
                return None
        return "path_info", {"path": raw}
    if claim.kind in (NUMBER, QUOTE) and last_page:
        return "browser_read", {"focus": claim.text[:60]}
    return None


def apply_check(claim: Claim, tool: str, output: str, evidence: dict[str, Any], ok: bool) -> None:
    """What the extra check showed."""
    if not ok:
        return
    if tool == "path_info":
        if evidence.get("exists") is False and re.search(r"\.\w{1,5}$", claim.text):
            # Only a complete-looking file name is proven wrong; a path cut at a space stays unverified.
            claim.contradicted, claim.note = True, "it does not exist"
        elif evidence.get("exists") is True:
            claim.grounded = True
        return
    hay = output.lower()
    if claim.kind == NUMBER:
        claim.grounded = claim.value in numbers_in(output)
    elif claim.kind == QUOTE:
        claim.grounded = claim.value in " ".join(hay.split())
