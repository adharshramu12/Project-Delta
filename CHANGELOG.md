# Changelog

All notable changes to Delta are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and Delta uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version lives in one place,
`pyproject.toml`; `delta --version` reads it from there.

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
