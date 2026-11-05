"""Delta browser: numbered element refs, act-and-observe snapshots and bounded waits.

Playwright is replaced by a small fake page, so these run anywhere without a browser."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from delta.config import Boundaries
from delta.safety.policy import PolicyEngine, Verdict
from delta.tools import browser
from delta.tools.base import Risk
from delta.tools.browser import (
    ELEMENTS_JS,
    SNAPSHOT_CHARS,
    BrowserClick,
    BrowserFill,
    BrowserManager,
    BrowserPress,
    BrowserRead,
    BrowserScroll,
    ClickArgs,
    ElementRef,
    FillArgs,
    PressArgs,
    ReadArgs,
    RefClickArgs,
    RefFillArgs,
    ScrollArgs,
    parse_refs,
    settle,
    snapshot_text,
)


class FakeElement:
    def __init__(self, ref: int, role: str, name: str, *, type: str = "", attrs: dict[str, str] | None = None,
                 editable: bool = False, in_view: bool = True) -> None:
        self.ref, self.role, self.name, self.type = ref, role, name, type
        self.attrs = {"type": type, **(attrs or {})}
        self.editable, self.in_view = editable, in_view
        self.clicked = False
        self.value: str | None = None
        self.pressed: list[str] = []

    def record(self) -> dict[str, Any]:
        extra = " ".join(self.attrs.get(a, "") for a in ("name", "aria-label", "placeholder", "autocomplete"))
        return {"ref": self.ref, "role": self.role, "tag": "input" if self.editable else "button",
                "type": self.type, "name": self.name, "attrs": extra.strip(), "inView": self.in_view}


class FakeLocator:
    def __init__(self, items: list[FakeElement]) -> None:
        self.items = items

    async def count(self) -> int:
        return len(self.items)

    @property
    def first(self) -> FakeLocator:
        return FakeLocator(self.items[:1])

    async def evaluate(self, script: str) -> str:
        return self.items[0].name

    async def click(self, timeout: int = 0) -> None:  # noqa: ASYNC109 (Playwright signature)
        self.items[0].clicked = True

    async def is_visible(self) -> bool:
        return True

    async def is_editable(self) -> bool:
        return self.items[0].editable

    async def get_attribute(self, name: str) -> str | None:
        return self.items[0].attrs.get(name)

    async def fill(self, value: str, timeout: int = 0) -> None:  # noqa: ASYNC109
        self.items[0].value = value

    async def press(self, key: str) -> None:
        self.items[0].pressed.append(key)


class FakeKeyboard:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def press(self, key: str) -> None:
        self.keys.append(key)


class FakeMouse:
    async def wheel(self, dx: float, dy: float) -> None:
        pass


class FakePage:
    def __init__(self, elements: list[FakeElement], url: str = "https://example.com/", title: str = "Example") -> None:
        self.elements = elements
        self.url = url
        self._title = title
        self.waits: list[tuple[str, int | None]] = []
        self.idle_raises = False
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()

    async def title(self) -> str:
        return self._title

    async def inner_text(self, selector: str) -> str:
        return "Welcome\nSome text"

    async def evaluate(self, script: str) -> Any:
        if script == ELEMENTS_JS:
            return [e.record() for e in self.elements]
        if script == "window.innerHeight":
            return 800
        return True  # animation-frame wait

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator([e for e in self.elements if f'[data-delta-ref="{e.ref}"]' == selector])

    async def wait_for_load_state(self, state: str, timeout: int | None = None) -> None:  # noqa: ASYNC109
        self.waits.append((state, timeout))
        if state == "networkidle" and self.idle_raises:
            raise TimeoutError("network never went idle")


def setup(tmp_path, elements: list[FakeElement]) -> tuple[BrowserManager, FakePage]:
    mgr = BrowserManager(tmp_path)
    page = FakePage(elements)

    async def fake_page() -> FakePage:
        return page

    mgr.page = fake_page  # type: ignore[method-assign]
    mgr.current_url = page.url
    return mgr, page


def page_elements() -> list[FakeElement]:
    return [FakeElement(1, "link", "Next page"), FakeElement(2, "button", "Sign in"),
            FakeElement(3, "button", "Send"), FakeElement(4, "button", "Delete file"),
            FakeElement(5, "searchbox", "Search Wikipedia", type="search", editable=True),
            FakeElement(6, "textbox", "Message", editable=True),
            FakeElement(7, "textbox", "Password", type="password", editable=True)]


def test_parse_refs_and_listing_lines():
    raw = [{"ref": 12, "role": "button", "name": "Sign in", "inView": True},
           {"ref": "3", "role": "textbox", "name": "Email", "type": "email"},
           {"ref": 0, "role": "x"}, {"ref": "nope"}, "junk"]
    refs = parse_refs(raw)
    assert [r.ref for r in refs] == [12, 3]
    assert refs[0].line() == '[12] button "Sign in"'
    assert refs[1].line() == '[3] textbox "Email" (email)'
    assert parse_refs(None) == []


async def test_read_numbers_elements_and_remembers_them(tmp_path):
    mgr, _ = setup(tmp_path, page_elements())
    result = await BrowserRead(mgr).run(ReadArgs())
    assert result.ok
    assert '[2] button "Sign in"' in result.output
    assert "Welcome" in result.output
    assert mgr.refs[3].name == "Send" and mgr.refs_url == "https://example.com/"


async def test_risk_for_a_ref_uses_the_elements_real_name(tmp_path):
    mgr, _ = setup(tmp_path, page_elements())
    await BrowserRead(mgr).run(ReadArgs())
    click, pe = BrowserClick(mgr), PolicyEngine(Boundaries())
    assert click.risk_for(RefClickArgs(ref=1)) == Risk.ACT
    assert click.risk_for(RefClickArgs(ref=3)) == Risk.EXTERNAL
    assert click.risk_for(RefClickArgs(ref=4)) == Risk.DESTRUCTIVE
    assert pe.evaluate(click, RefClickArgs(ref=1)).verdict == Verdict.ALLOW
    assert pe.evaluate(click, RefClickArgs(ref=3)).verdict == Verdict.ASK
    # A harmless-looking text never overrides the element the ref points at.
    assert pe.evaluate(click, RefClickArgs(ref=3, text="Next")).verdict == Verdict.ASK
    assert "[3] 'Send'" in click.summary(RefClickArgs(ref=3))


async def test_fill_risk_for_a_ref_reads_the_fields_name(tmp_path):
    mgr, _ = setup(tmp_path, page_elements())
    await BrowserRead(mgr).run(ReadArgs())
    fill, pe = BrowserFill(mgr), PolicyEngine(Boundaries())
    assert pe.evaluate(fill, RefFillArgs(ref=5, value="Ada", submit=True)).verdict == Verdict.ALLOW
    assert pe.evaluate(fill, RefFillArgs(ref=6, value="hi", submit=True)).verdict == Verdict.ASK
    assert pe.evaluate(fill, RefFillArgs(ref=99, value="hi", submit=True)).verdict == Verdict.ASK  # unknown: ask
    assert "[6] 'Message'" in fill.summary(RefFillArgs(ref=6, value="hi"))


async def test_click_by_ref_acts_and_returns_a_snapshot(tmp_path):
    elements = page_elements()
    mgr, page = setup(tmp_path, elements)
    await BrowserRead(mgr).run(ReadArgs())
    result = await BrowserClick(mgr).run(RefClickArgs(ref=2))
    assert result.ok and elements[1].clicked
    assert "clicked 'Sign in'" in result.output
    assert "Page: Example — https://example.com/" in result.output
    assert '[1] link "Next page"' in result.output  # next step can use refs without browser_read
    assert ("networkidle", browser.SETTLE_IDLE_MS) in page.waits


async def test_stale_or_unknown_ref_refuses_without_acting(tmp_path):
    elements = page_elements()
    mgr, page = setup(tmp_path, elements)
    result = await BrowserClick(mgr).run(RefClickArgs(ref=2))  # never listed
    assert not result.ok and "browser_read" in result.output and not elements[1].clicked
    await BrowserRead(mgr).run(ReadArgs())
    page.elements = []  # navigated: the new document has no numbered elements
    result = await BrowserClick(mgr).run(RefClickArgs(ref=2))
    assert not result.ok and "browser_read" in result.output and not elements[1].clicked


async def test_click_refuses_when_the_element_became_riskier(tmp_path):
    elements = page_elements()
    mgr, _ = setup(tmp_path, elements)
    await BrowserRead(mgr).run(ReadArgs())
    elements[0].name = "Delete account"  # same ref, approved as "Next page"
    result = await BrowserClick(mgr).run(RefClickArgs(ref=1))
    assert not result.ok and not elements[0].clicked and "browser_read" in result.output


async def test_fill_by_ref_types_submits_and_keeps_the_password_rule(tmp_path):
    elements = page_elements()
    mgr, _ = setup(tmp_path, elements)
    await BrowserRead(mgr).run(ReadArgs())
    fill = BrowserFill(mgr)
    result = await fill.run(RefFillArgs(ref=5, value="Ada Lovelace", submit=True))
    assert result.ok and elements[4].value == "Ada Lovelace" and elements[4].pressed == ["Enter"]
    assert mgr.last_fill_was_search and "Page: Example" in result.output
    refused = await fill.run(RefFillArgs(ref=7, value="hunter2"))
    assert not refused.ok and "REFUSED" in refused.output and elements[6].value is None
    not_field = await fill.run(RefFillArgs(ref=2, value="x"))
    assert not not_field.ok and "not a field" in not_field.output


async def test_fill_refuses_submit_when_search_box_turned_into_a_form(tmp_path):
    elements = page_elements()
    mgr, _ = setup(tmp_path, elements)
    await BrowserRead(mgr).run(ReadArgs())
    args = RefFillArgs(ref=5, value="hi", submit=True)
    assert BrowserFill(mgr).risk_for(args) == Risk.ACT  # approved as a search
    elements[4].attrs = {"type": "text"}
    elements[4].role, elements[4].name = "textbox", "Comment"
    result = await BrowserFill(mgr).run(args)
    assert not result.ok and elements[4].value is None


def test_text_and_field_still_work_and_one_target_is_required(tmp_path):
    mgr = BrowserManager(tmp_path)
    assert BrowserClick(mgr).risk_for(RefClickArgs(text="Submit")) == Risk.EXTERNAL
    assert BrowserClick(mgr).risk_for(ClickArgs(text="Next")) == Risk.ACT  # old args type still accepted
    assert BrowserFill(mgr).risk_for(FillArgs(field="Search", value="x", submit=True)) == Risk.ACT
    with pytest.raises(ValidationError):
        RefClickArgs()
    with pytest.raises(ValidationError):
        RefFillArgs(value="x")
    with pytest.raises(ValidationError):  # the user's-browser tools keep requiring text
        ClickArgs()  # type: ignore[call-arg]


def test_snapshot_is_bounded_and_lists_on_screen_elements_first():
    many = [ElementRef(ref=i, role="link", name=f"Result number {i} " + "x" * 60, in_view=i > 100)
            for i in range(1, 301)]
    text = snapshot_text("Results", "https://s.example/", many)
    assert len(text) < SNAPSHOT_CHARS + 400
    assert text.splitlines()[1].startswith("[101]")
    assert "more elements" in text


async def test_settle_gives_up_quietly_on_busy_pages(tmp_path):
    page = FakePage([])
    page.idle_raises = True
    await settle(page)  # must not raise
    assert [w[0] for w in page.waits] == ["domcontentloaded", "networkidle"]


async def test_press_and_scroll_return_snapshots(tmp_path):
    mgr, page = setup(tmp_path, page_elements())
    mgr.last_fill_was_search = True
    pressed = await BrowserPress(mgr).run(PressArgs(key="Enter"))
    assert pressed.ok and page.keyboard.keys == ["Enter"] and "[1] link" in pressed.output
    assert not mgr.last_fill_was_search
    scrolled = await BrowserScroll(mgr).run(ScrollArgs())
    assert scrolled.ok and "Page: Example" in scrolled.output and 3 in mgr.refs


class _NetPage:
    """Just enough of a page for settle(): load states return at once, requests are events."""

    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}

    def on(self, event: str, fn: Any) -> None:
        self.handlers.setdefault(event, []).append(fn)

    def emit(self, event: str, req: object) -> None:
        for fn in self.handlers.get(event, []):
            fn(req)

    async def wait_for_load_state(self, state: str, **kw: Any) -> None:
        return None  # "networkidle" already reached: returns at once, as in Playwright

    async def evaluate(self, js: str) -> bool:
        return True


async def test_settle_waits_for_requests_the_action_started():
    import asyncio
    import time

    page = _NetPage()
    browser.track_requests(page)
    browser.track_requests(page)  # once per page
    assert len(page.handlers["request"]) == 1
    req = object()
    page.emit("request", req)  # e.g. a click that fetches data

    async def finish() -> None:
        await asyncio.sleep(0.4)
        page.emit("requestfinished", req)

    task = asyncio.create_task(finish())
    start = time.monotonic()
    await settle(page)
    took = time.monotonic() - start
    await task
    assert 0.4 + browser.QUIET_S - 0.05 <= took < browser.SETTLE_IDLE_MS / 1000


async def test_settle_gives_up_on_requests_that_never_finish():
    import time

    page = _NetPage()
    browser.track_requests(page)
    page.emit("request", object())  # a live feed that never ends
    start = time.monotonic()
    await settle(page, idle_ms=300)
    assert time.monotonic() - start < 1.0
