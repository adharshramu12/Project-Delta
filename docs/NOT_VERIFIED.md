# What has not been verified on real hardware

Delta's tests and evals run on every push, on clean Windows runners in GitHub Actions (see the CI section in
the README). Those runners are virtual machines with no screen user, no phone, no webcam and no signed-in AI
apps, so some parts of Delta can only be checked by hand on a real PC. This page lists them honestly, so you
know what has been tested by machines and what has not. If you check one of these on your own PC, an issue
saying what you found (it worked, or how it failed) is very welcome.

## Installing and updating

- The full installer on a clean Windows 10 and Windows 11 laptop: `irm ... | iex` and double-clicking
  `Install Delta.cmd`, with Smart App Control on, the Desktop and Start-menu icons, the splash screen and the
  setup wizard.
- **Windows on ARM** (Snapdragon, Copilot+ PCs): installing the x64 Python 3.12 under emulation, building
  Delta's environment there, and running Delta. Not tried on an ARM PC.
- Finding an existing Python 3.12 through the `py` launcher and the registry (all-users installs, a per-user
  install in a custom folder), and skipping the Microsoft Store Python.
- The installer's newer checks on a real PC: the free-space check, the TLS 1.2 switch on an older Windows 10, the
  plain messages when `pip install uv` or `uv sync` fail behind a company proxy, and `Install Delta.cmd` opened from
  inside the zip. Their PowerShell syntax is checked (PowerShell 7 parser), but they did not run on Windows 5.1.
- A user folder with a non-ASCII or apostrophe name (`C:\Users\Rāj Kumar`, `C:\Users\D'Souza`): the Desktop
  shortcut quoting is unit-tested; the full install from such a folder is not.
- `scripts\uninstall.ps1` stopping a Delta that runs on another port and its dashboard window before removing
  the folder.
- `delta update` while Delta is running: the "close it first" message, `delta update --stop` closing it, and
  the installer waiting for the update command to exit before replacing files.
- The `delta` command on a real user's PC: a new PowerShell window (5.1 and 7) and Windows Terminal finding it
  after the installer adds `%LOCALAPPDATA%\Programs\Delta\bin` to the account's Path, `delta update` and the
  **Update Delta** Start-menu item replacing files while started through `bin\delta.cmd`, an install that started
  with an older installer, and the warning when another `delta` (the git diff tool) comes first on the Path. The CI
  Windows runner checks the Path entry, `delta --version` from a new process and uninstall removing it, in a test
  folder with a space, an apostrophe, `&` and a non-ASCII letter in its name.

## Windows features

- Real Windows Credential Manager and DPAPI on a user's PC: the encrypted memory and chats, the audit log's
  signing key, and the refusal to save when DPAPI is broken (only simulated in tests).
- Registry known folders, a Desktop redirected by OneDrive, and PowerShell commands on a user's own setup.
- Windows OCR on languages other than English.
- Desktop input safety against a live PowerShell window, the Run box and Delta's own window (the tests use
  fakes for these).
- Delta's private browser on a free remote-control port: covered by the CI end-to-end test on a clean runner,
  not yet on a PC where an older Delta browser is still open from before 0.2.0.

## Connected apps (MCP)

- **No real account was used in CI.** Notion, GitHub, Atlassian, Linear, Slack, Sentry, Stripe, Gmail, Google
  Drive and Calendar, Asana, Vercel, Supabase, Hugging Face and Figma were checked against their own documentation
  and, for the remote sign-ins, by reading their live 401 answer and OAuth metadata (on 2026-11-06). The full
  connect, call, refresh and revoke cycle is tested only against Delta's fake notes app with its fake OAuth server
  (unit tests, scripted evals and the Windows end-to-end test).
- The browser sign-in pages of each vendor, their consent screens, and whether each one accepts Delta's
  `resource` parameter and loopback redirect (Google requires a "Web application" client with the exact redirect
  URL; Slack and Asana need your own app) have not been tried by hand.
- Real tool lists: only Google's tool names are verified from its docs; other apps' tools default to "asks first"
  until you change them on the Apps page.
- The local reference servers (`npx` and `uvx`) at their pinned versions on a real PC, and Node.js / uv being
  missing on a stranger's laptop (the message is tested, the install is not).

## Saved logins and watchers

- **DPAPI on a fresh laptop**: saving, opening and refusing logins and watcher snapshots with real DPAPI on a newly
  set up Windows account. CI's Windows runners use real DPAPI in the end-to-end test, but not on a user's PC or
  after a Windows password change.
- **OCR on real captchas**: the "OCR first, at most 3 attempts" rule is tested with a captcha picture drawn by the
  fake portal. Real portal captchas (noise, warped letters) were not tried, so expect more hand-offs than in tests.
- **Real ERP and LMS sign-ins**: multi-step forms (user name first, password on the next page), single sign-on
  that sends you through another site, and forms submitted by JavaScript were tested only against the fake college
  portal. A portal that signs in on another address (SSO) is refused by design until you save a login for that
  address.
- **OTP hand-off through Telegram**: the hand-off is an ordinary approval, so it reaches Telegram like any other,
  but sending the masked picture of the waiting page to the phone has no automated test and was not tried on a real
  phone.
- **Recipe learning with a real model**: `inspect_portal` and `save_watch_recipe` are tested with scripted calls.
  How well a real model picks the right tables and keys on a real portal is not measured.
- **The browser window during watcher runs**: whether Delta's private browser window appears or takes focus on the
  desktop while a scheduled watcher runs (it should open one tab and close what it opened) was not checked on a
  real screen.

## Phone and remote control

- Telegram pairing, the PIN unlock, `/lock`, Remote Guard approvals from a phone, and Windows notifications.
- The wrong-PIN count surviving a real restart of the service on a PC (tested with the state file, not with a
  phone).

## AI providers

- The sign-in AI apps (Codex, Claude Code, Cline) against real signed-in apps, including Codex refusing
  Delta's safety settings (tested against a fake Codex only).
- API keys against the live providers (NVIDIA NIM, Groq, DeepSeek), and local servers (LM Studio, Ollama).
- The live eval reports in `evals/reports` were run by hand on the author's PC with a real model; the CI
  eval run uses a scripted model and proves the harness and the agent loop, not a model's skill.

## Attachments

- **Windows PDF rendering** (`winpdf.py`, Windows.Data.Pdf through Windows PowerShell 5.1) for scanned pages and
  protected PDFs on a real PC. Unit tests replace it with a fake renderer; the rendering itself did not run on a
  user's PC.
- **Windows OCR on attachments**: reading attached pictures and rendered PDF pages with the OCR built into
  Windows, with real phone photos and scans, and the "text only" path when no OCR language is installed.
- **Real Telegram downloads**: photos, documents and albums from a real phone through the Bot API, including files
  near the 20 MB limit. Tests use a fake Telegram.
- **Real vision providers with low detail**: that OpenAI-compatible providers accept the low-detail request and
  bill about 85 tokens, and how other providers count a downscaled picture. The token figures in the docs are
  estimates from the providers' published rules, not measured bills.

## Resource use

- Idle use measured on Linux (the service with no task, every default on): about 70 MB of memory, 3 threads,
  about 0.2 CPU seconds a minute. Windows adds pywin32 and the UI Automation libraries when the desktop tools
  first run; it has not been measured on a 4 GB dual-core laptop.
- A local app server (npx / uvx) is stopped after 10 idle minutes (`connectors.idle_close_s`) and started again
  on the next call: tested with the fake stdio server, not with the real Node or Python servers.
- The "about 100 MB at rest" figure and the battery and GPU guard come from the author's own laptop, not from
  a measured set of machines.
