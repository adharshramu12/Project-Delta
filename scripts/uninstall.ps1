# Removes Delta: stops it, deletes the app folder and its Desktop/Start-menu icons.
# Your data (%LOCALAPPDATA%\Delta: chats, memory, settings) is kept unless you pass -RemoveData.
# Saved keys live in Windows Credential Manager under "delta"; they are removed with -RemoveData too.
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
                   (Join-Path ([Environment]::GetFolderPath("Programs")) "Delta.lnk"))) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "removed $lnk" }
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
    cmdkey /list | Select-String -Pattern "Target: (.*delta.*)" | ForEach-Object {
        cmdkey /delete:$($_.Matches[0].Groups[1].Value.Trim()) | Out-Null
    }
    Write-Host "removed Delta's saved keys"
} else {
    Write-Host "kept your data in $data (run with -RemoveData to delete it)"
}
Write-Host "Delta is uninstalled."
