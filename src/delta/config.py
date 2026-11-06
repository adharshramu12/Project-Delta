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
    # Tokens one task may use before Delta pauses and asks whether to keep going. 0 = no limit.
    token_budget: int = 0  # 0 or less = off


class SearchConfig(BaseModel):
    order: list[str] = Field(default_factory=lambda: ["tavily", "serpapi", "duckduckgo"])
    max_results: int = 5


class Limit(BaseModel):
    throttle: float
    critical: float


class GovernorConfig(BaseModel):
    sample_interval_s: float = 15  # health checks: cheap, but not every 5 s all day
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
    # Which installed browser runs Delta's private profile (delta-profile mode). auto = Chrome if
    # installed, else Edge (what Delta always did). Added later: older settings files load unchanged.
    private_engine: Literal["auto", "chrome", "edge"] = "auto"
    orange_cursor: bool = True  # orange mouse pointer while Delta controls the mouse/keyboard
    # Delta profile: after an action on the same page, send the AI only what changed on it.
    snapshot_diffs: bool = True
    full_snapshot_every: int = 8  # a full page listing at least this often (below 1 counts as 1)


class CaptchaConfig(BaseModel):
    # Hosts where Delta may read simple image captchas with the vision model (your own accounts only).
    # Empty = off: every captcha goes to you. Changed only by you (`delta captcha allow <host>`).
    allowed_sites: list[str] = Field(default_factory=list)


class WriterConfig(BaseModel):
    # Writer and Documents (write_post, prepare_post, make_document). See README "Writer and Documents".
    enabled: bool = True
    output_dir: str = "auto"  # auto = <Documents>\\Delta. Or any absolute path.
    page_size: Literal["A4", "Letter"] = "A4"
    latex: Literal["auto", "off"] = "auto"  # auto = use tectonic/xelatex/pdflatex only if already installed
    pdf_browser: str = ""  # Chrome/Edge/Chromium used to print PDFs; "" = found automatically
    voice_file: str = ""  # your past posts (plain text) to copy your voice; "" = <output_dir>\\my-posts.txt
    substack_publication: str = ""  # e.g. "yourname": prepare_post opens yourname.substack.com/publish/post
    model_task: Literal["plan", "chat"] = "plan"  # which AI route writes (plan = the strongest models first)


class ApiConfig(BaseModel):
    host: str = "127.0.0.1"  # never 0.0.0.0: remote access goes through Tailscale/Cloudflare Tunnel
    port: int = 7878


class QuietHours(BaseModel):
    enabled: bool = False
    start: str = "23:30"
    end: str = "07:00"


class SchedulerConfig(BaseModel):
    """Scheduled tasks and reminders (delta.scheduler). Runs only while the background service is on."""
    enabled: bool = True
    # a run missed while the PC was off or asleep still runs once if it is at most this late
    grace_minutes: float = Field(default=60, ge=0)
    # scheduled AI runs at once (Delta still does one heavy task at a time overall)
    max_concurrent: int = Field(default=1, ge=1, le=4)
    history_limit: int = Field(default=50, ge=1, le=1000)  # runs kept per schedule
    timezone: str = ""  # IANA name like "Asia/Kolkata"; empty = this PC's own time zone


class EngineConfig(BaseModel):
    """Durable task engine (delta.engine): task states, the step log, crash resume and the tool watchdog.
    Added later: older settings files load unchanged and get these defaults."""
    enabled: bool = True  # off = tasks run exactly as before, nothing is written to tasks.db
    retention_days: float = Field(default=30, ge=0)  # finished tasks older than this are removed (0 = keep)
    max_mb: float = Field(default=50, ge=0)  # tasks.db above this: oldest finished tasks go first (0 = no cap)
    max_events_per_task: int = Field(default=2000, ge=10, le=100_000)
    # Watchdog: seconds one tool call may take before it is stopped (with every process it started).
    # 0 = no limit. A tool's own timeout_s argument (run_shell, run_python) is honoured plus a grace period.
    default_tool_timeout_s: float = Field(default=600, ge=0)
    tool_timeouts: dict[str, float] = Field(default_factory=dict)  # per tool name, e.g. {"web_search": 60}


class SelfHealConfig(BaseModel):
    """Self-heal (delta.selfheal): when a step fails, Delta classifies why and tries a fixed, safe recovery
    (re-read the page, wait and retry once, close a harmless banner, restart its own browser, back off on a
    busy AI) before asking the model or the user. Added later: older settings files load with these defaults.
    Every retry still goes through the same policy and approvals as the first try."""
    enabled: bool = True
    max_per_step: int = Field(default=2, ge=0, le=5)    # recovery attempts for one failed step
    max_per_task: int = Field(default=6, ge=0, le=50)   # recovery attempts in one task, all steps together
    # Close known harmless banners (cookie notices, "not now" prompts) when they block a click. Never on
    # payment, permission, sign-in or security dialogs, and never by accepting anything.
    dismiss_benign_popups: bool = True
    # Strategy ladder: after this many failures of one kind of UI action, step down a rung
    # (page structure -> Windows UI Automation -> screen vision -> ask you).
    strategy_after: int = Field(default=2, ge=1, le=10)
    max_backoff_s: float = Field(default=30, ge=0, le=300)  # longest wait when the AI service is busy


def _observe_only() -> list[Literal["observe", "act"]]:
    return ["observe"]


class RouterConfig(BaseModel):
    """Opt-in model ladder and failover (delta.llm.ladder). Both are OFF by default: with the defaults the
    router behaves exactly as before (same providers, same routes, same CLI providers)."""
    # Cheaper models of YOUR OWN configured providers (names from llm.providers), cheapest first. Simple
    # observation steps start on the first one; after `escalate_after` failed steps Delta moves up the
    # ladder and finally to your normal route. Planning and verification always use your normal route.
    ladder: list[str] = Field(default_factory=list)
    escalate_after: int = Field(default=2, ge=1, le=10)
    # Step types that may start on the cheapest model: "observe" (reading after a read-only step) and/or
    # "act" (any other agent step).
    cheap_steps: list[Literal["observe", "act"]] = Field(default_factory=_observe_only)
    # Extra providers (names from llm.providers) tried ONLY when every provider on the normal route failed
    # because of a rate limit, an outage or a used-up quota. Never a provider you did not list here.
    fallbacks: list[str] = Field(default_factory=list)


class UndoConfig(BaseModel):
    """Undo for file changes (delta.selfheal.undo): before Delta changes, moves or recycles a file inside
    your allowed folders it keeps a copy in <data folder>/undo/<task id>/, so `delta undo <task id>`, the
    dashboard or Telegram (with your PIN) can put it back."""
    enabled: bool = True
    max_file_mb: float = Field(default=25, ge=0)    # bigger files are not copied (you are told); 0 = no copies
    max_task_mb: float = Field(default=200, ge=0)   # copies kept for one task at most
    retention_days: float = Field(default=7, ge=0)  # older undo copies are removed (0 = keep)


class PerceptionConfig(BaseModel):
    """How Delta sees (delta.perception). Added later: older settings files load with these defaults.
    Pictures stay on this PC except that a picture goes to the vision model you chose."""
    # Set-of-marks: numbered boxes over clickable elements; the model answers with a number and Delta clicks
    # that element by its handle. Offered only when a model that can see pictures is set up.
    marks: bool = True
    max_marks: int = Field(default=40, ge=5, le=99)      # marks drawn on one picture
    max_side: int = Field(default=1280, ge=512, le=2048)  # longest side of a marked picture, in pixels
    jpeg_quality: int = Field(default=60, ge=30, le=90)
    # Windows' built-in OCR for apps with no accessibility tree. auto = use it when Windows has it.
    ocr: Literal["auto", "off"] = "auto"
    # Do not send a picture or a window read again when it has not changed since the last one.
    cache: bool = True


class HelpersConfig(BaseModel):
    """Parallel read-only helpers (delta.helpers): for a task that needs several independent lookups, the
    planner may hand them to up to `max_helpers` helpers that run at the same time. Helpers can only search the
    web, fetch or read pages in a fresh headless browser context and read files in your allowed folders; they
    can never click, type, write, send or use your own browser (enforced in code). OFF by default: turning it
    on adds a few lines to every planner request, so it costs a little more on simple tasks."""
    enabled: bool = False
    max_helpers: int = Field(default=2, ge=1, le=4)
    token_budget: int = Field(default=6000, ge=500, le=100_000)  # per helper, input + output tokens
    timeout_s: float = Field(default=60, ge=5, le=600)           # per helper
    max_steps: int = Field(default=4, ge=1, le=10)               # model calls per helper
    read_page: bool = True  # helpers may read a page in a fresh headless browser context (no cookies)


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
    captcha: CaptchaConfig = Field(default_factory=CaptchaConfig)
    writer: WriterConfig = Field(default_factory=WriterConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    selfheal: SelfHealConfig = Field(default_factory=SelfHealConfig)
    router: RouterConfig = Field(default_factory=RouterConfig)
    undo: UndoConfig = Field(default_factory=UndoConfig)
    perception: PerceptionConfig = Field(default_factory=PerceptionConfig)
    helpers: HelpersConfig = Field(default_factory=HelpersConfig)
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
