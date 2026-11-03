import asyncio
from datetime import datetime
from pathlib import Path

from delta.config import Boundaries, QuietHours
from delta.safety.approvals import ApprovalBroker, ApprovalRequest
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine, Verdict
from delta.tools.apps import OpenApp, OpenAppArgs
from delta.tools.base import Risk
from delta.tools.filesystem import DeletePath, PathArgs, ReadFile, WriteArgs, WriteFile
from delta.tools.shell import RunShell, ShellArgs, classify


def engine(tmp_path: Path, **kw) -> PolicyEngine:
    b = Boundaries(allowed_paths=[str(tmp_path / "ok")], denied_paths=[str(tmp_path / "secret")], **kw)
    return PolicyEngine(b)


def test_read_allowed_write_asks_delete_asks(tmp_path):
    pe = engine(tmp_path)
    assert pe.evaluate(ReadFile(), PathArgs(path=str(tmp_path / "a.txt"))).verdict == Verdict.ALLOW
    assert pe.evaluate(WriteFile(), WriteArgs(path=str(tmp_path / "ok/a.txt"), content="x")).verdict == Verdict.ASK
    assert pe.evaluate(DeletePath(), PathArgs(path=str(tmp_path / "ok/a.txt"))).verdict == Verdict.ASK


def test_trusted_writes_inside_allowed_only(tmp_path):
    pe = engine(tmp_path, autonomy="trusted")
    inside = WriteArgs(path=str(tmp_path / "ok/new.txt"), content="x")
    outside = WriteArgs(path=str(tmp_path / "elsewhere/new.txt"), content="x")
    assert pe.evaluate(WriteFile(), inside).verdict == Verdict.ALLOW
    assert pe.evaluate(WriteFile(), outside).verdict == Verdict.ASK
    # undoable (Recycle Bin) deletes no longer ask when trusted, but only inside allowed folders
    assert pe.evaluate(DeletePath(), PathArgs(path=str(tmp_path / "ok/new.txt"))).verdict == Verdict.ALLOW
    assert pe.evaluate(DeletePath(), PathArgs(path=str(tmp_path / "elsewhere/x.txt"))).verdict == Verdict.ASK


def test_autonomy_levels_ask_only_where_promised(tmp_path):
    """Users picked Trusted and still got approval prompts for everyday, undoable work."""
    from delta.messaging.tools import SendArgs, SendMessage
    from delta.tools.shell import RunShell, ShellArgs

    existing = tmp_path / "ok" / "notes.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("old")
    overwrite = WriteArgs(path=str(existing), content="new")
    hard_delete = ShellArgs(command=f"Remove-Item -Recurse -Force '{tmp_path / 'ok'}'")
    plain_cmd = ShellArgs(command="New-Item -ItemType Directory build")
    send = SendArgs.model_validate({"recipient": "Ravi", "app": "WhatsApp", "text": "hi"})
    expect = {  # level: (overwrite, plain command, permanent delete, send a message)
        "ask-risky": (Verdict.ASK, Verdict.ASK, Verdict.ASK, Verdict.ASK),
        "trusted": (Verdict.ALLOW, Verdict.ALLOW, Verdict.ASK, Verdict.ASK),
        "autopilot": (Verdict.ALLOW, Verdict.ALLOW, Verdict.ASK, Verdict.ALLOW),
    }
    for level, want in expect.items():
        pe = engine(tmp_path, autonomy=level)
        sender = SendMessage.__new__(SendMessage)
        got = (pe.evaluate(WriteFile(), overwrite).verdict, pe.evaluate(RunShell(), plain_cmd).verdict,
               pe.evaluate(RunShell(), hard_delete).verdict, pe.evaluate(sender, send).verdict)
        assert got == want, level


async def test_overwriting_keeps_the_old_version_in_the_recycle_bin(tmp_path, monkeypatch):
    from delta.tools import filesystem

    recycled = []
    monkeypatch.setattr(filesystem, "_recycle", lambda p: (recycled.append(p), p.unlink()))
    f = tmp_path / "a.txt"
    f.write_text("old")
    await WriteFile().run(WriteArgs(path=str(f), content="new"))
    assert f.read_text() == "new" and recycled == [f]


def test_denied_path_blocks_even_reads(tmp_path):
    pe = engine(tmp_path)
    d = pe.evaluate(ReadFile(), PathArgs(path=str(tmp_path / "secret/keys.txt")))
    assert d.verdict == Verdict.DENY


def test_denied_tool_and_app(tmp_path):
    pe = engine(tmp_path, denied_tools=["run_shell"], denied_apps=["regedit"])
    assert pe.evaluate(RunShell(), ShellArgs(command="dir")).verdict == Verdict.DENY
    assert pe.evaluate(OpenApp({}), OpenAppArgs(name="regedit")).verdict == Verdict.DENY


def test_shell_classification():
    assert classify("Get-ChildItem C:/") == Risk.READ
    assert classify("git status; git log -3") == Risk.READ
    assert classify("Remove-Item foo.txt") == Risk.DESTRUCTIVE
    assert classify("Set-MpPreference -DisableRealtimeMonitoring $true") == Risk.SYSTEM
    assert classify("format D:") == Risk.SYSTEM
    assert classify("npm install") == Risk.WRITE
    assert classify("git push origin main") == Risk.EXTERNAL


def test_system_shell_is_hard_denied(tmp_path):
    pe = engine(tmp_path, autonomy="trusted")
    assert pe.evaluate(RunShell(), ShellArgs(command="bcdedit /set x")).verdict == Verdict.DENY


def test_quiet_hours(tmp_path):
    b = Boundaries(quiet_hours=QuietHours(enabled=True, start="23:00", end="07:00"))
    pe = PolicyEngine(b, now=lambda: datetime(2026, 1, 1, 2, 0))
    assert pe.evaluate(OpenApp({}), OpenAppArgs(name="notepad")).verdict == Verdict.ASK
    assert pe.evaluate(ReadFile(), PathArgs(path="~/x")).verdict == Verdict.ALLOW


async def test_approval_resolve_and_timeout():
    broker = ApprovalBroker(timeout_s=0.2)
    seen = []

    async def presenter(req):
        seen.append(req)

    broker.add_presenter("test", presenter)
    req = ApprovalRequest(tool="t", summary="s", risk="write", reason="r")
    fut = asyncio.create_task(broker.request(req))
    await asyncio.sleep(0.01)
    assert broker.resolve(req.id, True, "me")
    assert (await fut).approved
    out = await broker.request(ApprovalRequest(tool="t", summary="s", risk="write", reason="r"))
    assert not out.approved and out.by == "timeout"
    assert len(seen) == 2


def test_audit_chain_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(3):
        log.record("tool", n=i)
    assert log.verify()
    assert AuditLog(tmp_path / "audit.jsonl").verify()  # reopen continues chain
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    lines[1] = lines[1].replace('"n": 1', '"n": 9')
    (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n")
    assert not log.verify()


def test_audit_chain_survives_two_writers(tmp_path):
    """The daemon and the CLI each open the log; interleaved writes must still form one chain."""
    daemon, cli = AuditLog(tmp_path / "audit.jsonl"), AuditLog(tmp_path / "audit.jsonl")
    daemon.record("delta.start")
    cli.record("task.start")
    cli.record("task.end")
    daemon.record("setup.complete")
    assert AuditLog(tmp_path / "audit.jsonl").verify()


def test_sure_argument_synonyms_are_fixed_without_a_round_trip():
    """A free model wrote write_file(text=...); the 'did you mean content' reply cost a whole AI call."""
    from pydantic import BaseModel

    from delta.tools.registry import fix_synonyms

    class WriteArgs(BaseModel):
        path: str
        content: str

    args, fixed = fix_synonyms(WriteArgs, {"file": "a.txt", "text": "hi"})
    assert args == {"path": "a.txt", "content": "hi"} and fixed == ["file->path", "text->content"]
    assert fix_synonyms(WriteArgs, {"path": "a", "file": "b", "content": "x"})[0]["file"] == "b"  # never overwrite
    assert fix_synonyms(WriteArgs, {"attendees": "x"})[1] == []  # not a sure synonym: still reported
