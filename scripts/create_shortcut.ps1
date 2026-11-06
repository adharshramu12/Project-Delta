# Creates "Delta" shortcuts on the Desktop and in the Start menu, and "Update Delta" in a Delta folder in the
# Start menu (it runs `delta update --stop`; there is no Desktop icon for it).
# One click starts Delta with no console window and opens the dashboard; clicking again
# while it runs just reopens the dashboard. Stop it with the dashboard's Shutdown button.
#
# The shortcut runs the venv's pythonw.exe directly. Not a .vbs (Windows is retiring VBScript and
# Explorer no longer runs it), and the venv must come from the official, signed python.org Python:
# with Smart App Control on, Windows silently blocks an unsigned python.exe started from the Desktop.
param(
    [switch]$NoCommand   # install.ps1 sets up the delta command itself and passes this
)
$root = Split-Path $PSScriptRoot -Parent
$installed = -not (Test-Path (Join-Path $root ".git"))   # an installed copy, not a developer checkout
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Run scripts\install.ps1 (or 'uv sync --extra all') first." }
$sac = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy" -ErrorAction SilentlyContinue).VerifiedAndReputablePolicyState
if ($sac -eq 1 -and (Get-AuthenticodeSignature $pythonw).Status -ne "Valid") {
    Write-Warning ("Smart App Control is on and .venv uses an unsigned Python, so Windows will block the shortcut. " +
                   "Run scripts\install.ps1 to rebuild .venv on the official python.org Python 3.12.")
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

$shell = New-Object -ComObject WScript.Shell
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Delta.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "Delta.lnk")
)
foreach ($path in $targets) {
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = "-m delta app"
    $lnk.WorkingDirectory = $root
    $lnk.Description = "Delta personal AI agent"
    $icon = Join-Path $root "src\delta\api\static\delta.ico"
    if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Save()
    Write-Host "created $path"
}

$icon = Join-Path $root "src\delta\api\static\delta.ico"
if ($installed) {
    # "Update Delta" in the Start menu: the same as `delta update --stop` (closes a running Delta, keeps your data).
    $menu = Join-Path ([Environment]::GetFolderPath("Programs")) "Delta"
    [void][System.IO.Directory]::CreateDirectory($menu)
    $path = Join-Path $menu "Update Delta.lnk"
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = Join-Path $root ".venv\Scripts\python.exe"
    $lnk.Arguments = "-m delta update --stop"
    $lnk.WorkingDirectory = $root
    $lnk.Description = "Update Delta to the newest version (closes Delta first, keeps your data)"
    if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Save()
    Write-Host "created $path"
}

# The `delta` terminal command. install.ps1 does this itself (-NoCommand); this covers an update started by an
# older installer, which runs this newer script but does not know about the command.
if ($installed -and -not $NoCommand) {
    try {
        $command = & (Join-Path $PSScriptRoot "delta_command.ps1") -Action Install -InstallDir $root | Select-Object -Last 1
        if ($command -and $command.PathAdded) {
            Write-Host "added the delta command ($($command.Bin) is on your Path). Open a new PowerShell window to use it."
        }
    } catch {
        Write-Warning "The delta command could not be set up: $($_.Exception.Message)"
    }
}
