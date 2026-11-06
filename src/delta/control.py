"""Changes a user can make from any channel (dashboard, Telegram): main AI, models, permission level,
stopping the running task. One implementation, so every channel behaves the same and is audited."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from delta.config import write_user_settings

if TYPE_CHECKING:
    from delta.bootstrap import Container

LEVELS = {"ask-everything": "Careful", "ask-risky": "Balanced", "trusted": "Trusted", "autopilot": "Autopilot"}
LEVEL_HELP = {
    "ask-everything": "asks before anything except reading",
    "ask-risky": "asks before changing or sending anything",
    "trusted": "doesn't ask about undoable work; asks before sending",
    "autopilot": "sends too; still asks on the riskiest steps",
}
# The whole truth about what still asks, for places with room for it (Telegram /mode, docs).
AUTOPILOT_STILL_ASKS = (
    "Autopilot still asks before: deleting anything that can't be undone, changing files outside your allowed "
    "folders, typing or clicking in a terminal, the Run box or Delta itself, emergency alerts you didn't clearly "
    "ask for, and, once a task has read a web page, file or message, before sending, deleting, running code, "
    "saving to memory, or opening a link that carries hidden data."
)
JOBS = ("plan", "chat", "verify")


def main_ai_choices(c: Container) -> list[dict[str, Any]]:
    ready = set(c.router.usable())
    return [{"name": n, "ready": n in ready, "current": n == c.router.preferred} for n in c.router.providers]


def set_main_ai(c: Container, name: str, by: str) -> str:
    if name != "auto" and name not in c.router.providers:
        raise ValueError(f"unknown AI '{name}'")
    write_user_settings(c.settings.data_dir, {"llm": {"preferred": name}})
    c.settings.llm.preferred = c.router.preferred = name
    c.audit.record("apps.preferred", name=name, by=by)
    return name


def model_choices(c: Container, name: str) -> list[dict[str, Any]]:
    p = c.router.providers.get(name)
    options = getattr(p, "model_options", None)
    return list(options()) if callable(options) else []


def set_model(c: Container, name: str, model: str, by: str, jobs: tuple[str, ...] = JOBS) -> None:
    """The model `name` uses for every job (planning, doing steps, checking)."""
    p = c.router.providers.get(name)
    cfg = getattr(p, "cfg", None)
    if cfg is None or not hasattr(cfg, "models"):
        raise ValueError(f"{name} has no model choice")
    models = {j: model.strip()[:80] for j in jobs}
    cfg.models.update(models)
    write_user_settings(c.settings.data_dir, {"llm": {"providers": {name: {"models": models}}}})
    c.audit.record("apps.models", app=name, models=models, by=by)


def set_autonomy(c: Container, level: str, by: str) -> str:
    if level not in LEVELS:
        raise ValueError(f"unknown permission level '{level}'")
    write_user_settings(c.settings.data_dir, {"boundaries": {"autonomy": level}})
    c.settings.boundaries.autonomy = level  # type: ignore[assignment]
    c.orchestrator.executor.policy.b.autonomy = level  # type: ignore[assignment]
    c.audit.record("settings.changed", sections=["boundaries"], autonomy=level, by=by)
    return LEVELS[level]


def key_status() -> list[dict[str, str]]:
    """Each AI/search key Delta knows: name, label, masked value (never the key itself)."""
    from delta.setup.checks import KEY_INFO
    from delta.system.secrets import get_secret, mask

    return [{"name": n, "label": label, "masked": mask(get_secret(n))} for n, label in KEY_INFO.items()]


async def replace_key(c: Container, name: str, value: str, by: str) -> tuple[bool, str]:
    """Test a new key with its service first; only a working key replaces the old one. Takes effect now."""
    from delta.setup.checks import KEY_INFO, validate_key
    from delta.system.secrets import mask, set_secret

    if name not in KEY_INFO:
        return False, f"unknown key '{name}'"
    ok, message = await validate_key(name, value.strip())
    if not ok:
        return False, f"That key didn't work ({message}). Your old key is unchanged."
    set_secret(name, value.strip())
    c.router.refresh()  # used from the next call on, no restart
    c.audit.record("keys.replaced", name=name, by=by)  # which key, never its value
    return True, f"{KEY_INFO[name]}: new key works and is in use now ({mask(value.strip())})."


def let_go(c: Container) -> None:
    """Stop driving input now: refuse any further key or mouse press, hide the in-control ring, and release
    every key and mouse button Delta still holds down. Nothing the user owns is closed."""
    from delta.system import input_stop

    input_stop.request()
    signal = getattr(c, "signal", None)
    if signal is not None:
        signal.stop()
    input_stop.release_held()
    with contextlib.suppress(RuntimeError):  # an action on the UI thread may press one more key: let go again
        asyncio.get_running_loop().call_later(0.3, input_stop.release_held)


def stop_running(c: Container, by: str) -> bool:
    """Cancel the task Delta is working on now (from any channel). False if nothing is running."""
    task = c.running
    if task is None or task.done():
        return False
    c.audit.record("task.cancel_requested", by=by)
    let_go(c)
    task.cancel()
    return True


def stop_task(c: Container, task_id: str, by: str) -> bool:
    """Cancel one task by its id, running or still waiting its turn. False if there is no such task."""
    task = getattr(c, "active", {}).get(task_id)
    if task is None or task.done():
        return False
    c.audit.record("task.cancel_requested", task=task_id, by=by)
    current = c.running is task
    if current:
        let_go(c)
    task.cancel()
    return True


def stop_all(c: Container, by: str) -> int:
    """Cancel the running task and every task waiting its turn. Returns how many were stopped."""
    tasks = {id(t): t for t in [c.running, *getattr(c, "active", {}).values()] if t is not None and not t.done()}
    if not tasks:
        return 0
    c.audit.record("task.cancel_all_requested", by=by, count=len(tasks))
    let_go(c)
    for t in tasks.values():
        t.cancel()
    return len(tasks)


STOP_WORDS = {"stop": "one", "/stop": "one", "stop all": "all", "/stop all": "all", "/stopall": "all",
              "stop everything": "all"}


def stop_command(c: Container, text: str, by: str) -> str | None:
    """A chat message that means stop ("/stop", "stop", "/stop all", "stop all"), from any channel (Telegram,
    WhatsApp, Slack): stop and return the reply. Anything else returns None and is handled as usual."""
    words = " ".join(text.strip().lower().rstrip(".!").split())
    kind = STOP_WORDS.get(words)
    if kind is None:
        return None
    if kind == "all":
        n = stop_all(c, by)
        return f"Stopped {n} task{'s' if n != 1 else ''}." if n else "Nothing is running right now."
    return "Stopped." if stop_running(c, by) else "Nothing is running right now."


# --- which browser Delta uses (dashboard, Telegram /browser, chat set_browser) ------------------------------

def browser_state(c: Container) -> dict[str, Any]:
    """The browser choice as people see it: the four options (unavailable ones with the reason), the private
    browser's engine, and the running task's browser when it differs from the saved one."""
    from delta.tools.browser_choice import describe, detect, engine_options, from_config, options

    hub = getattr(c, "browsers", None)
    inst = hub.installed() if hub is not None else detect()
    pick = from_config(c.settings.browser)
    running = [tb for tb in getattr(hub, "_active", {}).values() if tb.used and tb.pick != pick]
    return {"choice": pick.choice, "engine": pick.engine, "label": pick.label, "describe": describe(pick, inst),
            "options": options(inst, pick), "engines": engine_options(inst, pick),
            "running_note": (f"A task is using {running[0].pick.label} right now; your choice applies from the "
                             "next task.") if running else "",
            "signin_available": hub is not None and getattr(hub, "mgr", None) is not None}


def set_browser(c: Container, choice: str, by: str, engine: str | None = None) -> str:
    """Save the browser choice; the reply says it in plain words (and when a running task keeps the old one)."""
    from delta.tools.browser_switch import save_choice

    return save_choice(c.settings, c.audit, choice, engine, by=by, hub=getattr(c, "browsers", None))


def set_private_engine(c: Container, engine: str, by: str) -> str:
    from delta.tools.browser_switch import save_engine

    return save_engine(c.settings, c.audit, engine, by=by, hub=getattr(c, "browsers", None))


async def open_signin_browser(c: Container, url: str, by: str) -> tuple[bool, str]:
    """'Sign in to my sites': Delta's private browser opens visibly on this PC; the person signs in."""
    hub = getattr(c, "browsers", None)
    if hub is None or getattr(hub, "mgr", None) is None:
        return False, ("Delta's private browser is not available on this PC (it needs Google Chrome or Microsoft "
                       "Edge and the browser add-on).")
    result = await hub.open_signin(url)
    c.audit.record("browser.signin_requested", by=by, ok=result.ok)
    return result.ok, result.output
