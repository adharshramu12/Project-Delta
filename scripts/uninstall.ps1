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

foreach ($lnk in @((Join-Path ([Environment]::GetFolderPath("Desktop")) "Delta.lnk"),
                   (Join-Path ([Environment]::GetFolderPath("Programs")) "Delta.lnk"))) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "removed $lnk" }
}
if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force; Write-Host "removed $InstallDir" }

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
