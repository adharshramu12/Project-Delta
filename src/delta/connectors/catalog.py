"""The connected apps catalog (config/mcp_catalog.yaml) and custom servers the user adds.

Entries are validated when loaded: a remote server must use https (plain http only for a server on this PC),
a local server must pin its package version, and an entry the catalog cannot read is skipped with a warning
instead of stopping Delta. `command_for()` turns a local entry into the exact program and arguments that will
run, which is also what the custom-server confirmation shows the user.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from delta.system.logging import log
from delta.tools.base import Risk

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
NODE_HINT = ("Node.js is not installed (Delta needs its `npx` to run this app's server on your PC). Install the LTS "
             "version from https://nodejs.org or run `winget install OpenJS.NodeJS.LTS`, then connect again.")
UV_HINT = ("uv is not installed (Delta needs its `uvx` to run this app's server on your PC). Install it with "
           "`winget install astral-sh.uv` or see https://docs.astral.sh/uv/getting-started/installation/, then "
           "connect again.")


class RuntimeMissingError(RuntimeError):
    """npx or uvx is not on this PC: the message says how to install it."""


class AuthSpec(BaseModel):
    type: Literal["oauth", "token", "none"] = "none"
    client: Literal["dynamic", "user"] = "dynamic"  # dynamic client registration, or the user's own OAuth app
    client_secret: bool = False                      # the user's OAuth app also has a secret (confidential)
    scopes: list[str] = Field(default_factory=list)
    extra_params: dict[str, str] = Field(default_factory=dict)  # e.g. Google: access_type=offline
    redirect_port: int = 0                           # 0 = a random free port (vendors that allow any loopback port)
    scheme: str = "Bearer"                           # token auth: Authorization: <scheme> <token>
    label: str = ""
    help: str = ""


class InputSpec(BaseModel):
    name: str
    label: str = ""


class SecretSpec(BaseModel):
    env: str                 # the environment variable the local server reads it from
    label: str = ""


class ToolDefaults(BaseModel):
    read: list[str] = Field(default_factory=list)         # exact names or simple globs (get_*)
    destructive: list[str] = Field(default_factory=list)  # off until the user switches them on


class AppSpec(BaseModel):
    id: str
    name: str
    publisher: str = ""
    status: Literal["official", "official-preview", "reference", "community", "custom"] = "custom"
    description: str = ""
    transport: Literal["http", "sse", "stdio"] = "http"
    url: str = ""
    sse_url: str = ""
    runtime: Literal["", "node", "python", "command"] = ""
    package: str = ""
    command: str = ""        # custom local servers: the program to run
    args: list[str] = Field(default_factory=list)
    inputs: list[InputSpec] = Field(default_factory=list)
    secrets: list[SecretSpec] = Field(default_factory=list)
    auth: AuthSpec = Field(default_factory=AuthSpec)
    docs: str = ""
    checked: str = ""
    notes: str = ""
    tools: ToolDefaults = Field(default_factory=ToolDefaults)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("app ids are 2 to 32 lowercase letters, digits or _ and start with a letter")
        return v

    @model_validator(mode="after")
    def _shape(self) -> AppSpec:
        if self.transport in ("http", "sse"):
            check_url(self.url)
            if self.sse_url:
                check_url(self.sse_url)
        else:
            if self.runtime == "node" and not re.search(r".@[0-9][\w.\-+]*$", self.package):
                raise ValueError(f"{self.id}: an npx package must pin a version (name@1.2.3)")
            if self.runtime == "python" and "==" not in self.package:
                raise ValueError(f"{self.id}: a uvx package must pin a version (name==1.2.3)")
            if self.runtime in ("", "command") and not self.command:
                raise ValueError(f"{self.id}: a local server needs a command")
        return self

    @property
    def official(self) -> bool:
        return self.status in ("official", "official-preview", "reference")

    @property
    def remote(self) -> bool:
        return self.transport != "stdio"

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "publisher": self.publisher, "status": self.status,
                "description": self.description, "transport": self.transport, "url": self.url,
                "runtime": self.runtime, "package": self.package, "auth": self.auth.type,
                "client": self.auth.client if self.auth.type == "oauth" else "",
                "client_secret": self.auth.client_secret, "auth_label": self.auth.label, "auth_help": self.auth.help,
                "redirect_port": self.auth.redirect_port,
                "inputs": [i.model_dump() for i in self.inputs], "secrets": [s.model_dump() for s in self.secrets],
                "docs": self.docs, "checked": self.checked, "notes": self.notes, "official": self.official}


def check_url(url: str) -> None:
    """https only, except a server on this PC (Figma's desktop server, the test server)."""
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise ValueError(f"not a valid address: {url!r}") from e
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host:
        return
    if parts.scheme == "http" and host in LOOPBACK:
        return
    raise ValueError(f"{url!r}: remote servers must use https:// (plain http only for 127.0.0.1)")


def is_loopback(url: str) -> bool:
    """A server on this PC: never sent through a proxy configured for the internet."""
    try:
        return (urlsplit(url).hostname or "").lower() in LOOPBACK
    except ValueError:
        return False


def default_risk(spec: AppSpec, tool: str) -> Risk | None:
    """Delta's verified default for one tool name, or None (unknown to the catalog)."""
    if any(fnmatchcase(tool, p) for p in spec.tools.destructive):
        return Risk.DESTRUCTIVE
    if any(fnmatchcase(tool, p) for p in spec.tools.read):
        return Risk.READ
    return None


class Catalog:
    def __init__(self, apps: dict[str, AppSpec], version: int = 1, checked: str = "",
                 problems: list[str] | None = None) -> None:
        self.apps = apps
        self.version = version
        self.checked = checked
        self.problems = problems or []

    def get(self, app_id: str) -> AppSpec | None:
        return self.apps.get(app_id)

    def list(self) -> list[AppSpec]:
        return list(self.apps.values())


def load_catalog(path: Path) -> Catalog:
    """Read the catalog. A broken entry is skipped (and listed in `problems`); a missing file is an empty catalog."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        log.warning("connectors.catalog_unreadable", path=str(path), error=str(e))
        return Catalog({}, problems=[f"catalog unreadable: {e}"])
    apps: dict[str, AppSpec] = {}
    problems: list[str] = []
    for app_id, entry in (raw.get("apps") or {}).items():
        try:
            apps[str(app_id)] = AppSpec.model_validate({**(entry or {}), "id": str(app_id)})
        except ValidationError as e:
            problems.append(f"{app_id}: {e.errors(include_url=False)[0]['msg']}")
            log.warning("connectors.catalog_entry_skipped", app=app_id, error=str(e))
    return Catalog(apps, int(raw.get("version", 1) or 1), str(raw.get("checked", "")), problems)


# --- local servers ----------------------------------------------------------------------------------------------

def find_runtime(runtime: str, which: Any = shutil.which) -> str:
    """Full path of npx (node) or uvx (python). Raises RuntimeMissingError with an install hint."""
    if runtime == "node":
        found = which("npx")
        if not found:
            raise RuntimeMissingError(NODE_HINT)
        return str(found)
    if runtime == "python":
        found = which("uvx")
        if not found:
            raise RuntimeMissingError(UV_HINT)
        return str(found)
    raise ValueError(f"unknown runtime {runtime!r}")


def fill_inputs(args: list[str], values: dict[str, str]) -> list[str]:
    out = []
    for a in args:
        for k, v in values.items():
            a = a.replace("{" + k + "}", v)
        if re.search(r"\{\w+\}", a):
            raise ValueError(f"missing a value for {a}")
        out.append(a)
    return out


def command_for(spec: AppSpec, inputs: dict[str, str] | None = None, which: Any = shutil.which) -> list[str]:
    """The exact program and arguments a local server runs as (what the user confirms for a custom server)."""
    args = fill_inputs(spec.args, inputs or {})
    if spec.runtime == "node":
        return [find_runtime("node", which), "-y", spec.package, *args]
    if spec.runtime == "python":
        name = spec.package.split("==", 1)[0]
        return [find_runtime("python", which), "--from", spec.package, name, *args]
    program = spec.command
    found = which(program) if not os.path.isabs(program) else program
    if not found:
        lowered = os.path.basename(program).lower()
        if lowered in ("npx", "npx.cmd"):
            raise RuntimeMissingError(NODE_HINT)
        if lowered in ("uvx", "uvx.exe"):
            raise RuntimeMissingError(UV_HINT)
        raise RuntimeMissingError(f"'{program}' was not found on this PC. Install it or give its full path.")
    return [str(found), *args]


def custom_spec(app_id: str, name: str, *, url: str = "", command: str = "", auth: str = "none",
                client_id_needed: bool = False) -> AppSpec:
    """A server the user adds by hand: a URL (remote) or a command line (local). Marked custom everywhere."""
    if bool(url) == bool(command):
        raise ValueError("give either a server URL or a command, not both")
    if url:
        oauth = AuthSpec(type="oauth", client="user" if client_id_needed else "dynamic") if auth == "oauth" else \
            AuthSpec(type=auth)  # type: ignore[arg-type]
        return AppSpec(id=app_id, name=name or app_id, status="custom", transport="http", url=url.strip(), auth=oauth,
                       description="A server you added yourself.")
    parts = shlex.split(command, posix=os.name != "nt")
    if not parts:
        raise ValueError("the command is empty")
    program = parts[0].strip('"')
    return AppSpec(id=app_id, name=name or app_id, status="custom", transport="stdio", runtime="command",
                   command=program, args=[a.strip('"') for a in parts[1:]], auth=AuthSpec(type="none"),
                   description="A program on this PC you added yourself.")
