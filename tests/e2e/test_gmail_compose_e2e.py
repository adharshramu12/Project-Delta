"""Gmail's compose window in a real Chrome/Edge (a local stand-in page with Gmail's names, labels and suggestion
behaviour, tests/e2e/site/gmail_compose.html): the PageDriver reads it back, fixes what the compose link got wrong,
picks the exact contact (not the highlighted one) and only then saves or sends. Run with DELTA_E2E=1 pytest -m e2e."""

from __future__ import annotations

import os
from urllib.parse import urlencode

import pytest

from delta.tools.gmail_compose import GmailComposer, PageDriver

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("DELTA_E2E") != "1", reason="real-browser e2e: set DELTA_E2E=1"),
]

TO, SUBJECT, BODY = ["ravi@example.com"], "Deck for Friday", "Hi Ravi,\n\nThe deck is ready.\n\nAdharsh"


def url(site: str, mode: str) -> str:
    return f"{site}/gmail_compose.html?" + urlencode({"mode": mode, "to": ",".join(TO), "su": SUBJECT, "body": BODY})


async def test_a_good_compose_is_saved_as_is(site, mgr):
    page = await mgr.page()
    out = await GmailComposer(PageDriver(page)).run(url(site, "ok"), TO, SUBJECT, BODY, "draft")
    assert out.ok, out.detail
    assert out.fixed == []
    assert await page.evaluate("window.__saved") == 1
    assert await page.evaluate("window.__sent") is None


@pytest.mark.parametrize("mode", ["empty", "swap", "loose"])
async def test_a_wrong_compose_is_fixed_then_sent_to_the_exact_contact(site, mgr, mode):
    page = await mgr.page()
    out = await GmailComposer(PageDriver(page)).run(url(site, mode), TO, SUBJECT, BODY, "send")
    assert out.ok, out.detail
    assert out.fixed
    sent = await page.evaluate("window.__sent")
    assert sent["to"] == TO  # never ravi.k@gmail.com, the contact the page highlights first
    assert sent["subject"] == SUBJECT and sent["loose"] == ""
    assert " ".join(sent["body"].split()) == " ".join(BODY.split())


async def test_nothing_is_sent_when_the_compose_window_is_missing(site, mgr):
    page = await mgr.page()
    out = await GmailComposer(PageDriver(page), ready_wait_s=2).run(f"{site}/note.html", TO, SUBJECT, BODY, "send")
    assert not out.ok and "nothing was sent" in out.detail
