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
- **Smart App Control** on a real PC: `irm ... | iex` with Smart App Control on (the installer runs Delta's other
  scripts from their text and removes the "downloaded from the internet" mark from the files it installs), Windows
  blocking a double-clicked `Install Delta.cmd` from a downloaded zip, and `delta update` and the fix commands the
  installer prints (`powershell -ExecutionPolicy Bypass -File ...`) on such a PC. The CI Windows runner checks the
  installer's step 5, `delta update`, `Install Delta.cmd` and the Unblock-File step with the account's execution
  policy set to Restricted, but it has no Smart App Control. Since the `install-oneliner` CI job (docs/INSTALL.md),
  the whole one-liner, steps 1 to 6, runs under Restricted and AllSigned on Windows PowerShell 5.1 and 7.
- **Windows on ARM** (Snapdragon, Copilot+ PCs): the CI job installs on GitHub's Windows 11 ARM64 runner (x64
  Python under emulation, the environment, `delta --version`, `delta doctor`). Using Delta day to day on an ARM
  laptop (screen, apps, browser) is not tried.
- Finding an existing Python 3.12 through the `py` launcher and the registry (all-users installs, a per-user
  install in a custom folder), and skipping the Microsoft Store Python.
- The installer's newer checks on a real PC: the free-space check, the TLS 1.2 switch on an older Windows 10, the
  plain messages when `pip install uv` or `uv sync` fail behind a company proxy, and `Install Delta.cmd` opened from
  inside the zip. Their PowerShell syntax is checked (PowerShell 7 parser), but they did not run on Windows 5.1.
- A user folder with a non-ASCII or apostrophe name (`C:\Users\Rāj Kumar`, `C:\Users\D'Souza`): CI installs into
  such a folder (spaces, an apostrophe, a macron, Telugu letters) and checks the shortcuts hold the exact Unicode
  path; a real Windows account with such a name (its TEMP and AppData paths) is not tried.
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
- The "Delta is in control" ring and border on real hardware: multiple monitors with different scaling, high
  contrast themes, full-screen games or videos, and Windows 10 before 2004 (where screenshots hide it instead of
  excluding it). The CI end-to-end test checks the windows, capture exclusion and the stop messages on one
  runner monitor only.
- Esc twice on a real keyboard (CI can only post the stop message: keys it injects are ignored on purpose), and
  Ctrl+Alt+Shift+S when another app already owns that shortcut (Delta then logs it and the other stops still work).
- Opening apps: reuse of an open window and the Start-menu / App Paths / Store-app paths on a PC with many apps
  installed, OneDrive-redirected Start menus, and Smart App Control blocking one way of starting an app. CI opens
  Notepad only.

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

## Email, writing style and identity

- **Sending email** with a real Gmail account: the browser path (the compose page filled from the approved draft,
  then Send, then Gmail's "Message sent") and the Gmail-app path are tested with fakes only. Google's official Gmail
  MCP server has no send tool, so with it Delta sends through the browser; a third-party Gmail server with a send
  tool is matched by its tool name and arguments and has not been tried with a real server.
- The **Edit** button on a real phone in Telegram (Edit, then a reply with the changes) and the dashboard's Edit box
  are tested in code, not by hand.
- WhatsApp has no Approve / Edit / Cancel buttons: Delta does not take commands or approvals over WhatsApp (it only
  types into the WhatsApp app for you). Approvals come to the dashboard, Telegram and Slack.
- How well cheap real models follow the style, warmth and identity rules in other languages (Telugu, Hindi in
  English letters): the code checks are tested; real-model answers are not measured yet.

## Saved logins and watchers

- **DPAPI on a fresh laptop**: saving, opening and refusing logins and watcher snapshots with real DPAPI on a newly
  set up Windows account. CI's Windows runners use real DPAPI in the end-to-end test, but not on a user's PC or
  after a Windows password change.
- **Captcha reading on real portals**: the captcha model and Windows OCR are measured only on generated pictures
  (`delta.evals.captcha_bench`, three styles drawn with Pillow's built-in font, 120 pictures; the table is in
  docs/LOGINS.md). No real college or government portal captcha was tried, including the KL ERP one: real fonts,
  heavier warping and other noise may read worse. Mixed-case captchas are the weak spot (the model often cannot
  tell a small letter from its capital); a site that checks case-blind does better than the exact-match numbers.
  The model's settings (passes, capital lean, case margin) were chosen on benchmark seeds that are not the reported
  ones, but from the same generator, so the numbers may flatter it.
- **The captcha model on other PCs**: installed by the one-line installer and checked by `delta doctor` (it reads a
  test picture) on CI's Windows Server 2025 and 2022 x64 runners and on Windows 11 on ARM (x64 Python under
  emulation, as the installer sets it up). Not tried on a real user's PC, on an older x64 CPU without AVX, or with
  antivirus that blocks onnxruntime's DLLs; if the model fails to load, Delta falls back to Windows OCR (tested only
  with a stand-in) and `delta doctor` shows a WARN.
- **The #40 terminal turn-off migration**: read from the login file's last write time. Tested with files whose times
  are set by the test; a PC whose clock was wrong when the switch was changed, or files copied in a way that resets
  their times, may read the switch as on. A permission change or Replace made under #40 reads as off (the safe
  side). Logins already rewritten by #43's migration cannot be recovered and keep the new default.
- **The vision fallback with your own AI**: one small low-detail picture per try, only when the captcha model and
  Windows OCR are not sure. Tested with a stand-in model; whether a real vision model (or the Codex CLI provider)
  reads such a small crop well, and so how many of the unsure captchas it rescues, was not measured (the benchmark
  makes no vision calls and only counts them).
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

- **Logins in your own browser on a real PC**: the UI Automation driver (`delta.logins.own_uia`) that fills a saved
  login in your everyday Chrome or Edge, with real signed-in sessions and real extensions (password managers,
  autofill, ad blockers), was not tried on a real PC. CI checks the same rules with an attached browser standing in
  for yours, and runs the real driver only when the runner's desktop exposes the page through UI Automation. Things
  to check: that the page's full address (with http or https) and each frame's address are readable, that values
  set through UI Automation are kept by real sign-in forms (React and similar), that the length check works for
  password boxes, and how a password manager extension that fills the same box behaves.
- **The one-time own-browser consent**: that the question names the browser and site on the dashboard, Telegram and
  the terminal, that it is asked once per site and browser even for "Always allow", and that "Ask every time"
  withdraws it, on a real PC and phone.
- **Tab switch and navigation abort**: that switching tabs, clicking another window, moving the mouse or a page
  redirect during a fill in your real browser stops the sign-in, clears the boxes and submits nothing. Tested with a
  fake browser and with the attached stand-in, not with a person using a real browser.
- **Masked pictures of your own browser**: the painted-over password area follows the box's position when it was
  filled; a page that scrolls or moves the box afterwards was not checked on a real screen.
- **The captcha picture from your own browser**: Delta finds the Image next to the captcha box through UI Automation
  and captures that rectangle from the screen (`UIAOwnDriver.captcha_image`), now per-monitor DPI aware, with a band
  around the box as the fallback picture. Not tried on a real PC: display scaling above 100%, a captcha drawn on a canvas or as a CSS background (no Image element), and whether "clicking" the
  picture through UI Automation gives a new one on real portals.
- **The captcha reply from a phone**: the picture is sent to Telegram as a photo and the reply goes through the same
  chat; tested with the dashboard path and fakes, not with a real phone. The dashboard shows the picture with the
  answer only in the live chat (not after a reload).
- **The real college ERP from the report**: the fix was reproduced and tested against a fake portal built like it
  (hidden page until a late stylesheet, "Enter verification Code", hidden MFA box, Yii-style field names), not
  against the real site or its real captcha pictures.

- **The extra browser actions in your own browser** (`browser_select`, `browser_check`, `browser_upload`,
  `browser_hover`, `browser_wait`, `browser_tabs`, `browser_dialog` through UI Automation): only their argument
  handling and routing are tested; the UI Automation parts were not run against a real browser.

## Phone and remote control

- Telegram pairing, the PIN unlock, `/lock`, Remote Guard approvals from a phone, and Windows notifications.
- The wrong-PIN count surviving a real restart of the service on a PC (tested with the state file, not with a
  phone).
- **The Telegram "/" menu on a real bot**: that a phone shows the new grouped list after Delta starts (Telegram
  apps cache the menu; reopening the chat may be needed), and that clearing the old default and per-chat lists
  behaves as the Bot API documents. Tested against a fake bot only. `/new`, `/chats`, `/open`, `/find` and their
  buttons were tested with fake Telegram updates, not from a real phone.
- **WhatsApp menu**: Delta has no WhatsApp control channel, so the typed menu and chat commands
  (`delta.channels.menu.handle_text`) are only unit tested; nothing receives WhatsApp messages yet.
- **Long-chat recall with a real model**: the scripted evals check what the request carries (the quoted older line
  with its turn number, or nothing and the no-guess line); whether a real model then answers from it, or says it
  doesn't know, was not measured.

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
