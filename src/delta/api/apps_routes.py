"""Connected apps: AI apps the user already has (Claude Code, Codex, LM Studio).

Settings shows whether each one is installed and signed in, opens the app's own sign-in window,
and can send a one-line test message. Sign-in happens entirely in the vendor's CLI and browser
page; Delta never handles the password or token.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from delta.bootstrap import Container
from delta.config import write_user_settings
from delta.llm.cli_providers import CLIProvider
from delta.llm.schemas import LLMError
from delta.system.logging import log
from delta.system.secrets import get_secret, set_secret

LABELS = {"claude_code": "Claude Code (your Claude plan)", "codex": "Codex (your ChatGPT plan)",
          "cline": "Cline (your Cline account)", "deepseek": "DeepSeek (your API key)",
          "lmstudio": "LM Studio (on this PC)", "ollama": "Ollama (on this PC)",
          "groq": "Groq (free cloud key)", "nim": "NVIDIA NIM (free cloud key)",
          "nebius": "Nebius Token Factory (your Nebius credits)",
          "gemini": "Google Gemini (free cloud key)", "openrouter": "OpenRouter (free cloud key)"}


class PreferredIn(BaseModel):
    name: str
    fallback: bool | None = None  # llm.fallback: other AIs may answer when the main AI fails


class BackupsIn(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=12)


class GoogleClientIn(BaseModel):
    client_id: str = Field(min_length=20, max_length=200, pattern=r"^[\w.-]+\.apps\.googleusercontent\.com$")
    client_secret: str = Field(default="", max_length=200)


JOBS = ("plan", "chat", "verify")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")


class ModelsIn(BaseModel):
    models: dict[str, str] = {}
    efforts: dict[str, str] = {}


TEST_MESSAGES = [{"role": "user", "content": "Reply with only the word: ready"}]


def _says(e: Exception) -> str:
    """The message Delta itself wrote into one of its own errors (LLMError, GoogleError): plain words for the
    dashboard, never a traceback or an exception's repr."""
    first = e.args[0] if e.args else ""
    return first[:500] if isinstance(first, str) and first else "That did not work. Details are in Delta's log."


def find_lms() -> str | None:
    """LM Studio's command-line helper, used to start its local server."""
    if found := shutil.which("lms"):
        return found
    for name in ("lms.exe", "lms"):
        if (p := Path.home() / ".lmstudio" / "bin" / name).is_file():
            return str(p)
    return None


def backup_allowed(c: Any, name: str) -> bool:
    """A provider that may be a backup AI: an API key of the user's own. Never the AI apps (Claude Code, Codex,
    Cline: those spend a plan the user did not hand over for this) and never a model that runs on this PC."""
    p = c.settings.llm.providers.get(name)
    return p is not None and p.kind == "openai" and not p.local and bool(p.secret)


def register(app: FastAPI, c: Container, auth: Any) -> None:
    def connected() -> dict[str, Any]:
        out = {}
        for name, p in c.router.providers.items():
            cfg = getattr(p, "cfg", None)
            if isinstance(p, CLIProvider) or (cfg is not None and cfg.discover == "lmstudio"):
                out[name] = p
        return out

    def get(name: str) -> Any:
        p = connected().get(name)
        if p is None:
            raise HTTPException(404, "unknown app")
        return p

    async def describe(name: str, p: Any) -> dict[str, Any]:
        if isinstance(p, CLIProvider):
            if not p.available():
                p.refresh()  # the page is open because the user is looking: check again now, not in a minute
            exe = await asyncio.to_thread(p.executable)
            signed = await p.signed_in() if exe else None
            return {"name": name, "label": p.label, "kind": "cli", "installed": bool(exe),
                    "signed_in": signed, "path": exe,
                    "models": {j: p.model_for(j) for j in JOBS}, "efforts": {j: p.effort_for(j) for j in JOBS},
                    "options": await asyncio.to_thread(p.model_options)}
        running = await asyncio.to_thread(p.ping)
        return {"name": name, "label": "LM Studio", "kind": "local", "installed": find_lms() is not None,
                "running": running, "url": p.cfg.base_url}

    @app.get("/api/apps", dependencies=[Depends(auth)])
    async def list_apps() -> list[dict[str, Any]]:
        return list(await asyncio.gather(*(describe(n, p) for n, p in connected().items())))

    @app.get("/api/apps/preferred", dependencies=[Depends(auth)])
    async def get_preferred() -> dict[str, Any]:
        ready = set(await asyncio.to_thread(c.router.usable))
        return {"preferred": c.router.preferred, "fallback": c.router.fallback,
                "options": [{"name": n, "label": LABELS.get(n, n), "ready": n in ready} for n in c.router.providers],
                # Backup AIs: only your own API keys can be one, never an AI app's plan or a model on this PC
                "backups": list(c.router.fallbacks),
                "backup_choices": [{"name": n, "label": LABELS.get(n, n), "ready": n in ready}
                                   for n in c.router.providers if backup_allowed(c, n)]}

    @app.put("/api/apps/backups", dependencies=[Depends(auth)])
    async def set_backups(body: BackupsIn) -> dict[str, Any]:
        """The AIs that take over when the main AI runs out of credits or hits its limit (router.fallbacks)."""
        names = list(dict.fromkeys(body.names))
        bad = [n for n in names if not backup_allowed(c, n)]
        if bad:
            raise HTTPException(400, f"{', '.join(bad)} cannot be a backup AI: only an AI you use with your own API "
                                     "key can (not an AI app's plan, and not a model on this PC).")
        await asyncio.to_thread(write_user_settings, c.settings.data_dir, {"router": {"fallbacks": names}})
        c.settings.router.fallbacks = c.router.fallbacks = names
        c.audit.record("apps.backups", names=names)
        return {"ok": True, "backups": names}

    @app.put("/api/apps/preferred", dependencies=[Depends(auth)])
    async def set_preferred(body: PreferredIn) -> dict[str, Any]:
        if body.name != "auto" and body.name not in c.router.providers:
            raise HTTPException(400, "unknown AI")
        change: dict[str, Any] = {"preferred": body.name}
        if body.fallback is not None:
            change["fallback"] = body.fallback
        await asyncio.to_thread(write_user_settings, c.settings.data_dir, {"llm": change})
        c.settings.llm.preferred = c.router.preferred = body.name
        if body.fallback is not None:
            c.settings.llm.fallback = c.router.fallback = body.fallback
        c.audit.record("apps.preferred", name=body.name, fallback=c.router.fallback)
        return {"ok": True}

    @app.put("/api/apps/{name}/models", dependencies=[Depends(auth)])
    async def set_models(name: str, body: ModelsIn) -> dict[str, Any]:
        p = get(name)
        if not isinstance(p, CLIProvider):
            raise HTTPException(400, "this app has no model choice")
        models = {j: m.strip() for j, m in body.models.items() if j in JOBS and len(m) <= 80}
        efforts = {j: e for j, e in body.efforts.items() if j in JOBS and e in ("", *EFFORT_LEVELS)}
        p.cfg.models.update(models)
        p.cfg.efforts.update(efforts)
        await asyncio.to_thread(write_user_settings, c.settings.data_dir,
                                {"llm": {"providers": {name: {"models": models, "efforts": efforts}}}})
        c.audit.record("apps.models", app=name, models=models, efforts=efforts)
        return {"ok": True}

    # --- Google (official Calendar API) ---------------------------------------------------------
    @app.get("/api/google", dependencies=[Depends(auth)])
    async def google_status() -> dict[str, Any]:
        has_client = bool(await asyncio.to_thread(get_secret, "google_client_id"))
        has_token = bool(await asyncio.to_thread(get_secret, "google_refresh_token"))
        return {"client": has_client, "connected": has_client and has_token}

    @app.put("/api/google/client", dependencies=[Depends(auth)])
    async def google_client(body: GoogleClientIn) -> dict[str, Any]:
        await asyncio.to_thread(set_secret, "google_client_id", body.client_id.strip())
        if body.client_secret.strip():
            await asyncio.to_thread(set_secret, "google_client_secret", body.client_secret.strip())
        c.audit.record("google.client_set")
        return {"ok": True}

    @app.post("/api/google/connect", dependencies=[Depends(auth)])
    async def google_connect() -> dict[str, Any]:
        from delta.integrations import google

        try:
            account = await google.sign_in()
        except google.GoogleError as e:
            return {"ok": False, "message": _says(e)}
        c.audit.record("google.connected", account=account)
        return {"ok": True, "message": f"Connected to Google Calendar ({account}). Meetings now use the official API."}

    @app.post("/api/google/disconnect", dependencies=[Depends(auth)])
    async def google_disconnect() -> dict[str, Any]:
        from delta.integrations import google

        await google.sign_out()
        c.audit.record("google.disconnected")
        return {"ok": True, "message": "Disconnected; Delta's access was revoked at Google."}

    @app.post("/api/apps/{name}/login", dependencies=[Depends(auth)])
    async def login(name: str) -> dict[str, Any]:
        p = get(name)
        if not isinstance(p, CLIProvider):
            raise HTTPException(400, "this app has no sign-in")
        try:
            await asyncio.to_thread(p.open_login)
        except LLMError as e:
            return {"ok": False, "message": _says(e)}
        except OSError as e:  # the detail (paths, error codes) goes to the log, not to the page
            log.warning("apps.login_failed", app=name, error=str(e))
            return {"ok": False, "message": f"Could not open the {p.label} sign-in window. Check that {p.label} is "
                                            "installed, then try again."}
        c.audit.record("apps.login_opened", app=name)
        return {"ok": True, "message": f"A {p.label} sign-in window opened. Finish signing in there "
                                       "(your browser opens), then press Check."}

    @app.post("/api/apps/{name}/start", dependencies=[Depends(auth)])
    async def start(name: str) -> dict[str, Any]:
        p = get(name)
        lms = find_lms()
        if isinstance(p, CLIProvider) or lms is None:
            return {"ok": False, "message": "LM Studio is not installed."}
        r = await asyncio.to_thread(subprocess.run, [lms, "server", "start"], capture_output=True, text=True,
                                    timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        p._ping_at = -1e9  # check again now, not in 30 s
        return {"ok": r.returncode == 0, "message": (r.stdout or r.stderr).strip()[-300:]}

    @app.post("/api/apps/{name}/stop", dependencies=[Depends(auth)])
    async def stop(name: str) -> dict[str, Any]:
        """Unload every model (frees its RAM/VRAM), then stop the server. LM Studio itself stays
        installed; Start server brings it back."""
        p = get(name)
        lms = find_lms()
        if isinstance(p, CLIProvider) or lms is None:
            return {"ok": False, "message": "LM Studio is not installed."}

        def run(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run([lms, *args], capture_output=True, text=True, timeout=60,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        await asyncio.to_thread(run, "unload", "--all")
        r = await asyncio.to_thread(run, "server", "stop")
        p._ping_at = -1e9
        c.audit.record("apps.stopped", app=name)
        ok = r.returncode == 0
        return {"ok": ok, "message": "Server stopped and models unloaded: memory is free again."
                if ok else (r.stdout or r.stderr).strip()[-300:]}

    @app.post("/api/apps/{name}/test", dependencies=[Depends(auth)])
    async def test(name: str) -> dict[str, Any]:
        p = get(name)
        if callable(refresh := getattr(p, "refresh", None)):
            refresh()
        try:
            r = await p.complete("chat", TEST_MESSAGES, None, False)
        except LLMError as e:
            return {"ok": False, "message": _says(e)}
        c.router.breakers[name].record_success()
        return {"ok": True, "message": f"Answered: {r.content.strip()[:80]!r} (model {r.model})"}
