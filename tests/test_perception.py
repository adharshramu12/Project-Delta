"""Perception (delta.perception): set-of-marks pictures and legends, mark resolution through the executor (every
safety check on the real click), image token tuning, the perception cache, Windows OCR parsing and its no-op
fallback, the OCR rung of the strategy ladder, and the perception counters in the usage meter."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import BaseModel

from delta.config import Boundaries, PerceptionConfig, SelfHealConfig, Settings
from delta.core.events import EventBus
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.evals.fakebrowser import FakeBrowserManager, fake_browser_tools
from delta.evals.perception_fixtures import page_capture, text_capture
from delta.llm import usage
from delta.llm.router import ModelRouter
from delta.perception import images
from delta.perception.cache import SAME_WINDOW, PerceptionCache, tree_diff
from delta.perception.marks import BOOK, Candidate, Capture, Mark, MarkSet, mark_up, parse_answer, select
from delta.perception.ocr import OcrLine, WindowsOcr, find_text, parse
from delta.perception.sources import ocr_candidates, ocr_listing
from delta.perception.tools import ClickMark, ClickText, LookWithMarks, Perception, ReadScreenText, perception_tools
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.safety.remote_guard import REMOTE_PIN_TOOLS
from delta.selfheal import strategy
from delta.selfheal.healer import Healer
from delta.tools.base import Risk, Tool, ToolResult
from delta.tools.registry import UNTRUSTED_SOURCES, ToolExecutor, ToolRegistry


# --- images -----------------------------------------------------------------------------------------
def test_fit_never_enlarges_and_keeps_the_aspect() -> None:
    assert images.fit((1920, 1080), 1280) == (1280, 720)
    assert images.fit((800, 600), 1280) == (800, 600)
    assert images.fit((1080, 1920), 1024) == (576, 1024)


def test_image_token_estimate_follows_the_tile_rule() -> None:
    assert images.image_tokens(512, 512) == 85 + 170
    assert images.image_tokens(1024, 576) == 85 + 170 * 4  # short side under 768: 2x2 tiles
    assert images.image_tokens(1920, 1080) == images.image_tokens(1365, 768)  # scaled to a 768 short side
    assert images.image_tokens(1024, 576) < images.image_tokens(1920, 1080)
    assert images.image_tokens(0, 10) == 0


def test_digest_ignores_tiny_noise_but_sees_a_change() -> None:
    a = Image.new("RGB", (400, 300), (255, 255, 255))
    b = a.copy()
    b.putpixel((10, 10), (250, 250, 250))  # anti-aliasing level noise
    c = a.copy()
    for x in range(100, 300):
        for y in range(100, 200):
            c.putpixel((x, y), (0, 0, 0))  # a dialog appeared
    assert images.digest(a) == images.digest(b)
    assert images.digest(a) != images.digest(c)


def test_downscale_and_jpeg_are_small() -> None:
    big = Image.new("RGB", (2560, 1440), (200, 200, 200))
    small = images.downscale(big, 1280)
    assert small.size == (1280, 720)
    assert len(images.encode_jpeg(small, 60)) < len(images.encode_jpeg(big, 90))


# --- marks ------------------------------------------------------------------------------------------
def cand(label: str, box: tuple[int, int, int, int], kind: str = "dom", handle: Any = 1, in_view: bool = True
         ) -> Candidate:
    return Candidate(label, "button", box, kind, handle, in_view)


def test_select_drops_duplicates_offscreen_and_tiny_boxes_and_caps() -> None:
    cands = [cand("Save", (10, 10, 100, 30)), cand("Save copy", (12, 11, 98, 30)),  # same target
             cand("Gone", (5000, 10, 50, 20)), cand("Tiny", (50, 50, 2, 2)),
             cand("Later", (10, 300, 80, 20), in_view=False), cand("Open", (10, 100, 80, 20))]
    picked = select(cands, (800, 600), cap=10)
    assert [c.label for c in picked] == ["Save", "Open", "Later"]  # on-screen first
    assert len(select([cand(str(i), (0, i * 10, 50, 8)) for i in range(60)], (800, 2000), cap=40)) == 40


def test_mark_up_draws_on_a_downscaled_picture_and_builds_the_legend() -> None:
    img = Image.new("RGB", (2000, 1000), (255, 255, 255))
    cap = Capture(img, [cand("Save", (100, 100, 200, 60), handle=7), cand("Help", (100, 300, 200, 60), handle=9)],
                  "dom", "Editor")
    pic, marks = mark_up(cap, 1000)
    assert pic.size == (1000, 500)
    assert marks.marks[1].box == (50, 50, 100, 30)  # scaled with the picture
    assert marks.legend() == '1 button "Save"\n2 button "Help"'
    assert pic.getpixel((50, 60)) != (255, 255, 255)  # the box outline is drawn
    assert img.getpixel((100, 100)) == (255, 255, 255)  # the capture itself is untouched


@pytest.mark.parametrize(("answer", "want"), [
    ("MARK 3 - the Save button", 3), ("mark #12, top right", 12), ("Mark: 4", 4), ("7", 7), ("[2]", 2),
    ("MARK NONE - nothing fits", None), ("I think the second one", None), ("", None)])
def test_parse_answer(answer: str, want: int | None) -> None:
    assert parse_answer(answer) == want


def test_marks_resolve_to_the_handle_never_pixels_when_one_exists() -> None:
    ms = MarkSet({1: Mark(1, "Save", "button", (0, 0, 9, 9), "dom", 14),
                  2: Mark(2, "OK", "Button", (0, 0, 9, 9), "uia", object(), (300, 400)),
                  3: Mark(3, "Report", "text", (0, 0, 9, 9), "xy", None, (510, 220))}, "dom", window="Editor")
    assert ms.resolve(1) == ("browser_click", {"ref": 14})
    tool, args = ms.resolve(2, "t1")  # type: ignore[misc]
    assert tool == "click_element" and args == {"name": "OK", "handle": "t1#2", "window": "Editor"}
    assert ms.resolve(3) == ("click_at", {"x": 510, "y": 220, "expect": "Report"})
    assert "no mark 9" in str(ms.resolve(9))


def test_mark_book_hands_out_only_current_uia_elements() -> None:
    el = object()
    BOOK.put("bk", MarkSet({1: Mark(1, "OK", "Button", (0, 0, 1, 1), "uia", el)}, "uia"))
    assert BOOK.handle("bk#1") is el
    assert BOOK.handle("bk#2") is None and BOOK.handle("nope#1") is None and BOOK.handle("garbage") is None
    BOOK.put("bk", MarkSet({1: Mark(1, "Save", "dom", (0, 0, 1, 1), "dom", 3)}, "dom"))
    assert BOOK.handle("bk#1") is None  # a newer look replaced it, and a page ref is not a UIA element
    BOOK.forget("bk")
    assert BOOK.get("bk") is None


# --- the perception cache -------------------------------------------------------------------------------
def test_cache_pictures_and_answers_per_task() -> None:
    c = PerceptionCache()
    assert not c.picture_seen("t", "step:Notepad", "d1")
    assert c.picture_seen("t", "step:Notepad", "d1")
    assert not c.picture_seen("t", "step:Notepad", "d2")
    assert not c.picture_seen("other", "step:Notepad", "d2")  # never shared between tasks
    c.keep_answer("t", "k", "MARK 1")
    assert c.answer("t", "k") == "MARK 1" and c.answer("t", "x") is None
    c.forget("t")
    assert c.answer("t", "k") is None
    off = PerceptionCache(enabled=False)
    assert not off.picture_seen("t", "v", "d") and not off.picture_seen("t", "v", "d")


def test_window_reads_diff_only_against_the_read_right_before() -> None:
    c = PerceptionCache()
    tree = "WINDOW: Notepad\n" + "\n".join(f"[Button] b{i}" for i in range(30))
    assert c.window_text("t", "Notepad", tree, 1) == (tree, False)
    assert c.window_text("t", "Notepad", tree, 2) == (SAME_WINDOW, True)
    changed = tree.replace("[Button] b3", "[Button] Saved")
    out, cached = c.window_text("t", "Notepad", changed, 2)
    assert cached and out.startswith("Window changes: ") and "+ [Button] Saved" in out and "- [Button] b3" in out
    # the base was sent three results ago (the agent loop may have shortened it): full text again
    assert c.window_text("t", "Notepad", changed, 4) == (changed, False)


def test_tree_diff_counts_repeated_lines() -> None:
    out = tree_diff(["OK", "OK", "Cancel"], ["OK", "Cancel", "Help"])
    assert out.splitlines()[1:] == ["+ Help", "- OK"]


# --- OCR -----------------------------------------------------------------------------------------------
OCR_JSON = json.dumps({"lines": [
    {"text": "Save report", "words": [{"t": "Save", "x": 10, "y": 20, "w": 40, "h": 12},
                                      {"t": "report", "x": 55, "y": 20, "w": 60, "h": 12}]},
    {"text": "Cancel", "words": {"t": "Cancel", "x": 10, "y": 60, "w": 50, "h": 12}},  # PowerShell 5 flattening
    {"text": "", "words": []}]})


def test_ocr_json_is_parsed_into_lines_with_boxes() -> None:
    lines = parse(OCR_JSON)
    assert [ln.text for ln in lines] == ["Save report", "Cancel"]
    assert lines[0].box == (10, 20, 105, 12)
    assert parse("not json") == [] and parse('{"error": "no OCR"}') == []


def test_find_text_is_exact_first_and_never_fuzzy() -> None:
    lines = parse(OCR_JSON)
    assert find_text(lines, "cancel") == ("Cancel", (10, 60, 50, 12))
    assert find_text(lines, "report") == ("report", (55, 20, 60, 12))  # a run of words inside a line
    assert find_text(lines, "Save   report") == ("Save report", (10, 20, 105, 12))
    assert find_text(lines, "Delete") is None and find_text(lines, " ") is None


def test_ocr_lines_become_text_elements_with_screen_points() -> None:
    lines = parse(OCR_JSON)
    assert [c.kind for c in ocr_candidates(lines)] == ["xy", "xy"]
    assert ocr_listing(lines, origin=(100, 200)) == '"Save report" at (162,226)\n"Cancel" at (135,266)'


async def test_ocr_is_a_no_op_when_off_or_not_on_windows() -> None:
    off = WindowsOcr("off")
    assert not off.available() and await off.read(Image.new("RGB", (10, 10))) == []
    if sys.platform != "win32":
        auto = WindowsOcr("auto")
        assert not await auto.probe() and not auto.available()


# --- tools and the executor ------------------------------------------------------------------------------
class SeeingRouter:
    """A router whose vision answers are scripted (only what the perception tools use)."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.meter: dict[str, int] = {}
        self.vision_calls = 0

    def candidates(self, task: str) -> list[Any]:
        return [object()] if task == "vision" else []

    async def complete(self, messages: list[Any], tools: Any = None, task: str = "chat", **_: Any) -> Any:
        from delta.llm.schemas import LLMResponse

        self.vision_calls += 1
        r = LLMResponse(content=self.answers.pop(0) if self.answers else "MARK NONE")
        r.provider = "fake-vision"
        return r


def rig(tmp: Path, router: Any, approve: bool = True, autonomy: str = "ask-risky", popup: str = "",
        cfg: PerceptionConfig | None = None) -> tuple[ToolExecutor, FakeBrowserManager, Perception, list[Any]]:
    mgr = FakeBrowserManager(tmp / "page_state.json", {"popup": popup} if popup else {})
    p = Perception(cfg or PerceptionConfig(), router, WindowsOcr("off"), PerceptionCache())

    async def capture(where: str, window: str | None) -> Capture:
        return page_capture(mgr)

    p.capture_override = capture
    approvals = ApprovalBroker(timeout_s=2)
    asked: list[Any] = []

    async def answer(req: Any) -> None:
        asked.append(req)
        asyncio.get_running_loop().call_soon(approvals.resolve, req.id, approve, "test")

    approvals.add_presenter("test", answer)
    policy = PolicyEngine(Boundaries(autonomy=autonomy, allowed_paths=[str(tmp)]))  # type: ignore[arg-type]
    ex = ToolExecutor(ToolRegistry([*fake_browser_tools(mgr), *perception_tools(p)]),  # type: ignore[list-item]
                      policy, approvals, AuditLog(tmp / "audit.jsonl"))
    return ex, mgr, p, asked


def state(tmp: Path) -> dict[str, str]:
    return dict(json.loads((tmp / "page_state.json").read_text(encoding="utf-8")))


async def in_task(task_id: str, coro: Any) -> Any:
    token = CURRENT_REQUEST.set(TaskRequest("test", channel="api", id=task_id))
    try:
        return await coro
    finally:
        CURRENT_REQUEST.reset(token)


async def test_look_then_click_mark_clicks_the_element_by_ref(tmp_path: Path) -> None:
    router = SeeingRouter("MARK 2 - the Help link")
    ex, _, _, _ = rig(tmp_path, router)

    async def flow() -> tuple[ToolResult, ToolResult]:
        await ex.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="m1")
        look = await ex.execute("look_with_marks", {"question": "which mark opens help?"}, task_id="m1")
        click = await ex.execute("click_mark", {"mark": 2}, task_id="m1")
        return look, click

    look, click = await in_task("m1", flow())
    assert look.ok and "click_mark mark=2" in look.output and look.evidence["marks"] == 2
    assert click.ok and click.evidence["resolved_to"] == "browser_click" and click.evidence["via"] == "click_mark"
    assert state(tmp_path).get("clicked_help") == "yes" and "clicked" not in state(tmp_path)
    assert router.meter["images_sent"] == 1 and router.meter["marks_used"] == 2
    assert router.meter["image_tokens_est"] == images.image_tokens(900, 560)
    BOOK.forget("m1")


async def test_a_payment_mark_is_asked_like_any_payment_click(tmp_path: Path) -> None:
    router = SeeingRouter("MARK 3")
    ex, _, _, asked = rig(tmp_path, router, approve=False, popup="payment")

    async def flow() -> ToolResult:
        await ex.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="m2")
        await ex.execute("look_with_marks", {"question": "which to press?"}, task_id="m2")
        return await ex.execute("click_mark", {"mark": 3}, task_id="m2")

    r = await in_task("m2", flow())
    assert not r.ok and r.evidence.get("user_denied") and len(asked) == 1
    assert asked[0].tool == "browser_click" and "Pay now" in asked[0].summary  # the REAL click was judged
    assert state(tmp_path)["popup"] == "payment" and "clicked_pay_now" not in state(tmp_path)
    BOOK.forget("m2")


async def test_click_mark_risk_is_the_risk_of_the_click_it_stands_for(tmp_path: Path) -> None:
    router = SeeingRouter("MARK 1")
    ex, _, p, _ = rig(tmp_path, router, popup="payment")

    async def flow() -> None:
        await ex.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="m3")
        await ex.execute("look_with_marks", {"question": "x"}, task_id="m3")

    await in_task("m3", flow())
    tool = ClickMark(p)
    token = CURRENT_REQUEST.set(TaskRequest("t", channel="api", id="m3"))
    try:
        assert tool.risk_for(tool.Args(mark=1)) == Risk.ACT  # Mark done
        assert tool.risk_for(tool.Args(mark=3)) == Risk.EXTERNAL  # Pay now
        assert "Pay now" in tool.summary(tool.Args(mark=3))
    finally:
        CURRENT_REQUEST.reset(token)
    BOOK.forget("m3")


async def test_a_denied_alias_or_underlying_tool_never_runs(tmp_path: Path) -> None:
    router = SeeingRouter("MARK 1", "MARK 1")
    ex, _, _, _ = rig(tmp_path, router)
    ex.policy.b.denied_tools = ["browser_click"]

    async def flow() -> tuple[ToolResult, ToolResult]:
        await ex.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="m4")
        await ex.execute("look_with_marks", {"question": "x"}, task_id="m4")
        first = await ex.execute("click_mark", {"mark": 1}, task_id="m4")
        ex.policy.b.denied_tools = ["click_mark"]
        second = await ex.execute("click_mark", {"mark": 1}, task_id="m4")
        return first, second

    first, second = await in_task("m4", flow())
    assert "BLOCKED" in first.output and "BLOCKED" in second.output
    assert "clicked" not in state(tmp_path)
    BOOK.forget("m4")


async def test_unchanged_view_and_question_reuse_the_answer_without_a_picture(tmp_path: Path) -> None:
    router = SeeingRouter("MARK 1")
    ex, _, _, _ = rig(tmp_path, router)

    async def flow() -> ToolResult:
        await ex.execute("browser_open", {"url": "http://127.0.0.1/button.html"}, task_id="m5")
        await ex.execute("look_with_marks", {"question": "Which is done?"}, task_id="m5")
        return await ex.execute("look_with_marks", {"question": "which is done? "}, task_id="m5")

    again = await in_task("m5", flow())
    assert again.ok and again.evidence["cache_hit"] and router.vision_calls == 1
    assert router.meter["images_sent"] == 1 and router.meter["perception_cache_hits"] == 1
    BOOK.forget("m5")


async def test_no_vision_model_hides_marks_and_ocr_tools_hide_without_ocr(tmp_path: Path) -> None:
    blind = ModelRouter({}, {})
    p = Perception(PerceptionConfig(), blind, WindowsOcr("off"))
    tools = {t.name: t for t in perception_tools(p)}
    assert not tools["look_with_marks"].available() and not tools["click_mark"].available()
    assert not tools["read_screen_text"].available() and not tools["click_text"].available()
    p2 = Perception(PerceptionConfig(marks=False), SeeingRouter(), WindowsOcr("off"))
    assert not LookWithMarks(p2).available()


async def test_ocr_tools_resolve_text_to_a_coordinate_click_that_keeps_the_risk(tmp_path: Path) -> None:
    class StubOcr(WindowsOcr):
        def __init__(self) -> None:
            super().__init__("auto")

        def available(self) -> bool:
            return True

        async def read(self, image: Any) -> list[OcrLine]:
            self.calls += 1
            return parse(json.dumps({"lines": [
                {"text": "Send now", "words": [{"t": "Send", "x": 0, "y": 0, "w": 40, "h": 10},
                                               {"t": "now", "x": 45, "y": 0, "w": 30, "h": 10}]}]}))

    router = SeeingRouter()
    p = Perception(PerceptionConfig(), router, StubOcr())

    async def capture(where: str, window: str | None) -> Capture:
        return text_capture(["Send now"])

    p.capture_override = capture
    read = await ReadScreenText(p).run(ReadScreenText.Args(window="Eval fixture"))
    assert read.ok and '"Send now" at (138,205)' in read.output
    tool = ClickText(p)
    assert tool.risk_for(tool.Args(text="Send")) == Risk.EXTERNAL
    assert await tool.resolve(tool.Args(text="send")) == ("click_at", {"x": 120, "y": 205, "expect": "Send"})
    assert "no text 'Archive'" in str(await tool.resolve(tool.Args(text="Archive")))
    assert router.meter["ocr_calls"] == 3


def test_click_at_expect_only_ever_raises_the_risk() -> None:
    from delta.tools.desktop import ClickAt, ClickAtArgs, ClickElementArgs

    tool = ClickAt.__new__(ClickAt)
    tool.labels_at = lambda x, y: []  # type: ignore[method-assign]
    assert tool.risk_for(ClickAtArgs(x=1, y=1)) == Risk.ACT
    assert tool.risk_for(ClickAtArgs(x=1, y=1, expect="Delete all")) == Risk.DESTRUCTIVE
    tool.labels_at = lambda x, y: ["Send"]  # type: ignore[method-assign]
    assert tool.risk_for(ClickAtArgs(x=1, y=1, expect="Cancel")) == Risk.EXTERNAL  # never lowered
    # the hidden fields are not offered to the model
    assert "expect" not in json.dumps(ClickAtArgs.model_json_schema())
    assert "handle" not in json.dumps(ClickElementArgs.model_json_schema())


def test_new_screen_readers_taint_the_task_and_need_the_pin_remotely() -> None:
    assert {"look_with_marks", "read_screen_text"} <= UNTRUSTED_SOURCES
    assert {"look_with_marks", "read_screen_text", "click_text"} <= set(REMOTE_PIN_TOOLS)


# --- the strategy ladder's OCR rung ----------------------------------------------------------------------
def test_ocr_rung_sits_between_ui_automation_and_vision() -> None:
    assert strategy.next_rung("desktop", strategy.UIA, can_see=True) == strategy.VISION  # default: no OCR
    assert strategy.next_rung("desktop", strategy.UIA, can_see=True, can_ocr=True) == strategy.OCR
    assert strategy.next_rung("web", strategy.UIA, can_see=False, can_ocr=True) == strategy.OCR
    assert strategy.next_rung("desktop", strategy.OCR, can_see=False, can_ocr=True) == strategy.ASK
    move = strategy.STEP_DOWN[("click_element", strategy.OCR)]({"name": "Save", "window": "Paint"}, {})
    assert move == strategy.Translated("click_text", {"text": "Save", "window": "Paint"})
    assert strategy.rung_of("click_text") == ("desktop", strategy.OCR)
    assert "look_with_marks" in strategy.vision_hint("Save", marks=True)
    assert "look_at_screen" in strategy.vision_hint("Save")


class _Args(BaseModel):
    name: str = ""
    window: str | None = None
    text: str = ""


class _MissingElement(Tool[_Args]):
    name = "click_element"
    description = "test"
    risk = Risk.ACT
    Args = _Args

    async def run(self, args: _Args) -> ToolResult:
        return ToolResult(False, f"no visible control named '{args.name}'; call read_window to see what exists")


class _ClickText(Tool[_Args]):
    name = "click_text"
    description = "test"
    risk = Risk.ACT
    Args = _Args

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, args: _Args) -> ToolResult:
        self.calls.append(args.text)
        return ToolResult(True, f"clicked at the text '{args.text}'")


async def test_healer_steps_down_to_ocr_before_vision(tmp_path: Path) -> None:
    ocr_tool = _ClickText()
    policy = PolicyEngine(Boundaries(allowed_paths=[str(tmp_path)]))
    ex = ToolExecutor(ToolRegistry([_MissingElement(), ocr_tool]), policy, ApprovalBroker(timeout_s=1),
                      AuditLog(tmp_path / "audit.jsonl"))
    healer = Healer(SelfHealConfig(strategy_after=1), ex, EventBus(), settle_s=0, can_see=lambda: True,
                    can_ocr=lambda: True)
    args = {"name": "Export", "window": "Old App"}
    r = await ex.execute("click_element", args, task_id="h")
    rec = await healer.recover("h", "click_element", args, r, [])
    assert rec.recovered and ocr_tool.calls == ["Export"]
    assert any("Windows OCR" in line for line in rec.lines)


# --- usage meter ------------------------------------------------------------------------------------------
def test_usage_summary_lists_perception_counters_only_when_used() -> None:
    assert "perception" not in usage.summary({"calls": 1, "input": 10, "output": 1, "reported_calls": 1})
    meter: dict[str, int] = {"calls": 1, "input": 10, "output": 1, "reported_calls": 1}
    usage.bump(type("R", (), {"meter": meter})(), "images_sent")
    usage.bump(None, "images_sent")  # never fails
    usage.add(meter, "ocr_calls", 2)
    assert usage.summary(meter)["perception"] == {"images_sent": 1, "ocr_calls": 2}


# --- settings ---------------------------------------------------------------------------------------------
def test_perception_settings_are_additive_and_old_settings_load() -> None:
    s = Settings.model_validate({"agent": {"name": "Delta"}})
    assert s.perception.marks and s.perception.ocr == "auto" and s.perception.cache
    assert not s.helpers.enabled and s.helpers.max_helpers == 2
    with pytest.raises(ValueError):
        Settings.model_validate({"helpers": {"max_helpers": 5}})


async def test_orchestrator_does_not_resend_an_unchanged_screenshot(tmp_path: Path, monkeypatch: Any) -> None:
    import base64
    import io

    from engine_helpers import Rig, no_registry

    no_registry(monkeypatch, tmp_path)
    rig_ = Rig(tmp_path, {})
    orch = rig_.orch
    orch.perception = Perception(PerceptionConfig(), orch.router, WindowsOcr("off"), PerceptionCache())
    buf = io.BytesIO()
    Image.new("RGB", (640, 360), (20, 30, 40)).save(buf, "JPEG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    assert not orch._same_picture("t", "Notepad", b64)
    assert orch._same_picture("t", "Notepad", b64)
    assert orch.router.meter.get("perception_cache_hits") == 1
