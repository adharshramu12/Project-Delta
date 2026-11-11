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
import time
import webbrowser
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
                     "--renderer-process-limit=2", "--disable-features=Translate,msEdgeStartupBoost",
                     "--disk-cache-size=33554432", "--disable-breakpad"]  # 32 MB cache cap, no crash dumps
REAP_EVERY_S = 60  # how often the service looks for a closed window's leftover processes
ALREADY_RUNNING = "Delta is already running"  # what `delta run` prints when another copy won the race


class PortTakenError(RuntimeError):
    """Another program (not Delta) listens on Delta's dashboard port, so the dashboard cannot start there."""


def port_taken_message(settings: Settings) -> str:
    host, port = settings.api.host, settings.api.port
    return (f"Another program is using Delta's port {host}:{port}, so Delta cannot open its dashboard.\n"
            f"Close that program, or give Delta another port: add  api: {{port: {port + 1}}}  to "
            f"{settings.data_dir / 'settings.yaml'}, then open Delta again.")


def _delta_on_port(settings: Settings) -> bool:
    """Something answers on the dashboard port AND it is Delta (its delta.pid names a live Delta process).
    Only a port check here opened the dashboard on another program's page (a dev server, another app on 7878)
    and never started Delta at all."""
    if not _port_open(settings.api.host, settings.api.port):
        return False
    from delta.__main__ import running_delta

    return running_delta(settings.data_dir) is not None


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
# PC). With nothing on screen people click again; now they see Delta starting: Tri draws itself, lands with a
# small bounce, opens its eyes and says what it is doing, in the PC's light or dark colours. It switches itself
# to the dashboard once the service answers (after at least a second on screen, so it never just flashes), with a
# short fade. No token in this file: it arrives in the URL's #fragment. Still with Windows' reduced motion.
SPLASH = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Delta</title>
<meta name="color-scheme" content="dark light">
<style>
:root{--bg:#1f1e1d;--fg:#eceae6;--mut:#9b968f;--o:#E07A47;--od:#9C4A22}
@media (prefers-color-scheme:light){:root{--bg:#faf9f7;--fg:#1f1e1d;--mut:#77726b}}
body{margin:0;height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:16px "Segoe UI",system-ui,sans-serif;overflow:hidden}
main{display:grid;justify-items:center;transition:opacity .28s ease,transform .28s ease}
main.go{opacity:0;transform:scale(1.06)}
svg{width:132px;height:128px;overflow:visible}
.tri{transform-origin:60px 100px;animation:land .9s cubic-bezier(.2,1.5,.4,1) both,bob 3s ease-in-out 1.3s infinite}
.shape{fill:var(--o);stroke:var(--od);stroke-width:3;stroke-linejoin:round;stroke-dasharray:312;
stroke-dashoffset:312;fill-opacity:0;animation:draw .7s ease-out forwards,fill .35s .5s forwards}
.face{opacity:0;animation:show .25s .85s forwards}
.eyes{transform-origin:60px 67px;animation:blink 4.2s 1.8s infinite}
.shadow{fill:#0003;transform-origin:60px 110px;animation:shadow .9s both}
h1{font-size:22px;font-weight:600;letter-spacing:.2px;margin:14px 0 4px;opacity:0;animation:rise .5s 1s forwards}
#s{color:var(--mut);font-size:13px;min-height:18px;max-width:80vw;text-align:center;opacity:0;
animation:rise .5s 1.15s forwards;transition:opacity .2s}
#s.swap{opacity:0!important}
.bar{width:120px;height:3px;border-radius:3px;background:#8883;overflow:hidden;margin-top:16px;opacity:0;
animation:rise .5s 1.3s forwards}
.bar i{display:block;width:40%;height:100%;border-radius:3px;background:var(--o);
animation:slide 1.3s ease-in-out infinite}
@keyframes draw{to{stroke-dashoffset:0}}
@keyframes fill{to{fill-opacity:1}}
@keyframes show{to{opacity:1}}
@keyframes land{0%{transform:translateY(-26px) scale(.86)}60%{transform:translateY(0) scale(1.04,.94)}
100%{transform:none}}
@keyframes bob{50%{transform:translateY(-4px)}}
@keyframes blink{0%,93%,100%{transform:scaleY(1)}96%{transform:scaleY(.1)}}
@keyframes shadow{0%{transform:scaleX(.5);opacity:0}100%{transform:none;opacity:1}}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
@media (prefers-reduced-motion:reduce){*{animation-duration:.01s!important;animation-delay:0s!important;
animation-iteration-count:1!important;transition:none!important}}
</style>
<main id="m"><svg viewBox="0 0 120 116" aria-hidden="true">
<ellipse class="shadow" cx="60" cy="110" rx="40" ry="4"/>
<g class="tri"><path class="shape" d="M60 10 L112 100 L8 100 Z"/>
<g class="face"><path d="M40 52 L54 55 M80 52 L66 55" stroke="#3A2418" stroke-width="3" stroke-linecap="round"/>
<g class="eyes"><ellipse cx="49" cy="66" rx="8" ry="10" fill="#fff"/>
<ellipse cx="71" cy="66" rx="8" ry="10" fill="#fff"/>
<circle cx="51" cy="69" r="4.5" fill="#2A2420"/><circle cx="73" cy="69" r="4.5" fill="#2A2420"/></g>
<path d="M51 84 Q60 92 69 84" fill="none" stroke="#2A2420" stroke-width="3" stroke-linecap="round"/>
<ellipse cx="38" cy="82" rx="5" ry="3" fill="#F2A7A7"/><ellipse cx="82" cy="82" rx="5" ry="3" fill="#F2A7A7"/>
</g></g></svg>
<h1>Delta</h1><div id="s" role="status">Waking up…</div><div class="bar"><i></i></div></main>
<script>
const q = new URLSearchParams(location.search), target = q.get("target"), t0 = Date.now();
const s = document.getElementById("s"), m = document.getElementById("m");
const lines = ["Waking up…", "Stretching my corners…", "Finding your tools…", "Almost there…"];
let n = 0, late = false;
const talk = setInterval(() => {
  if (late) return;
  s.classList.add("swap");
  setTimeout(() => { s.textContent = lines[n = Math.min(n + 1, lines.length - 1)]; s.classList.remove("swap"); }, 200);
}, 2200);
function open() {
  clearInterval(talk);
  m.classList.add("go");
  setTimeout(() => location.replace(target + location.hash), 280);
}
async function poll() {
  try {
    await fetch(target, {mode: "no-cors", cache: "no-store"});
    setTimeout(open, Math.max(0, 1100 - (Date.now() - t0)));
  } catch (e) {
    if (Date.now() - t0 > 90000) {
      late = true;
      s.textContent = "Delta is taking unusually long. Details: " + q.get("log");
    }
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
            # Usually the network to the service is slow for a moment (just after boot or sleep): keep trying in
            # the background instead of leaving the phone unanswered until the next restart. The dashboard says so
            # only when it keeps failing for START_OUTAGE_S (it showed "Timed out" for every slow start before).
            await _stop_quietly(channel)  # a half-started bot would fight the retry for its chat
            log.info("channel.start_failed", channel=name, error=str(e) or type(e).__name__)
            retry = asyncio.create_task(_retry_channel(c, name, e), name=f"retry-{name}")
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
START_OUTAGE_S = 120.0  # a channel that still cannot start after this long gets a line on the dashboard


def _start_problem(name: str, e: BaseException) -> str:
    """Plain words for a channel that keeps failing to start (never a bare "Timed out")."""
    why = str(e) or type(e).__name__
    if name == "telegram":
        with contextlib.suppress(Exception):
            from delta.channels.telegram_net import describe

            low = f"{type(e).__name__} {why}".lower()
            if any(w in low for w in ("timed out", "timeout", "network", "connect", "proxy", "ssl", "unreachable")) \
                    or "telegram" in type(e).__module__:
                why = describe(e)
    return f"{name.title()} could not start: {why} Retrying in the background."


async def _retry_channel(c: Container, name: str, first: BaseException | None = None) -> None:
    began = time.monotonic()
    shown = ""
    last: BaseException | None = first
    for attempt in range(10_000):
        if last is not None and not shown and time.monotonic() - began >= START_OUTAGE_S:
            shown = _start_problem(name, last)
            c.warn(shown)
        await asyncio.sleep(RETRY_DELAYS_S[min(attempt, len(RETRY_DELAYS_S) - 1)])
        try:
            await start_channel(c, name)
        except Exception as e:
            last = e
            log.info("channel.retry_failed", channel=name, attempt=attempt + 1, error=str(e) or type(e).__name__)
            if not shown and time.monotonic() - began >= START_OUTAGE_S:
                shown = _start_problem(name, e)
                c.warn(shown)
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
        from delta.system.crashguard import client_went_away

        if client_went_away(ctx.get("exception")):
            return
        lp.default_exception_handler(ctx)

    loop.set_exception_handler(handler)


_BACKGROUND: set[asyncio.Task[None]] = set()  # one-off jobs kept referenced until they finish


async def _tidy_storage(data_dir: Path) -> None:
    """Trim the browser profiles' leftovers (delta.system.storage) in the background, never slowing the start."""
    from delta.system.storage import tidy

    with contextlib.suppress(Exception):
        await asyncio.sleep(20)  # after the window has opened and the first request is in
        await asyncio.to_thread(tidy, data_dir)


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
    if open_ui and _delta_on_port(settings):
        open_dashboard(settings, token)  # already running: a second double-click just shows the window
        return 0
    from delta.bootstrap import build
    from delta.system.lifecycle import AlreadyRunningError

    c = build(settings)
    from delta.system.crashguard import guard_of

    guard = guard_of(c)  # an unexpected error in a thread or background task: logged and shown, Delta carries on
    guard.install_threads()
    guard.install_loop(asyncio.get_running_loop())
    try:
        await c.start()
    except AlreadyRunningError:
        if open_ui:  # second click on the shortcut just brings the dashboard up
            open_dashboard(settings, token)
            return 0
        raise
    if _port_open(settings.api.host, settings.api.port):  # this is the only Delta, so the port is someone else's
        await c.stop()
        raise PortTakenError(port_taken_message(settings))

    server = uvicorn.Server(uvicorn.Config(create_app(c, token), host=settings.api.host, port=settings.api.port,
                                           log_level="warning", access_log=False, lifespan="off",
                                           log_config=None))
    serve = asyncio.create_task(server.serve(), name="api")
    tidying = asyncio.create_task(_tidy_storage(settings.data_dir), name="storage-tidy")
    _BACKGROUND.add(tidying)
    tidying.add_done_callback(_BACKGROUND.discard)
    channels = await _start_channels(c)
    if "telegram" in c.live_channels and c.engine is not None:  # tasks a crash cut off: ask, never auto-resume
        from delta.channels.telegram_tasks import announce_interrupted

        with contextlib.suppress(Exception):
            await announce_interrupted(c)
    if c.scheduler is not None:  # scheduled tasks: after the channels, so results can reach the phone
        await c.scheduler.start()
        c.lifecycle.on_shutdown("scheduler", c.scheduler.stop)
    if c.updates is not None:  # newer-version check: soon after start, then every 12 hours; never installs
        from delta.updates.service import notify_telegram

        async def tell_phone(view: dict[str, Any]) -> bool:
            return await notify_telegram(c, view)

        c.updates.notify = tell_phone
        c.updates.start()
        c.lifecycle.on_shutdown("updates", c.updates.stop)
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


def terminal_message(pid: int) -> str:
    return (f"Delta is already running in a terminal window (the 'delta cli' command, process {pid}).\n"
            "Only one Delta can run at a time, and the terminal one has no dashboard.\n\n"
            "To use the dashboard, close Delta in that terminal (type /quit or press Ctrl+C), then open Delta "
            "again. Or keep using it in the terminal.")


def _tell(settings: Settings, message: str) -> None:
    """A dialog (no console when started from the icon), and the same text in logs\\launcher.log."""
    import time

    with contextlib.suppress(OSError):
        logs = settings.data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "launcher.log", "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n{message}\n")
    alert(message)


def launch_app(background: bool = False) -> int:
    """Double-click entry (run by pythonw from the shortcut). Starts the service as a hidden console
    process, so it and every helper it spawns get real stdin/stdout/stderr handles (pythonw has none,
    which breaks logging setup and makes child processes hang), then opens the app window.

    background (`delta app --background`, what "Start Delta when I sign in to Windows" runs): start the service
    if it is not running, and open no window."""
    import sys
    import time

    settings = load_settings()
    from delta.__main__ import terminal_delta
    from delta.api.server import load_token

    token = load_token(settings.data_dir)
    if _delta_on_port(settings):
        if not background:
            open_dashboard(settings, token)
        return 0
    if _port_open(settings.api.host, settings.api.port):
        _tell(settings, port_taken_message(settings))
        return 1
    if (pid := terminal_delta(settings.data_dir)) is not None:
        # `delta cli` holds the one-Delta lock and serves no dashboard: starting the service would only wait for a
        # page that never comes ("taking too long" after 2 minutes). Say so now.
        _tell(settings, terminal_message(pid))
        return 1
    logs = settings.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    console = logs / "service.log"
    with _launch_lock(settings.data_dir) as first:
        if not first:  # clicked again while Delta starts: show what is there, never start a second copy
            if background:
                return 0
            if hwnd := _find_app_window():
                _bring_to_front(hwnd)
                return 0
            return 0 if _wait_then_open(settings, token, None, console) else 1
        if not background:
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
            out.write(f"\n--- launch {time.strftime('%Y-%m-%d %H:%M:%S')}{' (background)' if background else ''} ---\n")
            out.flush()
            proc = subprocess.Popen([str(python), "-m", "delta", "run"], stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT, cwd=str(Path.home()),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return 0 if _wait_then_open(settings, token, proc, console, start, show=not background) else 1


def _wait_for_window(seconds: float) -> int:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if hwnd := _find_app_window():
            return hwnd
        time.sleep(0.2)
    return 0


def _wait_then_open(settings: Settings, token: str, proc: subprocess.Popen[bytes] | None, console: Path,
                    start: int = 0, show: bool = True) -> bool:
    """Wait for the service to answer, then show the dashboard (show=False: only wait). A service that exits because
    Delta is already running (another click won the race) is not an error: keep waiting for that one, unless the
    one running is Delta in a terminal (`delta cli`), which never opens a dashboard: say so at once."""
    import time

    from delta.__main__ import terminal_delta

    host, port = settings.api.host, settings.api.port
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if _port_open(host, port, 0.2):
            if show:
                open_dashboard(settings, token)
            return True
        if proc is not None and proc.poll() is not None:
            with open(console, "rb") as f:
                f.seek(start)
                ours = f.read().decode("utf-8", "replace")
            if "AlreadyRunningError" not in ours and ALREADY_RUNNING not in ours:
                tail = ours.strip().splitlines()[-8:]
                _tell(settings, "Delta could not start.\n\n" + "\n".join(tail) + f"\n\nFull log: {console}")
                return False
            if (pid := terminal_delta(settings.data_dir)) is not None:
                _tell(settings, terminal_message(pid))
                return False
            proc = None  # another copy is starting it: wait for that one
        time.sleep(0.2)
    _tell(settings, f"Delta is taking too long to start.\n\nDetails: {console}")
    return False
