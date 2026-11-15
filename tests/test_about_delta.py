"""Delta knows its own version and recent features, from the changelog, and says so only when asked about itself."""

from delta.core.about import ABOUT, CARD_CHARS, about_delta, recent_changes
from delta.core.planner import hints_for

LOG = """# Changelog

## Unreleased

### Added
- not released yet

## 0.9.1 - 2026-01-02

Faster. In short:

- **Quick answers** (#12): one step
  instead of three.
- **Less memory** (#13).

### Changed
- a long entry that is not part of the summary

## 0.9.0 - 2026-01-01

### Fixed
- no summary lines here
"""


def test_the_card_is_built_from_each_release_summary():
    assert recent_changes(LOG) == [("0.9.1", ["Quick answers: one step instead of three.", "Less memory."])]
    card = about_delta()
    assert card.startswith("ABOUT DELTA (you): version ") and "Daily briefs" in card and len(card) <= CARD_CHARS
    assert "say you are not sure rather than inventing a feature" in card


def test_only_a_question_about_delta_gets_the_card():
    for q in ("what can you do", "what's new in Delta?", "how do I set up a daily brief", "which version are you",
              "can you also send results to Telegram"):
        assert ABOUT.search(q) and "ABOUT DELTA" in hints_for(q), q
    for q in ("open notepad", "what is the capital of Australia", "how do I make tea", "what can I cook with eggs"):
        assert not ABOUT.search(q) and "ABOUT DELTA" not in hints_for(q), q
