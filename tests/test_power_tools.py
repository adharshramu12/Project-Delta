"""Power tools: search inside files, exact edits, short Python scripts."""

from delta.tools import power
from delta.tools.base import Risk
from delta.tools.power import EditArgs, EditFile, PythonArgs, RunPython, SearchArgs, SearchFiles, classify_code


async def test_search_finds_text_inside_files(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "rent.txt").write_text("Flat 4B\nRent agreement signed 2026-01-05\n")
    (tmp_path / "notes" / "other.md").write_text("nothing here")
    r = await SearchFiles().run(SearchArgs(path=str(tmp_path), pattern="rent agreement"))
    assert r.ok and "rent.txt:2: Rent agreement signed" in r.output and r.evidence["matches"] == 1


async def test_edit_changes_one_exact_piece_and_keeps_a_backup(tmp_path, monkeypatch):
    backed_up = []
    monkeypatch.setattr(power, "_backup_to_recycle_bin", lambda p: backed_up.append(p.read_text()))
    f = tmp_path / "plan.txt"
    f.write_text("meet at 5 pm\nbring 5 pm notes\n")
    ambiguous = await EditFile().run(EditArgs(path=str(f), old="5 pm", new="6 pm"))
    assert not ambiguous.ok and "2 times" in ambiguous.output and backed_up == []
    r = await EditFile().run(EditArgs(path=str(f), old="meet at 5 pm", new="meet at 6 pm"))
    assert r.ok and f.read_text() == "meet at 6 pm\nbring 5 pm notes\n"
    assert backed_up == ["meet at 5 pm\nbring 5 pm notes\n"]  # the old version is restorable


async def test_python_runs_and_returns_its_output(tmp_path):
    r = await RunPython().run(PythonArgs(code="print(sum(range(1, 101)))", cwd=str(tmp_path)))
    assert r.ok and r.output.splitlines()[-1] == "5050"


def test_a_scripts_risk_comes_from_its_code():
    assert classify_code("print(2 ** 10)") == Risk.WRITE
    assert classify_code("import os\nos.remove('a.txt')") == Risk.DESTRUCTIVE
    assert classify_code("import shutil; shutil.rmtree('x')") == Risk.DESTRUCTIVE
    assert classify_code("import requests; requests.post('https://x.io', data={})") == Risk.EXTERNAL


async def test_search_finds_presentations_by_name(tmp_path):
    (tmp_path / "Work").mkdir()
    (tmp_path / "Work" / "Arva pitch deck.pptx").write_bytes(b"PK\x03\x04binary")
    r = await SearchFiles().run(SearchArgs(path=str(tmp_path), pattern="pitch deck"))
    assert r.ok and "Arva pitch deck.pptx  (file name matches)" in r.output
