"""Verification against a real screenshot, checkpoint by checkpoint, with the proof saved."""

import base64
import json
import os

from fakes import FakeProvider, js

from delta.core.orchestrator import proof_target
from delta.core.proof import ProofRecorder
from delta.core.state import Plan, StepRecord, TaskResult, Verification
from delta.core.verifier import verify
from delta.llm.router import ModelRouter

PLAN = Plan(True, "make notebook", success_criteria=["NotebookLM is open", "user is signed in",
                                                     "a source about the Indian Constitution is listed"])
STEPS = [StepRecord("browser_open", {"url": "https://notebooklm.google.com"}, True, "opened",
                    {"title": "NotebookLM", "browser": "chrome"})]
SHOT = base64.b64encode(b"\xff\xd8fakejpeg").decode()


async def test_checker_sees_the_screenshot_and_judges_each_checkpoint():
    seen = {}

    def handler(task, messages):
        seen.update(task=task, user=messages[1]["content"])
        return js({"checks": [
            {"checkpoint": "NotebookLM is open", "met": True, "seen": "NotebookLM header visible"},
            {"checkpoint": "user is signed in", "met": True, "seen": "account avatar top right"},
            {"checkpoint": "a source about the Indian Constitution is listed", "met": False,
             "seen": "sources panel is empty"}], "passed": True, "reason": "x"})

    router = ModelRouter({"cc": FakeProvider("cc", handler=handler)}, {"vision": ["cc"], "verify": ["cc"]})
    v = await verify(router, "make notebook", PLAN, STEPS, "All done!", SHOT, "NotebookLM")
    assert seen["task"] == "vision" and v.screenshot
    assert [p["type"] for p in seen["user"]] == ["text", "image_url"]
    assert SHOT in seen["user"][1]["image_url"]["url"]
    # One unmet checkpoint fails the task even if the model said passed=true.
    assert not v.passed and v.missing == ["a source about the Indian Constitution is listed"]
    assert v.checks[1]["seen"] == "account avatar top right"


class TextOnly(FakeProvider):
    def supports(self, task):
        return task != "vision"


async def test_without_vision_the_checker_falls_back_to_the_log_and_says_so():
    router = ModelRouter({"g": TextOnly("g", [js({"checks": [], "passed": True, "reason": "ok"})])},
                         {"verify": ["g"]})
    v = await verify(router, "x", PLAN, STEPS, "done", SHOT, "NotebookLM")
    assert v.passed and not v.screenshot


def test_proof_target_is_the_last_window_or_page_worked_in():
    assert proof_target(STEPS) == "NotebookLM"
    assert proof_target([StepRecord("type_text", {"window": "Notepad"}, True, "", {})]) == "Notepad"
    assert proof_target([StepRecord("write_file", {}, True, "", {})]) is None


def test_runs_are_saved_with_screenshots_and_old_ones_pruned(tmp_path):
    rec = ProofRecorder(tmp_path, keep=2, protect_recent_s=0)
    for i in range(3):
        if i:  # older runs get older times: file clocks can be too coarse to tell them apart
            for j in range(i):
                os.utime(tmp_path / f"task{j}", (1000 + j, 1000 + j))
        rec.start(f"task{i}", "make notebook")
        assert rec.save_image(SHOT, "final NotebookLM") == "01-final-notebooklm.jpg"
        rec.finish(TaskResult(f"task{i}", "done", "ok", 1, STEPS,
                              Verification(True, "ok", [], [{"checkpoint": "a", "met": True, "seen": "b"}], True)))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["task1", "task2"]
    run = json.loads((tmp_path / "task2" / "run.json").read_text())
    assert run["verification"]["screenshot"] and run["steps"][0]["tool"] == "browser_open"
    assert rec.files("task2") == ["01-final-notebooklm.jpg"]
    assert rec.save_image(SHOT, "x") is None  # nothing is saved outside a run


async def test_checkpoints_decide_even_if_the_overall_flag_disagrees():
    reply = js({"checks": [{"checkpoint": "song page open", "met": "true", "seen": "title"},
                           {"checkpoint": "playing", "met": True, "seen": "media_status PLAYING"}],
                "passed": False, "reason": "All checkpoints are supported."})
    router = ModelRouter({"g": TextOnly("g", [reply])}, {"verify": ["g"]})
    v = await verify(router, "play", PLAN, STEPS, "done")
    assert v.passed and v.missing == [] and "All checkpoints" in v.raw


def test_an_interrupted_run_is_offered_once_with_its_progress(tmp_path):
    rec = ProofRecorder(tmp_path)
    rec.start("crashed1", "send the report to Ravi")
    rec.checkpoint([StepRecord("open_app", {}, True, "opened", {}), StepRecord("type_text", {}, False, "x", {})])
    rec.dir = None  # Delta stopped here: finish() never ran
    other = ProofRecorder(tmp_path)  # next start of Delta
    run = other.unfinished()
    assert run["request"] == "send the report to Ravi"
    assert [s["tool"] for s in run["steps"]] == ["open_app", "type_text"]
    assert other.close_unfinished("crashed1") and other.unfinished() is None  # offered only once
    assert not other.close_unfinished("../etc")  # ids are checked


def test_finished_runs_are_never_offered(tmp_path):
    rec = ProofRecorder(tmp_path)
    rec.start("ok1", "x")
    rec.finish(TaskResult("ok1", "done", "fine"))
    assert rec.unfinished() is None


def _aged(path, seconds_ago, now):
    os.utime(path, (now - seconds_ago, now - seconds_ago))


def test_pruning_never_deletes_the_current_run_even_when_times_tie(tmp_path):
    """Found on a coarse file clock: every run had the same mtime and the run just started was pruned,
    so its first screenshot failed. The current run is never a candidate."""
    now = 2_000_000.0
    rec = ProofRecorder(tmp_path, keep=1, protect_recent_s=0, now=lambda: now)
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
        _aged(tmp_path / name, 3600, now)  # all the same time
    rec.dir = tmp_path / "b"  # "b" is the live run (e.g. resumed): sorted by name it would go first
    rec._prune()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b"]
    rec.dir = None
    rec.start("zz", "x")
    assert rec.save_image(SHOT, "first") == "01-first.jpg"  # the run just started is still there
    assert sorted(p.name for p in tmp_path.iterdir()) == ["zz"]


def test_pruning_keeps_recently_touched_runs(tmp_path):
    now = 2_000_000.0
    rec = ProofRecorder(tmp_path, keep=1, protect_recent_s=30 * 60, now=lambda: now)
    (tmp_path / "old").mkdir()
    (tmp_path / "busy").mkdir()  # another task may still be writing this one
    _aged(tmp_path / "old", 2 * 3600, now)
    _aged(tmp_path / "busy", 5 * 60, now)
    rec.start("new", "x")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["busy", "new"]


def test_pruning_survives_a_missing_root_and_keep_counts_the_current_run(tmp_path):
    now = 2_000_000.0
    rec = ProofRecorder(tmp_path / "runs", keep=3, protect_recent_s=0, now=lambda: now)
    rec.start("r0", "x")  # the runs folder did not exist yet
    rec.finish(TaskResult("r0", "done", "ok"))
    for i in range(1, 6):
        _aged(tmp_path / "runs" / f"r{i - 1}", 3600 - i, now)
        rec.start(f"r{i}", "x")
        rec.finish(TaskResult(f"r{i}", "done", "ok"))
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == ["r3", "r4", "r5"]
