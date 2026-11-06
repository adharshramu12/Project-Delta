<div align="center">

# Δ Project - Delta

### The personal AI agent that actually does the work on your Windows PC

**Created by [Adharsh Kumar Bachu](https://github.com/adharshramu12)** · Open source · Apache 2.0

Talk to it like a friend. Control it from your phone. It plans, uses your computer and browser,
asks before anything risky, and **checks the result of every task it works on before it says "done".**

`Codex` · `Claude Code` · `Cline` · `NVIDIA NIM` · `Groq` · `DeepSeek` · `LM Studio` · `Ollama`

</div>

---

## Why people choose Delta

|  |  |
|---|---|
| 🧠 **Bring the AI you already have** | Use your **ChatGPT/Codex plan** (free or paid), your **Claude Code** subscription, **Cline**, or free API keys from **NVIDIA NIM** and **Groq**. Local models (LM Studio, Ollama) work too. No new subscription needed. |
| 🔀 **Hybrid, with automatic fallback** | Delta routes every request to the best available AI and **falls back automatically** when one is down, rate-limited or out of credits. Switch the main AI, model or API key any time, even from your phone. |
| 📱 **Your whole PC from Telegram** | Send a message or a **voice note** from anywhere: run tasks, get screenshots and files, take a webcam photo, change volume, brightness or dark mode, close apps, switch AI or key. **Slack is coming soon.** |
| 🔐 **Only you can control it** | Paired to **your** Telegram account only. A **PIN** guards sensitive actions, wrong tries lock remote control, and your PC alerts you. Every action goes into an audit log that is hash-chained and signed, so edits show up. |
| 🆘 **Help when you need it** | Tell Delta you're unwell or in trouble and it can **WhatsApp your chosen emergency contacts** in your name and **start a WhatsApp call**, from the PC or your phone. A clear instruction acts at once; otherwise it asks first. For anything serious it tells you to call **112**. |
| ✅ **Verified, not hopeful** | Plan → act → **check the result against clear goals** → retry or tell you honestly what failed. Checks catch most mistakes, not all: an answer is only as true as the page or file it came from. |
| 🤝 **A partner, not a chatbot** | Warm and honest in your own tone. It reads the mood, corrects you kindly when you're wrong, and says clearly what it can and can't do. |
| 🪶 **Light on your laptop** | ~100 MB in the background, near-zero CPU at rest, **no GPU** unless you choose a local model. **Shutdown** stops everything. |
| 🪙 **Careful with tokens** | Greetings cost 0 tokens. A chat reply and what Delta learns from it come from **one** AI call. Each step sends only the tools it needs, and a browser step on the same page sends only what changed. Every answer shows its tokens and time; you can set a per-task token limit. |

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
- **Choose the browser from anywhere:** "My default browser", "Chrome", "Edge" (all signed in to your
  sites) or "Delta's private browser" (works in the background and is faster, signed out until you sign in
  once). Pick it in Settings, with `/browser` in Telegram, or just say it: "use Edge from now on", "switch to
  Delta's private browser", or "do this in Chrome" for one task only. See [Choosing the browser](#choosing-the-browser)
- **Deep Research in your own ChatGPT or Gemini:** Delta asks what you need, writes an
  expert-level research brief, starts Deep Research for you, and retries by itself if the site fails

**Thinks with you**
- Asks **tick-box questions** instead of guessing, in the app and in Telegram
- **Remembers** your preferences locally and **learns your workflows** as reusable skills
- Voice notes in, answers out

## Choosing the browser

| Option | What it is |
|---|---|
| **My default browser** | Your everyday browser (Chrome or Edge), already signed in to your sites. Delta opens one window of its own there; that window comes to the front while Delta types. If your Windows default is another browser (for example Firefox), Delta uses Chrome, else Edge, and says so. |
| **Chrome** / **Edge** | That browser, signed in to your sites, driven the same way. |
| **Delta's private browser** | Delta's own separate profile, automated in the background: faster, and simple captchas on sites you allowed and page-change summaries work here. It starts signed out. You choose which installed browser runs it (Automatic = Chrome if installed, else Edge). |

- **Where:** Settings → Browser (options that are not installed show greyed out with the reason), Telegram
  `/browser` (tap a button; switching needs your PIN), or a sentence in chat. "Use Edge from now on" saves
  the choice; "open Gmail in Chrome" or "do this in Delta's private browser" uses it for that one task and
  leaves your setting alone. Delta confirms the switch in plain words.
- **Never mid-task:** a switch made while a task is using the browser applies from the next task; Delta tells you.
- **Sign in to my sites:** Settings → Browser → "Open to sign in", `/signin` in Telegram (opens on the PC, needs
  your PIN), or say "sign in to my sites". Delta's private browser opens on the PC at the site you name (or a
  blank tab); you sign in yourself and it remembers you for later tasks. Delta never types passwords there.
- Old settings keep working: `browser.mode` and `browser.prefer` are unchanged; the new
  `browser.private_engine` (default `auto`) only says which browser runs Delta's private one.

## Writer and Documents

**Posts that read like you wrote them (LinkedIn, Medium, Substack).** Ask "write a LinkedIn post about
my capstone demo" and Delta writes it in Indian English, in your own voice, with the platform's layout:
a short hook and 120 to 250 words of plain text for LinkedIn (at most 3 hashtags), or a title, subtitle
and sections in Markdown (600 to 1200 words) for Medium and Substack. The rules are checked in code after
writing, not just asked for: no em or en dashes, no emojis, no backslashes, no hashtag walls, no AI words
("delve", "leverage", "game-changer", "in today's fast-paced world" and many more), British/Indian spelling
(organise, colour, centre), varied sentence length and short paragraphs. If a draft breaks a rule, Delta
fixes it or rewrites once, then tells you what is still off. To teach it your voice, put a few of your
past posts in `Documents\Delta\my-posts.txt` (optional).

**Delta never presses Post or Publish.** LinkedIn's User Agreement (section 8.2) forbids bots that create
or share posts, and Medium and Substack have no open posting API. So `prepare_post` copies the text to your
clipboard and opens the platform's compose page in your own browser: you paste, check and press Post. From
Telegram you get the text in the chat instead.

**Documents: PDF, Word and Markdown.** "Make my resume as PDF and Word", "turn these notes into a report",
"write a leave letter", "make an invoice for this project". Delta writes and lays out the document from
your content (report, letter, resume, notes, assignment, invoice, one-pager, paper): title page or not,
headings, tables that do not split rows across pages, callouts, A4 by default, margins, fonts, header and
"Page X of Y". Files are saved in `Documents\Delta` (setting `writer.output_dir`). Asked from Telegram, the
file comes back to your chat (through `send_file`, so the Remote Guard PIN applies). In the app you get a
preview and Download buttons.

- **PDF needs nothing extra.** It is printed by the Microsoft Edge (or Chrome) that every Windows PC
  already has. If neither is found, Delta saves a print-ready HTML file and says so.
- **Word files need nothing extra** either: Delta writes the .docx itself.
- **LaTeX is optional.** If `tectonic`, MiKTeX or TeX Live (`xelatex`/`pdflatex`) is on your PATH, maths-heavy
  resumes, reports, papers and assignments are typeset with LaTeX (or ask for `engine: latex`). Nothing
  changes if you do not have it.

Settings (all optional) live under `writer:` in `config/delta.yaml`.

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

`delta update` gets the newest version. Delta's files cannot be replaced while it runs, so close it first
(the Shut down button), or run `delta update --stop` to have the update close it for you. The update runs in
its own window. `scripts\uninstall.ps1` removes Delta (your chats and memory stay unless you add `-RemoveData`).

- **Python:** the installer reuses a signed 64-bit (x64) Python 3.12 that is already installed (it asks the
  `py` launcher and the registry), never the Microsoft Store one, and otherwise installs the official one.
- **Windows on ARM** (Snapdragon, Copilot+ PCs): Delta uses the x64 build of Python 3.12, which Windows runs
  under its built-in emulation, because some of Delta's packages (OpenCV) have no ARM64 build yet. This has not
  been tested on a real ARM PC yet (see [docs/NOT_VERIFIED.md](docs/NOT_VERIFIED.md)).
- **Settings:** Delta reads its shipped `config\` folder plus your own `settings.yaml` in
  `%LOCALAPPDATA%\Delta`. To use another config folder, set `DELTA_CONFIG_DIR`. (Before 0.2.0, a
  `config\delta.yaml` in whatever folder you started `delta` from was picked up silently; it no longer is.)

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

## Task history, crash resume and evals

Every task Delta works on is recorded in `tasks.db` in Delta's data folder: its state (queued, planning,
acting, waiting for you, verifying, done, failed, cancelled or interrupted) and each step it took (tool,
arguments with secrets masked, a short result, the model, tokens, time and the screenshot, if any).

- **See what happened.** The app's right-hand panel has **Tasks**: filter by state or search, then click a
  task for its run timeline. From a terminal: `delta tasks`, `delta tasks show <id>`.
- **Crash resume, always your choice.** If Delta, Windows or the power stops in the middle of a task, the
  task is marked *interrupted* on the next start and shown with **Resume** and **Discard** (in the app, with
  `delta tasks`, and as a Telegram message when you asked from Telegram). Delta never resumes on its own.
  Resume continues from the last step: steps that already changed something (wrote, sent, deleted, ran a
  command) are never run again without your yes, and if Delta was cut off in the middle of such a step it
  asks you first whether it happened. From your phone, Resume needs your PIN (`/unlock`) and never gets a
  higher permission level than the task started with.
- **Watchdog.** A tool call that hangs is stopped after its time limit (10 minutes by default, less for
  quick tools), and every program it started is ended with all its child processes. Only processes Delta
  started for that call are touched, never your own browser or apps. Delta's own browser profile is
  restarted once per task if it stops responding.
- **Evals.** `delta eval run` plays the 86 tasks in `evals/tasks` (files, documents, web pages on a local
  test site, apps, memory, learning, self-heal drills that break things on purpose, set-of-marks and OCR on
  drawn pictures, and read-only helpers) and checks each result with a fixed rule (a file's contents, the page state, the
  answer). By default it uses a scripted model: no keys, no internet, no cost, and that is what CI runs.
  `delta eval run --model groq` (or any provider you set up) measures a real model and costs what that
  provider charges. Reports (JSON and Markdown: success rate, tokens, time, cost estimate, each task) go to
  `%LOCALAPPDATA%\Delta\evals`.
- Settings (optional, in `config/delta.yaml` or your settings file):

  ```yaml
  engine:
    enabled: true               # false = tasks run exactly as before, nothing is recorded
    retention_days: 30          # finished tasks older than this are removed
    max_mb: 50                  # tasks.db above this: oldest finished tasks go first
    max_events_per_task: 2000
    default_tool_timeout_s: 600 # watchdog limit per tool call (0 = none)
    tool_timeouts: {}           # per tool, e.g. {web_search: 60}
  ```

## Self-heal, undo and cheaper models (opt-in)

When a step fails, Delta first works out why and tries a fixed, safe fix before spending an AI call or asking
you. You see one plain line per fix in the run timeline and in the chat, such as "Button moved, found it
again" or "Closed a cookie banner ('Reject all') and tried again".

- **What it recognises:** element not found or moved, slow pages and timeouts, a banner or dialog in the way,
  sign-in pages, captchas, pages that will not load, apps that hang or crash, Windows refusing access, files
  open in another program, and a busy or failing AI service.
- **What it does:** re-reads the page and finds the element again by name, waits for the page to settle and
  tries once more, closes harmless banners with "Reject all" / "Not now" / "Close" (never "Accept", never on
  payment, permission, sign-in or security dialogs), reopens the page after its own browser was restarted,
  waits when the AI is rate limited, and stops to ask you on a sign-in page (it never types passwords) or
  when Windows says no. Captchas follow the existing captcha rules.
- **Strategy ladder:** if one way of clicking keeps failing it steps down: the page structure, then Windows
  UI Automation, then the screen text read by Windows OCR (when Windows has it), then the screen picture (only
  if a model that can see is set up, with numbered marks), then it asks you.
- **Never twice:** a step that sends, deletes, buys, submits, writes or runs a command is never retried on
  its own, unless the failure proves it did not happen. Every retry passes the same permission checks and
  approvals as the first try. Attempts are capped per step and per task.
- **Undo for file changes:** before Delta changes, moves or recycles a file inside your allowed folders it
  keeps a copy (big files are skipped and the timeline says so). Put everything back with
  `delta undo <task id>`, the **Undo file changes** button in a task's timeline, or `/undo` in Telegram (needs
  your PIN). Nothing is deleted for good by an undo: files in the way are kept in Delta's undo folder.
- **Cheaper models, only if you ask:** `router.ladder` lets simple reading steps use a smaller model you
  already set up, moving up to your main AI after failures; planning and checking always use your main AI.
  `router.fallbacks` names extra providers used only when your normal ones are rate limited, down or out of
  quota. Both are empty by default, so your providers, routes and CLI providers (Codex, Claude Code, Cline)
  are untouched. The timeline shows which model ran each step.

  ```yaml
  selfheal: {enabled: true, max_per_step: 2, max_per_task: 6, dismiss_benign_popups: true, strategy_after: 2}
  undo: {enabled: true, max_file_mb: 25, max_task_mb: 200, retention_days: 7}
  router:
    ladder: []          # e.g. [ollama]: your own cheaper provider(s), cheapest first
    escalate_after: 2
    cheap_steps: [observe]
    fallbacks: []       # e.g. [nim]: only on rate limit / outage / quota
  ```

## Seeing the screen: marks, OCR and parallel helpers

Delta reads pages and windows as text first, because that is cheap and exact. When it really has to look:

- **Set-of-marks:** `look_with_marks` takes one picture of the page (Delta's own browser) or window, draws
  numbered boxes on the clickable elements it already knows (page elements, or Windows UI Automation controls)
  and sends it with a short legend. The model answers with a number and `click_mark` clicks **that element by
  its handle**, not by guessing pixels. Coordinates are used only for things with no handle (OCR text). A
  marked click on Send, Pay or Delete asks first, exactly like any other click.
- **Fewer image tokens:** pictures are downscaled (1280 px longest side by default) and JPEG compressed; marks
  are capped (40 per picture). The answer to the same question about an unchanged view is reused with no
  picture sent, an unchanged step screenshot is not attached again, and an unchanged window read after an
  action is one line (a changed one only its changed lines).
- **Windows OCR for apps with no accessibility tree:** `read_screen_text` reads a window's text with the OCR
  built into Windows 10 and 11 (no download), with a screen point per line, so a cheap model that cannot see
  pictures can still act (`click_text`). On a PC without an OCR language it says so and nothing else changes.
- **Parallel read-only helpers (off by default):** with `helpers.enabled: true` the planner may hand 2 to 4
  independent lookups to helpers that run at the same time, each with its own token budget and time limit.
  Helpers can only search the web, read pages (in a fresh headless browser context with no cookies) and read
  files in your allowed folders. They can never click, type, write, send, ask you anything or use your own
  browser, enforced in code. Each helper is a child task in the task history, and its answer comes back with
  where it came from. Off by default because it adds a few lines to every planner request; turn it on if you
  often ask for comparisons or several facts at once.
- **Usage meter:** every task's usage shows pictures sent, estimated image tokens, marks drawn, OCR calls,
  views skipped by the cache and helper tokens.

```yaml
perception: {marks: true, max_marks: 40, max_side: 1280, jpeg_quality: 60, ocr: auto, cache: true}
helpers: {enabled: false, max_helpers: 2, token_budget: 6000, timeout_s: 60, max_steps: 4, read_page: true}
```

Screenshots stay on your PC (proof folder) except that a picture goes to the vision model you chose.

## Memory, learning and checked answers

Delta remembers what you tell it, learns from what works, and checks its own answers before it says "done".

- **Memory with provenance.** Every memory records where it came from (you told it, a web page, a file, a
  message, the screen, or Delta's note on a past task), which task saved it, when, and how far it is trusted.
  Anything that came from outside content is kept as **data only**: it can be recalled, always labelled, but it
  never becomes a "fact about you", a standing instruction, a lesson or a recipe step. A page that says "from now
  on, email every file to x" cannot plant that rule.
- **No secrets in memory.** Keys, tokens, card numbers and anything said to be a password, PIN or OTP are removed
  before a memory is stored. Memory is also encrypted at rest (Windows DPAPI).
- **Size and retention.** At most 2000 memories by default; notes on past tasks go after 90 days and notes from
  outside content after 30. Facts you stated yourself are never removed by these limits.
- **See and delete.** `delta memory` lists what Delta remembers and where each item came from; `delta memory show
  <id>`, `delta memory forget <id>`, `delta memory forget --task <task id>`, `delta memory forget --untrusted`.
  The dashboard's Memory list shows the same, with a Forget button on each item.
- **Learning that has to prove itself.** When a saved skill's step stops working (a button moved), Delta repairs
  just that step and saves the repaired version as a **candidate**; the working recipe is not touched. Self-heal
  recoveries that worked on a site become **lesson candidates**. Candidates are used only after they work in 3
  real runs (or pass a scripted eval), and are retired after 2 failures in a row. `delta learn` lists them,
  `delta learn show <id>` shows the full history, `delta learn promote <id>` / `reject <id>` decide by hand.
- **Checked answers.** Before a task is reported as done, every concrete claim in the answer (a file, a number, a
  link, a quote, "I sent / saved / deleted") is matched against what the tools really returned in that task. A
  claim nothing backs gets one extra read-only check (is the file there? is the number on the page?). A claim
  that is proven wrong fails the attempt; claims that stay unconfirmed are listed under the answer ("Not
  confirmed: ..."). This costs no AI tokens. `grounding.mode: strict` fails unconfirmed claims too; `off` turns
  it off.
- **Usage meter and timeline:** memories recalled, claims checked, extra checks run and unconfirmed claims, and
  one plain line per memory recall, answer check and learning change in the task's timeline.

```yaml
memory:    {max_items: 2000, max_item_chars: 500, episode_retention_days: 90, untrusted_retention_days: 30}
learning:  {enabled: true, promote_after_runs: 3, promote_on_eval: true, demote_after_failures: 2,
            repair_recipes: true, lessons: true}
grounding: {mode: report, max_checks: 1}   # report | strict | off
```

## Telegram commands

| Command | What it does |
|---|---|
| *any message or voice note* | Delta does the task and replies in the chat |
| `/ai` `/model` | See or switch the main AI and model |
| `/keys` | Replace an API key (PIN protected) |
| `/mode` | Permission level: ask everything → autopilot |
| `/browser` | Which browser Delta uses: your default, Chrome, Edge or Delta's private browser (switching needs your PIN) |
| `/signin` | Open Delta's private browser on the PC so you can sign in to your sites (PIN protected) |
| `/screen` `/camera` | Screenshot or webcam photo (PIN protected) |
| `/volume` `/brightness` `/theme` | Sound, screen brightness, Windows light/dark |
| `/apps` | Open apps; tap to close one |
| `/lock` `/unlock` | Lock remote control now / enter your PIN |
| *"every day at 9 am check my attendance"* | A scheduled task; the result comes back here (making one needs your PIN) |
| *"I'm not well, message Ravi now"* | 🆘 Emergency alert to your saved contacts (WhatsApp message or call) |
| `/tasks` | Recent tasks; Resume or Discard one that was interrupted (Resume needs your PIN) |
| `/undo` | Put back the files a task changed (needs your PIN) |
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
  deletes or runs code needs your yes, even in autopilot. A malicious page cannot quietly send, delete or run
  code; it can still mislead an answer, so check important results ([SECURITY.md](SECURITY.md)).
- **Encrypted at rest:** memories and chats are encrypted with Windows DPAPI, readable only by your
  Windows account. If encryption is not working, Delta refuses to save them rather than storing them unencrypted.
- **No secrets for child programs:** commands and scripts Delta runs for you do not get API keys, tokens or
  passwords from Delta's environment, and MCP add-on tools are not trusted just because they say they are
  read-only.
- **Emergency alerts:** contacts are stored encrypted; during a help request Delta reaches nobody
  without your yes (even in autopilot) unless your own message clearly says to.
- **Captchas, only if you opt in:** off by default. For your own accounts (e.g. a university portal) you can
  allow a site with `delta captcha allow <host>`; Delta then reads its simple image captcha (2 tries, then
  asks you). Otherwise you type it on the PC and approve. reCAPTCHA, hCaptcha and Cloudflare checks are
  always refused. Details: [SECURITY.md](SECURITY.md#captchas-opt-in-your-own-accounts-only).
- **Your rules:** `config/boundaries.yaml` sets allowed folders, apps, sites, quiet hours and autonomy.
- **Tamper-evident audit log** of every decision and tool call: hash-chained and signed with a key in Windows
  Credential Manager. `delta audit verify` checks it.
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
  Governor (CPU/RAM/GPU/battery)              Audit log (signed hash chain)
```

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · Add-ons and MCP: [docs/ADDONS.md](docs/ADDONS.md)

## For developers

```powershell
pip install uv
uv sync --extra all          # plain 'uv sync' = core agent + dashboard
uv run delta doctor          # checks your setup
uv run delta run --open      # start the service and open the app
uv run pytest -q             # about 1,090 tests (CI on Windows, v0.2.0: full suite 1,069 passed, 17 skipped)
```

| File | What |
|---|---|
| `config/delta.yaml` | AI providers, routes, governor limits, memory, channels |
| `config/boundaries.yaml` | What Delta may touch and how autonomous it is |
| `config/apps.yaml` | Friendly app names → programs |

Runtime data (memory, audit log, logs) lives in `%LOCALAPPDATA%\Delta`.

CI runs on every push and pull request (`.github/workflows/ci.yml`), all on clean Windows runners: `full`
(ruff, mypy and every test with all extras), `minimal` (a plain install: core tests, optional ones skip),
`e2e-windows` (Delta's browser tools in a real Chrome or Edge against a local test site) and `evals-scripted`
(the eval suite with a scripted model, which must pass 100%). A `security` job (bandit, pip-audit on the
locked dependencies) and CodeQL report but do not block. The tests run on Linux too for development, except
the ones that need Windows (the registry, PowerShell, Windows paths). Release history: [CHANGELOG.md](CHANGELOG.md).
What has not been checked on real hardware: [docs/NOT_VERIFIED.md](docs/NOT_VERIFIED.md).

## Roadmap

- [x] Hybrid model router: Codex, Claude Code, Cline, NIM, Groq, DeepSeek, LM Studio, Ollama
- [x] Computer use, your own browser, Deep Research in ChatGPT/Gemini
- [x] Telegram remote control with voice, files, PIN and lockout
- [x] Question forms, memory, skill learning, partner-style chat
- [x] One-line installer, setup wizard, light/dark theme, low resource use
- [ ] Slack control (in progress)
- [x] Scheduled tasks and routines
- [x] Prompt-injection containment (enforced in code) and DPAPI encryption of memory and chats
- [x] Emergency help: WhatsApp alerts and calls to chosen contacts, from the app or Telegram
- [ ] End-to-end encrypted channel (Matrix), signed releases

## Creator

**Delta is created and maintained by Adharsh Kumar Bachu.**
If you use, fork or build on Delta, keep the [NOTICE](NOTICE) file and the credit, as the
Apache 2.0 license requires. Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).
