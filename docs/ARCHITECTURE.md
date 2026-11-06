# Delta architecture

*Delta is created and maintained by Adharsh Kumar Bachu.*

## Design goals
1. **Light on the laptop.** One asyncio service process (~100 MB idle, near-zero CPU, no GPU unless a local model is configured), no Docker/Redis/Postgres. Heavy modules load lazily. Local model weights leave VRAM after `keep_alive`.
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
| `core/planner.py` | request → goal, steps, **checkpoints**, optional one-call `direct` path; chat replies in Delta's partner voice and the facts to `remember`, in the same call |
| `core/questions.py` | tick-box and pick-one question forms (up to 4 questions) for the app and Telegram |
| `core/playbooks.py` | expert know-how added only to matching requests (Deep Research in ChatGPT/Gemini, with self-healing) |
| `core/verifier.py` | judges each checkpoint against a fresh screenshot and the tool log |
| `core/proof.py` | per-run proof: screenshots, steps, verdicts; progress checkpoints; interrupted-run recovery |
| `llm/router.py` | per-task routes, main-AI preference, fallback, circuit breakers, token meter |
| `llm/providers.py` | OpenAI-compatible client (Groq, NVIDIA NIM, DeepSeek, LM Studio, Ollama); JSON tool protocol for small models |
| `llm/cli_providers.py` | Claude Code, Codex (app-server, lean mode) and Cline as providers: the user's own plan; their own tools are off or read-only |
| `llm/capabilities.py` | what each brain can do; step budgets per brain |
| `tools/*` | typed tools (pydantic args) that declare risk, paths, domains, apps |
| `tools/registry.py` | `ToolExecutor`: strict args → policy → approval → run → audit. The only way a tool runs |
| `tools/browser.py` | Delta's own browser profile (Playwright over CDP): numbered element refs, act-and-observe listings, settle waits driven by network and DOM events |
| `tools/snapshot.py` | after an action on the same page, only the changed elements (`+` new, `~` changed, `-` gone); full listing after a navigation, every 8 actions, or when the diff is not smaller |
| `llm/usage.py` | per-task token meter: reported usage, an estimate (labelled) when an API gives none, "not reported" for CLI providers |
| `tools/user_browser.py` | the user's own signed-in Chrome/Edge through UI Automation: read, click (real pointer for web menus), fill (Shift+Enter newlines, paste for long text) |
| `tools/browser_choice.py` | the four-way browser choice (My default browser, Chrome, Edge, Delta's private browser) mapped onto `browser.mode` / `browser.prefer` / `browser.private_engine`; installed-browser detection (file checks and the Windows default, nothing launched); the sentences that mean a lasting or one-task switch |
| `tools/browser_switch.py` | `BrowserHub`: both browser kits are built at start-up; the registry holds one stand-in per browser tool that hands each call to the kit the current task uses. Each task is pinned at start (`Orchestrator.run`), so a switch never lands mid-task. `set_browser` (save, or this task only) and `open_signin_browser` ("Sign in to my sites") |
| `tools/pc.py` | volume, brightness, Windows light/dark theme, screenshot, webcam; pictures go straight to a remote requester |
| `tools/deliver.py` | `send_file` back to Telegram/Slack (Remote Guard PIN checked) |
| `tools/emergency.py` | encrypted emergency contacts; `alert_emergency_contacts` (WhatsApp message + optional voice call) |
| `tools/media.py` | Windows media status/control, `play_youtube` (no AI, playback proven by Windows) |
| `tools/meetings.py` | Meet/Zoom/Teams; schedule, edit, cancel (Calendar API when connected, else the web page) |
| `messaging/*` | WhatsApp / Telegram / Slack drivers, human texting style, delegated chats |
| `integrations/google.py` | OAuth 2.0 (PKCE, loopback) and the Google Calendar API |
| `addons/writer/*` | Writer and Documents: `write_post` (writing rules checked in code, one rewrite), `prepare_post` (clipboard + compose page, never publishes), `make_document` (layout from content; PDF via headless Edge/Chrome with JS and network off, DOCX written with the standard library, Markdown, optional LaTeX without shell escape) |
| `api/documents_routes.py` | list, preview and download documents in the dashboard (token-protected, listed files only) |
| `safety/*` | policy matrix, approvals broker, hash-chained audit log, **Remote Guard** (PIN, unlock window, lockout) |
| `channels/telegram.py`, `channels/pc_menu.py` | one-time-code pairing, commands, voice notes, question forms, PC controls, Remote Guard checks |
| `memory/*` | profile facts, task episodes, learned skills with recipes |
| `system/*` | governor (CPU/RAM/GPU/temps/battery), lifecycle, logging, secrets (Windows Credential Manager), PC control, notifications |
| `daemon.py` | the background service, the lean app window, leftover-window cleanup, full shutdown |
| `scheduler/*` | scheduled tasks and reminders: rules and next-run maths, plain-words parsing, SQLite store, the loop, agent tools, `delta schedule` |

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
- **Proof on disk.** `%LOCALAPPDATA%\Delta\runs\<task>\` keeps screenshots, `progress.json` (after every step), and `run.json`. The newest 30 runs are kept; pruning never deletes the run in progress or any run touched in the last 30 minutes.

## Failure handling
- A task always ends with a result. Any exception in a task (not only an AI error) becomes a *failed* task with a plain message (details in the log and the audit log as `task.error`); the run record is closed, the UI is told and the one-task lock is released. A Stop is cleaned up the same way and still ends as *cancelled*.

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
| hidden after-task "learn facts" calls | facts come back in the planner's own JSON (`remember`): no extra call |
| greetings and thanks | canned replies, 0 tokens |
| expert know-how in every prompt | playbooks are added only to requests that need them |
| typing long text key by key | pasted in one go (the user's clipboard is restored) |
| big screenshots | one window only, 1024 px for steps, 1280 px for the final proof |
| invisible cost | every answer shows AI calls, tokens (reported, estimate, or "not reported" for CLI AIs) and wall time; also in `GET /api/tasks/{id}` (`usage`, `wall_s`) and the run's `run.json` |
| a runaway task | optional `agent.token_budget` (default 0 = off): past it, Delta pauses and asks "Keep going / Stop here" |
| the whole page listing after every browser action | on the same page only the changes are sent (`browser.snapshot_diffs`, on by default; `browser.full_snapshot_every`, default 8) |
| old page listings in the history | a listing superseded by a newer full one becomes a one-line stub; the latest full listing and the diffs after it stay whole; older results keep their page text, not stale refs |
| element lines sent twice | the AFTER page read drops element lines the model already holds in its listing |
| fixed waits after browser actions | settle ends when the network and the DOM have been quiet briefly (200 ms / 150 ms), capped at the old 1.5 s; no 500 ms "networkidle" floor; a page restored by Back from the browser's back-forward cache no longer waits out a 10 to 15 s load-event timeout |
| screenshots in browser tasks | not sent: browser steps are checked from the page text and element listing; screenshots are kept for pixel actions and as a fallback when no page read exists |

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

## Scheduled tasks

`delta.scheduler` keeps schedules in `schedules.db` (SQLite, Delta's data folder): the task text, a rule
(once, daily, weekly, every N minutes or hours with a 5 minute floor, or a 5-field cron line), a time zone
(empty = the PC's own), who made it (channel and user), the permission level at that moment, enabled, next
run, last run and status, and a bounded run history.

- **Rules** (`rules.py`) are kept as local wall-clock times, so a 9 AM run stays at 9 AM across daylight saving.
  A time inside a DST gap runs right after the jump; a repeated time runs once (the first). Intervals count
  real elapsed time. Only the computed next run is stored, in UTC. Named zones use the standard library's
  `zoneinfo` (Windows gets the zone data from the `tzdata` package already in the lockfile); a zone that
  cannot be loaded falls back to the PC's own instead of failing.
- **Parsing** (`parse.py`) turns plain words into a rule with regular expressions, no AI. Anything unclear raises
  a question back ("What time should it run?"). `core/intents.py` sends sentences that start like a schedule
  ("remind me", "every", "daily") straight to `schedule_task`; the planner knows the tool for the rest.
- **The loop** (`service.py`) starts in `delta run` after the chat channels and stops before them. It wakes for
  the earliest next run, at least every 20 seconds (which also notices a wake from sleep), and when a
  schedule changes. For each due schedule: skip if the same schedule is still running (recorded as skipped);
  if it is later than the grace window (default 1 hour) record "missed" and move on; during quiet hours hold
  task runs (reminders still go out) and run them once when quiet hours end; otherwise move `next_run`
  forward first (so a crash never repeats a run) and start it. Task runs go through `Container.submit`, the
  normal Orchestrator / ToolExecutor / ApprovalBroker path, as the schedule's own channel and user, behind a
  semaphore (`max_concurrent`, default 1). Reminders skip the AI and only deliver the message.
- **Permission level**: `Container.submit(autonomy_cap=...)` swaps in a policy at the lower of the schedule's
  saved level and today's level for that one task (tasks run one at a time), then rebuilds today's policy.
  Nothing can raise it.
- **Delivery**: Telegram and Slack get the result through their `send_text`; everything also goes to the
  dashboard (a `schedule.result` event, the **Scheduled tasks** chat, a Windows notification when no phone
  channel took it).
- **Surfaces**: agent tools `schedule_task` (confirms the parsed schedule once at every permission level),
  `list_schedules`, `pause_schedule`, `resume_schedule`, `delete_schedule`, `run_schedule_now`; API routes in
  `api/schedule_routes.py` (dashboard token); the Schedules panel (`static/schedules.js`); `delta schedule`.

## Processes and lifecycle
```
double-click icon ─► launcher (pythonw) ─► service: python -m delta run   (hidden, ~100 MB)
                                         └► window:  Edge --app on Delta's own profile (UI only)
```
- **Close (X)** closes only the window. The service keeps running, so a Telegram message still reaches
  Delta. Edge's leftover processes for that window are freed within 60 s.
- **Double-click** again opens the window (never a second service: single-instance lock).
- **Shutdown** stops channels, helpers (Claude Code, Codex, Cline, local models), the service and the
  window. Nothing keeps listening.

## Browser choice
One choice, changed from anywhere (dashboard Settings, Telegram `/browser`, or chat through `set_browser` and an
instant intent for whole-message switches like "use Edge from now on"), all through `control.set_browser` /
`browser_switch.save_choice`: validated against what is installed, saved to the user's settings file, audited
(`settings.changed` with who changed it). The executor sees the real browser tool behind each stand-in (its
arguments, risk, domains, password and captcha refusals), so policy, approvals, ExfilGuard and Remote Guard are
unchanged. Each task is pinned to its browser when it starts; a saved switch applies from the next task, except
for the task that asked for it when it has not opened a browser yet. A browser named inside a request ("open
Gmail in Chrome") is a choice for that task only. The private browser's engine changes only between tasks (Delta
closes the browser it started, then opens the new one on first use). "Sign in to my sites" opens Delta's private
browser visibly on a page the user picks and keeps it open 30 minutes; Delta never types there.

## Remote control (Telegram)
- **Pairing:** a single-use 8-character code shown only on the PC (10 minutes) binds one Telegram account;
  all others are ignored.
- **Remote Guard** (`safety/remote_guard.py`), enforced centrally for every remote channel (anything but
  the terminal and the dashboard): `Container.submit` refuses remote tasks while locked; `ToolExecutor`
  needs the owner's PIN for sensitive reads and code runs (screen, webcam, windows, files, messages,
  clipboard, shell, Python; listing outside allowed folders) and runs nothing remote while locked;
  `ApprovalBroker.answer` turns a locked remote answer into a logged *deny* and needs the PIN to approve
  deletes and sends from a phone. The Telegram menu also needs the PIN for camera, screenshot, closing apps,
  keys, Autopilot, switching the browser (`/browser`) and opening the sign-in browser (`/signin`). PIN: scrypt hash, 15-minute unlock. 5 wrong PINs lock remote control until someone at
  the PC unlocks it; `/lock` locks at once. Full list in SECURITY.md.
- Every remote request shows a Windows notification and is audited. Results (pictures, files) are sent
  back into the chat.

## Prompt-injection containment and encryption
- **Taint guard** (`tools/registry.py`): the first time a task reads outside content (`browser_read`,
  `fetch_page`, `web_search`, `read_file`, `read_messages`, `read_window`, `look_at_screen`) the task is
  marked. From then on EXTERNAL and DESTRUCTIVE actions, code (`run_shell`, `run_python`), `send_file`,
  messages and `remember` turn into an approval, overriding autonomy and learned trust. In-page browser
  steps are exempt. The mark is cleared when the task ends. WRITE-level desktop input (Enter, Alt+F4) is
  taint-guarded too, except in Autopilot; `safety/exfil_guard.py` still asks for input into terminals, the
  Run box and Delta's own window at every level, and for addresses carrying encoded data after a read (its
  new-site rule is off in Autopilot).
- **Allow for this task** (`ToolExecutor.task_grant_key`): an approval may offer a third answer that covers
  the same tool, risk and target class (site, app, folder) for the rest of the task. Grants live in memory,
  are dropped in `forget_task`, and are never offered for DESTRUCTIVE/SYSTEM risk, code or memory tools,
  ExfilGuard's strict kinds (terminal, Delta window, unchecked target, encoded data), always-asked actions,
  or remote requests whose approval needs the PIN. `ApprovalBroker.answer` approves once only when a phone
  picks it for an action that needs the PIN.
- **Vault** (`system/vault.py`): memory contents and chat titles/messages are sealed with DPAPI
  (app-specific entropy). Old plain rows are sealed on start and the file is vacuumed; the plain-text
  FTS index was removed, and recall scores keywords in Python (memories are few, so this is fast).

## Emergency help
- **Contacts:** up to 5, saved from Settings, stored in `emergency.json` sealed with DPAPI.
- **Help mode:** `core/playbooks.py` detects distress ("not feeling well", "chest pain", "SOS", self-harm
  phrases) and adds the help playbook: stay calm, 112 first when serious (Tele-MANAS 14416 for self-harm
  thoughts), offer to alert contacts, never claim anyone was told unless the tool says sent.
- **Tool:** `alert_emergency_contacts` sends through the WhatsApp desktop driver (contact confirmed before
  typing) and can press WhatsApp's voice-call button through UI Automation.
- **Consent (policy):** the alert tool, and any message/chat/file during a help request, needs a human yes in
  every autonomy level, unless the user's own message clearly says to send/call/alert/tell someone. The same
  works from Telegram, where approval cards have Approve/Deny buttons.

## Resource policy
| Health | Trigger (defaults) | Effect |
|---|---|---|
| NORMAL | — | local models allowed |
| THROTTLE | GPU ≥ 78 °C, VRAM ≥ 85 %, RAM ≥ 85 %, battery ≤ 30 % unplugged | local inference skipped → cloud |
| CRITICAL | GPU ≥ 87 °C, VRAM ≥ 95 %, RAM ≥ 93 %, battery ≤ 15 % unplugged | agent pauses, user told |

The governor samples every 15 s and opens the NVIDIA driver only when a local model is configured, so a laptop's dedicated GPU can sleep. The default thread pool is capped at 4. The app window runs without GPU, extensions, sync or background mode. The daemon runs at BELOW_NORMAL priority. Local models unload after `keep_alive` and on shutdown; LM Studio is skipped when loading a model would exhaust RAM. Claude Code sessions (≈250 MB each) are capped at two and closed at the end of every task.

## Next
- Slack commands and PIN entry (Slack already gets the same Remote Guard lock and PIN rules).
- Scheduled tasks and routines.
- End-to-end encrypted channel (Matrix), signed releases.
- OAuth for Gmail and Outlook.
- A daily live run of `evals/` (YouTube, Meet, Deep Research, messaging) to catch site changes.
