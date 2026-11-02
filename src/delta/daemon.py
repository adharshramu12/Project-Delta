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


def _is_delta_title(title: str) -> bool:
    # the dashboard sets "Delta", "<chat> · Delta" or "(2) Approval needed · Delta"
    return title == "Delta" or title.endswith(" · Delta")


def _find_app_window() -> int:
    """Handle of the open Delta app window (an Edge --app window), or 0."""
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def check(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value != "Chrome_WidgetWin_1":
            return True
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if _is_delta_title(title.value):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(check, 0)
    return found[0] if found else 0


def _bring_to_front(hwnd: int) -> None:
    """Show the window on top. Windows refuses SetForegroundWindow to a process the user did not just
    click (ours is launched hidden via wscript), unless a key event came from it: a lone Alt
    press/release unlocks it without typing anything."""
    import ctypes

    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    alt, keyup = 0x12, 0x0002
    user32.keybd_event(alt, 0, 0, 0)
    user32.keybd_event(alt, 0, keyup, 0)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)


def open_dashboard(settings: Settings, token: str) -> None:
    """Open the dashboard as its own app window (Edge --app with a private profile, so it gets its
    own taskbar button and never mixes with the user's browser tabs), else a normal browser tab.
    The window always ends up in front: an already-open one is raised instead of opening a second."""
    import time

    if hwnd := _find_app_window():
        _bring_to_front(hwnd)
        return
    url = f"http://{settings.api.host}:{settings.api.port}/#token={token}"
    edge = shutil.which("msedge") or next(
        (p for p in (os.path.expandvars(x) for x in EDGE_PATHS) if Path(p).exists()), None)
    if not edge:
        webbrowser.open(url)
        return
    profile = settings.data_dir / "app-window"
    subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1180,800",
                      "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"],
                     close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:  # Edge opens it behind the current app; pull it forward
        if hwnd := _find_app_window():
            _bring_to_front(hwnd)
            return
        time.sleep(0.2)


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    import socket

    with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=timeout):
        return True
    return False


async def _wait_for_port(host: str, port: int, seconds: float = 15) -> bool:
    for _ in range(int(seconds / 0.1)):
        if await asyncio.to_thread(_port_open, host, port, 0.2):
            return True
        await asyncio.sleep(0.1)
    return False


def alert(message: str) -> None:
    """Started by double-click there is no console, so problems must show as a dialog."""
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "Delta", 0x10)


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
    if open_ui and _port_open(settings.api.host, settings.api.port):
        open_dashboard(settings, token)  # already running: a second double-click just shows the window
        return 0
    c = build(settings)
    try:
        await c.start()
    except AlreadyRunningError:
        if open_ui:  # second click on the shortcut just brings the dashboard up
            open_dashboard(settings, token)
            return 0
        raise

    server = uvicorn.Server(uvicorn.Config(create_app(c, token), host=settings.api.host, port=settings.api.port,
                                           log_level="warning", access_log=False, lifespan="off",
                                           log_config=None))
    serve = asyncio.create_task(server.serve(), name="api")
    channels = await _start_channels(c)
    log.info("delta.running", api=f"{settings.api.host}:{settings.api.port}",
             channels=[getattr(x, "name", "") for x in channels])
    print(f"Delta is running → http://{settings.api.host}:{settings.api.port}  (Ctrl+C or dashboard Shutdown to stop)")
    if open_ui and await _wait_for_port(settings.api.host, settings.api.port):
        await asyncio.to_thread(open_dashboard, settings, token)  # it waits for the page; keep serving it

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


def launch_app() -> int:
    """Double-click entry (run by pythonw from the shortcut). Starts the service as a hidden console
    process, so it and every helper it spawns get real stdin/stdout/stderr handles (pythonw has none,
    which breaks logging setup and makes child processes hang), then opens the app window."""
    import sys
    import time

    settings = load_settings()
    from delta.api.server import load_token

    token = load_token(settings.data_dir)
    host, port = settings.api.host, settings.api.port
    if _port_open(host, port):
        open_dashboard(settings, token)
        return 0
    logs = settings.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    console = logs / "service.log"
    python = Path(sys.executable).with_name("python.exe")
    if not python.exists():
        python = Path(sys.executable)
    with open(console, "w", encoding="utf-8") as out:
        proc = subprocess.Popen([str(python), "-m", "delta", "run"], stdin=subprocess.DEVNULL, stdout=out,
                                stderr=subprocess.STDOUT, cwd=str(Path.home()),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if _port_open(host, port, 0.2):
            open_dashboard(settings, token)
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    tail = console.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-8:]
    alert("Delta could not start.\n\n" + "\n".join(tail) + f"\n\nFull log: {console}")
    return 1
