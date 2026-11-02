# Creates "Delta" shortcuts on the Desktop and in the Start menu.
# One click starts Delta with no console window and opens the dashboard; clicking again
# while it runs just reopens the dashboard. Stop it with the dashboard's Shutdown button.
$root = Split-Path $PSScriptRoot -Parent
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "Run 'uv sync --extra all' first." }

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
