"""Keep Delta small on the user's disk: trim the browser profiles' regenerable leftovers.

Delta runs two Chromium profiles of its own: the dashboard window (data_dir/app-window) and its private browser
(data_dir/<browser>-profile). Edge and Chrome fill a profile over time with things Delta never needs: downloaded
components, provenance data, built-in extensions, metrics, crash dumps and caches. On a real PC the dashboard
window's profile had grown to 737 MB while the user's own Delta data was 3 MB.

Two layers:
  - launch flags cap the disk cache (see daemon.LEAN_WINDOW_FLAGS and tools.browser.chrome_args);
  - `tidy` deletes the leftovers listed below at start-up, only from a profile no browser process is using.

Only regenerable folders are ever removed. Cookies, saved site sign-ins, Local Storage (the dashboard's theme and
preferences), IndexedDB and Preferences are never touched: the browser rebuilds every listed folder by itself.
"""

from __future__ import annotations

import contextlib
import shutil
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


def tidy(data_dir: Path) -> list[Tidied]:
    """Trim Delta's own browser profiles. Safe to call any time: a profile in use is skipped."""
    out: list[Tidied] = []
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
