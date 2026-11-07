# Creates "Delta" shortcuts on the Desktop and in the Start menu, and "Update Delta" in a Delta folder in the
# Start menu (it runs `delta update --stop`; there is no Desktop icon for it).
# One click starts Delta with no console window and opens the dashboard; clicking again
# while it runs just reopens the dashboard. Stop it with the dashboard's Shutdown button.
#
# The shortcut runs the venv's pythonw.exe directly. Not a .vbs (Windows is retiring VBScript and
# Explorer no longer runs it), and the venv must come from the official, signed python.org Python:
# with Smart App Control on, Windows silently blocks an unsigned python.exe started from the Desktop.
# Neither shortcut runs a PowerShell script, so a PC whose execution policy runs no scripts (Restricted, the
# Windows default) opens and updates Delta the same way.
#
# Run it yourself with: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1
param(
    [switch]$NoCommand,  # install.ps1 sets up the delta command itself and passes this
    [string]$Root = ""   # Delta's folder; empty = the folder above this script. install.ps1 runs this file's text
                         # (no $PSScriptRoot then), so it passes the folder
)
if (-not $Root) { $Root = Split-Path $PSScriptRoot -Parent }
$root = $Root.TrimEnd("\")
$installed = -not (Test-Path (Join-Path $root ".git"))   # an installed copy, not a developer checkout
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Delta's environment is missing: run Delta's installer first (or 'uv sync --extra all' in a developer checkout)." }
$sac = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" -ErrorAction SilentlyContinue).VerifiedAndReputablePolicyState
if ($sac -eq 1 -and (Get-AuthenticodeSignature $pythonw).Status -ne "Valid") {
    Write-Warning ("Smart App Control is on and .venv uses an unsigned Python, so Windows will block the shortcut. " +
                   "Run Delta's installer again to rebuild .venv on the official python.org Python 3.12.")
}

# A "Delta" folder on the Desktop shows up as a second "Delta" icon (Windows hides ".lnk"), and
# double-clicking it opens an empty folder instead of the app. Older versions saved files there.
$desktop = [Environment]::GetFolderPath("Desktop")
$clash = Join-Path $desktop "Delta"
if (Test-Path $clash -PathType Container) {
    if (-not (Get-ChildItem $clash -Force)) {
        Add-Type -AssemblyName Microsoft.VisualBasic
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($clash, "OnlyErrorDialogs", "SendToRecycleBin")
        Write-Host "moved the empty Desktop\Delta folder to the Recycle Bin (it hid the app icon)"
    } else {
        Write-Warning "Desktop\Delta is a folder with files; rename it so it is not confused with the Delta app icon."
    }
}

# Written through the Unicode shortcut interface (scripts\shell_link.ps1): WScript.Shell alone turned a user folder
# such as C:\Users\R<a with macron>j into C:\Users\Raj, a folder that does not exist. Its text is run here (policy).
$linkText = [System.IO.File]::ReadAllText((Join-Path $root "scripts\shell_link.ps1"))
. ([scriptblock]::Create($linkText))
$icon = Join-Path $root "src\delta\api\static\delta.ico"
if (-not (Test-Path $icon)) { $icon = "" }
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Delta.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Delta.lnk")
)
foreach ($path in $targets) {
    [void](Set-DeltaShortcut -Path $path -Target $pythonw -Arguments "-m delta app" -WorkingDirectory $root `
        -Description "Delta personal AI agent" -Icon $icon)
    Write-Host "created $path"
}

if ($installed) {
    # "Update Delta" in the Start menu: the same as `delta update --stop` (closes a running Delta, keeps your data).
    $menu = Join-Path ([Environment]::GetFolderPath("Programs")) "Delta"
    $path = Join-Path $menu "Update Delta.lnk"
    [void](Set-DeltaShortcut -Path $path -Target (Join-Path $root ".venv\Scripts\python.exe") `
        -Arguments "-m delta update --stop" -WorkingDirectory $root `
        -Description "Update Delta to the newest version (closes Delta first, keeps your data)" -Icon $icon)
    Write-Host "created $path"
}

# The `delta` terminal command. install.ps1 does this itself (-NoCommand); this covers an update started by an
# older installer, which runs this newer script but does not know about the command.
if ($installed -and -not $NoCommand) {
    try {
        # Its text, run here, not the file: the execution policy (Restricted on a new PC) refuses a .ps1 file
        # when this script itself was run as text by install.ps1.
        $block = [scriptblock]::Create([System.IO.File]::ReadAllText((Join-Path $root "scripts\delta_command.ps1")))
        $command = & $block -Action Install -InstallDir $root | Select-Object -Last 1
        if ($command -and $command.PathAdded) {
            Write-Host "added the delta command ($($command.Bin) is on your Path). Open a new PowerShell window to use it."
        }
    } catch {
        Write-Warning "The delta command could not be set up: $($_.Exception.Message)"
    }
}
