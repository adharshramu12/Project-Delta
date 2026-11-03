"""Chat like a partner, learn from it inside the planner's own call (no extra AI call)."""

from types import SimpleNamespace

from delta.core.planner import PLANNER_PROMPT, make_plan


class OneCallRouter:
    def __init__(self, reply: str) -> None:
        self.reply, self.calls = reply, 0

    async def complete(self, messages, **kw):
        self.calls += 1
        return SimpleNamespace(content=self.reply)


async def test_chat_reply_and_learning_come_from_one_call():
    router = OneCallRouter('{"needs_tools": false, "answer": "Nice, buddy! How is the launch going?", '
                           '"remember": ["User is building an AI agent called Delta", '
                           '"User wants to open Notepad", "User prefers short answers"]}')
    plan = await make_plan(router, "i'm launching my startup next month, keep answers short", [], "", "Delta")
    assert router.calls == 1 and not plan.needs_tools and plan.answer.startswith("Nice")
    # one-off requests and facts about Delta itself are dropped
    assert plan.remember == ["User prefers short answers"]


def test_planner_asks_for_partner_tone_and_free_learning():
    assert "trusted partner" in PLANNER_PROMPT and '"remember"' in PLANNER_PROMPT
    assert "never sugar-coated" in PLANNER_PROMPT and "cannot do" in PLANNER_PROMPT
    # the character rides in every planning call: keep it small
    assert len(PLANNER_PROMPT) < 7500
