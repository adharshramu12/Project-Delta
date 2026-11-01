"""`delta run`: the background service. Starts the API/dashboard and enabled chat channels, and
stops everything when asked (dashboard Shutdown button, Ctrl+C, or Windows logoff).

Heavy channel libraries are imported only for channels that are enabled.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import webbrowser
from pathlib import Path

from delta.bootstrap import Container, build
from delta.config import Settings, load_settings
from delta.system.lifecycle import AlreadyRunningError
from delta.system.logging import log, setup_logging
from delta.system.secrets import get_secret

EDGE_PATHS = [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"]


def open_dashboard(settings: Settings, token: str) -> None:
    """Open the dashboard as a chromeless app window (Edge --app) or a normal browser tab."""
    url = f"http://{settings.api.host}:{settings.api.port}/#token={token}"
    edge = shutil.which("msedge") or next(
        (p for p in (os.path.expandvars(x) for x in EDGE_PATHS) if Path(p).exists()), None)
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--window-size=1100,760"], close_fds=True)
    else:
        webbrowser.open(url)


def _make_telegram(c: Container) -> object:
    cfg = c.settings.channels["telegram"]
    token = get_secret("telegram_bot_token")
    if not token:
        raise RuntimeError("no bot token: run `delta secrets set telegram_bot_token`")
    if not cfg.allowed_user_ids:
        raise RuntimeError("allowed_user_ids is empty in config/delta.yaml, so nobody could use the bot")
    from delta.channels.telegram import TelegramChannel

    return TelegramChannel(c, token, cfg.allowed_user_ids)


def _make_slack(c: Container) -> object:
    cfg = c.settings.channels["slack"]
    bot, app = get_secret("slack_bot_token"), get_secret("slack_app_token")
    if not (bot and app):
        raise RuntimeError("missing tokens: run `delta secrets set slack_bot_token` and `slack_app_token`")
    if not cfg.allowed_user_ids:
        raise RuntimeError("allowed_user_ids is empty in config/delta.yaml, so nobody could use the bot")
    from delta.channels.slack import SlackChannel

    return SlackChannel(c, bot, app, cfg.allowed_user_ids)


CHANNELS = {"telegram": (_make_telegram, "channels"), "slack": (_make_slack, "channels")}


async def _start_channels(c: Container) -> list[object]:
    """Start enabled chat channels. A broken channel (missing library, bad token, network down)
    is reported to the user and skipped; it never stops Delta from starting."""
    ok = []
    for name, (factory, extra) in CHANNELS.items():
        cfg = c.settings.channels.get(name)
        if not cfg or not cfg.enabled:
            continue
        try:
            channel = factory(c)
            await asyncio.wait_for(channel.start(), 30)  # type: ignore[attr-defined]
        except ImportError:
            c.warn(f"{name.title()} is enabled but not installed. Run: uv sync --extra {extra}")
            continue
        except Exception as e:
            c.warn(f"{name.title()} could not start: {e}")
            continue
        c.lifecycle.on_shutdown(name, channel.stop)  # type: ignore[attr-defined]
        ok.append(channel)
    return ok


def _quiet_connection_resets() -> None:
    """Windows' Proactor loop reports every browser tab closing a socket as an error. Ignore those."""
    loop = asyncio.get_running_loop()

    def handler(lp: asyncio.AbstractEventLoop, ctx: dict) -> None:
        if isinstance(ctx.get("exception"), ConnectionResetError):
            return
        lp.default_exception_handler(ctx)

    loop.set_exception_handler(handler)


async def run(open_ui: bool = False) -> int:
    import uvicorn

    from delta.api.server import create_app, load_token

    settings = load_settings()
    setup_logging(settings.data_dir)
    _quiet_connection_resets()
    token = load_token(settings.data_dir)
    c = build(settings)
    try:
        await c.start()
    except AlreadyRunningError:
        if open_ui:  # second click on the shortcut just brings the dashboard up
            open_dashboard(settings, token)
            return 0
        raise

    server = uvicorn.Server(uvicorn.Config(create_app(c, token), host=settings.api.host, port=settings.api.port,
                                           log_level="warning", access_log=False, lifespan="off"))
    serve = asyncio.create_task(server.serve(), name="api")
    channels = await _start_channels(c)
    log.info("delta.running", api=f"{settings.api.host}:{settings.api.port}",
             channels=[getattr(x, "name", "") for x in channels])
    print(f"Delta is running → http://{settings.api.host}:{settings.api.port}  (Ctrl+C or dashboard Shutdown to stop)")
    if open_ui:
        await asyncio.sleep(0.8)
        open_dashboard(settings, token)

    stop = asyncio.create_task(c.lifecycle.stopping.wait())
    try:
        await asyncio.wait({serve, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve, 5)
        stop.cancel()
        await c.stop()
        print("Delta is fully stopped.")
    return 0
