"""Playbooks: expert know-how for one kind of task, added to the agent's instructions only when a request
needs it, so everyday requests stay short and cheap.

DEEP_RESEARCH: the user wants their own signed-in ChatGPT or Gemini to run a Deep Research. Delta asks
what it must know, writes the research brief the way prompt engineers at AI labs do, then starts it in
the app (the user's browser profile, their plan) and reports back."""

from __future__ import annotations

import re

DEEP_RESEARCH = re.compile(r"\bdeep[\s-]?research\b|\bresearch\b.*\b(chatgpt|gemini|gpt)\b|"
                           r"\b(chatgpt|gemini)\b.*\bresearch\b", re.I | re.S)

DEEP_RESEARCH_PLAYBOOK = """

PLAYBOOK: DEEP RESEARCH IN THE USER'S CHATGPT OR GEMINI
1. Understand first. If the topic's purpose, scope, region/timeframe or output format is unclear, ask ONE
   question form (NEED_USER, up to 4 questions, tick boxes for what to cover) before opening anything. Use
   what the chat already tells you; never ask what you can infer.
2. Write the research brief like an expert prompt engineer (this is what makes the result excellent):
   - Role: "You are a senior research analyst specialising in <domain>."
   - Objective: what to find out, for whom, and the decision it supports.
   - Context: every fact the user gave (their situation, constraints, location, budget, audience).
   - Scope: what to include and exclude; region; timeframe ("as of <today's date>").
   - Key questions: 5-10 numbered, specific questions the research must answer.
   - Evidence standards: prefer primary and official sources and recent data; cite every claim with a link;
     note conflicting evidence and uncertainty; never invent figures or sources.
   - Output: executive summary first; clear sections; comparison tables where useful; key numbers; risks and
     open questions; concrete recommendations; full source list.
   Written in the user's language, specific to their request, no filler.
3. Open the app in the user's own browser (signed in with any account is fine):
   ChatGPT: https://chatgpt.com/      Gemini: https://gemini.google.com/app
   If the user named neither, use ChatGPT. If the page asks to sign in, stop and tell the user.
4. Turn on Deep Research: ChatGPT: the tools / "+" menu of the message box -> "Deep research".
   Gemini: the Tools (or mode) menu -> "Deep Research". Read the page to find the current names; they change.
5. Put the whole brief in the message box with browser_fill (one step, not typed key by key) and send it.
   Gemini first shows a research plan: click "Start research".
6. Check it started (the page shows it researching), then reply DONE: which app, that results take about
   5-30 minutes and appear in that chat, and the brief you sent (so the user sees what was asked). Do not
   wait for the research to finish."""


def playbooks_for(request: str) -> str:
    """The playbooks this request needs (empty for everyday requests)."""
    return DEEP_RESEARCH_PLAYBOOK if DEEP_RESEARCH.search(request) else ""
