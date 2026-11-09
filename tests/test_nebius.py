"""Nebius Token Factory: an OpenAI-compatible provider people use with their own Nebius credits."""

import httpx

from delta.config import load_settings
from delta.llm.capabilities import BRAINS
from delta.llm.providers import display_name
from delta.setup import checks
from delta.system.secrets import KNOWN_SECRETS


def test_nebius_is_a_provider_on_every_text_route():
    s = load_settings()
    p = s.llm.providers["nebius"]
    assert p.base_url == "https://api.tokenfactory.nebius.com/v1" and p.secret == "nebius_api_key"
    assert all("nebius" in s.llm.routes[t] for t in ("chat", "plan", "verify"))
    assert display_name("nebius") == "Nebius" and "nebius" in BRAINS
    assert "nebius_api_key" in checks.KEY_INFO and "nebius_api_key" in KNOWN_SECRETS  # Settings, /keys, CLI


async def test_a_nebius_key_is_checked_against_its_models_list(monkeypatch):
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200 if request.headers["authorization"] == "Bearer good" else 401, json={"data": []})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert await checks.validate_key("nebius_api_key", "good") == (True, "Key works.")
    ok, msg = await checks.validate_key("nebius_api_key", "bad")
    assert not ok and "rejected" in msg
    assert seen[0] == ("https://api.tokenfactory.nebius.com/v1/models", "Bearer good")
