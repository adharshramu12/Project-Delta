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


def test_a_pc_without_a_graphics_card_tries_cloud_keys_before_a_local_model():
    from types import SimpleNamespace

    from delta.config import GovernorConfig
    from delta.system.governor import Governor

    assert not Governor(GovernorConfig(), sensor=SimpleNamespace(vram_gb=0.0)).local_first()
    assert not Governor(GovernorConfig(), sensor=SimpleNamespace(vram_gb=2.0)).local_first()
    assert Governor(GovernorConfig(), sensor=SimpleNamespace(vram_gb=6.4)).local_first()


async def test_ollama_is_not_loaded_when_ram_is_short_unless_already_in_memory(monkeypatch):
    from types import SimpleNamespace

    import httpx
    import psutil
    import pytest

    from delta.config import ProviderConfig
    from delta.llm.providers import OpenAICompatProvider
    from delta.llm.schemas import LLMError

    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=2e9))
    running: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/ps":
            return httpx.Response(200, json={"models": running})
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    p = OpenAICompatProvider("ollama", ProviderConfig(base_url="http://127.0.0.1:11434/v1", local=True,
                                                      min_free_ram_gb=6), None)
    with pytest.raises(LLMError, match="RAM free"):
        await p._check_room("gemma3n:e4b")
    running.append({"name": "gemma3n:e4b", "model": "gemma3n:e4b"})
    await p._check_room("gemma3n:e4b")  # already loaded: costs no extra RAM


def test_shipped_ollama_has_a_ram_guard():
    from delta.config import REPO_ROOT, _read_yaml

    assert _read_yaml(REPO_ROOT / "config" / "delta.yaml")["llm"]["providers"]["ollama"]["min_free_ram_gb"] >= 4
