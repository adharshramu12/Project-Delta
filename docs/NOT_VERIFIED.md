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
- `delta update` while Delta is running: the "close it first" message, `delta update --stop` closing it, and
  the installer waiting for the update command to exit before replacing files.

## Windows features

- Real Windows Credential Manager and DPAPI on a user's PC: the encrypted memory and chats, the audit log's
  signing key, and the refusal to save when DPAPI is broken (only simulated in tests).
- Registry known folders, a Desktop redirected by OneDrive, and PowerShell commands on a user's own setup.
- Windows OCR on languages other than English.
- Desktop input safety against a live PowerShell window, the Run box and Delta's own window (the tests use
  fakes for these).
- Delta's private browser on a free remote-control port: covered by the CI end-to-end test on a clean runner,
  not yet on a PC where an older Delta browser is still open from before 0.2.0.

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

## Resource use

- The "about 100 MB at rest" figure and the battery and GPU guard come from the author's own laptop, not from
  a measured set of machines.
