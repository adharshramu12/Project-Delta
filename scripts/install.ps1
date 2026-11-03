# Delta installer. Asks before installing anything.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

function Confirm-Step($msg) { (Read-Host "$msg [y/N]") -match '^(y|yes)$' }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    if (Confirm-Step "Install uv (Python package manager) with pip?") { python -m pip install --user uv }
}
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    if (Confirm-Step "Install Ollama (local models) with winget?") { winget install --id Ollama.Ollama -e }
}
# Release VRAM quickly when idle, and keep one model resident at most.
[Environment]::SetEnvironmentVariable("OLLAMA_KEEP_ALIVE", "2m", "User")
[Environment]::SetEnvironmentVariable("OLLAMA_MAX_LOADED_MODELS", "1", "User")

# Build .venv on the official, signed python.org Python 3.12. uv's own Python builds are unsigned, and
# with Smart App Control on Windows silently blocks them when Delta is opened from the Desktop.
$py312 = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (-not (Test-Path $py312) -and (Confirm-Step "Install the official Python 3.12 from python.org with winget (per-user)?")) {
    winget install --id Python.Python.3.12 --scope user -e --override "/quiet InstallAllUsers=0 PrependPath=0"
}
if ((Test-Path $py312) -and -not (Test-Path .venv\Scripts\python.exe)) { & $py312 -m venv .venv }
python -m uv sync --python 3.12 --extra all  # reuses the .venv made above

powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1

$model = (Select-String -Path config/delta.yaml -Pattern '^\s+chat:\s+(\S+)' | Select-Object -First 1).Matches.Groups[1].Value
if ((Get-Command ollama -ErrorAction SilentlyContinue) -and (Confirm-Step "Pull local model $model (~3 GB)?")) {
    ollama pull $model
}

Write-Host "`nAdd cloud fallback keys (optional):"
foreach ($k in "groq_api_key", "nvidia_api_key", "tavily_api_key") {
    if (Confirm-Step "Set $k now?") { python -m uv run delta secrets set $k }
}
python -m uv run delta doctor
Write-Host "`nStart Delta with:  uv run delta"
