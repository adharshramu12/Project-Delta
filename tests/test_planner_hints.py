"""The planner's per-topic examples are sent only with requests about them; rules and the tool list always are."""

from fakes import FakeProvider, text

from delta.core.planner import HINTS, PLANNER_PROMPT, hints_for, make_plan
from delta.llm.router import ModelRouter

CASES = {
    "media": "play kesariya on youtube", "meetings": "schedule a meeting with ravi@x.com tomorrow 5pm",
    "messages": "send hi to Ravi on WhatsApp", "email": "email ravi@x.com about Friday",
    "post": "write a LinkedIn post about my launch", "documents": "make a PPT about solar power",
    "schedules": "every day at 9 am check my attendance", "browsers": "use Edge from now on",
    "deep_research": "deep research on the EV market in India",
    "learning": "learn how GST filing works and remember it",
}
TOOL_OF = {"media": "play_youtube", "meetings": "schedule_meeting", "messages": "send_message", "email": "send_email",
           "post": "write_post", "documents": "make_presentation", "schedules": "schedule_task",
           "browsers": "set_browser", "deep_research": "Deep Research", "learning": "learn_topic"}


def test_every_topic_brings_its_own_examples_and_only_when_asked():
    assert {n for n, _, _ in HINTS} == set(CASES)
    for topic, request in CASES.items():
        assert TOOL_OF[topic] in hints_for(request), topic
        assert TOOL_OF[topic] not in PLANNER_PROMPT, topic  # no longer sent with every request
    for plain in ("what is the capital of Australia", "hi buddy, how are you", "list the files in my Downloads"):
        assert hints_for(plain) == ""
    assert all(TOOL_OF[t] in hints_for("") for t in CASES)  # no request text: everything


def test_what_every_request_needs_is_still_always_sent():
    for must in ("web_answer", "needs_tools", "success_criteria", "WEBSITES and web apps", "FAST PATH",
                 "never sugar-coated", "LEARNING", "{hints}"):
        assert must in PLANNER_PROMPT
    assert len(PLANNER_PROMPT) < 4600  # was about 7,200: a quarter of every task's planning cost


async def test_the_hint_reaches_the_model_after_the_part_that_never_changes():
    p = FakeProvider("groq", [text('{"needs_tools": false, "answer": "ok"}')] * 2)
    router = ModelRouter({"groq": p}, {"plan": ["groq"], "chat": ["groq"]})
    await make_plan(router, "make a PPT about solar power", ["make_presentation"], "", "Delta")
    await make_plan(router, "what is 2+2", ["make_presentation"], "", "Delta")
    with_hint, without = (c[1][0]["content"] for c in p.calls)
    assert "make_presentation {" in with_hint and "{{" not in with_hint
    assert "MORE FAST PATH" not in without and len(without) < len(with_hint)
    static = without[:without.index("Skills the agent learned")]
    assert with_hint.startswith(static)  # the shared start is identical, so a prompt cache can reuse it
