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
   ChatGPT: https://chatgpt.com/?hints=research      Gemini: https://gemini.google.com/app
   If the user named neither, use ChatGPT. If the page asks to sign in, stop and tell the user.
4. Turn on Deep Research: ChatGPT: open https://chatgpt.com/?hints=research (Deep research already on;
   check a "Deep research" chip shows by the message box), else the "+" menu -> "Deep research".
   Gemini: the Tools (or mode) menu -> "Deep Research". Read the page to find the current names; they change.
5. Put the whole brief in the message box with browser_fill (one step, not typed key by key) and send it.
   Gemini first shows a research plan: click "Start research".
6. STARTED = SUCCESS: once your brief shows as a sent message and the app is answering (a Stop button, "Stop
   generating"/"Stop streaming", "Researching", "Thinking", sources being read, a research plan, or a reply
   with headings appearing), it has started: go straight to step 8. Never send the brief again, never open
   another chat, never type in the address bar, and do not wait for the reply to finish.
7. SELF-HEAL, only when it clearly did NOT start: read the page after sending and after Start research. If it
   shows an error ("something went
   wrong", "try again", "network error", "too many requests", a stopped response) or nothing changes in ~30 s:
   a) click Retry / Try again (or Regenerate) once and read again;
   b) still failing: open a NEW chat (Gemini https://gemini.google.com/app, ChatGPT https://chatgpt.com/),
      turn Deep Research on again and send the SAME brief again (do not rewrite it);
   c) still failing: use the other app (Gemini <-> ChatGPT) with the same brief;
   d) only after these three tries, reply NEED_USER with what each try showed. Never claim it started unless
      the page shows it researching.
8. Reply DONE: which app, that results take about
   5-30 minutes and appear in that chat, and the brief you sent (so the user sees what was asked). Do not
   wait for the research to finish."""


EMERGENCY = re.compile(r"\b(emergency|sos|not feeling (well|good)|unwell|feel(ing)? (sick|dizzy|faint|very bad)|"
                       r"chest pain|can'?t breathe|breathless|fainted|fell down|accident|bleeding|ambulance|"
                       r"kill myself|suicid\w*|end my life|hurt myself)\b", re.I)

EMERGENCY_PLAYBOOK = """

PLAYBOOK: THE USER MAY NEED HELP (health, safety, distress)
- Stay calm, warm and SHORT. Their safety comes before any task.
- Life-threatening signs (chest pain, trouble breathing, fainting, heavy bleeding, stroke signs, an accident,
  or any thought of hurting themselves): tell them FIRST to call 112 now (India's emergency number; another
  country: its own). For thoughts of self-harm also give Tele-MANAS 14416 (free, 24x7) and stay with them.
- Offer to alert their emergency contacts with alert_emergency_contacts (always this tool for them, never
  send_message) (WhatsApp message in their name,
  optionally a WhatsApp call). Write the message as the user, plainly: what is happening and what they need.
  The tool always asks the user before sending; never claim anyone was told unless the tool says sent.
- If it is unclear how serious it is, ask ONE quick question (NEED_USER) with options, e.g.
  "- Alert my emergency contacts now" / "- Call one of them on WhatsApp" / "- I'm okay, just stay with me".
- No emergency contacts saved: say how to add them (Delta → Settings → Emergency contacts) and give 112.
- Never diagnose; simple first steps are fine (sit down, unlock the door, keep the phone close)."""


def playbooks_for(request: str) -> str:
    """The playbooks this request needs (empty for everyday requests)."""
    found = DEEP_RESEARCH_PLAYBOOK if DEEP_RESEARCH.search(request) else ""
    return found + (EMERGENCY_PLAYBOOK if EMERGENCY.search(request) else "")
