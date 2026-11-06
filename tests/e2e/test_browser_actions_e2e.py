"""The extra browser actions in a real Chrome/Edge: dropdown, checkbox, file upload, hover menu, waits, a new tab,
a confirm dialog, an element inside an iframe and an infinite list. Each action checks its own result.
Run with DELTA_E2E=1 pytest -m e2e."""

from __future__ import annotations

import os

import pytest

from delta.tools.browser import (
    BrowserClick,
    BrowserManager,
    BrowserOpen,
    BrowserScroll,
    OpenArgs,
    RefClickArgs,
    ScrollArgs,
)
from delta.tools.browser_actions import (
    BrowserCheck,
    BrowserDialog,
    BrowserHover,
    BrowserSelect,
    BrowserTabs,
    BrowserUpload,
    BrowserWait,
    CheckArgs,
    DialogArgs,
    SelectArgs,
    TabsArgs,
    UploadArgs,
    WaitArgs,
    _Target,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]


def ref(mgr: BrowserManager, name: str, frame: bool = False) -> int:
    found = [e.ref for e in mgr.refs.values() if e.name == name and bool(e.frame) == frame]
    assert found, f"no element '{name}' in {[e.line() for e in mgr.refs.values()]}"
    return found[0]


async def test_extra_actions(mgr: BrowserManager, site: str, tmp_path):
    assert (await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/actions.html"))).ok
    page = await mgr.page()

    r = await BrowserSelect(mgr).run(SelectArgs(label="Semester", option="Seventh"))
    assert r.ok, r.output
    assert await page.input_value("#sem") == "7"
    bad = await BrowserSelect(mgr).run(SelectArgs(label="Semester", option="Ninth"))
    assert not bad.ok and "Seventh" in bad.output  # says which options there are

    r = await BrowserCheck(mgr).run(CheckArgs(label="I agree", checked=True))
    assert r.ok, r.output
    assert await page.is_checked("#agree")

    doc = tmp_path / "note.txt"
    doc.write_text("hello", encoding="utf-8")
    r = await BrowserUpload(mgr).run(UploadArgs(label="Attachment", path=str(doc)))
    assert r.ok, r.output
    assert await page.evaluate("document.getElementById('file').files[0].name") == "note.txt"

    r = await BrowserHover(mgr).run(_Target(label="Reports"))
    assert r.ok, r.output
    assert await page.is_visible("#att")

    assert (await BrowserClick(mgr).run(RefClickArgs(text="Load later"))).ok
    r = await BrowserWait(mgr).run(WaitArgs(until="text", text="Loaded", timeout_s=5))
    assert r.ok, r.output
    late = await BrowserWait(mgr).run(WaitArgs(until="text", text="Never shows", timeout_s=1))
    assert not late.ok and "did not happen" in late.output

    # a confirm dialog: cancelled by default and reported; accepted only when asked for
    r = await BrowserClick(mgr).run(RefClickArgs(text="Remove draft"))
    assert r.ok and "Delete the draft?" in r.output and "dismissed" in r.output
    assert await page.inner_text("#out") == "kept"
    assert (await BrowserDialog(mgr).run(DialogArgs(accept=True))).ok
    await BrowserClick(mgr).run(RefClickArgs(text="Remove draft"))
    assert await page.inner_text("#out") == "deleted"

    # an element inside an iframe gets a ref like any other and can be clicked
    await BrowserScroll(mgr).run(ScrollArgs(direction="down"))
    inner = ref(mgr, "Press me", frame=True)
    assert inner > 1000
    r = await BrowserClick(mgr).run(RefClickArgs(ref=inner))
    assert r.ok, r.output
    assert await page.frame_locator("#inner").locator("button").inner_text() == "Pressed"

    # an infinite list inside a box: scrolled until nothing new loads
    feed = await page.evaluate("document.getElementById('feed').children.length")
    assert feed == 5
    r = await BrowserScroll(mgr).run(ScrollArgs(ref=ref(mgr, "Row 1"), load_more=True))
    assert r.ok, r.output
    assert await page.evaluate("document.getElementById('feed').children.length") == 20

    # a link that opens a new tab: Delta works in it and can switch back
    r = await BrowserClick(mgr).run(RefClickArgs(text="Open results in a new tab"))
    assert r.ok and "new tab" in r.output, r.output
    assert (await mgr.page()).url.endswith("/results.html")
    tabs = await BrowserTabs(mgr).run(TabsArgs(action="list"))
    assert "actions.html" in tabs.output and "(current)" in tabs.output
    r = await BrowserTabs(mgr).run(TabsArgs(action="switch", index=1))
    assert r.ok and (await mgr.page()).url.endswith("/actions.html")


async def test_click_on_a_covered_element_says_why(mgr: BrowserManager, site: str):
    await BrowserOpen(mgr).run(OpenArgs(url=f"{site}/actions.html"))
    page = await mgr.page()
    await page.evaluate("""() => { const d = document.createElement('div'); d.id = 'cover';
      d.style = 'position:fixed;inset:0;background:rgba(0,0,0,.3);z-index:9'; document.body.appendChild(d); }""")
    r = await BrowserClick(mgr).run(RefClickArgs(text="Load later"))
    assert not r.ok and "covered" in r.output, r.output
