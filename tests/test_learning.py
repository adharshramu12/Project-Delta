"""Learning from the user's approvals, denials and corrections."""

import asyncio

from fakes import FakeProvider, js
from test_orchestrator import make

from delta.config import Boundaries
from delta.core.state import TaskRequest
from delta.learning.corrections import learn_corrections, sounds_like_instruction, standing_instructions
from delta.learning.store import LearningStore
from delta.llm.router import ModelRouter
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.tools.filesystem import filesystem_tools
from delta.tools.registry import ToolExecutor, ToolRegistry

# --- store rules ---------------------------------------------------------------------------------


def test_two_denials_teach_avoid_and_undo_is_permanent(tmp_path):
    s = LearningStore(tmp_path / "l.db")
    key = r"delete_path|destructive|c:\users\x\downloads"
    assert s.record_decision(key, "destructive", "recycle a.zip", False) is None
    item = s.record_decision(key, "destructive", "recycle b.zip", False)
    assert item and item.kind == "avoid" and item.status == "active"
    s.set_status(item.id, "undone")
    for _ in range(3):
        assert s.record_decision(key, "destructive", "recycle c.zip", False) is None  # never re-learned


def test_trust_is_only_suggested_and_never_for_deletes(tmp_path):
    s = LearningStore(tmp_path / "l.db")
    write = r"write_file|write|c:\users\x\delta"
    out = [s.record_decision(write, "write", "create a.txt", True) for _ in range(3)]
    assert out[:2] == [None, None] and out[2] and out[2].kind == "trust" and out[2].status == "pending"
    delete = r"delete_path|destructive|c:\users\x\delta"
    assert all(s.record_decision(delete, "destructive", "recycle", True) is None for _ in range(5))


def test_mixed_decisions_teach_nothing(tmp_path):
    s = LearningStore(tmp_path / "l.db")
    k = "run_shell|write|npm"
    for approved in (True, False, True, False):
        assert s.record_decision(k, "write", "npm i", approved) is None


# --- executor behaviour ----------------------------------------------------------------------------


def executor(tmp_path, answer):
    broker = ApprovalBroker(timeout_s=0.3)
    asked = []

    async def present(req):
        asked.append(req.summary)
        if answer is not None:
            asyncio.get_running_loop().call_soon(broker.resolve, req.id, answer, "user")

    broker.add_presenter("t", present)
    store = LearningStore(tmp_path / "l.db")
    learned = []

    async def on_learned(item):
        learned.append(item)

    ex = ToolExecutor(ToolRegistry(filesystem_tools()), PolicyEngine(Boundaries()), broker,
                      AuditLog(tmp_path / "a.jsonl"), store, on_learned)
    return ex, store, asked, learned


async def test_learned_avoid_blocks_without_asking(tmp_path):
    ex, _, asked, learned = executor(tmp_path, answer=False)
    for name in ("a", "b"):
        (tmp_path / f"{name}.txt").write_text("x")
        r = await ex.execute("delete_path", {"path": str(tmp_path / f"{name}.txt")}, task_id=f"t{name}")
        assert not r.ok
    assert len(asked) == 2 and learned and learned[0].kind == "avoid"
    (tmp_path / "c.txt").write_text("x")
    r = await ex.execute("delete_path", {"path": str(tmp_path / "c.txt")}, task_id="tc")
    assert not r.ok and "refused this kind of action before" in r.output
    assert r.evidence["user_denied"] is True  # the orchestrator stops the task, like a fresh "no"
    assert len(asked) == 2  # not asked a third time
    assert (tmp_path / "c.txt").exists()


async def test_avoid_applies_to_every_tool_in_that_folder(tmp_path):
    """Live finding: after 'avoid deleting here' was learned, the model used Remove-Item instead."""
    from delta.tools.shell import RunShell

    ex, store, asked, _ = executor(tmp_path, answer=True)
    ex.registry.register(RunShell())
    folder = str(tmp_path).lower()
    store.add("avoid", f"delete_path|destructive|{folder}", "avoid deleting here")
    (tmp_path / "x.log").write_text("keep")
    shell = await ex.execute("run_shell", {"command": f'Remove-Item -Path "{tmp_path / "x.log"}" -Force'},
                             task_id="a")
    move = await ex.execute("move_path", {"source": str(tmp_path / "x.log"),
                                          "destination": str(tmp_path / "x.log")}, task_id="b")
    assert "refused this kind of action before" in shell.output
    assert "refused this kind of action before" in move.output  # move onto existing = destructive
    assert asked == [] and (tmp_path / "x.log").exists()


async def test_trust_covers_folder_writes_but_never_the_shell(tmp_path):
    from delta.tools.shell import RunShell

    ex, store, asked, _ = executor(tmp_path, answer=True)
    ex.registry.register(RunShell())
    store.add("trust", f"write_file|write|{str(tmp_path).lower()}", "trusted folder")
    r1 = await ex.execute("make_dir", {"path": str(tmp_path / "sub")}, task_id="a")
    r2 = await ex.execute("write_file", {"path": str(tmp_path / "sub" / "n.txt"), "content": "x"}, task_id="b")
    assert r1.ok and r2.ok and asked == []  # any plain write inside the trusted folder
    await ex.execute("run_shell", {"command": "npm install", "cwd": str(tmp_path)}, task_id="c")
    assert len(asked) == 1  # shell is never covered by folder trust
    await ex.execute("write_file", {"path": str(tmp_path.parent / "outside.txt"), "content": "x"}, task_id="d")
    assert len(asked) == 2  # outside the folder: asks


async def test_existing_folder_needs_no_approval(tmp_path):
    ex, _, asked, _ = executor(tmp_path, answer=True)
    r = await ex.execute("make_dir", {"path": str(tmp_path)}, task_id="a")
    assert r.ok and asked == []


async def test_trust_needs_users_keep_before_skipping_questions(tmp_path):
    ex, store, asked, learned = executor(tmp_path, answer=True)
    for i in range(3):
        await ex.execute("write_file", {"path": str(tmp_path / f"n{i}.txt"), "content": "x"}, task_id=f"t{i}")
    assert len(asked) == 3 and learned[-1].kind == "trust" and learned[-1].status == "pending"
    await ex.execute("write_file", {"path": str(tmp_path / "n3.txt"), "content": "x"}, task_id="t3")
    assert len(asked) == 4  # pending suggestion changes nothing
    store.set_status(learned[-1].id, "active")  # user pressed Keep
    r = await ex.execute("write_file", {"path": str(tmp_path / "n4.txt"), "content": "x"}, task_id="t4")
    assert r.ok and len(asked) == 4  # trusted: no question
    (tmp_path / "n4.txt").write_text("existing")
    r = await ex.execute("write_file", {"path": str(tmp_path / "n4.txt"), "content": "overwrite"}, task_id="t5")
    assert len(asked) == 5  # overwriting is destructive: still asks despite trust


async def test_timeouts_teach_nothing(tmp_path):
    ex, store, asked, learned = executor(tmp_path, answer=None)  # nobody answers
    for i in range(3):
        await ex.execute("write_file", {"path": str(tmp_path / f"t{i}.txt"), "content": "x"}, task_id=f"t{i}")
    assert len(asked) == 3 and learned == [] and store.list() == []


# --- corrections -----------------------------------------------------------------------------------


def test_cue_gate():
    assert sounds_like_instruction("From now on reply in Telugu")
    assert sounds_like_instruction("that was too long, be brief")
    assert not sounds_like_instruction("What is the capital of Japan?")


async def test_corrections_need_a_cue_and_are_deduplicated(tmp_path):
    store = LearningStore(tmp_path / "l.db")
    p = FakeProvider("p", [js({"instructions": ["Reply in Telugu", "Keep replies short"]}),
                           js({"instructions": ["Reply in Telugu"]})])
    router = ModelRouter({"p": p}, {"verify": ["p"]})
    assert await learn_corrections(router, store, "what time is it?") == []
    assert p.calls == []  # no cue: no model call at all
    assert await learn_corrections(router, store, "from now on reply in Telugu and keep it short") == \
        ["Reply in Telugu", "Keep replies short"]
    assert await learn_corrections(router, store, "always reply in Telugu please") == []  # already known
    assert "Reply in Telugu" in standing_instructions(store)


async def test_standing_instructions_reach_the_planner(tmp_path):
    orch, _, _ = make(tmp_path, {"plan": [js({"needs_tools": False, "answer": "Tokyo."})]})
    orch.learning = LearningStore(tmp_path / "l.db")
    orch.learning.add("preference", "short", "Keep replies to one sentence")
    seen = []
    original = orch.router.complete

    async def spy(messages, tools=None, task="chat", json_mode=False):
        seen.append((task, messages[0]["content"]))
        return await original(messages, tools=tools, task=task, json_mode=json_mode)

    orch.router.complete = spy  # type: ignore[method-assign]
    await orch.run(TaskRequest("capital of japan?"))
    await orch.drain()
    plan_prompt = next(p for t, p in seen if t == "plan")
    assert "STANDING INSTRUCTIONS" in plan_prompt and "Keep replies to one sentence" in plan_prompt


async def test_learned_api_keep_and_undo(tmp_path):
    from fastapi.testclient import TestClient

    from delta.api.server import create_app, load_token
    from delta.bootstrap import build
    from delta.config import Settings

    c = build(Settings(data_dir=tmp_path))
    item = c.orchestrator.learning.add("trust", "k", "Stop asking before: write_file", status="pending")
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        assert tc.get("/api/learned").json()[0]["status"] == "pending"
        assert tc.post(f"/api/learned/{item.id}", json={"action": "keep"}).json()["status"] == "active"
        assert tc.post(f"/api/learned/{item.id}", json={"action": "undo"}).json()["status"] == "undone"
        assert tc.post(f"/api/learned/{item.id}", json={"action": "delete"}).status_code == 422
        assert tc.post("/api/learned/999", json={"action": "keep"}).status_code == 404
