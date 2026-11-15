"""Keep Delta small on the user's disk: trim the browser profiles' regenerable leftovers.

Delta runs two Chromium profiles of its own: the dashboard window (data_dir/app-window) and its private browser
(data_dir/<browser>-profile). Edge and Chrome fill a profile over time with things Delta never needs: downloaded
components, provenance data, built-in extensions, metrics, crash dumps and caches. On a real PC the dashboard
window's profile had grown to 737 MB while the user's own Delta data was 3 MB.

Two layers:
  - launch flags cap the disk cache (see daemon.LEAN_WINDOW_FLAGS and tools.browser.chrome_args);
  - the leftovers listed below are removed only from a profile no browser process is using:
      the dashboard window's profile just before the window opens (`set_aside`: the folders are moved into
      data_dir/.trash, which is instant on the same drive, so the window does not wait), and the private browser's
      at start-up (`tidy`). `tidy` also empties .trash, in the background.

The dashboard window used to be tidied only at start-up, 20 seconds after the window had opened, so it was always
"in use" and never tidied: on a real PC its profile stayed at 764 MB from its first day.

Only regenerable folders are ever removed. Cookies, saved site sign-ins, Local Storage (the dashboard's theme and
preferences), IndexedDB and Preferences are never touched: the browser rebuilds every listed folder by itself.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from delta.system.logging import log

# Folders inside a Chromium user-data dir that the browser re-creates when it needs them.
CACHES = ("Default/Cache", "Default/Code Cache", "Default/GPUCache", "Default/Shared Dictionary",
          "Default/DawnGraphiteCache", "Default/DawnWebGPUCache", "Default/Service Worker/CacheStorage",
          "Default/Service Worker/ScriptCache", "GrShaderCache", "GraphiteDawnCache", "ShaderCache")
COMPONENTS = ("component_crx_cache", "ProvenanceData", "BrowserMetrics", "Crashpad", "WidevineCdm",
              "OptimizationGuidePredictionModels", "optimization_guide_model_store", "Default/Extensions")
# The dashboard window is a page of Delta's own: no extension, component or cache of it is needed.
WINDOW_JUNK = CACHES + COMPONENTS
# The private browser keeps the user's site sign-ins: only caches and components, never Default/Extensions
# (an extension the user added there stays).
BROWSER_JUNK = CACHES + tuple(c for c in COMPONENTS if c != "Default/Extensions")
BROWSER_TRIM_OVER_MB = 150.0  # the private browser is trimmed only when it has grown past this
TRASH = ".trash"  # inside the data folder: folders set aside here are deleted in the background


@dataclass
class Tidied:
    profile: str
    freed_mb: float
    skipped: str = ""


def size_mb(path: Path) -> float:
    total = 0
    for f in path.rglob("*"):
        with contextlib.suppress(OSError):
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
    return total / 1_048_576


def in_use(profile: Path) -> bool:
    """A browser process started with this --user-data-dir is running (then nothing in it is touched)."""
    import psutil

    key = str(profile).lower()
    for p in psutil.process_iter(["name", "cmdline"]):
        with contextlib.suppress(psutil.Error, TypeError):
            if (p.info["name"] or "").lower() in ("msedge.exe", "chrome.exe") and key in " ".join(
                    p.info["cmdline"] or []).lower():
                return True
    return False


def trim(profile: Path, junk: tuple[str, ...]) -> float:
    """Remove the listed folders from one profile; the MB freed."""
    freed = 0.0
    root = profile.resolve()
    for rel in junk:
        target = (profile / rel).resolve()
        if not target.is_dir() or root not in target.parents:  # never outside the profile, never a file or link
            continue
        mb = size_mb(target)
        shutil.rmtree(target, ignore_errors=True)
        if not target.exists():
            freed += mb
    return freed


def set_aside(profile: Path, junk: tuple[str, ...], data_dir: Path) -> int:
    """Move the listed folders out of a profile into data_dir/.trash, for `empty_trash` to delete later. A move on
    the same drive is instant whatever the folder holds, so this can run right before a window opens. The caller
    makes sure no browser uses the profile. Returns how many folders were moved; one that cannot be moved stays."""
    trash = data_dir / TRASH
    moved = 0
    root = profile.resolve()
    for rel in junk:
        target = profile / rel
        with contextlib.suppress(OSError):
            real = target.resolve()
            if target.is_symlink() or not target.is_dir() or root not in real.parents:
                continue  # never outside the profile, never a file or link
            trash.mkdir(parents=True, exist_ok=True)
            os.replace(real, trash / f"{uuid.uuid4().hex[:12]}-{real.name}")
            moved += 1
    return moved


def empty_trash(data_dir: Path) -> float:
    """Delete what `set_aside` moved into data_dir/.trash; the MB freed. Only that folder is ever touched."""
    trash = data_dir / TRASH
    if not trash.is_dir() or trash.is_symlink():
        return 0.0
    freed = 0.0
    for item in trash.iterdir():
        if item.is_symlink():
            with contextlib.suppress(OSError):
                item.unlink()
            continue
        mb = size_mb(item) if item.is_dir() else item.stat().st_size / 1_048_576
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                item.unlink()
        if not item.exists():
            freed += mb
    return freed


def before_window(data_dir: Path) -> int:
    """Called right before the dashboard window opens: set its profile's leftovers aside when no window uses it."""
    profile = data_dir / "app-window"
    if not profile.is_dir() or in_use(profile):
        return 0
    return set_aside(profile, WINDOW_JUNK, data_dir)


def tidy(data_dir: Path) -> list[Tidied]:
    """Trim Delta's own browser profiles and empty .trash. Safe to call any time: a profile in use is skipped."""
    out: list[Tidied] = []
    trashed = empty_trash(data_dir)
    if trashed:
        out.append(Tidied(TRASH, round(trashed, 1)))
    plans: list[tuple[Path, tuple[str, ...], float]] = [(data_dir / "app-window", WINDOW_JUNK, 0.0)]
    plans += [(p, BROWSER_JUNK, BROWSER_TRIM_OVER_MB) for p in sorted(data_dir.glob("*-profile")) if p.is_dir()]
    for profile, junk, over in plans:
        if not profile.is_dir():
            continue
        if in_use(profile):
            out.append(Tidied(profile.name, 0.0, "in use"))
            continue
        if over and size_mb(profile) < over:
            out.append(Tidied(profile.name, 0.0, "small enough"))
            continue
        out.append(Tidied(profile.name, round(trim(profile, junk), 1)))
    freed = sum(t.freed_mb for t in out)
    if freed:
        log.info("storage.tidied", freed_mb=round(freed, 1), profiles=[t.profile for t in out if t.freed_mb])
    return out
