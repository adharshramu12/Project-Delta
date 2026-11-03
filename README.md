<div align="center">

# Δ Delta

### The personal AI agent that actually does the work on your Windows PC

**Created by [Adharsh Kumar Bachu](https://github.com/adharshramu12)** · Open source · Apache 2.0

Talk to it like a friend. Control it from your phone. It plans, uses your computer and browser,
asks before anything risky, and **verifies every task before it says "done".**

`Codex` · `Claude Code` · `Cline` · `NVIDIA NIM` · `Groq` · `DeepSeek` · `LM Studio` · `Ollama`

</div>

---

## Why people choose Delta

|  |  |
|---|---|
| 🧠 **Bring the AI you already have** | Use your **ChatGPT/Codex plan** (free or paid), your **Claude Code** subscription, **Cline**, or free API keys from **NVIDIA NIM** and **Groq**. Local models (LM Studio, Ollama) work too. No new subscription needed. |
| 🔀 **Hybrid, never stuck** | Delta routes every request to the best available AI and **falls back automatically** when one is down, rate-limited or out of credits. Switch the main AI, model or API key any time, even from your phone. |
| 📱 **Your whole PC from Telegram** | Send a message or a **voice note** from anywhere: run tasks, get screenshots and files, take a webcam photo, change volume, brightness or dark mode, close apps, switch AI or key. **Slack is coming soon.** |
| 🔐 **Only you can control it** | Paired to **your** Telegram account only. A **PIN** guards sensitive actions, wrong tries lock remote control, and your PC alerts you. Every action goes into a tamper-evident audit log. |
| ✅ **Verified, not hopeful** | Plan → act → **check the result against clear goals** → retry or tell you honestly what failed. |
| 🤝 **A partner, not a chatbot** | Warm and honest in your own tone. It reads the mood, corrects you kindly when you're wrong, and says clearly what it can and can't do. |
| 🪶 **Light on your laptop** | ~100 MB in the background, near-zero CPU at rest, **no GPU** unless you choose a local model. **Shutdown** stops everything. |
| 🪙 **Careful with tokens** | Greetings cost 0 tokens. A chat reply and what Delta learns from it come from **one** AI call. Each step sends only the tools it needs. |

## What Delta can do

**On your PC**
- Open apps, manage files, type, click and read the screen like a person
- Volume, brightness, Windows light/dark mode, screenshots, webcam, close apps
- Schedule or start Google Meet calls; send WhatsApp and Telegram messages (it asks first)
- **🆘 Emergency help:** say you're unwell or in trouble and Delta can message your chosen emergency
  contacts on WhatsApp in your name and start a WhatsApp call, from the app or Telegram. Say it clearly
  ("message Kushi now") and it acts at once; otherwise it asks you first. It also tells you
  to call 112 when it's serious

**In your browser** (your own signed-in Chrome or Edge)
- Open sites, read pages, fill forms, play YouTube
- **Deep Research in your own ChatGPT or Gemini:** Delta asks what you need, writes an
  expert-level research brief, starts Deep Research for you, and retries by itself if the site fails

**Thinks with you**
- Asks **tick-box questions** instead of guessing, in the app and in Telegram
- **Remembers** your preferences locally and **learns your workflows** as reusable skills
- Voice notes in, answers out

## Works with the AI you already pay for (or none at all)

| How you sign in | Cost | Good for |
|---|---|---|
| **Codex** (ChatGPT account) | Your ChatGPT free or paid plan | Everyday tasks, coding, PC work |
| **Claude Code** | Your Claude subscription | Deep reasoning, long tasks |
| **Cline** | Free models available | Coding and agent work |
| **NVIDIA NIM** API key | Free tier | Fast cloud models |
| **Groq** API key | Free tier | Very fast replies, voice transcription |
| **DeepSeek** API key | Low cost | Strong reasoning |
| **LM Studio / Ollama** | Free, on your PC | Fully offline, private |

Set them up in the app's **Setup** page, or from Telegram with `/ai`, `/model` and `/keys`.
Keys are stored in **Windows Credential Manager**, never in files.

## Install (Windows 10/11)

```powershell
irm https://raw.githubusercontent.com/adharshramu12/Project-Delta/main/scripts/install.ps1 | iex
```

Or download the repo and **double-click `Install Delta.cmd`**. It installs the official signed
Python if needed, adds a **Delta** icon to your Desktop and Start menu, and opens the setup wizard:
pick your AI, set permissions, and pair your Telegram in 3 steps.

- **Close (X)** hides the window; Delta keeps listening for your Telegram messages.
- **Double-click the icon** to open it again.
- **⏻ Shutdown** stops everything and frees all memory.

`delta update` gets the newest version. `scripts\uninstall.ps1` removes Delta (your chats and
memory stay unless you add `-RemoveData`).

## Telegram commands

| Command | What it does |
|---|---|
| *any message or voice note* | Delta does the task and replies in the chat |
| `/ai` `/model` | See or switch the main AI and model |
| `/keys` | Replace an API key (PIN protected) |
| `/mode` | Permission level: ask everything → autopilot |
| `/screen` `/camera` | Screenshot or webcam photo (PIN protected) |
| `/volume` `/brightness` `/theme` | Sound, screen brightness, Windows light/dark |
| `/apps` | Open apps; tap to close one |
| `/lock` `/unlock` | Lock remote control now / enter your PIN |
| `/stop` `/status` `/help` | Stop a task, PC health and AIs, all commands |

## Safety by design

- **You stay in charge:** every action is risk-checked. Deleting, sending and anything that leaves
  your PC need your approval (unless you choose autopilot). Deletes go to the Recycle Bin.
- **Hard limits:** passwords, payment details and security settings are always refused.
- **Prompt-injection containment:** after Delta reads a web page, file or message, anything that sends,
  deletes or runs code needs your yes, even in autopilot. A malicious page can't quietly take over.
- **Encrypted at rest:** memories and chats are encrypted with Windows DPAPI, readable only by your
  Windows account.
- **Your rules:** `config/boundaries.yaml` sets allowed folders, apps, sites, quiet hours and autonomy.
- **Tamper-evident audit log** of every decision and tool call.
- Full threat model and known limits: [SECURITY.md](SECURITY.md).

## Architecture

```
  Desktop app · Telegram · Slack (soon) · Voice · CLI
                        │
                        ▼
  Orchestrator ── plan → act → observe → verify → learn
     │       │                                   │
  Memory     ▼                                   ▼
  & skills  Tools ──► Policy engine ──► Approvals (any device) + Remote Guard PIN
             │    files · apps · desktop · browser · deep research · meetings · messages · MCP
             ▼
  Model router ── Codex ▸ Claude Code ▸ Cline ▸ NIM ▸ Groq ▸ DeepSeek ▸ LM Studio ▸ Ollama
                        ▲
  Governor (CPU/RAM/GPU/battery)              Audit log (hash chain)
```

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Add-ons and MCP: [docs/ADDONS.md](docs/ADDONS.md)

## For developers

```powershell
pip install uv
uv sync --extra all          # plain 'uv sync' = core agent + dashboard
uv run delta doctor          # checks your setup
uv run delta run --open      # start the service and open the app
uv run pytest -q             # 290+ tests
```

| File | What |
|---|---|
| `config/delta.yaml` | AI providers, routes, governor limits, memory, channels |
| `config/boundaries.yaml` | What Delta may touch and how autonomous it is |
| `config/apps.yaml` | Friendly app names → programs |

Runtime data (memory, audit log, logs) lives in `%LOCALAPPDATA%\Delta`.

## Roadmap

- [x] Hybrid model router: Codex, Claude Code, Cline, NIM, Groq, DeepSeek, LM Studio, Ollama
- [x] Computer use, your own browser, Deep Research in ChatGPT/Gemini
- [x] Telegram remote control with voice, files, PIN and lockout
- [x] Question forms, memory, skill learning, partner-style chat
- [x] One-line installer, setup wizard, light/dark theme, low resource use
- [ ] Slack control (in progress)
- [ ] Scheduled tasks and routines
- [x] Prompt-injection containment (enforced in code) and DPAPI encryption of memory and chats
- [ ] End-to-end encrypted channel (Matrix), signed releases

## Creator

**Delta is created and maintained by Adharsh Kumar Bachu.**
If you use, fork or build on Delta, keep the [NOTICE](NOTICE) file and the credit, as the
Apache 2.0 license requires. Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).
