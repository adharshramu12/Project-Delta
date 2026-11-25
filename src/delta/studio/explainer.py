"""Explainer: turn a document (a PDF, Word or PowerPoint file, or notes) into an interactive 3D lesson.

The user drops a file on the Build page. Delta reads its text with the same reader as chat attachments
(delta.attachments.extract: pages are kept, scanned pages are marked), makes a new Build project named after it with:
  source/notes.md      the document's text, page by page: the only source of facts for the lesson
  vendor/three/        three.js 0.187.0 (MIT), copied from Delta itself, so the lesson works offline
and starts a Build request that makes the lesson: chapters that each show the idea as a 3D model you can turn and
zoom, short plain explanations next to it, and a quiz at the end. The craft notes (EXPLAINER below) say how; the
model checks its own page in a browser (delta.studio.selfcheck) and fixes errors. Everything else is an ordinary
Build project: it can be changed by asking, previewed, undone and downloaded.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from delta.attachments.kinds import IMAGE, AttachmentError, detect

ASSETS = Path(__file__).parent / "assets" / "three"
EXPLAINER_THREE = "0.187.0"
MAX_BYTES = 30 * 1024 * 1024   # the upload
MAX_PAGES = 80
NOTES_CHARS = 60_000           # the text kept for the lesson (about 15k tokens)
MARKER = "[3D explainer]"      # in the request: the craft notes add the EXPLAINER notes

REQUEST = (f"{MARKER} Make an interactive 3D lesson that explains source/notes.md (\"{{title}}\") to a student. "
           "Read the notes first. Use only facts from them, and cite the page like (p. 3). Then show it in the "
           "preview and check it.")

EXPLAINER = """3D EXPLAINER (this request makes an interactive 3D lesson from source/notes.md):
- Facts only from source/notes.md (read it all first, in parts if long); never invent; cite pages like (p. 4).
  Pick the 5 to 8 ideas that matter most, in a teaching order, for a student who is new to the topic.
- Files: index.html, style.css, lesson.js (the content: export const chapters = [{title, explain: 2-4 plain
  sentences, points: [3 short key points], page, scene: a short name}] and export const quiz = [4-6 questions with
  choices, answer, why]), app.js (the 3D and the interface, a module). index.html loads three.js from the local copy
  with an import map: {"imports": {"three": "./vendor/three/three.module.js",
  "three/addons/controls/OrbitControls.js": "./vendor/three/OrbitControls.js"}}. Never load anything from the web.
- 3D: one renderer and scene. Each chapter builds a small model from primitives that shows the idea, not decoration:
  a process as objects moving along a path, a structure as layers that separate (exploded view), parts of a whole as
  labelled pieces, a comparison side by side, numbers as 3D bars, a cycle as orbiting elements. Soft lighting, a calm
  palette, gentle idle motion; camera glides between chapters; OrbitControls to turn and zoom; clicking a piece shows
  its name and one line about it. Labels as HTML positioned from 3D points (not 3D text).
- Interface: the chapter panel beside the 3D view (below it on a phone) with title, explanation and key points;
  Previous / Next, arrow keys, progress dots; a last chapter with the quiz (one question at a time, instant
  feedback with the reason, a score and Try again). All text is real HTML, readable without the 3D.
- Quality: the DESIGN notes apply; prefers-reduced-motion stops the motion; at most ~60 meshes per chapter, shared
  geometries and materials, pixel ratio capped at 2, rendering paused while the tab is hidden.
- Write the files once, complete. Then start_preview and check_preview, and fix every error it reports."""


class ExplainerError(ValueError):
    """The document cannot be turned into a lesson; the message says why, in plain words."""


def title_of(name: str, found: str = "") -> str:
    base = (found or Path(name).stem).strip()
    base = re.sub(r"[_\s]+", " ", base)
    base = re.sub(r"[^\w .,()'&-]+", "", base).strip(" .")
    return (base or "Lesson")[:60]


def read(path: Path, name: str) -> tuple[str, str, int]:
    """(title, notes text with page headings, pages read) for a document; ExplainerError when it has no text."""
    from delta.attachments.extract import read_document, read_text_file

    with path.open("rb") as f:
        head = f.read(4096)
    try:
        kind = detect(head, name)
    except AttachmentError as e:
        raise ExplainerError(str(e)) from e
    if kind == IMAGE:
        raise ExplainerError("A picture can't be explained yet: send a PDF, Word, PowerPoint or text file.")
    try:
        ex = read_text_file(path, MAX_PAGES) if kind == "text" else read_document(path, kind, MAX_PAGES)
    except AttachmentError as e:
        raise ExplainerError(str(e)) from e
    parts, used = [], 0
    for s in ex.sections:
        body = (s.text or "").strip()
        if not body:
            continue
        label = ex.unit.capitalize() if ex.unit != "part" else "Part"
        block = f"## {label} {s.number}\n\n{body}\n"
        if used + len(block) > NOTES_CHARS:
            parts.append(f"\n(The rest of the document was left out: the lesson covers about {NOTES_CHARS:,} "
                         "characters.)\n")
            break
        parts.append(block)
        used += len(block)
    if not parts:
        why = " It looks scanned (pictures of text)." if any(s.scanned for s in ex.sections) else ""
        raise ExplainerError("No text could be read from that file." + why +
                             " Send a file with real text, or paste the notes into a text file.")
    title = title_of(name, ex.title)
    return title, f"# {title}\n\nSource: {name}\n\n" + "\n".join(parts), len(ex.sections)


def prepare(folder: Path, title: str, notes: str) -> None:
    """Fill a new, empty project folder: the notes and the local copy of three.js."""
    (folder / "source").mkdir(parents=True, exist_ok=True)
    (folder / "source" / "notes.md").write_text(notes, encoding="utf-8")
    vendor = folder / "vendor" / "three"
    vendor.mkdir(parents=True, exist_ok=True)
    for f in ("three.module.js", "three.core.js", "OrbitControls.js", "LICENSE"):
        shutil.copy2(ASSETS / f, vendor / f)

