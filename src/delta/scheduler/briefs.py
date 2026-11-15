"""Ready-made daily briefs: one tap in Schedules fills the box with a schedule the user can edit before saving.

Each brief is an ordinary schedule in plain words (delta.scheduler.parse reads it like any typed one), so it runs
through the same agent, approvals, backup AIs and delivery as every other schedule. The wording does the work:
it asks for dated items from the last day with their sources, in a fixed short format, so the answer is real,
current and small. The markets brief is information only: Delta never trades and gives no buy or sell advice
(delta.safety.trading).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

_FRESH = ("Use only items dated in the last day, check each date against the current date, and give the source "
          "and date of every item. If nothing new is found, say so; never fill in from memory.")


@dataclass(frozen=True)
class Brief:
    id: str
    title: str
    about: str
    text: str  # what goes into the "New schedule" box

    def public(self) -> dict[str, str]:
        return asdict(self)


BRIEFS: tuple[Brief, ...] = (
    Brief("markets", "Markets brief", "Market news and movers every weekday morning. Information only.",
          "Every weekday at 8:30 am give me my markets brief: search the web for the latest news on Indian and "
          "global markets (Nifty, Sensex, big movers, company results, RBI and policy news). " + _FRESH +
          " Format: 5 to 8 short bullets, most important first, then one line on what to watch. Facts only: no "
          "buy or sell advice."),
    Brief("ai_tech", "AI and tech brief", "New AI models, tools, launches and research from the last day.",
          "Every day at 9 am give me my AI and tech brief: search the web for new AI models and tools, major "
          "launches, funding and notable research. " + _FRESH +
          " Format: 5 to 8 short bullets, most important first, each with its link."),
    Brief("gmail", "Gmail summary", "Unread mail grouped by what needs you. Nothing is opened, sent or deleted.",
          "Every day at 8 am summarise my Gmail: open Gmail in the browser and read the unread mail of the last "
          "day. Group it: needs my reply, security or account alerts, important information, promotions (a count "
          "only). One line per mail with the sender. Do not open links, reply, delete or mark anything. If Gmail "
          "asks to sign in, tell me instead."),
)
