# Opens Delta's own browser profile so you can sign in to the sites you want Delta to use
# (Gmail, GitHub, ...). Close the window when you are done. Your normal browser profile is untouched.
# Uses Google Chrome if installed, otherwise Microsoft Edge (same order as Delta itself).
$browsers = @(
    @{ name = "chrome"; paths = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                                  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                                  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") },
    @{ name = "edge";   paths = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
                                  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe") }
)
foreach ($b in $browsers) {
    $exe = $b.paths | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($exe) {
        $profileDir = Join-Path $env:LOCALAPPDATA "Delta\$($b.name)-profile"
        New-Item -ItemType Directory -Force $profileDir | Out-Null
        Start-Process $exe "--user-data-dir=`"$profileDir`" --no-first-run --no-default-browser-check https://accounts.google.com"
        Write-Host "Opened Delta's $($b.name) profile. Sign in to your sites, then close the window."
        exit 0
    }
}
throw "No supported browser found. Install Google Chrome or Microsoft Edge."
