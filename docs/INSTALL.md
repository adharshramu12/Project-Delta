# Installing Delta on Windows

The short version is in the README: open PowerShell and paste

```powershell
irm https://raw.githubusercontent.com/adharshramu12/Project-Delta/main/scripts/install.ps1 | iex
```

This page is the long version: what the installer does, which PCs it is tested on, the options, and what to do
when something goes wrong. After any install, `delta doctor` in a new PowerShell window checks the PC and prints
the exact fix for anything that is not right.

## What the installer does

1. **Python 3.12 (64-bit, x64, signed).** Reuses one that is already installed (found through the `py` launcher,
   the registry or the usual folders) when it is the official signed build and not the Microsoft Store one.
   Otherwise installs it for your account only (winget, or python.org's own signed installer). No administrator
   rights. Windows on ARM (Snapdragon, Copilot+ PCs) gets the x64 Python under Windows' built-in emulation,
   because some packages (OpenCV) have no ARM64 build yet.
2. **Delta's files** go to `%LOCALAPPDATA%\Programs\Delta`, downloaded from GitHub (or copied from the folder you
   ran `Install Delta.cmd` from). The "downloaded from the internet" mark is removed from them.
3. **Delta's environment** (`.venv`) is built from the exact locked package versions (`uv sync --frozen`).
4. **Check:** Delta must start and report its version.
5. **The `delta` command** (`bin\delta.cmd`, added to your account's Path, never the PC-wide one) and the
   **Desktop and Start-menu icons** ("Delta", and "Update Delta" in a Delta folder in the Start menu). These are
   optional: if one cannot be made, the installer says so and prints the one command that adds it.
6. **Opens Delta**, whose setup page takes it from there.

Your data (`%LOCALAPPDATA%\Delta`: chats, memory, settings) is never touched by an install, update or uninstall
(unless you ask uninstall to remove it). The full log is `%TEMP%\delta-install.log`.

## Options

`irm ... | iex` cannot take parameters, so these can also be set as environment variables first (a parameter
wins when both are given):

| Environment variable | Parameter | What it does |
|---|---|---|
| `DELTA_REF` | `-Branch` | A branch, tag or full commit id to install instead of `main` |
| `DELTA_REPO` | `-Repo` | Install from a fork (`owner/name`) |
| `DELTA_INSTALL_DIR` | `-InstallDir` | Another install folder |
| `DELTA_NO_LAUNCH=1` | `-NoLaunch` | Do not open Delta at the end |
| | `-NoShortcuts` | No icons, and leave the `delta` command alone (a trial install) |
| | `-NoPath` | Make `bin\delta.cmd` but do not add it to your Path |

Example, a specific version:

```powershell
$env:DELTA_REF = "v0.3.0"; irm https://raw.githubusercontent.com/adharshramu12/Project-Delta/main/scripts/install.ps1 | iex
```

With parameters, run the downloaded text as a script block:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/adharshramu12/Project-Delta/main/scripts/install.ps1))) -NoLaunch
```

## Update, repair, uninstall

- **Update:** "Update Delta" in the Start menu, or `delta update --stop` in a new PowerShell window. Running the
  install command again also updates (and repairs) in place; it is safe to run as often as you like.
- **Uninstall:**
  `powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Delta\scripts\uninstall.ps1"`.
  It removes the folder, the icons and the Path entry. Add `-RemoveData` to delete your chats, memory, settings
  and saved keys too.

## Tested

Every change runs the real one-liner on clean GitHub runners (CI job `install-oneliner`, steps in
`tests/ci/install_oneliner.ps1`), against the commit being tested:

| | |
|---|---|
| Windows | Windows Server 2025 and 2022 (the Windows 11 24H2 and Windows 10 / 21H2 code bases), Windows 11 on ARM64 |
| PowerShell | Windows PowerShell 5.1 and PowerShell 7 |
| Execution policy | Restricted (a new PC) and AllSigned; nothing changes the policy |
| Folder | the normal profile folder, and one with spaces, an apostrophe, a macron and Telugu letters |
| After install | `delta --version` and `delta doctor` in a new window, the three shortcuts point at real files |
| Again | a second run keeps the environment (update/repair), then uninstall removes everything |

Not covered by CI (see docs/NOT_VERIFIED.md for how they were reasoned about): Windows 10 22H2 and Windows 11
desktop editions themselves, Smart App Control turned on, a OneDrive-redirected Desktop, a company proxy, and a
PC with no Python and no winget.

## When something goes wrong

Run `delta doctor` first (in a new PowerShell window). It checks Python, the `delta` command, Delta's folders,
the dashboard port, the browser, Windows OCR, Telegram and your AI models, and prints a fix for each problem.

| What you see | What to do |
|---|---|
| `delta` is not recognized | Open a new PowerShell window (the Path is read when a window opens). If it still fails: `powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Delta\scripts\delta_command.ps1" -Action Install` |
| "running scripts is disabled on this system" | Use the `irm` command; it runs as text, which that setting does not cover, and changes nothing. |
| Smart App Control blocks the icon | Run the `irm` command again: it rebuilds Delta on the official signed Python. `delta doctor` says whether Delta's Python is signed. |
| Company network or proxy | Set the proxy for your account, open a new window, run the command again: `setx HTTPS_PROXY http://proxy.example:8080`. The installer uses Windows' certificate store, so a company's HTTPS inspection certificate is trusted. |
| The icon does nothing, your user name has letters like a, e with accents or non-Latin script | Fixed: icons are written with the Unicode shortcut interface. Run the install command again to rewrite them. |
| Desktop is in OneDrive | Nothing to do: the icon goes to the Desktop Windows really shows, and Delta keeps its files on this PC. |
| "Could not create SSL/TLS secure channel" | Older Windows PowerShell: run `[Net.ServicePointManager]::SecurityProtocol = 3072`, then the command again. |
| Not enough disk space | The first install needs about 2 GB free on the drive with your user folder. |
| Port 7878 is in use | `delta doctor` says so; add `api: {port: 7879}` to `%LOCALAPPDATA%\Delta\settings.yaml`, then start Delta again. |
