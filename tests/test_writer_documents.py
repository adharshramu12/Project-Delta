"""Writer: documents (layout from content, DOCX/Markdown/PDF/LaTeX), delivery routing and the download API."""

import asyncio
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from delta.addons.writer import latex
from delta.addons.writer.document import apply_rules, build_doc, list_documents, parse_markdown, unique_stem
from delta.addons.writer.render_docx import render_docx
from delta.addons.writer.render_html import render_html
from delta.addons.writer.render_md import render_markdown
from delta.addons.writer.render_pdf import PdfUnavailable, find_pdf_browser, html_to_pdf, render_pdf
from delta.addons.writer.tools import MakeDocument, MakeDocumentArgs, delivery_route
from delta.core.state import CURRENT_REQUEST, TaskRequest
from delta.tools.base import Risk, ToolResult

REPORT = """---
title: Campus Placement Readiness
subtitle: What final-year students need before December
author: ARVA Talks Academy
date: 6 November 2026
---
## Summary
We surveyed 120 students and found most have not done a mock interview \u2014 a real gap.

> Note: The biggest gap is practice, not knowledge.

## Results
| Area | Ready | Not ready |
| --- | --- | --- |
| DSA | 48 | 72 |
| System design | 12 | 108 |

## Plan
1. Two mock interviews a week
2. One project with real users

- We will leverage seniors to organize mocks

The gain is $\\frac{a}{b}$ over six weeks.
"""

RESUME = """# Adharsh Kumar
---
## Skills
- Python, FastAPI
"""


def test_markdown_reader_finds_every_block_type():
    meta, blocks = parse_markdown(REPORT)
    assert meta["title"] == "Campus Placement Readiness" and meta["author"] == "ARVA Talks Academy"
    kinds = [b.type for b in blocks]
    for k in ("heading", "paragraph", "callout", "table", "numbered", "bullets"):
        assert k in kinds
    table = next(b for b in blocks if b.type == "table")
    assert table.rows[0] == ["Area", "Ready", "Not ready"] and len(table.rows) == 3


def test_writing_rules_apply_inside_documents_but_maths_is_kept():
    doc = apply_rules(build_doc("report", REPORT))
    text = render_markdown(doc)
    assert "\u2014" not in text and "leverage" not in text and "organise" in text
    assert "$\\frac{a}{b}$" in text  # maths keeps its backslashes


def test_layout_is_chosen_from_content():
    short = build_doc("report", REPORT)
    assert short.layout.template == "report" and short.layout.title_page is False and short.layout.page_size == "A4"
    long_md = REPORT + "\n".join(f"## Section {i}\n" + "Some words here for length. " * 40 for i in range(8))
    long = build_doc("report", long_md)
    assert long.layout.title_page and long.layout.toc and long.layout.header
    assert build_doc("assignment", REPORT).layout.title_page
    letter = build_doc("letter", "---\nfrom: A / Vijayawada\nto: HR / Acme\nsubject: Internship\n---\n"
                                 "Dear Sir,\n\nHello.")
    assert letter.layout.template == "letter" and letter.layout.font == "serif" and not letter.layout.title_page
    resume = build_doc("resume", "# Adharsh Kumar\n\n# Skills\n- Python")
    assert resume.title == "Adharsh Kumar" and resume.layout.template == "resume" and resume.layout.margins_mm < 20
    notes = build_doc("notes", "## A\n- b", page_size="Letter")
    assert notes.layout.template == "notes" and notes.layout.page_size == "Letter"
    wide = build_doc("report", "| " + " | ".join("abcdefgh") + " |\n|" + "---|" * 8 + "\n| "
                     + " | ".join("12345678") + " |")
    assert wide.layout.landscape


def test_html_escapes_content_and_has_no_scripts():
    doc = build_doc("notes", "<script>alert(1)</script>\n\n**bold** [x](javascript:alert(1)) [ok](https://a.in)")
    html = render_html(doc)
    assert "<script>" not in html and "&lt;script&gt;" in html and 'href="javascript' not in html
    assert '<a href="https://a.in">ok</a>' in html and "default-src 'none'" in html and "<b>bold</b>" in html


def test_docx_structure(tmp_path):
    doc = apply_rules(build_doc("report", REPORT))
    out = render_docx(doc, tmp_path / "r.docx")
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"[Content_Types].xml", "word/document.xml", "word/styles.xml", "word/numbering.xml",
                "word/_rels/document.xml.rels", "word/footer1.xml", "docProps/core.xml"} <= names
        body = z.read("word/document.xml").decode()
        styles = z.read("word/styles.xml").decode()
        assert 'w:pStyle w:val="Title"' in body and 'w:pStyle w:val="Heading2"' in body
        assert "<w:tblHeader/>" in body and "<w:cantSplit/>" in body  # header repeats, rows never split
        assert '<w:pgSz w:w="11906" w:h="16838"/>' in body  # A4
        assert "<w:numId" in body and 'w:styleId="Heading2"' in styles and 'w:val="en-IN"' in styles
        assert "PAGE" in z.read("word/footer1.xml").decode()
        assert "Campus Placement Readiness" in z.read("docProps/core.xml").decode()
        import xml.dom.minidom

        for n in names:
            if n.endswith((".xml", ".rels")):
                xml.dom.minidom.parseString(z.read(n))  # every part is well-formed XML


def test_letter_docx_has_sender_subject_and_signature(tmp_path):
    md = ("---\nfrom: Adharsh / Vijayawada\nto: HR / Acme\nsubject: Internship\n"
          "signature: Yours sincerely, / Adharsh\n---\nDear Sir,\n\nHello.")
    with zipfile.ZipFile(render_docx(build_doc("letter", md), tmp_path / "l.docx")) as z:
        body = z.read("word/document.xml").decode()
    assert "Subject: Internship" in body and "Yours sincerely," in body and 'w:val="right"' in body


def test_names_are_unique_and_listed(tmp_path):
    a = unique_stem(tmp_path, "My Report: Q3/Q4?")
    assert "/" not in a and "?" not in a and ":" not in a
    (tmp_path / f"{a}.pdf").write_bytes(b"%PDF")
    assert unique_stem(tmp_path, "My Report: Q3/Q4?") != a
    assert list_documents(tmp_path)[0]["name"] == f"{a}.pdf"


# --- PDF (real browser) -------------------------------------------------------------------------------

needs_browser = pytest.mark.skipif(find_pdf_browser() is None, reason="no Chrome/Edge/Chromium on this machine")


@needs_browser
async def test_pdf_render_smoke(tmp_path):
    out = await render_pdf(apply_rules(build_doc("report", REPORT)), tmp_path / "r.pdf")
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 2000


@needs_browser
async def test_pdf_page_fetches_nothing_and_runs_no_javascript(tmp_path):
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    html = (f'<html><head><link rel="stylesheet" href="{base}/css"></head><body><img src="{base}/img">'
            f'<iframe src="{base}/frame"></iframe><script>fetch("{base}/js"); document.title="ran";</script>'
            f'<p>hello</p></body></html>')
    try:
        out = await html_to_pdf(html, tmp_path / "x.pdf")
    finally:
        server.shutdown()
    assert out.read_bytes().startswith(b"%PDF") and hits == []


async def test_no_browser_is_reported_clearly(tmp_path, monkeypatch):
    import delta.addons.writer.render_pdf as rp

    monkeypatch.setattr(rp, "find_pdf_browser", lambda override="": None)
    with pytest.raises(PdfUnavailable):
        await rp.html_to_pdf("<p>x</p>", tmp_path / "x.pdf")


# --- LaTeX (optional) ---------------------------------------------------------------------------------


def test_latex_escapes_text_and_refuses_unsafe_maths():
    doc = build_doc("paper", "Costs 50% & more_than #1 {x}\n\nSafe $\\frac{1}{2}$ and bad $\\input{/etc/passwd}$\n\n"
                             "$$\n\\immediate\\write18{calc}\n$$")
    tex = latex.to_latex(doc, "pdflatex")
    assert "50\\% \\& more\\_than \\#1 \\{x\\}" in tex and "$\\frac{1}{2}$" in tex
    assert "\\input{" not in tex and "\\write18" not in tex and "\\immediate" not in tex
    assert latex.safe_math("x^2 + \\alpha") and not latex.safe_math("\\catcode`\\@=11")


@pytest.mark.skipif(latex.find_engine() is not None, reason="LaTeX is installed here")
def test_latex_is_optional_when_not_installed():
    assert latex.find_engine() is None


@pytest.mark.skipif(latex.find_engine() is None, reason="no LaTeX engine on PATH (optional)")
async def test_latex_compile_smoke(tmp_path):
    doc = apply_rules(build_doc("report", REPORT))
    out = await latex.compile_latex(latex.to_latex(doc, latex.find_engine()[0]), tmp_path / "r.pdf")
    assert out.read_bytes().startswith(b"%PDF")


async def test_latex_compile_runs_without_shell_escape(tmp_path, monkeypatch):
    seen = {}

    class Proc:
        returncode = 0

        async def communicate(self):
            (Path(seen["cwd"]) / "doc.pdf").write_bytes(b"%PDF-1.5")
            return b"ok", b""

    async def fake_exec(*cmd, cwd=None, env=None, **kw):
        seen.update(cmd=cmd, cwd=cwd, env=env)
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await latex.compile_latex("x", tmp_path / "o.pdf", ("pdflatex", "pdflatex"))
    assert "-no-shell-escape" in seen["cmd"] and "-shell-escape" not in seen["cmd"]
    assert seen["env"]["openin_any"] == "p" and seen["env"]["openout_any"] == "p"
    assert Path(seen["cwd"]) != tmp_path and not Path(seen["cwd"]).exists()  # temp dir, cleaned up
    await latex.compile_latex("x", tmp_path / "t.pdf", ("tectonic", "tectonic"))
    assert "--untrusted" in seen["cmd"]


# --- the tool: rendering, writing rules and delivery --------------------------------------------------


class Cfg:
    def __init__(self, folder):
        self.output_dir = str(folder)
        self.page_size = "A4"
        self.latex = "off"
        self.pdf_browser = ""
        self.model_task = "plan"


class FakeRouter:
    def __init__(self, text):
        self.text = text

    async def complete(self, messages, task="chat", **kw):
        return type("R", (), {"content": self.text})()


class FakeExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, name, args, task_id=""):
        self.calls.append((name, args, task_id))
        return ToolResult(True, "sent", {"delivered": True})


class FakeEvents:
    def __init__(self):
        self.events = []

    async def publish(self, event, **data):
        self.events.append((event, data))


def test_delivery_route():
    assert delivery_route("telegram") == "chat" and delivery_route("slack") == "chat"
    assert delivery_route("api") == "dashboard" and delivery_route(None) == "dashboard"


async def test_from_telegram_the_file_goes_back_through_send_file(tmp_path):
    tool = MakeDocument(FakeRouter(REPORT), Cfg(tmp_path / "docs"))
    tool.executor = FakeExecutor()
    req = TaskRequest("make a report", channel="telegram", user="42")
    token = CURRENT_REQUEST.set(req)
    try:
        r = await tool.run(MakeDocumentArgs(kind="report", content="notes", formats=["docx", "md"]))
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok and r.evidence["delivered"] and r.evidence["route"] == "chat"
    name, args, task_id = tool.executor.calls[0]
    assert name == "send_file" and task_id == req.id and args["path"].endswith(".docx")  # via the executor


async def test_on_the_dashboard_a_preview_event_is_published(tmp_path):
    events = FakeEvents()
    tool = MakeDocument(FakeRouter(REPORT), Cfg(tmp_path / "docs"), events)
    tool.executor = FakeExecutor()
    token = CURRENT_REQUEST.set(TaskRequest("make a report", channel="api"))
    try:
        r = await tool.run(MakeDocumentArgs(kind="report", content="notes", formats=["md", "docx"]))
    finally:
        CURRENT_REQUEST.reset(token)
    assert r.ok and tool.executor.calls == [] and r.evidence["route"] == "dashboard"
    event, data = events.events[0]
    assert event == "document.created" and len(data["files"]) == 2
    md = next(Path(f) for f in r.evidence["files"] if f.endswith(".md"))
    assert "\u2014" not in md.read_text(encoding="utf-8") and "organise" in md.read_text(encoding="utf-8")


async def test_without_a_model_the_content_is_laid_out_as_given(tmp_path):
    tool = MakeDocument(None, Cfg(tmp_path))
    r = await tool.run(MakeDocumentArgs(kind="notes", content="# Notes\n\n- one\n- two", formats=["md"]))
    assert r.ok and "as given" in r.output and Path(r.evidence["path"]).is_file()


def test_make_document_declares_write_risk_and_its_folder(tmp_path):
    tool = MakeDocument(None, Cfg(tmp_path))
    args = MakeDocumentArgs(content="x")
    assert tool.risk == Risk.WRITE and tool.paths(args) == [str(tmp_path)] and args.formats == ["pdf"]


# --- dashboard API ------------------------------------------------------------------------------------


def test_documents_api_needs_the_token_and_serves_only_listed_files(tmp_path):
    from delta.api.server import create_app, load_token
    from delta.bootstrap import build
    from delta.config import Settings, WriterConfig

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "Report-1.pdf").write_bytes(b"%PDF-1.4 test")
    (tmp_path / "secret.txt").write_text("no")
    c = build(Settings(data_dir=tmp_path / "data", writer=WriterConfig(output_dir=str(docs))))
    token = load_token(tmp_path / "data")
    assert "make_document" in c.registry.names() and "write_post" in c.registry.names()
    with TestClient(create_app(c, token)) as tc:
        assert tc.get("/api/documents").status_code == 401
        assert tc.get("/api/documents/Report-1.pdf").status_code == 401
        auth = {"Authorization": f"Bearer {token}"}
        listing = tc.get("/api/documents", headers=auth).json()
        assert [d["name"] for d in listing["documents"]] == ["Report-1.pdf"]
        r = tc.get("/api/documents/Report-1.pdf", headers=auth)
        assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["content-type"] == "application/pdf"
        d = tc.get("/api/documents/Report-1.pdf?download=true", headers=auth)
        assert "attachment" in d.headers["content-disposition"]
        assert tc.get("/api/documents/..%2Fsecret.txt", headers=auth).status_code == 404
        assert tc.get("/api/documents/secret.txt", headers=auth).status_code == 404


def test_writer_can_be_switched_off(tmp_path):
    from delta.bootstrap import build
    from delta.config import Settings, WriterConfig

    c = build(Settings(data_dir=tmp_path, writer=WriterConfig(enabled=False)))
    assert "make_document" not in c.registry.names() and "write_post" not in c.registry.names()
