"""The eval harness: specs, deterministic checkers, the scripted model and the report."""

import argparse
import json
from pathlib import Path

import pytest
from engine_helpers import no_registry

from delta.core.state import StepRecord
from delta.evals.checkers import CHECKS, CheckContext, run_checks
from delta.evals.cli import run_cli
from delta.evals.fixtures import FixtureSite
from delta.evals.runner import SITE_DIR, SPECS_DIR, markdown, run_suite, summarise, write_report
from delta.evals.scripted import ScriptedProvider
from delta.evals.spec import SUITES, SpecError, fill, load_specs


def test_the_suite_is_30_to_50_tasks_over_every_area():
    tasks = load_specs(SPECS_DIR)
    assert 30 <= len(tasks) <= 50
    assert {t.suite for t in tasks} == set(SUITES)
    for t in tasks:
        assert t.script.get("plan"), f"{t.id} has no scripted plan"
        for item in t.check:
            (name,) = item
            assert name in CHECKS, f"{t.id}: unknown check {name}"


def test_bad_specs_are_refused(tmp_path):
    (tmp_path / "x.yaml").write_text("- id: a\n  request: r\n  check: [{status: done}]\n- id: a\n  request: r\n"
                                     "  check: [{status: done}]\n")
    with pytest.raises(SpecError):
        load_specs(tmp_path)
    (tmp_path / "x.yaml").write_text("- id: a\n  request: r\n  check: []\n")
    with pytest.raises(SpecError):
        load_specs(tmp_path)


def test_placeholders():
    assert fill({"{sandbox}/a": ["{site}/b", 3]}, {"sandbox": "S", "site": "W"}) == {"S/a": ["W/b", 3]}


def test_checkers_read_real_state(tmp_path):
    (tmp_path / "a.txt").write_text("Hello World")
    (tmp_path / "s.json").write_text(json.dumps({"x": {"y": [1, 2]}}))
    ctx = CheckContext(tmp_path, "done", "The answer is 42.",
                       [StepRecord("read_file", {}, True, "contents: Hello", {}),
                        StepRecord("run_python", {}, False, "timed out after 3s", {})],
                       page_state={"submit.name": "Ada"}, memories=["likes teal"])
    good = [{"status": "done"}, {"file_exists": "a.txt"}, {"file_exists": "*.txt"}, {"file_absent": "b.txt"},
            {"file_contains": {"path": "a.txt", "text": ["hello", "world"]}},
            {"file_equals": {"path": "a.txt", "text": "Hello World"}},
            {"json_field": {"path": "s.json", "field": "x.y.1", "equals": 2}}, {"answer_contains": ["41", "42"]},
            {"answer_not_contains": "secret"}, {"page_state": {"submit.name": "Ada"}}, {"memory_contains": "TEAL"},
            {"steps_include": ["read_file"]}, {"max_steps": 2}, {"steps_ok": False},
            {"step_output_contains": {"tool": "read_file", "text": "hello"}}, {"any_output_contains": "timed out"},
            {"step_failed_with": {"tool": "run_python", "text": "timed out"}}, {"no_process": "no-such-marker-zz"}]
    results = run_checks(ctx, good)
    assert all(r["passed"] for r in results), [r for r in results if not r["passed"]]
    bad = [{"status": "failed"}, {"file_exists": "zz.txt"}, {"file_contains": {"path": "a.txt", "text": "bye"}},
           {"answer_contains": "43"}, {"page_state": {"submit.name": "Bob"}}, {"steps_ok": True},
           {"nonsense": 1}, {"json_field": {"path": "s.json", "field": "nope", "equals": 1}},
           {"events_recorded": {"min_steps": 1}}]
    assert not any(r["passed"] for r in run_checks(ctx, bad))


async def test_scripted_provider_plays_the_script():
    p = ScriptedProvider({"plan": [{"needs_tools": True}], "chat": [{"calls": [{"tool": "a", "args": {"x": 1}},
                                                                               {"tool": "b"}]}, "DONE: ok"]})
    plan = await p.complete("plan", [], None, True)
    assert json.loads(plan.content) == {"needs_tools": True}
    step = await p.complete("chat", [], None)
    assert [c.name for c in step.tool_calls] == ["a", "b"] and step.tool_calls[0].arguments == {"x": 1}
    assert (await p.complete("chat", [], None)).content == "DONE: ok"
    assert (await p.complete("chat", [], None)).content.startswith("DONE:")
    assert json.loads((await p.complete("verify", [], None)).content)["passed"] is True


def test_fixture_site_records_page_state():
    import urllib.request

    with FixtureSite(SITE_DIR) as site:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        assert b"Kestrel" in opener.open(site.url + "/article.html").read()
        opener.open(site.url + "/submit?name=Ada")
        opener.open(site.url + "/api/mark?clicked=yes")
        assert site.snapshot() == {"submit.name": "Ada", "clicked": "yes"}
        site.reset()
        assert site.snapshot() == {}


async def test_scripted_suite_runs_and_passes(tmp_path, monkeypatch):
    no_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    tasks = [t for t in load_specs(SPECS_DIR) if not t.needs]
    report = await run_suite(tasks, log=lambda *_: None)
    failed = [(r["id"], r["checks"], r["error"]) for r in report["results"] if not r["passed"] and not r["skipped"]]
    assert not failed, failed
    assert report["ran"] == len(tasks) and report["success_rate"] == 1.0
    assert report["tokens_in"] > 0 and report["cost_usd"] == 0.0
    md = markdown(report)
    assert "| Task | Result |" in md and "files-create-note" in md
    j, m = write_report(report, tmp_path / "out")
    assert json.loads(j.read_text())["passed"] == report["passed"] and m.read_text().startswith("# Delta eval")


def test_a_failing_task_shows_in_the_report():
    from delta.evals.runner import TaskOutcome

    report = summarise([TaskOutcome("a", "files", status="done", passed=True),
                        TaskOutcome("b", "files", status="failed", checks=[{"check": "status", "passed": False,
                                                                             "detail": "status failed"}]),
                        TaskOutcome("c", "apps", status="skipped", skipped="needs Windows")], "scripted", 1.0)
    assert (report["ran"], report["passed"], report["skipped"], report["success_rate"]) == (2, 1, 1, 0.5)
    md = markdown(report)
    assert "| b | FAIL |" in md and "status failed" in md and "| c | skipped |" in md


def test_cli_lists_tasks(capsys):
    args = argparse.Namespace(action="list", suite=["memory"], task=None, model=None, scripted=False, specs=None,
                              out=None, keep=False, min_success=None)
    assert run_cli(args) == 0
    out = capsys.readouterr().out
    assert "mem-remember-colour" in out and "files-create-note" not in out
    args.specs = str(Path("/no/such/folder"))
    assert run_cli(args) == 2
