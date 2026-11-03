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
param(
    [string]$Source = "",   # a Delta folder or .zip; empty = this script's own folder if it is one, else GitHub
    [string]$Repo = "adharshramu12/Project-Delta",
    [string]$Branch = "main",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Delta"),
    [switch]$NoLaunch,
    [switch]$NoShortcuts,    # for a trial install that must not repoint the Desktop icon
    [switch]$FromGitHub      # always download the newest version (delta update)
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

try {
    Write-Host "Installing Delta, your personal AI agent for Windows" -ForegroundColor White

    # 1. Signed Python 3.12 -----------------------------------------------------------------------
    Step 1 "Python 3.12 (official, signed)"
    $py = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (-not (Test-Path $py)) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            winget install --id Python.Python.3.12 --scope user -e --silent --accept-package-agreements `
                --accept-source-agreements --override "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0" | Out-Host
        }
        if (-not (Test-Path $py)) {   # no winget: python.org's own signed installer
            $exe = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
            Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $exe
            if ((Get-AuthenticodeSignature $exe).Status -ne "Valid") { Fail "the Python installer's signature is not valid" }
            Start-Process $exe -ArgumentList "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0" -Wait
        }
    }
    if (-not (Test-Path $py)) { Fail "Python 3.12 did not install. Install it from python.org and run this again." }
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
