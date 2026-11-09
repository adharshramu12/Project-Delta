"""Delta's browser profiles are trimmed of regenerable leftovers, never of sign-ins or settings."""

from pathlib import Path

from delta.daemon import LEAN_WINDOW_FLAGS
from delta.system import storage
from delta.tools.browser import chrome_args


def make(root: Path, rel: str, mb: float = 0.01) -> Path:
    p = root / rel / "data.bin"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * int(mb * 1_048_576))
    return p


def test_the_dashboard_window_profile_loses_its_junk_and_keeps_the_users_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "in_use", lambda p: False)
    win = tmp_path / "app-window"
    junk = [make(win, rel) for rel in ("component_crx_cache", "ProvenanceData/2025", "Default/Extensions/abc",
                                       "Default/Cache/Cache_Data", "BrowserMetrics", "WidevineCdm/4.1")]
    keep = [make(win, rel) for rel in ("Default/Local Storage/leveldb", "Default/IndexedDB")]
    (win / "Default" / "Preferences").write_text("{}")
    out = storage.tidy(tmp_path)
    assert not any(p.exists() for p in junk)
    assert all(p.exists() for p in keep) and (win / "Default" / "Preferences").exists()
    assert out[0].profile == "app-window" and out[0].freed_mb > 0


def test_the_private_browser_keeps_sign_ins_and_extensions_and_is_left_alone_while_small(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "in_use", lambda p: False)
    prof = tmp_path / "chrome-profile"
    cookies = make(prof, "Default/Network")
    ext = make(prof, "Default/Extensions/mine")
    cache = make(prof, "Default/Cache", mb=1)
    assert storage.tidy(tmp_path)[0].skipped == "small enough" and cache.exists()
    monkeypatch.setattr(storage, "BROWSER_TRIM_OVER_MB", 0.5)
    storage.tidy(tmp_path)
    assert not cache.exists() and cookies.exists() and ext.exists()


def test_a_profile_in_use_is_never_touched(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "in_use", lambda p: True)
    junk = make(tmp_path / "app-window", "component_crx_cache")
    assert storage.tidy(tmp_path)[0].skipped == "in use" and junk.exists()


def test_both_browsers_start_with_a_capped_disk_cache():
    assert "--disk-cache-size=33554432" in LEAN_WINDOW_FLAGS
    assert "--disk-cache-size=104857600" in chrome_args(Path("x"), 9222)
