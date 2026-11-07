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

## Windows security prompts

What a new PC may show, and what to do. Delta needs no administrator rights and changes no security setting.

- **No firewall prompt is expected.** Delta's dashboard listens on `127.0.0.1` (this PC only), which Windows
  Firewall does not ask about. If you ever see "Windows Defender Firewall has blocked some features" for Python,
  something set Delta's address to listen on the network: choose **Cancel**, then check `api: host` in
  `%LOCALAPPDATA%\Delta\settings.yaml` (it should be `127.0.0.1`).
- **"Windows protected your PC" (SmartScreen).** It can appear when you double-click `Install Delta.cmd` from a
  downloaded zip, because the file is new to Microsoft. The `irm` command does not trigger it (it runs as text, not
  as a downloaded file). To go on with the zip: choose **More info**, check that the file is `Install Delta.cmd`
  from the folder you extracted, then **Run anyway**. Or close it and use the `irm` command instead.
- **Microsoft Defender.** If Defender stops one of Delta's files, open Windows Security > Virus & threat protection > Protection history and read what was blocked.
  Do not turn Defender off. Run the install command again (it repairs the files), and if the same file is stopped
  again, report it as a false positive to Microsoft and open an issue on GitHub with the name Defender gave it.
- **Smart App Control** blocks unsigned programs: Delta runs on the official signed Python, so the icon works. If
  it is blocked, run the `irm` command again (see the table below).

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
  It shuts Delta down (on the port in your settings) and closes its window, then removes the folder, the icons,
  the Path entry and "Start Delta when I sign in to Windows". Add `-RemoveData` to delete your chats, memory,
  settings and saved keys too.

  **What always stays**, listed at the end of the uninstall with exact paths:
  - **The files Delta made for you**: the save folder, normally **Delta files** on your Desktop
    (`%USERPROFILE%\Delta` when OneDrive holds your Desktop, with a "Delta files" shortcut on the Desktop). They are
    yours, so the uninstaller never deletes them. Delete the folder yourself when you no longer need it.
  - **Python 3.12**, the one Delta was built on (normally `%LOCALAPPDATA%\Programs\Python\Python312`; the uninstaller
    prints the exact path). The installer reuses it or installs it, and other programs may use it too. To remove it: Settings > Apps > Installed apps, find "Python 3.12", then Uninstall. Or
    in PowerShell: `winget uninstall --id Python.Python.3.12`.

## Start with Windows

Delta runs until you shut it down or restart the PC. After a restart it is off, and Telegram stops answering, until
you open it again. To have it start by itself, turn on **Start Delta when I sign in to Windows** in Settings >
Setup and shutdown (setup offers it when you connect Telegram). It adds one entry for your account only
(`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, named `DeltaAgent`, no administrator rights) that starts
Delta in the background with no window. Turning it off, or uninstalling, removes that entry. Task Manager's
Startup apps tab shows it as well, and turning it off there is respected.

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
| A failed install | The installer says what went wrong and leaves your PowerShell window open. Fix that cause, then paste the command again. |
| `delta` is not recognized | Open a new PowerShell window (the Path is read when a window opens). If it still fails: `powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Delta\scripts\delta_command.ps1" -Action Install` |
| "running scripts is disabled on this system" | Use the `irm` command; it runs as text, which that setting does not cover, and changes nothing. |
| Smart App Control blocks the icon | Run the `irm` command again: it rebuilds Delta on the official signed Python. `delta doctor` says whether Delta's Python is signed. |
| Company network or proxy | Set the proxy for your account, open a new window, run the command again: `setx HTTPS_PROXY http://proxy.example:8080`. The installer uses Windows' certificate store, so a company's HTTPS inspection certificate is trusted. |
| The icon does nothing, your user name has letters like a, e with accents or non-Latin script | Fixed: icons are written with the Unicode shortcut interface. Run the install command again to rewrite them. |
| Desktop is in OneDrive | Nothing to do: the icon goes to the Desktop Windows really shows, and Delta keeps its files on this PC. |
| "Could not create SSL/TLS secure channel" | Older Windows PowerShell: run `[Net.ServicePointManager]::SecurityProtocol = 3072`, then the command again. |
| Not enough disk space | The first install needs about 2 GB free on the drive Delta is installed to (your user folder's drive, or the one in `DELTA_INSTALL_DIR`). |
| "Windows protected your PC", Defender, firewall | See [Windows security prompts](#windows-security-prompts) above. |
| The icon says Delta is running in a terminal | You started `delta cli` (or `delta`) in a terminal. Only one Delta runs at a time: type `/quit` there, then open Delta from its icon. |
| Telegram stops answering after a restart | Turn on Start Delta when I sign in to Windows (see [Start with Windows](#start-with-windows)). |
| Port 7878 is in use | `delta doctor` says so; add `api: {port: 7879}` to `%LOCALAPPDATA%\Delta\settings.yaml`, then start Delta again. |
