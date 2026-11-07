# Removes Delta: stops it (and its dashboard window), deletes the app folder, its Desktop/Start-menu icons (and
# "Update Delta"), takes the delta command off your Path and removes "Start Delta when I sign in to Windows".
# Your data (%LOCALAPPDATA%\Delta: chats, memory, settings) is kept unless you pass -RemoveData.
# Saved keys live in Windows Credential Manager under "delta-agent"; they are removed with -RemoveData too.
# Never removed, and listed at the end with their exact paths: the files Delta made for you (your save folder,
# normally "Delta files" on the Desktop), because they are yours, and Python 3.12, which other programs may use.
#
# Run it with (works whatever the PC's execution policy is, and changes none):
#   powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Delta\scripts\uninstall.ps1"
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Delta"),
    [switch]$RemoveData
)
$data = Join-Path $env:LOCALAPPDATA "Delta"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$approvedKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
$autostartName = "DeltaAgent"   # delta.system.autostart.VALUE

# What Delta itself says about where things are, read before its folder goes: the dashboard's port (a port changed
# in settings.yaml included; 7878 was assumed before, so a Delta on another port was killed, not shut down), its
# data folder and its save folder. Defaults when Delta's Python cannot say.
function Get-DeltaRuntime([string]$dir) {
    $d = if ("$env:DELTA_DATA_DIR".Trim()) { "$env:DELTA_DATA_DIR".Trim() } else { $data }
    $rt = [pscustomobject]@{ Host = "127.0.0.1"; Port = 7878; Data = $d; Save = ""; FromDelta = $false }
    $py = Join-Path $dir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $py)) { return $rt }
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $code = ("import json; from delta.config import load_settings, save_dir; s = load_settings(); " +
                 "print('DELTA-RUNTIME ' + json.dumps({'host': s.api.host, 'port': s.api.port, " +
                 "'data': str(s.data_dir), 'save': str(save_dir(s.files, create=False))}))")
        $line = @(& $py -c $code 2>$null) | Where-Object { "$_".StartsWith("DELTA-RUNTIME ") } | Select-Object -Last 1
        if ($line) {
            $j = "$line".Substring(14) | ConvertFrom-Json
            $rt.Host = if ("$($j.host)" -in "", "0.0.0.0", "::") { "127.0.0.1" } else { "$($j.host)" }
            $rt.Port = [int]$j.port
            $rt.Data = "$($j.data)"
            $rt.Save = "$($j.save)"
            $rt.FromDelta = $true
        }
    } catch { } finally { $ErrorActionPreference = $eap }
    return $rt
}
$rt = Get-DeltaRuntime $InstallDir
# The Python 3.12 Delta's environment was built from (.venv\pyvenv.cfg, "home = <folder>"), read before the folder
# goes: it stays installed, and the end of this script says where it is.
$basePython = $null
try {
    $cfg = Get-Content -LiteralPath (Join-Path $InstallDir ".venv\pyvenv.cfg") -ErrorAction Stop
    $homeLine = @($cfg | Where-Object { $_ -match '^\s*home\s*=\s*(.+?)\s*$' }) | Select-Object -First 1
    if ($homeLine -and $homeLine -match '^\s*home\s*=\s*(.+?)\s*$') {
        $exe = Join-Path $Matches[1] "python.exe"
        if (Test-Path -LiteralPath $exe) { $basePython = $exe }
    }
} catch { }

# Stop a running Delta the way its Shutdown button does (it closes its own window too).
$tokenFile = Join-Path $rt.Data "api_token"
if (Test-Path -LiteralPath $tokenFile) {
    try {
        Invoke-RestMethod -Method Post -Uri ("http://{0}:{1}/api/shutdown" -f $rt.Host, $rt.Port) -TimeoutSec 5 `
            -UseBasicParsing -Headers @{ Authorization = "Bearer " + (Get-Content -LiteralPath $tokenFile -Raw).Trim() } | Out-Null
        Write-Host ("asked Delta to shut down (its dashboard on port {0})" -f $rt.Port)
        Start-Sleep 5
    } catch {}
}

# Anything still running from Delta's folders (a hung copy, `delta cli`) and Delta's own dashboard window (Edge with
# Delta's private profile, never the user's browser): a running program keeps its files locked.
$venv = (Join-Path $InstallDir ".venv").TrimEnd("\") + "\"
$profiles = @((Join-Path $rt.Data "app-window"), (Join-Path $data "app-window")) | ForEach-Object { [regex]::Escape($_.ToLowerInvariant()) } |
            Sort-Object -Unique
$targets = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $proc = $_
    ($proc.ExecutablePath -and $proc.ExecutablePath.StartsWith($venv, [System.StringComparison]::OrdinalIgnoreCase)) -or
    ($proc.Name -eq "msedge.exe" -and $proc.CommandLine -and
     @($profiles | Where-Object { $proc.CommandLine.ToLowerInvariant() -match ("--user-data-dir=""?" + $_ + "(""|\s|$)") }).Count -gt 0)
})
foreach ($p in $targets) { & taskkill.exe /PID $p.ProcessId /T /F 2>$null | Out-Null }
Start-Sleep -Seconds 1

# "Start Delta when I sign in to Windows" (Settings > Setup and shutdown): its Run value, and Task Manager's note on it.
foreach ($key in $runKey, $approvedKey) {
    if (Get-ItemProperty -Path $key -Name $autostartName -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $key -Name $autostartName -ErrorAction SilentlyContinue
        if ($key -eq $runKey) { Write-Host "removed 'Start Delta when I sign in to Windows'" }
    }
}

foreach ($lnk in @((Join-Path ([Environment]::GetFolderPath("Desktop")) "Delta.lnk"),
                   (Join-Path ([Environment]::GetFolderPath("Programs")) "Delta.lnk"),
                   (Join-Path ([Environment]::GetFolderPath("Programs")) "Delta\Update Delta.lnk"))) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "removed $lnk" }
}
$menu = Join-Path ([Environment]::GetFolderPath("Programs")) "Delta"
if ((Test-Path $menu -PathType Container) -and -not (Get-ChildItem $menu -Force)) { Remove-Item $menu -Force }

# The delta command: its folder (bin) comes off your account's Path. The script is read before the folder goes.
$command = Join-Path $PSScriptRoot "delta_command.ps1"
if (-not (Test-Path $command)) { $command = Join-Path $InstallDir "scripts\delta_command.ps1" }
if (Test-Path $command) {
    try {
        # Its text, run here, not the file, so the execution policy (Restricted on a new PC) does not refuse it.
        $block = [scriptblock]::Create([System.IO.File]::ReadAllText($command))
        $removed = & $block -Action Uninstall -InstallDir $InstallDir | Select-Object -Last 1
        if ($removed -and $removed.PathRemoved) { Write-Host "removed the delta command from your Path" }
    } catch {
        Write-Host "Could not take the delta command off your Path: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}
if (Test-Path $InstallDir) {
    try { Remove-Item $InstallDir -Recurse -Force -ErrorAction Stop; Write-Host "removed $InstallDir" }
    catch {
        Write-Host "Could not remove $InstallDir completely: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "Restart the PC and run this again, or delete that folder by hand."
        exit 1
    }
}

if ($RemoveData) {
    if (Test-Path $data) { Remove-Item $data -Recurse -Force; Write-Host "removed your Delta data ($data)" }
    # Only Delta's own entries: service "delta-agent", saved by Python's keyring as "delta-agent" or
    # "<name>@delta-agent". Matching any "delta" in the name also deleted other programs' saved logins (a bank or
    # airline called Delta...). The "Target:" label is translated on non-English Windows, so it is not matched.
    cmdkey /list | Select-String -Pattern 'target=((?:[^\s@]+@)?delta-agent)\s*$' | ForEach-Object {
        $name = $_.Matches[0].Groups[1].Value
        cmdkey "/delete:LegacyGeneric:target=$name" 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { cmdkey "/delete:$name" 2>$null | Out-Null }
    }
    Write-Host "removed Delta's saved keys"
}
Write-Host "Delta is uninstalled."

# What stays, said plainly with exact paths, so nothing is left behind without the person knowing.
$kept = New-Object System.Collections.Generic.List[string]
if (-not $RemoveData -and (Test-Path -LiteralPath $data)) {
    $kept.Add("Your chats, memory and settings: $data (run this again with -RemoveData to delete them).")
}
$save = if ($rt.Save) { $rt.Save } else { Join-Path ([Environment]::GetFolderPath("Desktop")) "Delta files" }
if (Test-Path -LiteralPath $save) {
    $kept.Add("The files Delta made for you: $save. They are yours, so they are never deleted for you. " +
              "Delete that folder yourself when you no longer need them.")
}
$saveLink = Join-Path ([Environment]::GetFolderPath("Desktop")) "Delta files.lnk"
if (Test-Path -LiteralPath $saveLink) { $kept.Add("The Desktop shortcut to them: $saveLink.") }
$python = $basePython
foreach ($c in @((Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"))) {
    if (-not $python -and (Test-Path -LiteralPath $c)) { $python = $c }
}
if (-not $python) {
    try {
        $reg = Get-ItemProperty -Path "HKCU:\Software\Python\PythonCore\3.12\InstallPath" -ErrorAction Stop
        $p = if ($reg.ExecutablePath) { [string]$reg.ExecutablePath } else { Join-Path ([string]$reg."(default)") "python.exe" }
        if (Test-Path -LiteralPath $p) { $python = $p }
    } catch { }
}
if ($python) {
    $kept.Add("Python 3.12 ($python). Delta's installer uses it (or installed it), and other programs may use it too, " +
              "so it stays. To remove it: Settings > Apps > Installed apps, find 'Python 3.12', then Uninstall " +
              "(or in PowerShell: winget uninstall --id Python.Python.3.12).")
}
if ($kept.Count) {
    Write-Host "`nKept on this PC:"
    foreach ($k in $kept) { Write-Host "  - $k" }
}
