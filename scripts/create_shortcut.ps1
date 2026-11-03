# Creates "Delta" shortcuts on the Desktop and in the Start menu.
# One click starts Delta with no console window and opens the dashboard; clicking again
# while it runs just reopens the dashboard. Stop it with the dashboard's Shutdown button.
#
# The shortcut runs the venv's pythonw.exe directly. Not a .vbs (Windows is retiring VBScript and
# Explorer no longer runs it), and the venv must come from the official, signed python.org Python:
# with Smart App Control on, Windows silently blocks an unsigned python.exe started from the Desktop.
$root = Split-Path $PSScriptRoot -Parent
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
