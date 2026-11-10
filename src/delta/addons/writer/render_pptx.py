"""Deck -> .pptx with nothing but the standard library (zipfile + PresentationML).

python-pptx would pull in lxml, a compiled package; the parts a clean deck needs are few. Output opens in
PowerPoint, Google Slides, LibreOffice Impress and Keynote: 16:9, a real slide master with a title layout and a
content layout (so "Design" and "Reset" in PowerPoint work), real title and body placeholders (so Outline view and
screen readers see the text), tables as real tables, speaker notes, and slide numbers.

All text is escaped; nothing in a deck is ever a macro, a link target or an external reference.
"""

from __future__ import annotations

import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from delta.addons.writer.slides import Deck, Slide

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
NS = f'xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}"'
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "application/vnd.openxmlformats-officedocument.presentationml"
XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

W, H = 12192000, 6858000  # 16:9 in EMU
MARGIN = 685800
INK, MUTED, DARK, ACCENT, PALE = "1F2937", "6B7280", "1F2A44", "E07A47", "F3F4F6"
FONT = "Segoe UI"
_BOLD = re.compile(r"\*\*(.+?)\*\*")
# XML 1.0 forbids these control characters; one in a title would make the whole file unreadable.
_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _esc(text: str) -> str:
    return escape(_BAD.sub("", text))


def _runs(text: str, size: int = 0, color: str = "", bold: bool = False) -> str:
    """Text -> runs; **bold** spans are the only inline mark (everything else is shown as written)."""
    out: list[str] = []
    pos = 0

    def run(t: str, b: bool) -> str:
        attrs = ' lang="en-IN"' + (f' sz="{size}"' if size else "") + (' b="1"' if b else "") + ' dirty="0"'
        fill = f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>' if color else ""
        return f"<a:r><a:rPr{attrs}>{fill}</a:rPr><a:t>{_esc(t)}</a:t></a:r>"

    for m in _BOLD.finditer(text):
        if m.start() > pos:
            out.append(run(text[pos:m.start()], bold))
        out.append(run(m.group(1), True))
        pos = m.end()
    if pos < len(text):
        out.append(run(text[pos:], bold))
    return "".join(out)


def _xfrm(x: int, y: int, cx: int, cy: int) -> str:
    return f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'


def _placeholder(sid: int, name: str, ph: str, paragraphs: str, xfrm: str = "") -> str:
    sp_pr = f"<p:spPr>{xfrm}</p:spPr>" if xfrm else "<p:spPr/>"
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{name}"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
            f"<p:nvPr><p:ph {ph}/></p:nvPr></p:nvSpPr>{sp_pr}"
            f"<p:txBody><a:bodyPr/><a:lstStyle/>{paragraphs}</p:txBody></p:sp>")


def _rect(sid: int, name: str, x: int, y: int, cx: int, cy: int, color: str) -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="{name}"/><p:cNvSpPr/><p:nvPr userDrawn="1"/></p:nvSpPr>'
            f'<p:spPr>{_xfrm(x, y, cx, cy)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr>'
            '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-IN"/></a:p></p:txBody></p:sp>')


TREE_HEAD = ('<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr>'
             '<a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/>'
             "</a:xfrm></p:grpSpPr>")
CLR_MAP = ('<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" '
           'accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>')
OVR = "<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>"
EMPTY_P = '<a:p><a:endParaRPr lang="en-IN"/></a:p>'

TITLE_BOX = _xfrm(MARGIN, 420000, W - 2 * MARGIN, 1000000)
BODY_BOX = _xfrm(MARGIN, 1600000, W - 2 * MARGIN, 4500000)
NUM_BOX = _xfrm(W - MARGIN - 900000, H - 480000, 900000, 300000)
FIELD_ID = "{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}"


def _number(sid: int, xfrm: str = "") -> str:
    """The slide-number placeholder. A placeholder on the master is only a position: a slide shows its number
    when the slide (and its layout) carry the placeholder too."""
    sp_pr = f"<p:spPr>{xfrm}</p:spPr>" if xfrm else "<p:spPr/>"
    style = (f'<a:lstStyle><a:lvl1pPr algn="r"><a:defRPr sz="1200"><a:solidFill><a:srgbClr val="{MUTED}"/>'
             "</a:solidFill></a:defRPr></a:lvl1pPr></a:lstStyle>") if xfrm else "<a:lstStyle/>"
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="Slide Number"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
            f'<p:nvPr><p:ph type="sldNum" sz="quarter" idx="12"/></p:nvPr></p:nvSpPr>{sp_pr}'
            f'<p:txBody><a:bodyPr anchor="ctr"/>{style}<a:p><a:fld id="{FIELD_ID}" type="slidenum">'
            '<a:rPr lang="en-IN"/><a:t>‹#›</a:t></a:fld><a:endParaRPr lang="en-IN"/></a:p></p:txBody></p:sp>')


def _level(n: int, size: int, indent: int, color: str = INK, bullet: str = "•") -> str:
    left = 342900 + indent * 400000
    return (f'<a:lvl{n}pPr marL="{left}" indent="-342900" algn="l"><a:spcBef><a:spcPts val="900"/></a:spcBef>'
            f'<a:buClr><a:srgbClr val="{ACCENT}"/></a:buClr><a:buFont typeface="Arial"/><a:buChar char="{bullet}"/>'
            f'<a:defRPr sz="{size}"><a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            f'<a:latin typeface="{FONT}"/></a:defRPr></a:lvl{n}pPr>')


def _master() -> str:
    title_style = (f'<a:lvl1pPr algn="l"><a:lnSpc><a:spcPct val="95000"/></a:lnSpc><a:buNone/>'
                   f'<a:defRPr sz="3200" b="1"><a:solidFill><a:srgbClr val="{DARK}"/></a:solidFill>'
                   f'<a:latin typeface="{FONT}"/></a:defRPr></a:lvl1pPr>')
    body_style = _level(1, 2200, 0) + _level(2, 1800, 1, MUTED, "–") + _level(3, 1600, 2, MUTED, "–")
    other = (f'<a:lvl1pPr><a:defRPr sz="1200"><a:solidFill><a:srgbClr val="{MUTED}"/></a:solidFill>'
             f'<a:latin typeface="{FONT}"/></a:defRPr></a:lvl1pPr>')
    num = _number(5, NUM_BOX)
    title = ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
             f'<p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr><p:spPr>{TITLE_BOX}</p:spPr><p:txBody>'
             '<a:bodyPr anchor="b"><a:normAutofit/></a:bodyPr><a:lstStyle/><a:p><a:r><a:rPr lang="en-IN"/>'
             "<a:t>Title</a:t></a:r></a:p></p:txBody></p:sp>")
    body = ('<p:sp><p:nvSpPr><p:cNvPr id="3" name="Content"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
            f'<p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr>{BODY_BOX}</p:spPr><p:txBody>'
            '<a:bodyPr><a:normAutofit/></a:bodyPr><a:lstStyle/><a:p><a:pPr lvl="0"/><a:r><a:rPr lang="en-IN"/>'
            "<a:t>Text</a:t></a:r></a:p></p:txBody></p:sp>")
    bar = _rect(4, "Accent", MARGIN, 1440000, 900000, 45720, ACCENT)
    layouts = "".join(f'<p:sldLayoutId id="{2147483649 + i}" r:id="rId{i + 1}"/>' for i in range(3))
    return (f'{XML}<p:sldMaster {NS}><p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
            f"<a:effectLst/></p:bgPr></p:bg><p:spTree>{TREE_HEAD}{title}{body}{bar}{num}</p:spTree></p:cSld>{CLR_MAP}"
            f"<p:sldLayoutIdLst>{layouts}</p:sldLayoutIdLst><p:txStyles><p:titleStyle>{title_style}</p:titleStyle>"
            f"<p:bodyStyle>{body_style}</p:bodyStyle><p:otherStyle>{other}</p:otherStyle></p:txStyles></p:sldMaster>")


def _layout(kind: str) -> str:
    """title: the opening slide (dark, large). content: title + points. table: title only (the table is drawn)."""
    if kind == "title":
        shapes = (_rect(4, "Accent", MARGIN, 3560000, 1200000, 60960, ACCENT)
                  + _placeholder(2, "Title", 'type="ctrTitle"',
                                 '<a:p><a:r><a:rPr lang="en-IN"/><a:t>Title</a:t></a:r></a:p>',
                                 _xfrm(MARGIN, 1500000, W - 2 * MARGIN, 1900000)).replace(
                      "<a:bodyPr/><a:lstStyle/>",
                      '<a:bodyPr anchor="b"><a:normAutofit/></a:bodyPr><a:lstStyle><a:lvl1pPr algn="l">'
                      '<a:defRPr sz="4400" b="1"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:defRPr>'
                      "</a:lvl1pPr></a:lstStyle>")
                  + _placeholder(3, "Subtitle", 'type="subTitle" idx="1"',
                                 '<a:p><a:r><a:rPr lang="en-IN"/><a:t>Subtitle</a:t></a:r></a:p>',
                                 _xfrm(MARGIN, 3800000, W - 2 * MARGIN, 1200000)).replace(
                      "<a:bodyPr/><a:lstStyle/>",
                      '<a:bodyPr><a:normAutofit/></a:bodyPr><a:lstStyle><a:lvl1pPr marL="0" indent="0" algn="l">'
                      '<a:buNone/><a:defRPr sz="2000"><a:solidFill><a:srgbClr val="D1D5DB"/></a:solidFill></a:defRPr>'
                      "</a:lvl1pPr></a:lstStyle>"))
        bg = (f'<p:bg><p:bgPr><a:solidFill><a:srgbClr val="{DARK}"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>')
        return (f'{XML}<p:sldLayout {NS} type="title" preserve="1" showMasterSp="0"><p:cSld name="Title Slide">{bg}'
                f"<p:spTree>{TREE_HEAD}{shapes}</p:spTree></p:cSld>{OVR}</p:sldLayout>")
    shapes = _placeholder(2, "Title", 'type="title"', '<a:p><a:r><a:rPr lang="en-IN"/><a:t>Title</a:t></a:r></a:p>')
    if kind == "content":
        shapes += _placeholder(3, "Content", 'idx="1"',
                               '<a:p><a:pPr lvl="0"/><a:r><a:rPr lang="en-IN"/><a:t>Text</a:t></a:r></a:p>')
    shapes += _number(9)
    name, typ = ("Title and Content", "obj") if kind == "content" else ("Title Only", "titleOnly")
    return (f'{XML}<p:sldLayout {NS} type="{typ}" preserve="1"><p:cSld name="{name}"><p:spTree>{TREE_HEAD}{shapes}'
            f"</p:spTree></p:cSld>{OVR}</p:sldLayout>")


def _table(rows: list[list[str]], sid: int) -> str:
    cols = len(rows[0])
    width = W - 2 * MARGIN
    col_w = width // cols
    row_h = 420000
    size = 1600 if cols <= 4 else 1400
    out = [f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{sid}" name="Table"/><p:cNvGraphicFramePr>'
           f'<a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr>'
           f'<p:xfrm><a:off x="{MARGIN}" y="1700000"/><a:ext cx="{col_w * cols}" cy="{row_h * len(rows)}"/></p:xfrm>'
           f'<a:graphic><a:graphicData uri="{A.rsplit("/", 1)[0]}/table"><a:tbl>'
           '<a:tblPr firstRow="1" bandRow="1"/><a:tblGrid>'
           + "".join(f'<a:gridCol w="{col_w}"/>' for _ in range(cols)) + "</a:tblGrid>"]
    line = '<a:solidFill><a:srgbClr val="D1D5DB"/></a:solidFill>'
    for i, row in enumerate(rows):
        cells = []
        for cell in row:
            head = i == 0
            fill = DARK if head else (PALE if i % 2 == 0 else "FFFFFF")
            text = (_runs(cell, size, "FFFFFF" if head else INK, bold=head)
                    or f'<a:endParaRPr lang="en-IN" sz="{size}"/>')
            cells.append(f"<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p>{text}</a:p></a:txBody>"
                         f'<a:tcPr marL="110000" marR="110000" marT="70000" marB="70000" anchor="ctr">'
                         f'<a:lnL w="6350">{line}</a:lnL><a:lnR w="6350">{line}</a:lnR>'
                         f'<a:lnT w="6350">{line}</a:lnT><a:lnB w="6350">{line}</a:lnB>'
                         f'<a:solidFill><a:srgbClr val="{fill}"/></a:solidFill></a:tcPr></a:tc>')
        out.append(f'<a:tr h="{row_h}">{"".join(cells)}</a:tr>')
    out.append("</a:tbl></a:graphicData></a:graphic></p:graphicFrame>")
    return "".join(out)


def _slide(s: Slide) -> tuple[str, str]:
    """(slide xml, layout kind)."""
    title = _placeholder(2, "Title", 'type="title"', f"<a:p>{_runs(s.title)}</a:p>")
    if s.table:
        shapes = title + _table(s.table, 4) + _number(9)
        return f"{XML}<p:sld {NS}><p:cSld><p:spTree>{TREE_HEAD}{shapes}</p:spTree></p:cSld>{OVR}</p:sld>", "table"
    paras = "".join(f'<a:p><a:pPr lvl="{lvl}"/>{_runs(text)}</a:p>' for lvl, text in s.points) or EMPTY_P
    body = _placeholder(3, "Content", 'idx="1"', paras)
    shapes = title + body + _number(9)
    return f"{XML}<p:sld {NS}><p:cSld><p:spTree>{TREE_HEAD}{shapes}</p:spTree></p:cSld>{OVR}</p:sld>", "content"


def _title_slide(deck: Deck) -> str:
    sub = (_placeholder(3, "Subtitle", 'type="subTitle" idx="1"', f"<a:p>{_runs(deck.subtitle)}</a:p>")
           if deck.subtitle else "")
    title = _placeholder(2, "Title", 'type="ctrTitle"', f"<a:p>{_runs(deck.title)}</a:p>")
    return f"{XML}<p:sld {NS}><p:cSld><p:spTree>{TREE_HEAD}{title}{sub}</p:spTree></p:cSld>{OVR}</p:sld>"


def _notes_master() -> str:
    img = ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image"/><p:cNvSpPr><a:spLocks noGrp="1" noRot="1" '
           'noChangeAspect="1"/></p:cNvSpPr><p:nvPr><p:ph type="sldImg" idx="2"/></p:nvPr></p:nvSpPr><p:spPr>'
           f'{_xfrm(685800, 1143000, 5486400, 3086100)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
           '<a:noFill/><a:ln w="12700"><a:solidFill><a:prstClr val="black"/></a:solidFill></a:ln></p:spPr></p:sp>')
    body = ('<p:sp><p:nvSpPr><p:cNvPr id="3" name="Notes"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr>'
            f'<p:nvPr><p:ph type="body" sz="quarter" idx="3"/></p:nvPr></p:nvSpPr><p:spPr>'
            f'{_xfrm(685800, 4400550, 5486400, 3600450)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
            '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-IN"/><a:t>Notes</a:t></a:r></a:p>'
            "</p:txBody></p:sp>")
    style = (f'<a:lvl1pPr marL="0" algn="l"><a:defRPr sz="1200"><a:solidFill><a:srgbClr val="{INK}"/></a:solidFill>'
             f'<a:latin typeface="{FONT}"/></a:defRPr></a:lvl1pPr>')
    return (f'{XML}<p:notesMaster {NS}><p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>'
            f"<a:effectLst/></p:bgPr></p:bg><p:spTree>{TREE_HEAD}{img}{body}</p:spTree></p:cSld>{CLR_MAP}"
            f"<p:notesStyle>{style}</p:notesStyle></p:notesMaster>")


def _notes_slide(text: str) -> str:
    img = ('<p:sp><p:nvSpPr><p:cNvPr id="2" name="Slide Image"/><p:cNvSpPr><a:spLocks noGrp="1" noRot="1" '
           'noChangeAspect="1"/></p:cNvSpPr><p:nvPr><p:ph type="sldImg"/></p:nvPr></p:nvSpPr><p:spPr/></p:sp>')
    body = _placeholder(3, "Notes", 'type="body" idx="1"', f"<a:p>{_runs(text)}</a:p>")
    return f"{XML}<p:notes {NS}><p:cSld><p:spTree>{TREE_HEAD}{img}{body}</p:spTree></p:cSld>{OVR}</p:notes>"


def _theme(name: str) -> str:
    fill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    ln = ('<a:ln w="{w}" cap="flat" cmpd="sng" algn="ctr"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
          '<a:prstDash val="solid"/></a:ln>')
    colors = [("dk1", "000000"), ("lt1", "FFFFFF"), ("dk2", DARK), ("lt2", PALE), ("accent1", ACCENT),
              ("accent2", "2563EB"), ("accent3", "059669"), ("accent4", "D97706"), ("accent5", "7C3AED"),
              ("accent6", "DB2777"), ("hlink", "2563EB"), ("folHlink", "7C3AED")]
    scheme = "".join(f'<a:{n}><a:srgbClr val="{v}"/></a:{n}>' for n, v in colors)
    font = f'<a:latin typeface="{FONT}"/><a:ea typeface=""/><a:cs typeface=""/>'
    return (f'{XML}<a:theme xmlns:a="{A}" name="{name}"><a:themeElements><a:clrScheme name="Delta">{scheme}'
            f'</a:clrScheme><a:fontScheme name="Delta"><a:majorFont>{font}</a:majorFont><a:minorFont>{font}'
            f'</a:minorFont></a:fontScheme><a:fmtScheme name="Delta"><a:fillStyleLst>{fill * 3}</a:fillStyleLst>'
            f'<a:lnStyleLst>{ln.format(w=6350)}{ln.format(w=12700)}{ln.format(w=19050)}</a:lnStyleLst>'
            "<a:effectStyleLst>" + "<a:effectStyle><a:effectLst/></a:effectStyle>" * 3 + "</a:effectStyleLst>"
            f"<a:bgFillStyleLst>{fill * 3}</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>")


def _rels(items: list[tuple[str, str]]) -> str:
    """[(type suffix, target)] -> a relationships part, ids rId1.. in order."""
    body = "".join(f'<Relationship Id="rId{i}" Type="{REL}/{t}" Target="{target}"/>'
                   for i, (t, target) in enumerate(items, 1))
    return f'{XML}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{body}</Relationships>'


def render_pptx(deck: Deck, out: Path) -> Path:
    """Write the deck to `out`. The opening slide is made from the deck's title; then one slide per Slide."""
    layouts = ["title", "content", "table"]
    slides: list[tuple[str, str, str]] = [(_title_slide(deck), "title", "")]
    for s in deck.slides:
        xml, kind = _slide(s)
        slides.append((xml, kind, s.notes))
    with_notes = [i for i, (_, _, notes) in enumerate(slides, 1) if notes]

    types = [f'<Override PartName="/ppt/presentation.xml" ContentType="{CT}.presentation.main+xml"/>',
             f'<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="{CT}.slideMaster+xml"/>',
             '<Override PartName="/ppt/theme/theme1.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>',
             '<Override PartName="/docProps/core.xml" '
             'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
             '<Override PartName="/docProps/app.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>']
    types += [f'<Override PartName="/ppt/slideLayouts/slideLayout{i}.xml" ContentType="{CT}.slideLayout+xml"/>'
              for i in range(1, len(layouts) + 1)]
    types += [f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="{CT}.slide+xml"/>'
              for i in range(1, len(slides) + 1)]
    if with_notes:
        types += [f'<Override PartName="/ppt/notesMasters/notesMaster1.xml" ContentType="{CT}.notesMaster+xml"/>',
                  '<Override PartName="/ppt/theme/theme2.xml" '
                  'ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>']
        types += [f'<Override PartName="/ppt/notesSlides/notesSlide{i}.xml" ContentType="{CT}.notesSlide+xml"/>'
                  for i in with_notes]

    # presentation.xml: rId1 = master, rId2.. = slides, then the notes master and the theme
    pres_rels = [("slideMaster", "slideMasters/slideMaster1.xml")]
    pres_rels += [("slide", f"slides/slide{i}.xml") for i in range(1, len(slides) + 1)]
    notes_master_id = ""
    if with_notes:
        pres_rels.append(("notesMaster", "notesMasters/notesMaster1.xml"))
        notes_master_id = f'<p:notesMasterIdLst><p:notesMasterId r:id="rId{len(pres_rels)}"/></p:notesMasterIdLst>'
    pres_rels.append(("theme", "theme/theme1.xml"))
    slide_ids = "".join(f'<p:sldId id="{255 + i}" r:id="rId{i + 1}"/>' for i in range(1, len(slides) + 1))
    presentation = (f'{XML}<p:presentation {NS} saveSubsetFonts="1"><p:sldMasterIdLst><p:sldMasterId id="2147483648" '
                    f'r:id="rId1"/></p:sldMasterIdLst>{notes_master_id}<p:sldIdLst>{slide_ids}</p:sldIdLst>'
                    f'<p:sldSz cx="{W}" cy="{H}"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>')

    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    core = (f'{XML}<cp:coreProperties '
            'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{_esc(deck.title)}</dc:title><dc:creator>Delta</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified></cp:coreProperties>')
    app = (f'{XML}<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
           f"<Application>Delta</Application><Slides>{len(slides)}</Slides></Properties>")

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   f'{XML}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   f'<Default Extension="xml" ContentType="application/xml"/>{"".join(types)}</Types>')
        z.writestr("_rels/.rels", _rels([("officeDocument", "ppt/presentation.xml")]).replace(
            f'Type="{REL}/officeDocument" Target="ppt/presentation.xml"/>',
            f'Type="{REL}/officeDocument" Target="ppt/presentation.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/'
            'core-properties" Target="docProps/core.xml"/>'
            f'<Relationship Id="rId3" Type="{REL}/extended-properties" Target="docProps/app.xml"/>'))
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        z.writestr("ppt/presentation.xml", presentation)
        z.writestr("ppt/_rels/presentation.xml.rels", _rels(pres_rels))
        z.writestr("ppt/theme/theme1.xml", _theme("Delta"))
        z.writestr("ppt/slideMasters/slideMaster1.xml", _master())
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels",
                   _rels([("slideLayout", f"../slideLayouts/slideLayout{i}.xml") for i in range(1, 4)]
                         + [("theme", "../theme/theme1.xml")]))
        for i, kind in enumerate(layouts, 1):
            z.writestr(f"ppt/slideLayouts/slideLayout{i}.xml", _layout(kind))
            z.writestr(f"ppt/slideLayouts/_rels/slideLayout{i}.xml.rels",
                       _rels([("slideMaster", "../slideMasters/slideMaster1.xml")]))
        for i, (xml, kind, notes) in enumerate(slides, 1):
            z.writestr(f"ppt/slides/slide{i}.xml", xml)
            rels = [("slideLayout", f"../slideLayouts/slideLayout{layouts.index(kind) + 1}.xml")]
            if notes:
                rels.append(("notesSlide", f"../notesSlides/notesSlide{i}.xml"))
                z.writestr(f"ppt/notesSlides/notesSlide{i}.xml", _notes_slide(notes))
                z.writestr(f"ppt/notesSlides/_rels/notesSlide{i}.xml.rels",
                           _rels([("notesMaster", "../notesMasters/notesMaster1.xml"),
                                  ("slide", f"../slides/slide{i}.xml")]))
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", _rels(rels))
        if with_notes:
            z.writestr("ppt/notesMasters/notesMaster1.xml", _notes_master())
            z.writestr("ppt/notesMasters/_rels/notesMaster1.xml.rels", _rels([("theme", "../theme/theme2.xml")]))
            z.writestr("ppt/theme/theme2.xml", _theme("Delta notes"))
    return out
