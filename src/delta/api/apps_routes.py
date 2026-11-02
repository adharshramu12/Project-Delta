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
from pydantic import BaseModel

from delta.bootstrap import Container
from delta.config import write_user_settings
from delta.llm.cli_providers import CLIProvider
from delta.llm.schemas import LLMError

LABELS = {"claude_code": "Claude Code (your Claude plan)", "codex": "Codex (your ChatGPT plan)",
          "lmstudio": "LM Studio (on this PC)", "ollama": "Ollama (on this PC)",
          "groq": "Groq (free cloud key)", "nim": "NVIDIA NIM (free cloud key)"}


class PreferredIn(BaseModel):
    name: str


JOBS = ("plan", "chat", "verify")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")


class ModelsIn(BaseModel):
    models: dict[str, str] = {}
    efforts: dict[str, str] = {}


TEST_MESSAGES = [{"role": "user", "content": "Reply with only the word: ready"}]


def find_lms() -> str | None:
    """LM Studio's command-line helper, used to start its local server."""
    if found := shutil.which("lms"):
        return found
    for name in ("lms.exe", "lms"):
        if (p := Path.home() / ".lmstudio" / "bin" / name).is_file():
            return str(p)
    return None


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
        return {"preferred": c.router.preferred,
                "options": [{"name": n, "label": LABELS.get(n, n), "ready": n in ready} for n in c.router.providers]}

    @app.put("/api/apps/preferred", dependencies=[Depends(auth)])
    async def set_preferred(body: PreferredIn) -> dict[str, Any]:
        if body.name != "auto" and body.name not in c.router.providers:
            raise HTTPException(400, "unknown AI")
        await asyncio.to_thread(write_user_settings, c.settings.data_dir, {"llm": {"preferred": body.name}})
        c.settings.llm.preferred = c.router.preferred = body.name
        c.audit.record("apps.preferred", name=body.name)
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

    @app.post("/api/apps/{name}/login", dependencies=[Depends(auth)])
    async def login(name: str) -> dict[str, Any]:
        p = get(name)
        if not isinstance(p, CLIProvider):
            raise HTTPException(400, "this app has no sign-in")
        try:
            await asyncio.to_thread(p.open_login)
        except (LLMError, OSError) as e:
            return {"ok": False, "message": str(e)}
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

    @app.post("/api/apps/{name}/test", dependencies=[Depends(auth)])
    async def test(name: str) -> dict[str, Any]:
        p = get(name)
        if callable(refresh := getattr(p, "refresh", None)):
            refresh()
        try:
            r = await p.complete("chat", TEST_MESSAGES, None, False)
        except LLMError as e:
            return {"ok": False, "message": str(e)}
        c.router.breakers[name].record_success()
        return {"ok": True, "message": f"Answered: {r.content.strip()[:80]!r} (model {r.model})"}
