"""Set-of-marks against a real Chrome/Edge: marks come from the page's numbered elements with their real boxes,
the marked picture is small, and click_mark clicks the chosen element by its ref through the executor (Send
asks first, exactly like browser_click). Run with DELTA_E2E=1 pytest -m e2e."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from delta.config import Boundaries, PerceptionConfig
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.llm.schemas import LLMResponse
from delta.perception.cache import PerceptionCache
from delta.perception.marks import BOOK, mark_up
from delta.perception.ocr import WindowsOcr
from delta.perception.sources import browser_capture
from delta.perception.tools import Perception, perception_tools
from delta.safety.approvals import ApprovalBroker
from delta.safety.audit import AuditLog
from delta.safety.policy import PolicyEngine
from delta.tools.browser import BrowserManager, browser_tools
from delta.tools.registry import ToolExecutor, ToolRegistry

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]


class _Vision:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.meter: dict[str, int] = {}

    def candidates(self, task: str) -> list[Any]:
        return [object()] if task == "vision" else []

    async def complete(self, messages: list[Any], tools: Any = None, task: str = "chat", **_: Any) -> Any:
        r = LLMResponse(content=self.answer)
        r.provider = "e2e-vision"
        return r


class _Hub:
    def __init__(self, mgr: BrowserManager) -> None:
        self.mgr = mgr

    def kind_now(self) -> str:
        return "private"


async def test_marks_are_the_pages_own_elements_with_real_boxes(mgr: BrowserManager, site: str) -> None:
    page = await mgr.page()
    await page.goto(f"{site}/compose.html")
    mgr.current_url = page.url
    cap = await browser_capture(mgr)
    names = {c.label: c for c in cap.candidates}
    assert {"Preview", "Send", "Delete draft"} <= set(names)
    assert all(c.kind == "dom" and c.handle in mgr.refs for c in cap.candidates)
    w, h = cap.image.size
    for c in cap.candidates:
        x, y, bw, bh = c.box
        assert 0 <= x < w and 0 <= y < h and bw > 0 and bh > 0
    picture, marks = mark_up(cap, 1024, 40)
    assert max(picture.size) <= 1024 and len(marks.marks) == len(cap.candidates)


async def test_click_mark_clicks_by_ref_and_send_still_asks(mgr: BrowserManager, site: str, tmp_path: Path) -> None:
    asked: list[Any] = []
    approvals = ApprovalBroker(timeout_s=5)

    async def answer(req: Any) -> None:
        asked.append(req)
        asyncio.get_running_loop().call_soon(approvals.resolve, req.id, False, "e2e")

    approvals.add_presenter("e2e", answer)
    router = _Vision("MARK 1")
    p = Perception(PerceptionConfig(), router, WindowsOcr("off"), PerceptionCache(), hub=_Hub(mgr))
    policy = PolicyEngine(Boundaries(allowed_paths=[str(tmp_path)]))
    ex = ToolExecutor(ToolRegistry([*browser_tools(mgr), *perception_tools(p)]), policy, approvals,
                      AuditLog(tmp_path / "a.jsonl"))
    token = CURRENT_REQUEST.set(TaskRequest("e2e", channel="api", id="e2e-marks"))
    try:
        opened = await ex.execute("browser_open", {"url": f"{site}/compose.html"}, task_id="e2e-marks")
        assert opened.ok
        look = await ex.execute("look_with_marks", {"question": "which mark is Preview?"}, task_id="e2e-marks")
        assert look.ok and look.evidence["source"] == "dom" and look.evidence["marks"] >= 3
        marks = BOOK.get("e2e-marks")
        assert marks is not None
        preview = next(n for n, m in marks.marks.items() if m.label == "Preview")
        send = next(n for n, m in marks.marks.items() if m.label == "Send")
        r = await ex.execute("click_mark", {"mark": preview}, task_id="e2e-marks")
        assert r.ok and r.evidence["resolved_to"] == "browser_click"
        page = await mgr.page()
        assert await page.inner_text("#status") == "previewed"
        r = await ex.execute("click_mark", {"mark": send}, task_id="e2e-marks")
        assert not r.ok and asked and asked[-1].tool == "browser_click"
        assert await page.inner_text("#status") == "previewed"  # Send was refused, never clicked
        assert router.meter["images_sent"] == 1 and router.meter["image_tokens_est"] > 0
    finally:
        CURRENT_REQUEST.reset(token)
        BOOK.forget("e2e-marks")
