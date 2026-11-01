import sys
from pathlib import Path
from types import SimpleNamespace

from delta.addons.api import AddonContext
from delta.addons.builtin import builtin_addons
from delta.addons.builtin.mcp_bridge import FreeArgs, McpTool, pick_risk, tool_name
from delta.addons.loader import start_addons
from delta.core.events import EventBus
from delta.llm.router import ModelRouter
from delta.tools.base import Risk
from delta.tools.registry import ToolRegistry

HERE = Path(__file__).parent

GOOD_ADDON = '''
from pydantic import BaseModel
from delta.addons.api import Risk, Tool, ToolResult

class EchoArgs(BaseModel):
    text: str

class Echo(Tool[EchoArgs]):
    name = "echo"
    description = "echo text"
    risk = Risk.READ
    Args = EchoArgs
    async def run(self, args):
        return ToolResult(True, args.text)

class EchoAddon:
    name = "echo"
    version = "0.1.0"
    description = "test add-on"
    async def setup(self, ctx):
        ctx.register_tool(Echo())

addon = EchoAddon()
'''


def ctx_factory(tmp_path: Path, registry: ToolRegistry):
    def make(name, conf):
        d = tmp_path / "data" / name
        d.mkdir(parents=True, exist_ok=True)
        return AddonContext(name, conf, d, ModelRouter({}, {}), EventBus(), registry)
    return make


async def test_folder_addon_loads_only_when_enabled(tmp_path):
    addons = tmp_path / "addons"
    (addons / "echo").mkdir(parents=True)
    (addons / "echo" / "addon.py").write_text(GOOD_ADDON)
    (addons / "broken").mkdir()
    (addons / "broken" / "addon.py").write_text("raise RuntimeError('boom')")

    reg = ToolRegistry()
    infos, _ = await start_addons([], {}, {}, ctx_factory(tmp_path, reg), addons)
    assert infos == [] and reg.names() == []  # nothing enabled -> no add-on code runs

    infos, _ctxs = await start_addons(["echo", "broken", "missing"], {}, {}, ctx_factory(tmp_path, reg), addons)
    status = {i.name: i.status for i in infos}
    assert status == {"echo": "running", "broken": "failed", "missing": "not found"}
    assert reg.names() == ["echo"]
    assert (await reg.get("echo").run(reg.get("echo").Args(text="hi"))).output == "hi"


def test_mcp_risk_mapping():
    def tool(name, **ann):
        return SimpleNamespace(name=name, annotations=SimpleNamespace(**ann) if ann else None)

    assert pick_risk(tool("x", read_only_hint=True), {}) == Risk.READ
    assert pick_risk(tool("x", destructive_hint=True), {}) == Risk.DESTRUCTIVE
    assert pick_risk(tool("x"), {}) == Risk.WRITE  # unknown tools ask first
    assert pick_risk(tool("x"), {"risk": "external"}) == Risk.EXTERNAL
    assert pick_risk(tool("x", destructive_hint=True), {"tools": {"x": "read"}}) == Risk.READ
    assert tool_name("my server", "do.thing") == "my_server__do_thing"


async def test_mcp_tool_maps_results():
    class Session:
        async def call_tool(self, name, args, read_timeout_seconds=None):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=f"{name}:{args['q']}")],
                                   structured_content=None, is_error=False)

    t = McpTool("srv", Session(), SimpleNamespace(name="find", description="find", input_schema=None,
                                                  annotations=None), Risk.READ)
    r = await t.run(FreeArgs(q="x"))
    assert r.ok and r.output == "find:x" and t.spec()["function"]["name"] == "srv__find"


async def test_real_mcp_server_over_stdio(tmp_path):
    reg = ToolRegistry()
    conf = {"mcp": {"servers": {"calc": {"command": sys.executable, "args": [str(HERE / "mcp_test_server.py")]}}}}
    infos, ctxs = await start_addons(["mcp"], conf, builtin_addons(), ctx_factory(tmp_path, reg), tmp_path)
    try:
        assert infos[0].status == "running", infos[0].error
        assert set(reg.names()) == {"calc__add", "calc__create_note"}
        add = reg.get("calc__add")
        assert add.risk == Risk.READ  # from the server's read_only_hint
        assert reg.get("calc__create_note").risk == Risk.WRITE
        result = await add.run(add.Args(a=2, b=3))
        assert result.ok and result.output == "5"
    finally:
        for ctx in ctxs:
            for _, hook in ctx._shutdown:
                await hook()
