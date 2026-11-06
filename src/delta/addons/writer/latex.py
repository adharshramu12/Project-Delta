"""Optional LaTeX engine for maths-heavy or academic documents (resume, report, paper, assignment).

Never required. Used only when tectonic, xelatex or pdflatex (TeX Live or MiKTeX) is already on PATH.
Compiling is locked down, because document text can come from web pages:
  - shell escape off (-no-shell-escape; tectonic --untrusted), so \\write18 cannot run programs,
  - openin_any/openout_any = p (paranoid): TeX may not read or write files outside its temp folder,
  - all text is escaped; maths is passed through only when it holds no file, shell or macro commands,
  - a throwaway temp folder, a hard timeout, and the process is killed if it overruns.
If anything fails the caller falls back to the browser PDF and says so.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from delta.addons.writer.document import MATH, Block, Doc

ENGINES = ("tectonic", "xelatex", "pdflatex")
COMPILE_TIMEOUT_S = 120
LATEX_KINDS = {"resume", "report", "paper", "assignment"}

# Anything that reads/writes files, runs code, or redefines TeX is refused inside maths.
UNSAFE = re.compile(
    r"\\(?:input|include|openin|openout|write|read|immediate|catcode|def|edef|gdef|xdef|let|csname|"
    r"usepackage|RequirePackage|special|directlua|latelua|lua\w*|newread|newwrite|ShellEscape|includegraphics|"
    r"verbatiminput|lstinputlisting|loop|expandafter|uppercase|lowercase|endinput|documentclass|makeatletter|"
    r"pdf\w+|XeTeX\w+|newcommand|renewcommand|providecommand|DeclareRobustCommand|scantokens|everyeof|"
    r"begin\s*\{\s*document|end\s*\{\s*document|url|href|hyperlink|jobname|outer|afterassignment|output)\b",
    re.I)


def find_engine() -> tuple[str, str] | None:
    """(name, exe) of the first LaTeX engine on PATH, or None."""
    for name in ENGINES:
        if exe := shutil.which(name):
            return name, exe
    return None


_SPECIAL = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{",
            "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "<": r"\textless{}",
            ">": r"\textgreater{}", "|": r"\textbar{}"}
_PDFTEX_SAFE = {"₹": "Rs.", "\u2018": "`", "\u2019": "'", "\u201c": "``", "\u201d": "''", "\u2026": r"\ldots{}",
                "\u00a0": "~", "\u2022": r"\textbullet{}"}


def escape_tex(text: str, pdftex: bool = False) -> str:
    out = "".join(_SPECIAL.get(ch, ch) for ch in text)
    if pdftex:
        out = "".join(_PDFTEX_SAFE.get(ch, ch) for ch in out)
        out = "".join(ch if ord(ch) < 0x250 or ch in "\u2013\u2014" else "?" for ch in out)  # pdflatex lacks glyphs
    return out


def safe_math(src: str) -> bool:
    return not UNSAFE.search(src) and src.count("{") == src.count("}") and len(src) < 2000


def inline_tex(text: str, pdftex: bool = False) -> str:
    """Escape prose; keep safe $maths$; turn **bold**, *italic* and `code` into commands."""
    parts: list[str] = []
    last = 0
    for m in MATH.finditer(text):
        parts.append(_markup(text[last:m.start()], pdftex))
        raw = m.group(0)
        inner = raw.strip("$")
        if raw.startswith(("\\(", "\\[")):
            inner = raw[2:-2]
        parts.append(f"${inner}$" if safe_math(inner) else _markup(raw, pdftex))
        last = m.end()
    parts.append(_markup(text[last:], pdftex))
    return "".join(parts)


def _markup(text: str, pdftex: bool) -> str:
    t = escape_tex(text, pdftex)
    t = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\\emph{\1}", t)
    t = re.sub(r"`([^`]+)`", r"\\texttt{\1}", t)
    return t


def _block(b: Block, pdftex: bool, resume: bool) -> str:
    if b.type == "heading":
        if resume and b.level == 1:
            return f"\\rsection{{{inline_tex(b.text, pdftex)}}}\n"
        cmd = {1: "section", 2: "subsection", 3: "subsubsection"}.get(b.level, "paragraph")
        return f"\\{cmd}{{{inline_tex(b.text, pdftex)}}}\n"
    if b.type == "paragraph":
        return inline_tex(b.text, pdftex) + "\n\n"
    if b.type in ("bullets", "numbered"):
        env = "enumerate" if b.type == "numbered" else "itemize"
        opts = "[leftmargin=*,itemsep=1pt,topsep=2pt]" if resume else "[itemsep=2pt]"
        items = "".join(f"  \\item {inline_tex(i, pdftex)}\n" for i in b.items)
        return f"\\begin{{{env}}}{opts}\n{items}\\end{{{env}}}\n"
    if b.type == "table" and b.rows:
        cols = len(b.rows[0])
        spec = "@{}" + ("p{%.3f\\linewidth}" % (0.96 / cols)) * cols + "@{}"
        head = " & ".join(f"\\textbf{{{inline_tex(c, pdftex)}}}" for c in b.rows[0]) + r" \\"
        rows = "".join(" & ".join(inline_tex(c, pdftex) for c in r) + " \\\\\n" for r in b.rows[1:])
        size = "\\small\n" if cols >= 5 else ""
        return (f"{{{size}\\begin{{longtable}}{{{spec}}}\n\\toprule\n{head}\n\\midrule\n\\endhead\n"
                f"{rows}\\bottomrule\n\\end{{longtable}}}}\n")
    if b.type == "callout":
        return (f"\\noindent\\fcolorbox{{accent}}{{accentbg}}{{\\parbox{{\\dimexpr\\linewidth-2\\fboxsep-2\\fboxrule}}"
                f"{{\\textbf{{{escape_tex(b.label, pdftex)}:}} {inline_tex(b.text, pdftex)}}}}}\n\n")
    if b.type == "quote":
        return f"\\begin{{quote}}\\itshape {inline_tex(b.text, pdftex)}\\end{{quote}}\n"
    if b.type == "code":
        lines = "\\\\\n".join(escape_tex(line, pdftex).replace(" ", "~") for line in b.text.split("\n"))
        return f"{{\\small\\ttfamily\\noindent {lines}}}\n\n"
    if b.type == "math":
        return f"\\[\n{b.text}\n\\]\n" if safe_math(b.text) else escape_tex(b.text, pdftex) + "\n\n"
    if b.type == "pagebreak":
        return "\\newpage\n"
    return ""


def to_latex(doc: Doc, engine: str = "pdflatex") -> str:
    lay = doc.layout
    pdftex = engine == "pdflatex"
    paper = "a4paper" if lay.page_size == "A4" else "letterpaper"
    resume = lay.template == "resume"
    fonts = ("\\usepackage[T1]{fontenc}\n\\usepackage[utf8]{inputenc}\n\\usepackage{lmodern}\n" if pdftex
             else "\\usepackage{fontspec}\n")
    pt = "10pt" if lay.font_pt <= 10.5 else "11pt"
    land = ",landscape" if lay.landscape else ""
    head = [
        f"\\documentclass[{pt},{paper}]{{article}}",
        fonts.rstrip(),
        f"\\usepackage[margin={lay.margins_mm}mm{land}]{{geometry}}",
        "\\usepackage{amsmath,amssymb}",
        "\\usepackage{booktabs,longtable,array}",
        "\\usepackage{enumitem}",
        "\\usepackage[dvipsnames]{xcolor}",
        "\\usepackage{fancyhdr}",
        "\\usepackage{lastpage}",
        "\\usepackage{parskip}",
        "\\definecolor{accent}{HTML}{1F4E79}\\definecolor{accentbg}{HTML}{F1F6FB}",
        "\\setcounter{secnumdepth}{0}",  # plain headings, still listed in the contents
        "\\pagestyle{fancy}\\fancyhf{}\\renewcommand{\\headrulewidth}{0pt}",
    ]
    if lay.page_numbers:
        head.append("\\cfoot{\\small Page \\thepage\\ of \\pageref{LastPage}}")
    if lay.header:
        head.append(f"\\rhead{{\\small\\color{{gray}} {escape_tex(lay.header, pdftex)}}}")
    if resume:
        head.append("\\newcommand{\\rsection}[1]{\\par\\vspace{6pt}{\\color{accent}\\large\\bfseries"
                    "\\MakeUppercase{#1}}\\par\\vspace{-4pt}{\\color{accent}\\rule{\\linewidth}{0.8pt}}\\par"
                    "\\vspace{2pt}}")
    body: list[str] = []
    title = inline_tex(doc.title, pdftex)
    sub = inline_tex(doc.subtitle, pdftex) if doc.subtitle else ""
    by = " \\quad ".join(escape_tex(x, pdftex) for x in (doc.meta.get("contact", "") if resume else "",
                                                          doc.author, doc.date) if x)
    if lay.title_page:
        body.append("\\begin{titlepage}\\vspace*{\\fill}{\\color{accent}\\rule{60mm}{2pt}}\\par\\vspace{12pt}"
                    f"{{\\Huge\\bfseries {title}\\par}}\\vspace{{10pt}}{{\\Large {sub}\\par}}\\vspace{{30pt}}"
                    f"{{\\large {by}\\par}}\\vspace*{{\\fill}}\\end{{titlepage}}")
        if lay.toc:
            body.append("\\tableofcontents\\newpage")
    else:
        align = "\\centering" if resume else "\\raggedright"
        body.append(f"{{{align}{{\\LARGE\\bfseries {title}\\par}}"
                    + (f"\\vspace{{2pt}}{{\\large\\color{{accent}} {sub}\\par}}" if sub else "")
                    + (f"\\vspace{{2pt}}{{\\small {by}\\par}}" if by else "") + "}\\vspace{8pt}")
    body += [_block(b, pdftex, resume) for b in doc.blocks]
    return "\n".join(head) + "\n\\begin{document}\n" + "\n".join(body) + "\n\\end{document}\n"


async def compile_latex(tex: str, out: Path, engine: tuple[str, str] | None = None,
                        timeout_s: float = COMPILE_TIMEOUT_S) -> Path:
    """Compile in a temp folder with shell escape off and file access confined. Raises on failure."""
    found = engine or find_engine()
    if not found:
        raise RuntimeError("no LaTeX engine (tectonic, xelatex or pdflatex) on PATH")
    name, exe = found
    work = Path(tempfile.mkdtemp(prefix="delta-tex-"))
    try:
        (work / "doc.tex").write_text(tex, encoding="utf-8")
        env = {**os.environ, "openin_any": "p", "openout_any": "p", "shell_escape": "f",
               "TECTONIC_UNTRUSTED_MODE": "1", "TEXMFOUTPUT": str(work)}
        if name == "tectonic":
            cmds = [[exe, "--untrusted", "--outdir", str(work), "doc.tex"]]
        else:
            one = [exe, "-no-shell-escape", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error",
                   "doc.tex"]
            cmds = [one, one]  # second pass resolves "Page X of Y" and the contents
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        for cmd in cmds:
            proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(work), env=env, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT, creationflags=flags)
            try:
                output, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(f"{name} took longer than {timeout_s:.0f} s and was stopped") from None
            if proc.returncode != 0:
                tail = output.decode("utf-8", "replace").strip().splitlines()[-6:]
                raise RuntimeError(f"{name} failed: " + " | ".join(tail))
        pdf = work / "doc.pdf"
        if not pdf.is_file():
            raise RuntimeError(f"{name} produced no PDF")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf, out)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)
