"""Fixes for what first users reported (11 Nov): Codex and Cline "not installed", WhatsApp stopping after it
opened, the orange border vanishing between steps, and web search failing for an hour after one empty result."""

import time

import pytest

from delta.llm import cli_providers
from delta.llm.cli_providers import find_cli
from delta.messaging import apps
from delta.messaging.apps import MessagingError, WhatsAppDriver, phone_number
from delta.system import agent_cursor
from delta.system.agent_cursor import AgentCursor
from delta.tools.web_search import SearchError, SearchHit, SearchRouter

# --- finding the AI apps ---------------------------------------------------------------------------------


@pytest.fixture
def bare_pc(tmp_path, monkeypatch):
    """A PC where nothing is on PATH and no usual folder has a CLI."""
    monkeypatch.setattr(cli_providers.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli_providers.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    for var in ("APPDATA", "LOCALAPPDATA", "NVM_SYMLINK", "PNPM_HOME", "BUN_INSTALL", "VOLTA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli_providers, "windows_path_now", lambda: [])
    monkeypatch.setattr(cli_providers, "store_package_roots", lambda prefix: [])
    return tmp_path


def test_the_codex_store_app_is_found_in_its_package_folder(bare_pc, monkeypatch):
    assert find_cli(("codex",)) is None
    root = bare_pc / "WindowsApps" / "OpenAI.Codex_26.1103.4239.0_x64__2p2nqsd0c76g0"
    exe = root / "app" / "resources" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(cli_providers, "store_package_roots",
                        lambda prefix: [root] if prefix == "OpenAI.Codex_" else [])
    assert find_cli(("codex",)) == str(exe)
    assert find_cli(("cline", "cline-cli")) is None  # the Store folder is asked only for Codex


def test_a_cli_installed_after_delta_started_is_found_on_todays_path(bare_pc, monkeypatch):
    """Delta's own PATH is from sign-in; the registry has the PATH as it is now."""
    folder = bare_pc / "nodejs-global"
    folder.mkdir()
    (folder / "cline.cmd").write_text("", encoding="utf-8")
    monkeypatch.setattr(cli_providers, "windows_path_now", lambda: [bare_pc / "gone-drive", folder])
    assert find_cli(("cline", "cline-cli")) == str(folder / "cline.cmd")


@pytest.mark.parametrize("var, sub", [("LOCALAPPDATA", "pnpm"), ("LOCALAPPDATA", "Microsoft/WinGet/Links"),
                                      ("NVM_SYMLINK", ""), ("PNPM_HOME", "")])
def test_other_package_managers_folders_are_searched(bare_pc, monkeypatch, var, sub):
    base = bare_pc / var
    folder = base / sub if sub else base
    folder.mkdir(parents=True)
    (folder / "codex.exe").write_bytes(b"")
    monkeypatch.setenv(var, str(base))
    assert find_cli(("codex",)) == str(folder / "codex.exe")


def test_registry_helpers_never_raise():
    assert isinstance(cli_providers.windows_path_now(), list)
    assert cli_providers.store_package_roots("No.Such.Package_") == []


# --- WhatsApp --------------------------------------------------------------------------------------------


def test_a_phone_number_gets_its_country_code_and_a_name_is_left_alone():
    assert phone_number("+91 98765 43210", "") == "919876543210"
    assert phone_number("9876543210", "IN") == "919876543210"
    assert phone_number("098765-43210", "IN") == "919876543210"  # written with the leading 0
    assert phone_number("919876543210", "IN") == "919876543210"  # already has it
    assert phone_number("0044 20 7946 0958", "IN") == "442079460958"
    assert phone_number("(415) 555 0132", "US") == "14155550132"
    for name in ("Ravi Kumar", "Team 2026", "Mom", "12345", "Flat 4B"):
        assert phone_number(name, "IN") is None
    with pytest.raises(MessagingError, match="country code"):
        phone_number("9876543210", "")  # Windows does not say the region: ask, never guess


def test_whatsapp_window_titles():
    d = WhatsAppDriver()
    assert all(d._is_mine(t) for t in ("WhatsApp", " WhatsApp ", "(3) WhatsApp", "(99+) WhatsApp", "WhatsApp Beta"))
    assert not any(d._is_mine(t) for t in ("WhatsApp - Google Chrome", "My WhatsApp notes.txt - Notepad", ""))


def test_a_number_opens_its_chat_with_whatsapps_own_link(monkeypatch):
    opened: list[str] = []

    class Window:
        def set_focus(self):
            opened.append("focus")

    d = WhatsAppDriver()
    monkeypatch.setattr(apps.os, "startfile", opened.append, raising=False)
    monkeypatch.setattr(apps, "region_code", lambda: "IN")
    monkeypatch.setattr(d, "window", lambda wait_s=0: Window())
    monkeypatch.setattr(d, "_wait_for_message_box", lambda w, seconds: True)
    assert d.open_chat("98765 43210") == "+919876543210"
    assert opened == ["whatsapp://send?phone=919876543210", "focus"]
    monkeypatch.setattr(d, "_wait_for_message_box", lambda w, seconds: False)
    with pytest.raises(MessagingError, match="may not be on WhatsApp"):
        d.open_chat("+91 98765 43210")


def test_the_message_box_is_looked_for_at_least_once_however_slow_a_look_is(monkeypatch):
    """One look at the new WhatsApp took 17 s against a 5 s wait: the wait ended before the first answer."""
    d = WhatsAppDriver()
    looks = []

    def slow_edits(w):
        looks.append(1)
        return None, object()

    monkeypatch.setattr(d, "_edits", slow_edits)
    assert d._wait_for_message_box(object(), 0) and looks == [1]


def test_controls_are_looked_for_in_the_page_and_the_window_is_the_fallback():
    class Plain:  # no UI Automation element at all (a fake, or an app with no web page)
        pass

    w = Plain()
    assert apps._scope(w) is w


# --- the orange signal -----------------------------------------------------------------------------------


class FakeOverlay:
    def __init__(self):
        self.calls: list[str] = []

    def start(self):
        return True

    def show(self, label=True):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")

    def beat(self):
        pass

    def close(self):
        pass


def test_inside_a_task_the_signal_stays_on_between_steps_until_the_task_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_cursor, "HOLD_S", 0.05)
    overlay = FakeOverlay()
    c = AgentCursor(tmp_path, overlay_factory=lambda on_stop: overlay)
    c.arm(lambda kind: None)  # a task starts
    assert overlay.calls == []  # nothing until Delta really drives something
    with c.active():
        pass
    time.sleep(0.2)  # the AI is thinking about the next step
    assert overlay.calls == ["show"] and c.on
    with pytest.raises(RuntimeError), c.active():  # a step fails: Delta will try another way
        raise RuntimeError("element not found")
    assert overlay.calls == ["show"] and c.on
    c.stop()  # done, failed or stopped
    assert overlay.calls == ["show", "hide"] and not c.on
    with c.active():  # outside a task it is as before: on for the action, then gone
        pass
    time.sleep(0.2)
    assert overlay.calls == ["show", "hide", "show", "hide"]


# --- web search ------------------------------------------------------------------------------------------


class Search:
    def __init__(self, name, hits=None, error=""):
        self.name, self.hits, self.error, self.calls = name, hits or [], error, 0

    def available(self):
        return True

    async def search(self, query, n):
        self.calls += 1
        if self.error:
            raise SearchError(f"{self.name}: {self.error}")
        return self.hits


async def test_no_results_is_an_answer_and_never_switches_search_off():
    """Before: one odd query with no results rested the only free provider for an hour, and every search in that
    hour failed with 'all search providers failed: ' and no reason."""
    ddg = Search("duckduckgo", error="No results found.")
    r = SearchRouter([ddg])
    with pytest.raises(SearchError, match="No results for 'zxqv'"):
        await r.search("zxqv")
    ddg.error, ddg.hits = "", [SearchHit("t", "https://x", "s")]
    assert (await r.search("python"))[0] == "duckduckgo" and ddg.calls == 2


async def test_a_blip_rests_a_provider_briefly_and_the_last_one_is_always_asked():
    ddg = Search("duckduckgo", error="timed out")
    r = SearchRouter([ddg])
    with pytest.raises(SearchError, match="browser_search"):
        await r.search("q")
    assert r._cooldown["duckduckgo"] - time.monotonic() <= SearchRouter.SHORT_COOLDOWN_S
    ddg.error, ddg.hits = "", [SearchHit("t", "https://x", "s")]
    assert (await r.search("q"))[0] == "duckduckgo"  # resting, but it is the only one: asked anyway
    paid = Search("tavily", error="HTTP 402 out of credits")
    r = SearchRouter([paid, ddg])
    await r.search("q")
    assert r._cooldown["tavily"] - time.monotonic() > SearchRouter.SHORT_COOLDOWN_S  # a used-up plan rests long
