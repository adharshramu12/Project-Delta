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


def test_window_leftovers_are_set_aside_before_it_opens_and_deleted_later(tmp_path, monkeypatch):
    """The window profile was tidied only after the window opened, so it was always in use: 764 MB, never trimmed."""
    monkeypatch.setattr(storage, "in_use", lambda p: False)
    win = tmp_path / "app-window"
    junk = [make(win, rel, mb=1) for rel in ("component_crx_cache", "Default/Cache/Cache_Data")]
    keep = make(win, "Default/Local Storage/leveldb")
    assert storage.before_window(tmp_path) == 2
    assert not any(p.exists() for p in junk) and keep.exists()
    assert len(list((tmp_path / storage.TRASH).iterdir())) == 2
    out = storage.tidy(tmp_path)
    assert out[0].profile == storage.TRASH and out[0].freed_mb > 0
    assert list((tmp_path / storage.TRASH).iterdir()) == []


def test_window_in_use_is_never_touched(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "in_use", lambda p: True)
    cache = make(tmp_path / "app-window", "component_crx_cache")
    assert storage.before_window(tmp_path) == 0 and cache.exists()


def test_set_aside_never_follows_a_link_out_of_the_profile(tmp_path):
    outside = make(tmp_path / "precious", "files")
    prof = tmp_path / "app-window"
    prof.mkdir()
    try:
        (prof / "component_crx_cache").symlink_to(tmp_path / "precious", target_is_directory=True)
    except OSError:
        import pytest

        pytest.skip("no symlink rights on this machine")
    assert storage.set_aside(prof, ("component_crx_cache",), tmp_path) == 0
    storage.empty_trash(tmp_path)
    assert outside.exists()
