# Delta architecture

## Design goals
1. **Light on the laptop.** One asyncio process, no Docker/Redis/Postgres. Heavy modules load lazily. Local model weights leave VRAM after `keep_alive`.
2. **Cheapest path first.** A request takes the cheapest route that can do it with proof: rules (0 tokens) → a learned recipe → one tool call → the full agent loop.
3. **Never stuck.** Every external dependency (models, search) has an ordered fallback chain with circuit breakers.
4. **Human in charge.** Nothing risky runs without approval; some things never run; any task can be stopped.
5. **Done means verified.** Completion is judged from evidence (tool proof, a final screenshot), never from the model's own claim.
6. **Replaceable parts.** Everything is wired in `bootstrap.py`; tests swap in fakes.

## Execution ladder
Every request walks down this ladder and stops at the first rung that can finish it with proof.

```
user text
 │
 ├─ 0. Ask first ─────────── core/clarify.py      missing meeting day/time, "12 am", reschedule without a time
 │                                                → one question with tap-able options        0 tokens
 ├─ 1. Instant command ───── core/intents.py      "pause the music", "start a Google Meet",
 │                                                "play X on YouTube", "stop chatting"          0 tokens
 ├─ 2. Skill replay ──────── core/replay.py       a proven skill's recorded tool calls,
 │                                                slots filled by one small call                ~400 tokens
 ├─ 3. Direct tool ───────── planner "direct"     one tool does the whole job and proves it
 │                                                (play_youtube, schedule_meeting, send_message) ~3k tokens
 └─ 4. Agent loop ────────── core/orchestrator.py plan → act → observe → verify → retry          as needed
```

A rung that cannot prove its result hands over to the next one **with what it already did**, so nothing is repeated.

## Components

| Module | Responsibility |
|---|---|
| `core/orchestrator.py` | the ladder above; the agent loop; run control (stop, checkpoints) |
| `core/clarify.py` | questions to ask before acting (0 tokens) |
| `core/intents.py` | strict instant-command rules (0 tokens) |
| `core/replay.py` | recipe validation, slot filling, replay of proven skills |
| `core/planner.py` | request → goal, steps, **checkpoints**, optional one-call `direct` path |
| `core/verifier.py` | judges each checkpoint against a fresh screenshot and the tool log |
| `core/proof.py` | per-run proof: screenshots, steps, verdicts; progress checkpoints; interrupted-run recovery |
| `llm/router.py` | per-task routes, main-AI preference, fallback, circuit breakers, token meter |
| `llm/providers.py` | OpenAI-compatible client (Ollama, LM Studio, Groq, NIM); JSON tool protocol for small models |
| `llm/cli_providers.py` | Claude Code / Codex as providers (the user's own plan); one live Claude session per task |
| `llm/capabilities.py` | what each brain can do; step budgets per brain |
| `tools/*` | typed tools (pydantic args) that declare risk, paths, domains, apps |
| `tools/registry.py` | `ToolExecutor`: strict args → policy → approval → run → audit. The only way a tool runs |
| `tools/media.py` | Windows media status/control, `play_youtube` (no AI, playback proven by Windows) |
| `tools/meetings.py` | Meet/Zoom/Teams; schedule, edit, cancel (Calendar API when connected, else the web page) |
| `messaging/*` | WhatsApp / Telegram / Slack drivers, human texting style, delegated chats |
| `integrations/google.py` | OAuth 2.0 (PKCE, loopback) and the Google Calendar API |
| `safety/*` | policy matrix, approvals broker, hash-chained audit log |
| `memory/*` | profile facts, task episodes, learned skills with recipes |
| `system/*` | governor (CPU/RAM/GPU/temps/battery), lifecycle, logging, secrets (Windows Credential Manager) |

## The agent loop (rung 4)
```
plan (router task="plan", Claude Code session prewarmed meanwhile)
loop attempts ≤ max_attempts:
  act steps ≤ budget (budget scales with the brain: frontier ×2, small local ×0.8):
     model call (one live session per task: only new messages are sent, the rest is cached)
     ├─ tool call → ToolExecutor → result
     │    └─ AFTER observation: read_window / browser_read, or a screenshot straight into the
     │       model's own conversation for pixel actions (no separate vision call)
     ├─ "DONE: …"      → leave the loop
     ├─ "NEED_USER: …" → ask the user (no retries burned)
     └─ anything else  → "continue" nudge (max 3), the loop never ends on a progress note
  fresh media status (playback tasks) + final screenshot of the window worked in
  verifier: every checkpoint met? → done │ else feedback → next attempt (same tools, cache kept)
```

## Verification and proof
- **Checkpoints, not actions.** The planner writes 2–5 end-state facts ("a new notebook is open"), never "clicked X".
- **Fresh evidence.** After `DONE:` Delta photographs the exact window it worked in (even if covered, via `PrintWindow`) and the verifier judges each checkpoint against it. The checkpoint verdicts decide; an overall flag cannot contradict them.
- **Hard proof beats pictures.** Tools that can prove their own result (`verified` evidence: Windows says *Playing*, Google returns the event, the message is seen in the chat) skip the verifier call.
- **Proof on disk.** `%LOCALAPPDATA%\Delta\runs\<task>\` keeps screenshots, `progress.json` (after every step), and `run.json`. The newest 30 runs are kept.

## Run control
- **Stop.** Every task runs as its own asyncio task; `POST /api/tasks/{id}/cancel` cancels it. Clean-up still runs: live CLI sessions closed, run record closed, audit entry written, its approval cards withdrawn.
- **Interrupted runs.** A run with progress but no `run.json` (Delta or the PC stopped) is offered once on the next start: *Resume* (with the finished steps listed, so nothing is repeated blindly) or *Dismiss*. Never resumed silently, because a half-done task may already have sent something.
- **Live checklist.** The plan's steps are shown as a checklist; each tool call ticks the step that names it; a passed verification ticks the rest. No tokens.

## Chats

The dashboard has separate chats, like Claude's sidebar (`memory/chats.py`, `chats.db` on this PC).
Each request carries a bounded recap of its own chat's last few turns (at most ~1.6k characters,
newest first, long messages clipped) as `TaskRequest.history`; the planner, the agent and the
verifier read `with_context`, while instant commands and skill matching read the bare request.
So follow-ups ("add 10 to that", "send it to him") work, a chat never sees another chat's turns,
and a long chat costs no more per request than a short one. Memory, learned skills, approvals
and the audit log are shared by all chats. Greetings and thanks are answered on the spot (0 tokens)
and never name a chat. A chat's work keeps running when you switch away; the sidebar shows a
pulsing dot while it works and a green dot when an answer is waiting.

## Token strategy
| Waste | Fix |
|---|---|
| a planner call for common commands | instant-command rules (0 tokens) |
| a whole agent loop for repeat jobs | skill recipes replayed with one small slot-fill call |
| 6 s and a cold prompt per Claude Code call | one live session per task, prewarmed; only new messages sent; prompt cache |
| a separate "describe the screen" call per step | the screenshot goes into the acting model's own conversation |
| re-checking what AFTER already showed | the model is told to reply DONE; the final check catches mistakes |
| a retry switching to all ~30 tool specs | same tool list on every attempt (session and cache survive) |
| hidden after-task "learn facts" calls | only when the user said something about themselves |
| big screenshots | one window only, 1024 px for steps, 1280 px for the final proof |
| invisible cost | every answer shows AI calls and tokens (input / cached / output) |

## Safety
**Risk classes** (declared by each tool, can depend on args, e.g. `write_file` over an existing file is DESTRUCTIVE, `run_shell` classifies the command, a batch of mouse steps is as risky as its riskiest step):

| Risk | ask-everything | ask-risky (default) | trusted |
|---|---|---|---|
| READ | allow | allow | allow |
| ACT (open app, click, type) | ask | allow | allow |
| WRITE | ask | ask | allow inside `allowed_paths`, else ask |
| DESTRUCTIVE | ask | ask | ask |
| EXTERNAL (sends data out: messages, invites) | ask | ask | ask |
| SYSTEM (OS/security/credentials/money) | **deny** | **deny** | **deny** |

Before the matrix: denied tools, paths, apps and domains are DENY; quiet hours turn every non-read into ASK. Deletes use the Recycle Bin. Unknown tool arguments are rejected (no silent drops).

**Messaging on the user's behalf.** One-time messages ask with the app, person and exact text. A delegated chat (`chat_for_me`) is approved once with its limits (who, app, minutes, mood, goal); each reply also asks unless the user turned that off; it never answers money, OTP, password, personal-detail or "are you a bot" messages (it hands the chat back); every sent message is audited; "stop chatting" ends it.

**The user at the keyboard wins.** Moving the mouse pauses desktop actions; Delta continues only after the mouse is still for 4 s, and stops if the user keeps working.

## Resource policy
| Health | Trigger (defaults) | Effect |
|---|---|---|
| NORMAL | — | local models allowed |
| THROTTLE | GPU ≥ 78 °C, VRAM ≥ 85 %, RAM ≥ 85 %, battery ≤ 30 % unplugged | local inference skipped → cloud |
| CRITICAL | GPU ≥ 87 °C, VRAM ≥ 95 %, RAM ≥ 93 %, battery ≤ 15 % unplugged | agent pauses, user told |

The daemon runs at BELOW_NORMAL priority. Local models unload after `keep_alive` and on shutdown; LM Studio is skipped when loading a model would exhaust RAM. Claude Code sessions (≈250 MB each) are capped at two and closed at the end of every task.

## Next
- Codex live session (same as Claude Code); Telegram/Slack official APIs so delegated chats run without using the screen.
- OAuth for Gmail and Outlook; Zoom and Teams APIs.
- A daily live run of `evals/` (YouTube, Meet, schedule, messaging) to catch site changes.
- Installer with auto-update; Tauri desktop app; remote access with device pairing.
