# Creates "Delta" shortcuts on the Desktop and in the Start menu.
# One click starts Delta with no console window and opens the dashboard; clicking again
# while it runs just reopens the dashboard. Stop it with the dashboard's Shutdown button.
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "Run 'uv sync --extra all' first." }

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
    $lnk.TargetPath = Join-Path $env:WINDIR "System32\wscript.exe"
    $lnk.Arguments = "`"" + (Join-Path $root "scripts\delta.vbs") + "`""
    $lnk.WorkingDirectory = $root
    $lnk.Description = "Delta personal AI agent"
    $icon = Join-Path $root "src\delta\api\static\delta.ico"
    if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
    $lnk.Save()
    Write-Host "created $path"
}
