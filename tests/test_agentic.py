"""See-act-check: after each screen action the model is shown the result, sized to its brain."""

import json
from pathlib import Path

import pytest
from fakes import FakeProvider, call, js, text
from pydantic import BaseModel

from delta.config import AgentConfig, Boundaries, MemoryConfig, ProviderConfig, Settings
from delta.core.events import EventBus
from delta.core.orchestrator import Orchestrator
from delta.core.state import TaskRequest
from delta.llm.capabilities import abilities_text
from delta.llm.cli_providers import ClaudeCodeProvider, CodexProvider
from delta.llm.router import ModelRouter
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.registry import ToolExecutor, ToolRegistry


class Args(BaseModel):
    target: str = ""


class FakeClick(Tool[Args]):
    name = "click_element"
    description = "click"
    risk = Risk.READ  # no approval prompt in the test
    Args = Args

    async def run(self, args: Args) -> ToolResult:
        return ToolResult(True, f"clicked {args.target}")


class ReadArgs(BaseModel):
    depth: int = 6


class FakeRead(Tool[ReadArgs]):
    name = "read_window"
    description = "read"
    risk = Risk.READ
    Args = ReadArgs

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, args: ReadArgs) -> ToolResult:
        self.calls += 1
        return ToolResult(True, "WINDOW: Save As\n[Button] Save")


def make(tmp_path: Path, provider: str, script: dict[str, list], seen: list):
    settings = Settings(agent=AgentConfig(max_attempts=1, max_steps=10), memory=MemoryConfig(enabled=False),
                        data_dir=tmp_path)
    queues = {k: list(v) for k, v in script.items()}

    def handler(task, messages):
        seen.append((task, messages))
        return queues[task].pop(0)

    router = ModelRouter({provider: FakeProvider(provider, handler=handler)},
                         {"plan": [provider], "chat": [provider], "verify": [provider]})
    reader = FakeRead()
    executor = ToolExecutor(ToolRegistry([FakeClick(), reader]), PolicyEngine(Boundaries()),
                            ApprovalBroker(timeout_s=1), AuditLog(tmp_path / "a.jsonl"))
    return Orchestrator(settings, router, executor, None, None, EventBus(), AuditLog(tmp_path / "b.jsonl")), reader


PLAN = js({"needs_tools": True, "goal": "save", "steps": ["click Save"], "tools": ["click_element"],
           "success_criteria": ["saved"]})


async def test_each_click_is_followed_by_what_the_screen_shows(tmp_path):
    seen: list = []
    orch, reader = make(tmp_path, "claude_code", {
        "plan": [PLAN], "chat": [call("click_element", target="File"), text("DONE: Done")],
        "verify": [js({"passed": True, "reason": "ok"})]}, seen)
    r = await orch.run(TaskRequest("save it"))
    assert r.status == "done" and reader.calls == 1
    tool_msg = next(m for task, msgs in seen if task == "chat" for m in msgs if m.get("role") == "tool")
    assert "AFTER (read_window):" in tool_msg["content"] and "[Button] Save" in tool_msg["content"]
    assert [s.tool for s in r.steps] == ["click_element", "read_window"]  # the verifier sees it too


async def test_frontier_brains_get_more_steps_and_know_it(tmp_path):
    seen: list = []
    orch, _ = make(tmp_path, "claude_code", {"plan": [PLAN], "chat": [text("DONE: Done")],
                                             "verify": [js({"passed": True, "reason": "ok"})]}, seen)
    await orch.run(TaskRequest("x"))
    system = next(msgs for task, msgs in seen if task == "chat")[0]["content"]
    assert "Claude Code" in system and "up to 20 actions" in system and "AFTER" in system
    small, _ = make(tmp_path, "lmstudio", {}, [])
    assert small._step_budget() == 8


def test_abilities_mention_sight_only_when_available():
    assert "CAN see the screen" in abilities_text("codex", True, 30)
    assert "cannot see images" in abilities_text("ollama", False, 12)


async def test_screenshots_reach_claude_code_and_codex(tmp_path):
    shot = {"role": "user", "content": [{"type": "text", "text": "where is Save?"},
                                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/AA=="}}]}
    claude = ClaudeCodeProvider("c", ProviderConfig(kind="claude_code", models={"chat": "haiku"}))
    claude._path = "claude"
    assert claude.supports("vision")
    got = {}

    class Sess:
        sent, system, last_used, closed = 0, None, 0.0, False

        def alive(self):
            return True

        def continues(self, messages):
            return False

        async def ask(self, line, limit_s):
            got["event"] = json.loads(line)
            return {"type": "result", "is_error": False, "result": "top right"}

        def close(self):
            pass

    async def spawn(model, effort):
        return Sess()

    claude._spawn = spawn
    assert (await claude.complete("vision", [shot], None)).content == "top right"
    assert "stream-json" in claude.session_args("claude", "haiku", "")
    event = got["event"]
    assert [b["type"] for b in event["message"]["content"]] == ["text", "image"]
    assert "where is Save?" in event["message"]["content"][0]["text"]
    got.clear()

    codex = CodexProvider("x", ProviderConfig(kind="codex", models={"chat": ""}))
    codex._path = "codex"

    async def run2(cmd, prompt, cwd):
        got.update(cmd=cmd, files=[Path(cmd[i + 1]).read_bytes() for i, a in enumerate(cmd) if a == "--image"])
        return "top right", "", 0

    codex._run = run2
    await codex.complete("vision", [shot], None)
    assert got["files"] == [b"\xff\xd8\xff\x00"] and got["cmd"][-2:] == ["--", "-"]


async def test_a_progress_note_does_not_end_the_task(tmp_path):
    """The NotebookLM bug: after the sign-in check the model wrote a note instead of acting."""
    seen: list = []
    orch, _ = make(tmp_path, "claude_code", {
        "plan": [PLAN],
        "chat": [text("Signed in as RAMU. Next I will click New notebook."),  # used to end the task here
                 call("click_element", target="New notebook"), text("DONE: Notebook created")],
        "verify": [js({"passed": True, "reason": "ok"})]}, seen)
    r = await orch.run(TaskRequest("make a notebook"))
    assert r.status == "done" and r.answer == "Notebook created"
    assert [s.tool for s in r.steps] == ["click_element", "read_window"]
    nudge = [m for task, msgs in seen if task == "chat" for m in msgs if "Not finished yet" in str(m.get("content"))]
    assert nudge


async def test_need_user_asks_instead_of_burning_retries(tmp_path):
    orch, _ = make(tmp_path, "claude_code", {
        "plan": [PLAN], "chat": [text("NEED_USER: NotebookLM shows a Google sign-in page. Please sign in.")]}, [])
    r = await orch.run(TaskRequest("make a notebook"))
    assert r.status == "needs_user" and r.answer.startswith("NotebookLM shows")


async def test_unmarked_answers_are_accepted_after_a_few_nudges(tmp_path):
    """Small local models may never learn the markers: don't loop forever."""
    orch, _ = make(tmp_path, "lmstudio", {
        "plan": [PLAN], "chat": [text("ok")] * 4, "verify": [js({"passed": False, "reason": "no"})]}, [])
    orch.s.agent.max_attempts = 1
    r = await orch.run(TaskRequest("x"))
    assert r.status == "failed"


async def test_mouse_use_pauses_then_resumes(monkeypatch):
    from delta.tools import desktop

    ctl = desktop.DesktopController()
    moves = iter([(500, 500)] * 3 + [(900, 900)] * 3 + [(901, 900)] * 40)
    ctl.position = lambda: next(moves)
    real_sleep = desktop.asyncio.sleep
    monkeypatch.setattr(desktop.asyncio, "sleep", lambda s: real_sleep(0))
    assert await ctl.wait_until_idle(quiet_s=0, max_s=5)
    assert ctl._last_pos is None and ctl._paused_until == 0


def test_typing_a_newline_needs_the_same_approval_as_enter(monkeypatch):
    from delta.tools import desktop
    from delta.tools.base import Risk as R
    from delta.tools.desktop import TypeArgs, TypeText

    # Risk depends on the app being typed into; pin it, or a chat app in front makes this flaky.
    monkeypatch.setattr(desktop, "_title_of", lambda window: "Notepad")

    t = TypeText.__new__(TypeText)
    assert t.risk_for(TypeArgs(text="Indian Constitution podcast\n")) == R.WRITE
    assert t.risk_for(TypeArgs(text="just text")) == R.ACT


async def test_playback_tasks_get_a_fresh_media_reading_before_the_check(tmp_path):
    from delta.core.state import StepRecord as SR

    orch, _ = make(tmp_path, "claude_code", {}, [])
    readings = iter(["PLAYING: \"Oh Sita Hey Rama\""])

    class Exec:
        async def execute(self, name, args, task_id=None):
            assert name == "media_status"
            return ToolResult(True, next(readings), {"playing": ["Oh Sita Hey Rama"]})

    orch.executor = Exec()
    steps = [SR("media_status", {}, True, 'PLAYING: "Ad"', {})]
    await orch._fresh_media_status("t", steps)
    assert steps[-1].output.endswith('"Oh Sita Hey Rama"') and steps[-1].args == {"auto": "before final check"}
    no_media = [SR("click_element", {}, True, "", {})]
    await orch._fresh_media_status("t", no_media)
    assert len(no_media) == 1  # nothing to do for tasks without playback


async def test_json_protocol_final_counts_as_done(tmp_path):
    """Claude Code/Codex end with {"final": ...}: that is a finish, not a progress note."""
    from delta.llm.schemas import LLMResponse

    orch, _ = make(tmp_path, "claude_code", {
        "plan": [PLAN], "chat": [call("click_element", target="Play"),
                                 LLMResponse(content="Song is playing", final=True)],
        "verify": [js({"passed": True, "reason": "ok"})]}, [])
    r = await orch.run(TaskRequest("play a song"))
    assert r.status == "done" and r.answer == "Song is playing"


class PlayArgs(BaseModel):
    query: str


class FakePlay(Tool[PlayArgs]):
    name = "play_youtube"
    description = "play"
    risk = Risk.READ
    Args = PlayArgs

    def __init__(self, proved=True):
        self.proved = proved

    async def run(self, args: PlayArgs) -> ToolResult:
        return ToolResult(True, f'PLAYING on YouTube: "{args.query}"', {"verified": self.proved})


def make_direct(tmp_path, proved, script):
    seen: list = []
    orch, _ = make(tmp_path, "claude_code", script, seen)
    orch.executor.registry.register(FakePlay(proved))
    return orch, seen


DIRECT = js({"needs_tools": True, "goal": "play", "steps": ["play it"], "success_criteria": ["song is playing"],
             "direct": {"tool": "play_youtube", "args": {"query": "Kaanunna Kalyanam"}}})


async def test_one_call_job_with_proof_needs_only_the_planner(tmp_path):
    orch, seen = make_direct(tmp_path, True, {"plan": [DIRECT]})
    r = await orch.run(TaskRequest("could you get Kaanunna Kalyanam going"))  # not an instant command
    assert r.status == "done" and r.fast and r.verification.passed
    assert [task for task, _ in seen] == ["plan"]  # no agent loop, no checker, no learning calls
    assert r.verification.checks[0]["checkpoint"] == "song is playing"


async def test_direct_call_without_proof_hands_over_to_the_agent(tmp_path):
    orch, seen = make_direct(tmp_path, False, {
        "plan": [DIRECT], "chat": [text("DONE: playing")], "verify": [js({"passed": True, "reason": "ok"})]})
    r = await orch.run(TaskRequest("play it"))
    assert r.status == "done" and not r.fast
    chat = next(msgs for task, msgs in seen if task == "chat")
    assert any("Already done: play_youtube" in str(m.get("content")) for m in chat)


async def test_instant_commands_cost_zero_ai_calls(tmp_path):
    orch, seen = make_direct(tmp_path, True, {})
    r = await orch.run(TaskRequest("Play Kaanunna Kalyanam on YouTube"))
    assert r.status == "done" and r.fast and seen == []  # not even a planner call
    assert r.steps[0].args == {"query": "Kaanunna Kalyanam"}


def test_instant_command_patterns_are_strict():
    from delta.core.intents import match_intent

    tools = {"media_control", "media_status", "play_youtube", "start_meeting"}
    assert match_intent("pause the music", tools).args == {"action": "pause"}
    assert match_intent("next song", tools).args == {"action": "next"}
    assert match_intent("please play oh sita hey rama song", tools).args == {"query": "oh sita hey rama song"}
    assert match_intent("Start a Google Meet", tools).args == {"platform": "google_meet"}
    assert match_intent("create an instant zoom meeting", tools).args == {"platform": "zoom"}
    for vague in ["play chess with me", "schedule a meeting tomorrow at 5 with Ravi", "stop", "open meet notes"]:
        assert match_intent(vague, tools) is None, vague
    assert match_intent("pause the music", {"media_status"}) is None  # tool not installed


async def test_unknown_argument_names_are_rejected_with_a_hint(tmp_path):
    """A guest list sent as 'attendees' must not silently vanish."""
    from delta.tools.registry import unknown_arguments

    orch, _ = make(tmp_path, "claude_code", {}, [])
    orch.executor.registry.register(FakePlay())
    r = await orch.executor.execute("play_youtube", {"querry": "x"})
    assert not r.ok and "did you mean 'query'" in r.output
    assert unknown_arguments(PlayArgs, {"query": "x"}) == ""

    from delta.tools.meetings import ScheduleArgs
    assert unknown_arguments(ScheduleArgs, {"title": "t", "start": "s", "attendees": []}) == ""  # an alias
    assert "did you mean 'guests'" in unknown_arguments(ScheduleArgs, {"title": "t", "start": "s", "guest": []})


async def test_stopping_a_task_closes_its_record_and_tells_the_ui(tmp_path):
    import asyncio

    from delta.core.proof import ProofRecorder

    class Slow(Tool[Args]):
        name, description, risk, Args = "slow_tool", "slow", Risk.READ, Args

        async def run(self, args):
            await asyncio.sleep(30)
            return ToolResult(True, "never")

    seen: list = []
    plan = js({"needs_tools": True, "goal": "x", "steps": ["x"], "tools": ["slow_tool"], "success_criteria": ["x"]})
    orch, _ = make(tmp_path, "claude_code", {"plan": [plan], "chat": [call("slow_tool", target="x")]}, seen)
    orch.executor.registry.register(Slow())
    orch.proof = ProofRecorder(tmp_path / "runs")
    finished = []
    async def on_event(event, data):
        if event == "task.finished":
            finished.append((event, data.get("status")))

    orch.events.subscribe(on_event)
    req = TaskRequest("click x")
    t = asyncio.create_task(orch.run(req))
    await asyncio.sleep(0.2)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert ("task.finished", "cancelled") in finished
    assert (tmp_path / "runs" / req.id / "run.json").exists()
