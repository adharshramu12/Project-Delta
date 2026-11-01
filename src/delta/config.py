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
    base_url: str
    secret: str | None = None  # name of the secret holding the API key
    local: bool = False
    native_tools: bool = True
    keep_alive: str | None = None
    timeout_s: float = 60
    models: dict[str, str] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    routes: dict[str, list[str]] = Field(default_factory=dict)


class AgentConfig(BaseModel):
    name: str = "Delta"
    owner_name: str = ""
    max_steps: int = 15
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

    auto: <Desktop>\\Delta when the Desktop is a normal local folder. When OneDrive has taken over
    the Desktop, files go to a local folder OneDrive never syncs (%USERPROFILE%\\Delta), so a full
    or offline OneDrive cannot break saving; a Desktop shortcut keeps it one click away
    (see ensure_desktop_shortcut).
    """
    if cfg.save_dir != "auto":
        path = Path(os.path.expandvars(os.path.expanduser(cfg.save_dir)))
    else:
        desktop = known_folder("Desktop")
        path = Path.home() / "Delta" if in_onedrive(desktop) else desktop / "Delta"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_desktop_shortcut(folder: Path) -> Path | None:
    """Put a 'Delta' shortcut to `folder` on the visible Desktop, if the folder is not already there."""
    desktop = known_folder("Desktop")
    if folder.parent == desktop or not desktop.exists():
        return None
    link = desktop / "Delta.lnk"
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


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"  # never 0.0.0.0: remote access goes through Tailscale/Cloudflare Tunnel
    port: int = 7878


class QuietHours(BaseModel):
    enabled: bool = False
    start: str = "23:30"
    end: str = "07:00"


Autonomy = Literal["ask-everything", "ask-risky", "trusted"]


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


def load_settings(config_dir: Path | None = None) -> Settings:
    cdir = config_dir or _config_dir()
    raw = _read_yaml(cdir / "delta.yaml")
    raw["boundaries"] = _read_yaml(cdir / "boundaries.yaml")
    raw["apps"] = _read_yaml(cdir / "apps.yaml").get("apps", {})
    raw["config_dir"] = cdir
    raw["data_dir"] = _data_dir()
    return Settings.model_validate(raw)
