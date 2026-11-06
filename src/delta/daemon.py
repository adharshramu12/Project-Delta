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
import sys
import webbrowser
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from delta.config import Settings, load_settings
from delta.system.logging import log, setup_logging

if TYPE_CHECKING:  # the double-click path must start fast: the agent loads only in `run`
    from delta.bootstrap import Container

EDGE_PATHS = [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
              r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"]


# The dashboard is a simple page: no GPU, no background mode (Edge otherwise keeps ~10 processes and
# ~900 MB alive after the window is closed), no extensions, sync or update checks, few renderers.
LEAN_WINDOW_FLAGS = ["--no-first-run", "--no-default-browser-check", "--disable-background-mode",
                     "--disable-gpu", "--disable-extensions", "--disable-sync", "--disable-component-update",
                     "--renderer-process-limit=2", "--disable-features=Translate,msEdgeStartupBoost"]
REAP_EVERY_S = 60  # how often the service looks for a closed window's leftover processes
ALREADY_RUNNING = "Delta is already running"  # what `delta run` prints when another copy won the race


def window_processes(data_dir: Path) -> list[object]:
    """Edge processes of Delta's own app window (its private profile), never the user's browser."""
    import psutil

    profile = str(data_dir / "app-window").lower()
    found: list[object] = []
    for p in psutil.process_iter(["name", "cmdline"]):
        with contextlib.suppress(psutil.Error):
            if (p.info["name"] or "").lower() == "msedge.exe" and profile in " ".join(p.info["cmdline"] or []).lower():
                found.append(p)
    return found


def close_window_processes(data_dir: Path) -> int:
    import psutil

    procs = window_processes(data_dir)
    for p in procs:
        with contextlib.suppress(psutil.Error):
            p.terminate()  # type: ignore[attr-defined]
    return len(procs)


async def _reap_closed_window(data_dir: Path) -> None:
    """X closes only the window; the service (and Telegram) keeps running. Edge sometimes keeps the
    closed window's processes alive anyway, so free them once no Delta window is on screen."""
    while True:
        await asyncio.sleep(REAP_EVERY_S)
        with contextlib.suppress(Exception):
            if not _find_app_window() and (n := await asyncio.to_thread(close_window_processes, data_dir)):
                log.info("window.leftovers_closed", processes=n)


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
    click (ours is launched hidden by pythonw), unless a key event came from it: a lone Alt
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


def _open_app_window(settings: Settings, url: str) -> bool:
    """An Edge --app window on `url` with Delta's own profile. False when Edge is not installed."""
    edge = shutil.which("msedge") or next(
        (p for p in (os.path.expandvars(x) for x in EDGE_PATHS) if Path(p).exists()), None)
    if not edge:
        return False
    profile = settings.data_dir / "app-window"
    subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1180,800",
                      *LEAN_WINDOW_FLAGS],
                     close_fds=True, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return True


# Shown the instant the user double-clicks, while the service starts (a few seconds, longer on a busy
# PC). With nothing on screen people click again; now they see Delta starting. It switches itself to
# the dashboard once the service answers. No token in this file: it arrives in the URL's #fragment.
SPLASH = """<!doctype html><meta charset="utf-8"><title>Delta</title>
<style>body{margin:0;height:100vh;display:grid;place-items:center;background:#1f1e1d;color:#eceae6;
font:16px system-ui,sans-serif}.d{width:10px;height:10px;border-radius:50%;background:#d97757;display:inline-block;
margin-right:10px;animation:p .8s infinite alternate}@keyframes p{to{opacity:.25}}#s{color:#9b968f;font-size:13px;
margin-top:10px}</style>
<div><span class="d"></span>Starting Delta…<div id="s">This takes a few seconds.</div></div>
<script>
const q = new URLSearchParams(location.search), target = q.get("target"), t0 = Date.now();
async function poll() {
  try { await fetch(target, {mode: "no-cors", cache: "no-store"}); location.replace(target + location.hash); }
  catch (e) {
    if (Date.now() - t0 > 90000) document.getElementById("s").textContent =
      "Delta is taking unusually long. Details: " + q.get("log");
    setTimeout(poll, 400);
  }
}
poll();
</script>"""


def _show_splash(settings: Settings, token: str, log_file: Path) -> bool:
    from urllib.parse import quote

    page = settings.data_dir / "app-window-splash.html"
    with contextlib.suppress(OSError):
        page.write_text(SPLASH, encoding="utf-8")
        target = f"http://{settings.api.host}:{settings.api.port}/"
        return _open_app_window(settings, f"{page.as_uri()}?target={quote(target, safe='')}"
                                          f"&log={quote(str(log_file), safe='')}#token={token}")
    return False


@contextlib.contextmanager
def _launch_lock(data_dir: Path) -> Iterator[bool]:
    """True for the one launcher that starts the service; False for clicks made while it starts."""
    path = data_dir / "launch.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            yield True
    finally:
        os.close(fd)


def open_dashboard(settings: Settings, token: str) -> None:
    """Open the dashboard as its own app window (Edge --app with a private profile, so it gets its
    own taskbar button and never mixes with the user's browser tabs), else a normal browser tab.
    The window always ends up in front: an already-open one is raised instead of opening a second."""
    import time

    if hwnd := _find_app_window():
        _bring_to_front(hwnd)
        return
    url = f"http://{settings.api.host}:{settings.api.port}/#token={token}"
    if not _open_app_window(settings, url):
        webbrowser.open(url)
        return
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
    from delta.system.secrets import get_secret

    token = get_secret("telegram_bot_token")
    if not token:
        raise RuntimeError("no bot token: run `delta secrets set telegram_bot_token`")
    from delta.channels.telegram import TelegramChannel

    # An empty allow-list is fine: nobody can use the bot until they pair with a code from the dashboard.
    ch = TelegramChannel(c, token, cfg.allowed_user_ids)
    ch.on_pair = lambda ids: save_telegram_users(c, ids)
    return ch


def save_telegram_users(c: Container, ids: list[str]) -> None:
    from delta.config import ChannelConfig, write_user_settings

    write_user_settings(c.settings.data_dir, {"channels": {"telegram": {"enabled": True, "allowed_user_ids": ids}}})
    c.settings.channels["telegram"] = ChannelConfig(enabled=True, allowed_user_ids=list(ids))


async def start_channel(c: Container, name: str) -> object:
    """Start one channel now (e.g. Telegram right after its token is saved): no restart needed."""
    if name in c.live_channels:
        return c.live_channels[name]
    factory, _ = CHANNELS[name]
    channel = factory(c)
    try:
        await asyncio.wait_for(channel.start(), 30)  # type: ignore[attr-defined]
    except BaseException:
        await _stop_quietly(channel)
        raise
    c.lifecycle.on_shutdown(name, channel.stop)  # type: ignore[attr-defined]
    c.live_channels[name] = channel
    return channel


def _make_slack(c: Container) -> object:
    cfg = c.settings.channels["slack"]
    from delta.system.secrets import get_secret

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
        channel = None
        try:
            channel = factory(c)
            await asyncio.wait_for(channel.start(), 30)  # type: ignore[attr-defined]
        except ImportError:
            c.warn(f"{name.title()} is enabled but not installed. Run: uv sync --extra {extra}")
            continue
        except Exception as e:
            # Usually the network to the service is slow for a moment: keep trying in the background
            # instead of leaving the phone unanswered until the next restart.
            await _stop_quietly(channel)  # a half-started bot would fight the retry for its chat
            c.warn(f"{name.title()} could not start: {e or type(e).__name__}. Retrying in the background.")
            retry = asyncio.create_task(_retry_channel(c, name), name=f"retry-{name}")
            c.lifecycle.on_shutdown(f"retry-{name}", _cancel_task(retry))
            continue
        c.lifecycle.on_shutdown(name, channel.stop)  # type: ignore[attr-defined]
        c.live_channels[name] = channel
        ok.append(channel)
    return ok


async def _stop_quietly(channel: object) -> None:
    if channel is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(channel.stop(), 10)  # type: ignore[attr-defined]


RETRY_DELAYS_S = (10, 30, 60)  # then every 60 s


async def _retry_channel(c: Container, name: str) -> None:
    for attempt in range(10_000):
        await asyncio.sleep(RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)])
        try:
            await start_channel(c, name)
        except Exception as e:
            log.info("channel.retry_failed", channel=name, attempt=attempt + 1, error=str(e) or type(e).__name__)
            continue
        c.warnings[:] = [w for w in c.warnings if not w.startswith(f"{name.title()} could not start")]
        log.info("channel.connected_after_retry", channel=name, attempts=attempt + 1)
        return


def _cancel_task(task: asyncio.Task[None]) -> Callable[[], Awaitable[None]]:
    async def cancel() -> None:
        task.cancel()

    return cancel


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
    from concurrent.futures import ThreadPoolExecutor

    # asyncio.to_thread would otherwise grow to cpu+4 idle threads; 4 is plenty for Delta's blocking calls
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(4, thread_name_prefix="delta-io"))
    token = load_token(settings.data_dir)
    if open_ui and _port_open(settings.api.host, settings.api.port):
        open_dashboard(settings, token)  # already running: a second double-click just shows the window
        return 0
    from delta.bootstrap import build
    from delta.system.lifecycle import AlreadyRunningError

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
    if "telegram" in c.live_channels and c.engine is not None:  # tasks a crash cut off: ask, never auto-resume
        from delta.channels.telegram_tasks import announce_interrupted

        with contextlib.suppress(Exception):
            await announce_interrupted(c)
    if c.scheduler is not None:  # scheduled tasks: after the channels, so results can reach the phone
        await c.scheduler.start()
        c.lifecycle.on_shutdown("scheduler", c.scheduler.stop)
    if sys.platform == "win32" and (opener := c.registry.get("open_app")) is not None:
        with contextlib.suppress(Exception):  # the first "open X" then needs no PowerShell (background, cheap)
            opener.index.warm()  # type: ignore[attr-defined]
    log.info("delta.running", api=f"{settings.api.host}:{settings.api.port}",
             channels=[getattr(x, "name", "") for x in channels])
    print(f"Delta is running → http://{settings.api.host}:{settings.api.port}  (Ctrl+C or dashboard Shutdown to stop)")
    if open_ui and await _wait_for_port(settings.api.host, settings.api.port):
        await asyncio.to_thread(open_dashboard, settings, token)  # it waits for the page; keep serving it

    reaper = asyncio.create_task(_reap_closed_window(settings.data_dir), name="window-reaper")
    stop = asyncio.create_task(c.lifecycle.stopping.wait())
    try:
        await asyncio.wait({serve, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serve, 5)
        stop.cancel()
        reaper.cancel()
        await c.stop()
        close_window_processes(settings.data_dir)  # Shutdown means everything, the window too
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
    with _launch_lock(settings.data_dir) as first:
        if not first:  # clicked again while Delta starts: show what is there, never start a second copy
            if hwnd := _find_app_window():
                _bring_to_front(hwnd)
                return 0
            return 0 if _wait_then_open(settings, token, None, console) else 1
        splash = _show_splash(settings, token, console)
        if splash and (hwnd := _wait_for_window(10)):
            _bring_to_front(hwnd)
        python = Path(sys.executable).with_name("python.exe")
        if not python.exists():
            python = Path(sys.executable)
        with contextlib.suppress(OSError):
            if console.stat().st_size > 1_000_000:
                console.replace(console.with_suffix(".log.1"))
        start = console.stat().st_size if console.exists() else 0
        with open(console, "a", encoding="utf-8") as out:  # never wipe what a running copy is writing
            out.write(f"\n--- launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            out.flush()
            proc = subprocess.Popen([str(python), "-m", "delta", "run"], stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, cwd=str(Path.home()),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return 0 if _wait_then_open(settings, token, proc, console, start) else 1


def _wait_for_window(seconds: float) -> int:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if hwnd := _find_app_window():
            return hwnd
        time.sleep(0.2)
    return 0


def _wait_then_open(settings: Settings, token: str, proc: subprocess.Popen[bytes] | None, console: Path,
                    start: int = 0) -> bool:
    """Wait for the service to answer, then show the dashboard. A service that exits because Delta is
    already running (another click won the race) is not an error: keep waiting for that one."""
    import time

    host, port = settings.api.host, settings.api.port
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if _port_open(host, port, 0.2):
            open_dashboard(settings, token)
            return True
        if proc is not None and proc.poll() is not None:
            with open(console, "rb") as f:
                f.seek(start)
                ours = f.read().decode("utf-8", "replace")
            if "AlreadyRunningError" not in ours and ALREADY_RUNNING not in ours:
                tail = ours.strip().splitlines()[-8:]
                alert("Delta could not start.\n\n" + "\n".join(tail) + f"\n\nFull log: {console}")
                return False
            proc = None  # another copy is starting it: wait for that one
        time.sleep(0.2)
    alert(f"Delta is taking too long to start.\n\nDetails: {console}")
    return False
