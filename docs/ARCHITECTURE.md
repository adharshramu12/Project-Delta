# Delta architecture

## Design goals
1. **Light on the laptop.** One asyncio process, no Docker/Redis/Postgres. Heavy modules load lazily. Local model weights leave VRAM after `keep_alive`.
2. **Never stuck.** Every external dependency (models, search) has an ordered fallback chain with circuit breakers.
3. **Human in charge.** Nothing risky runs without approval; some things never run.
4. **Done means verified.** Completion is judged from tool evidence by a separate verifier pass.
5. **Replaceable parts.** Everything is wired in `bootstrap.py`; tests swap in fakes.

## Components

| Module | Responsibility |
|---|---|
| `core/orchestrator.py` | plan → act → observe → verify → reflect loop |
| `core/planner.py` | request → goal, steps, **checkable success criteria**; direct answers for chit-chat |
| `core/verifier.py` | strict judge over the tool log; the actor's claims are not evidence |
| `llm/router.py` | per-task provider routes, fallback, circuit breakers, governor-aware |
| `llm/providers.py` | one OpenAI-compatible client for Ollama / Groq / NIM; JSON tool protocol for small local models |
| `tools/*` | typed tools (pydantic args) that declare risk, paths, domains, apps |
| `tools/registry.py` | `ToolExecutor`: validate → policy → approval → run → audit. The only way a tool runs |
| `safety/policy.py` | hard floor ∩ `boundaries.yaml` → ALLOW / ASK / DENY |
| `safety/approvals.py` | broadcasts approval requests to every channel; first answer wins; timeout = deny |
| `safety/audit.py` | hash-chained JSONL; `delta audit verify` detects edits |
| `memory/*` | SQLite + FTS5: profile facts and task episodes, auto-extraction, user-editable |
| `system/governor.py` | samples CPU/RAM/GPU/VRAM/temps/battery; NORMAL / THROTTLE / CRITICAL |
| `system/lifecycle.py` | single instance, ordered shutdown hooks, kills child processes |

## Request flow
```
user text ─► Container.submit (1 heavy task at a time)
   └► Orchestrator.run
        ├─ memory.context_for(text)
        ├─ planner  ── router(task="plan")
        ├─ loop ≤ max_attempts:
        │    ├─ act ≤ max_steps: router(task="chat", tools) → ToolExecutor.execute
        │    │        (governor CRITICAL → pause and report)
        │    └─ verifier ── router(task="verify") → pass ⇒ done | fail ⇒ feedback, retry
        ├─ audit + events (task.plan / task.step / task.verify / task.finished)
        └─ memory: episode + extracted profile facts
```

## Safety
**Risk classes** (declared by each tool, can depend on args — e.g. `write_file` over an existing file is DESTRUCTIVE, `run_shell` classifies the command):

| Risk | ask-everything | ask-risky (default) | trusted |
|---|---|---|---|
| READ | allow | allow | allow |
| ACT (open app, click, type) | ask | allow | allow |
| WRITE | ask | ask | allow inside `allowed_paths`, else ask |
| DESTRUCTIVE | ask | ask | ask |
| EXTERNAL (sends data out) | ask | ask | ask |
| SYSTEM (OS/security/credentials/money) | **deny** | **deny** | **deny** |

Before the matrix: denied tools, denied paths (even for reads), denied apps and domains are DENY; quiet hours turn every non-read into ASK. Deletes use the Recycle Bin.

## Resource policy
| Health | Trigger (defaults) | Effect |
|---|---|---|
| NORMAL | — | local models allowed |
| THROTTLE | GPU ≥ 78 °C, VRAM ≥ 85 %, RAM ≥ 85 %, battery ≤ 30 % unplugged | local inference skipped → cloud |
| CRITICAL | GPU ≥ 87 °C, VRAM ≥ 95 %, RAM ≥ 93 %, battery ≤ 15 % unplugged | agent pauses, user told |

The daemon runs at BELOW_NORMAL priority. Ollama models unload after `keep_alive` and on shutdown.

## Planned components
- `api/` FastAPI + WebSocket on 127.0.0.1 — one API for the Tauri desktop app, remote PWA and tests.
- `apps/desktop/` Tauri v2 + React: one-click start, tray, approvals, memory & boundaries editors, full shutdown.
- `channels/telegram.py`, `channels/slack.py` (Socket Mode) — approvals via inline buttons, allow-listed user IDs.
- Remote: Tailscale or Cloudflare Tunnel, device pairing with TOTP, kill switch.
- Computer use (built, core install): pywinauto UIA tree first, Unicode SendInput typing, mss + vision model when needed; Playwright over CDP to Delta's own profile in the installed Chrome, else Edge.
- Voice: openWakeWord → faster-whisper → Piper; Vapi adapter.
