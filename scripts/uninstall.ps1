# Removes Delta: stops it, deletes the app folder, its Desktop/Start-menu icons (and "Update Delta") and takes the
# delta command off your Path.
# Your data (%LOCALAPPDATA%\Delta: chats, memory, settings) is kept unless you pass -RemoveData.
# Saved keys live in Windows Credential Manager under "delta"; they are removed with -RemoveData too.
#
# Run it with (works whatever the PC's execution policy is, and changes none):
#   powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Delta\scripts\uninstall.ps1"
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Delta"),
    [switch]$RemoveData
)
$data = Join-Path $env:LOCALAPPDATA "Delta"

# Stop a running Delta the way its Shutdown button does.
$tokenFile = Join-Path $data "api_token"
if (Test-Path $tokenFile) {
    try {
        Invoke-RestMethod -Method Post -Uri http://127.0.0.1:7878/api/shutdown -TimeoutSec 5 `
            -Headers @{ Authorization = "Bearer " + (Get-Content $tokenFile -Raw).Trim() } | Out-Null
        Start-Sleep 5
    } catch {}
}

# Anything still running from Delta's folders (the service on another port, a hung copy, the dashboard window):
# a running program keeps its files locked, so the folder could not be removed.
$venv = (Join-Path $InstallDir ".venv").TrimEnd("\") + "\"
$window = (Join-Path $data "app-window").ToLowerInvariant()
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [System.StringComparison]::OrdinalIgnoreCase)) -or
    ($_.Name -eq "msedge.exe" -and $_.CommandLine -and $_.CommandLine.ToLowerInvariant().Contains($window))
} | ForEach-Object { & taskkill.exe /PID $_.ProcessId /T /F 2>$null | Out-Null }
Start-Sleep -Seconds 1

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
} else {
    Write-Host "kept your data in $data (run with -RemoveData to delete it)"
}
Write-Host "Delta is uninstalled."
