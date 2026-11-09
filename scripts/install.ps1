# Delta installer: one command, no terminal knowledge needed.
#
#   From a shared copy:   double-click "Install Delta.cmd" in the Delta folder (or zip)
#   From GitHub:          irm https://raw.githubusercontent.com/adharshramu12/Project-Delta/main/scripts/install.ps1 | iex
#
# It installs the official, signed Python 3.12 if needed (Smart App Control blocks unsigned Python
# started from the Desktop), copies Delta to %LOCALAPPDATA%\Programs\Delta, builds its environment
# from the locked package versions, adds the `delta` terminal command (bin\delta.cmd, on your account's Path),
# Desktop and Start-menu icons (and "Update Delta" in the Start menu) and opens Delta, whose setup wizard takes it
# from there. Run it again to repair or update. Your data (%LOCALAPPDATA%\Delta: chats, memory, settings) is
# never touched.
#
# Python: an x64 CPython 3.12 that is already installed is reused when it is signed and not the Microsoft
# Store one (found through the py launcher, the registry, or the usual folders). Windows on ARM (Snapdragon,
# Copilot+ PCs): Delta uses the x64 build of Python under Windows' built-in x64 emulation, because some of its
# packages (OpenCV) have no ARM64 build yet. Updating while Delta runs: it is closed first (you are asked).
#
# Execution policy: a new Windows PC runs no .ps1 files (policy Restricted), and `irm ... | iex` runs this
# installer as text, which the policy does not cover. So this installer never starts another script as a file:
# Delta's other scripts are read as text and run here (Invoke-DeltaScript), and nothing changes the PC's or the
# account's execution policy. The installed files are unblocked (Unblock-File removes the "downloaded from the
# internet" mark), so Delta's own scripts are not treated as downloaded files later.
#
# `irm ... | iex` cannot pass parameters, so three of them can also come from the environment (a parameter given
# on the command line wins): DELTA_REF (a branch, tag or commit to download instead of main), DELTA_REPO (a fork,
# owner/name), DELTA_INSTALL_DIR, DELTA_NO_LAUNCH=1. Example: $env:DELTA_REF = "v0.3.0"; irm .../scripts/install.ps1 | iex   (docs/INSTALL.md)
#
# Scope: `irm ... | iex` runs this text in the user's own PowerShell window. Everything below runs inside one script
# block (& { ... }), so its settings ($ErrorActionPreference = "Stop"), variables and functions end with it and the
# window is left as it was. A failure prints what went wrong and returns: it never closes that window (`exit` there
# would). Run as a file (Install Delta.cmd, delta update) it still ends with exit code 1 on a failure.
$__DeltaInstaller = {
param(
    [string]$Source = "",   # a Delta folder or .zip; empty = this script's own folder if it is one, else GitHub
    [string]$Repo = "adharshramu12/Project-Delta",
    [string]$Branch = "main",
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Delta"),
    [switch]$NoLaunch,
    [switch]$NoShortcuts,    # for a trial install that must not repoint the Desktop icon (nor the delta command)
    [switch]$NoPath,         # make bin\delta.cmd but do not add it to your Path
    [switch]$FromGitHub,     # always download the newest version (delta update)
    [switch]$StopRunning,    # close a running Delta without asking (delta update --stop)
    [string]$WaitForPid = "", # comma-separated process ids to wait for first (the `delta update` that started us)
    [string]$ExpectSha256 = "", # pyproject.toml=<sha256>;uv.lock=<sha256>: `delta update` read them from this commit
    [string]$ExpectVersion = "", # the version that commit's pyproject.toml must have (with -ExpectSha256)
    [switch]$DefineOnly      # only define the functions below and stop (the tests use this)
)
$ErrorActionPreference = "Stop"   # this script block's own scope: the user's window keeps its own setting
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest is 10x slower with the progress bar
if (-not $PSBoundParameters.ContainsKey("Branch") -and "$env:DELTA_REF".Trim()) { $Branch = "$env:DELTA_REF".Trim() }
if (-not $PSBoundParameters.ContainsKey("Repo") -and "$env:DELTA_REPO".Trim()) { $Repo = "$env:DELTA_REPO".Trim() }
if (-not $PSBoundParameters.ContainsKey("InstallDir") -and "$env:DELTA_INSTALL_DIR".Trim()) {
    $InstallDir = "$env:DELTA_INSTALL_DIR".Trim()
}
if ("$env:DELTA_NO_LAUNCH".Trim() -match "^(1|true|yes)$") { $NoLaunch = [switch]$true }
# Windows PowerShell 5.1 can default to TLS 1.0, which GitHub and python.org refuse ("Could not create SSL/TLS
# secure channel"). Allow TLS 1.2 as well (3072); nothing older is added.
$tlsSaved = $null
try {
    $tlsSaved = [Net.ServicePointManager]::SecurityProtocol   # put back at the end: the user's window keeps its own
    [Net.ServicePointManager]::SecurityProtocol = $tlsSaved -bor 3072
} catch { }
$log = Join-Path $env:TEMP "delta-install.log"
if (-not $DefineOnly) { Start-Transcript -Path $log -Force | Out-Null }

function Test-Interactive {
    # A person at a console window, who can answer a question. Not a CI runner, a scheduled task or input piped in
    # (Read-Host would wait forever there, or fail).
    if ($env:CI -or $env:GITHUB_ACTIONS -or $env:TF_BUILD) { return $false }
    if ($Host.Name -ne "ConsoleHost" -or -not [Environment]::UserInteractive) { return $false }
    try { return -not [Console]::IsInputRedirected } catch { return $true }
}

function Get-SourceZipUrl([string]$repo, [string]$ref) {
    # GitHub's zip of one branch, tag or commit. A full commit id and a refs/... name are used as they are; any other
    # name is a branch (refs/heads/<name>, which also works for names with a slash such as fix/x).
    $ref = $ref.Trim()
    if ($ref -match "^[0-9a-fA-F]{40}$" -or $ref.StartsWith("refs/")) { return "https://github.com/$repo/archive/$ref.zip" }
    return "https://github.com/$repo/archive/refs/heads/$ref.zip"
}

function Get-Sha256Hex([string]$path) {
    # .NET itself: Get-FileHash is a script-module function in Windows PowerShell 5.1, and the Restricted policy of a
    # new PC refuses to load script modules.
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $stream = [System.IO.File]::OpenRead($path)
        try { $bytes = $sha.ComputeHash($stream) } finally { $stream.Dispose() }
    } finally { $sha.Dispose() }
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Test-DeltaFiles([string]$folder, [string]$expect, [string]$version) {
    # `delta update` read pyproject.toml and uv.lock from the exact commit it pinned, over HTTPS from the official
    # repository; the downloaded copy must be byte for byte the same (uv.lock then pins every package by its hash).
    # Returns "" when everything matches, else what differs.
    $pairs = @($expect -split ";" | Where-Object { $_ })
    if ($pairs.Count -ne 2) { return "the expected file hashes are not valid" }
    $problems = @()
    foreach ($pair in $pairs) {
        $name, $hash = $pair -split "=", 2
        if (($name -notin @("pyproject.toml", "uv.lock")) -or ("$hash" -notmatch "^[0-9a-fA-F]{64}$")) {
            return "the expected file hashes are not valid"
        }
        $file = Join-Path $folder $name
        if (-not (Test-Path -LiteralPath $file)) { $problems += "$name is missing"; continue }
        if ((Get-Sha256Hex $file) -ne $hash.ToLower()) { $problems += "$name is not the file Delta checked" }
    }
    if ($version) {
        $m = [regex]::Match([IO.File]::ReadAllText((Join-Path $folder "pyproject.toml")), '(?m)^version\s*=\s*"([^"]+)"')
        if (-not $m.Success -or $m.Groups[1].Value -ne $version) { $problems += "its version is not $version" }
    }
    return ($problems -join "; ")
}

function Test-SignatureValid([string]$path) {
    # Get-AuthenticodeSignature lives in PowerShell's Security module. Under AllSigned, PowerShell 7 refuses to load
    # that module when Microsoft's certificate is not in the account's Trusted Publishers ("AuthorizationManager
    # check failed"), found by the install CI job on pwsh. Windows PowerShell 5.1 (on every Windows) loads its own
    # catalog-signed copy under AllSigned, so the same check is asked of it then. The path goes through the
    # environment, so a folder name with quotes or non-English letters needs no escaping.
    try {
        return ((Get-AuthenticodeSignature -LiteralPath $path -ErrorAction Stop).Status -eq "Valid")
    } catch {
        $ps51 = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
        if ($PSVersionTable.PSEdition -ne "Core" -or -not (Test-Path -LiteralPath $ps51)) { throw }
        $env:DELTA_SIGNATURE_CHECK = $path
        try {
            $status = & $ps51 -NoProfile -NonInteractive -Command "(Get-AuthenticodeSignature -LiteralPath `$env:DELTA_SIGNATURE_CHECK).Status.ToString()" 2>$null
        } finally { Remove-Item Env:DELTA_SIGNATURE_CHECK -ErrorAction SilentlyContinue }
        return ("$status".Trim() -eq "Valid")
    }
}

function Step($n, $text) { Write-Host "`n[$n/6] $text" -ForegroundColor Cyan }
function Done($text) { Write-Host "      OK  $text" -ForegroundColor Green }
$failMark = "DELTA-INSTALL-FAILED: "
function Fail($text) {
    # Says what went wrong and stops the install by throwing to the end of this script block. Never `exit`: under
    # `irm ... | iex` that closed the user's PowerShell window, error and all, before they could read it.
    Write-Host "`nDelta could not be installed: $text" -ForegroundColor Red
    Write-Host "Details: $log"
    throw ($failMark + $text)
}

function Test-FreeSpace($path, [double]$needGB) {
    # Delta's packages and uv's download cache (both in the install folder) take about 1.5 GB on a first install, so
    # this measures the drive the install folder is on (DELTA_INSTALL_DIR can put it on another drive than
    # %LOCALAPPDATA%: that drive was measured before, and a full D: passed the check).
    try {
        $drive = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($path))
        $free = (New-Object System.IO.DriveInfo($drive)).AvailableFreeSpace / 1GB
    } catch { return }   # a drive Windows cannot measure (network share): carry on
    if ($free -lt $needGB) {
        Fail (("there is only {0:N1} GB free on {1}, and Delta needs about {2} GB. Free some space (Settings > " +
               "System > Storage, or empty the Recycle Bin) and run this again.") -f $free, $drive, $needGB)
    }
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
        # sysconfig's platform is what the interpreter was BUILT for. platform.machine() reads the environment, and an
        # x64 Python running under emulation on Windows on ARM can report ARM64 there, so the right Python was
        # refused on ARM PCs ("Python 3.12 (64-bit, x64) did not install"), found by the windows-11-arm CI job.
        $ok = & $exe -c "import sys, sysconfig; print(sys.version_info[:2] == (3, 12) and sysconfig.get_platform() == 'win-amd64')" 2>$null
        if ($LASTEXITCODE -ne 0 -or "$ok".Trim() -ne "True") { return $false }
        return (Test-SignatureValid $exe)
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

function Invoke-DeltaScript([string]$Path, [hashtable]$Arguments = @{}) {
    # Runs one of Delta's own scripts from its text, here in this PowerShell, never as a script file. The
    # execution policy (Restricted on a new PC) refuses `& file.ps1` when this installer runs as text
    # (irm ... | iex): "running scripts is disabled on this system". Text is not checked against that policy, nor
    # by Smart App Control as a downloaded script file. The script's own $PSScriptRoot is empty this way, so the
    # scripts run here take their folder as a parameter. No execution policy is changed.
    $block = [scriptblock]::Create([System.IO.File]::ReadAllText($Path))
    & $block @Arguments
}

function Unblock-DeltaFiles([string]$dir) {
    # Files from a downloaded zip that Explorer extracted carry a "downloaded from the internet" mark
    # (Zone.Identifier), and robocopy copies it. Removing it from Delta's own install folder means `delta update`,
    # the Start-menu items and the fix commands below are not treated as downloaded scripts. Not the environment
    # (.venv): it is built here and has no mark.
    $venv = (Join-Path $dir ".venv").TrimEnd("\") + "\"
    $cache = (Join-Path $dir ".uv-cache").TrimEnd("\") + "\"
    Get-ChildItem -LiteralPath $dir -Recurse -File -Force -ErrorAction SilentlyContinue |
        Where-Object { -not $_.FullName.StartsWith($venv, [System.StringComparison]::OrdinalIgnoreCase) -and
                       -not $_.FullName.StartsWith($cache, [System.StringComparison]::OrdinalIgnoreCase) } |
        Unblock-File -ErrorAction SilentlyContinue
}

function Install-DeltaShortcuts([string]$dir, [bool]$noPath, [bool]$noShortcuts) {
    # The delta command and the icons. Optional: Delta is installed and checked before this, so a part that fails
    # here is a warning with the exact command that adds it, never "Delta could not be installed".
    $scripts = Join-Path $dir "scripts"
    $result = [pscustomobject]@{ Command = $null; Fixes = (New-Object System.Collections.Generic.List[string]) }
    try {
        # Your account's Path only (HKCU), never the PC-wide one. A trial install (-NoShortcuts) leaves it alone.
        $result.Command = Invoke-DeltaScript (Join-Path $scripts "delta_command.ps1") @{
            Action = "Install"; InstallDir = $dir; NoPath = ($noPath -or $noShortcuts) } | Select-Object -Last 1
    } catch {
        Write-Host "      The delta command could not be set up: $($_.Exception.Message)" -ForegroundColor Yellow
        $result.Fixes.Add(('powershell -NoProfile -ExecutionPolicy Bypass -File "{0}" -Action Install -InstallDir "{1}"' -f
                           (Join-Path $scripts "delta_command.ps1"), $dir))
    }
    $command = $result.Command
    if ($command -and $command.OnPath) {
        Done "delta command added ($($command.Bin) is on your Path)"
        if ($command.Other) {
            Write-Host ("      Another program called delta comes first on your Path ($($command.Other)), so 'delta' " +
                        "runs that one. Use the full path instead: ""$($command.Shim)"" update") -ForegroundColor Yellow
        }
    } elseif ($command) { Done "delta command made in $($command.Bin) (not added to your Path)" }
    if ($noShortcuts) { Done "icons skipped (-NoShortcuts)" } else {
        try {
            Invoke-DeltaScript (Join-Path $scripts "create_shortcut.ps1") @{ NoCommand = $true; Root = $dir } | Out-Host
            Done "Delta icon added"
        } catch {
            Write-Host "      The Desktop and Start-menu icons could not be made: $($_.Exception.Message)" -ForegroundColor Yellow
            $result.Fixes.Add(('powershell -NoProfile -ExecutionPolicy Bypass -File "{0}"' -f
                               (Join-Path $scripts "create_shortcut.ps1")))
        }
    }
    return $result
}

function Get-DeltaRuntime([string]$dir) {
    # Where this Delta keeps its data, the address its dashboard listens on and its save folder, read the way Delta
    # itself reads its settings (a port changed in settings.yaml included). Defaults when its Python cannot say.
    $data = if ("$env:DELTA_DATA_DIR".Trim()) { "$env:DELTA_DATA_DIR".Trim() } else { Join-Path $env:LOCALAPPDATA "Delta" }
    $rt = [pscustomobject]@{ Host = "127.0.0.1"; Port = 7878; Data = $data; Save = ""; FromDelta = $false }
    $py = Join-Path $dir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $py)) { return $rt }
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"   # a warning on stderr is not a failure here
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

function Request-DeltaShutdown($rt) {
    # The dashboard's Shut down button: Delta stops its channels, its helpers and its own window. True when it said yes.
    $tokenFile = Join-Path $rt.Data "api_token"
    if (-not (Test-Path -LiteralPath $tokenFile)) { return $false }
    try {
        Invoke-RestMethod -Method Post -Uri ("http://{0}:{1}/api/shutdown" -f $rt.Host, $rt.Port) -TimeoutSec 5 `
            -UseBasicParsing -Headers @{ Authorization = "Bearer " + (Get-Content -LiteralPath $tokenFile -Raw).Trim() } | Out-Null
        return $true
    } catch { return $false }
}

function Get-DeltaWindowProcesses([string]$dataDir) {
    # Edge processes of Delta's own dashboard window: started with Delta's private profile (<data>\app-window), which
    # only Delta uses. Never the user's own Edge or Chrome.
    $profileDir = [regex]::Escape((Join-Path $dataDir "app-window").ToLowerInvariant())
    @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine.ToLowerInvariant() -match ("--user-data-dir=""?" + $profileDir + "(""|\s|$)")
    })
}

function Close-DeltaWindows([string]$dataDir) {
    $procs = @(Get-DeltaWindowProcesses $dataDir)
    foreach ($p in $procs) { try { & taskkill.exe /PID $p.ProcessId /T /F 2>$null | Out-Null } catch { } }
    return $procs.Count
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

if ($DefineOnly) { return }

$failed = $false
try {
    Write-Host "Installing Delta, your personal AI agent for Windows" -ForegroundColor White
    $waitIds = @()
    foreach ($id in ($WaitForPid -split ",")) { if ($id.Trim() -match "^\d+$") { $waitIds += [int]$id.Trim() } }
    foreach ($id in $waitIds) { Wait-Process -Id $id -Timeout 60 -ErrorAction SilentlyContinue }
    $onArm = Test-Arm64
    $fresh = -not (Test-Path (Join-Path $InstallDir ".venv\Scripts\python.exe"))
    Test-FreeSpace $InstallDir $(if ($fresh) { 2 } else { 0.5 })
    # Python 3.12 (when it has to be installed) and Delta's data go to %LOCALAPPDATA%: a little room there too.
    if ([System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($InstallDir)) -ne
        [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($env:LOCALAPPDATA))) {
        Test-FreeSpace $env:LOCALAPPDATA 0.3
    }

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
            Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $exe -UseBasicParsing
            if (-not (Test-SignatureValid $exe)) { Fail "the Python installer's signature is not valid" }
            $setup = Start-Process $exe -ArgumentList "/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0" -Wait -PassThru
            if ($setup.ExitCode -ne 0) { Write-Host "      python.org's installer ended with code $($setup.ExitCode)" -ForegroundColor Yellow }
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
        # -UseBasicParsing: Windows PowerShell 5.1 otherwise needs Internet Explorer's engine, which a PC without
        # it (Windows Server, IE removed) does not have ("The response content cannot be parsed").
        Invoke-WebRequest (Get-SourceZipUrl $Repo $Branch) -OutFile $zip -UseBasicParsing
        $Source = $zip
    }
    if ($Source -like "*.zip") {
        # .NET's own zip reader, not Expand-Archive: that cmdlet lives in a script module (.psm1), which PowerShell 7
        # refuses to load under the Restricted policy of a new PC ("running scripts is disabled"), found by CI.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        [System.IO.Compression.ZipFile]::ExtractToDirectory($Source, $staging)
        $inner = Get-ChildItem $staging -Directory | Where-Object { Test-Path (Join-Path $_.FullName "pyproject.toml") } | Select-Object -First 1
        $Source = if ($inner) { $inner.FullName } else { $staging }
    }
    if (-not (Test-Path (Join-Path $Source "pyproject.toml"))) { Fail "$Source is not a Delta folder" }
    if ($ExpectSha256) {
        # Update now / delta update: only the exact commit that was checked, and only if its files are the ones read
        if ($Branch -notmatch "^[0-9a-fA-F]{40}$") { Fail "a checked update must name one commit, not '$Branch'" }
        $bad = Test-DeltaFiles $Source $ExpectSha256 $ExpectVersion
        if ($bad) { Fail "the downloaded files are not the version Delta checked ($bad). Nothing was changed." }
        Done ("verified pyproject.toml and uv.lock of $Repo at commit " + $Branch.Substring(0, 12))
    }
    $same = (Resolve-Path $Source).Path.TrimEnd("\") -ieq $InstallDir.TrimEnd("\")
    # Files in use cannot be replaced: a running Delta is closed first (asked, unless -StopRunning).
    $running = @(Get-RunningDelta)
    if ($running.Count -gt 0) {
        $ids = ($running | ForEach-Object { $_.ProcessId }) -join ", "
        $stop = [bool]$StopRunning
        if (-not $stop -and (Test-Interactive)) {
            $answer = Read-Host "      Delta is running (process $ids). Close it now so it can be updated? [Y/n]"
            $stop = ($answer -eq "" -or $answer -match "^[Yy]")
        }
        if (-not $stop) { Fail "Delta is running. Close it (the Shut down button in Delta's window) and run this again." }
        # As its Shut down button does first (Delta closes its own window too), then whatever is left. Its dashboard
        # window's Edge processes stayed behind when only the service was killed (fresh-user CI journey).
        $rt = Get-DeltaRuntime $InstallDir
        if (Request-DeltaShutdown $rt) {
            $deadline = (Get-Date).AddSeconds(20)
            while ((Get-Date) -lt $deadline -and @(Get-RunningDelta).Count -gt 0) { Start-Sleep -Milliseconds 500 }
        }
        foreach ($p in @(Get-RunningDelta)) { try { & taskkill.exe /PID $p.ProcessId /T /F 2>$null | Out-Null } catch { } }
        Start-Sleep -Seconds 2
        $windows = Close-DeltaWindows $rt.Data
        Done ("closed the running Delta" + $(if ($windows) { " and its window" } else { "" }))
    }
    if (-not $same) {
        New-Item -ItemType Directory -Force $InstallDir | Out-Null
        # mirror the code; keep the installed .venv so an update does not rebuild it from nothing
        # and bin (the delta command): `delta update` may still be finishing in it while this runs.
        robocopy $Source $InstallDir /MIR /XD .git .venv .venv-* .uv-cache bin .mypy_cache .pytest_cache .ruff_cache __pycache__ `
            /XF *.lnk .outside-* .realview-* /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { Fail "copying Delta's files failed (robocopy code $LASTEXITCODE)" }
    }
    Unblock-DeltaFiles $InstallDir
    Done "files in place"

    # 3. Environment ---------------------------------------------------------------------------------
    Step 3 "Delta's environment (exact package versions)"
    $venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
    if ((Test-Path $venvPy) -and -not (Test-SignatureValid $venvPy)) {
        Remove-Item (Join-Path $InstallDir ".venv") -Recurse -Force   # an unsigned one would be blocked
    }
    if ((Test-Path $venvPy) -and -not (Test-Python312x64 $venvPy)) {
        Remove-Item (Join-Path $InstallDir ".venv") -Recurse -Force   # built from another Python (e.g. ARM64): rebuild
    }
    if (-not (Test-Path $venvPy)) {
        & $py -m venv (Join-Path $InstallDir ".venv")
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPy)) { Fail "Python could not create Delta's environment in $InstallDir (see the log)" }
    }
    # No pip cache and uv's download cache inside Delta's own folder (.uv-cache): an update reuses it, and the
    # uninstaller removes it with the folder. In uv's shared cache (%LOCALAPPDATA%\uv\cache) about 500 MB stayed on
    # the PC after uninstall (found by the fresh-user CI journey). A cache folder the user chose (UV_CACHE_DIR) wins.
    & $venvPy -m pip install --quiet --disable-pip-version-check --no-cache-dir --upgrade uv
    if ($LASTEXITCODE -ne 0) {
        Fail ("uv (the package installer Delta uses) could not be downloaded. Check the internet connection. Behind a " +
              "company proxy, set HTTPS_PROXY (for example: setx HTTPS_PROXY http://proxy.example:8080), open a new " +
              "window and run this again.")
    }
    Push-Location $InstallDir
    # `irm ... | iex` runs in the user's own PowerShell window: uv's settings for this one install are put back
    # afterwards, so a later `uv sync` of the user's own project in that window does not go into Delta's folder.
    $uvSaved = @{}
    $ownCache = -not "$env:UV_CACHE_DIR".Trim()
    foreach ($k in "UV_PROJECT_ENVIRONMENT", "UV_CACHE_DIR", "UV_NATIVE_TLS") { $uvSaved[$k] = [Environment]::GetEnvironmentVariable($k) }
    try {
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $InstallDir ".venv"
        if (-not "$env:UV_CACHE_DIR".Trim()) { $env:UV_CACHE_DIR = Join-Path $InstallDir ".uv-cache" }
        # Windows' own certificate store, as pip already uses: behind a company proxy that inspects HTTPS its
        # certificate is in that store (put there by the company), not in uv's built-in list.
        if (-not "$env:UV_NATIVE_TLS".Trim()) { $env:UV_NATIVE_TLS = "1" }
        & $venvPy -m uv sync --frozen --extra all --no-dev --python $venvPy
        if ($LASTEXITCODE -ne 0) {
            Fail ("installing Delta's packages failed (see the log). Usual causes: no internet, a company proxy " +
                  "(set HTTPS_PROXY and run this again), or antivirus blocking files in $InstallDir.")
        }
    } finally {
        Pop-Location
        foreach ($k in $uvSaved.Keys) { [Environment]::SetEnvironmentVariable($k, $uvSaved[$k]) }
    }
    # The download cache is only needed while installing: about 130 MB freed. An update installs only the packages
    # that changed into the existing .venv, so it downloads little without it. A cache the user chose stays.
    if ($ownCache) { Remove-Item -LiteralPath (Join-Path $InstallDir ".uv-cache") -Recurse -Force -ErrorAction SilentlyContinue }
    Done "packages installed"

    # 4. Check ---------------------------------------------------------------------------------------
    Step 4 "Checking"
    $ver = & $venvPy -c "import delta; print(delta.__version__)"
    if ($LASTEXITCODE -ne 0) { Fail "Delta does not start (see the log)" }
    Done "Delta $ver"

    # 5. Command and icons ----------------------------------------------------------------------------
    Step 5 "The delta command, Desktop and Start-menu icons"
    $setup = Install-DeltaShortcuts $InstallDir ([bool]$NoPath) ([bool]$NoShortcuts)
    $command = $setup.Command

    # 6. Open ----------------------------------------------------------------------------------------
    Step 6 "Opening Delta"
    if (-not $NoLaunch) {
        Start-Process (Join-Path $InstallDir ".venv\Scripts\pythonw.exe") -ArgumentList "-m delta app" -WorkingDirectory $InstallDir
        Done "the setup wizard opens in a few seconds"
    } else { Done "skipped (-NoLaunch)" }

    Write-Host "`nDelta is installed. Open it any time from the Delta icon on your Desktop." -ForegroundColor Green
    if ($command -and $command.OnPath) { Write-Host "Open a new PowerShell window to use the delta command." }
    $later = if ($NoShortcuts) { "" } else { "'Update Delta' in the Start menu, " }
    Write-Host "Update later: $later'delta update' in a new PowerShell window, or run this again."
    Write-Host ('Remove: powershell -NoProfile -ExecutionPolicy Bypass -File "{0}"' -f (Join-Path $InstallDir "scripts\uninstall.ps1"))
    if ($setup.Fixes.Count -gt 0) {
        Write-Host "`nSome optional parts were not set up (Delta itself works). To add them, paste this in PowerShell:" -ForegroundColor Yellow
        foreach ($fix in $setup.Fixes) { Write-Host "  $fix" -ForegroundColor Yellow }
    }
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue }
} catch {
    $failed = $true
    $why = "$($_.Exception.Message)"
    if (-not $why.StartsWith($failMark)) {   # an unexpected error (Fail has already said what went wrong)
        Write-Host "`nDelta could not be installed: $why" -ForegroundColor Red
        Write-Host "Details: $log"
    }
} finally {
    try { Stop-Transcript | Out-Null } catch { }
    if ($null -ne $tlsSaved) { try { [Net.ServicePointManager]::SecurityProtocol = $tlsSaved } catch { } }
}
if ($failed) {
    if ($PSCommandPath) {
        # Run as a file (Install Delta.cmd, delta update): its own window, so let the person read it, and exit 1 for
        # whatever started it. Never under `irm ... | iex`: there it would close the user's window.
        if (Test-Interactive) { Read-Host "Press Enter to close" }
        exit 1
    }
    Write-Host "Your PowerShell window stays open. Fix the cause above and paste the install command again." -ForegroundColor Yellow
    $global:LASTEXITCODE = 1
}
}

# Run the installer in its own scope (see "Scope" at the top). -DefineOnly (tests) dot-sources it instead, so its
# functions stay defined for the caller.
try {
    if (@($args) -contains "-DefineOnly") { . $__DeltaInstaller @args } else { & $__DeltaInstaller @args }
} finally {
    if (-not (@($args) -contains "-DefineOnly")) { Remove-Variable -Name __DeltaInstaller -ErrorAction SilentlyContinue }
}
