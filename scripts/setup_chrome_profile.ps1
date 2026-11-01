# Opens Delta's own Chrome profile so you can sign in to the sites you want Delta to use
# (Gmail, GitHub, …). Close the window when you are done. Your normal Chrome profile is untouched.
$profileDir = Join-Path $env:LOCALAPPDATA "Delta\chrome-profile"
New-Item -ItemType Directory -Force $profileDir | Out-Null
$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw "Google Chrome not found." }
Start-Process $chrome "--user-data-dir=`"$profileDir`" --no-first-run --no-default-browser-check https://accounts.google.com"
Write-Host "Sign in to your sites in the window that opened, then close it."
