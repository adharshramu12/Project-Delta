"""Token use per task type stays where this branch put it (evals/token_bench.py, scripted model, no network).

Guards the savings, not exact numbers (Windows registers more tools than other systems, so totals differ):
- "open notepad" needs no model call at all (instant command; open_app proves it by its window),
- an app opened through the agent loop needs no checker call (the tool's proof is the check),
- the planner and actor prompts keep their per-request parts at the end, so most of each request is the same
  between two tasks a minute apart and a provider's prompt cache can reuse it,
- every task type still finishes (quality: the scripted outcome is unchanged).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bench():
    spec = importlib.util.spec_from_file_location("token_bench", ROOT / "evals" / "token_bench.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def test_token_use_per_task_type():
    bench = _bench()
    data = await bench.measure()
    print("\n" + bench.table(data))
    for name, r in data.items():
        assert r["status"].startswith("done"), (name, r["status"])
    assert data["open_app"]["calls"] == 0
    assert "verify" not in data["open_app_planned"]["jobs"]
    assert data["simple_chat"]["calls"] == 1  # the planner answers by itself, as before
    for name in ("browser_read", "multi_step", "try_again"):
        assert data[name]["jobs"]["verify"]["calls"] >= 1, name  # tasks that did not prove themselves are checked
    for name, r in data.items():
        p = r["stable_prefix"]
        if "plan" in p:
            assert p["plan"] >= 0.9 * p["plan_total"], (name, p)
        if "chat" in p:
            assert p["chat"] >= 0.8 * p["chat_total"], (name, p)
