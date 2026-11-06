"""Writer: platform posts (length, layout, retry) and prepare_post, which must never publish."""

import ast
import inspect
from pathlib import Path

from delta.addons.writer import post as posts
from delta.addons.writer import publish
from delta.addons.writer.tools import PreparePost, PreparePostArgs, WritePost, WritePostArgs
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.tools.base import Risk

LINKEDIN_OK = "\n\n".join([
    'Most students apply to 200 jobs and hear nothing back.',
    'I did the same in my third year. Then I stopped and looked at what was going wrong.',
    'My resume listed tools. It never said what I built with them, or who used it.',
    'So I rewrote it around three projects. One had 40 real users from my own college. '
    'That single line got me more calls than the whole old resume.',
    'Recruiters read fast. They want proof, not adjectives. A number beats a paragraph.',
    'If you are in final year, try this tonight. Pick one project. '
    'Write one line about who used it and what changed for them.',
    'It takes ten minutes. It might change your placement season.',
    'What is one project you are proud of but never put on your resume?',
    '#placements #resume',
])

MEDIUM_OK = "# Why I Stopped Chasing Every AI Tool\n\nA simple filter that saved my evenings.\n\n" + "\n\n".join(
    f"## Part {n}\n\n" + " ".join(
        ["I tried a new tool every week last year and learnt very little from most of them."] * 3
        + ["Short point here."] + ["Then I picked two tools and used them daily for real college work."] * 3)
    for n in range(1, 5))


def fake_ask(*replies):
    calls = []

    async def ask(prompt):
        calls.append(prompt)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    return ask, calls


def test_good_linkedin_post_passes_all_checks():
    assert posts.check_post(LINKEDIN_OK, posts.PLATFORMS["linkedin"]) == []


def test_linkedin_rules_length_hashtags_markdown_and_hook():
    li = posts.PLATFORMS["linkedin"]
    assert li.markdown is False and li.max_hashtags == 3 and (li.min_words, li.max_words) == (120, 250)
    bad = "**Big news** " + "word " * 40 + "\n\n#a #b #c #d #e"
    found = {i.code for i in posts.check_post(bad, li)}
    assert {"length", "hashtags", "markdown", "hook"} <= found


def test_medium_layout_title_subtitle_sections():
    md = posts.PLATFORMS["medium"]
    assert md.markdown and md.max_hashtags == 0 and (md.min_words, md.max_words) == (600, 1200)
    assert not [i for i in posts.check_post(MEDIUM_OK, md) if i.code == "layout"]
    no_title = MEDIUM_OK.replace("# Why", "Why", 1)
    assert any(i.code == "layout" for i in posts.check_post(no_title, md))
    tagged = MEDIUM_OK + "\n\n#ai"
    assert any(i.code == "hashtags" for i in posts.check_post(tagged, md))


async def test_generate_fixes_mechanics_without_a_second_call():
    slipped = LINKEDIN_OK.replace("Recruiters read fast.", "Recruiters read fast \u2014 really fast \U0001F680.")
    ask, calls = fake_ask(slipped)
    res = await posts.generate_post(ask, posts.PLATFORMS["linkedin"], "resume tips")
    assert len(calls) == 1 and res.clean and "\u2014" not in res.text and "\U0001F680" not in res.text


async def test_generate_retries_once_then_reports_what_remains():
    ask, calls = fake_ask("Too short.", "Still short.")
    res = await posts.generate_post(ask, posts.PLATFORMS["linkedin"], "x")
    assert len(calls) == 2 and res.attempts == 2 and not res.clean
    assert "broke these rules" in calls[1] and any(i.code == "length" for i in res.issues)


async def test_prompt_carries_rules_platform_layout_and_voice():
    ask, calls = fake_ask(LINKEDIN_OK)
    await posts.generate_post(ask, posts.PLATFORMS["linkedin"], "AI for students", notes="40 users",
                              voice="i write short. lowercase sometimes.")
    p = calls[0]
    assert "Indian English" in p and "em dashes" in p and "Plain text only" in p
    assert "40 users" in p and "lowercase sometimes" in p and "delve" in p


def test_voice_file_is_optional(tmp_path):
    assert posts.load_voice(tmp_path / "missing.txt") == ""
    f = tmp_path / "my-posts.txt"
    f.write_text("old post one\n\nold post two", encoding="utf-8")
    assert "old post two" in posts.load_voice(f, "pasted sample")


class Cfg:
    def __init__(self, folder):
        self.output_dir = str(folder)
        self.voice_file = ""
        self.substack_publication = ""
        self.model_task = "plan"


class FakeRouter:
    def __init__(self, text):
        self.text = text
        self.calls = []

    async def complete(self, messages, task="chat", **kw):
        self.calls.append((messages, task))
        return type("R", (), {"content": self.text})()


async def test_write_post_tool_returns_text_and_never_writes_unless_asked(tmp_path):
    tool = WritePost(FakeRouter(LINKEDIN_OK), Cfg(tmp_path))
    args = WritePostArgs(topic="resume tips")
    assert tool.risk_for(args) == Risk.READ and tool.paths(args) == []
    r = await tool.run(args)
    assert r.ok and r.evidence["checks_passed"] and "Most students apply" in r.output
    assert not list(tmp_path.iterdir())
    saved = WritePostArgs(topic="resume tips", save=True)
    assert tool.risk_for(saved) == Risk.WRITE
    r2 = await tool.run(saved)
    assert Path(r2.evidence["path"]).read_text(encoding="utf-8").startswith("Most students")


async def test_prepare_post_copies_and_opens_compose_page_and_never_publishes(tmp_path, monkeypatch):
    copied, opened = [], []
    monkeypatch.setattr(publish, "copy_to_clipboard", lambda t: copied.append(t) or True)
    monkeypatch.setattr(publish, "open_in_user_browser", lambda u: opened.append(u) or True)
    tool = PreparePost(None, Cfg(tmp_path))
    r = await tool.run(PreparePostArgs(text=LINKEDIN_OK, platform="linkedin"))
    assert r.ok and r.evidence["published"] is False
    assert opened == ["https://www.linkedin.com/feed/?shareActive=true"] and copied
    assert "press Post yourself" in r.output
    assert tool.risk == Risk.ACT


def test_prepare_post_code_has_no_way_to_click_or_submit():
    """Static guarantee: the publish module and prepare_post import no browser/desktop automation and call no
    click, fill, press, type or submit. Posting stays a human action (LinkedIn User Agreement 8.2)."""
    for obj in (publish, PreparePost):
        src = inspect.getsource(obj)
        tree = ast.parse(src)
        names = {n.attr if isinstance(n, ast.Attribute) else getattr(n, "id", "") for n in ast.walk(tree)
                 if isinstance(n, ast.Attribute | ast.Name)}
        assert not names & {"click", "fill", "press", "type_text", "submit", "send_keys", "keyboard", "mouse"}
        imported = " ".join(ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Import | ast.ImportFrom))
        for banned in ("playwright", "pyautogui", "pywinauto", "delta.tools.browser", "delta.tools.user_browser",
                       "delta.tools.desktop"):
            assert banned not in imported


async def test_prepare_post_from_the_phone_opens_nothing_on_the_pc(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(publish, "open_in_user_browser", lambda u: opened.append(u) or True)
    token = CURRENT_REQUEST.set(TaskRequest("post it", channel="telegram", user="1"))
    try:
        r = await PreparePost(None, Cfg(tmp_path)).run(PreparePostArgs(text=LINKEDIN_OK))
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok and opened == [] and r.evidence["published"] is False


def test_substack_compose_url_uses_the_publication():
    sub = posts.PLATFORMS["substack"]
    assert publish.compose_url(sub, "adharsh") == "https://adharsh.substack.com/publish/post"
    assert publish.compose_url(sub, "evil.com/x") == sub.compose_url  # only *.substack.com
