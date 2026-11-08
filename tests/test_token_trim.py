"""Fewer tokens per step, same information: compact tool signatures for prompt-protocol models (Codex, Claude Code,
Cline, local), lean schemas for API tool calling, and no rules about tools a task is not given."""

from __future__ import annotations

import json

import pytest

from delta.llm.providers import inject_tool_prompt, lean_schema, tool_lines, wire_tools

SPEC = {"type": "function", "function": {
    "name": "browser_click",
    "description": "Click a link or button.",
    "parameters": {"description": "Delta browser's click.", "title": "Click", "type": "object",
                   "properties": {
                       "text": {"default": "", "description": "Visible text", "type": "string", "title": "Text"},
                       "ref": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": None,
                               "description": "Element number"},
                       "sort": {"default": "name", "enum": ["name", "size"], "type": "string"},
                       "limit": {"default": 200, "minimum": 1, "maximum": 500, "type": "integer"},
                       "direction": {"default": "down", "pattern": "^(up|down)$", "type": "string"},
                       "items": {"type": "array", "items": {"type": "object", "properties": {
                           "x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["x"]}},
                       "url": {"type": "string"}},
                   "required": ["url"]}}}


def test_lean_schema_keeps_meaning() -> None:
    lean = lean_schema(SPEC["function"]["parameters"])
    assert "title" not in json.dumps(lean) and "description" not in {k for k in lean if k != "properties"}
    assert lean["properties"]["ref"] == {"type": "integer", "description": "Element number"}
    assert lean["required"] == ["url"]
    assert lean["properties"]["sort"]["enum"] == ["name", "size"]
    wired = wire_tools([SPEC])
    assert wired is not None and wired[0]["function"]["name"] == "browser_click"
    assert SPEC["function"]["parameters"]["title"] == "Click"  # the registry's own spec is untouched


def test_tool_lines_carry_every_argument() -> None:
    text = tool_lines([SPEC])
    first = text.splitlines()[0]
    assert first.startswith("- browser_click(")
    for part in ('text?: string', "ref?: integer", 'sort: "name"|"size"="name"', "limit: integer=200",
                 'direction: "up"|"down"="down"', "items?: list[{x: integer, y?: integer}]", "url: string"):
        assert part in first, part
    assert "Click a link or button." in first
    assert "    ref: Element number" in text
    assert "    limit: at least 1, at most 500" in text
    assert len(text) < len(json.dumps([SPEC["function"]])) * 0.7


def test_prompt_protocol_still_asks_for_json_calls() -> None:
    msgs = inject_tool_prompt([{"role": "system", "content": "S"}, {"role": "user", "content": "u"}], [SPEC])
    system = msgs[0]["content"]
    assert system.startswith("S\n\n") and "- browser_click(" in system
    assert '{"tool_calls": [{"name": "<tool name>", "arguments": {...}}]}' in system
    assert '{"final": "<your answer to the user>"}' in system


def test_rules_for_tools_not_sent_are_left_out() -> None:
    from delta.core.orchestrator import ACTOR_PROMPT, fit_rules

    rules = ACTOR_PROMPT.split("Rules:", 1)[1]
    files_only = fit_rules(rules, {"read_file", "write_file", "path_info"})
    assert "- Websites:" not in files_only and "browser_fill with submit" not in files_only
    assert "- Messages in WhatsApp" not in files_only and "- Email:" not in files_only
    # Safety and asking rules always stay.
    for keep in ("- If a tool says BLOCKED", "- Never ask for or handle passwords", "- KEEP GOING",
                 "- NEVER GUESS", "NEED_USER:", "    - [ ] <tick box", "{style}"):
        assert keep in files_only, keep
    web = fit_rules(rules, {"browser_open", "browser_read"})
    assert "- Websites:" in web and "To search INSIDE a site" in web
    everything = fit_rules(rules, {"browser_open", "send_file", "set_volume", "run_python", "read_window",
                                   "click_at", "media_status", "send_message", "send_email"})
    assert everything == rules


def test_screen_lines_need_screen_tools() -> None:
    from delta.core.orchestrator import fit_rules
    from delta.llm.capabilities import abilities_text

    text = abilities_text("codex", False, 12)
    assert "Mouse work:" not in fit_rules(text, {"read_file"})
    assert "EVERY mouse/keyboard/browser action" not in fit_rules(text, {"read_file"})
    assert "BE FAST" in fit_rules(text, {"read_file"})
    assert "Mouse work:" in fit_rules(text, {"do_actions"})
    assert "EVERY mouse/keyboard/browser action" in fit_rules(text, {"browser_open"})


def test_token_bench_before_and_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """evals/token_bench.py with these cuts and without them, in the same run on the same PC (Windows sends more
    tools and longer paths than Linux, so absolute numbers differ by platform): the cuts must save at least 8% of
    the API input and 12% of what a CLI provider is sent. Measured on Linux: 44,766 -> 39,729 tokens (-11%) and
    177,019 -> 145,443 CLI characters (-18%)."""
    import asyncio
    import sys
    from pathlib import Path

    from delta.core import orchestrator
    from delta.llm import providers

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "evals"))
    try:
        import token_bench
    finally:
        sys.path.remove(str(root / "evals"))
    after = asyncio.run(token_bench.measure())
    monkeypatch.setattr(orchestrator, "fit_rules", lambda system, tools: system)
    monkeypatch.setattr(providers, "wire_tools", lambda tools: tools)
    monkeypatch.setattr(providers, "tool_lines", lambda tools: json.dumps([t["function"] for t in tools or []],
                                                                          separators=(",", ":")))
    before = asyncio.run(token_bench.measure())

    def total(data: dict, key: str) -> int:
        return sum(r[key] for r in data.values())

    assert all(r["status"].startswith("done") for r in after.values())
    assert total(after, "tokens") <= total(before, "tokens") * 0.92, (total(before, "tokens"), total(after, "tokens"))
    assert total(after, "cli_chars") <= total(before, "cli_chars") * 0.88, (total(before, "cli_chars"),
                                                                            total(after, "cli_chars"))
