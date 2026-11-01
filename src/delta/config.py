"""Typed configuration loaded from `config/*.yaml`.

Lookup order for the config directory: $DELTA_CONFIG_DIR, then ./config, then the repo's config/.
Runtime data (memory, audit log, logs) lives in $DELTA_DATA_DIR or %LOCALAPPDATA%/Delta.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

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
    recall_k: int = 6


class ChannelConfig(BaseModel):
    enabled: bool = False
    allowed_user_ids: list[str] = Field(default_factory=list)


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
