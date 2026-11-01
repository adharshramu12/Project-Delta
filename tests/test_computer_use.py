import subprocess
import sys
from pathlib import Path

import pytest

from delta.config import Boundaries
from delta.safety.policy import PolicyEngine, Verdict
from delta.tools.base import Risk
from delta.tools.browser import BrowserClick, BrowserManager, BrowserPress, ClickArgs, PressArgs, chrome_args
from delta.tools.desktop import (
    ClickElement,
    ClickElementArgs,
    DesktopController,
    KeysArgs,
    PressKeys,
    UserOverrideError,
    click_risk,
    unicode_inputs,
)


def test_click_risk_by_button_name():
    assert click_risk("Save") == Risk.ACT
    assert click_risk("Send message") == Risk.EXTERNAL
    assert click_risk("Place order") == Risk.EXTERNAL
    assert click_risk("Delete repository") == Risk.DESTRUCTIVE


def test_risky_clicks_need_approval_even_in_trusted_mode():
    pe = PolicyEngine(Boundaries(autonomy="trusted"))
    ctl = DesktopController()
    try:
        assert pe.evaluate(ClickElement(ctl), ClickElementArgs(name="OK")).verdict == Verdict.ALLOW
        assert pe.evaluate(ClickElement(ctl), ClickElementArgs(name="Pay now")).verdict == Verdict.ASK
    finally:
        ctl.close()


def test_press_keys_risk():
    t = PressKeys(DesktopController())
    assert t.risk_for(KeysArgs(keys=["ctrl", "s"])) == Risk.ACT
    assert t.risk_for(KeysArgs(keys=["enter"])) == Risk.WRITE
    assert t.risk_for(KeysArgs(keys=["shift", "delete"])) == Risk.DESTRUCTIVE


def test_user_mouse_movement_stops_delta():
    ctl = DesktopController()
    pos = [(100, 100)]
    ctl.position = lambda: pos[0]
    ctl.guard()
    ctl.mark()
    pos[0] = (105, 102)  # jitter is fine
    ctl.guard()
    pos[0] = (600, 400)  # the human grabbed the mouse
    with pytest.raises(UserOverrideError, match="moved the mouse"):
        ctl.guard()
    with pytest.raises(UserOverrideError, match="paused"):
        ctl.guard()  # backs off for a while
    ctl.close()


def test_unicode_typing_events():
    ev = unicode_inputs("a(\n😀")
    assert ev[0] == (0, ord("a"), 0x4) and ev[1] == (0, ord("a"), 0x6)
    assert (0x0D, 0, 0) in ev  # newline is a real Enter key
    assert len(ev) == 2 + 2 + 2 + 4  # emoji = two UTF-16 units, each down+up


def test_browser_risks(tmp_path):
    mgr = BrowserManager(tmp_path)
    assert BrowserClick(mgr).risk_for(ClickArgs(text="Next")) == Risk.ACT
    assert BrowserClick(mgr).risk_for(ClickArgs(text="Submit")) == Risk.EXTERNAL
    assert BrowserPress(mgr).risk_for(PressArgs(key="Enter")) == Risk.EXTERNAL
    mgr.current_url = "https://bank.example.com/x"
    pe = PolicyEngine(Boundaries(denied_domains=["example.com"]))
    assert pe.evaluate(BrowserClick(mgr), ClickArgs(text="Next")).verdict == Verdict.DENY


def test_chrome_uses_dedicated_profile(tmp_path):
    args = chrome_args(tmp_path / "p")
    assert any(a.startswith("--user-data-dir=") and str(tmp_path) in a for a in args)
    assert "--remote-debugging-port=9223" in args


def test_computer_use_registered_but_not_loaded(tmp_path):
    """Startup-cost guard. Building Delta registers every tool but must not import heavy libraries
    (desktop control, browser, the ~2 s openai SDK, the credential store, HTTP clients): those load on
    first use. If this fails, someone added a top-level import that slows startup for every user."""
    code = (
        "import sys; from delta.bootstrap import build; from delta.config import Settings; from pathlib import Path\n"
        f"c = build(Settings(data_dir=Path(r'{tmp_path}')))\n"
        "want = {'read_window', 'click_element', 'look_at_screen', 'browser_open'}\n"
        "print(sorted(want - set(c.registry.names())))\n"
        "heavy = ['pyautogui','pywinauto','playwright','mss','PIL','openai','aiohttp','keyring','httpx','send2trash']\n"
        "print([m for m in heavy if m in sys.modules])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).parent.parent)
    missing, loaded = out.stdout.strip().splitlines()
    assert missing == "[]"
    assert loaded == "[]"
