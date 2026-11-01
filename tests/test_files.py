from pathlib import Path

from delta.config import FilesConfig, ensure_desktop_shortcut, in_onedrive, save_dir


def test_local_desktop_gets_delta_folder(monkeypatch, tmp_path):
    monkeypatch.setattr("delta.config.known_folder", lambda name: tmp_path / "Desktop")
    assert save_dir(FilesConfig(), create=False) == tmp_path / "Desktop" / "Delta"
    assert save_dir(FilesConfig()).is_dir()


def test_onedrive_desktop_saves_locally_instead(monkeypatch, tmp_path):
    monkeypatch.setenv("OneDrive", str(tmp_path / "OneDrive"))
    monkeypatch.setattr("delta.config.known_folder", lambda name: tmp_path / "OneDrive" / "Desktop")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    out = save_dir(FilesConfig(), create=False)
    assert out == tmp_path / "home" / "Delta"
    assert not in_onedrive(out)


def test_custom_save_dir(tmp_path):
    assert save_dir(FilesConfig(save_dir=str(tmp_path / "out")), create=False) == tmp_path / "out"


def test_onedrive_detection(monkeypatch):
    monkeypatch.setenv("OneDrive", r"C:\Users\x\OneDrive")
    assert in_onedrive(Path(r"C:\Users\x\OneDrive\Desktop\Delta"))
    assert not in_onedrive(Path(r"C:\Users\x\Desktop\Delta"))


def test_desktop_shortcut_points_to_local_folder(monkeypatch, tmp_path):
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    folder = tmp_path / "home" / "Delta"
    folder.mkdir(parents=True)
    monkeypatch.setattr("delta.config.known_folder", lambda name: desktop)
    link = ensure_desktop_shortcut(folder)
    assert link == desktop / "Delta.lnk" and link.exists()
    assert ensure_desktop_shortcut(desktop / "Delta") is None  # folder already on the Desktop: no shortcut
