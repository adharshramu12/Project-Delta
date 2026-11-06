"""The agent loop: plan -> act -> observe -> verify -> (reflect + retry) -> report."""

from __future__ import annotations

import asyncio
import contextlib
import json
import platform
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from delta import CREDIT
from delta.config import Settings, known_folder, save_dir
from delta.core.academy import AcademyNote
from delta.core.clarify import ask_first
from delta.core.events import EventBus
from delta.core.intents import match_intent, small_talk
from delta.core.planner import make_plan
from delta.core.playbooks import playbooks_for
from delta.core.proof import ProofRecorder
from delta.core.questions import parse_questions
from delta.core.replay import Recipe, fill, match_slots
from delta.core.state import (
    CURRENT_REQUEST,
    TASK_CRASHED,
    Plan,
    StepRecord,
    TaskRequest,
    TaskResult,
    Verification,
)
from delta.core.style import STYLE_BLOCK, slop_check
from delta.core.verifier import verify
from delta.identity import identity_answer, is_identity_question
from delta.learning.corrections import learn_corrections, standing_instructions
from delta.learning.store import LearningStore
from delta.llm import usage
from delta.llm.capabilities import abilities_text, brain_for
from delta.llm.router import ModelRouter
from delta.llm.schemas import AllProvidersFailedError, LLMError, Message
from delta.memory.distill import distill
from delta.memory.extractor import extract_facts
from delta.memory.keywords import keywords
from delta.memory.provenance import fact_trust, recipe_trust, traced_to_user
from delta.memory.skills import Skill, SkillStore
from delta.memory.store import MemoryStore
from delta.safety.audit import AuditLog
from delta.system.governor import Governor
from delta.system.hardware_guard import Health
from delta.system.logging import log
from delta.tools import snapshot
from delta.tools.registry import ToolExecutor

ACTOR_PROMPT = """You are {name}, {owner}'s personal AI agent with access to their Windows PC
({os}). Today is {now}. Home folder: {home}.
{credit}
Desktop: {desktop}   Documents: {documents}
SAVE FOLDER: {save_dir}  (the user opens it from the "Delta files" item on their Desktop)
Save every file you create in the SAVE FOLDER unless the user names another folder.
Always use full paths (Save dialogs otherwise fall back to their own default, e.g. OneDrive).

Work through the plan using tools, checking results as you go. Work like an expert: the fewest steps
that get a verified result. Every reply costs the user time and tokens.
Rules:
- Independent read-only calls (several reads, a search plus a file check) go together in ONE reply.
- Files and folders: list_dir (sizes, sort, limit, recursive), path_info, read_file answer most
  questions in one call. search_files finds text inside files; edit_file changes one exact part.
- The user may be away, writing from their phone: "send me <file>" means find it (search_files/list_dir)
  and send_file it to them; a folder is zipped. Say what you sent.
- PC controls: set_volume, set_brightness; list_windows + close_window to close apps; take_screenshot /
  camera_photo ("who is using my laptop?") then send_file the picture to a user writing from their phone.
- run_python does in ONE step what would take many: maths, bulk renames, CSV/Excel/JSON work. Prefer
  it (or run_shell) over long chains of small actions.
- Prefer the simplest tool. Read before you write. After changing something, check it
  (path_info, list_running_apps, read_file) so there is evidence.
- If a tool says BLOCKED or DENIED, do not retry it. Find an allowed way or explain.
- If you are unsure about a fact, use web_search; cite URLs you used.
- Desktop apps: read_window first (fast, free). Fill boxes with set_field (more reliable than
  typing; dialogs like 'Save as' can be targeted by title). Click by name with click_element; use
  click_at / look_at_screen only when read_window cannot see it. Re-read after acting.
- Websites: use the browser freely (browser_search, browser_open, browser_read, browser_click,
  browser_fill with submit=true for search boxes, browser_scroll, browser_back).
  Every browser action already returns the page text that matters for this task (AFTER browser_read):
  read that first. Call browser_read only for a different part, with a new focus (focus="born").
  When the page listing numbers its elements ([12] button "Sign in"), act by number:
  browser_click ref=12, browser_fill ref=7 value="…". Use the numbers from the LATEST listing; if a
  ref is reported stale, the page changed: read it again. Without numbers, click/fill by name.
  {diff_help}
  When search results already show the page that answers (an /about or /contact page, a product
  page), open THAT page directly, not the home page. Answer from text read on the page, never from
  a search snippet alone.
  To search INSIDE a site (YouTube, Amazon, Wikipedia…), type into its box with
  browser_fill field="Search" value="…" submit=true. Clicking a Search button does not type
  anything. Opening the site's search URL directly also works, e.g.
  https://www.youtube.com/results?search_query=… . If the same action fails twice, try another way.
  Clicking Send/Buy/Delete, submitting forms and anything with passwords or payments asks the
  user first.
- If a desktop tool says STOPPED, the user took over: stop and report.
- Songs/videos: to prove something is playing, call media_status (Windows' own Playing/Paused,
  title and position: exact, instant, free). A screenshot cannot show playback. If an ad
  plays first, click its Skip button when shown, then check media_status again.
- Never ask for or handle passwords, card numbers or API keys. If a site needs the user signed in and they saved a
  login (list_logins), call sign_in(site): Delta types it on the exact site; you never see it.
- Messages in WhatsApp, Telegram or Slack: ONLY with send_message (it checks the right chat
  and the text before sending). Never click, type and press Enter in those apps yourself; if
  send_message fails, tell the user why instead.
- Signed in to a site with ANY account counts as signed in: carry on and name the account in your
  final summary. Only stop for sign-in if the page asks to log in, or the user named an account.
- KEEP GOING until every success criterion is met. Every reply WITHOUT a tool call must start with:
    DONE: <short, friendly summary for the user>  (all criteria met, you have seen the evidence)
    NEED_USER: <the one question or blocker>    (only if you truly cannot continue: not signed
                                                in, a choice only the user can make, missing info)
  Any other reply is treated as a progress note and you will be asked to continue.
- NEVER GUESS between several matches. If more than one file, contact, app or meaning fits (two
  "daddy" photos, three chats named Inka), or the request is unclear, stop and ask BEFORE acting.
  Sending the wrong file or message is worse than asking.
- Before a big open-ended task (research, a report, a plan, writing) whose key details are missing, ask
  once, up to 4 short questions, each with choices the user taps (they can also type their own):
    NEED_USER: <one line on why you ask>
    <Question 1?>
    - [ ] <tick box: the user may pick several>
    - [ ] <tick box>
    <Question 2?>
    - <pick one>
    - <pick one>
  Ask only what changes the result; never ask what you can find out yourself.
- Email: draft it in the user's voice from what you know (recipient, thread, past tone), then call send_email
  with the full draft. It shows the user the draft and recipients and sends ONLY after they press Approve.
{style}

GOAL: {goal}
PLAN:
{steps}
SUCCESS CRITERIA:
{criteria}
{skill}
What you know about the user:
{memory}"""


def friendly_model_error(e: LLMError) -> str:
    """What a non-technical user should read when no model could answer."""
    if isinstance(e, AllProvidersFailedError):
        if e.only_rate_limited:
            return ("The AI service is busy right now (free-tier usage limit). Please try again in a minute. "
                    "Adding a second provider key (e.g. NVIDIA NIM) or a local model avoids this.")
        if not e.kinds:
            return ("No AI model is set up yet. Add a free Groq key with `delta secrets set groq_api_key`, "
                    "sign in to Claude Code or Codex to use your existing plan, "
                    "or start Ollama / LM Studio to run a model on this PC.")
    return f"I couldn't reach an AI model to finish this. Details: {e}"


# Free tiers cap a single request at ~7-8k tokens; ~4 chars per token, leaving room for tools.
CONTEXT_BUDGET_CHARS = 18000
OLD_RESULT_CHARS = 500


def _split_listing(content: str) -> tuple[str, str, str, str] | None:
    """(before, listing, after, kind) of a tool result holding a browser page listing."""
    found = snapshot.find_listing(content)
    if found is None:
        return None
    a, b, kind = found
    return content[:a], content[a:b], content[b:], kind


def _head(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[older result trimmed to save space]"


def compact_history(messages: list[Message], budget: int, keep_last: int = 2) -> None:
    """Keep long multi-step tasks within the model's request limit.

    The newest tool results stay in full (the model is acting on them); older ones are cut to a
    short head, since their key facts are already in the model's own later messages. Browser page
    listings are handled apart: a listing superseded by a newer full one becomes a one-line stub
    (its refs are out of date), while the latest full listing and the diffs after it are kept whole,
    since together they are the model's current view of the page. If still over budget, those go
    to stubs too (oldest first), then the newest results are trimmed. Edits `messages` in place;
    never drops a message, so tool-call ids stay paired (and CLI sessions, which count messages,
    keep working)."""
    shots = [i for i, m in enumerate(messages) if isinstance(m.get("content"), list)]
    for i in shots[:-1]:  # only the newest screenshot is worth its ~1k tokens
        messages[i]["content"] = "[earlier screenshot removed to save tokens]"
    tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    parts = {i: sp for i in tool_idx if messages[i].get("name") in BROWSER_ACTIONS
             and (sp := _split_listing(str(messages[i].get("content", ""))))}
    base = max((i for i, sp in parts.items() if sp[3] == "full"), default=-1)
    protected = {i for i in parts if i >= base} if base >= 0 else set(parts)

    def stub(i: int) -> None:
        before, listing, after, kind = parts.pop(i)
        what = "page listing" if kind == "full" else "page changes"
        messages[i]["content"] = (f"{before}[older {what} for {snapshot.page_line(listing)} superseded; "
                                  f"use the latest listing]{after}")

    for i in [i for i in parts if i not in protected]:
        stub(i)
    recent = set(tool_idx[-keep_last:]) if keep_last else set()
    for i in tool_idx:
        if i in recent:
            continue
        if i in parts:  # keep the current listing whole; cut only the text around it
            before, listing, after, _ = parts[i]
            room = max(0, OLD_RESULT_CHARS - len(before))
            if len(before) + len(after) > OLD_RESULT_CHARS:
                messages[i]["content"] = before[:OLD_RESULT_CHARS] + listing + _head(after, room)
                parts[i] = (before[:OLD_RESULT_CHARS], listing, _head(after, room), parts[i][3])
            continue
        content = str(messages[i].get("content", ""))
        if len(content) > OLD_RESULT_CHARS:
            messages[i]["content"] = content[:OLD_RESULT_CHARS] + "\n…[older result trimmed to save space]"

    def size() -> int:
        return sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "")) for m in messages)

    for i in sorted(i for i in parts if i not in recent):  # still too big: older listings go too
        if size() <= budget:
            break
        stub(i)
        content = str(messages[i].get("content", ""))
        if len(content) > OLD_RESULT_CHARS:
            messages[i]["content"] = content[:OLD_RESULT_CHARS] + "\n…[older result trimmed to save space]"
    over = size() - budget
    for i in reversed(tool_idx[-keep_last:] if keep_last else []):
        if over <= 0:
            break
        content = str(messages[i].get("content", ""))
        keep = max(OLD_RESULT_CHARS, len(content) - over - 100)
        if keep < len(content):
            messages[i]["content"] = content[:keep] + "\n…[trimmed to fit the model's limit]"
            over = size() - budget


_ELEMENT_LINE = re.compile(r"^(?:[+~] )?(\[\d+\] .*)$")


def held_element_lines(messages: list[Message]) -> set[str]:
    """Element lines ([12] button "Sign in") of the page listing the model currently holds: the
    latest full listing plus the diffs after it."""
    held: set[str] = set()
    for m in messages:
        if m.get("role") != "tool" or m.get("name") not in BROWSER_ACTIONS:
            continue
        found = snapshot.find_listing(str(m.get("content", "")))
        if found is None:
            continue
        a, b, kind = found
        if kind == "full":
            held = set()
        for line in str(m["content"])[a:b].splitlines():
            if (mm := _ELEMENT_LINE.match(line)) and not line.startswith("- "):
                held.add(mm.group(1))
    return held


UNCHANGED_TEXT = "(page text unchanged since the previous step)"


def same_text_as_before(text: str, url: str, prev: tuple[str, str] | None) -> tuple[str, tuple[str, str] | None]:
    """(text to send, what the next step may compare with). The same page text as the previous tool
    result's AFTER, on the same page, is sent as a one-line note. Only the immediately previous
    result counts, and only when it was sent in full: that one is still whole in the history
    (compact_history keeps the newest results), so nothing the model needs is lost."""
    if prev is not None and url and snapshot.same_page(prev[0], url) and text == prev[1]:
        return UNCHANGED_TEXT, None
    return text, (url, text)


def drop_held_lines(text: str, held: set[str]) -> str:
    """An observation without the element lines the model already holds in its page listing."""
    if not held:
        return text
    kept = [ln for ln in text.splitlines() if ln.strip() not in held]
    return "\n".join(kept)


# Said when the request comes from a phone: the user cannot see this PC, so opening a picture or file on
# its screen (a screenshot typed into the Run box) helps nobody. Results go into their chat.
REMOTE_CHANNELS = {"telegram", "slack"}
REMOTE_NOTE = """

THE USER IS NOT AT THIS PC: they are writing from their phone in {channel} and cannot see its screen.
- Never open files, pictures or windows on the PC for them to look at.
- Pictures and files they ask for go to them with send_file (take_screenshot and camera_photo already
  send what they take). Answers go in your final reply, in short, plain words for a phone screen."""

DONE, NEED_USER = "DONE:", "NEED_USER:"
# An answer to the token budget question that means "continue" (the button sends "Keep going").
KEEP_GOING = re.compile(r"^\W*(?:(?:yes|ok|okay|sure)\W+)?(?:please\W+)?(?:keep going|continue|go on|carry on)\b",
                        re.I)


MAX_NUDGES = 3  # small models may never use the markers: accept their answer after this many
CONTINUE_NUDGE = ("Not finished yet: that reply had no tool call and did not start with DONE: or NEED_USER:. "
                  "Continue with the next unmet success criterion now (call a tool). Reply DONE: only when "
                  "every criterion is met, or NEED_USER: if only the user can unblock you.")

# A model judging by its own sandbox may stop with "this environment doesn't provide desktop tools"
# while Delta's tools are right there. That is never a real blocker: say which tools it has.
_NO_TOOLS = re.compile(r"(environment|sandbox|don['’]?t|doesn['’]?t|do not|does not|no|not|unable|cannot|can['’]?t)\b"
                       r"[^.]{0,60}\b(tools?|desktop[- ]control|access to (the |your )?(pc|computer|screen|browser))",
                       re.I)


def claims_no_tools(text: str) -> bool:
    return bool(_NO_TOOLS.search(text))


LOOKING = {"read_window", "list_windows", "path_info", "look_at_screen", "media_status", "look_with_marks",
           "read_screen_text"}


def _jpeg_tokens(b64: str) -> int:
    """Estimated image tokens of a base64 JPEG (its size read from the header; 0 if unreadable)."""
    try:
        import base64
        import io

        from PIL import Image

        from delta.perception.images import image_tokens

        with Image.open(io.BytesIO(base64.b64decode(b64))) as img:
            return image_tokens(*img.size)
    except Exception:
        return 0


def stuck_in_loop(steps: list[StepRecord], repeats: int = 3) -> bool:
    """The same action with the same arguments `repeats` times in a row (reads in between ignored,
    since checking the page after each try is normal)."""
    acts = [(s.tool, json.dumps(s.args, sort_keys=True)) for s in steps
            if not s.tool.endswith("_read") and s.tool not in LOOKING]
    return len(acts) >= repeats and len(set(acts[-repeats:])) == 1


# Actions that change what is on screen. After each one the agent sees the result (AFTER: ...),
# like a person glancing at the screen after clicking, instead of acting blind.
DESKTOP_ACTIONS = {"click_element", "click_at", "do_actions", "type_text", "press_keys", "set_field", "scroll",
                   "focus_window", "close_window", "open_app", "open_target"}
BROWSER_ACTIONS = {"browser_open", "browser_search", "browser_click", "browser_fill", "browser_press",
                   "browser_scroll", "browser_back", "browser_select", "browser_check", "browser_upload",
                   "browser_hover", "browser_wait", "browser_tabs", "sign_in"}
TEXT_ACTIONS = {"type_text", "set_field", "press_keys"}
PIXEL_ACTIONS = {"click_at", "scroll", "do_actions"}  # aimed by coordinates: only a screenshot shows what happened
OBSERVE_CHARS = 1500
OBSERVE_WIDTH = 1024  # step screenshots: enough to read buttons, about 1k image tokens
FINAL_WIDTH = 1280    # the proof screenshot the verifier judges

# Words that say how to get somewhere, not what to look for on the page.
_WEB_WORDS = {"open", "website", "site", "page", "browser", "chrome", "edge", "tell", "search", "google",
              "scroll", "click", "show", "find", "read", "visit", "link", "url", "platform", "check", "look",
              "there", "about", "info", "details", "information", "play", "watch", "listen"}


def task_focus(request: str, evidence: dict[str, Any]) -> str | None:
    """The request's content words the page does not already name in its title or address: what to
    look for on it ("founder" for "open arva talks academy website and tell me the founder")."""
    seen = f"{evidence.get('title', '')} {evidence.get('url', '')}".lower()
    words = [w for w in keywords(request) if w not in _WEB_WORDS and w[:5] not in seen]
    return " ".join(words[:6]) or None


def read_page_last(steps: list[StepRecord]) -> bool:
    """The last browser action was followed by a successful page read. That text is the better proof
    for a web page, so the check needs no screenshot: no image tokens, and no detour to a model that
    can see (the main AI may not)."""
    last_action = max((i for i, s in enumerate(steps) if s.tool in BROWSER_ACTIONS), default=-1)
    return last_action >= 0 and any(s.tool == "browser_read" and s.ok for s in steps[last_action + 1:])


def proof_target(steps: list[StepRecord]) -> str | None:
    """Title of the window the task last worked in: "" = whole screen, None = no screen work."""
    for s in reversed(steps):
        if s.tool in BROWSER_ACTIONS or s.tool == "browser_read":
            return str(s.evidence.get("title") or s.evidence.get("browser") or "")
        if s.tool in DESKTOP_ACTIONS or s.tool in {"read_window", "look_at_screen"}:
            return str(s.args.get("window") or s.evidence.get("window") or s.evidence.get("foreground_window") or "")
    return None


# Read-only helpers every task may need for checking its own work.
ALWAYS_TOOLS = {"path_info", "read_file", "list_dir", "search_files", "web_search", "recall",
                "list_windows", "read_window", "focus_window", "look_at_screen", "browser_read",
                "media_status", "do_actions", "scroll",
                "find_app_tool", "use_app_tool"}  # connected apps: only offered while an app is connected
BROWSER_TOOLS = BROWSER_ACTIONS | {"browser_read"}
ATTACHMENT_TOOLS = {"read_attachment", "look_at_attachment"}
SCREEN_TOOLS = {"click_element", "click_at", "type_text", "press_keys", "set_field"}
# Set-of-marks and OCR (delta.perception): sent with the screen tools, and only when they are usable here.
PERCEPTION_TOOLS = {"look_with_marks", "click_mark", "read_screen_text", "click_text"}
WEBSITE = re.compile(r"https?://|www\.|\b[\w-]+\.(com|org|net|in|io|ai|dev|app)\b|\b(website|web ?site|web ?page|"
                     r"browser|chrome|edge|youtube|gmail|notebooklm|google (docs|drive|sheets|slides)|gemini|"
                     r"chatgpt|amazon|flipkart|linkedin|twitter|instagram|facebook|wikipedia)\b", re.I)


class Orchestrator:
    engine: Any = None  # TaskEngine (delta.engine), set by bootstrap: task states in tasks.db. None = not recorded
    healer: Any = None  # Healer (delta.selfheal), set by bootstrap: recovery playbooks. None = no self-heal
    ladder: Any = None  # ModelLadder (delta.llm.ladder): opt-in cheap-first models. None or empty = off
    perception: Any = None  # Perception (delta.perception): set-of-marks, OCR, the perception cache. None = off
    helpers: Any = None  # HelperSupervisor (delta.helpers): parallel read-only helpers. None or disabled = off
    gate: Any = None  # PromotionGate (delta.learning.gate): lessons and repaired recipes. None = no candidates
    lessons: Any = None  # LessonBook (delta.learning.lessons): self-heal recoveries -> lesson candidates. None = off
    connectors: Any = None  # AppTools (delta.connectors.tools): connected apps' one-line index for the planner
    attachments: Any = None  # AttachmentService (delta.attachments): files sent with a request. None = off

    def __init__(self, settings: Settings, router: ModelRouter, executor: ToolExecutor,
                 memory: MemoryStore | None, governor: Governor | None, events: EventBus,
                 audit: AuditLog, skills: SkillStore | None = None,
                 learning: LearningStore | None = None, proof: ProofRecorder | None = None) -> None:
        self.s = settings
        self.router = router
        self.executor = executor
        self.memory = memory
        self.governor = governor
        self.events = events
        self.audit = audit
        self.skills = skills
        self.learning = learning
        self.proof = proof
        self._planned: set[str] = set()  # tasks whose planner call also did the fact learning
        self._learning: set[asyncio.Task[None]] = set()
        # task id -> (first outside source, text read from outside), kept from the end of a task until its
        # learning step has run: the executor forgets both when the task ends.
        self._after: dict[str, tuple[str | None, str | None]] = {}
        # Token budget (agent.token_budget). task id -> (tokens already used before this task, its limit), and
        # chat -> (used, limit) for a task that stopped at its limit, so "Keep going" in the same chat continues
        # with an explicitly extended limit (and the earlier usage still counted) instead of a silent fresh one.
        self._budget: dict[str, tuple[int, int]] = {}
        self._budget_stops: dict[str, tuple[int, int]] = {}
        self.browsers: Any = None  # BrowserHub (delta.tools.browser_switch), set by bootstrap
        # The once-per-conversation academy line for people who ask to learn AI (delta.core.academy).
        self.academy = AcademyNote(getattr(getattr(settings, "academy", None), "enabled", True))

    def _finish_proof(self, result: TaskResult) -> None:
        """Close the run record; a proof folder that cannot be written must not fail the task itself."""
        if not self.proof:
            return
        try:
            self.proof.finish(result)
        except Exception:
            log.exception("proof.finish_failed", task=result.task_id)
            self.proof.dir = None

    async def drain(self) -> None:
        """Wait for background learning (used at shutdown and in tests)."""
        if self._learning:
            await asyncio.gather(*self._learning, return_exceptions=True)

    async def run(self, req: TaskRequest) -> TaskResult:
        """Always ends with a result, never with a task left "running": any failure (a bug, a full disk, a
        broken tool) becomes a failed task with a safe message, the run record is closed and the UI told.
        A cancel (the user pressed Stop) is cleaned up the same way and then re-raised."""
        self.audit.record("task.start", task=req.id, channel=req.channel, text=req.text)
        started = time.monotonic()
        current = CURRENT_REQUEST.set(req)
        # The browser this task uses is fixed now: a switch saved while it runs applies to the next task.
        pinned = self.browsers.begin_task(req.id, req.text) if self.browsers is not None else None
        try:
            await self.events.publish("task.started", task_id=req.id, text=req.text)
            if self.proof:
                self.proof.start(req.id, req.text)
            self.router.reset_meter()
            self._begin_budget(req)
            result = await self._run(req)
        except LLMError as e:
            result = TaskResult(req.id, "failed", friendly_model_error(e))
        except asyncio.CancelledError:
            # Stopped by the user: close the run record and tell the UI, then let the cancel finish.
            self.audit.record("task.end", task=req.id, status="cancelled", attempts=0)
            self._finish_proof(TaskResult(req.id, "cancelled", "Stopped by the user."))
            if self.engine is not None:
                self.engine.finish(req.id, TaskResult(req.id, "cancelled", "Stopped by the user."), req)
            with contextlib.suppress(Exception):
                await self.events.publish("task.finished", task_id=req.id, status="cancelled", answer="Stopped.")
            raise
        except Exception as e:
            # The details go to the log and the audit; the user gets a plain message, never a traceback.
            log.exception("task.crashed", task=req.id)
            self.audit.record("task.error", task=req.id, error=type(e).__name__)
            result = TaskResult(req.id, "failed", TASK_CRASHED)
        finally:
            if pinned is not None:
                self.browsers.end_task(req.id, pinned)
            CURRENT_REQUEST.reset(current)
            self._budget.pop(req.id, None)
            self._keep_provenance(req.id)
            self.executor.forget_task(req.id)
            for helper in (self.healer, self.ladder):
                if helper is not None:
                    helper.forget(req.id)
            if self.perception is not None:
                from delta.perception.marks import BOOK

                self.perception.cache.forget(req.id)
                BOOK.forget(req.id)
            try:
                await self.router.release()  # free long-lived CLI sessions (RAM) between tasks
            except Exception:
                log.exception("router.release_failed", task=req.id)
        if result.status in ("done", "needs_user") and not result.fast and result.answer:
            # One writing style everywhere (delta.core.style): long dashes, AI phrases, US spellings, stray emojis
            # and any claim to be human are fixed in code, at no token cost.
            result.answer = slop_check(result.answer, "chat", req.text).text
        if req.attachment_notes and result.status != "cancelled":
            # Plain words the model might leave out: a picture that was not seen, pages that could not be read.
            notes = [n for n in dict.fromkeys(req.attachment_notes) if n not in result.answer]
            if notes:
                result.answer = (result.answer.rstrip() + "\n\n" + "\n".join(notes)).strip()
        result.tainted_by = (self._after.get(req.id) or (None, None))[0] or ""
        result.tokens = dict(self.router.meter)
        result.usage = usage.summary(result.tokens)
        if models := dict(getattr(self.router, "model_calls", None) or {}):
            result.usage["models"] = models  # which model answered how many calls (the token meter)
        result.wall_s = round(time.monotonic() - started, 2)
        result.fallback = self.router.fallback_note()
        self._finish_proof(result)
        if self.engine is not None:  # the task's final state in tasks.db (never raises)
            self.engine.finish(req.id, result, req)
        self.audit.record("task.end", task=req.id, status=result.status, attempts=result.attempts)
        try:
            await self.events.publish("task.finished", task_id=req.id, status=result.status, answer=result.answer)
        except Exception:  # a broken listener must not lose the answer the caller is waiting for
            log.exception("task.finished_publish_failed", task=req.id)
        # Learning happens after the answer is delivered, so the user never waits for it.
        t = asyncio.create_task(self._learn(req, result))
        self._learning.add(t)
        t.add_done_callback(self._learning.discard)
        return result

    async def _run(self, req: TaskRequest) -> TaskResult:
        if self.engine is not None:  # queued -> planning; tool steps move it to acting (delta.engine)
            self.engine.begin(req, str(self.executor.policy.b.autonomy))
        # Files sent with the request: read on this PC, a compact extract goes with it (delta.attachments). A
        # message with files is never small talk, an instant command or a learned recipe: it is about the files.
        files = await self._attach(req)
        if not files and (reply := small_talk(req.text, self.s.agent.owner_name)):  # "hi", "thanks": 0 tokens
            return TaskResult(req.id, "done", reply, fast=True)
        if not files and is_identity_question(req.text):  # "who made you?": 0 tokens, always the same answer
            return TaskResult(req.id, "done", identity_answer(self.s.agent.name), fast=True)
        # Never guess what only the user can decide (e.g. a meeting's day/time): ask first, 0 tokens.
        if not files and (question := ask_first(req.text)):
            return TaskResult(req.id, "needs_user", question.text, options=question.options,
                              asked_about=req.text)
        steps: list[StepRecord] = []
        # Instant commands ("pause the music", "start a Google Meet"): 0 tokens, no planner call.
        if not files and (intent := match_intent(req.text, set(self.executor.registry.names()))):
            quick = Plan(True, goal=req.text, success_criteria=[intent.check],
                         direct={"tool": intent.tool, "args": intent.args})
            await self.events.publish("task.plan", task_id=req.id, plan=quick.__dict__)
            if done := await self._direct(req, quick, steps):
                return done
        if not files and not steps and (done := await self._replay(req, steps)):
            return done
        memory_ctx = await self._memory_context(req)
        if self.learning and (rules := standing_instructions(self.learning)):
            memory_ctx = (f"STANDING INSTRUCTIONS FROM THE USER (always follow these):\n{rules}\n\n"
                          + (memory_ctx or ""))
        known = self.skills.relevant(req.text) if self.skills else []
        environment = (f"Now: {datetime.now():%A %d %B %Y %H:%M} (local time). "
                       f"Save folder for new files: {save_dir(self.s.files)}. Desktop: {known_folder('Desktop')}. "
                       f"Documents: {known_folder('Personal')}. Home: {Path.home()}.")
        # Boot the acting model (Claude Code: ~6 s) while the plan is being made, not after.
        warm = asyncio.create_task(self.router.prewarm("chat"))
        if self.connectors is not None and (apps_hint := self.connectors.planner_hint()):
            environment += " " + apps_hint
        plan = await make_plan(self.router, req.with_context, self.executor.registry.planner_names(), memory_ctx,
                               self.s.agent.name, known, environment, helpers=self._helper_slots())
        with contextlib.suppress(Exception):
            await warm
        await self.events.publish("task.plan", task_id=req.id, plan=plan.__dict__)
        self._planned.add(req.id)  # the planner already looked for facts to learn: no extra call later
        if plan.remember and self.memory and self.s.memory.enabled and self.s.memory.auto_extract:
            for fact in plan.remember:
                if (req.history or req.attachment_context) and not traced_to_user(fact, req.text):
                    # The planner read earlier turns too (answers that may come from web pages): a fact is
                    # kept only when the user's own message says it (audit item M7).
                    log.info("memory.fact_not_from_user", task=req.id)
                    continue
                if self.memory.add(fact, kind="profile", source="auto", task_id=req.id):
                    await self.events.publish("memory.learned", fact=fact)
        if not plan.needs_tools:
            answer = self.academy.add(plan.answer, req.text, f"{req.channel}:{req.chat_id or req.user}", req.history)
            return TaskResult(req.id, "done", answer, provider=self.router.last_provider or "")
        if plan.direct and not steps:
            done = await self._direct(req, plan, steps)
            if done is not None:
                return done

        skill = next((s for s in known if s.id == plan.skill_id), None)
        messages = self._initial_messages(req, plan, memory_ctx, skill)
        if steps:  # the fast path ran but could not prove the result: continue from there
            messages.append({"role": "user", "content": (
                f"Already done: {steps[0].tool}({json.dumps(steps[0].args)}) -> {steps[0].output[:800]}\n"
                "Continue from here to finish the task.")})
        if plan.research and self._helper_slots():
            # Independent lookups go to read-only helpers at the same time (opt-in, delta.helpers).
            await self.events.publish("task.step", task_id=req.id, tool="parallel_research",
                                      args={"questions": plan.research})
            found = await self.executor.execute("parallel_research", {"questions": plan.research}, task_id=req.id)
            steps.append(StepRecord("parallel_research", {"questions": plan.research}, found.ok,
                                    found.output[:2000], found.evidence))
            if found.output:
                messages.append({"role": "user", "content": found.output[:6000]})
        budget = self._step_budget()
        messages[0]["content"] += "\n\n" + abilities_text(self._brain(), self._can_see(), budget)
        verification: Verification | None = None
        final = ""
        # The screenshot check runs on its own (cheap) model: boot it while the agent works.
        warm_checker = asyncio.create_task(self.router.prewarm("vision"))
        self._learning.add(warm_checker)  # keep a reference; drained like learning at shutdown
        warm_checker.add_done_callback(self._learning.discard)
        for attempt in range(1, self.s.agent.max_attempts + 1):
            # Only the tools the plan needs (plus looking/checking helpers), on every attempt: the
            # same tool list keeps a Claude Code session and its prompt cache alive across retries,
            # where switching to all ~30 tools would restart it and re-send everything.
            tools = self._tool_specs(plan.tools, req.text)
            final, stopped = await self._act(req, messages, steps, tools, budget)
            if stopped:
                return TaskResult(req.id, "cancelled", stopped, attempt, steps)
            if final.startswith(NEED_USER):
                # A real blocker only the user can clear: ask, don't burn retries on it.
                ask, questions = parse_questions(final.removeprefix(NEED_USER).strip())
                simple = len(questions) == 1 and not questions[0]["multi"]  # one pick-one: plain buttons
                return TaskResult(req.id, "needs_user", ask or (questions[0]["q"] if simple else ""), attempt,
                                  steps, provider=self.router.last_provider or "",
                                  options=questions[0]["options"] if simple else [],
                                  questions=[] if simple else questions, asked_about=req.text)
            final = final.removeprefix(DONE).strip()
            report = await self._ground(req, final, steps)
            if report is not None and (wrong := report.failure(self.s.grounding.mode == "strict")):
                # A claim the task did not show, or that a check proved wrong: no verifier call, the agent
                # gets one more go with the reason (the same path as a failed verification).
                verification = Verification(False, f"grounding: {wrong}", [wrong])
                await self.events.publish("task.verify", task_id=req.id, attempt=attempt, passed=False,
                                          reason=verification.reason, checks=[], screenshot=False)
                self.audit.record("verify", task=req.id, attempt=attempt, passed=False, reason=verification.reason)
                messages.append({"role": "user", "content": (
                    f"Grounding check FAILED: {wrong}. Check the real state with a read-only tool, fix what is "
                    "missing, and in your summary state only what the tools in this task showed.")})
                continue
            await self._fresh_media_status(req.id, steps)
            shot, target = await self._final_screenshot(steps)  # saved as proof either way
            if self.engine is not None:
                self.engine.verifying(req.id)
            verification = await verify(self.router, req.with_context, plan, steps, final,
                                        None if read_page_last(steps) else shot, target)
            await self.events.publish("task.verify", task_id=req.id, attempt=attempt,
                                      passed=verification.passed, reason=verification.reason,
                                      checks=verification.checks, screenshot=verification.screenshot)
            self.audit.record("verify", task=req.id, attempt=attempt, passed=verification.passed,
                              reason=verification.reason, checks=verification.checks, raw=verification.raw)
            if verification.passed:
                if report is not None and (note := report.footnote()):
                    final = f"{final}\n\n{note}"
                done = TaskResult(req.id, "done", final, attempt, steps, verification,
                                  self.router.last_provider or "", plan.skill_id)
                done.grounding = report.to_dict() if report is not None else {}
                return done
            messages.append({"role": "user", "content": (
                f"Verification FAILED: {verification.reason}. Unmet: {verification.missing}. "
                "Inspect the current state with tools, fix what is missing, then summarise again. "
                "If the evidence already shows the result and the check misread it (e.g. the same number "
                "written with different digit grouping), do NOT redo the work: reply DONE: quoting the exact "
                "evidence. Stay with the apps and pages of this task; never open unrelated apps.")})
        answer = (f"{final}\n\n⚠ I could not verify this task is complete after "
                  f"{self.s.agent.max_attempts} attempts: {verification.reason if verification else ''}")
        return TaskResult(req.id, "failed", answer.strip(), self.s.agent.max_attempts, steps, verification,
                          self.router.last_provider or "", plan.skill_id)

    async def _attach(self, req: TaskRequest) -> bool:
        """Read the request's attached files and put their compact extract into the request. True when the
        message itself carries files. The task is marked as having read outside content (ExfilGuard and the taint
        rule apply from the first step) and the extract is logged as outside text for memory provenance."""
        if self.attachments is None or not (req.attachments or req.chat_id):
            return bool(req.attachments)
        try:
            prepared = await self.attachments.prepare(req)
        except Exception:
            log.exception("attachments.prepare_failed", task=req.id)
            if req.attachments:
                req.attachment_notes.append("I could not read the attached file, so I answered without it.")
            return bool(req.attachments)
        req.attachment_context = prepared.context
        req.attachment_notes.extend(prepared.notes)
        if prepared.ids:
            self.executor.taint(req.id, "attachment")
            observations = getattr(self.executor, "observations", None)
            if observations is not None:
                observations.add(req.id, "attachment", True, {"ids": prepared.ids}, prepared.outside, {}, True, True)
            await self.events.publish("task.attachments", task_id=req.id, files=len(prepared.ids),
                                      tokens=prepared.tokens, full_tokens=prepared.full_tokens)
        return bool(req.attachments)

    async def _replay(self, req: TaskRequest, steps: list[StepRecord]) -> TaskResult | None:
        """A proven skill with a recipe: one small call to fill its slots, then its tool calls run
        back to back with no AI in between (each still under the user's approval rules). Proof is
        the last tool's own evidence or the final-screenshot check. Anything off: the normal agent
        continues from here, and the skill's record notes the failure."""
        if not self.skills:
            return None
        for skill in self.skills.replayable(req.text):
            recipe = Recipe.from_json(skill.recipe)
            if recipe is None:
                continue
            values = await match_slots(self.router, req.text, skill.name, skill.when_to_use, recipe)
            if values is None:
                continue
            plan = Plan(True, goal=req.text, steps=[f"{s['tool']}" for s in recipe.steps],
                        success_criteria=[f"The request is fulfilled: {req.text}"], skill_id=skill.id)
            await self.events.publish("task.plan", task_id=req.id, plan={**plan.__dict__, "replay": skill.name})
            candidates = (self.skills.candidates(skill.id, status="candidate")
                          if self.gate is not None and self.s.learning.repair_recipes else [])
            used: list[int] = []  # recipe candidates created or used in this replay (delta.learning.repair)
            broken = False
            for index, step in enumerate(recipe.steps):
                args = fill(step["args"], values)
                await self.events.publish("task.step", task_id=req.id, tool=step["tool"], args=args)
                result = await self.executor.execute(step["tool"], args, task_id=req.id)
                steps.append(StepRecord(step["tool"], args, result.ok, result.output[:2000], result.evidence))
                self._count(result)
                if result.evidence.get("user_denied"):
                    return TaskResult(req.id, "cancelled", "OK, I didn't do it.", 1, steps)
                if result.ok:
                    continue
                fixed = await self._repair_step(req, skill, recipe, index, step, args, result, values, steps,
                                                candidates)
                if fixed is None:
                    broken = True
                    break
                if fixed:
                    used.append(fixed)
            if not broken and steps and steps[-1].ok and steps[-1].evidence.get("verified"):
                verification = Verification(True, f"replayed skill '{skill.name}'", [], [], False)
            elif not broken and steps:
                shot, target = await self._final_screenshot(steps)
                if self.engine is not None:
                    self.engine.verifying(req.id)
                verification = await verify(self.router, req.text, plan, steps, "", shot, target)
            else:
                verification = Verification(False, "a replayed step failed")
            for cand in dict.fromkeys(used):
                self.gate.record_run(f"R{cand}", verification.passed, req.id)
            change = self.skills.record(skill.id, success=verification.passed)
            await self.events.publish("skill.changed", change=change.change, skill=change.skill.to_dict())
            if verification.passed:
                return TaskResult(req.id, "done", f"Done using the learned skill \"{skill.name}\": "
                                  + "; ".join(s.output[:120] for s in steps[-2:]), 1, steps, verification,
                                  self.router.last_provider or "", skill.id, fast=True)
            log.info("orchestrator.replay_fallback", task=req.id, skill=skill.id)
            return None  # the agent continues; `steps` tells it what already happened
        return None

    async def _direct(self, req: TaskRequest, plan: Plan, steps: list[StepRecord]) -> TaskResult | None:
        """Fast path: the planner named one tool call that does the whole job. If the tool proves its
        own result (evidence["verified"], e.g. Windows says the song is PLAYING), that IS the
        verification: no agent loop, no screenshots, no checker call. Otherwise fall back to the
        normal loop, which starts with this step already done."""
        direct = plan.direct or {}
        tool, args = direct["tool"], direct.get("args", {})
        await self.events.publish("task.step", task_id=req.id, tool=tool, args=args)
        result = await self.executor.execute(tool, args, task_id=req.id)
        steps.append(StepRecord(tool, args, result.ok, result.output[:2000], result.evidence))
        self._count(result)
        if result.evidence.get("user_denied"):
            return TaskResult(req.id, "cancelled", f"OK, I didn't do it. You declined: "
                              f"{result.evidence.get('denied_action', tool)}.", 1, steps)
        if not (result.ok and result.evidence.get("verified")):
            log.info("orchestrator.direct_fallback", task=req.id, tool=tool, ok=result.ok)
            return None
        checks = [{"checkpoint": c, "met": True, "seen": result.output[:300]} for c in plan.success_criteria]
        verification = Verification(True, f"{tool} proved it: {result.output[:200]}", [], checks, False)
        await self.events.publish("task.verify", task_id=req.id, attempt=1, passed=True,
                                  reason=verification.reason, checks=checks, screenshot=False)
        return TaskResult(req.id, "done", result.output, 1, steps, verification,
                          self.router.last_provider or "", fast=True)

    def _initial_messages(self, req: TaskRequest, plan: Plan, memory_ctx: str,
                          skill: Skill | None = None) -> list[Message]:
        system = ACTOR_PROMPT.format(
            name=self.s.agent.name, credit=CREDIT, style=STYLE_BLOCK, owner=self.s.agent.owner_name or "the user",
            os=f"{platform.system()} {platform.release()}", now=datetime.now().strftime("%A %d %B %Y %H:%M"),
            home=Path.home(), desktop=known_folder("Desktop"), documents=known_folder("Personal"),
            save_dir=save_dir(self.s.files), diff_help=snapshot.DIFF_HELP,
            goal=plan.goal,
            steps="\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan.steps)) or "(decide as you go)",
            criteria="\n".join(f"- {c}" for c in plan.success_criteria), memory=memory_ctx or "(nothing yet)",
            skill=(f"\nFOLLOW LEARNED SKILL #{skill.id} ({skill.name}), adapting it to this request:\n"
                   + "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(skill.procedure)) + "\n") if skill else "")
        system += playbooks_for(req.text)  # expert know-how only when the request needs it
        if req.channel in REMOTE_CHANNELS:
            system += REMOTE_NOTE.format(channel=req.channel.title())
        return [{"role": "system", "content": system}, {"role": "user", "content": req.with_context}]

    def _brain(self) -> str:
        """The provider that will most likely answer: the main AI, else the first on the route."""
        first = self.router.candidates("chat")
        return first[0].name if first else ""

    def _helper_slots(self) -> int:
        """How many parallel read-only helpers the planner may ask for: 0 unless turned on (off by default)."""
        if self.helpers is None or not self.helpers.enabled:
            return 0
        if "parallel_research" not in self.executor.registry.names():
            return 0
        return int(self.helpers.slots())

    def _can_see(self) -> bool:
        return "look_at_screen" in self.executor.registry.names() and bool(self.router.candidates("vision"))

    def _step_budget(self) -> int:
        return max(6, round(self.s.agent.max_steps * brain_for(self._brain()).step_factor))

    def _brain_sees(self) -> bool:
        """The acting model takes images itself, so a screenshot can go straight into its
        conversation instead of costing a separate describe-the-screen call."""
        first = self.router.candidates("chat")
        return bool(first) and first[0].supports("vision") and self._can_see()

    async def _observe(self, tool: str, task_id: str, steps: list[StepRecord] | None = None,
                       request: str = "", evidence: dict[str, Any] | None = None, seq: int = -10
                       ) -> tuple[str, str, dict[str, Any], str | None] | None:
        """What the screen/page shows right after an action: (observer, text, args, screenshot)."""
        if not self.s.agent.auto_observe:
            return None
        names = set(self.executor.registry.names())
        if tool in PIXEL_ACTIONS and self._brain_sees():
            shot, target = await self._screenshot(proof_target(steps or []) or "", OBSERVE_WIDTH, "after " + tool)
            if shot:
                if self._same_picture(task_id, target, shot):  # perception cache: not sent twice
                    from delta.perception.cache import SAME_PICTURE

                    return "screenshot", SAME_PICTURE, {"window": target}, None
                usage.bump(self.router, "images_sent")
                usage.bump(self.router, "image_tokens_est", _jpeg_tokens(shot))
                return "screenshot", f"(screenshot of {target} attached below)", {"window": target}, shot
        args: dict[str, Any]
        if tool in BROWSER_ACTIONS and "browser_read" in names:
            # Read the part of the page the task is about: unfocused, the answer can fall past the cut
            # and the agent ends up answering from a search snippet the checker rejects (3 extra calls).
            observer, args = "browser_read", {"focus": task_focus(request, evidence or {})}
        elif tool in PIXEL_ACTIONS and self._can_see():
            observer, args = "look_at_screen", {"question": (
                "In 2-3 short sentences: what is on the screen now? Mention any dialog, error, "
                "selected item or text field with focus.")}
        elif tool in DESKTOP_ACTIONS and "read_window" in names:
            # Typed text sits deep in the control tree (e.g. Notepad's editor): read far enough to show it.
            observer = "read_window"
            args = {"depth": 10 if tool in TEXT_ACTIONS else 5}
            if window := (evidence or {}).get("window"):  # e.g. the window open_app just opened and focused
                args["window"] = window
        else:
            return None
        result = await self.executor.execute(observer, args, task_id=task_id)
        if not result.ok:
            return None
        text = result.output
        if observer == "read_window" and self.perception is not None and task_id:
            # Perception cache: an unchanged window is one line, a changed one only its changed lines.
            window = str(result.evidence.get("window") or args.get("window") or "")
            text, cached = self.perception.cache.window_text(task_id, window, text, seq)
            if cached:
                usage.bump(self.router, "perception_cache_hits")
        if len(text) > OBSERVE_CHARS:
            text = text[:OBSERVE_CHARS] + "\n…[more: call the read tool again with a focus]"
        return observer, text, args, None

    def _same_picture(self, task_id: str, target: str, b64: str) -> bool:
        if self.perception is None or not task_id:
            return False
        try:
            import base64
            import io

            from PIL import Image

            from delta.perception.images import digest

            img = Image.open(io.BytesIO(base64.b64decode(b64)))
            seen = self.perception.cache.picture_seen(task_id, f"step:{target}", digest(img))
        except Exception:
            return False
        if seen:
            usage.bump(self.router, "perception_cache_hits")
        return bool(seen)

    async def _fresh_media_status(self, task_id: str, steps: list[StepRecord]) -> None:
        """Playback tasks: what Windows says is playing right now goes into the evidence, so the
        check never relies on an old reading (e.g. taken while an ad was still running).
        Exact, ~0.6 s and no AI tokens."""
        if not any(s.tool in ("media_status", "media_control") for s in steps):
            return
        result = await self.executor.execute("media_status", {}, task_id=task_id)
        if result.ok:
            steps.append(StepRecord("media_status", {"auto": "before final check"}, True, result.output[:2000],
                                    result.evidence))

    async def _final_screenshot(self, steps: list[StepRecord]) -> tuple[str | None, str]:
        """A fresh picture of where the task happened (the app window or browser page it used),
        for the verifier and the saved proof. None when the task never touched the screen."""
        if not self.s.agent.auto_observe or not self._can_see():
            return None, ""
        target = proof_target(steps)
        if target is None:
            return None, ""
        return await self._screenshot(target, FINAL_WIDTH, "final")

    async def _screenshot(self, target: str, width: int, label: str) -> tuple[str | None, str]:
        """One window (even if covered) or, if not found, the whole screen; saved as proof."""
        try:
            from delta.tools import screen

            hwnd = await asyncio.to_thread(screen.find_window_handle, target) if target else None
            if hwnd:
                b64 = (await asyncio.to_thread(screen.capture_window_jpeg, hwnd, width))[0]
            else:
                target = "the whole screen"
                b64 = (await asyncio.to_thread(screen.capture_jpeg, width))[0]
        except Exception as e:  # no display, locked screen, missing extras: carry on without it
            log.info("orchestrator.screenshot_failed", error=str(e))
            return None, ""
        if self.proof:
            self.proof.save_image(b64, f"{label} {target}")
        return b64, target

    def _tool_specs(self, wanted: list[str], request: str = "") -> list[dict[str, Any]]:
        """Specs for the planned tools plus a few cheap read-only helpers; all tools if none planned.
        Sending 6 tool descriptions instead of 30+ cuts most of the tokens of every call. A tool group
        is sent whole (half a browser kit makes the agent fall back to pixel clicks), and a request
        about a website always gets the browser tools."""
        specs = self.executor.registry.specs()
        if not wanted:
            return specs
        keep = set(wanted) | ALWAYS_TOOLS
        if (req := CURRENT_REQUEST.get()) and req.channel in ("telegram", "slack"):
            keep |= {"send_file", "search_files"}  # away from the PC: "send me that file" must always work
        if req is not None and req.attachment_context:
            keep |= ATTACHMENT_TOOLS  # pull more pages, or look at a picture, only when needed
        if set(wanted) & BROWSER_TOOLS or WEBSITE.search(request):  # not ALWAYS_TOOLS' browser_read
            keep |= BROWSER_TOOLS
        if set(wanted) & SCREEN_TOOLS:
            keep |= SCREEN_TOOLS | PERCEPTION_TOOLS
        return [s for s in specs if s["function"]["name"] in keep]

    async def _complete_with_recovery(self, messages: list[Message], tools: list[dict[str, Any]],
                                      task_id: str = "", prefer: str | None = None) -> Any:
        """Model call that recovers from the common, fixable failures of free/small models: a request over
        the size limit (trim harder), a malformed tool call (ask again) and, with self-heal on, a busy AI
        service (wait, then ask once more; capped per task). Each recovery is a line in the run timeline."""
        from delta.selfheal.taxonomy import Failure, classify_llm_error

        healed = False
        for attempt in range(3):
            try:
                if prefer:
                    resp = await self.router.complete(messages, tools=tools, task="chat", prefer=prefer)
                else:
                    resp = await self.router.complete(messages, tools=tools, task="chat")
                if healed and self.healer is not None:
                    await self.healer.record(task_id, Failure.RATE_LIMIT,
                                             "The AI service was busy; waited and tried again.", True)
                return resp
            except LLMError as e:
                kinds = {e.kind, *getattr(e, "kinds", [])}
                failure = classify_llm_error(e.kind, str(e), list(getattr(e, "kinds", [])))
                if self.engine is not None and task_id:
                    self.engine.failure(task_id, str(failure), str(e).split(": ", 1)[-1][:300])
                if (attempt < 2 and failure == Failure.RATE_LIMIT and self.healer is not None
                        and self.healer.cfg.enabled and self.healer.take(task_id, "model:rate_limit")):
                    wait = min(self.healer.cfg.max_backoff_s,
                               float(getattr(e, "retry_after_s", None) or 10.0) * (attempt + 1))
                    log.info("orchestrator.rate_limit_backoff", task=task_id, seconds=wait)
                    await asyncio.sleep(wait)
                    healed = True
                    continue
                if attempt == 2 or not kinds & {"too_large", "bad_tool_call"}:
                    raise
                if "too_large" in kinds:
                    # The provider's real limit is lower than our estimate: cut at least 30%.
                    current = sum(len(str(m.get("content") or "")) for m in messages)
                    compact_history(messages, min(CONTEXT_BUDGET_CHARS // 2, int(current * 0.7)), keep_last=1)
                    line = "The conversation was too long for the AI; shortened it and asked again."
                else:
                    log.info("orchestrator.retry_bad_tool_call", attempt=attempt + 1)
                    messages.append({"role": "user", "content": (
                        "Your last tool call was not valid JSON. Call the tool again with arguments as a "
                        "valid JSON object (double quotes, no comments, no trailing commas).")})
                    line = "The AI sent a broken tool call; asked it to send it again."
                if self.healer is not None and task_id:
                    await self.healer.record(task_id, Failure.MODEL_ERROR, line, True)
        raise AssertionError("unreachable")

    async def _act(self, req: TaskRequest, messages: list[Message], steps: list[StepRecord],
                   tools: list[dict[str, Any]], budget: int | None = None) -> tuple[str, str]:
        """Run tool calls until the model gives a final answer. Returns (final_answer, stop_reason)."""
        nudges = 0
        # The previous tool result's AFTER page text, when it was sent in full: (address, text).
        prev_after: tuple[str, str] | None = None
        since = len(steps)  # steps taken since the last model reply (the ladder's step type)
        for _ in range(budget or self.s.agent.max_steps):
            if self.governor and self.governor.health == Health.CRITICAL:
                reasons = ", ".join(self.governor.assessment.reasons)
                return "", f"Paused to protect your laptop ({reasons}). Ask me again once it cools down."
            if question := self._over_budget():
                return question, ""
            compact_history(messages, CONTEXT_BUDGET_CHARS)
            prefer = None
            if self.ladder is not None and self.ladder.enabled:  # opt-in cheap-first models (off by default)
                prefer = self.ladder.pick(req.id, self.ladder.step_kind(steps, since))
            since = len(steps)
            resp = await self._complete_with_recovery(messages, tools, req.id, prefer)
            if not resp.tool_calls:
                content = (resp.content or "").strip()
                if getattr(resp, "final", False) and not content.startswith((DONE, NEED_USER)):
                    content = f"{DONE} {content}"  # {"final": ...} is an explicit "I am finished"
                messages.append({"role": "assistant", "content": content})
                if content.startswith(NEED_USER) and claims_no_tools(content) and nudges < MAX_NUDGES:
                    nudges += 1
                    names = ", ".join(t["function"]["name"] for t in tools)
                    log.info("orchestrator.false_no_tools", task=req.id, said=content[:160])
                    messages.append({"role": "user", "content": (
                        f"You DO have working tools for this: {names}. They act on the user's PC right now. "
                        "Continue the task with them (call a tool). Do not say tools are missing.")})
                    continue
                if content.startswith((DONE, NEED_USER)) or nudges >= MAX_NUDGES:
                    return content, ""
                # A progress note or "should I continue?" is not the end of the task.
                nudges += 1
                log.info("orchestrator.continue_nudge", task=req.id, nudge=nudges, said=content[:160])
                messages.append({"role": "user", "content": CONTINUE_NUDGE})
                continue
            messages.append({"role": "assistant", "content": resp.content or "", "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in resp.tool_calls]})
            latest_shot: tuple[str, str] | None = None
            for call in resp.tool_calls:
                await self.events.publish("task.step", task_id=req.id, tool=call.name, args=call.arguments)
                result = await self.executor.execute(call.name, call.arguments, task_id=req.id)
                steps.append(StepRecord(call.name, call.arguments, result.ok, result.output[:2000], result.evidence))
                heal = None
                if self.healer is not None and not result.evidence.get("user_denied"):
                    # Self-heal (delta.selfheal): a fixed, safe recovery first; retries pass the same gate.
                    if result.ok:
                        heal = await self.healer.after_ok(req.id, req.text, call.name, call.arguments, result)
                    else:
                        heal = await self.healer.recover(req.id, call.name, call.arguments, result, steps, req.text)
                        result = heal.result
                self._count(result)
                if heal is not None and heal.lines and self.lessons is not None:
                    await self._note_lesson(req, heal, steps, result)
                if self.ladder is not None:
                    self.ladder.record(req.id, result.ok)
                if self.proof:
                    self.proof.checkpoint(steps)
                if result.evidence.get("user_denied"):
                    # A human "no" ends the task: no workarounds, no retries, no second question.
                    return "", (f"OK, I didn't do it. You declined: {result.evidence.get('denied_action', call.name)}. "
                                "Tell me if you want something different.")
                content = result.for_llm()
                if heal is not None and (note := heal.note()):
                    content += "\n\n" + note
                if heal is not None and heal.stop:
                    # Only the user can clear this (a sign-in, a Windows permission, every way tried): ask.
                    messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content})
                    for later in resp.tool_calls[resp.tool_calls.index(call) + 1:]:
                        messages.append({"role": "tool", "tool_call_id": later.id, "name": later.name,
                                         "content": "Not run: the task stopped to ask the user."})
                    messages.append({"role": "assistant", "content": heal.stop})
                    return heal.stop, ""
                acted = str(result.evidence.get("resolved_to") or call.name)  # click_mark -> browser_click, ...
                seq = sum(1 for m in messages if m.get("role") == "tool")
                if result.ok and (seen := await self._observe(acted, req.id, steps, req.text, result.evidence, seq)):
                    observer, text, obs_args, shot = seen
                    if shot:
                        latest_shot = (shot, obs_args.get("window", ""))
                    steps.append(StepRecord(observer, obs_args, True, text[:2000], {"auto_observe": True}))
                    if observer == "browser_read":  # elements already in the page listing: not twice
                        text = drop_held_lines(text, held_element_lines(
                            [*messages, {"role": "tool", "name": call.name, "content": content}]))
                        text, prev_after = same_text_as_before(text, str(result.evidence.get("url") or ""),
                                                               prev_after)
                    else:
                        prev_after = None
                    await self.events.publish("task.step", task_id=req.id, tool=observer, args={"auto": True})
                    content += f"\n\nAFTER ({observer}):\n{text}"
                else:
                    prev_after = None
                if stuck_in_loop(steps):
                    # Small models repeat a failing action; tell them plainly to change approach.
                    content += ("\n\nNOTE: you have now done exactly this 3 times in a row and it is not "
                                "working. Do something different (another tool, another field, a direct "
                                "URL), or tell the user what is blocking you.")
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content})
            if latest_shot:
                # The picture itself, for a model that can see: no extra vision call, no description.
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": f"AFTER screenshot of {latest_shot[1] or 'the screen'}:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{latest_shot[0]}"}}]})
        return "I ran out of steps for this attempt.", ""

    # --- memory, grounding and learning (Batch D) ------------------------------------------------
    def _keep_provenance(self, task_id: str) -> None:
        """Before the executor forgets the task: keep its taint and what it read from outside, for learning."""
        try:
            source = self.executor.tainted_by(task_id)
            observations = getattr(self.executor, "observations", None)
            outside = observations.outside_text(task_id) if (source and observations is not None) else None
            self._after[task_id] = (source, outside)
            while len(self._after) > 64:  # tasks whose learning never ran (shut down): drop the oldest
                self._after.pop(next(iter(self._after)))
        except Exception:
            log.exception("orchestrator.provenance_failed", task=task_id)

    def _count(self, result: Any) -> None:
        """Usage-meter counters a tool result reports (recall hits)."""
        ev = getattr(result, "evidence", None) or {}
        if n := ev.get("memory_hits"):
            usage.bump(self.router, "memory_hits", int(n))
        if n := ev.get("memory_untrusted_hits"):
            usage.bump(self.router, "memory_untrusted_hits", int(n))

    async def _memory_context(self, req: TaskRequest) -> str:
        if not self.memory:
            return ""
        with_hits = getattr(self.memory, "context_with_hits", None)
        if callable(with_hits):
            ctx, hits = with_hits(req.text, self.s.memory.recall_k)
        else:  # a store without hit counts (older or test stores)
            ctx, hits = self.memory.context_for(req.text, self.s.memory.recall_k), 0
        if hits:
            usage.bump(self.router, "memory_hits", hits)
            if self.engine is not None:
                self.engine.line(req.id, "memory", f"Recalled {hits} past task note(s) that match this request.",
                                 data={"hits": hits})
        return str(ctx or "")

    def _allowed_quietly(self, name: str, args: dict[str, Any]) -> bool:
        """A read-only call the policy allows without asking (grounding checks never trigger an approval)."""
        from delta.safety.policy import Verdict

        tool = self.executor.registry.get(name)
        if tool is None:
            return False
        try:
            decision = self.executor.policy.evaluate(tool, tool.Args.model_validate(args))
        except Exception:
            return False
        return bool(decision.verdict == Verdict.ALLOW and str(decision.risk) == "read")

    async def _ground(self, req: TaskRequest, final: str, steps: list[StepRecord]) -> Any:
        """Answer grounding (delta.core.grounding). None when switched off."""
        from delta.core.grounding import GroundingReport, apply_check, check_for, extract_claims, ground

        cfg = self.s.grounding
        if cfg.mode == "off":
            return None
        report = GroundingReport(extract_claims(final))
        if not report.claims:
            return report
        observations = getattr(self.executor, "observations", None)
        items = observations.items(req.id) if observations is not None else []
        if items and observations is not None:
            corpus = observations.corpus(req.id)
            ok_tools = {o.tool for o in items if o.ok}
        else:  # an executor without an observation log: the step records (outputs cut to 2000 characters)
            corpus = "\n".join(f"{st.output}\n{json.dumps(st.args, default=str)}\n"
                               f"{json.dumps(st.evidence, default=str)}" for st in steps if st.ok)
            ok_tools = {st.tool for st in steps if st.ok}
        known = (f"{datetime.now():%A %d %B %Y %H:%M} {save_dir(self.s.files)} {known_folder('Desktop')} "
                 f"{known_folder('Personal')} {Path.home()}")
        missing = observations.missing_paths(req.id) if (items and observations is not None) else set()
        ground(report.claims, corpus, req.text, ok_tools, known, missing)
        failed_args = [st.args for st in steps if not st.ok]
        last_page = any(st.tool in BROWSER_TOOLS for st in steps) and "browser_read" in self.executor.registry.names()
        order = sorted(report.unverified, key=lambda c: (c.kind != "path", c.kind != "number"))
        for claim in order:
            if report.checks >= cfg.max_checks:
                break
            found = check_for(claim, failed_args, last_page)
            if found is None or not self._allowed_quietly(*found):
                continue
            tool, args = found
            result = await self.executor.execute(tool, args, task_id=req.id)
            report.checks += 1
            steps.append(StepRecord(tool, args, result.ok, result.output[:2000],
                                    {**result.evidence, "grounding": True}))
            apply_check(claim, tool, result.output, result.evidence, result.ok)
        usage.bump(self.router, "grounding_claims", len(report.claims))
        usage.bump(self.router, "grounding_checks", report.checks)
        usage.bump(self.router, "unverified_claims", len(report.unverified))
        usage.bump(self.router, "contradicted_claims", len(report.contradicted))
        if self.engine is not None:
            clean = not report.unverified and not report.contradicted
            self.engine.line(req.id, "grounding", report.summary(), ok=clean,
                             data={"claims": len(report.claims),
                                   "unverified": [c.text for c in report.unverified][:10],
                                   "contradicted": [c.text for c in report.contradicted][:10]})
        with contextlib.suppress(Exception):
            await self.events.publish("task.grounding", task_id=req.id, **report.to_dict())
        return report

    async def _repair_step(self, req: TaskRequest, skill: Skill, recipe: Recipe, index: int, step: dict[str, Any],
                           args: dict[str, Any], result: Any, values: dict[str, str], steps: list[StepRecord],
                           candidates: list[Any]) -> int | None:
        """A replayed step failed: repair just that step (delta.learning.repair). Returns the recipe candidate
        it used or created, 0 when it recovered without a new version (a retry worked), None when it could not."""
        from delta.learning.repair import REPAIRABLE, candidate_step, describe_change, repaired, same_recipe
        from delta.selfheal.taxonomy import classify, is_refusal

        if self.gate is None or not self.s.learning.repair_recipes or not self.skills:
            return None
        if is_refusal(result.output, result.evidence):
            return None
        tool = str(step["tool"])
        failure = classify(tool, result.output, result.evidence)
        if failure not in REPAIRABLE:
            return None
        found = candidate_step(candidates, index, values)
        if found is not None:  # a repaired version of this step is waiting for evidence: try it
            cand_id, cand_tool, cand_args = found
            await self.events.publish("task.step", task_id=req.id, tool=cand_tool, args=cand_args)
            tried = await self.executor.execute(cand_tool, cand_args, task_id=req.id)
            steps.append(StepRecord(cand_tool, cand_args, tried.ok, tried.output[:2000],
                                    {**tried.evidence, "recipe_candidate": cand_id}))
            if tried.ok:
                if self.engine is not None:
                    self.engine.line(req.id, "learning",
                                     f"Step {index + 1} of skill '{skill.name}' failed ({failure}); the repaired "
                                     f"version from candidate R{cand_id} worked.", ok=True,
                                     data={"item": f"R{cand_id}", "event": "used"})
                return cand_id
            self.gate.record_run(f"R{cand_id}", False, req.id)
        if self.healer is None:
            return None
        heal = await self.healer.recover(req.id, tool, args, result, steps, req.text)
        if not heal.recovered:
            return None
        worked = next((st for st in reversed(steps) if st.ok and st.evidence.get("recovery")), None)
        if worked is None:
            return 0
        new = repaired(recipe, index, worked.tool, worked.args, values)
        if same_recipe(new.to_json(), skill.recipe):
            return 0  # the same call worked on a second try: nothing to learn
        source, outside = None, None
        with contextlib.suppress(Exception):
            source = self.executor.tainted_by(req.id)
            observations = getattr(self.executor, "observations", None)
            outside = observations.outside_text(req.id) if (source and observations is not None) else None
        trust, why = recipe_trust([new.steps[index]], req.text, source, outside)
        cand = self.skills.add_candidate(skill.id, new.to_json(), skill.recipe or "", index, str(failure),
                                         describe_change(step, worked.tool, worked.args), trust, req.id)
        usage.bump(self.router, "learning_candidates")
        tail = ("it is untrusted (" + why + ") and will never replace the working one" if trust != "trusted"
                else "the working recipe stays until the candidate passes")
        line = (f"Step {index + 1} of skill '{skill.name}' failed ({failure}); found a way that works and saved it "
                f"as candidate R{cand.id}: {tail}.")
        self.gate._history(f"R{cand.id}", "candidate", req.id, line)
        return int(cand.id)

    async def _note_lesson(self, req: TaskRequest, heal: Any, steps: list[StepRecord], result: Any) -> None:
        """A self-heal outcome may teach (or count against) a lesson for the site (delta.learning.lessons)."""
        from delta.learning.lessons import site_of

        try:
            site = site_of(steps, str(getattr(result, "evidence", {}).get("url") or ""))
            change = self.lessons.note(req.id, req.text, heal.failure, site, bool(heal.recovered),
                                       self.executor.tainted_by(req.id))
            if change is not None and change.change == "candidate":
                usage.bump(self.router, "learning_candidates")
        except Exception:
            log.exception("orchestrator.lesson_failed", task=req.id)

    @staticmethod
    def _chat_key(req: TaskRequest) -> str:
        return req.chat_id or f"{req.channel}:{req.user}"

    def _begin_budget(self, req: TaskRequest) -> None:
        """Set this task's token limit. Normally the user's per-task budget. When the previous task in this chat
        stopped at its limit and the user answered "Keep going", the limit is raised by one more budget on top
        of what was already used, said so in the audit log, and the earlier usage keeps counting."""
        base = self.s.agent.token_budget
        if base <= 0:
            return
        stop = self._budget_stops.pop(self._chat_key(req), None)
        if stop is not None and KEEP_GOING.match(req.text.strip()):
            used, cap = stop
            extended = max(cap, used) + base
            self._budget[req.id] = (used, extended)
            self.audit.record("task.budget_extended", task=req.id, used=used, previous=cap, budget=extended)
            log.info("orchestrator.token_budget_extended", task=req.id, used=used, budget=extended)
        else:
            self._budget[req.id] = (0, base)

    def _over_budget(self) -> str:
        """NEED_USER question once the task's tokens pass its limit (off when the budget is 0). After a
        "Keep going" the limit is the extended one and tokens used before the pause still count."""
        base = self.s.agent.token_budget
        if base <= 0:
            return ""
        req = CURRENT_REQUEST.get()
        carried, cap = self._budget.get(req.id, (0, base)) if req is not None else (0, base)
        used = carried + usage.used_for_budget(self.router.meter)
        if used < cap:
            return ""
        log.info("orchestrator.token_budget", used=used, budget=cap)
        if req is not None:
            self._budget_stops[self._chat_key(req)] = (used, cap)
            while len(self._budget_stops) > 64:
                self._budget_stops.pop(next(iter(self._budget_stops)))
            if self.engine is not None:
                self.engine.failure(req.id, "budget", f"token budget reached: {used} of {cap}")
        limit = (f"your limit of {cap:,} tokens per task" if cap <= base
                 else f"the extended limit of {cap:,} tokens (your limit is {base:,} per task)")
        return (f"{NEED_USER} This task has used about {used:,} tokens, which passes {limit}. Keep going adds "
                f"another {base:,} tokens (new limit {max(cap, used) + base:,}).\nShould I keep going?"
                "\n- Keep going\n- Stop here")

    async def _learn(self, req: TaskRequest, result: TaskResult) -> None:
        """Self-improvement after a task: memory facts + skills. Never raises."""
        taint, outside = self._after.pop(req.id, (None, None))
        if result.status in ("cancelled", "needs_user") or result.fast:
            self._planned.discard(req.id)  # nothing to learn: forget the task here too (the set must not grow)
            return
        try:
            if self.learning:
                for text in await learn_corrections(self.router, self.learning, req.text):
                    self.audit.record("learned", task=req.id, kind="preference", content=text)
                    item = self.learning.find("preference", re.sub(r"\W+", " ", text.lower()).strip())
                    await self.events.publish("learning.added", item=item.to_dict() if item else {"content": text})
            await self._learn_skill(req, result, taint, outside)
            if self.memory and self.s.memory.enabled:
                if result.steps:
                    self.memory.add(f"{req.text[:200]} -> {result.status}", kind="episode", source="agent",
                                    task_id=req.id)
                if self.s.memory.auto_extract and req.id not in self._planned:
                    for fact in await extract_facts(self.router, req.text, result.answer):
                        # The answer may repeat what a page said: in a task that read outside content a fact is
                        # kept only when it traces to the user's own words (audit item M7).
                        if fact_trust(fact, req.text, taint, outside)[0] != "trusted":
                            self.audit.record("memory.refused", task=req.id, reason="traced to outside content")
                            continue
                        if self.memory.add(fact, kind="profile", source="auto", task_id=req.id):
                            await self.events.publish("memory.learned", fact=fact)
        except Exception as e:
            log.error("learning.failed", task=req.id, error=str(e))
        finally:
            self._planned.discard(req.id)

    async def _learn_skill(self, req: TaskRequest, result: TaskResult, taint: str | None = None,
                           outside: str | None = None) -> None:
        if not self.skills or not self.s.memory.learn_skills:
            return
        verified = result.status == "done" and result.verification is not None and result.verification.passed
        if result.skill_id is not None:  # a learned skill was used: its track record changes
            change = self.skills.record(result.skill_id, success=verified)
        elif verified:
            # The distiller reads the arguments of the steps that worked. When the task read outside content,
            # every value it would learn must trace to the user's request, else nothing is learned (M7).
            acted = [{"tool": st.tool, "args": st.args} for st in result.steps if st.ok]
            trust, why = recipe_trust(acted, req.text, taint, outside)
            if trust != "trusted":
                self.audit.record("skill", task=req.id, change="refused", reason=why)
                if self.learning is not None:
                    self.learning.log("S-", "refused", req.id, f"No skill learned: {why}.")
                if self.engine is not None:
                    self.engine.line(req.id, "learning", f"Did not learn a skill from this task: {why}.", ok=False)
                return
            draft = await distill(self.router, req.text, result.steps)
            if draft is None:
                return
            change = self.skills.learn(draft.name, draft.when_to_use, draft.procedure, draft.tools,
                                       recipe=draft.recipe.to_json() if draft.recipe else None)
        else:
            return
        self.audit.record("skill", task=req.id, skill=change.skill.id, change=change.change)
        await self.events.publish("skill.changed", change=change.change, skill=change.skill.to_dict())
