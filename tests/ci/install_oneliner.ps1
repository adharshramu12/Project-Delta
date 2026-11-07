# CI only (job install-oneliner in .github/workflows/ci.yml): the one-command install a new user types, run for
# real on a clean Windows runner against the commit being tested, then everything a user does next.
#
#   1. under the execution policy a new PC has (Restricted, set for this account and put back at the end):
#      $env:DELTA_REF = <commit>; irm https://raw.githubusercontent.com/<repo>/<commit>/scripts/install.ps1 | iex
#   2. a NEW PowerShell (Path read from the registry, as a new window): delta --version, delta doctor, which delta
#   3. the Desktop and Start-menu shortcuts point at files that exist (read back as Unicode)
#   4. the same one-liner again (repair/update): succeeds, keeps the environment
#   5. uninstall.ps1: folder, shortcuts and the Path entry are gone
#   6. again into a folder with spaces, an apostrophe, a macron and Telugu letters (a user name like that), under
#      AllSigned: shortcuts hold the exact Unicode path, delta --version runs from it; then uninstall
# Every Delta command runs in a child process of $Shell (powershell = Windows PowerShell 5.1, or pwsh = 7).
# Keep this file ASCII: Windows PowerShell 5.1 reads a file without a BOM in the ANSI code page.
param(
    [Parameter(Mandatory = $true)][string]$Shell,
    [Parameter(Mandatory = $true)][string]$Ref,
    [Parameter(Mandatory = $true)][string]$Repo,
    [Parameter(Mandatory = $true)][string]$Version
)
$ErrorActionPreference = "Stop"
$exe = (Get-Command $Shell).Source
$url = "https://raw.githubusercontent.com/$Repo/$Ref/scripts/install.ps1"
$default = Join-Path $env:LOCALAPPDATA "Programs\Delta"
$linkPs = Join-Path $PSScriptRoot "..\..\scripts\shell_link.ps1"
$newWindow = "`$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + " +
             "[Environment]::GetEnvironmentVariable('Path','User'); "

function Child([string]$command, [hashtable]$envs = @{}) {
    # A new PowerShell as a user opens it: no -ExecutionPolicy, no profile, PSModulePath of its own.
    $saved = @{}
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # a child's stderr line is output to show, not an error here
    foreach ($k in @($envs.Keys) + "PSModulePath") { $saved[$k] = [Environment]::GetEnvironmentVariable($k) }
    try {
        [Environment]::SetEnvironmentVariable("PSModulePath", $null)
        foreach ($k in $envs.Keys) { [Environment]::SetEnvironmentVariable($k, $envs[$k]) }
        $out = & $exe -NoProfile -Command ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding `$false; " +
                                           $command) 2>&1 | ForEach-Object { "$_" }
        $code = $LASTEXITCODE
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
        $ErrorActionPreference = $eap
    }
    $out | ForEach-Object { Write-Host "    | $_" }
    return [pscustomobject]@{ Code = $code; Out = ($out -join "`n") }
}

function Check([bool]$ok, [string]$what) {
    if (-not $ok) { throw "FAILED: $what" }
    Write-Host "PASS  $what" -ForegroundColor Green
}

function Install([string]$dir, [string]$policy) {
    $envs = @{ DELTA_REF = $Ref; DELTA_REPO = $Repo; DELTA_NO_LAUNCH = "1" }
    if ($dir) { $envs["DELTA_INSTALL_DIR"] = $dir }
    $t = Get-Date
    $r = Child "Write-Host ('policy: ' + (Get-ExecutionPolicy)); irm '$url' | iex" $envs
    Check ($r.Code -eq 0) "one-liner exit code under $policy (got $($r.Code)) in $([int]((Get-Date) - $t).TotalSeconds) s"
    Check ($r.Out -match "policy: $policy") "the one-liner really ran under $policy"
    Check ($r.Out -match "Delta is installed") "installer says Delta is installed"
}

function Verify([string]$dir) {
    $r = Child ($newWindow + "(Get-Command delta).Source; delta --version; exit `$LASTEXITCODE")
    Check ($r.Code -eq 0) "delta --version in a new window"
    $lines = @($r.Out -split "`n" | Where-Object { $_.Trim() })
    Check ([IO.Path]::GetFullPath($lines[0].Trim()) -eq [IO.Path]::GetFullPath((Join-Path $dir "bin\delta.cmd"))) `
        "delta resolves to $dir\bin\delta.cmd (got $($lines[0]))"
    Check ($r.Out -match [regex]::Escape($Version)) "delta --version prints $Version"
    $d = Child ($newWindow + "delta doctor; exit `$LASTEXITCODE")
    Check ($d.Code -eq 0) "delta doctor passes on a fresh install"
    Check ($d.Out -match "Setup checks:" -and $d.Out -notmatch "FAIL ") "delta doctor reports no FAIL"
    . ([scriptblock]::Create([IO.File]::ReadAllText($linkPs)))
    foreach ($lnk in @((Join-Path ([Environment]::GetFolderPath("Desktop")) "Delta.lnk"),
                       (Join-Path ([Environment]::GetFolderPath("Programs")) "Delta.lnk"),
                       (Join-Path ([Environment]::GetFolderPath("Programs")) "Delta\Update Delta.lnk"))) {
        Check (Test-Path -LiteralPath $lnk) "shortcut $lnk exists"
        $s = Get-DeltaShortcut $lnk
        Check ($s.Target.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase)) "$lnk points into $dir (got $($s.Target))"
        Check (Test-Path -LiteralPath $s.Target) "$lnk target exists"
    }
}

function Uninstall([string]$dir) {
    $r = Child ("powershell -NoProfile -ExecutionPolicy Bypass -File `"$dir\scripts\uninstall.ps1`" -InstallDir `"$dir`"; " +
                "exit `$LASTEXITCODE")
    Check ($r.Code -eq 0) "uninstall.ps1 exit code"
    Check (-not (Test-Path -LiteralPath $dir)) "$dir is removed"
    Check (-not (Test-Path (Join-Path ([Environment]::GetFolderPath("Desktop")) "Delta.lnk"))) "Desktop icon removed"
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    Check (-not ("$userPath" -split ";" | Where-Object { $_.TrimEnd("\") -ieq (Join-Path $dir "bin") })) "bin is off the user Path"
}

$setPolicy = { param($p) Child "Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy $p -Force" | Out-Null }
try {
    & $setPolicy Restricted
    Install "" "Restricted"
    Verify $default
    $venvPy = Join-Path $default ".venv\Scripts\python.exe"
    $built = (Get-Item $venvPy).LastWriteTimeUtc
    Install "" "Restricted"   # repair / update: run it again
    Check ((Get-Item $venvPy).LastWriteTimeUtc -eq $built) "the second run kept the environment"
    Verify $default
    Uninstall $default

    & $setPolicy AllSigned
    $odd = Join-Path $env:RUNNER_TEMP ("R" + [char]0x101 + "j D'Souza " + [char]0x0C24 + [char]0x0C46 +
                                       [char]0x0C32 + [char]0x0C41 + [char]0x0C17 + [char]0x0C41 + "\Programs\Delta")
    Install $odd "AllSigned"
    Verify $odd
    Uninstall $odd
} finally {
    & $setPolicy Undefined
    $log = Join-Path $env:TEMP "delta-install.log"
    if (Test-Path $log) { Copy-Item $log (Join-Path $env:RUNNER_TEMP "delta-install.log") -Force }
}
Write-Host "`nAll install checks passed ($Shell)." -ForegroundColor Green
