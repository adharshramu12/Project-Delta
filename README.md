# Δ Delta

**A personal, local-first, OS-level AI agent for Windows.**
Talk to it from the terminal, Telegram, Slack or your voice. It plans, uses your computer,
asks before anything risky, and **verifies every task before it says "done".**

> Status: `v0.1` — foundation and agent core are working (model routing, safety, tools,
> memory, web search, verify loop, CLI). Desktop app, channels, computer-use and voice are
> next on the [roadmap](#roadmap).

## Why Delta is different

| | |
|---|---|
| **Runs on a laptop** | Local Gemma via Ollama on a 6 GB GPU. Models unload after 2 idle minutes. Daemon runs at below-normal priority. |
| **Never stuck** | Model router falls back Ollama → Groq → NVIDIA NIM with circuit breakers. Web search falls back Tavily → SerpAPI → DuckDuckGo (free). |
| **Protects the hardware** | Governor watches GPU temp, VRAM, RAM and battery. Over the limit → local inference stops and work moves to the cloud; at critical → Delta pauses. |
| **You stay in charge** | Every action is risk-classified. Writes, deletes and anything that leaves the machine need your approval. Deletes go to the Recycle Bin. System settings, credentials and payments are hard-blocked. |
| **Your rules** | `config/boundaries.yaml`: allowed/denied folders, apps, tools, domains, quiet hours, autonomy level. |
| **Verified, not hopeful** | Plan → act → **verify against explicit success criteria** → retry or report honest failure. |
| **Learns your workflows** | Every verified multi-step task becomes a reusable *skill*. Skills are promoted only after they work again, and retired automatically if they keep failing. You can see and remove every one. |
| **Extensible** | Add-ons via a small stable SDK, plus a built-in **MCP** bridge to any Model Context Protocol server. Add-on tools obey the same safety rules. See [docs/ADDONS.md](docs/ADDONS.md). |
| **Remembers you** | Local memory of your preferences and past tasks. View, edit, delete any time (`/memory`). |
| **Auditable** | Hash-chained, tamper-evident audit log of every decision and tool call. |

## Architecture

```
 CLI / Telegram / Slack / Voice / Desktop app / Remote PWA
                     │
                     ▼
              Orchestrator ── plan → act → observe → verify → reflect
               │      │                                   │
         Memory│      ▼                                   ▼
               │  ToolRegistry ──► PolicyEngine ──► ApprovalBroker (any device)
               │      │              (floor ∩ your boundaries)
               │      ├─ files · shell · apps · web search · fetch
               │      └─ desktop · screen · browser · MCP   (roadmap)
               ▼
          ModelRouter ── Ollama ▸ Groq ▸ NVIDIA NIM
                     ▲
     Governor (CPU/RAM/GPU/temps/battery)        AuditLog (hash chain)
```

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quick start (Windows 11)

```powershell
# 1. tools
pip install uv
winget install Ollama.Ollama
ollama pull gemma3n:e4b          # or the Gemma tag you prefer; set it in config/delta.yaml

# 2. install
uv sync --extra all        # everything; plain 'uv sync' gives the core agent + dashboard

# 3. cloud fallback keys (optional but recommended) -> stored in Windows Credential Manager
uv run delta secrets set groq_api_key
uv run delta secrets set nvidia_api_key
uv run delta secrets set tavily_api_key

# 4. check and talk
uv run delta doctor
uv run delta cli
```

### One-click app
```powershell
uv sync --extra all
powershell -File scripts/create_shortcut.ps1   # adds "Delta" to Desktop + Start menu
```
Click **Delta** → it starts in the background and opens the dashboard. **Shutdown** in the
dashboard stops everything and frees RAM/VRAM. Or run `uv run delta run --open` yourself.

### Telegram / Slack
```powershell
uv run delta secrets set telegram_bot_token      # from @BotFather
uv run delta secrets set slack_bot_token         # xoxb-…
uv run delta secrets set slack_app_token         # xapp-… (Socket Mode)
```
Then in `config/delta.yaml` set `enabled: true` and add your own user id to `allowed_user_ids`
(an empty list means nobody can use the bot).

In the CLI: just type a task. Commands: `/status`, `/memory`, `/remember <fact>`,
`/forget <id>`, `/audit`, `/quit`.

## Configuration

| File | What |
|---|---|
| `config/delta.yaml` | providers, routes, governor limits, memory, channels |
| `config/boundaries.yaml` | what Delta may touch and how autonomous it is |
| `config/apps.yaml` | friendly app names → executables (Codex, Antigravity, Explorer, …) |

Runtime data (memory DB, audit log, logs) lives in `%LOCALAPPDATA%\Delta`.

## Roadmap

- [x] Model router with fallback + circuit breakers
- [x] Governor + hardware guard, clean shutdown
- [x] Policy engine, boundaries, approvals, audit log
- [x] Tools: files, shell, apps, web search, page fetch
- [x] Memory (profile + episodic, FTS recall)
- [x] Plan → act → verify loop, CLI
- [x] Background service + local API (FastAPI/WebSocket, token auth, localhost only)
- [x] Web dashboard: chat, live steps, approvals, memory, health, one-click Shutdown
- [x] Desktop shortcut (no console window; second click reopens the dashboard)
- [x] Telegram + Slack with approve/deny buttons (allow-listed users only)
- [x] Computer use (in every install): UI Automation, Unicode typing, user-takeover stop, screen vision,
      browser via Delta's own Chrome or Edge profile (free browsing; send/buy/delete still ask)
- [x] Add-on SDK + MCP bridge
- [x] Skill learning from verified tasks (candidate → proven → retired), export/import
- [ ] Preference learning from approvals/corrections, reflection lessons
- [ ] Routines and proactive suggestions
- [ ] First-run setup page, one-line installer, community skill library
- [ ] Tauri desktop app with tray icon (wraps the same dashboard)
- [ ] Remote control (Tailscale / Cloudflare Tunnel, device pairing, PWA)
- [ ] Voice: wake word, faster-whisper, Piper, Vapi
- [ ] Semantic memory (sqlite-vec), memory encryption at rest, MCP tools

## Contributing / Security

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). Licensed under Apache-2.0.
