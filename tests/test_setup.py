"""First-run setup: user settings overlay, key validation/saving, live apply. No real keys or network."""

import pytest
import yaml
from fastapi.testclient import TestClient

from delta.api.server import create_app, load_token
from delta.bootstrap import build
from delta.config import Settings, deep_merge, load_settings, read_user_settings, write_user_settings
from delta.setup.checks import check_hardware


def test_user_settings_override_shipped_config_without_touching_it(tmp_path, monkeypatch):
    cdir = tmp_path / "config"
    cdir.mkdir()
    (cdir / "delta.yaml").write_text(yaml.safe_dump({"agent": {"name": "Delta", "max_steps": 15}}))
    (cdir / "boundaries.yaml").write_text(yaml.safe_dump({"autonomy": "ask-risky"}))
    data = tmp_path / "data"
    monkeypatch.setenv("DELTA_DATA_DIR", str(data))
    write_user_settings(data, {"agent": {"owner_name": "Ravi"}, "boundaries": {"autonomy": "trusted"}})
    s = load_settings(cdir)
    assert s.agent.owner_name == "Ravi" and s.agent.max_steps == 15  # merged, not replaced
    assert s.boundaries.autonomy == "trusted"
    assert yaml.safe_load((cdir / "boundaries.yaml").read_text())["autonomy"] == "ask-risky"  # repo untouched


def test_deep_merge():
    assert deep_merge({"a": {"b": 1, "c": 2}}, {"a": {"c": 3}}) == {"a": {"b": 1, "c": 3}}


def test_hardware_recommendation_has_text():
    hw = check_hardware(ollama_running=False)
    assert hw.ram_gb > 0 and hw.recommendation


@pytest.fixture
def setup_client(tmp_path, monkeypatch):
    saved: dict[str, str] = {}
    monkeypatch.setattr("delta.api.setup_routes.set_secret", lambda n, v: saved.__setitem__(n, v))
    monkeypatch.setattr("delta.api.setup_routes.get_secret", lambda n: saved.get(n))
    monkeypatch.setattr("delta.bootstrap.get_secret", lambda n: saved.get(n))

    async def fake_validate(name, value):
        ok = value.startswith("good")
        return ok, "Key works." if ok else "The service rejected this key."

    monkeypatch.setattr("delta.api.setup_routes.validate_key", fake_validate)
    c = build(Settings(data_dir=tmp_path))
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        yield tc, c, saved


def test_setup_flow(setup_client, tmp_path):
    tc, c, saved = setup_client
    s = tc.get("/api/setup").json()
    assert s["completed"] is False and "groq_api_key" in s["keys"] and s["hardware"]["ram_gb"] > 0

    bad = tc.post("/api/setup/key", json={"name": "groq_api_key", "value": "typo"}).json()
    assert bad["ok"] is False and saved == {}  # a rejected key is never stored
    assert tc.post("/api/setup/key", json={"name": "evil", "value": "x"}).status_code == 400

    good = tc.post("/api/setup/key", json={"name": "groq_api_key", "value": "good-key"}).json()
    assert good["ok"] is True and saved["groq_api_key"] == "good-key"
    assert "good-key" not in str(good)  # never echoed back

    r = tc.post("/api/setup/complete", json={"owner_name": "Ravi", "autonomy": "ask-everything",
                                             "telegram_enabled": False}).json()
    assert r["ok"] and r["restart_needed"] is False
    assert c.settings.agent.owner_name == "Ravi"
    assert c.orchestrator.executor.policy.b.autonomy == "ask-everything"  # applied live
    stored = read_user_settings(tmp_path)
    assert stored["setup"]["completed"] is True and stored["boundaries"]["autonomy"] == "ask-everything"
    assert tc.get("/api/setup").json()["completed"] is True
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "good-key" not in audit  # keys never reach the audit log


def test_telegram_needs_a_user_id_and_a_restart(setup_client):
    tc, c, _ = setup_client
    assert tc.post("/api/setup/complete", json={"telegram_enabled": True, "telegram_user_ids": []}).status_code == 400
    r = tc.post("/api/setup/complete", json={"telegram_enabled": True, "telegram_user_ids": ["123"]}).json()
    assert r["restart_needed"] is True and c.settings.channels["telegram"].allowed_user_ids == ["123"]
    assert tc.post("/api/setup/complete", json={"autonomy": "reckless"}).status_code == 422
