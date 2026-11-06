"""Faster app opening: the cached installed-apps index, reusing an open window, launching through the shell,
and the bounded self-heal when a way of starting an app fails. Real launches: tests/e2e/test_open_app_speed_e2e.py."""

from __future__ import annotations

import json
import os
import time

import pytest

from delta.tools import apps
from delta.tools.apps import (
    AppIndex,
    AppRecord,
    Launch,
    OpenApp,
    OpenAppArgs,
    match_record,
    pick_open_window,
    scan_shortcuts,
    title_names_app,
)

STORE = [{"Name": "Calculator", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
         {"Name": "WhatsApp", "AppID": "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"}]


@pytest.fixture
def quiet_sources(monkeypatch):
    """No real Start menu or registry: the index sees only what each test gives it."""
    state = {"shortcuts": [], "app_paths": []}
    monkeypatch.setattr(apps, "scan_shortcuts", lambda dirs=None: (state["shortcuts"], 1.0))
    monkeypatch.setattr(apps, "scan_app_paths", lambda: (state["app_paths"], [1, 1]))
    return state


def counting_scan(result=STORE):
    calls = {"n": 0}

    def scan():
        calls["n"] += 1
        return list(result)

    return scan, calls


def test_store_apps_are_read_once_then_from_memory_and_disk(tmp_path, quiet_sources):
    scan, calls = counting_scan()
    idx = AppIndex(tmp_path, store_scan=scan)
    t0 = time.perf_counter()
    for _ in range(200):
        assert apps.match_start_app("calc", idx.store())["Name"] == "Calculator"
    warm = time.perf_counter() - t0
    assert calls["n"] == 1 and warm < 1.0  # one PowerShell call, then microseconds each
    saved = json.loads((tmp_path / "apps-index.json").read_text(encoding="utf-8"))
    assert saved["store"][0]["Name"] == "Calculator"
    again = AppIndex(tmp_path, store_scan=scan)  # Delta restarted: the disk copy is used
    assert again.store() == STORE and calls["n"] == 1


def test_store_apps_are_read_again_when_store_apps_changed(tmp_path, quiet_sources, monkeypatch):
    scan, calls = counting_scan()
    stamp = {"v": 1}
    monkeypatch.setattr(AppIndex, "_store_stamp", staticmethod(lambda: stamp["v"]))
    idx = AppIndex(tmp_path, store_scan=scan)
    idx.store()
    stamp["v"] = 2  # an app was installed from the Store
    idx.store()
    assert calls["n"] == 2
    assert AppIndex(tmp_path, store_scan=scan).store() and calls["n"] == 2  # the new stamp was saved


def test_a_broken_or_foreign_cache_file_is_ignored(tmp_path, quiet_sources):
    (tmp_path / "apps-index.json").write_text("{not json", encoding="utf-8")
    scan, calls = counting_scan()
    assert AppIndex(tmp_path, store_scan=scan).store() == STORE and calls["n"] == 1


def test_non_ascii_names_survive_the_cache(tmp_path, quiet_sources):
    names = [{"Name": "メモ帳", "AppID": "a"}, {"Name": "Tâches", "AppID": "b"}]
    scan, _ = counting_scan(names)
    AppIndex(tmp_path, store_scan=scan).store()
    assert AppIndex(tmp_path, store_scan=lambda: []).store() == names


def test_start_menu_shortcuts_are_found_in_subfolders(tmp_path):
    root = tmp_path / "Programs"
    (root / "Vendor Tools").mkdir(parents=True)
    (root / "Notepad++.lnk").write_bytes(b"")
    (root / "Vendor Tools" / "Pläne.lnk").write_bytes(b"")
    (root / "Vendor Tools" / "Uninstall Pläne.lnk").write_bytes(b"")
    (root / "readme.txt").write_text("x")
    found, newest = scan_shortcuts((str(root),))
    assert sorted(r.name for r in found) == ["Notepad++", "Pläne"] and newest > 0
    assert match_record("pläne", found).target.endswith("Pläne.lnk")


def test_window_of_an_open_app_but_not_a_web_page_named_like_it():
    windows = {1: ("WhatsApp Web - Google Chrome", 10), 2: ("Untitled - Notepad", 20), 3: ("WhatsApp", 30)}
    procs = {10: "chrome.exe", 20: "notepad.exe", 30: "applicationframehost.exe"}
    assert pick_open_window(windows, procs, ["notepad"], "notepad.exe") == (2, "Untitled - Notepad")
    assert pick_open_window(windows, procs, ["whatsapp"]) == (3, "WhatsApp")
    assert pick_open_window({1: windows[1]}, procs, ["whatsapp"]) is None
    assert title_names_app("Report.docx - Word", ["word"]) and not title_names_app("Wordle - Chrome", ["word"])


def tool_with(tmp_path, store=STORE, shortcuts=(), app_paths=()) -> OpenApp:
    t = OpenApp({}, tmp_path)
    t.index = AppIndex(tmp_path, store_scan=lambda: list(store))
    t.index._check = lambda: None  # type: ignore[method-assign]
    t.index._shortcuts, t.index._app_paths = list(shortcuts), list(app_paths)
    return t


def test_launch_order_keeps_what_worked_before(tmp_path, monkeypatch):
    monkeypatch.setattr(apps.shutil, "which", lambda exe: r"C:\Windows\notepad.exe" if exe == "notepad" else None)
    t = tool_with(tmp_path)
    key, _, ways, err = t.plan(OpenAppArgs(name="notepad"))
    assert not err and ways[0].how == "exe" and ways[0].exe == "notepad.exe"
    key, _hints, ways, err = t.plan(OpenAppArgs(name="calculator"))
    assert not err and key == "Calculator" and [w.how for w in ways] == ["Start menu"]
    key, _, ways, err = t.plan(OpenAppArgs(name="photoshop"))
    assert ways == [] and "could not find 'photoshop'" in err


def test_an_exact_shortcut_needs_no_powershell(tmp_path, monkeypatch):
    monkeypatch.setattr(apps.shutil, "which", lambda exe: None)

    def no_powershell():
        raise AssertionError("Get-StartApps must not run")

    t = tool_with(tmp_path, shortcuts=[AppRecord("Visual Studio Code", "lnk", r"C:\SM\Visual Studio Code.lnk")])
    t.index.store_scan = no_powershell
    _, _, ways, err = t.plan(OpenAppArgs(name="Visual Studio Code"))
    assert not err and [w.how for w in ways] == ["shortcut"]


def test_app_paths_names_open_through_the_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(apps.shutil, "which", lambda exe: None)
    t = tool_with(tmp_path, app_paths=[AppRecord("chrome", "exe", r"C:\Program Files\Google\Chrome\chrome.exe")])
    opened: list[tuple] = []
    monkeypatch.setattr(apps, "_shell_open", lambda target, argument=None: opened.append((target, argument)))
    _, _, ways, _ = t.plan(OpenAppArgs(name="chrome"))
    ways[0].run()
    assert ways[0].how == "App Paths" and opened == [("chrome.exe", None)] and ways[0].exe == "chrome.exe"


def run_with(monkeypatch, t: OpenApp, ways: list[Launch], window: str | None = None, open_now=None,
             running: bool = False):
    monkeypatch.setattr(OpenApp, "plan", lambda self, args: ("Notepad", ["notepad"], ways, ""))
    monkeypatch.setattr(apps, "find_open_window", lambda names, exe="": open_now)
    monkeypatch.setattr(apps, "_bring_front", lambda hwnd: None)
    monkeypatch.setattr(apps, "_windows", lambda: {})
    monkeypatch.setattr(apps, "wait_for_app_window", lambda before, hints, seconds=8: window)
    monkeypatch.setattr(apps, "_process_running", lambda exe: running)


async def test_an_open_app_is_brought_forward_not_started_again(tmp_path, monkeypatch):
    started: list[str] = []
    t = tool_with(tmp_path)
    run_with(monkeypatch, t, [Launch("exe", lambda: started.append("exe"), "notepad.exe")],
             open_now=(7, "notes.txt - Notepad"))
    r = await t.run(OpenAppArgs(name="notepad"))
    assert r.ok and r.evidence["reused"] and "already open" in r.output and started == []
    r = await t.run(OpenAppArgs(name="notepad", new_window=True))  # asked for another copy
    assert started == ["exe"] and not r.evidence.get("reused")


async def test_a_refused_launch_heals_through_the_next_way_and_says_so(tmp_path, monkeypatch):
    started: list[str] = []

    def blocked():
        raise OSError(4551, "An Application Control policy has blocked this file")

    t = tool_with(tmp_path)
    run_with(monkeypatch, t, [Launch("exe", blocked, "notepad.exe"),
                              Launch("Start menu", lambda: started.append("menu"))], window="Untitled - Notepad")
    r = await t.run(OpenAppArgs(name="notepad"))
    assert r.ok and started == ["menu"] and r.evidence["launched_via"] == "Start menu"
    assert "Application Control" in r.evidence["recoveries"][0] and "instead" in r.output


async def test_started_but_slow_is_never_started_twice(tmp_path, monkeypatch):
    started: list[str] = []
    t = tool_with(tmp_path)
    run_with(monkeypatch, t, [Launch("exe", lambda: started.append("exe"), "notepad.exe"),
                              Launch("Start menu", lambda: started.append("menu"))], window=None, running=True)
    r = await t.run(OpenAppArgs(name="notepad"))
    assert r.ok and started == ["exe"] and "no window seen yet" in r.output


async def test_every_way_failing_is_reported_honestly(tmp_path, monkeypatch):
    def blocked():
        raise OSError(2, "The system cannot find the file specified")

    t = tool_with(tmp_path)
    run_with(monkeypatch, t, [Launch("exe", blocked, "x.exe"), Launch("shortcut", blocked)])
    r = await t.run(OpenAppArgs(name="notepad"))
    assert not r.ok and "could not open Notepad" in r.output and r.evidence["tried"] == ["exe", "shortcut"]


async def test_nothing_ran_moves_on_but_at_most_three_ways(tmp_path, monkeypatch):
    started: list[str] = []
    t = tool_with(tmp_path)
    ways = [Launch(f"way{i}", lambda i=i: started.append(f"way{i}"), f"x{i}.exe") for i in range(3)]
    run_with(monkeypatch, t, ways, window=None, running=False)
    r = await t.run(OpenAppArgs(name="notepad"))
    assert started == ["way0", "way1", "way2"] and r.ok and "no window seen yet" in r.output
    assert apps.MAX_LAUNCH_TRIES == 3


def test_overlay_windows_have_no_title_so_they_are_never_taken_for_an_app():
    with open(os.path.join(os.path.dirname(apps.__file__), "..", "system", "control_overlay.py"),
              encoding="utf-8") as f:
        src = f.read()
    assert 'CreateWindowExW(OVERLAY_EX_STYLE, self._class, "",' in src
