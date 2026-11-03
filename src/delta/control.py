"""Changes a user can make from any channel (dashboard, Telegram): main AI, models, permission level,
stopping the running task. One implementation, so every channel behaves the same and is audited."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from delta.config import write_user_settings

if TYPE_CHECKING:
    from delta.bootstrap import Container

LEVELS = {"ask-everything": "Careful", "ask-risky": "Balanced", "trusted": "Trusted", "autopilot": "Autopilot"}
LEVEL_HELP = {
    "ask-everything": "asks before anything except reading",
    "ask-risky": "asks before changing or sending anything",
    "trusted": "doesn't ask about undoable work; asks before sending",
    "autopilot": "also sends messages without asking",
}
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


def stop_running(c: Container, by: str) -> bool:
    """Cancel the task Delta is working on now (from any channel). False if nothing is running."""
    task = c.running
    if task is None or task.done():
        return False
    c.audit.record("task.cancel_requested", by=by)
    task.cancel()
    return True
