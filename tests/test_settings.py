"""Settings page API and the protected locations no setting can unlock."""

import pytest
from fastapi.testclient import TestClient

from delta.api.server import create_app, load_token
from delta.bootstrap import build, make_policy
from delta.config import Boundaries, Settings, read_user_settings
from delta.safety.policy import PolicyEngine, Verdict
from delta.tools.filesystem import PathArgs, ReadFile, WriteArgs, WriteFile
from delta.tools.shell import RunShell, ShellArgs

# --- protected locations ---------------------------------------------------------------------------


def test_delta_cannot_read_its_own_token_or_rewrite_its_rules(tmp_path):
    data, cfg = tmp_path / "data", tmp_path / "config"
    pe = PolicyEngine(Boundaries(autonomy="trusted", allowed_paths=[str(tmp_path)]),
                      protected=[data], read_only=[cfg])
    assert pe.evaluate(ReadFile(), PathArgs(path=str(data / "api_token"))).verdict == Verdict.DENY
    assert pe.evaluate(WriteFile(), WriteArgs(path=str(data / "settings.yaml"), content="x")).verdict == Verdict.DENY
    shell = pe.evaluate(RunShell(), ShellArgs(command=f'Get-Content "{data / "api_token"}"'))
    assert shell.verdict == Verdict.DENY  # caught inside free text too
    assert pe.evaluate(ReadFile(), PathArgs(path=str(cfg / "boundaries.yaml"))).verdict == Verdict.ALLOW
    edit = pe.evaluate(WriteFile(), WriteArgs(path=str(cfg / "boundaries.yaml"), content="autonomy: trusted"))
    assert edit.verdict == Verdict.DENY and "read-only" in edit.reason


def test_blocked_folder_cannot_be_read_through_the_shell(tmp_path, monkeypatch):
    """Live finding: after blocking a folder, the model read the file with `Get-Content` instead."""
    private = tmp_path / "private"
    monkeypatch.setenv("DELTATESTROOT", str(tmp_path))
    pe = PolicyEngine(Boundaries(denied_paths=[str(private)], denied_domains=["blocked.example"]))
    for cmd in (f'Get-Content "{private / "diary.txt"}"',
                f"type {str(private).replace(chr(92), '/')}/diary.txt",
                r'Get-Content "$env:DELTATESTROOT\private\diary.txt"',
                r"type %DELTATESTROOT%\private\diary.txt"):
        assert pe.evaluate(RunShell(), ShellArgs(command=cmd)).verdict == Verdict.DENY, cmd
    assert pe.evaluate(RunShell(), ShellArgs(command="curl https://blocked.example/x")).verdict == Verdict.DENY
    assert pe.evaluate(RunShell(), ShellArgs(command="Get-ChildItem C:\\Users")).verdict == Verdict.ALLOW
    assert pe.evaluate(RunShell(), ShellArgs(command="echo notblocked.example.org")).verdict != Verdict.DENY


def test_system_folders_are_always_protected(tmp_path):
    pe = PolicyEngine(Boundaries(denied_paths=[]))  # user removed every denied folder
    assert pe.evaluate(ReadFile(), PathArgs(path="~/.ssh/id_rsa")).verdict == Verdict.DENY
    assert pe.evaluate(ReadFile(), PathArgs(path=r"C:\Windows\System32\config\SAM")).verdict == Verdict.DENY


def test_built_policy_protects_data_dir(tmp_path):
    policy = make_policy(Settings(data_dir=tmp_path / "d", config_dir=tmp_path / "c"))
    assert policy.evaluate(ReadFile(), PathArgs(path=str(tmp_path / "d" / "audit.jsonl"))).verdict == Verdict.DENY


# --- settings API ----------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    removed = []
    monkeypatch.setattr("delta.api.settings_routes.delete_secret", removed.append)
    monkeypatch.setattr("delta.api.settings_routes.get_secret", lambda n: "x" if n == "groq_api_key" else None)
    c = build(Settings(data_dir=tmp_path / "data", config_dir=tmp_path / "config"))
    token = load_token(c.settings.data_dir)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        yield tc, c, removed


def test_get_settings(client):
    tc, _, _ = client
    s = tc.get("/api/settings").json()
    assert s["boundaries"]["autonomy"] == "ask-risky" and "mcp" in s["addons"]["available"]
    assert s["keys"]["groq_api_key"]["set"] is True and s["save_dir_resolved"]


def test_blocking_a_folder_applies_to_the_next_action(client, tmp_path):
    tc, c, _ = client
    secret = tmp_path / "private"
    before = c.orchestrator.executor.policy.evaluate(ReadFile(), PathArgs(path=str(secret / "a.txt")))
    assert before.verdict == Verdict.ALLOW
    r = tc.put("/api/settings", json={"boundaries": {"denied_paths": [str(secret), "  ", str(secret)]}}).json()
    assert r["ok"] and r["restart_needed"] is False
    after = c.orchestrator.executor.policy.evaluate(ReadFile(), PathArgs(path=str(secret / "a.txt")))
    assert after.verdict == Verdict.DENY  # live, no restart
    assert read_user_settings(c.settings.data_dir)["boundaries"]["denied_paths"] == [str(secret)]  # cleaned


def test_memory_switches_and_addon_restart(client):
    tc, c, _ = client
    r = tc.put("/api/settings", json={"memory": {"learn_skills": False}, "addons_enabled": ["mcp"]}).json()
    assert r["restart_needed"] is True and c.settings.memory.learn_skills is False
    assert tc.put("/api/settings", json={"addons_enabled": ["not-installed"]}).status_code == 400


def test_validation_rejects_dangerous_or_broken_values(client):
    tc, c, _ = client
    assert tc.put("/api/settings", json={"boundaries": {"allowed_paths": ["C:\\"]}}).status_code == 400
    bad_time = {"boundaries": {"quiet_hours": {"enabled": True, "start": "late", "end": "07:00"}}}
    assert tc.put("/api/settings", json=bad_time).status_code == 400
    assert tc.put("/api/settings", json={"save_dir": str(c.settings.data_dir / "x")}).status_code == 400
    assert tc.put("/api/settings", json={"boundaries": {"autonomy": "yolo"}}).status_code == 422


def test_quiet_hours_and_save_dir_apply(client, tmp_path):
    tc, c, _ = client
    out = tmp_path / "MyFiles"
    r = tc.put("/api/settings", json={"save_dir": str(out),
                                      "boundaries": {"quiet_hours": {"enabled": True, "start": "22:00",
                                                                     "end": "06:00"}}}).json()
    assert r["save_dir_resolved"] == str(out)
    assert c.settings.boundaries.quiet_hours.enabled is True
    w = c.orchestrator.executor.policy.evaluate(WriteFile(), WriteArgs(path=str(out / "n.txt"), content="x"))
    assert w.verdict != Verdict.DENY  # new save folder is allowed


def test_remove_key(client):
    tc, _, removed = client
    assert tc.delete("/api/settings/key/groq_api_key").json()["ok"] and removed == ["groq_api_key"]
    assert tc.delete("/api/settings/key/../../etc").status_code in (400, 404)
