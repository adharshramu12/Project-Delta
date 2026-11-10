

# Project Delta

[![CI](https://github.com/adharshramu12/Project-Delta/actions/workflows/ci.yml/badge.svg)](https://github.com/adharshramu12/Project-Delta/actions/workflows/ci.yml)
[![Licence: AGPL-3.0-or-later](https://img.shields.io/badge/licence-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Platform: Windows 10/11](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078d4.svg)](#requirements)

Delta, an open source AI agent I built that does real work on a Windows PC and checks the result before it says done.

You talk to it in the app on your PC or from Telegram on your phone. It plans the task, uses your apps, files and
browser like a person would, asks before anything risky, and then checks what actually happened against the goal.
If something failed, it tells you plainly instead of pretending.

![Project Delta's dashboard: a chat with the task progress panel and an approval card](docs/images/dashboard-chat-light.png)

## What it does

- **Dashboard.** One window with Chat, Tasks, Apps, Schedules, Documents, Memory and Settings. A top bar shows the
  AI model, the browser, phone (PIN) protection and the connection. Anything that changes or sends something shows
  up as a card that says exactly what will happen, with Approve, Allow for this task and Deny. Light and dark
  themes follow Windows. More: [docs/UI.md](docs/UI.md).
- **Telegram on your phone.** Send a message or a voice note and Delta does the task on your PC. The "/" menu lists
  every command, grouped by area, and `/help` (or typing `menu`) shows the same list. Old chats are there too:
  `/chats` lists them, `/open` continues one and `/find` searches them. You only ever see your own Telegram chats.
- **Saved logins with captcha handling.** Save a login for a site once (Settings > Logins and watchers, or
  `delta logins add`) and Delta signs in itself. The login is sealed with Windows DPAPI, bound to that exact site,
  and never shown to the AI or written to a log. A simple text captcha on your own account is read by Delta itself,
  with no approval ("let Delta read it" is on by default for every login with a password; you can turn it off per
  login); if it cannot read it in 3 tries, Delta sends the captcha picture to your chat and types your reply. More:
  [docs/LOGINS.md](docs/LOGINS.md).
- **Watchers.** "Watch my portal for exams, timetable and notices." Delta signs in on a schedule (every 6 hours by
  default, never more than once an hour), reads the pages with a saved recipe and no AI, and tells you only what is
  new. More: [docs/WATCHERS.md](docs/WATCHERS.md).
- **Attachments.** Send a picture, PDF, Word (.docx), PowerPoint (.pptx) or text file in the chat or on Telegram.
  Delta reads it on your PC first and sends the AI only a short extract, so it costs far fewer tokens. More:
  [docs/ATTACHMENTS.md](docs/ATTACHMENTS.md).
- **Stop it anytime.** While Delta drives your mouse, keyboard or browser, an orange ring follows the pointer and a
  thin orange border marks the screen. Press Esc twice, press Ctrl+Alt+Shift+S, press Stop in the dashboard, or
  send "stop" or `/stop all` from your phone. Delta lets go at once and closes nothing of yours.
- **Faster app opening and self-heal.** An app that is already open is brought to the front instead of started
  again, and installed apps are indexed so "open X" is quick. When a step fails, Delta works out why and tries a
  safe fix first (find the moved button again, wait for a slow page, close a harmless banner, try another way of
  starting an app). A step that sends, deletes, buys or submits is never retried on its own.
- **Gmail drafts with approval.** "Email Ravi that the review moves to 4 pm." Delta drafts it in your voice and shows
  the whole email with every recipient. It sends only when you press Approve, every single time, at every
  permission level. Edit and Cancel send nothing.
- **43 apps, one click each.** The Apps page is a directory: search, filters by category (Productivity, Email and
  chat, Design, Developer tools, Databases, Websites, Automation, Docs and AI, Finance and markets, Marketing) and a
  details panel for every app saying what it does, how it signs in, where it runs and what asks first. Most connect
  with a browser sign-in and nothing to paste: Notion, Linear, Atlassian, Canva, Todoist, ClickUp, monday.com,
  Airtable, Zapier, Webflow, Wix, Netlify, Neon, Intercom, Product Hunt and more; DeepWiki, Context7, Microsoft Learn,
  AWS Knowledge and Excalidraw need no sign-in at all. GitHub, Slack, Gmail, Google Drive and Calendar, Asana and any
  MCP server you add are there too. Reading runs at once; anything that changes something asks first. Market data
  (Zerodha Kite, Alpaca, CoinGecko) is read-only by default: Kite's order tools stay off until you switch them on,
  and every order still needs your yes. More: [docs/APPS.md](docs/APPS.md).
- **Never looks stuck.** While a task runs, the chat shows what Delta is doing right now in plain words ("Working in
  Notion: search", "Opening youtube.com") with a timer, and says "Still waiting" when one step is slow. On Telegram a
  longer task gets one status message that updates itself and ends with Finished, Stopped or Could not finish, then
  the answer. A request that has to wait for the running task says so, and no task runs for more than 20 minutes.
- **Human writing.** Chat answers, emails and documents come out in plain Indian English. A check in code (no extra
  AI call) removes long dashes, stock AI phrases and US spellings before you see the text. More:
  [docs/STYLE_AND_IDENTITY.md](docs/STYLE_AND_IDENTITY.md).

- **Light on your laptop.** About 100 MB of memory in the background, near-zero CPU at rest, and no GPU unless you
  pick a local model. On disk the browser caches are capped and their leftovers are tidied away by themselves, so
  Delta stays under about 1 GB instead of growing.

Delta also runs scheduled tasks and reminders, keeps a history of every task with resume after a crash, lets you
undo its file changes, remembers what you tell it (encrypted on your PC) and makes PDF and Word documents, PowerPoint presentations
and Excel spreadsheets. The
docs below cover each one.

## Requirements

- **Windows 10 or Windows 11, 64-bit.** Windows on ARM (Snapdragon, Copilot+ PCs) uses the x64 build of Python
  under Windows' own emulation; this has not been tested on a real ARM PC yet.
- **RAM:** 4 GB with a cloud AI. 8 GB or more is better, and 16 GB if you want to run local models.
- **Disk:** about 2 GB free for the install, plus room for local models if you use them.
- **Browser:** Microsoft Edge (every Windows PC has it) or Chrome.
- **Internet** to install, and for cloud AIs, web search, Telegram and connected apps.
- **No administrator rights** needed. Python 3.12 is installed for you if it is missing; Node.js and uv are not
  needed for Delta itself.

## Install

**Steps**

1. **One command.** Open PowerShell (Start menu, type PowerShell) and paste:

   ```powershell
   irm https://raw.githubusercontent.com/adharshramu12/Project-Delta/main/scripts/install.ps1 | iex
   ```

   This works on a new PC where scripts are not allowed, and it does not change that setting. It is also the way
   to install on a PC with Smart App Control on. The installer reuses or installs the official signed Python 3.12
   (64-bit), copies Delta to `%LOCALAPPDATA%\Programs\Delta`, installs its packages (a few minutes), adds a Delta
   icon to the Desktop and Start menu, and adds the `delta` command. The log is at `%TEMP%\delta-install.log`.
   Windows security: the one command is not expected to show a SmartScreen or firewall prompt. Delta's dashboard
   listens on this PC only (127.0.0.1), so Windows Firewall has nothing to ask. If you use the zip and Windows says "Windows protected
   your PC" (SmartScreen), or Microsoft Defender stops a file, see
   [docs/INSTALL.md](docs/INSTALL.md#windows-security-prompts).
2. **Or from a zip.** On GitHub press **Code > Download ZIP**, right-click the zip, choose **Extract All...**, then
   double-click **`Install Delta.cmd`** in the extracted folder. With Smart App Control on, Windows may block a
   file from a downloaded zip; use the command in step 1 instead.
3. **Set up.** The setup wizard opens: pick your AI, set permissions and pair your Telegram. A short tour of the
   dashboard follows; skip it with Esc, or take it again later by typing `/tour` in the chat box.

**Update.** When a newer version is out, Delta shows a banner (and, with Telegram on, sends one message). Press
**Update now**: Delta checks the release's files, closes, updates and opens again, and your chats and settings are
kept. Nothing is installed until you press it, and Settings > Updates and help can turn the checks off. More in
[docs/UPDATES.md](docs/UPDATES.md). You can also choose **Update Delta** in the Start menu (Delta folder), or run
`delta update --stop` in a new PowerShell window. `--stop` closes a running Delta first; plain `delta update` asks you to close it yourself. Your
data is kept. If `delta` is not recognised, paste the install command again (it updates in place), or run Delta's
own Python by its full path:

```powershell
& "$env:LOCALAPPDATA\Programs\Delta\.venv\Scripts\python.exe" -m delta update --stop
```

**Uninstall.** This removes Delta, its icons, the `delta` command and "Start Delta when I sign in to Windows". Your
chats and memory stay unless you add `-RemoveData`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Delta\scripts\uninstall.ps1"
```

`-ExecutionPolicy Bypass` applies to that one run only. Two things always stay, and the uninstaller prints their
exact paths: the files Delta made for you (the **Delta files** folder on your Desktop), because they are yours, and
**Python 3.12**, because other programs may use it. Delete the folder yourself when you no longer need it; remove
Python from Settings > Apps > Installed apps if nothing else needs it.

### Troubleshooting

| What you see | What to do |
|---|---|
| "running scripts is disabled on this system" | Use the `irm` command from step 1; it installs without changing that setting. To run one of Delta's own scripts yourself, use `powershell -NoProfile -ExecutionPolicy Bypass -File <script>` (that run only). |
| Smart App Control blocks `Install Delta.cmd` or the Delta icon | Use the `irm` command from step 1. It installs, or rebuilds Delta on the official signed Python, without running a file from the zip. |
| "Delta's files are not next to this file" | You opened `Install Delta.cmd` inside the zip. Extract the zip first. |
| "Could not create SSL/TLS secure channel" | Older Windows PowerShell. Run `[Net.ServicePointManager]::SecurityProtocol = 3072`, then the `irm` command again. |
| "Some optional parts were not set up" | Delta is installed and works. Paste the command the installer printed under that line. |
| "delta is not recognized" | Open a new PowerShell window. Still not found: paste the install command again, or use the full Python path shown under Update. |
| "No AI model is available" | In Delta, open Settings > Keys and add a free Groq key, or start LM Studio or Ollama, or sign in to Codex or Claude Code. |
| "Windows protected your PC" or Defender stops a file | See [Windows security prompts](docs/INSTALL.md#windows-security-prompts). The `irm` command from step 1 avoids SmartScreen. |
| The icon does nothing | Read the last lines of `%LOCALAPPDATA%\Delta\logs\service.log`, or run `delta doctor` in a new PowerShell window. |

## Quick start

1. Pick an AI in the setup wizard (see Model providers below). A free Groq or NVIDIA NIM key is enough to start.
2. Pair Telegram if you want to use Delta from your phone, and set a PIN for sensitive actions.
3. Ask something in the chat, for example "open Notepad and write today's to-do list", "summarise this PDF" or
   "what is on my timetable tomorrow?" after saving a login.
4. Approve or deny the cards Delta shows. Close (X) hides the window and Delta keeps listening; Shut down stops
   everything. After a restart Delta is off (and Telegram stops answering) until you open it again, unless you turn
   on **Start Delta when I sign in to Windows** in Settings > Setup and shutdown (setup offers it when you connect
   Telegram). It then starts in the background with no window.

From a terminal: `delta doctor` checks your setup, `delta app` opens Delta. The full install guide (options, what is tested, every fix) is in [docs/INSTALL.md](docs/INSTALL.md).

## Safety model

- **Approvals.** Every action is risk-checked. Deleting, sending and anything that leaves your PC needs your yes
  unless you choose autopilot, and even autopilot asks before permanent deletes, changes outside your folders,
  typing into a terminal, and sending or running anything after Delta has read a web page, file or message.
  "Allow for this task" lasts until the task ends and is never saved. Deletes go to the Recycle Bin.
- **PIN.** Sensitive actions from your phone (screenshots, webcam, sign-ins, keys, connecting apps, resuming
  tasks) need your PIN. The PIN is kept only as a salted hash in Windows Credential Manager, and wrong tries lock
  remote control.
- **Captchas.** Delta never bypasses managed captchas: reCAPTCHA, hCaptcha and Cloudflare Turnstile are always
  refused and handed to you. Only simple text captchas are read, and only on your own accounts: on a saved login with
  a password (on by default, one switch per login turns it off) or on a site you allowed with `delta captcha allow`.
- **No posting for you.** Delta writes LinkedIn, Medium and Substack drafts but never presses Post; you paste and
  post yourself.
- **Your data stays yours.** Keys live in Windows Credential Manager. Memory, chats and saved logins are encrypted
  with Windows DPAPI, and Delta refuses to save them if encryption is not working. Every decision and tool call
  goes into a signed, hash-chained audit log (`delta audit verify`).

Full threat model and known limits: [SECURITY.md](SECURITY.md).

## Model providers

Use the AI you already have, or run fully offline. Delta routes each request to an available provider and falls
back to the next when one is down, rate limited or out of credits.

| Provider | How you sign in | Cost |
|---|---|---|
| Codex | Your ChatGPT account, through the Codex CLI | Your ChatGPT free or paid plan |
| Claude Code | Your Claude account, through Claude Code | Your Claude subscription |
| Cline | Your Cline account, through cline-cli | Free models available |
| NVIDIA NIM | API key | Free tier |
| Groq | API key | Free tier |
| Nebius Token Factory | API key | Your Nebius credits (pay as you go) |
| DeepSeek | API key | Low cost |
| LM Studio, Ollama | Local, on your PC | Free, works offline |

Set them up in Setup, or from Telegram with `/ai`, `/model` and `/keys`. Routes and fallbacks are in
`config/delta.yaml`.

## Docs

- [Architecture](docs/ARCHITECTURE.md) and [add-ons](docs/ADDONS.md)
- [Connected apps and MCP](docs/APPS.md)
- [Saved logins](docs/LOGINS.md) and [watchers](docs/WATCHERS.md)
- [Attachments](docs/ATTACHMENTS.md)
- [Writing style and identity](docs/STYLE_AND_IDENTITY.md)
- [Dashboard design](docs/UI.md) and [updates](docs/UPDATES.md)
- [Security](SECURITY.md)
- [What has not been checked on real hardware](docs/NOT_VERIFIED.md)
- [Changelog](CHANGELOG.md) and [contributing](CONTRIBUTING.md)

For developers: `uv sync --extra all`, then `uv run pytest -q`, `uv run ruff check src tests` and `uv run mypy`.
CI runs the full tests, a minimal install, real-browser end-to-end tests and the scripted evals on clean Windows
runners for every push and pull request.

## Licence and trademarks

Project Delta is open source under the **GNU Affero General Public License v3.0 or later** ([LICENSE](LICENSE)).
Anyone can use it, study it, change it and share it, including at work. If you share a modified version, or let
people use one over a network, you must release your source under the same licence and keep the
[NOTICE](NOTICE) credit. Closed-source forks are not allowed. Versions up to and including 0.2.0 were released
under Apache-2.0.

The names "Project Delta" and "ARVA Talks Academy" and the Delta logo are not covered by the licence. Forks,
products and anything that suggests endorsement need written approval: see [TRADEMARKS.md](TRADEMARKS.md).

Built by Adharsh Kumar Bachu, founder of ARVA Talks Academy ([arvatalksacademy.com](https://arvatalksacademy.com/)).
