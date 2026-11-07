"""Open applications, folders, files and URLs; list running apps.

Opening an app is fast because Delta does as little as possible each time:
- an already-open window of the app is brought to the front instead of starting a second copy (unless the
  model asks for a new window or passes a file to open);
- the installed apps are kept in an index (Start-menu shortcuts, the App Paths registry, and Store apps from
  Get-StartApps), in memory and in Delta's data folder, rebuilt only when one of those sources changed. The
  only PowerShell call (Get-StartApps) runs when Store apps were installed or removed, not on every start;
- apps are started straight through the Windows shell (ShellExecute: App Paths names, shortcuts and Store
  apps by their AppUserModelID), with no PowerShell or explorer.exe in between;
- Delta watches for the app's window from the first moment, a little more often at the start, and takes a
  single-copy app's existing window as soon as the app brings it forward.

Self-heal: if a way of starting the app fails (Windows refused it, or nothing ran), the next way is tried, at
most MAX_LAUNCH_TRIES in all, and each try is logged and reported in the result. A launch that started the
app but has not shown a window yet is waited for, never started twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ntpath
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
from pydantic import BaseModel, Field

from delta.config import AppEntry
from delta.system.logging import log
from delta.tools.base import Risk, Tool, ToolResult

MAX_LAUNCH_TRIES = 3
RECHECK_S = 30.0          # how often the index looks for changes in its sources (cheap: a few stats)
WINDOW_WAIT_S = 8.0
EXISTING_AFTER_S = 1.5    # a single-copy app's existing window counts after this (or at once if in front)
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "iexplore.exe"}
START_MENU_DIRS = (r"%ProgramData%\Microsoft\Windows\Start Menu\Programs",
                   r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")
APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
PACKAGES_KEY = (r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel"
                r"\Repository\Packages")
# Get-StartApps as UTF-8 JSON: app names in any language survive PowerShell 5.1's console code page.
START_APPS_PS = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                 "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")


# --- the installed-apps index -----------------------------------------------------------------------------

@dataclass(frozen=True)
class AppRecord:
    name: str           # as shown in the Start menu
    kind: str           # "lnk" (Start-menu shortcut), "exe" (App Paths), "uwp" (Get-StartApps AppID)
    target: str         # the shortcut's path, the exe's path, or the AppID


def _expand(path: str) -> str:
    return os.path.expandvars(path.strip().strip('"'))


def scan_shortcuts(dirs: tuple[str, ...] = START_MENU_DIRS) -> tuple[list[AppRecord], float]:
    """Start-menu shortcuts (name = file name) and the newest folder change time (the folder's time changes
    when a shortcut is added or removed in it)."""
    found: list[AppRecord] = []
    newest = 0.0
    for root in dirs:
        stack = [_expand(root)]
        while stack:
            folder = stack.pop()
            try:
                newest = max(newest, os.stat(folder).st_mtime)
                with os.scandir(folder) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.name.lower().endswith(".lnk"):
                            name = e.name[:-4]
                            if not name.lower().startswith("uninstall"):
                                found.append(AppRecord(name, "lnk", e.path))
            except OSError:
                continue
    return found, newest


def _key_time(root: Any, path: str) -> int:
    try:
        import winreg

        with winreg.OpenKey(root, path) as k:
            return int(winreg.QueryInfoKey(k)[2])  # last write time
    except OSError:
        return 0


def scan_app_paths() -> tuple[list[AppRecord], list[int]]:
    """App Paths registry entries (what Win+R knows by exe name), HKCU and HKLM, and their change times."""
    if sys.platform != "win32":
        return [], []
    import winreg

    found: list[AppRecord] = []
    stamps: list[int] = []
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        stamps.append(_key_time(root, APP_PATHS))
        try:
            key = winreg.OpenKey(root, APP_PATHS)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                if not sub.lower().endswith(".exe"):
                    continue
                with contextlib.suppress(OSError), winreg.OpenKey(key, sub) as k:
                    path = _expand(str(winreg.QueryValueEx(k, "")[0] or ""))
                    if path:
                        found.append(AppRecord(sub[:-4], "exe", path))
    return found, stamps


def scan_store_apps() -> list[dict[str, str]]:
    """Every Start-menu app with its AppID (Store apps like Calculator included), from Get-StartApps."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", START_APPS_PS],
                             capture_output=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = out.stdout.decode("utf-8-sig", errors="replace").strip() or "[]"
        data = json.loads(raw)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log.info("apps.start_apps_unavailable", error=str(e))  # PowerShell blocked or missing: shortcuts still work
        return []
    items = data if isinstance(data, list) else [data]
    return [{"Name": str(a["Name"]), "AppID": str(a["AppID"])} for a in items
            if isinstance(a, dict) and a.get("Name") and a.get("AppID")]


class AppIndex:
    """Installed apps, in memory and in `cache_dir/apps-index.json`, rebuilt only when a source changed:
    Start-menu shortcuts (folder times), App Paths (registry key times) and Store apps (the per-user package
    repository key time; without it, once a day)."""

    def __init__(self, cache_dir: Path | None = None,
                 store_scan: Callable[[], list[dict[str, str]]] = scan_store_apps) -> None:
        self.cache_dir = cache_dir
        self.store_scan = store_scan
        self._lock = threading.Lock()
        self._shortcuts: list[AppRecord] = []
        self._app_paths: list[AppRecord] = []
        self._store: list[dict[str, str]] | None = None
        self._stamp: dict[str, Any] = {}
        self._checked = 0.0
        self.store_scans = 0  # how many times Get-StartApps ran (tests and the benchmark read it)

    @property
    def file(self) -> Path | None:
        return self.cache_dir / "apps-index.json" if self.cache_dir else None

    def reset(self) -> None:
        with self._lock:
            self._shortcuts, self._app_paths, self._store, self._stamp, self._checked = [], [], None, {}, 0.0
            if self.file is not None:
                with contextlib.suppress(OSError):
                    self.file.unlink()

    @staticmethod
    def _store_stamp() -> Any:
        if sys.platform == "win32":
            import winreg

            stamp = _key_time(winreg.HKEY_CURRENT_USER, PACKAGES_KEY)
            if stamp:
                return stamp
        return int(time.time() // 86400)  # no change signal: look again once a day

    def _refresh_local(self) -> None:
        """Shortcuts and App Paths: fast (a folder walk and a registry read), so read on every check."""
        shortcuts, lnk_time = scan_shortcuts()
        app_paths, reg_times = scan_app_paths()
        self._shortcuts, self._app_paths = shortcuts, app_paths
        self._stamp["lnk"], self._stamp["app_paths"] = lnk_time, reg_times

    def _load_disk(self) -> None:
        if self._store is not None or self.file is None:
            return
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and isinstance(data.get("store"), list):
            self._store = [a for a in data["store"] if isinstance(a, dict) and a.get("Name") and a.get("AppID")]
            self._stamp["store"] = data.get("store_stamp")

    def _save_disk(self) -> None:
        if self.file is None or self._store is None:
            return
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"store": self._store, "store_stamp": self._stamp.get("store")}),
                           encoding="utf-8")
            os.replace(tmp, self.file)
        except OSError as e:
            log.info("apps.index_not_saved", error=str(e))

    def _check(self) -> None:
        now = time.monotonic()
        if self._checked and now - self._checked < RECHECK_S:
            return
        self._checked = now
        self._refresh_local()
        self._load_disk()

    def shortcuts(self) -> list[AppRecord]:
        with self._lock:
            self._check()
            return list(self._shortcuts)

    def app_paths(self) -> list[AppRecord]:
        with self._lock:
            self._check()
            return list(self._app_paths)

    def store(self) -> list[dict[str, str]]:
        """Get-StartApps' list; PowerShell runs only when nothing is cached or Store apps changed."""
        with self._lock:
            self._check()
            stamp = self._store_stamp()
            if self._store is None or self._stamp.get("store") != stamp:
                t0 = time.perf_counter()
                self._store = self.store_scan()
                self.store_scans += 1
                self._stamp["store"] = stamp
                log.info("apps.index_built", store_apps=len(self._store), seconds=round(time.perf_counter() - t0, 3))
                if self._store:
                    self._save_disk()
            return list(self._store)

    def store_cached(self) -> bool:
        return self._store is not None

    def warm(self) -> None:
        """Build the index in the background at start-up, so the first open_app does not wait for it."""
        def run() -> None:
            with contextlib.suppress(Exception):
                self.store()

        threading.Thread(target=run, name="delta-apps-index", daemon=True).start()


_default_index = AppIndex()


def reset_app_index(cache_dir: Path | None = None) -> None:
    """Forget every cached app list (tests and the benchmark)."""
    _default_index.reset()
    if cache_dir is not None:
        AppIndex(cache_dir).reset()


def start_apps() -> list[dict[str, str]]:
    """Apps in the Start menu ({"Name", "AppID"}), Store apps like Calculator included (cached, see AppIndex)."""
    return _default_index.store() if os.name == "nt" else []


def match_start_app(name: str, apps: list[dict[str, str]]) -> dict[str, str] | None:
    """Exact Start-menu name first, then one that starts with / contains it (shortest wins)."""
    key = name.strip().lower()
    for test in (lambda n: n == key, lambda n: n.startswith(key), lambda n: key in n):
        hits = [a for a in apps if test(a["Name"].lower())]
        if hits:
            return min(hits, key=lambda a: len(a["Name"]))
    return None


def match_record(name: str, records: list[AppRecord], exact_only: bool = False) -> AppRecord | None:
    key = name.strip().lower()
    tests: list[Callable[[str], bool]] = [lambda n: n == key]
    if not exact_only:
        tests += [lambda n: n.startswith(key), lambda n: key in n]
    for test in tests:
        hits = [r for r in records if test(r.name.lower())]
        if hits:
            return min(hits, key=lambda r: len(r.name))
    return None


# --- windows --------------------------------------------------------------------------------------------

def _windows() -> dict[int, str]:
    """Visible top-level windows with a title: {handle: title}."""
    return {h: t for h, (t, _pid) in _windows_with_pids().items()}


def _windows_with_pids() -> dict[int, tuple[str, int]]:
    """Visible top-level windows with a title: {handle: (title, process id)}."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes

    user32, found = ctypes.windll.user32, {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            if user32.GetWindowTextW(hwnd, buf, 256) and buf.value not in ("Program Manager",):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found[int(hwnd)] = (buf.value, int(pid.value))
        return True

    user32.EnumWindows(visit, 0)
    return found


def _foreground() -> int:
    if os.name != "nt":
        return 0
    import ctypes

    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def _bring_front(hwnd: int) -> None:
    import ctypes

    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.keybd_event(0x12, 0, 0, 0)  # a lone Alt lets a background process take the foreground
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)


def _process_names(pids: set[int]) -> dict[int, str]:
    names: dict[int, str] = {}
    for pid in pids:
        with contextlib.suppress(psutil.Error, OSError):
            names[pid] = psutil.Process(pid).name().lower()
    return names


def title_names_app(title: str, names: list[str]) -> bool:
    """The window title is the app's own: exactly its name, or 'document - App'."""
    t = title.strip().lower()
    for n in (x.strip().lower() for x in names if x and x.strip()):
        if t == n or any(t.endswith(sep + n) for sep in (" - ", " – ", " — ")):
            return True
    return False


def pick_open_window(windows: dict[int, tuple[str, int]], procs: dict[int, str], names: list[str],
                     exe: str = "") -> tuple[int, str] | None:
    """An open window of this app: its process is the app's exe, or its title is the app's (a web page in a
    browser named like the app does not count)."""
    exe = exe.lower()
    for hwnd, (title, pid) in windows.items():
        proc = procs.get(pid, "")
        if exe and proc == exe:
            return hwnd, title
        if proc not in BROWSERS and title_names_app(title, names):
            return hwnd, title
    return None


def find_open_window(names: list[str], exe: str = "") -> tuple[int, str] | None:
    windows = _windows_with_pids()
    return pick_open_window(windows, _process_names({pid for _, pid in windows.values()}), names, exe)


def wait_for_app_window(before: dict[int, str], hints: list[str], seconds: float = WINDOW_WAIT_S) -> str | None:
    """The window the launch opened (new since `before`), else an existing one whose title names the app
    (single-copy apps reuse theirs: taken at once when the app brought it to the front). Brought to the front;
    returns its title."""
    start = time.monotonic()
    deadline = start + seconds
    pause = 0.05
    while True:
        now = _windows()
        new = [(h, t) for h, t in now.items() if h not in before]
        named = [(h, t) for h, t in now.items() if any(x and x in t.lower() for x in hints)]
        front = _foreground()
        pick = None
        if new:
            pick = next(((h, t) for h, t in new if any(x and x in t.lower() for x in hints)), new[0])
        elif named:
            in_front = next(((h, t) for h, t in named if h == front), None)
            if in_front or time.monotonic() - start > EXISTING_AFTER_S:
                pick = in_front or named[0]
        if pick:
            _bring_front(pick[0])
            return pick[1]
        if time.monotonic() > deadline:
            return None
        time.sleep(pause)
        pause = min(0.25, pause * 1.5)  # often at first: most apps show a window within a second


# --- open_app ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Launch:
    how: str                      # said in the result and the log: "exe", "App Paths", "shortcut", "Start menu"
    run: Callable[[], None]
    exe: str = ""                 # process name to look for, when known


def _popen(cmd: list[str]) -> None:
    subprocess.Popen(cmd, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                     close_fds=True)


def _shell_open(target: str, argument: str | None = None) -> None:
    """ShellExecute: App Paths names, shortcuts and shell:AppsFolder\\<AppID> without a helper process."""
    if argument:
        os.startfile(target, arguments=argument)
    else:
        os.startfile(target)


def _apps_folder(app_id: str) -> None:
    try:
        _shell_open("shell:AppsFolder\\" + app_id)
    except OSError:
        _popen(["explorer.exe", "shell:AppsFolder\\" + app_id])  # what Delta always did


class OpenAppArgs(BaseModel):
    name: str = Field(description="App name or alias, e.g. 'antigravity', 'file explorer', 'vscode'")
    argument: str | None = Field(default=None, description="Optional file/folder to open with it")
    new_window: bool = Field(default=False, description="Start another copy even if the app is already open")


class OpenApp(Tool[OpenAppArgs]):
    name = "open_app"
    description = ("Launch a desktop application by its name as in the Start menu (e.g. 'Calculator', "
                   "'WhatsApp'), a friendly name from apps.yaml, or an executable. If it is already open, its "
                   "window is brought to the front instead (new_window=true starts another copy). Waits for its "
                   "window, brings it to the front and returns the window title to use with the window tools.")
    risk = Risk.ACT
    Args = OpenAppArgs

    def __init__(self, apps: dict[str, AppEntry], cache_dir: Path | None = None,
                 denied_apps: Callable[[], list[str]] | None = None) -> None:
        self.apps = apps
        self.index = AppIndex(cache_dir) if cache_dir is not None else _default_index
        # The user's blocked apps (boundaries.denied_apps), read at each call so a settings change applies at once.
        # The policy checks the name the agent ASKED for; a loose Start-menu match can open another app (asked
        # "registry", opened "Registry Editor"), so run() checks what it is about to open as well.
        self.denied_apps = denied_apps or (lambda: [])

    def blocked(self, names: list[str]) -> str | None:
        """The first of `names` (app names, shortcut names, exe names) that the user blocked, else None."""
        denied = {_app_key(d) for d in self.denied_apps() if d and d.strip()}
        if not denied:
            return None
        return next((n for n in names if n and _app_key(n) in denied), None)

    def spec(self) -> dict[str, Any]:
        spec = super().spec()
        spec["function"]["description"] += f" Known apps: {', '.join(sorted(self.apps))}."
        return spec

    def resolve(self, name: str) -> tuple[str, AppEntry | None]:
        key = name.strip().lower()
        for k, entry in self.apps.items():
            if key == k.lower() or key in (a.lower() for a in entry.aliases):
                return k, entry
        return key, None

    def app(self, args: OpenAppArgs) -> str | None:
        return self.resolve(args.name)[0]

    def paths(self, args: OpenAppArgs) -> list[str]:
        return [args.argument] if args.argument and not args.argument.startswith("http") else []

    def plan(self, args: OpenAppArgs) -> tuple[str, list[str], list[Launch], str]:
        """(app name, window hints, ways to start it in order, error). The first way is what Delta always
        used; the others are the self-heal alternatives."""
        key, entry = self.resolve(args.name)
        hints = [key, args.name.strip().lower()]
        extra = [args.argument] if args.argument else []
        ways: list[Launch] = []
        if entry and entry.path:
            target = os.path.expandvars(entry.path)
            if not os.path.exists(target):
                return key, hints, [], f"{key} not found at {target}; fix config/apps.yaml"
            return key, hints, [Launch("apps.yaml", lambda: _popen([target, *extra]),
                                       ntpath.basename(target).lower())], ""
        exe = entry.command if entry and entry.command else key
        found = shutil.which(exe)
        if found:  # on PATH: what Delta always did first
            ways.append(Launch("exe", lambda: _popen([found, *extra]), ntpath.basename(found).lower()))
        app_path = match_record(exe, self.index.app_paths(), exact_only=True)
        if app_path:  # App Paths names (chrome, winword...): ShellExecute finds them like Win+R does
            ways.append(Launch("App Paths", lambda: _shell_open(app_path.name + ".exe", args.argument),
                               ntpath.basename(app_path.target).lower()))
        shortcut = match_record(args.name, self.index.shortcuts(), exact_only=True)
        if shortcut:  # a Start-menu shortcut with exactly this name: no PowerShell needed
            hints.append(shortcut.name.lower())
            ways.append(Launch("shortcut", lambda: _shell_open(shortcut.target, args.argument)))
        if not extra and (not ways or self.index.store_cached()):
            sa = match_start_app(args.name, self.index.store())  # Store and Start-menu apps (Calculator...)
            if sa:
                if not ways:
                    key = sa["Name"]
                hints.append(sa["Name"].lower())
                ways.append(Launch("Start menu", lambda: _apps_folder(sa["AppID"])))
        if not ways and (near := match_record(args.name, self.index.shortcuts())):
            # Get-StartApps unavailable (PowerShell blocked) or no match there: the closest shortcut name
            key = near.name
            hints.append(near.name.lower())
            ways.append(Launch("shortcut", lambda: _shell_open(near.target, args.argument)))
        if not ways:
            return key, hints, [], (f"could not find '{args.name}' in the Start menu or on PATH; "
                                    "add it to config/apps.yaml")
        return key, hints, ways[:MAX_LAUNCH_TRIES], ""

    async def run(self, args: OpenAppArgs) -> ToolResult:
        key, hints, ways, error = await asyncio.to_thread(self.plan, args)
        if error:
            return ToolResult(False, error)
        if hit := self.blocked([key, args.name, *hints, *(w.exe for w in ways)]):
            return ToolResult(False, f"BLOCKED: '{args.name}' would open '{hit}', an app you blocked in Delta's "
                                     "settings (Permissions). Delta will not open it.",
                              {"app": key, "denied": True})
        exe = next((w.exe for w in ways if w.exe), "")
        if not args.argument and not args.new_window:
            names = list(dict.fromkeys([key, args.name.strip(), *hints]))
            open_now = await asyncio.to_thread(find_open_window, names, exe)
            if open_now:
                await asyncio.to_thread(_bring_front, open_now[0])
                return ToolResult(True, f"{key} was already open; brought its window '{open_now[1]}' to the front "
                                        "(use new_window=true to start another copy)",
                                  {"app": key, "process_running": True, "window": open_now[1], "reused": True,
                                   "verified": True})
        recoveries: list[str] = []
        problems: list[str] = []
        for n, way in enumerate(ways):
            before = await asyncio.to_thread(_windows)
            try:
                await asyncio.to_thread(way.run)
            except OSError as e:  # Windows refused this way (blocked file, missing target): try the next
                problems.append(f"{way.how}: {e.strerror or e}")
                log.info("selfheal.recovery", tool="open_app", app=key, how=way.how, ok=False, error=str(e))
                continue
            title = await asyncio.to_thread(wait_for_app_window, before, hints)
            running = bool(title) or await asyncio.to_thread(_process_running, way.exe)
            if n:
                line = f"Starting {key} as {ways[0].how} did not work ({'; '.join(problems) or 'nothing ran'}); " \
                       f"started it through the {way.how} instead."
                recoveries.append(line)
                log.info("selfheal.recovery", tool="open_app", app=key, how=way.how, ok=bool(title or running),
                         line=line)
            note = (" " + " ".join(recoveries)) if recoveries else ""
            ev: dict[str, Any] = {"app": key, "process_running": running, "launched_via": way.how}
            if recoveries:
                ev["recoveries"] = recoveries
            if title:  # Windows shows the app's own window in front: that is the proof it opened
                return ToolResult(True, f"launched {key}; its window '{title}' is open and in front{note}",
                                  {**ev, "window": title, "verified": True})
            if running or not way.exe or n == len(ways) - 1:
                # It started (or we cannot tell): never start a second copy, just say what is known.
                return ToolResult(True, f"launched {key} (no window seen yet; check with list_windows){note}", ev)
            problems.append(f"{way.how}: the app did not start")
        return ToolResult(False, f"could not open {key}: " + "; ".join(problems) +
                          ". Windows may be blocking it (for example Smart App Control), or it is not installed.",
                          {"app": key, "tried": [w.how for w in ways]})


def _app_key(name: str) -> str:
    """How app names are compared with the blocked list: any case, no folder, no .exe/.lnk."""
    n = ntpath.basename(name.strip().strip('"')).lower()
    return n[:-4] if n.endswith((".exe", ".lnk")) else n


def _process_running(exe: str) -> bool:
    if not exe:
        return False
    return any((p.info["name"] or "").lower() == exe for p in psutil.process_iter(["name"]))


class OpenTargetArgs(BaseModel):
    target: str = Field(description="Folder, file or URL to open with its default handler")


class OpenTarget(Tool[OpenTargetArgs]):
    name = "open_target"
    description = "Open a folder in File Explorer, a file with its default app, or a URL in the browser."
    risk = Risk.ACT
    Args = OpenTargetArgs

    def paths(self, args: OpenTargetArgs) -> list[str]:
        return [] if "://" in args.target else [args.target]

    def domains(self, args: OpenTargetArgs) -> list[str]:
        return [args.target] if "://" in args.target else []

    async def run(self, args: OpenTargetArgs) -> ToolResult:
        target = args.target if "://" in args.target else os.path.expandvars(os.path.expanduser(args.target))
        if "://" not in target and not os.path.exists(target):
            return ToolResult(False, f"not found: {target}")
        await asyncio.to_thread(os.startfile, target)
        return ToolResult(True, f"opened {target}", {"target": target})


class NoArgs(BaseModel):
    pass


class ListRunningApps(Tool[NoArgs]):
    name = "list_running_apps"
    description = "List running applications (process names with memory use). Useful to verify an app opened."
    risk = Risk.READ
    Args = NoArgs

    async def run(self, args: NoArgs) -> ToolResult:
        seen: dict[str, float] = {}
        for p in psutil.process_iter(["name", "memory_info"]):
            name, mem = p.info["name"], p.info["memory_info"]
            if name and mem:
                seen[name] = seen.get(name, 0) + mem.rss / 1e6
        top = sorted(seen.items(), key=lambda kv: -kv[1])[:40]
        return ToolResult(True, "\n".join(f"{n}: {mb:.0f} MB" for n, mb in top), {"apps": [n for n, _ in top]})


def app_tools(apps: dict[str, AppEntry], cache_dir: Path | None = None) -> list[Tool]:
    return [OpenApp(apps, cache_dir), OpenTarget(), ListRunningApps()]
