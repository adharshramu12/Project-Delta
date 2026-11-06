# Changelog

All notable changes to Delta are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and Delta uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version lives in one place,
`pyproject.toml`; `delta --version` reads it from there.

## Unreleased

### Added
- **"Delta is in control" signal**: while Delta drives the mouse, the keyboard or a browser (your own browser or
  Delta's private one), an orange ring follows the pointer, a thin orange border marks each screen it works on,
  and a small label says "Delta is working. Press Esc twice or say stop". Click-through, never focused, always on
  top, kept out of screenshots and screen recording (Windows 10 2004+; older Windows: hidden for Delta's own
  screenshots), high contrast and per-monitor DPI aware. It disappears at once when the task ends, fails or is
  stopped, and by itself within a few seconds if Delta hangs; it dies with Delta's process. Your real mouse
  pointer is never recoloured any more (an older version's orange pointer is still restored after a crash). Turn
  it off in Settings, Appearance (same setting as before, `browser.orange_cursor`).
- **Stop anytime**: a Stop button on every running task in the Tasks view (Chat already had one) and Stop all;
  `/stop`, `/stop all` and a plain "stop" or "stop all" message in Telegram and Slack (`delta.control.stop_command`,
  ready for any chat channel); Esc twice within a second while Delta is driving, or Ctrl+Alt+Shift+S, on the PC.
  A stop refuses any further key or mouse press, releases every key and mouse button Delta still holds, hides the
  ring, closes nothing of yours, and marks the task stopped. Waiting tasks can be stopped by id too.
- **Faster app opening**: an app that is already open is brought to the front instead of started again
  (`new_window=true` starts another copy); installed apps are indexed (Start-menu shortcuts, App Paths, Store apps)
  and cached in the data folder, rebuilt only when one of those changed, so PowerShell (Get-StartApps) no longer
  runs on the first "open X" after every start; apps start through ShellExecute (App Paths names, shortcuts,
  AppUserModelID) instead of a helper process; window detection polls faster at first and takes a single-copy
  app's window as soon as it comes forward. Store app names in any language survive PowerShell 5.1 (UTF-8).
- **Self-heal for app and browser steps**: `open_app` tries the next way of starting an app (exe, App Paths,
  Start-menu shortcut, AppUserModelID; at most 3) when Windows refuses one or nothing runs, never starts an app
  twice, logs each recovery and says plainly what failed. A browser step that finds its window gone reopens the
  page the task was on once and retries. Stopped steps are never retried.
- **One writing style everywhere**: chat answers, emails, app messages and documents use crisp, plain Indian
  English. A short style block in the prompts plus a deterministic slop check (`delta/core/style.py`, no model call)
  that rewrites long dashes, removes AI phrases ("I hope this email finds you well", delve, game-changer), fixes US
  spellings and "gotten", and drops emojis from professional text (kept in casual chat when the user uses them).
  URLs, code, paths, flags and tables are never touched. Outgoing email gets at most one cheap model rewrite, only
  when something code cannot fix is left. See [docs/STYLE_AND_IDENTITY.md](docs/STYLE_AND_IDENTITY.md).
- **Warmth without pretending**: Delta notices stress, frustration and good news and answers kindly and patiently;
  it never claims real feelings or to be human, and a slip saying so is replaced in code with an honest line.
- **Email with per-email approval**: `draft_email` writes the draft in the user's voice; `send_email` shows the whole
  draft with every recipient and sends only after Approve. Edit (say what to change) and Cancel send nothing.
  Dashboard: Approve / Edit / Cancel; Telegram: Approve / Edit / Cancel; Slack: Approve / Cancel. Asked at every
  permission level, never "for this task", never learned. Sent through the Gmail app when it can send, else Gmail
  in the user's browser.
- **"Who made you?"**: answered at once with no model call: "I'm Project Delta. I was built by Adharsh Kumar Bachu,
  founder of ARVA Talks Academy." No personal details. One constants file for forks: `src/delta/identity.py`.
- **ARVA Talks Academy line**: after a proper answer to "how do I learn AI / agents / prompt engineering ...", one
  light line at most once per conversation; never in unrelated tasks or in text written for others.
  `academy.enabled: false` turns it off.
- **Apps is the one place for apps**: the "mcp" tick left Settings > Add-ons. Custom MCP servers start by themselves
  when configured and sit under Apps > Advanced: custom MCP servers with one switch. Old settings are migrated once
  with nothing lost.
- Scripted evals `evals/tasks/style.yaml` (identity, slop check, warmth, academy line, email approval) and an `asked`
  checker.
- **Saved logins in your own browser**: `sign_in` now fills a saved login in the browser the task uses, including
  your own default browser (Chrome or Edge, through Windows UI Automation on Delta's one window there), instead of
  moving the task to the private browser. Every existing rule applies the same way (exact origin, no lookalike,
  punycode, downgrade, other port or cross-origin frame; the password never in the model's view, a log, the audit
  log, memory or a screenshot; a wrong password never retried; the same permission, PIN and Remote Guard rules;
  no DPAPI, no saving). New for your own browser: a one-time consent per site and browser that names both and is
  asked even for "Always allow" (remembered in the sealed login; "Ask every time" withdraws it), a tab binding that
  stops, clears and submits nothing when you switch tabs or the page moves mid-fill, values set into the box itself
  rather than typed into whatever has focus, a check that the box kept what was filled (extensions), password boxes
  painted over in screenshots, and one normal submit. Audit entries say which browser (`private`, `own:chrome`,
  `own:edge`). New outcome `aborted`. See [docs/LOGINS.md](docs/LOGINS.md).
- **Browser actions in your own browser**: `browser_select`, `browser_check`, `browser_upload` (asks first),
  `browser_hover`, `browser_wait`, `browser_tabs` (Delta's own window only) and `browser_dialog` now work in your
  own browser too, by label. Your browser choice is never changed for you.
- **Own-browser evals and tests**: two scripted evals (`portal-own-browser-login`,
  `portal-own-browser-lookalike-refused`) and end-to-end tests that run the own-browser rules against the fake
  portal in a real Chrome or Edge (attached over CDP as a stand-in for your everyday browser), plus an end-to-end
  test of the real UI Automation driver that runs where the runner's desktop allows it.
- **Saved site logins**: save a login for a site such as a college ERP or LMS on the PC (Settings, Logins and
  watchers, or `delta logins add`), then ask Delta to use it. One DPAPI-sealed file per login (saving is refused
  without DPAPI); bound to one exact origin, so lookalike hosts, http downgrades, other ports and cross-origin
  frames are refused before anything is typed. The password is masked in screenshots and never reaches the
  model, a log or the audit log. Per-login permission (ask every time, the default, or always for this site);
  PIN for sign-ins approved from a phone; one-time codes and reCAPTCHA-type checks handed to the user on the PC.
  New tools `sign_in` and `list_logins`; Telegram `/logins` (read-only). See [docs/LOGINS.md](docs/LOGINS.md).
- **Portal watchers**: "watch my ERP for exams and notifications". Schedules of kind `watch` (default every 6
  hours, at most hourly; manual checks at most every 10 minutes) sign in, read each page with a recipe and no AI,
  compare with a DPAPI-sealed snapshot and report only what is new, at once or in a daily digest. New exams and
  deadlines are offered as reminders and made only when the user says so. Recipes are learned per site with
  `inspect_portal` and `save_watch_recipe` and kept as skills. `delta watch list|run|remove`, Telegram
  `/watchers`, and a Settings panel. See [docs/WATCHERS.md](docs/WATCHERS.md).
- **Steadier browser actions** in Delta's private browser: retries with a check afterwards that the page really
  changed, elements inside frames, new tabs and dialogs, scrolling lists, and new tools `browser_select`,
  `browser_check`, `browser_upload` (asks first), `browser_hover`, `browser_wait`, `browser_tabs` and
  `browser_dialog`.
- **Portal evals**: five scripted evals against the fake college portal (`evals/tasks/portal.yaml`): sign in and
  read the timetable, report only the new exam, refuse a lookalike site, ignore an injection in a portal page,
  and hand a reCAPTCHA-type check to the user. Each also proves the saved password appears nowhere.
- **Attachments**: send pictures (JPG, PNG, WebP, GIF), PDF, Word (.docx), PowerPoint (.pptx) and text files
  (.txt, .md, .csv) in the dashboard chat (paper clip, drag and drop, paste) or on Telegram (photo or file with a
  caption, albums as one request). Read on the PC first with a standard-library PDF reader and an OOXML reader
  with hardened XML parsing, Windows OCR for pictures and scanned pages, and Windows' PDF engine to render scanned
  pages. Only a compact extract with page or slide numbers goes with the request; the AI reads more with
  `read_attachment` and looks at a picture with `look_at_attachment` only when the picture matters. Opt-in
  picture-only vision route (`attachments.vision`, `vision_model`); default providers and routing are unchanged.
  File content is outside content (taint rule and ExfilGuard apply) and memories from it have the origin
  `attachment`. 20 MB per file, 5 per message, removed after 30 days; extracts cached by file hash. The usage
  line, task progress and chat usage box show attachment tokens. New `attachments` settings section and a
  Settings, Attachments panel. See [docs/ATTACHMENTS.md](docs/ATTACHMENTS.md).
- **Connected apps (MCP)**: connect Notion, GitHub, Atlassian, Linear, Slack, Sentry, Stripe, Gmail, Google
  Drive and Calendar, Asana, Vercel, Supabase, Hugging Face, Figma (desktop) and custom MCP servers from the
  dashboard's Apps page, `delta apps` or Telegram `/connect` (PIN), then ask Delta to work in them. A curated,
  versioned catalog (`config/mcp_catalog.yaml`) checked against each vendor's docs; OAuth 2.1 with PKCE, discovery
  and dynamic client registration; tokens sealed with DPAPI (refused without it); Streamable HTTP with SSE
  fallback and stdio servers with pinned versions; lazy tool loading (`find_app_tool` / `use_app_tool`);
  per-tool risk and switches; time limits, read-only retries and a circuit breaker; 16 scripted evals. Works
  without the optional `mcp` extra. See [docs/APPS.md](docs/APPS.md).

- `connectors.idle_close_s` (default 600): a local app server (npx / uvx) unused for 10 minutes is stopped to free
  its memory and started again on the next call. 0 keeps the old behaviour.

### Security
- A Telegram message shown as a Windows notification could run PowerShell (`$(...)` in the text) before any PIN
  check. The notification text is now passed to Windows as data only.

### Fixed
- Documents and posts: a zero-width joiner inside Telugu, Hindi or Malayalam text was removed as if it were an
  emoji, changing the words. Only emoji sequences are removed now.
- On a PC with the default execution policy (Restricted, so no `.ps1` file runs), `irm .../install.ps1 | iex`
  installed and checked Delta, then failed at step 5 with "running scripts is disabled on this system" for
  `delta_command.ps1` and `create_shortcut.ps1`, and said "Delta could not be installed". The installer runs as text,
  which the policy does not cover, but it started those two scripts as files. Now no script starts another of
  Delta's scripts as a file: `install.ps1`, `create_shortcut.ps1` and `uninstall.ps1` read the other script's text
  and run it in the same PowerShell, which also keeps them clear of Smart App Control's check of downloaded script
  files. No execution policy is changed, for the PC or the account. A delta command or icons that still fail at
  step 5 are a warning ("Some optional parts were not set up (Delta itself works)") with the exact command that
  adds them, not a failed install. The installer removes the "downloaded from the internet" mark from the files it
  installs (not the environment), and the messages that name a script (uninstall, Delta's browser profile) give a
  command with `-ExecutionPolicy Bypass -File`, which applies to that one run only. The Desktop icon (`pythonw.exe`),
  **Update Delta** (`python.exe -m delta update --stop`), `delta update` and `Install Delta.cmd` already worked under
  that policy and are now tested under it. The README leads with the `irm` line and says that Smart App Control may
  block `Install Delta.cmd` from a downloaded zip. Real Smart App Control behaviour is not verified yet (see
  [docs/NOT_VERIFIED.md](docs/NOT_VERIFIED.md)).
- In a normal PowerShell, `delta update` (and every other `delta` command the README shows) said "delta is not
  recognized": Delta lives in its own environment and Python is installed without touching the Path. The installer
  now adds a `delta` command (`%LOCALAPPDATA%\Programs\Delta\bin\delta.cmd`) and puts that folder on your own
  account's Path (never the PC-wide one; nothing is added twice; `-NoPath` and `-NoShortcuts` leave it alone), tells
  open programs that the Path changed, and says "Open a new PowerShell window to use the delta command." The Start
  menu gets **Update Delta** (in a Delta folder), which runs `delta update --stop`. An update keeps `bin` in place.
  `scripts\uninstall.ps1` takes the folder off the Path and removes **Update Delta**. The README gives two fallbacks:
  the install line again (updates in place, keeps your data) and Delta's Python by its full path.
- A picture attached in the dashboard could take 20 seconds or more to show as read on a slow PC: Windows OCR
  started PowerShell twice (a probe, then the read), and the next file waited behind it. The first OCR read is now
  also the probe (one PowerShell start), and attached files are uploaded and read at the same time.

### Fixed (fresh-laptop audit)
- A damaged `memory.db`, `chats.db`, `learning.db`, `skills.db`, `schedules.db` or `tasks.db` (power cut, full disk)
  stopped Delta from starting. The damaged file is now kept as `<name>.damaged-<date>` and a new one is opened, with
  a start-up message.
- A last audit-log line cut off by a power cut made every later action fail. New records now chain to the last
  whole record; `delta audit verify` still reports the cut line.
- A stale `delta.pid` whose process id Windows had reused for another program made Delta refuse to start
  ("already running") after a crash or power cut.
- A broken `settings.yaml` (or a value Delta cannot use) gave a traceback; it now names the file and how to fix it.
- A save folder on a drive that is gone, a slow first PowerShell start, or a user folder with an apostrophe
  (`C:\Users\D'Souza`) no longer stop start-up or break the Desktop shortcut.
- Local app servers' logs are kept to about 1 MB each; the learner no longer keeps ids of tasks that ended with a
  question.
- `delta` and the Desktop icon start faster (httpx loads only for `delta doctor` and the service).
- Installer: TLS 1.2 on older Windows PowerShell, a free-space check, plain messages when uv or the packages cannot
  be downloaded (proxy hint), and `Install Delta.cmd` explains when it was opened inside the zip. Uninstall stops
  anything still running from Delta's folders first.
- CodeQL findings: the Telegram token is checked before it becomes part of an address, documents are served by
  their listed name only, a schedule-parsing regex is linear, and duplicate emoji ranges were removed.

### Changed
- Captcha help (`solve_captcha`) and sign-in share one solver: the allowlist rule is unchanged, Windows OCR is
  tried first and at most 3 attempts are made before a hand-off.
- The dashboard's payload cap is 221 KB (was 215 KB) for the Logins and watchers panel.
- Telegram `/apps` now shows your connected apps; the list of open windows moved to `/windows` (a button under
  `/apps` opens it too).

## 0.2.0 - Unreleased

Not tagged yet. Everything merged after 0.1.0 (pull requests #9 to #23) plus the production hygiene batch.

### Added
- **Captchas, opt-in for your own accounts** (#10): `solve_captcha` reads a simple image or text captcha on a
  site you allowed with `delta captcha allow <host>` (2 tries, then it asks you). reCAPTCHA, hCaptcha and
  Cloudflare Turnstile are always refused; off by default.
- **Faster, more exact browser control** (#12): numbered element refs, act-and-observe snapshots after every
  browser action, and one bounded smart wait instead of fixed sleeps.
- **"Allow for this task" approvals** (#15): one yes covers the same kind of action on the same target for the
  rest of a task; never saved, never for permanent deletes, code, terminals or data-carrying links.
- **Browser speed and token savings** (#16): snapshot diffs on the same page, event-driven settling, leaner
  history, a per-task token meter on every answer and an optional per-task token budget
  (`agent.token_budget`).
- **Writer and Documents** (#17): posts that read human for LinkedIn, Medium and Substack (Delta never presses
  Post), and PDF, Word and Markdown documents (report, letter, resume, notes, assignment, invoice, paper).
- **Scheduled tasks and routines** (#18): once, daily, weekly, every N minutes or hours, and cron, through the
  same safety path as a typed request; PIN needed to create one from a phone.
- **Browser choice everywhere** (#19): your default browser, Chrome, Edge or Delta's private browser, from
  Settings, Telegram `/browser` or chat, applied per task; `/signin` helper.
- **Durable task engine** (#20): `tasks.db` with task states and a step log, crash resume (never automatic),
  a tool watchdog that ends whole process trees, the eval harness (`delta eval run`) and a run timeline.
- **Self-heal and cheap-first routing** (#21): a failure taxonomy, deterministic recovery playbooks, a strategy
  ladder (page, UI Automation, OCR, vision, ask you), lessons, `delta undo`, and an opt-in model ladder.
- **Perception and parallel work** (#22): set-of-marks pictures, Windows OCR, and opt-in parallel read-only
  helpers with their own budgets.
- **Memory, learning and grounded answers** (#23): memory with provenance and trust levels, secret redaction
  before storage, `delta memory` and `delta learn`, a learning gate, and answer grounding that lists claims
  the task could not confirm.
- Issue templates (bug, feature), a pull request template, Dependabot (GitHub Actions and Python packages,
  weekly), a CodeQL workflow and a non-required `security` CI job (bandit, pip-audit on the locked packages).
- `docs/NOT_VERIFIED.md`: what has not been checked on real hardware.
- `delta update --stop`, and `agent.child_env_allow` for environment variables a command really needs.

### Changed
- The installer reuses a signed x64 Python 3.12 found through the `py` launcher or the registry (never the
  Store Python), installs the x64 build on Windows on ARM (runs under emulation) and closes a running Delta
  before replacing files (it asks first).
- `delta update` no longer runs the installer from inside the running Delta environment: it refuses while
  Delta runs (or closes it with `--stop`) and starts the installer in its own window after it exits.
- **Config lookup:** a `config/delta.yaml` in the current folder is no longer picked up silently. Delta reads
  its own `config/` (or `DELTA_CONFIG_DIR` when set) plus your `settings.yaml`.
- **Token budget:** answering "Keep going" continues with the limit raised by one more budget on top of what
  was already used (and audited as `task.budget_extended`), instead of a silent fresh budget.
- **MCP tools:** a server's own `readOnlyHint` no longer makes a tool "read only" on its own; unknown MCP tools
  count as sending data outside unless you set their risk (`tools:` / `risk:`) or `trust_read_only_hints`.
  MCP results now mark the task as having read outside content.
- Delta's private browser picks a free remote-control port at each launch instead of the fixed 9223, and
  reattaches after a restart only to a browser it started itself.
- CI: every action pinned to a full commit SHA, moved off the Node 20 actions, read-only token by default,
  `uv sync --locked`.
- README: real test and eval counts, overclaims removed; SECURITY.md aligned with the code.
- Version 0.2.0, read from `pyproject.toml` only.

### Fixed
- Hub broadcast crash when a dashboard joins or leaves mid-broadcast (#9).
- A stalled dashboard can no longer hold up events: each WebSocket send has a time limit, sends run side by
  side, and a socket is always removed from the hub however it ends.
- No stuck "running" tasks after an unexpected error; safe proof pruning (#14).
- `run_python` no longer leaves temporary folders behind (the scratch folder stays only if a file was made in
  it), and the backup copy folder used before edits is removed.
- `*.cmd` and `*.bat` files are checked out and zipped with CRLF line endings.

### Security
- `run_shell` classification fails closed (newlines, backticks, subexpressions, encoded commands), the
  Telegram pairing code is exact-match with an attempt limit, approvals are safe under concurrency (#11).
- No read-then-leak through web addresses after a task read outside content; secret files and folders are on
  the hard floor; desktop clicks and typing are judged by their real target (#13).
- Remote Guard is enforced centrally for every task, tool call and approval, and `/lock` covers approvals (#14).
- Autopilot asks less, with the list of what still asks made exact in the UI and docs (#15).
- **Wrong PINs are counted in a row and saved**: 5 wrong PINs lock remote control even across restarts (before,
  the count lived in memory within a 15-minute window).
- **Programs Delta starts** (`run_shell`, `run_python`) no longer inherit API keys, tokens, passwords or
  `DELTA_<NAME>` key overrides from Delta's environment.
- **Codex fails closed:** if it refuses Delta's lean safety settings it is not used, instead of running with
  the user's own plugins, MCP servers and shell.
- **No plain-text fallback for encryption:** on Windows, if DPAPI or pywin32 is not working, chats and memories
  are refused (with an explanation on start-up) instead of being stored unencrypted.
- **Signed audit log:** new records also carry an HMAC chain keyed by a secret in Windows Credential Manager;
  `delta audit verify` names the first broken line.
- The API only ever listens on loopback (a non-loopback `api.host` is replaced and logged); the API token
  file is created atomically with owner-only permissions; token comparisons never crash on non-ASCII input.

## 0.1.0 - 2026-11-03

First public version (pull requests #1 to #7).

### Added
- Planner, act and verify loop on Windows with a hybrid model router: Codex, Claude Code, Cline, NVIDIA NIM,
  Groq, DeepSeek, LM Studio and Ollama, with automatic fallback.
- Computer use (apps, files, keyboard, mouse, screen), Delta's own browser and the user's browser, Deep
  Research, Google Meet scheduling, WhatsApp and Telegram messages, emergency alerts to chosen contacts.
- Telegram remote control with voice notes, files, a PIN (Remote Guard) and lockout; a dashboard app with a
  setup wizard; a one-line installer.
- Policy engine with permission levels, approvals that time out to deny, prompt-injection containment, DPAPI
  encryption of memory and chats, a hash-chained audit log, memory and skill learning.
- A live eval report (7/7 scenarios with Claude Code) and documentation (architecture, add-ons, security).
