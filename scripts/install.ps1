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

python -m uv sync --python 3.12 --extra all

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
