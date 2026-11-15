"""Ready-made daily briefs are ordinary schedules: each one parses, repeats daily and asks for dated, sourced items."""

from datetime import UTC, datetime

from delta.scheduler.briefs import BRIEFS
from delta.scheduler.parse import parse_schedule

NOW = datetime(2026, 11, 16, 6, 0, tzinfo=UTC)


def test_every_brief_is_a_repeating_task_with_its_whole_prompt():
    assert {b.id for b in BRIEFS} == {"markets", "ai_tech", "gmail"}
    for b in BRIEFS:
        p = parse_schedule(b.text, "Asia/Kolkata", NOW)
        assert p.kind == "task" and p.rule.type in ("daily", "weekly", "cron"), b.id
        assert len(p.prompt) > 150 and p.prompt[-25:].rstrip(".") in b.text, b.id  # nothing cut off by the time parser
        assert "—" not in b.text


def test_briefs_ask_for_real_dated_news_and_the_markets_one_never_advises():
    by = {b.id: b.text for b in BRIEFS}
    for key in ("markets", "ai_tech"):
        assert "source" in by[key] and "date" in by[key] and "never fill in from memory" in by[key]
    assert "no buy or sell advice" in by["markets"]
    assert "Do not open links, reply, delete or mark anything" in by["gmail"]
