"""Typed configuration loaded from `config/*.yaml`.

Lookup order for the config directory: $DELTA_CONFIG_DIR, then ./config, then the repo's config/.
Runtime data (memory, audit log, logs) lives in $DELTA_DATA_DIR or %LOCALAPPDATA%/Delta.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]


class ProviderConfig(BaseModel):
    # "openai": any OpenAI-compatible server (Ollama, LM Studio, Groq, NIM, ...).
    # "claude_code" / "codex" / "cline": the user's own signed-in AI app, so their plan or credits pay.
    kind: Literal["openai", "claude_code", "codex", "cline"] = "openai"
    base_url: str = ""
    command: str | None = None  # CLI kinds: path to the executable (default: found automatically)
    discover: Literal["lmstudio"] | None = None  # read the server's port from the app's own settings
    secret: str | None = None  # name of the secret holding the API key
    local: bool = False
    native_tools: bool = True
    keep_alive: str | None = None
    # Local servers that load models on demand (LM Studio): skip them when the model is not loaded
    # yet and less RAM than this is free, instead of freezing the PC while it loads.
    min_free_ram_gb: float = 0
    timeout_s: float = 60
    models: dict[str, str] = Field(default_factory=dict)
    # CLI kinds: thinking effort per job (plan/chat/verify; "chat" is the default for the others)
    efforts: dict[str, str] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    routes: dict[str, list[str]] = Field(default_factory=dict)
    preferred: str = "auto"  # provider tried first for every job; "auto" = follow `routes`


class AgentConfig(BaseModel):
    name: str = "Delta"
    owner_name: str = ""
    max_steps: int = 15  # scaled by the brain: frontier models get more, small local ones fewer
    auto_observe: bool = True  # after each mouse/keyboard/browser action, show the model the result
    max_attempts: int = 3
    approval_timeout_s: float = 300


class SearchConfig(BaseModel):
    order: list[str] = Field(default_factory=lambda: ["tavily", "serpapi", "duckduckgo"])
    max_results: int = 5


class Limit(BaseModel):
    throttle: float
    critical: float


class GovernorConfig(BaseModel):
    sample_interval_s: float = 5
    below_normal_priority: bool = True
    limits: dict[str, Limit] = Field(default_factory=dict)


class MemoryConfig(BaseModel):
    enabled: bool = True
    auto_extract: bool = True
    learn_skills: bool = True  # learn reusable skills from verified tasks
    recall_k: int = 6


class ChannelConfig(BaseModel):
    enabled: bool = False
    allowed_user_ids: list[str] = Field(default_factory=list)


class FilesConfig(BaseModel):
    # "auto" = a Delta folder on the Desktop Windows actually shows. Or any absolute path.
    save_dir: str = "auto"


def known_folder(name: str) -> Path:
    """Where Windows really keeps a shell folder ("Desktop", "Personal" = Documents).
    OneDrive backup can move these, so ~/Desktop is not reliable."""
    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, _ = winreg.QueryValueEx(k, name)
        return Path(os.path.expandvars(value))
    except OSError:
        return Path.home() / ("Documents" if name == "Personal" else name)


def in_onedrive(path: Path) -> bool:
    roots = [os.environ.get(v) for v in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    p = str(path).lower()
    return any(r and p.startswith(r.lower()) for r in roots) or "\\onedrive" in p


def save_dir(cfg: FilesConfig, create: bool = True) -> Path:
    """Default folder for files Delta creates for the user.

    auto: <Desktop>\\Delta files when the Desktop is a normal local folder. Not "Delta": that name is
    the app's own shortcut, and two Desktop icons called "Delta" make a double-click open an empty
    folder instead of the app. A Desktop\\Delta folder that already holds files is kept. When OneDrive has taken over
    the Desktop, files go to a local folder OneDrive never syncs (%USERPROFILE%\\Delta), so a full
    or offline OneDrive cannot break saving; a Desktop shortcut keeps it one click away
    (see ensure_desktop_shortcut).
    """
    if cfg.save_dir != "auto":
        path = Path(os.path.expandvars(os.path.expanduser(cfg.save_dir)))
    else:
        desktop = known_folder("Desktop")
        legacy = desktop / "Delta"
        if in_onedrive(desktop):
            path = Path.home() / "Delta"
        elif legacy.is_dir() and any(legacy.iterdir()):
            path = legacy
        else:
            path = desktop / "Delta files"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_desktop_shortcut(folder: Path) -> Path | None:
    """Put a 'Delta' shortcut to `folder` on the visible Desktop, if the folder is not already there."""
    desktop = known_folder("Desktop")
    if folder.parent == desktop or not desktop.exists():
        return None
    link = desktop / "Delta files.lnk"
    if link.exists():
        return link
    import subprocess

    script = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
              f"$s.TargetPath='{folder}';$s.Description='Files made by Delta (stored on this PC)';$s.Save()")
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   capture_output=True, timeout=15, check=False)
    return link if link.exists() else None


class AddonsConfig(BaseModel):
    enabled: list[str] = Field(default_factory=list)  # nothing runs unless listed here
    config: dict[str, dict[str, Any]] = Field(default_factory=dict)


class BrowserConfig(BaseModel):
    # your-browser: your normal signed-in Chrome/Edge, driven like a person (default)
    # delta-profile: a separate Delta profile, automated in the background, not signed in
    mode: Literal["your-browser", "delta-profile"] = "your-browser"
    prefer: Literal["auto", "chrome", "edge"] = "auto"  # auto = your Windows default browser
    orange_cursor: bool = True  # orange mouse pointer while Delta controls the mouse/keyboard


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"  # never 0.0.0.0: remote access goes through Tailscale/Cloudflare Tunnel
    port: int = 7878


class QuietHours(BaseModel):
    enabled: bool = False
    start: str = "23:30"
    end: str = "07:00"


Autonomy = Literal["ask-everything", "ask-risky", "trusted", "autopilot"]


class Boundaries(BaseModel):
    autonomy: Autonomy = "ask-risky"
    allowed_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)
    denied_apps: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    allowed_domains: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)
    quiet_hours: QuietHours = Field(default_factory=QuietHours)


class AppEntry(BaseModel):
    command: str | None = None
    path: str | None = None
    aliases: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    agent: AgentConfig = Field(default_factory=AgentConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    governor: GovernorConfig = Field(default_factory=GovernorConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    channels: dict[str, ChannelConfig] = Field(default_factory=dict)
    api: ApiConfig = Field(default_factory=ApiConfig)
    files: FilesConfig = Field(default_factory=FilesConfig)
    addons: AddonsConfig = Field(default_factory=AddonsConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    boundaries: Boundaries = Field(default_factory=Boundaries)
    apps: dict[str, AppEntry] = Field(default_factory=dict)
    config_dir: Path = REPO_ROOT / "config"
    data_dir: Path = Path(".")


def _config_dir() -> Path:
    env = os.environ.get("DELTA_CONFIG_DIR")
    if env:
        return Path(env)
    cwd = Path.cwd() / "config"
    return cwd if (cwd / "delta.yaml").exists() else REPO_ROOT / "config"


def _data_dir() -> Path:
    env = os.environ.get("DELTA_DATA_DIR")
    base = Path(env) if env else Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "Delta"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


USER_SETTINGS = "settings.yaml"  # in the data dir: the user's own choices (setup page, settings page)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read_user_settings(data_dir: Path) -> dict[str, Any]:
    return _read_yaml(data_dir / USER_SETTINGS)


def write_user_settings(data_dir: Path, changes: dict[str, Any]) -> dict[str, Any]:
    """Merge `changes` into the user's settings file. The repo's config/ is never modified, so
    updates (git pull, new versions) cannot overwrite what the user chose."""
    merged = deep_merge(read_user_settings(data_dir), changes)
    data_dir.mkdir(parents=True, exist_ok=True)  # first run: the folder may not exist yet
    path = data_dir / USER_SETTINGS
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(merged, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)  # atomic: a crash never leaves a half-written settings file
    return merged


def load_settings(config_dir: Path | None = None) -> Settings:
    """Shipped defaults (config/*.yaml) overlaid with the user's own settings (data dir)."""
    cdir = config_dir or _config_dir()
    data = _data_dir()
    raw = _read_yaml(cdir / "delta.yaml")
    raw["boundaries"] = _read_yaml(cdir / "boundaries.yaml")
    raw["apps"] = _read_yaml(cdir / "apps.yaml").get("apps", {})
    raw = deep_merge(raw, read_user_settings(data))
    raw["config_dir"] = cdir
    raw["data_dir"] = data
    return Settings.model_validate(raw)
