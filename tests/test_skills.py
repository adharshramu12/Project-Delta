from pathlib import Path

from fakes import call, js, text
from test_orchestrator import make

from delta.core.state import TaskRequest
from delta.memory.skills import SkillStore


def test_skill_lifecycle(tmp_path):
    s = SkillStore(tmp_path / "skills.db")
    ch = s.learn("make a note file", "user asks to write a note or text file",
                 ["write_file {path}", "path_info {path}"], ["write_file", "path_info"])
    assert ch.change == "learned" and ch.skill.status == "candidate"
    again = s.learn("create note", "write a note file for the user", ["write_file", "path_info"],
                    ["write_file", "path_info"])
    assert again.change == "promoted" and again.skill.id == ch.skill.id  # same job + tools: reinforced
    assert s.relevant("please write a note on my desktop")[0].id == ch.skill.id
    for _ in range(3):
        last = s.record(ch.skill.id, success=False)
    assert last.change == "retired"
    assert s.relevant("write a note") == []  # retired skills are never offered


def test_export_import_starts_as_candidate(tmp_path):
    a = SkillStore(tmp_path / "a.db")
    sid = a.learn("open project", "open a code project", ["open_app vscode"], ["open_app"]).skill.id
    a.set_status(sid, "active")
    shared = a.export()
    assert shared and "id" not in shared[0]
    b = SkillStore(tmp_path / "b.db")
    assert b.import_skills(shared) == 1
    assert b.list()[0].status == "candidate"  # must prove itself on this PC


async def test_verified_task_teaches_a_skill_then_reuses_it(tmp_path):
    target = str(tmp_path / "notes.txt")
    plan = {"needs_tools": True, "goal": "note", "steps": ["write", "check"],
            "success_criteria": [f"{target} exists"]}
    skill_json = {"name": "write a note file", "when_to_use": "user asks to write a note file",
                  "procedure": ["write_file {path} {text}", "path_info {path} to confirm"],
                  "tools": ["write_file", "path_info"]}
    orch, _audit, _ = make(tmp_path, {
        "plan": [js(plan)],
        "chat": [call("write_file", path=target, content="hi"), call("path_info", path=target), text("Done")],
        "verify": [js({"passed": True, "reason": "exists"}), js(skill_json)],
    })
    store = SkillStore(tmp_path / "skills.db")
    orch.skills = store
    r = await orch.run(TaskRequest("write a note file"))
    await orch.drain()
    assert r.status == "done"
    learned = store.list()
    assert len(learned) == 1 and learned[0].status == "candidate"
    sid = learned[0].id

    # Second time: the planner picks the skill; success promotes it to proven.
    Path(target).unlink()
    orch2, _, _ = make(tmp_path, {
        "plan": [js({**plan, "skill_id": sid})],
        "chat": [call("write_file", path=target, content="hi"), call("path_info", path=target), text("Done")],
        "verify": [js({"passed": True, "reason": "exists"})],
    })
    orch2.skills = store
    seen = []

    async def spy(event, data):
        seen.append((event, data))

    orch2.events.subscribe(spy)
    r2 = await orch2.run(TaskRequest("write another note file"))
    await orch2.drain()
    assert r2.skill_id == sid
    assert store.get(sid).status == "active"
    assert ("skill.changed", "promoted") in [(e, d.get("change")) for e, d in seen]


async def test_invented_skill_id_is_ignored(tmp_path):
    orch, _, _ = make(tmp_path, {
        "plan": [js({"needs_tools": True, "success_criteria": ["x"], "skill_id": 999})],
        "chat": [text("nothing"), text("still nothing")], "verify": [],
    })
    orch.skills = SkillStore(tmp_path / "skills.db")
    r = await orch.run(TaskRequest("anything"))
    await orch.drain()
    assert r.skill_id is None
