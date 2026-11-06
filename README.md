<div align="center">

# Δ Project - Delta

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
| 🆘 **Help when you need it** | Tell Delta you're unwell or in trouble and it can **WhatsApp your chosen emergency contacts** in your name and **start a WhatsApp call**, from the PC or your phone. A clear instruction acts at once; otherwise it asks first. For anything serious it tells you to call **112**. |
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
  ("message Ravi now") and it acts at once; otherwise it asks you first. It also tells you
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

## Scheduled tasks and reminders

Tell Delta when, in plain words, from the app, Telegram or a terminal:

- *"every day at 9 am check my attendance on the ERP"*
- *"remind me at 6 pm to call Ravi"* (a reminder only sends the message: no AI, no tokens)
- *"every Monday 10 am summarise my Downloads folder"*
- *"every 30 minutes check my mail"*, *"tomorrow at 7:30 am open Zoom"*, *"in 20 minutes remind me to stretch"*,
  or a cron line: *"cron 0 9 \* \* 1-5 check the ERP"*

Delta shows what it understood and the next run time ("Every day at 9:00 AM: check my attendance on the ERP.
Next run: Sat 7 Nov 2026, 9:00 AM") and saves it only after your OK. The result comes back to the chat you
asked from (Telegram, Slack, or the app's **Scheduled tasks** chat with a Windows notification); a failure
comes back with the reason.

- **Manage them** in the app's right-hand panel (**Schedules**: next run, last result, pause, resume, run now,
  delete, and a box to add one), by asking ("what's scheduled?", "pause the attendance check"), or from a
  terminal: `delta schedule add "every day at 9 am ..."`, `delta schedule list`, `pause`, `resume`, `remove`,
  `run`.
- **Runs only while Delta is on** (the app or `delta run`). If the PC was off or asleep, a run that is at most
  1 hour late runs once when it wakes; an older one is noted as missed and the schedule carries on.
- **Same safety as a typed request**: every action asks for approval the same way, an approval nobody answers
  counts as "no", quiet hours hold scheduled tasks until they end, and a schedule never gets a higher permission
  level than when it was made. Making or turning on a schedule from your phone needs your PIN.
- Settings (optional, in `config/delta.yaml` or your settings file):

  ```yaml
  scheduler:
    enabled: true
    grace_minutes: 60     # how late a missed run may still run
    max_concurrent: 1     # scheduled AI tasks at once
    history_limit: 50     # past runs kept per schedule
    timezone: ""          # e.g. "Asia/Kolkata"; empty = this PC's time zone
  ```

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
| *"every day at 9 am check my attendance"* | A scheduled task; the result comes back here (making one needs your PIN) |
| *"I'm not well, message Ravi now"* | 🆘 Emergency alert to your saved contacts (WhatsApp message or call) |
| `/stop` `/status` `/help` | Stop a task, PC health and AIs, all commands |

## Safety by design

- **You stay in charge:** every action is risk-checked. Deleting, sending and anything that leaves
  your PC need your approval (unless you choose autopilot). Deletes go to the Recycle Bin.
- **Autopilot still asks** before deleting anything that can't be undone, changing files outside your
  folders, typing or clicking in a terminal, the Run box or Delta itself, and, once a task has read a web page,
  file or message, before sending, deleting, running code, saving to memory or opening a link that carries
  hidden data. Full list: [SECURITY.md](SECURITY.md).
- **"Allow for this task":** approve the same kind of action (same app, same site) once for the rest of a
  task. Never saved, and never offered for permanent deletes, terminals or links carrying data.
- **Hard limits:** passwords, payment details and security settings are always refused.
- **Prompt-injection containment:** after Delta reads a web page, file or message, anything that sends,
  deletes or runs code needs your yes, even in autopilot. A malicious page can't quietly take over.
- **Encrypted at rest:** memories and chats are encrypted with Windows DPAPI, readable only by your
  Windows account.
- **Emergency alerts:** contacts are stored encrypted; during a help request Delta reaches nobody
  without your yes (even in autopilot) unless your own message clearly says to.
- **Captchas, only if you opt in:** off by default. For your own accounts (e.g. a university portal) you can
  allow a site with `delta captcha allow <host>`; Delta then reads its simple image captcha (2 tries, then
  asks you). Otherwise you type it on the PC and approve. reCAPTCHA, hCaptcha and Cloudflare checks are
  always refused. Details: [SECURITY.md](SECURITY.md#captchas-opt-in-your-own-accounts-only).
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
             │    files · apps · desktop · browser · deep research · meetings · messages ·
             │    emergency alerts · PC controls · MCP
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
- [x] Emergency help: WhatsApp alerts and calls to chosen contacts, from the app or Telegram
- [ ] End-to-end encrypted channel (Matrix), signed releases

## Creator

**Delta is created and maintained by Adharsh Kumar Bachu.**
If you use, fork or build on Delta, keep the [NOTICE](NOTICE) file and the credit, as the
Apache 2.0 license requires. Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).
