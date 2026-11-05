"""Delta's browser tools against a real Chrome/Edge: refs, snapshots, stale refs and risk checks.

Drives the real tool classes (BrowserOpen/Read/Click/Fill/Press/Scroll/Back) through Delta's own
BrowserManager, which launches the installed browser exactly as on a user's PC. Run with
DELTA_E2E=1 pytest -m e2e."""

from __future__ import annotations

import os
import re

import pytest

from delta.config import Boundaries
from delta.safety.policy import PolicyEngine, Verdict
from delta.tools.base import Risk, ToolResult
from delta.tools.browser import (
    BrowserBack,
    BrowserClick,
    BrowserFill,
    BrowserManager,
    BrowserOpen,
    BrowserPress,
    BrowserRead,
    BrowserScroll,
    ElementRef,
    OpenArgs,
    PressArgs,
    ReadArgs,
    RefClickArgs,
    RefFillArgs,
    ScrollArgs,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]

SECRET = "Hunter2-Prefilled!"


def ref_of(mgr: BrowserManager, role: str, name: str) -> ElementRef:
    found = [e for e in mgr.refs.values() if e.role == role and e.name == name]
    assert len(found) == 1, f"expected one {role} '{name}', got {found} in {list(mgr.refs.values())}"
    return found[0]


def ok(result: ToolResult) -> str:
    assert result.ok, result.output
    return result.output


async def text_of(mgr: BrowserManager, selector: str) -> str:
    page = await mgr.page()
    return str(await page.inner_text(selector))


async def open_page(mgr: BrowserManager, url: str) -> str:
    return ok(await BrowserOpen(mgr).run(OpenArgs(url=url)))


# (a) search box + submit -------------------------------------------------------------------------

async def test_search_box_fill_submit_press_scroll_back(mgr: BrowserManager, site: str) -> None:
    out = await open_page(mgr, f"{site}/search.html")
    assert "Docs search" in out
    box = ref_of(mgr, "searchbox", "Search docs")
    assert f'[{box.ref}] searchbox "Search docs"' in out  # the agent is shown the ref it will use

    fill = BrowserFill(mgr)
    args = RefFillArgs(ref=box.ref, value="element refs", submit=True)
    assert fill.risk_for(args) == Risk.ACT  # submitting a search box needs no approval
    assert PolicyEngine(Boundaries()).evaluate(fill, args).verdict == Verdict.ALLOW
    out = ok(await fill.run(args))
    assert "results.html?q=element+refs" in out
    assert await text_of(mgr, "#out") == "Results for: element refs"
    assert "New search" in out  # act-and-observe: the new page's elements came back with the action

    ok(await BrowserBack(mgr).run(BrowserBack.Args()))
    assert (await mgr.page()).url.endswith("/search.html")

    # Typing then pressing Enter in the search box: Enter is free right after a search fill.
    box = ref_of(mgr, "searchbox", "Search docs")
    ok(await fill.run(RefFillArgs(ref=box.ref, value="pressed enter")))
    press = BrowserPress(mgr)
    assert press.risk_for(PressArgs(key="Enter")) == Risk.ACT
    ok(await press.run(PressArgs(key="Enter")))
    assert await text_of(mgr, "#out") == "Results for: pressed enter"
    assert press.risk_for(PressArgs(key="Enter")) == Risk.EXTERNAL  # one free Enter per typed search

    await open_page(mgr, f"{site}/search.html")
    page = await mgr.page()
    assert await page.evaluate("window.scrollY") == 0
    ok(await BrowserScroll(mgr).run(ScrollArgs(direction="down", pages=2)))
    assert await page.evaluate("window.scrollY") > 0


# (b) login form with duplicate "Sign in" buttons -------------------------------------------------

async def test_login_refs_click_the_visible_sign_in(mgr: BrowserManager, site: str) -> None:
    out = await open_page(mgr, f"{site}/login.html")
    signin = ref_of(mgr, "button", "Sign in")  # hidden duplicates get no ref at all
    assert out.count('button "Sign in"') == 1

    user = ref_of(mgr, "textbox", "Username")
    ok(await BrowserFill(mgr).run(RefFillArgs(ref=user.ref, value="ada")))
    page = await mgr.page()
    assert await page.input_value("#user") == "ada"

    pw = ref_of(mgr, "textbox", "Password")
    refused = await BrowserFill(mgr).run(RefFillArgs(ref=pw.ref, value="guess"))
    assert not refused.ok and "REFUSED" in refused.output
    assert await page.input_value("#pass") == SECRET  # untouched

    ok(await BrowserClick(mgr).run(RefClickArgs(ref=signin.ref)))
    assert await text_of(mgr, "#status") == "RIGHT: visible Sign in clicked"

    # Clicking by text also lands on the visible button, not the hidden ones first in the DOM.
    await page.evaluate("document.getElementById('status').textContent = 'reset'")
    ok(await BrowserClick(mgr).run(RefClickArgs(text="Sign in")))
    assert await text_of(mgr, "#status") == "RIGHT: visible Sign in clicked"


# (c) SPA: elements added by JS after a click -----------------------------------------------------

async def test_spa_snapshot_new_refs_and_stale_ref(mgr: BrowserManager, site: str) -> None:
    await open_page(mgr, f"{site}/spa.html")
    load = ref_of(mgr, "button", "Load items")
    clear = ref_of(mgr, "button", "Clear list")

    out = ok(await BrowserClick(mgr).run(RefClickArgs(ref=load.ref)))
    # The click's own snapshot already shows what the slow API call rendered, with fresh refs.
    for name in ("Open report", "Archive thread", "Star message"):
        assert f'button "{name}"' in out, out
    star = ref_of(mgr, "button", "Star message")
    assert star.ref > max(load.ref, clear.ref)  # new elements get new numbers
    assert ref_of(mgr, "button", "Load items").ref == load.ref  # existing ones keep theirs

    ok(await BrowserClick(mgr).run(RefClickArgs(ref=star.ref)))
    assert await text_of(mgr, "#picked") == "picked Star message"

    ok(await BrowserClick(mgr).run(RefClickArgs(ref=clear.ref)))
    stale = await BrowserClick(mgr).run(RefClickArgs(ref=star.ref))
    assert not stale.ok
    assert f"element [{star.ref}] is no longer on the page" in stale.output
    assert "browser_read" in stale.output

    # browser_read lists the page as it is now.
    out = ok(await BrowserRead(mgr).run(ReadArgs()))
    assert "Star message" not in out and 'button "Load items"' in out


# (d) a Send button reached by ref is still risky ---------------------------------------------------

async def test_send_by_ref_is_still_risky(mgr: BrowserManager, site: str) -> None:
    await open_page(mgr, f"{site}/compose.html")
    click, pe = BrowserClick(mgr), PolicyEngine(Boundaries())
    send = ref_of(mgr, "button", "Send")
    trash = ref_of(mgr, "button", "Delete draft")
    preview = ref_of(mgr, "button", "Preview")

    assert click.risk_for(RefClickArgs(ref=send.ref)) == Risk.EXTERNAL
    assert click.risk_for(RefClickArgs(ref=send.ref, text="Next")) == Risk.EXTERNAL  # text cannot launder a ref
    assert click.risk_for(RefClickArgs(ref=trash.ref)) == Risk.DESTRUCTIVE
    assert click.risk_for(RefClickArgs(ref=preview.ref)) == Risk.ACT
    assert pe.evaluate(click, RefClickArgs(ref=send.ref)).verdict == Verdict.ASK
    assert pe.evaluate(click, RefClickArgs(ref=preview.ref)).verdict == Verdict.ALLOW
    assert "'Send'" in click.summary(RefClickArgs(ref=send.ref))  # the approval names what is clicked

    # The page relabels a harmless button after it was listed: the click is refused, not sent.
    page = await mgr.page()
    await page.evaluate("document.getElementById('preview').textContent = 'Send now'")
    refused = await click.run(RefClickArgs(ref=preview.ref))
    assert not refused.ok and "now reads 'Send now'" in refused.output
    assert await text_of(mgr, "#status") == "draft"


# (e) password values never appear in read output ---------------------------------------------------

async def test_password_values_never_in_read_output(mgr: BrowserManager, site: str) -> None:
    outputs = [await open_page(mgr, f"{site}/login.html")]
    page = await mgr.page()
    typed = "Typed-By-User-77"
    await page.fill("#pass", typed)  # the user typing their password into the page themselves
    outputs.append(ok(await BrowserRead(mgr).run(ReadArgs())))
    outputs.append(ok(await BrowserRead(mgr).run(ReadArgs(focus="password"))))
    user = ref_of(mgr, "textbox", "Username")
    outputs.append(ok(await BrowserFill(mgr).run(RefFillArgs(ref=user.ref, value="ada"))))
    outputs.append(ok(await BrowserClick(mgr).run(RefClickArgs(text="Sign in"))))
    for out in outputs:
        assert SECRET not in out and typed not in out, out
        assert not re.search(r"Hunter2|Typed-By-User", out), out
    assert any('textbox "Password" (password)' in out for out in outputs)  # listed, value never shown
