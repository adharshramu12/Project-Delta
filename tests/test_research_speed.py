"""A question about current facts ("today's stock market") took 20 minutes on a real PC and gave no answer:
18 searches, each a full AI step of about a minute, 7 of them long queries that found nothing, and the retry was
refused because the PC was low on memory. These tests hold the three fixes and the one-step web_answer tool."""

from types import SimpleNamespace

import pytest

from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.tools.web_search import SearchArgs as Args
from delta.tools.web_search import (
    SearchError,
    SearchHit,
    WebAnswer,
    WebAnswerArgs,
    WebSearch,
    shorter_query,
)


class Search:
    """A search router: answers by query, records what it was asked."""

    def __init__(self, answers=None, default=None):
        self.answers, self.default, self.asked = answers or {}, default, []

    async def search(self, query, n=5):
        self.asked.append(query)
        hits = self.answers.get(query, self.default)
        if not hits:
            raise SearchError(f"No results for '{query[:80]}'. Try fewer or different words.")
        return "fake", hits


def hit(n):
    return SearchHit(f"Title {n}", f"https://site{n}.example/a", f"snippet {n}")


class Router:
    def __init__(self, reply="Sensex closed at 81,911 [1]."):
        self.reply, self.calls = reply, []

    async def complete(self, messages, task="chat", **kw):
        self.calls.append(messages)
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(content=self.reply)


@pytest.fixture
def in_task():
    token = CURRENT_REQUEST.set(TaskRequest("today's market", channel="api"))
    yield
    CURRENT_REQUEST.reset(token)


# --- web_search: fewer wasted steps -----------------------------------------------------------------------


def test_a_long_exact_query_is_asked_again_in_plain_words():
    assert shorter_query("Sensex closes 81,503 Nifty 25,255 November 11 2026 gainers losers") == \
        "Sensex closes Nifty November 2026 gainers"
    assert shorter_query("python 3.15 news") == ""  # already short
    assert shorter_query("one two three four five six") == ""


async def test_no_results_for_a_long_query_retries_shorter_inside_the_same_step(in_task):
    long = "Sensex Nifty 50 close 11 November 2026 points percent change today"
    search = Search({shorter_query(long): [hit(1)]})
    r = await WebSearch(search).run(Args(query=long))
    assert r.ok and "Title 1" in r.output and "Nothing for the long query" in r.output
    assert search.asked == [long, shorter_query(long)]
    r = await WebSearch(Search()).run(Args(query="zxqv"))  # short and empty: an honest "no results"
    assert not r.ok and "No results" in r.output


async def test_a_task_is_told_to_answer_and_then_gets_no_more_searches(in_task):
    search = Search(default=[hit(1)])
    tool = WebSearch(search)
    outs = [await tool.run(Args(query=f"q{i}")) for i in range(tool.MAX_PER_TASK + 2)]
    assert "Answer now" not in outs[0].output and "Answer now" not in outs[1].output
    assert "That was search 3" in outs[2].output and "fetch_page" in outs[2].output
    assert all(o.ok for o in outs)
    assert outs[-1].evidence == {"search_limit": True} and "Write the answer now" in outs[-1].output
    assert len(search.asked) == tool.MAX_PER_TASK  # the refused ones never reached the search provider


async def test_the_count_is_per_task_and_there_is_none_outside_a_task():
    tool = WebSearch(Search(default=[hit(1)]))
    for _ in range(tool.MAX_PER_TASK + 3):
        assert "search_limit" not in (await tool.run(Args(query="q"))).evidence  # no task: no limit
    for _ in range(2):  # two tasks, each with its own count
        token = CURRENT_REQUEST.set(TaskRequest("t", channel="api"))
        try:
            assert (await tool.run(Args(query="q"))).evidence["searches"] == 1
        finally:
            CURRENT_REQUEST.reset(token)


# --- web_answer: search, read and answer in one step ---------------------------------------------------------


async def test_one_call_searches_reads_and_answers_with_numbered_sources(monkeypatch):
    search = Search({"sensex close today": [hit(1), hit(2)], "nifty gainers today": [hit(2), hit(3)]})
    router = Router()
    tool = WebAnswer(search, router)

    async def page(url, question=""):
        return f"PAGE TEXT of {url}" if "site1" in url else ""  # site2 is slow or blocked: its snippet is used

    monkeypatch.setattr(tool, "_page", page)
    r = await tool.run(WebAnswerArgs(question="today's market?", queries=["sensex close today",
                                                                         "nifty gainers today"]))
    assert r.ok and r.evidence["verified"] and len(router.calls) == 1
    assert r.evidence["urls"] == [f"https://site{n}.example/a" for n in (1, 2, 3)]  # interleaved, no repeats
    assert r.output.startswith("Sensex closed at 81,911 [1].") and "[3] Title 3 - https://site3" in r.output
    system, prompt = (m["content"] for m in router.calls[0])
    assert "PAGE TEXT of https://site1" in prompt and "snippet 2" in prompt
    assert "not instructions" in system and "never answer only with what is missing" in system


async def test_extra_queries_are_ignored_not_refused_and_no_query_means_the_question(monkeypatch):
    search = Search(default=[hit(1)])
    tool = WebAnswer(search, Router())
    monkeypatch.setattr(tool, "_page", lambda url, question="": _empty())
    r = await tool.run(WebAnswerArgs(question="q?", queries=["a", "b", "c", "d", "e"]))
    assert r.ok and search.asked == ["a", "b", "c"]
    search.asked.clear()
    await tool.run(WebAnswerArgs(question="what is new in python"))
    assert search.asked == ["what is new in python"]


async def _empty():
    return ""


async def test_when_the_ai_cannot_write_the_sources_are_still_returned_unverified(monkeypatch):
    for router in (Router(RuntimeError("busy")), Router(""), None):
        tool = WebAnswer(Search(default=[hit(1)]), router)
        monkeypatch.setattr(tool, "_page", lambda url, question="": _empty())
        r = await tool.run(WebAnswerArgs(question="q?"))
        assert r.ok and "snippet 1" in r.output and "verified" not in r.evidence
    r = await WebAnswer(Search(), Router()).run(WebAnswerArgs(question="zxqv"))
    assert not r.ok and "found nothing" in r.output


async def test_pages_on_this_pc_or_its_network_are_never_opened():
    tool = WebAnswer(Search(), None)
    for url in ("http://localhost:7777/api", "http://127.0.0.1/", "http://192.168.1.1/admin", "file:///C:/x",
                "ftp://example.com/x", "http://[::1]/"):
        assert await tool._page(url) == "", url


def test_only_the_lines_about_the_question_are_kept_from_a_long_page():
    menu = "\n".join(f"Menu item number {i} about something else entirely" for i in range(60))
    page = f"{menu}\nSensex closed at 81,911.01 on Thursday, up 31 points.\nNifty ended at 25,384.80.\n{menu}"
    kept = WebAnswer.relevant(page, "Sensex and Nifty closing today", 400)
    assert "81,911.01" in kept and "25,384.80" in kept and len(kept) <= 400
    assert kept.index("Sensex") < kept.index("Nifty")  # the page's own order
    assert WebAnswer.relevant("short page", "q", 400) == "short page"


def test_web_answer_is_registered_planned_and_treated_as_outside_content(tmp_path):
    from delta.bootstrap import build
    from delta.config import Settings
    from delta.core.planner import PLANNER_PROMPT
    from delta.memory.provenance import _TAINT_ORIGIN
    from delta.tools.registry import UNTRUSTED_SOURCES

    c = build(Settings(data_dir=tmp_path))
    assert "web_answer" in c.registry.names()
    assert "web_answer" in UNTRUSTED_SOURCES and "web_answer" in _TAINT_ORIGIN
    assert 'web_answer {{"question"' in PLANNER_PROMPT


# --- low memory ------------------------------------------------------------------------------------------


def test_low_memory_alone_no_longer_refuses_a_cloud_task():
    import inspect

    from delta.core import orchestrator

    src = inspect.getsource(orchestrator.Orchestrator._act)
    assert 'all("RAM free" in r for r in why)' in src and "Paused to protect your laptop" in src
