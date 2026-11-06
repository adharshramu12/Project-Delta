"""Doc -> .docx with nothing but the standard library (zipfile + WordprocessingML).

python-docx would pull in lxml, a compiled package; writing the few parts a clean document needs is
small and keeps Delta's install unchanged. Output opens in Word, LibreOffice, Google Docs and WPS:
real Title/Heading styles (so Word's navigation pane and contents work), bullet and numbered lists,
tables with a repeating header row that do not split rows across pages, A4/Letter page size and
margins, a running header and "Page X of Y" in the footer.
"""

from __future__ import annotations

import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from delta.addons.writer.document import MATH, Block, Doc

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
ACCENT = "1F4E79"
PAGE = {"A4": (11906, 16838), "Letter": (12240, 15840)}


def _t(text: str) -> str:
    return f'<w:t xml:space="preserve">{escape(text)}</w:t>'


def _run(text: str, bold: bool = False, italic: bool = False, mono: bool = False, math: bool = False,
         color: str = "", size_half_pt: int = 0) -> str:
    props = []
    if mono:
        props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>')
    elif math:
        props.append('<w:rFonts w:ascii="Cambria Math" w:hAnsi="Cambria Math"/>')
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    if size_half_pt:
        props.append(f'<w:sz w:val="{size_half_pt}"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    return f"<w:r>{rpr}{_t(text)}</w:r>"


_INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\(https?://[^)\s]+\)|(?<![\w*])\*(?!\s).+?(?<!\s)\*(?![\w*]))")


def runs(text: str, **base: object) -> str:
    """Inline Markdown subset -> runs: **bold**, *italic*, `code`, [label](url), maths spans."""
    out: list[str] = []
    pieces: list[tuple[str, bool]] = []
    last = 0
    for m in MATH.finditer(text):
        pieces.append((text[last:m.start()], False))
        pieces.append((m.group(0).strip("$"), True))
        last = m.end()
    pieces.append((text[last:], False))
    for chunk, is_math in pieces:
        if not chunk:
            continue
        if is_math:
            out.append(_run(chunk, math=True, **base))  # type: ignore[arg-type]
            continue
        pos = 0
        for m in _INLINE.finditer(chunk):
            if m.start() > pos:
                out.append(_run(chunk[pos:m.start()], **base))  # type: ignore[arg-type]
            tok = m.group(0)
            if tok.startswith("**"):
                out.append(_run(tok[2:-2], **{**base, "bold": True}))  # type: ignore[arg-type]
            elif tok.startswith("`"):
                out.append(_run(tok[1:-1], **{**base, "mono": True}))  # type: ignore[arg-type]
            elif tok.startswith("["):
                label, url = re.match(r"\[([^\]]+)\]\(([^)]+)\)", tok).groups()  # type: ignore[union-attr]
                out.append(_run(f"{label} ({url})", **{**base, "color": ACCENT}))  # type: ignore[arg-type]
            else:
                out.append(_run(tok[1:-1], **{**base, "italic": True}))  # type: ignore[arg-type]
            pos = m.end()
        if pos < len(chunk):
            out.append(_run(chunk[pos:], **base))  # type: ignore[arg-type]
    return "".join(out)


def _p(content: str, style: str = "", align: str = "", keep_next: bool = False, page_break_before: bool = False,
       num: tuple[int, int] | None = None, spacing_after: int | None = None) -> str:
    props = []
    if style:
        props.append(f'<w:pStyle w:val="{style}"/>')
    if keep_next:
        props.append("<w:keepNext/>")
    if page_break_before:
        props.append("<w:pageBreakBefore/>")
    if num:
        props.append(f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>')
    if spacing_after is not None:
        props.append(f'<w:spacing w:after="{spacing_after}"/>')
    if align:
        props.append(f'<w:jc w:val="{align}"/>')
    ppr = f"<w:pPr>{''.join(props)}</w:pPr>" if props else ""
    return f"<w:p>{ppr}{content}</w:p>"


def _page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def _table(b: Block, width_twips: int) -> str:
    if not b.rows:
        return ""
    cols = len(b.rows[0])
    col_w = width_twips // max(cols, 1)
    size = 18 if cols >= 6 else 20  # half-points: wide tables get a smaller font
    border = ('<w:tblBorders>' + "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="C9D3DE"/>'
                                         for s in ("top", "left", "bottom", "right", "insideH", "insideV"))
              + "</w:tblBorders>")
    grid = "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{col_w}"/>' for _ in range(cols)) + "</w:tblGrid>"
    out = [f'<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>{border}'
           '<w:tblCellMar><w:left w:w="90" w:type="dxa"/><w:right w:w="90" w:type="dxa"/></w:tblCellMar>'
           f"</w:tblPr>{grid}"]
    for i, row in enumerate(b.rows):
        trpr = "<w:trPr><w:cantSplit/>" + ("<w:tblHeader/>" if i == 0 else "") + "</w:trPr>"
        cells = []
        for c in row:
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="EEF3F8"/>' if i == 0 else ""
            para = (f'<w:p><w:pPr><w:spacing w:before="20" w:after="20"/></w:pPr>'
                    f'{runs(c, bold=(i == 0), size_half_pt=size)}</w:p>')
            cells.append(f'<w:tc><w:tcPr><w:tcW w:w="{col_w}" w:type="dxa"/>{shade}</w:tcPr>{para}</w:tc>')
        out.append(f"<w:tr>{trpr}{''.join(cells)}</w:tr>")
    out.append("</w:tbl>")
    out.append(_p("", spacing_after=120))
    return "".join(out)


class _Numbering:
    """One w:num per list, so each numbered list starts at 1."""

    def __init__(self) -> None:
        self.nums: list[int] = []  # abstract id per num (0 = bullet, 1 = decimal)

    def new(self, numbered: bool) -> int:
        self.nums.append(1 if numbered else 0)
        return len(self.nums)

    def xml(self) -> str:
        bullet = ('<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="hybridMultilevel"/>'
                  '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="\u2022"/>'
                  '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr>'
                  '<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr></w:lvl>'
                  "</w:abstractNum>")
        bullet = bullet.replace('<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol" w:hint="default"/></w:rPr>', "")
        decimal = ('<w:abstractNum w:abstractNumId="1"><w:multiLevelType w:val="hybridMultilevel"/>'
                   '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>'
                   '<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>'
                   "</w:abstractNum>")
        nums = "".join(f'<w:num w:numId="{i + 1}"><w:abstractNumId w:val="{a}"/>'
                       + ('<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride>' if a == 1 else "")
                       + "</w:num>" for i, a in enumerate(self.nums))
        return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:numbering xmlns:w="{W_NS}">' \
               f"{bullet}{decimal}{nums}</w:numbering>"


def _styles(doc: Doc) -> str:
    lay = doc.layout
    body_font = "Cambria" if lay.font == "serif" else "Calibri"
    head_font = "Calibri" if lay.template in ("report", "notes") else body_font
    size = int(lay.font_pt * 2)

    def heading(n: int, pt: float, before: int, after: int, caps: bool = False) -> str:
        caps_xml = "<w:caps/>" if caps else ""
        border = ('<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="1F4E79"/></w:pBdr>'
                  if (n == 1 and lay.template in ("report", "resume")) else "")
        return (f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/>'
                f'<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:uiPriority w:val="9"/><w:qFormat/>'
                f'<w:pPr><w:keepNext/><w:keepLines/>{border}<w:spacing w:before="{before}" w:after="{after}"/>'
                f'<w:outlineLvl w:val="{n - 1}"/></w:pPr>'
                f'<w:rPr><w:rFonts w:ascii="{head_font}" w:hAnsi="{head_font}"/><w:b/>{caps_xml}'
                f'<w:color w:val="{ACCENT if n < 4 else "333333"}"/><w:sz w:val="{int(pt * 2)}"/></w:rPr></w:style>')

    resume = lay.template == "resume"
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{W_NS}">'
            f'<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="{body_font}" w:hAnsi="{body_font}" '
            f'w:cs="{body_font}" w:eastAsia="{body_font}"/><w:sz w:val="{size}"/><w:szCs w:val="{size}"/>'
            '<w:lang w:val="en-IN"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr>'
            f'<w:spacing w:after="{80 if resume else 140}" w:line="{252 if resume else 276}" w:lineRule="auto"/>'
            "<w:widowControl/></w:pPr></w:pPrDefault></w:docDefaults>"
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/>'
            "</w:style>"
            '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/>'
            '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="80"/>'
            + ('<w:jc w:val="center"/>' if resume else "")
            + f'</w:pPr><w:rPr><w:rFonts w:ascii="{head_font}" w:hAnsi="{head_font}"/><w:b/>'
            f'<w:color w:val="{"111111" if resume else ACCENT}"/><w:sz w:val="{44 if resume else 52}"/></w:rPr>'
            "</w:style>"
            '<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/>'
            '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="200"/>'
            + ('<w:jc w:val="center"/>' if resume else "")
            + '</w:pPr><w:rPr><w:color w:val="4B5563"/><w:sz w:val="26"/></w:rPr></w:style>'
            + heading(1, 12 if resume else 15, 200 if resume else 320, 80, caps=resume)
            + heading(2, 11 if resume else 12.5, 160 if resume else 240, 60)
            + heading(3, 11, 160, 40) + heading(4, 10.5, 120, 40)
            + '<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/><w:basedOn w:val="Normal"/>'
            '<w:pPr><w:ind w:left="567"/></w:pPr><w:rPr><w:i/><w:color w:val="374151"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Callout"><w:name w:val="Callout"/><w:basedOn w:val="Normal"/>'
            '<w:pPr><w:keepLines/><w:pBdr><w:left w:val="single" w:sz="24" w:space="8" w:color="1F4E79"/></w:pBdr>'
            '<w:shd w:val="clear" w:color="auto" w:fill="F1F6FB"/><w:ind w:left="227" w:right="113"/></w:pPr>'
            "</w:style>"
            '<w:style w:type="paragraph" w:styleId="Code"><w:name w:val="Code"/><w:basedOn w:val="Normal"/>'
            '<w:pPr><w:keepLines/><w:shd w:val="clear" w:color="auto" w:fill="F5F5F5"/><w:spacing w:after="0"/></w:pPr>'
            '<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="18"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Header"><w:name w:val="header"/><w:basedOn w:val="Normal"/>'
            '<w:pPr><w:jc w:val="right"/></w:pPr><w:rPr><w:color w:val="6B7280"/><w:sz w:val="16"/></w:rPr></w:style>'
            '<w:style w:type="paragraph" w:styleId="Footer"><w:name w:val="footer"/><w:basedOn w:val="Normal"/>'
            '<w:pPr><w:jc w:val="center"/></w:pPr><w:rPr><w:color w:val="6B7280"/><w:sz w:val="16"/></w:rPr></w:style>'
            '<w:style w:type="table" w:default="1" w:styleId="TableNormal"><w:name w:val="Normal Table"/>'
            '<w:tblPr><w:tblInd w:w="0" w:type="dxa"/><w:tblCellMar><w:top w:w="0" w:type="dxa"/>'
            '<w:left w:w="108" w:type="dxa"/><w:bottom w:w="0" w:type="dxa"/><w:right w:w="108" w:type="dxa"/>'
            "</w:tblCellMar></w:tblPr></w:style></w:styles>")


def _field(instr: str) -> str:
    return (f'<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> {instr} '
            '</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>')


def _footer() -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:ftr xmlns:w="{W_NS}" xmlns:r="{R_NS}">'
            f'<w:p><w:pPr><w:pStyle w:val="Footer"/></w:pPr>{_run("Page ")}{_field("PAGE")}{_run(" of ")}'
            f'{_field("NUMPAGES")}</w:p></w:ftr>')


def _header(text: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:hdr xmlns:w="{W_NS}" xmlns:r="{R_NS}">'
            f'<w:p><w:pPr><w:pStyle w:val="Header"/></w:pPr>{_run(text)}</w:p></w:hdr>')


def body_xml(doc: Doc, numbering: _Numbering, width_twips: int) -> str:
    lay = doc.layout
    out: list[str] = []
    m = doc.meta
    if lay.template == "letter":
        if m.get("from"):
            for line in m["from"].split(" / "):
                out.append(_p(runs(line), align="right", spacing_after=0))
            out.append(_p(""))
        if doc.date:
            out.append(_p(runs(doc.date)))
        if m.get("to"):
            for line in m["to"].split(" / "):
                out.append(_p(runs(line), spacing_after=0))
            out.append(_p(""))
        subject = m.get("subject") or (doc.title if doc.title.lower() != "letter" else "")
        if subject:
            out.append(_p(runs("Subject: " + subject, bold=True)))
    else:
        out.append(_p(runs(doc.title), "Title"))
        if doc.subtitle:
            out.append(_p(runs(doc.subtitle), "Subtitle"))
        byline = m.get("contact", "") if lay.template == "resume" else ""
        byline = byline or " · ".join(x for x in (doc.author, doc.date) if x)
        if byline:
            out.append(_p(runs(byline, color="4B5563"), align="center" if lay.template == "resume" else ""))
        if doc.kind == "invoice":
            for key, label in (("number", "Invoice no."), ("due", "Due"), ("from", "From"), ("to", "Bill to")):
                if m.get(key):
                    out.append(_p(runs(f"**{label}:** {m[key]}"), spacing_after=40))
        if lay.title_page:
            for key in ("course", "institution", "submitted to", "roll number", "organisation", "organization"):
                if m.get(key):
                    out.append(_p(runs(f"{key.title()}: {m[key]}")))
            out.append(_page_break())
            if lay.toc:
                out.append(_p(runs("Contents"), "Heading1"))
                for h in doc.headings(2):
                    indent = "    " if h.level == 2 else ""
                    out.append(_p(runs(indent + h.text), spacing_after=40))
                out.append(_page_break())
    for b in doc.blocks:
        if b.type == "heading":
            out.append(_p(runs(b.text), f"Heading{min(4, b.level)}"))
        elif b.type == "paragraph":
            out.append(_p(runs(b.text)))
        elif b.type in ("bullets", "numbered"):
            num = numbering.new(b.type == "numbered")
            for item in b.items:
                out.append(_p(runs(item), num=(num, 0), spacing_after=40))
            out.append(_p("", spacing_after=40))
        elif b.type == "table":
            out.append(_table(b, width_twips))
        elif b.type == "callout":
            out.append(_p(runs(b.label + ": ", bold=True, color=ACCENT) + runs(b.text), "Callout"))
        elif b.type == "quote":
            out.append(_p(runs(b.text), "Quote"))
        elif b.type == "code":
            for line in b.text.split("\n") or [""]:
                out.append(_p(_run(line, mono=True), "Code"))
            out.append(_p(""))
        elif b.type == "math":
            out.append(_p(_run(b.text, math=True), align="center"))
        elif b.type == "pagebreak":
            out.append(_page_break())
    if lay.template == "letter" and m.get("signature"):
        out.append(_p(""))
        for line in m["signature"].split(" / "):
            out.append(_p(runs(line), spacing_after=0))
    return "".join(out)


def render_docx(doc: Doc, out: Path) -> Path:
    lay = doc.layout
    w, h = PAGE.get(lay.page_size, PAGE["A4"])
    if lay.landscape:
        w, h = h, w
    margin = int(lay.margins_mm * 56.7)
    numbering = _Numbering()
    body = body_xml(doc, numbering, w - 2 * margin)
    refs = []
    rels = [('rId1', "styles", "styles.xml"), ('rId2', "numbering", "numbering.xml"),
            ('rId3', "settings", "settings.xml")]
    if lay.page_numbers:
        rels.append(("rId4", "footer", "footer1.xml"))
        refs.append('<w:footerReference w:type="default" r:id="rId4"/>')
    if lay.header:
        rels.append(("rId5", "header", "header1.xml"))
        refs.insert(0, '<w:headerReference w:type="default" r:id="rId5"/>')
    orient = ' w:orient="landscape"' if lay.landscape else ""
    sect = (f"<w:sectPr>{''.join(refs)}<w:pgSz w:w=\"{w}\" w:h=\"{h}\"{orient}/>"
            f'<w:pgMar w:top="{margin}" w:right="{margin}" w:bottom="{margin}" w:left="{margin}" '
            f'w:header="567" w:footer="567" w:gutter="0"/></w:sectPr>')
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W_NS}" '
                f'xmlns:r="{R_NS}"><w:body>{body}{sect}</w:body></w:document>')
    rel_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join(f'<Relationship Id="{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                         f'relationships/{t}" Target="{target}"/>' for i, t, target in rels)
               + "</Relationships>")
    overrides = ['<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 'wordprocessingml.document.main+xml"/>',
                 '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 'wordprocessingml.styles+xml"/>',
                 '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-'
                 'officedocument.wordprocessingml.numbering+xml"/>',
                 '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-'
                 'officedocument.wordprocessingml.settings+xml"/>',
                 '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.'
                 'core-properties+xml"/>',
                 '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-'
                 'officedocument.extended-properties+xml"/>']
    if lay.page_numbers:
        overrides.append('<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-'
                         'officedocument.wordprocessingml.footer+xml"/>')
    if lay.header:
        overrides.append('<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-'
                         'officedocument.wordprocessingml.header+xml"/>')
    content_types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                     '<Default Extension="rels" '
                     'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                     '<Default Extension="xml" ContentType="application/xml"/>' + "".join(overrides) + "</Types>")
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                 'officeDocument" Target="word/document.xml"/>'
                 '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/'
                 'core-properties" Target="docProps/core.xml"/>'
                 '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
                 'extended-properties" Target="docProps/app.xml"/></Relationships>')
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape(doc.title)}</dc:title><dc:creator>{escape(doc.author or 'Delta')}</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>')
    app = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.'
           'org/officeDocument/2006/extended-properties"><Application>Delta</Application></Properties>')
    settings = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings xmlns:w="{W_NS}">'
                '<w:defaultTabStop w:val="720"/><w:compat><w:compatSetting w:name="compatibilityMode" '
                'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>')
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".docx.part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", _styles(doc))
        z.writestr("word/numbering.xml", numbering.xml())
        z.writestr("word/settings.xml", settings)
        z.writestr("word/_rels/document.xml.rels", rel_xml)
        if lay.page_numbers:
            z.writestr("word/footer1.xml", _footer())
        if lay.header:
            z.writestr("word/header1.xml", _header(lay.header))
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
    tmp.replace(out)
    return out
