# The `delta` terminal command: a small delta.cmd in <install folder>\bin that runs Delta from its own
# environment, and that bin folder on the Path of your Windows account (HKCU\Environment; the PC-wide Path is
# never touched, and no administrator rights are needed). The installer runs this; so does uninstall.ps1.
#
#   delta_command.ps1 -Action Install   [-InstallDir <folder>] [-NoPath]
#   delta_command.ps1 -Action Uninstall [-InstallDir <folder>]
#   . .\delta_command.ps1               (no -Action: only defines the functions below; the tests use this)
#
# Why: Python is installed with PrependPath=0 and Delta lives in its own .venv, so without this a normal
# PowerShell says "delta is not recognized". Safe to run again: nothing is added twice. Keep this file ASCII
# (Windows PowerShell 5.1 reads a file without a BOM in the ANSI code page).
param(
    [ValidateSet("", "Install", "Uninstall")]
    [string]$Action = "",
    [string]$InstallDir = "",   # empty = %LOCALAPPDATA%\Programs\Delta
    [switch]$NoPath,            # make bin\delta.cmd but leave the Path alone (trial installs)
    [string]$EnvKey = "Environment"   # the key under HKEY_CURRENT_USER that holds Path; the tests use their own
)

function Get-DeltaShimText {
    # %~dp0 is the folder of this .cmd, read by cmd.exe each time it runs. No user-folder name is written into
    # the file, so spaces, apostrophes, & and non-ASCII names (C:\Users\D'Souza, C:\Users\Raj Kumar with any
    # accent) need no escaping and no code page. %* passes every argument on as typed; the exit code is Delta's.
    $lines = @(
        "@echo off",
        "rem The delta command. Made by Delta's installer: runs Delta from its own environment.",
        "setlocal DisableDelayedExpansion",
        '"%~dp0..\.venv\Scripts\python.exe" -m delta %*',
        "exit /b %ERRORLEVEL%"
    )
    return ($lines -join "`r`n") + "`r`n"
}

function ConvertTo-PathKey([string]$entry) {
    # How two Path entries are compared: %VARIABLES% expanded, no quotes, no trailing backslash, any case.
    $e = [Environment]::ExpandEnvironmentVariables("$entry".Trim().Trim('"').Trim())
    return $e.TrimEnd([char[]]@([char]92, [char]47)).ToLowerInvariant()   # 92 = \, 47 = /
}

function Test-PathListContains([string]$pathValue, [string]$dir) {
    $key = ConvertTo-PathKey $dir
    foreach ($p in ("$pathValue" -split ";")) {
        if ($p.Trim() -and (ConvertTo-PathKey $p) -eq $key) { return $true }
    }
    return $false
}

function Add-PathListEntry([string]$pathValue, [string]$dir) {
    # The Path with $dir added at the end (after everything already there), or $null when it is already in it.
    # The other entries are kept exactly as they are, %VARIABLES% included. A Path that ends in ";" keeps that
    # ending after $dir, so removing $dir later gives back exactly the value there was before.
    if (Test-PathListContains $pathValue $dir) { return $null }
    if (-not "$pathValue".Trim()) { return $dir }
    if ("$pathValue".EndsWith(";")) { return "$pathValue$dir;" }
    return "$pathValue;$dir"
}

function Remove-PathListEntry([string]$pathValue, [string]$dir) {
    # The Path without $dir (every spelling of it), or $null when it is not in it. Other entries are kept as they are.
    if (-not (Test-PathListContains $pathValue $dir)) { return $null }
    $key = ConvertTo-PathKey $dir
    $kept = @("$pathValue" -split ";" | Where-Object { -not ($_.Trim() -and (ConvertTo-PathKey $_) -eq $key) })
    return ($kept -join ";")
}

function Get-UserPathValue([string]$envKey) {
    # The raw value, with %VARIABLES% not expanded, and its kind, so it is written back the same way
    # (REG_EXPAND_SZ stays REG_EXPAND_SZ). [Environment]::GetEnvironmentVariable would expand it.
    $result = @{ Value = ""; Kind = [Microsoft.Win32.RegistryValueKind]::ExpandString }
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($envKey)
    if ($null -eq $key) { return $result }
    try {
        if ($key.GetValueNames() -contains "Path") {
            $result.Value = [string]$key.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $kind = $key.GetValueKind("Path")
            if ($kind -eq [Microsoft.Win32.RegistryValueKind]::String) { $result.Kind = $kind }
        }
    } finally { $key.Close() }
    return $result
}

function Set-UserPathValue([string]$envKey, [string]$value, $kind) {
    $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($envKey)
    try {
        if ("$value".Trim()) { $key.SetValue("Path", $value, $kind) }
        else { $key.DeleteValue("Path", $false) }
    } finally { $key.Close() }
}

function Send-EnvironmentChange {
    # Tells Explorer that the Path changed (as the Settings app does), so windows opened from now on see it.
    try {
        if (-not ("DeltaSetup.EnvironmentBroadcast" -as [type])) {
            Add-Type -Namespace DeltaSetup -Name EnvironmentBroadcast -MemberDefinition @"
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint msg, UIntPtr wParam, string lParam,
    uint flags, uint timeout, out UIntPtr result);
"@
        }
        $out = [UIntPtr]::Zero
        # HWND_BROADCAST, WM_SETTINGCHANGE, "Environment", SMTO_ABORTIFHUNG, at most 5 seconds
        [void][DeltaSetup.EnvironmentBroadcast]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero,
            "Environment", 2, 5000, [ref]$out)
    } catch {
        # Add-Type not allowed (for example a locked-down PowerShell): .NET sends the same message when it
        # changes a user variable, so set and clear a throwaway one.
        try {
            [Environment]::SetEnvironmentVariable("DELTA_SETUP_REFRESH", "1", "User")
            [Environment]::SetEnvironmentVariable("DELTA_SETUP_REFRESH", $null, "User")
        } catch { }
    }
}

function Find-OtherDelta([string]$pathValue, [string]$binDir) {
    # Another program called delta (for example the git diff tool "delta") in a Path folder that comes before
    # ours: typing delta would run that one. Returns its full path, or $null.
    $mine = ConvertTo-PathKey $binDir
    foreach ($p in ("$pathValue" -split ";")) {
        if (-not $p.Trim()) { continue }
        if ((ConvertTo-PathKey $p) -eq $mine) { return $null }
        $dir = [Environment]::ExpandEnvironmentVariables($p.Trim().Trim('"'))
        foreach ($ext in ".com", ".exe", ".bat", ".cmd") {
            try {
                $f = Join-Path $dir "delta$ext"
                if (Test-Path -LiteralPath $f -PathType Leaf) { return $f }
            } catch { }
        }
    }
    return $null
}

function Install-DeltaCommand([string]$installDir, [bool]$noPath, [string]$envKey) {
    $bin = Join-Path $installDir "bin"
    $shim = Join-Path $bin "delta.cmd"
    [void][System.IO.Directory]::CreateDirectory($bin)
    $text = Get-DeltaShimText
    $old = if (Test-Path -LiteralPath $shim) { [System.IO.File]::ReadAllText($shim) } else { $null }
    # Written only when it changes: `delta update` started through this very file can still be finishing in
    # cmd.exe, which reads a .cmd file line by line while it runs.
    if ($old -cne $text) { [System.IO.File]::WriteAllText($shim, $text, (New-Object System.Text.ASCIIEncoding)) }

    $result = [pscustomobject]@{ Shim = $shim; Bin = $bin; OnPath = $false; PathAdded = $false; Other = $null }
    if ($noPath) { return $result }
    $real = ($envKey -eq "Environment")
    $current = Get-UserPathValue $envKey
    $new = Add-PathListEntry $current.Value $bin
    if ($null -ne $new) {
        Set-UserPathValue $envKey $new $current.Kind
        $result.PathAdded = $true
        if ($real) { Send-EnvironmentChange }
    }
    $result.OnPath = $true
    if ($real) {
        # This window too, so `delta` works here straight away when the installer ran in it (irm ... | iex).
        if (-not (Test-PathListContains $env:Path $bin)) { $env:Path = Add-PathListEntry $env:Path $bin }
        # A new window's Path is the PC-wide one followed by the account's own.
        $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $user = if ($null -ne $new) { $new } else { $current.Value }
        $result.Other = Find-OtherDelta "$machine;$user" $bin
    }
    return $result
}

function Uninstall-DeltaCommand([string]$installDir, [string]$envKey) {
    $bin = Join-Path $installDir "bin"
    $result = [pscustomobject]@{ Bin = $bin; PathRemoved = $false }
    $current = Get-UserPathValue $envKey
    $new = Remove-PathListEntry $current.Value $bin
    if ($null -ne $new) {
        Set-UserPathValue $envKey $new $current.Kind
        $result.PathRemoved = $true
        if ($envKey -eq "Environment") { Send-EnvironmentChange }
    }
    if ($envKey -eq "Environment") {
        $session = Remove-PathListEntry $env:Path $bin
        if ($null -ne $session) { $env:Path = $session }
    }
    if (Test-Path -LiteralPath $bin) { Remove-Item -LiteralPath $bin -Recurse -Force -ErrorAction SilentlyContinue }
    return $result
}

if ($Action) {
    $ErrorActionPreference = "Stop"
    if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA "Programs\Delta" }
    $InstallDir = $InstallDir.TrimEnd("\")
    if ($Action -eq "Install") { Install-DeltaCommand $InstallDir ([bool]$NoPath) $EnvKey }
    else { Uninstall-DeltaCommand $InstallDir $EnvKey }
}
