# Delta installer: one command, no terminal knowledge needed.
#
#   From a shared copy:   double-click "Install Delta.cmd" in the Delta folder (or zip)
#   From GitHub:          irm https://raw.githubusercontent.com/adharshramu12/Project-Delta/main/scripts/install.ps1 | iex
#
# It installs the official, signed Python 3.12 if needed (Smart App Control blocks unsigned Python
# started from the Desktop), copies Delta to %LOCALAPPDATA%\Programs\Delta, builds its environment
# from the locked package versions, adds Desktop and Start-menu icons and opens Delta, whose setup
# wizard takes it from there. Run it again to repair or update. Your data (%LOCALAPPDATA%\Delta:
# chats, memory, settings) is never touched.
#
# Python: an x64 CPython 3.12 that is already installed is reused when it is signed and not the Microsoft
# Store one (found through the py launcher, the registry, or the usual folders). Windows on ARM (Snapdragon,
# Copilot+ PCs): Delta uses the x64 build of Python under Windows' built-in x64 emulation, because some of its
# packages (OpenCV) have no ARM64 build yet. Updating while Delta runs: it is closed first (you are asked).
param(
    [string]$Source = "",   # a Delta folder or .zip; empty = this script's own folder if it is one, else GitHub
    [string]$Repo = "adharshramu12/Project-Delta",
    [string]$Branch = "main",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Delta"),
    [switch]$NoLaunch,
    [switch]$NoShortcuts,    # for a trial install that must not repoint the Desktop icon
    [switch]$FromGitHub,     # always download the newest version (delta update)
    [switch]$StopRunning,    # close a running Delta without asking (delta update --stop)
    [string]$WaitForPid = "" # comma-separated process ids to wait for first (the `delta update` that started us)
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x slower with the progress bar
$log = Join-Path $env:TEMP "delta-install.log"
Start-Transcript -Path $log -Force | Out-Null

function Step($n, $text) { Write-Host "`n[$n/6] $text" -ForegroundColor Cyan }
function Done($text) { Write-Host "      OK  $text" -ForegroundColor Green }
function Fail($text) {
    Write-Host "`nDelta could not be installed: $text" -ForegroundColor Red
    Write-Host "Details: $log"
    Stop-Transcript | Out-Null
    if ($Host.Name -eq "ConsoleHost") { Read-Host "Press Enter to close" }
    exit 1
}

function Test-Arm64 {
    # Native PowerShell on ARM64 says ARM64; an emulated x64 one does not, so ask the processor too (12 = ARM64).
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") { return $true }
    try { return [bool](Get-CimInstance Win32_Processor -ErrorAction Stop | Where-Object { $_.Architecture -eq 12 }) }
    catch { return $false }
}

function Test-Python312x64($exe) {
    # A working, signed CPython 3.12 for x64. Not the Microsoft Store one: it redirects AppData, so Delta's
    # data would end up somewhere else.
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    if ($exe -like "*\WindowsApps\*") { return $false }
    try {
        $ok = & $exe -c "import sys, platform; print(sys.version_info[:2] == (3, 12) and platform.machine() == 'AMD64')" 2>$null
        if ($LASTEXITCODE -ne 0 -or "$ok".Trim() -ne "True") { return $false }
        return ((Get-AuthenticodeSignature $exe).Status -eq "Valid")
    } catch { return $false }
}

function Find-Python312 {
    $candidates = New-Object System.Collections.Generic.List[string]
    $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"))
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue   # knows every registered install
    if ($launcher) {
        try {
            $found = & $launcher.Source -3.12-64 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $found) { $candidates.Add("$found".Trim()) }
        } catch { }
    }
    foreach ($key in "HKCU:\Software\Python\PythonCore\3.12\InstallPath", "HKLM:\SOFTWARE\Python\PythonCore\3.12\InstallPath") {
        try {
            $reg = Get-ItemProperty -Path $key -ErrorAction Stop   # PEP 514: per-user and all-users installs
            if ($reg.ExecutablePath) { $candidates.Add([string]$reg.ExecutablePath) }
            elseif ($reg."(default)") { $candidates.Add((Join-Path ([string]$reg."(default)") "python.exe")) }
        } catch { }
    }
    if ($env:ProgramFiles) { $candidates.Add((Join-Path $env:ProgramFiles "Python312\python.exe")) }
    foreach ($c in $candidates) { if (Test-Python312x64 $c) { return $c } }
    return $null
}

function Get-RunningDelta {
    # Processes started from this install's environment (python.exe, pythonw.exe, delta.exe in .venv\Scripts).
    $venv = (Join-Path $InstallDir ".venv").TrimEnd("\") + "\"
    $skip = @($PID) + $waitIds
    @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [System.StringComparison]::OrdinalIgnoreCase) -and
        ($skip -notcontains [int]$_.ProcessId)
    })
}

try {
    Write-Host "Installing Delta, your personal AI agent for Windows" -ForegroundColor White
    $waitIds = @()
    foreach ($id in ($WaitForPid -split ",")) { if ($id.Trim() -match "^\d+$") { $waitIds += [int]$id.Trim() } }
    foreach ($id in $waitIds) { Wait-Process -Id $id -Timeout 60 -ErrorAction SilentlyContinue }
    $onArm = Test-Arm64

    # 1. Signed Python 3.12 -----------------------------------------------------------------------
    Step 1 "Python 3.12 (official, signed)"
    if ($onArm) {
        Write-Host "      This PC has an ARM processor. Delta uses the x64 build of Python 3.12, which Windows runs"
        Write-Host "      under its built-in emulation (some of Delta's packages have no ARM64 build yet)."
    }
    $py = Find-Python312
    if (-not $py) {
        $py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install --id Python.Python.3.12 --scope user -e --silent --architecture x64 --accept-package-agreements `
                --accept-source-agreements --override "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0" | Out-Host
        }
        if (-not (Test-Python312x64 $py)) {   # no winget (or it failed): python.org's own signed x64 installer
            $exe = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
            Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $exe
            if ((Get-AuthenticodeSignature $exe).Status -ne "Valid") { Fail "the Python installer's signature is not valid" }
            Start-Process $exe -ArgumentList "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0" -Wait
        }
    }
    if (-not (Test-Python312x64 $py)) {
        Fail "Python 3.12 (64-bit, x64) did not install. Install it from python.org (Windows installer, 64-bit) and run this again."
    }
    Done (& $py --version)

    # 2. Delta's files -------------------------------------------------------------------------------
    Step 2 "Delta's files -> $InstallDir"
    if (-not $Source -and -not $FromGitHub -and $PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "..\pyproject.toml"))) {
        $Source = Split-Path $PSScriptRoot -Parent
    }
    $staging = Join-Path $env:TEMP ("delta-src-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
    if (-not $Source) {
        $zip = "$staging.zip"
        Write-Host "      downloading $Repo ($Branch)"
        Invoke-WebRequest "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip
        $Source = $zip
    }
    if ($Source -like "*.zip") {
        Expand-Archive $Source -DestinationPath $staging -Force
        $inner = Get-ChildItem $staging -Directory | Where-Object { Test-Path (Join-Path $_.FullName "pyproject.toml") } | Select-Object -First 1
        $Source = if ($inner) { $inner.FullName } else { $staging }
    }
    if (-not (Test-Path (Join-Path $Source "pyproject.toml"))) { Fail "$Source is not a Delta folder" }
    $same = (Resolve-Path $Source).Path.TrimEnd("\") -ieq $InstallDir.TrimEnd("\")
    # Files in use cannot be replaced: a running Delta is closed first (asked, unless -StopRunning).
    $running = @(Get-RunningDelta)
    if ($running.Count -gt 0) {
        $ids = ($running | ForEach-Object { $_.ProcessId }) -join ", "
        $stop = [bool]$StopRunning
        if (-not $stop -and $Host.Name -eq "ConsoleHost") {
            $answer = Read-Host "      Delta is running (process $ids). Close it now so it can be updated? [Y/n]"
            $stop = ($answer -eq "" -or $answer -match "^[Yy]")
        }
        if (-not $stop) { Fail "Delta is running. Close it (the Shut down button in Delta's window) and run this again." }
        foreach ($p in $running) { try { & taskkill.exe /PID $p.ProcessId /T /F 2>$null | Out-Null } catch { } }
        Start-Sleep -Seconds 2
        Done "closed the running Delta"
    }
    if (-not $same) {
        New-Item -ItemType Directory -Force $InstallDir | Out-Null
        # mirror the code; keep the installed .venv so an update does not rebuild it from nothing
        robocopy $Source $InstallDir /MIR /XD .git .venv .venv-* .mypy_cache .pytest_cache .ruff_cache __pycache__ `
            /XF *.lnk .outside-* .realview-* /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { Fail "copying Delta's files failed (robocopy code $LASTEXITCODE)" }
    }
    Done "files in place"

    # 3. Environment ---------------------------------------------------------------------------------
    Step 3 "Delta's environment (exact package versions)"
    $venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
    if ((Test-Path $venvPy) -and (Get-AuthenticodeSignature $venvPy).Status -ne "Valid") {
        Remove-Item (Join-Path $InstallDir ".venv") -Recurse -Force   # an unsigned one would be blocked
    }
    if ((Test-Path $venvPy) -and -not (Test-Python312x64 $venvPy)) {
        Remove-Item (Join-Path $InstallDir ".venv") -Recurse -Force   # built from another Python (e.g. ARM64): rebuild
    }
    if (-not (Test-Path $venvPy)) { & $py -m venv (Join-Path $InstallDir ".venv") }
    & $venvPy -m pip install --quiet --disable-pip-version-check --upgrade uv
    Push-Location $InstallDir
    try {
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $InstallDir ".venv"
        & $venvPy -m uv sync --frozen --extra all --no-dev --python $venvPy
        if ($LASTEXITCODE -ne 0) { Fail "installing Delta's packages failed (see the log)" }
    } finally { Pop-Location }
    Done "packages installed"

    # 4. Check ---------------------------------------------------------------------------------------
    Step 4 "Checking"
    $ver = & $venvPy -c "import delta; print(delta.__version__)"
    if ($LASTEXITCODE -ne 0) { Fail "Delta does not start (see the log)" }
    Done "Delta $ver"

    # 5. Icons ---------------------------------------------------------------------------------------
    Step 5 "Desktop and Start-menu icons"
    if ($NoShortcuts) { Done "skipped (-NoShortcuts)" } else {
        & (Join-Path $InstallDir "scripts\create_shortcut.ps1") | Out-Host
        Done "Delta icon added"
    }

    # 6. Open ----------------------------------------------------------------------------------------
    Step 6 "Opening Delta"
    if (-not $NoLaunch) {
        Start-Process (Join-Path $InstallDir ".venv\Scripts\pythonw.exe") -ArgumentList "-m delta app" -WorkingDirectory $InstallDir
        Done "the setup wizard opens in a few seconds"
    } else { Done "skipped (-NoLaunch)" }

    Write-Host "`nDelta is installed. Open it any time from the Delta icon on your Desktop." -ForegroundColor Green
    Write-Host "Update later: run this again, or 'delta update'. Remove: scripts\uninstall.ps1"
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue }
    Stop-Transcript | Out-Null
} catch {
    Fail $_.Exception.Message
}
