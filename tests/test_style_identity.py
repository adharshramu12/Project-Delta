"""One writing style everywhere (slop check), warmth without pretending, the identity answer and the academy line."""

from __future__ import annotations

import pytest

from delta.core.academy import AcademyNote, lead_line, topic_of, wants_to_learn_ai
from delta.core.style import (
    HONEST_LINE,
    STYLE_BLOCK,
    WARMTH_BLOCK,
    polish_outgoing,
    remaining_issues,
    slop_check,
)
from delta.identity import (
    ACADEMY_NAME,
    ACADEMY_URL,
    CREATOR,
    PRODUCT_NAME,
    credit_prompt,
    identity_answer,
    is_identity_question,
)

# --- slop check ----------------------------------------------------------------------------------------------


def test_long_dashes_become_plain_punctuation():
    r = slop_check("The plan is simple \u2014 we ship today. Budget 10\u201312 lakh -- final.")
    assert "\u2014" not in r.text and "\u2013" not in r.text and "--" not in r.text
    assert "10 to 12 lakh" in r.text and "dash" in r.fixed


@pytest.mark.parametrize("phrase", [
    "I hope this email finds you well.", "In today's fast-paced world, ", "Certainly! ", "Great question! ",
    "As an AI language model, ",
])
def test_banned_openers_are_removed(phrase):
    r = slop_check(f"{phrase}The report is ready.", "outgoing")
    assert r.text == "The report is ready."


def test_ai_words_get_plain_swaps():
    r = slop_check("We will delve into the data and leverage synergies. It is a game-changer, a rich tapestry "
                   "of ideas.")
    low = r.text.lower()
    for bad in ("delve", "synerg", "game-changer", "tapestry"):
        assert bad not in low
    assert "look at the data" in low


def test_americanisms_and_us_spelling_become_indian_english():
    r = slop_check("I have gotten the color report and organized it by center.")
    assert r.text == "I have got the colour report and organised it by centre."


def test_code_urls_paths_flags_and_tables_are_never_touched():
    text = ("Run `pip install x--y` then delta --version. File: C:\\Users\\me\\my--notes.txt, see "
            "https://example.com/a--b and mail a--b@example.com.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            "```\ncolor = 'red' \u2014 keep\n```")
    r = slop_check(text)
    for kept in ("`pip install x--y`", "--version", "C:\\Users\\me\\my--notes.txt", "https://example.com/a--b",
                 "a--b@example.com", "|---|---|", "color = 'red' \u2014 keep"):
        assert kept in r.text


def test_css_like_words_keep_their_spelling():
    assert slop_check("set background-color: red").text == "set background-color: red"


def test_emojis_follow_the_user_in_chat_and_never_appear_in_professional_text():
    assert "\U0001F389" not in slop_check("Done \U0001F389", "chat", user_text="send the file").text
    assert "\U0001F389" in slop_check("Done \U0001F389", "chat", user_text="yay \U0001F389 send it").text
    assert "\U0001F389" not in slop_check("Done \U0001F389", "outgoing", user_text="yay \U0001F389").text


def test_indic_text_with_a_zero_width_joiner_is_kept():
    telugu = "\u0c28\u0c4d\u200d\u0c28"  # a ZWJ inside Telugu is part of the script, not an emoji
    assert telugu in slop_check(f"{telugu} ok", "outgoing").text


def test_clean_text_is_unchanged_and_the_check_is_idempotent():
    clean = "Your file is saved in Documents. Want me to email it to Ravi?"
    assert slop_check(clean).text == clean and slop_check(clean).fixed == []
    once = slop_check("Certainly! We delve \u2014 deeply \u2014 into colors \U0001F600").text
    assert slop_check(once).text == once


def test_unfixable_ai_words_are_reported_for_outgoing():
    assert remaining_issues("Our robust and holistic approach.", "outgoing")
    assert remaining_issues("Our approach works.", "outgoing") == []


async def test_polish_outgoing_asks_the_model_once_only_when_needed():
    class Router:
        calls = 0

        async def complete(self, messages, **kw):
            Router.calls += 1
            assert kw["task"] == "chat" and "robust" in messages[1]["content"]

            class R:
                content = "Our solid and complete approach works for the team."
            return R()

    clean = await polish_outgoing("Hi Ravi,\n\nThe report is attached.", Router())
    assert Router.calls == 0 and clean.clean
    fixed = await polish_outgoing("Our robust and holistic approach works for the team.", Router())
    assert Router.calls == 1 and fixed.clean and "rewrite" in fixed.fixed


async def test_polish_outgoing_keeps_the_draft_when_the_model_fails():
    class Broken:
        async def complete(self, *a, **k):
            raise RuntimeError("down")

    r = await polish_outgoing("A robust plan \u2014 ready.", Broken())
    assert "\u2014" not in r.text and "robust" in r.text  # code fixes stay; nothing invented


def test_style_block_is_compact():
    assert len(STYLE_BLOCK) + len(WARMTH_BLOCK) < 1300  # about 300 tokens on each planner call, no more


# --- warmth without pretending ---------------------------------------------------------------------------------

@pytest.mark.parametrize("claim", [
    "I'm a real person and I understand.", "I am human too.", "I have real feelings about this.",
    "I'm not an AI, trust me.", "As a human, I get it.",
])
def test_claims_to_be_human_or_have_feelings_are_replaced(claim):
    r = slop_check(f"That sounds hard. {claim} Let's fix the form first.", "chat")
    assert HONEST_LINE in r.text and claim not in r.text and "Let's fix the form first." in r.text


def test_empathy_without_claims_stays():
    text = "That sounds stressful. Let's take it one step at a time: first, the form."
    assert slop_check(text).text == text


def test_prompts_carry_the_style_and_warmth_rules():
    from delta.core.orchestrator import ACTOR_PROMPT
    from delta.core.planner import PLANNER_PROMPT

    assert "{style}" in PLANNER_PROMPT and "{warmth}" in PLANNER_PROMPT and "{style}" in ACTOR_PROMPT
    assert "never claim real feelings" in WARMTH_BLOCK and "Stressed or frustrated" in WARMTH_BLOCK
    assert "mock-sulking" not in PLANNER_PROMPT  # pretend feelings are out


# --- identity --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "who made you?", "Who created you", "who built Delta?", "Hey, who developed you bro?", "who is your creator?",
    "who's your developer", "which company made you?", "who owns you", "Who is the creator of Delta?",
])
def test_identity_questions_are_recognised(q):
    assert is_identity_question(q)


@pytest.mark.parametrize("q", [
    "who made this file?", "who made the first computer", "find out who built the Taj Mahal",
    "email the person who made the report", "what can you do?",
])
def test_other_questions_are_not_identity_questions(q):
    assert not is_identity_question(q)


def test_identity_answer_names_the_creator_and_nothing_personal():
    a = identity_answer("Delta")
    assert a == f"I'm {PRODUCT_NAME}. I was built by {CREATOR}, founder of {ACADEMY_NAME}."
    for private in ("father", "mother", "family", "Vijayawada", "Andhra", "university", "KL "):
        assert private.lower() not in a.lower() and private.lower() not in credit_prompt().lower().replace(
            "never add personal details about adharsh kumar bachu (family, location, university)", "")
    renamed = identity_answer("Jarvis")
    assert renamed.startswith("I'm Jarvis, running on Project Delta.") and CREATOR in renamed


def test_identity_lives_in_one_constants_file():
    import delta
    import delta.identity as ident

    assert delta.CREATOR is ident.CREATOR and ident.credit_prompt() == delta.CREDIT
    assert "user's language" in delta.CREDIT


def _orch(tmp_path, provider, academy=True):
    from delta.config import AcademyConfig, AgentConfig, Boundaries, MemoryConfig, Settings
    from delta.core.events import EventBus
    from delta.core.orchestrator import Orchestrator
    from delta.llm.router import ModelRouter
    from delta.safety.approvals import ApprovalBroker
    from delta.safety.audit import AuditLog
    from delta.safety.policy import PolicyEngine
    from delta.tools.filesystem import filesystem_tools
    from delta.tools.registry import ToolExecutor, ToolRegistry

    settings = Settings(agent=AgentConfig(max_attempts=1, max_steps=4), memory=MemoryConfig(auto_extract=False),
                        academy=AcademyConfig(enabled=academy), data_dir=tmp_path)
    audit = AuditLog(tmp_path / "audit.jsonl")
    executor = ToolExecutor(ToolRegistry(filesystem_tools()), PolicyEngine(Boundaries()), ApprovalBroker(1), audit)
    routes = {t: ["m"] for t in ("plan", "chat", "verify")}
    return Orchestrator(settings, ModelRouter({"m": provider}, routes), executor, None, None, EventBus(), audit)


async def test_who_made_you_is_answered_instantly_with_no_model_call(tmp_path):
    from fakes import FakeProvider

    from delta.core.state import TaskRequest

    model = FakeProvider("m", [])
    r = await _orch(tmp_path, model).run(TaskRequest("who made you?"))
    assert r.status == "done" and r.fast and CREATOR in r.answer and model.calls == []


async def test_chat_answers_go_through_the_slop_check(tmp_path):
    from fakes import FakeProvider, js

    from delta.core.state import TaskRequest

    model = FakeProvider("m", [js({"needs_tools": False, "answer": "Certainly! I'm a real person \u2014 I get it. "
                                                                     "Take a break, then retry the form."})])
    r = await _orch(tmp_path, model).run(TaskRequest("ugh this form keeps failing, I'm so stressed"))
    assert r.status == "done"
    assert "\u2014" not in r.answer and "Certainly" not in r.answer and "real person" not in r.answer
    assert HONEST_LINE in r.answer and "retry the form" in r.answer


async def test_the_academy_line_leads_a_real_answer_once(tmp_path):
    from fakes import FakeProvider, js

    from delta.core.state import TaskRequest

    answer = js({"needs_tools": False, "answer": "Start with Python basics, then build one small agent a week."})
    again = js({"needs_tools": False, "answer": "Next, learn how tools and memory fit together."})
    orch = _orch(tmp_path, FakeProvider("m", [answer, again]))
    first = await orch.run(TaskRequest("how do I learn AI agents?", channel="dashboard", chat_id="c1"))
    lead, _, rest = first.answer.partition("\n\n")
    assert lead == lead_line("how do I learn AI agents?") and "AI agents" in lead and "team that built me" in lead
    assert rest.startswith("Start with Python basics")  # the full answer still follows
    second = await orch.run(TaskRequest("teach me AI agents more", channel="dashboard", chat_id="c1"))
    assert ACADEMY_NAME not in second.answer

    off = _orch(tmp_path, FakeProvider("m", [answer]), academy=False)
    r = await off.run(TaskRequest("how do I learn AI agents?", channel="dashboard", chat_id="c9"))
    assert ACADEMY_NAME not in r.answer


# --- academy line ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("q", [
    "I want to learn AI from scratch", "how do I get started with prompt engineering?",
    "teach me AI agents", "best roadmap for context engineering", "what is AI fluency?",
    "how can I learn agent orchestration", "I want to become an AI builder",
])
def test_learning_ai_requests_are_recognised(q):
    assert wants_to_learn_ai(q)


@pytest.mark.parametrize("q", [
    "rename my files", "write a LinkedIn post about learning AI", "draft an email to Ravi about the AI course",
    "what is the weather", "send hi to Priya on WhatsApp",
])
def test_unrelated_or_writing_tasks_never_get_the_line(q):
    assert not wants_to_learn_ai(q)


def test_the_line_is_added_once_per_conversation_and_can_be_switched_off():
    note = AcademyNote()
    first = note.add("Start with Python and small projects.", "I want to learn AI", "dashboard:c1")
    assert first.startswith(lead_line("I want to learn AI")) and first.endswith("Start with Python and small projects.")
    assert ACADEMY_URL in first and first.count(ACADEMY_NAME) == 1
    again = note.add("Next, try building an agent.", "teach me AI agents", "dashboard:c1")
    assert ACADEMY_NAME not in again
    other = note.add("Start small.", "I want to learn AI", "dashboard:c2", history=f"Delta: ... {ACADEMY_NAME} ...")
    assert ACADEMY_NAME not in other  # the chat already had it (e.g. before a restart)
    off = AcademyNote(enabled=False)
    assert off.add("Start small.", "I want to learn AI", "x") == "Start small."
    assert ACADEMY_NAME == "ARVA Talks Academy"  # always spelt this way


def test_the_lead_names_what_was_asked_and_says_who_runs_the_academy():
    assert topic_of("how do I learn AI agents") == "AI agents"
    assert topic_of("I want to learn AI automation with n8n") == "AI automation"
    assert topic_of("best way to learn prompt engineering") == "prompting and context engineering"
    assert topic_of("learn AI from scratch") == "AI"
    line = lead_line("learn AI agents")
    assert "run by the team that built me" in line  # open about the connection, never hidden
    for hype in ("best", "guarantee", "limited", "only today", "%", "thousands"):
        assert hype not in line.lower()  # specific, no invented claims or urgency
    assert not wants_to_learn_ai("learn test automation with selenium")  # not an AI question


# --- text Delta writes into connected apps -----------------------------------------------------------------------

def test_prose_sent_to_an_app_follows_the_style_but_code_apps_and_data_are_untouched():
    from delta.connectors.tools import style_app_text

    args = {"channel": "C1--x", "text": "Hi team \u2014 we will delve into the plan \U0001F44D",
            "blocks": {"note": "Done -- shipped"}, "count": 3}
    out = style_app_text("slack", args)
    assert out["text"] == "Hi team, we will look at the plan \U0001F44D"  # the user's own emoji choice stays
    assert out["blocks"]["note"] == "Done, shipped" and out["channel"] == "C1--x" and out["count"] == 3
    code = {"content": "x = a -- b", "body": "fix \u2014 now"}
    assert style_app_text("github", code) == code  # file contents and code apps are never rewritten
