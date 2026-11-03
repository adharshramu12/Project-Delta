"""PC controls from Telegram: volume, brightness, closing apps, screenshot, camera. A fake PC, so tests never
change the real volume or close real apps."""

from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")

from delta.channels import pc_menu
from delta.channels.telegram import TelegramChannel
from delta.system.pc_control import OpenWindow, may_lose_work


class FakePC:
    def __init__(self, tmp_path):
        self.vol, self.muted, self.bri = 40, False, 70
        self.windows = [OpenWindow(1, "Free Fire - YouTube - Google Chrome", "chrome.exe", 10),
                        OpenWindow(2, "*notes.txt - Notepad", "Notepad.exe", 11)]
        self.closed, self.tmp = [], tmp_path
        self.close_result = "closed"

    def volume(self):
        return self.vol, self.muted

    def set_volume(self, level=None, step=0, mute=None):
        if level is not None or step:
            self.vol = max(0, min(100, (self.vol + step) if level is None else level))
            self.muted = False
        if mute is not None:
            self.muted = mute
        return self.volume()

    def brightness(self):
        return self.bri

    def set_brightness(self, level=None, step=0):
        self.bri = (self.bri + step) if level is None else level
        return self.bri

    def open_windows(self):
        return list(self.windows)

    def may_lose_work(self, w):
        return may_lose_work(w)

    def close_window(self, hwnd, force=False):
        self.closed.append((hwnd, force))
        if self.close_result == "closed" or force:
            self.windows = [w for w in self.windows if w.hwnd != hwnd]
            return "closed"
        return self.close_result

    def screenshot(self):
        p = self.tmp / "screen.jpg"
        p.write_bytes(b"jpg")
        return p


@pytest.fixture
def setup(tmp_path, monkeypatch):
    fake = FakePC(tmp_path)
    monkeypatch.setattr(pc_menu, "pc", fake)
    events = []
    c = SimpleNamespace(audit=SimpleNamespace(record=lambda e, **k: events.append((e, k))))
    ch = TelegramChannel(c, "123456:TEST-TOKEN", ["5"])  # type: ignore[arg-type]
    return ch, fake, events


class Tap:
    def __init__(self, data):
        self.data, self.from_user = data, SimpleNamespace(id=5)
        self.replies = []
        self.message = SimpleNamespace(chat=SimpleNamespace(id=5), reply_text=self._reply)
        self.edits = []

    async def answer(self, *a):
        return None

    async def _reply(self, text, **kw):
        self.replies.append(text)

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))


def labels(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


async def press(ch, data):
    tap = Tap(data)
    await ch._on_pc_button(SimpleNamespace(callback_query=tap), None)
    return tap


async def test_volume_buttons_change_it_and_show_the_new_level(setup):
    ch, fake, events = setup
    tap = await press(ch, "sys:vol:+10")
    assert fake.vol == 50 and tap.edits[-1][0] == "🔊 Volume 50%" and "🔇 Mute" in labels(tap.edits[-1][1])
    tap = await press(ch, "sys:vol:mute")
    assert fake.muted and "(muted)" in tap.edits[-1][0]
    await press(ch, "sys:bri:=25")
    assert fake.bri == 25 and ("pc.brightness", {"change": "=25", "by": "5"}) in events


async def test_closing_apps_asks_only_when_work_could_be_lost(setup):
    ch, fake, events = setup
    shown = []

    async def say(text, reply_markup=None):
        shown.append((text, reply_markup))

    await ch._show_apps(say)
    assert labels(shown[-1][1])[:2] == ["✖ chrome: Free Fire - YouTube - Google Chrome",
                                        "✖ Notepad: *notes.txt - Notepad"]

    tap = await press(ch, "sys:close:0")  # a browser: nothing to lose, closes at once
    assert fake.closed == [(1, False)] and tap.edits[-1][0].startswith("✅ Closed chrome")
    assert events[-1][0] == "pc.close" and events[-1][1]["title"].startswith("Free Fire")

    tap = await press(ch, "sys:close:1")  # unsaved Notepad: asks first, nothing closed yet
    assert fake.closed == [(1, False)] and "unsaved work" in tap.edits[-1][0]
    fake.close_result = "still_open"  # it shows "Save changes?"
    tap = await press(ch, "sys:closeok:1")
    assert "still open" in tap.edits[-1][0] and "Force close (unsaved work is lost)" in labels(tap.edits[-1][1])
    await press(ch, "sys:kill:1")
    assert fake.closed[-1] == (2, True) and fake.windows == []


async def test_an_old_list_never_closes_the_wrong_window(setup):
    ch, fake, _ = setup

    async def say(text, reply_markup=None):
        return None

    await ch._show_apps(say)
    fake.windows[0] = OpenWindow(1, "Bank - Google Chrome", "chrome.exe", 10)  # same window, other page now
    tap = await press(ch, "sys:close:0")
    assert fake.closed == [] and "changed" in tap.edits[-1][0]


async def test_screenshot_is_sent_then_removed_from_the_pc(setup):
    ch, fake, events = setup
    sent = []

    async def send_photo(chat_id, f, caption=""):
        sent.append((chat_id, f.read(), caption))

    ch.app = SimpleNamespace(bot=SimpleNamespace(send_photo=send_photo))
    await ch._send_picture(5, "screen", "5")
    assert sent[0][1] == b"jpg" and sent[0][2].startswith("Screen ·") and ("pc.screen", {"by": "5"}) in events
    assert not (fake.tmp / "screen.jpg").exists()


def test_windows_theme_switch(monkeypatch):
    import winreg

    from delta.system import pc_control as pc

    store = {"AppsUseLightTheme": 1, "SystemUsesLightTheme": 1}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(winreg, "OpenKey", lambda *a: Key())
    monkeypatch.setattr(winreg, "QueryValueEx", lambda k, n: (store[n], 4))
    monkeypatch.setattr(winreg, "SetValueEx", lambda k, n, r, t, v: store.__setitem__(n, v))
    assert pc.windows_theme() == "light"
    assert pc.set_windows_theme("toggle") == "dark" and store == {"AppsUseLightTheme": 0, "SystemUsesLightTheme": 0}
    assert pc.set_windows_theme("light") == "light" and pc.windows_theme() == "light"
