"""Clarifying-question forms: tick boxes and pick-one, in Telegram and from the agent; research playbook."""

from types import SimpleNamespace

import pytest

from delta.core.playbooks import playbooks_for
from delta.core.questions import answers_text, parse_questions

FORM = """A few details so the research is exactly what you need.
Which areas should it cover?
- [ ] Market size in India
- [ ] Competitors
- [ ] Pricing
How deep should it go?
- Quick overview
- Detailed report with sources"""


def test_a_form_has_tick_boxes_and_pick_one_questions():
    intro, qs = parse_questions(FORM)
    assert intro == "A few details so the research is exactly what you need."
    assert [(q["q"], q["multi"], len(q["options"])) for q in qs] == [
        ("Which areas should it cover?", True, 3), ("How deep should it go?", False, 2)]
    assert parse_questions("You are not signed in. Please sign in.") == ("You are not signed in. Please sign in.", [])
    text = answers_text(qs, {0: ["Competitors", "Pricing"]})
    assert "Which areas should it cover? -> Competitors, Pricing" in text and "(no preference)" in text


def test_research_know_how_only_for_research():
    assert "Deep Research" in playbooks_for("do a deep research on the EV market in India")
    assert "Deep Research" in playbooks_for("use chatgpt to research the best laptops under 60k")
    research = playbooks_for("deep research on edtech")
    assert "SELF-HEAL" in research  # recovers from Gemini/ChatGPT errors
    assert "Never send the brief again" in research  # a reply being written means it started: stop there
    assert playbooks_for("play rajshamani podcast") == "" and playbooks_for("open gemini") == ""


telegram = pytest.importorskip("telegram")


async def test_telegram_form_ticks_then_continues_with_all_answers(tmp_path):
    from delta.channels.telegram import TelegramChannel
    from delta.core.state import TaskResult

    _, qs = parse_questions(FORM)
    c = SimpleNamespace(audit=SimpleNamespace(record=lambda *a, **k: None))
    ch = TelegramChannel(c, "123456:TEST-TOKEN", ["5"])  # type: ignore[arg-type]
    shown, ran = [], []

    async def reply_text(text, reply_markup=None, **kw):
        shown.append((text, reply_markup))

    async def send_action(a):
        return None

    message = SimpleNamespace(reply_text=reply_text, chat=SimpleNamespace(send_action=send_action))
    await ch._start_form(message, "5", TaskResult("t1", "needs_user", "Quick details:", questions=qs))
    assert shown[0][0].startswith("Quick details:")
    assert [b.text for row in shown[1][1].inline_keyboard for b in row] == [
        "☐ Market size in India", "☐ Competitors", "☐ Pricing", "✓ Done"]

    async def continue_task(update, uid, text):
        ran.append(text)

    ch._run = continue_task

    async def tap(data):
        edits = []

        async def answer(*a):
            return None

        async def edit_markup(kb):
            edits.append(("markup", kb))

        async def edit_text(t):
            edits.append(("text", t))

        q = SimpleNamespace(from_user=SimpleNamespace(id=5), data=data, answer=answer,
                            edit_message_reply_markup=edit_markup, edit_message_text=edit_text)
        await ch._on_form(SimpleNamespace(callback_query=q), None)
        return edits

    edits = await tap("fq:0:1")  # tick Competitors
    assert [b.text for row in edits[0][1].inline_keyboard for b in row][1] == "☑ Competitors"
    await tap("fq:0:2")  # tick Pricing
    await tap("fq:0:1")  # untick Competitors
    edits = await tap("fd:0")  # Done
    assert edits[0] == ("text", "Which areas should it cover?\n→ Pricing") and ran == []  # one question left
    await tap("fq:1:1")  # pick-one answers at once
    assert ran == ["My answers:\nWhich areas should it cover? -> Pricing\n"
                   "How deep should it go? -> Detailed report with sources"]
    assert "5" not in ch._forms  # the form is closed


def test_delta_knows_its_creator():
    from delta import CREATOR
    from delta.core.orchestrator import ACTOR_PROMPT
    from delta.core.planner import PLANNER_PROMPT

    assert CREATOR == "Adharsh Kumar Bachu"
    assert "{credit}" in ACTOR_PROMPT and "{credit}" in PLANNER_PROMPT
