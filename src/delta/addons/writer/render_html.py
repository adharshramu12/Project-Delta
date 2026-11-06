"""Doc -> a self-contained, print-ready HTML page (no scripts, no external files, no web fonts).

Four templates share one base: report, letter, resume, notes. The layout fields (title page, contents,
fonts, margins, page size) come from document.plan_layout. Everything the user or a web page wrote is
HTML-escaped first; only a tiny Markdown subset (bold, italic, code, http(s) links) is turned into tags.
"""

from __future__ import annotations

import html
import re

from delta.addons.writer.document import MATH, Block, Doc

SERIF = '"Cambria", "Georgia", "Times New Roman", "Noto Serif", serif'
SANS = '"Segoe UI", "Calibri", "Helvetica Neue", "Arial", "Noto Sans", sans-serif'
MONO = '"Cascadia Mono", "Consolas", "Courier New", monospace'
ACCENT = "#1f4e79"

BASE_CSS = """
@page { size: %(size)s; margin: %(margin)smm %(margin)smm %(margin_bottom)smm %(margin)smm; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { font-family: %(font)s; font-size: %(pt)spt; line-height: 1.5; color: #1b1b1b; margin: 0;
       hyphens: auto; orphans: 3; widows: 3; }
h1, h2, h3, h4 { font-family: %(head_font)s; color: %(accent)s; line-height: 1.25; break-after: avoid;
                 page-break-after: avoid; break-inside: avoid; }
h1 { font-size: 1.6em; margin: 1.2em 0 0.4em; }
h2 { font-size: 1.3em; margin: 1.3em 0 0.4em; padding-bottom: 0.15em; border-bottom: 1px solid #d5dde6; }
h3 { font-size: 1.1em; margin: 1.1em 0 0.3em; }
h4 { font-size: 1em; margin: 1em 0 0.2em; color: #333; }
p { margin: 0 0 0.7em; text-align: left; }
ul, ol { margin: 0 0 0.8em 1.3em; padding: 0; }
li { margin: 0.15em 0; break-inside: avoid; }
table { width: 100%%; border-collapse: collapse; margin: 0.6em 0 1em; font-size: 0.92em; break-inside: auto; }
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th { background: #eef3f8; color: #12324f; text-align: left; font-weight: 600; }
th, td { border: 1px solid #c9d3de; padding: 0.35em 0.55em; vertical-align: top; }
tbody tr:nth-child(even) td { background: #f8fafc; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
table.wide { font-size: 0.8em; }
.callout { border-left: 4px solid %(accent)s; background: #f1f6fb; padding: 0.55em 0.8em; margin: 0.8em 0;
           break-inside: avoid; }
.callout b.label { display: block; color: %(accent)s; margin-bottom: 0.15em; }
.callout.warning, .callout.caution { border-color: #b45309; background: #fff7ed; }
.callout.warning b.label, .callout.caution b.label { color: #9a3412; }
blockquote { margin: 0.8em 0; padding: 0.1em 1em; border-left: 3px solid #cbd5e1; color: #374151; font-style: italic; }
pre { font-family: %(mono)s; font-size: 0.85em; background: #f5f5f5; border: 1px solid #e3e3e3; padding: 0.6em;
      white-space: pre-wrap; word-break: break-word; break-inside: avoid; }
code { font-family: %(mono)s; font-size: 0.9em; background: #f3f4f6; padding: 0 0.2em; }
.math { font-family: "Cambria Math", %(mono)s; }
div.math-block { text-align: center; margin: 0.8em 0; break-inside: avoid; }
a { color: %(accent)s; text-decoration: none; }
.pagebreak { break-after: page; page-break-after: always; height: 0; }
.doc-head { margin-bottom: 1.2em; }
.doc-head h1 { margin: 0 0 0.15em; font-size: 1.9em; }
.doc-head .subtitle { font-size: 1.1em; color: #4b5563; margin: 0 0 0.3em; }
.doc-head .byline { color: #6b7280; font-size: 0.92em; }
.title-page { height: 230mm; display: flex; flex-direction: column; justify-content: center; break-after: page;
              page-break-after: always; }
.title-page h1 { font-size: 2.4em; margin: 0 0 0.3em; border: 0; }
.title-page .subtitle { font-size: 1.25em; color: #4b5563; margin-bottom: 2em; }
.title-page .rule { width: 60mm; height: 3px; background: %(accent)s; margin: 0 0 1.2em; }
.title-page .meta { color: #374151; line-height: 1.8; }
.toc { break-after: page; page-break-after: always; }
.toc ol { list-style: none; margin-left: 0; }
.toc li.l2 { margin-left: 1.4em; }
"""

TEMPLATE_CSS = {
    "report": "",
    "letter": """
.letter-from { text-align: right; margin-bottom: 1.2em; white-space: pre-line; }
.letter-date { margin-bottom: 1.2em; }
.letter-to { margin-bottom: 1.2em; white-space: pre-line; }
.letter-subject { font-weight: 700; margin-bottom: 1em; }
.letter-sign { margin-top: 1.8em; white-space: pre-line; }
h2 { border: 0; font-size: 1.05em; }
""",
    "resume": """
body { line-height: 1.38; }
.doc-head { text-align: center; margin-bottom: 0.6em; }
.doc-head h1 { font-size: 2.1em; letter-spacing: 0.02em; color: #111; }
.doc-head .subtitle { font-size: 1.05em; color: %(accent)s; margin: 0.1em 0; }
.doc-head .byline { font-size: 0.9em; color: #374151; }
h2 { font-size: 0.98em; text-transform: uppercase; letter-spacing: 0.08em; margin: 0.9em 0 0.3em;
     border-bottom: 1.5px solid %(accent)s; }
h3 { font-size: 1em; color: #111; margin: 0.6em 0 0.1em; }
p { margin: 0 0 0.35em; }
ul { margin-bottom: 0.4em; }
li { margin: 0.05em 0; }
""",
    "notes": """
.doc-head { border-bottom: 3px solid %(accent)s; padding-bottom: 0.5em; }
h2 { border-bottom: 0; color: %(accent)s; }
""",
}


def inline(text: str) -> str:
    """Escape, then the small Markdown subset. Maths spans are shown as written, in a maths font."""
    parts: list[str] = []
    last = 0
    for m in MATH.finditer(text):
        parts.append(_inline_plain(text[last:m.start()]))
        parts.append(f'<span class="math">{html.escape(m.group(0).strip("$"))}</span>')
        last = m.end()
    parts.append(_inline_plain(text[last:]))
    return "".join(parts)


def _inline_plain(text: str) -> str:
    t = html.escape(text, quote=True)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', t)
    return t


_NUM = re.compile(r"^[\s(]*[-+]?(?:[₹$€£]|rs\.?|inr)?\s*[\d,]+(?:\.\d+)?\s*%?\)?$", re.I)


def _table(b: Block) -> str:
    if not b.rows:
        return ""
    head, body = b.rows[0], b.rows[1:]
    numeric = [bool(body) and all(_NUM.match(r[i]) or not r[i].strip() for r in body) for i in range(len(head))]
    cls = ' class="wide"' if len(head) >= 6 else ""
    out = [f"<table{cls}><thead><tr>"]
    out += [f'<th{" class=num" if numeric[i] else ""}>{inline(c)}</th>' for i, c in enumerate(head)]
    out.append("</tr></thead><tbody>")
    for r in body:
        out.append("<tr>" + "".join(f'<td{" class=num" if numeric[i] else ""}>{inline(c)}</td>'
                                    for i, c in enumerate(r)) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def block_html(b: Block, anchor: str = "") -> str:
    if b.type == "heading":
        level = min(4, max(1, b.level + 1))  # the document title is the only h1
        aid = f' id="{anchor}"' if anchor else ""
        return f"<h{level}{aid}>{inline(b.text)}</h{level}>"
    if b.type == "paragraph":
        return f"<p>{inline(b.text)}</p>"
    if b.type in ("bullets", "numbered"):
        tag = "ol" if b.type == "numbered" else "ul"
        return f"<{tag}>" + "".join(f"<li>{inline(i)}</li>" for i in b.items) + f"</{tag}>"
    if b.type == "table":
        return _table(b)
    if b.type == "callout":
        kind = b.label.lower()
        label = f'<b class="label">{html.escape(b.label)}</b>'
        return f'<div class="callout {html.escape(kind)}">{label}{inline(b.text)}</div>'
    if b.type == "quote":
        return f"<blockquote><p>{inline(b.text)}</p></blockquote>"
    if b.type == "code":
        return f"<pre>{html.escape(b.text)}</pre>"
    if b.type == "math":
        return f'<div class="math-block math">{html.escape(b.text)}</div>'
    if b.type == "pagebreak":
        return '<div class="pagebreak"></div>'
    return ""


def css_for(doc: Doc) -> str:
    lay = doc.layout
    font = SERIF if lay.font == "serif" else SANS
    size = f"{lay.page_size} landscape" if lay.landscape else lay.page_size
    values = {"size": size, "margin": lay.margins_mm,
              "margin_bottom": lay.margins_mm + (6 if lay.page_numbers else 0), "font": font,
              "head_font": SANS if lay.template in ("report", "notes") and lay.font == "serif" else font,
              "pt": lay.font_pt, "accent": ACCENT, "mono": MONO}
    return (BASE_CSS % values) + (TEMPLATE_CSS.get(lay.template, "") % values)


def _byline(doc: Doc) -> str:
    bits = [x for x in (doc.author, doc.date) if x]
    return " · ".join(html.escape(b) for b in bits)


def _head(doc: Doc) -> str:
    lay = doc.layout
    if lay.template == "letter":
        m = doc.meta
        out = []
        if m.get("from"):
            out.append(f'<div class="letter-from">{inline(m["from"].replace(" / ", chr(10)))}</div>')
        if doc.date:
            out.append(f'<div class="letter-date">{html.escape(doc.date)}</div>')
        if m.get("to"):
            out.append(f'<div class="letter-to">{inline(m["to"].replace(" / ", chr(10)))}</div>')
        subject = m.get("subject") or (doc.title if doc.title.lower() != "letter" else "")
        if subject:
            out.append(f'<div class="letter-subject">Subject: {inline(subject)}</div>')
        return "".join(out)
    if lay.title_page:
        meta = []
        if doc.author:
            meta.append(html.escape(doc.author))
        for key in ("course", "institution", "submitted to", "roll number", "organisation", "organization"):
            if doc.meta.get(key):
                meta.append(f"{html.escape(key.title())}: {html.escape(doc.meta[key])}")
        if doc.date:
            meta.append(html.escape(doc.date))
        sub = f'<div class="subtitle">{inline(doc.subtitle)}</div>' if doc.subtitle else ""
        return (f'<section class="title-page"><div class="rule"></div><h1>{inline(doc.title)}</h1>{sub}'
                f'<div class="meta">{"<br>".join(meta)}</div></section>')
    sub = f'<p class="subtitle">{inline(doc.subtitle)}</p>' if doc.subtitle else ""
    by = _byline(doc) if lay.template != "resume" else html.escape(doc.meta.get("contact", "") or _byline(doc))
    by_html = f'<div class="byline">{by}</div>' if by else ""
    return f'<header class="doc-head"><h1>{inline(doc.title)}</h1>{sub}{by_html}</header>'


def _invoice_meta(doc: Doc) -> str:
    keys = [("number", "Invoice no."), ("date", "Date"), ("due", "Due"), ("from", "From"), ("to", "Bill to")]
    rows = [(label, doc.meta.get(k) or (doc.date if k == "date" else "")) for k, label in keys]
    rows = [(label, v) for label, v in rows if v]
    if not rows:
        return ""
    cells = "".join(f"<tr><th>{html.escape(label)}</th><td>{inline(v)}</td></tr>" for label, v in rows)
    return f'<table class="invoice-meta">{cells}</table>'


def render_html(doc: Doc) -> str:
    lay = doc.layout
    body: list[str] = [_head(doc)]
    if doc.kind == "invoice":
        body.append(_invoice_meta(doc))
    anchors: list[tuple[int, str, str]] = []
    rendered: list[str] = []
    for n, b in enumerate(doc.blocks):
        anchor = f"s{n}" if b.type == "heading" and b.level <= 2 else ""
        if anchor:
            anchors.append((b.level, anchor, b.text))
        rendered.append(block_html(b, anchor))
    if lay.toc and anchors:
        items = "".join(f'<li class="l{lvl}">{inline(t)}</li>' for lvl, _a, t in anchors)
        body.append(f'<nav class="toc"><h2>Contents</h2><ol>{items}</ol></nav>')
    body += rendered
    if lay.template == "letter" and doc.meta.get("signature"):
        body.append(f'<div class="letter-sign">{inline(doc.meta["signature"].replace(" / ", chr(10)))}</div>')
    return ("<!doctype html><html lang=\"en-IN\"><head><meta charset=\"utf-8\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; "
            "img-src data:; font-src data:\">"
            f"<title>{html.escape(doc.title)}</title><style>{css_for(doc)}</style></head>"
            f"<body class=\"t-{lay.template} k-{doc.kind}\">{''.join(body)}</body></html>")


def footer_template(doc: Doc) -> str:
    """Chromium print footer: page numbers. Inline styles only (print templates take no page CSS)."""
    if not doc.layout.page_numbers:
        return "<span></span>"
    return ('<div style="width:100%;font-size:8pt;color:#6b7280;text-align:center;font-family:Segoe UI,Arial,'
            'sans-serif;">Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>')


def header_template(doc: Doc) -> str:
    if not doc.layout.header:
        return "<span></span>"
    return ('<div style="width:100%;font-size:8pt;color:#6b7280;padding:0 15mm;text-align:right;'
            f'font-family:Segoe UI,Arial,sans-serif;">{html.escape(doc.layout.header)}</div>')
