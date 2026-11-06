"""Writer and Documents: human-sounding posts (LinkedIn, Medium, Substack) and clean documents (PDF, DOCX,
Markdown, optional LaTeX).

Pieces:
  rules.py     the writing rules, checked and partly fixed in code (dashes, emojis, AI words, US spellings)
  post.py      platform specs and post generation with a check-and-retry loop
  publish.py   prepare_post: clipboard + the platform's compose page. Delta never presses Post/Publish.
  document.py  the document model, layout planning from content, and the save folder
  render_*.py  HTML/PDF (installed Chrome/Edge through Playwright), DOCX (no extra packages), Markdown
  latex.py     optional LaTeX engine when tectonic/xelatex/pdflatex is on PATH (no shell-escape)
  tools.py     write_post, prepare_post, make_document

Heavy work (Playwright, subprocesses) happens only inside a tool run, so importing this costs little.
"""

from __future__ import annotations

from delta.addons.writer.tools import writer_tools

__all__ = ["writer_tools"]
