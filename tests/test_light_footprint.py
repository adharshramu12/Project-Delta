"""Delta stays light: no GPU unless a local model needs it, a lean window that really closes."""

import sys

from delta.daemon import LEAN_WINDOW_FLAGS, window_processes
from delta.system.governor import SystemSensor


def test_window_is_lean_and_does_not_linger():
    for flag in ("--disable-background-mode", "--disable-gpu", "--disable-extensions"):
        assert flag in LEAN_WINDOW_FLAGS


def test_no_gpu_driver_without_local_models(monkeypatch):
    monkeypatch.setitem(sys.modules, "pynvml", None)  # importing it would fail loudly
    s = SystemSensor(watch_gpu=False)
    assert s._nvml is None and s.read().gpu_temp_c is None


def test_only_delta_window_processes_are_found(tmp_path, monkeypatch):
    import psutil

    class P:
        def __init__(self, name, cmd):
            self.info = {"name": name, "cmdline": cmd}

    profile = str(tmp_path / "app-window")
    procs = [P("msedge.exe", ["msedge.exe", f"--user-data-dir={profile}"]),
             P("msedge.exe", ["msedge.exe", "--profile-directory=Default"]),  # the user's own browser
             P("chrome.exe", ["chrome.exe", f"--user-data-dir={profile}"])]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter(procs))
    assert window_processes(tmp_path) == [procs[0]]
