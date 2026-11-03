from pathlib import Path

from delta.config import FilesConfig, ensure_desktop_shortcut, in_onedrive, save_dir


def test_local_desktop_gets_delta_folder(monkeypatch, tmp_path):
    monkeypatch.setattr("delta.config.known_folder", lambda name: tmp_path / "Desktop")
    assert save_dir(FilesConfig(), create=False) == tmp_path / "Desktop" / "Delta files"
    assert save_dir(FilesConfig()).is_dir()


def test_save_folder_never_clashes_with_app_icon(monkeypatch, tmp_path):
    """An empty Desktop\\Delta folder sits beside the 'Delta' app shortcut and steals the double-click."""
    monkeypatch.setattr("delta.config.known_folder", lambda name: tmp_path / "Desktop")
    (tmp_path / "Desktop" / "Delta").mkdir(parents=True)
    assert save_dir(FilesConfig(), create=False).name == "Delta files"
    (tmp_path / "Desktop" / "Delta" / "note.txt").write_text("kept")
    assert save_dir(FilesConfig(), create=False).name == "Delta"  # a folder with files stays in use


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
    assert link == desktop / "Delta files.lnk" and link.exists()
    assert ensure_desktop_shortcut(desktop / "Delta") is None  # folder already on the Desktop: no shortcut


async def test_list_dir_finds_the_biggest_files_in_one_call(tmp_path):
    """Without sizes the agent asked for a shell command (needs approval) or checked files one by one."""
    from delta.tools.filesystem import ListDir, ListDirArgs

    (tmp_path / "sub").mkdir()
    (tmp_path / "small.txt").write_bytes(b"x" * 10)
    (tmp_path / "sub" / "big.zip").write_bytes(b"x" * 5000)
    (tmp_path / "mid.pdf").write_bytes(b"x" * 2000)
    r = await ListDir().run(ListDirArgs(path=str(tmp_path), sort="size", limit=2, recursive=True))
    lines = r.output.splitlines()
    assert "big.zip" in lines[0] and "4.9 KB" in lines[0] and "mid.pdf" in lines[1]
    assert lines[2] == "…1 more"


def test_start_menu_names_resolve_store_apps():
    from delta.tools.apps import match_start_app

    apps = [{"Name": "Calculator", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
            {"Name": "Calculator Plus", "AppID": "x"}, {"Name": "WhatsApp", "AppID": "y"}]
    assert match_start_app("calculator", apps)["AppID"].startswith("Microsoft.WindowsCalculator")
    assert match_start_app("whats", apps)["Name"] == "WhatsApp"
    assert match_start_app("photoshop", apps) is None
