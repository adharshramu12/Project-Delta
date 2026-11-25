"""Craft notes for a Build request: how to make a page look designed, and how to make a small game well.

Sent only with a request that is about a web page / UI or a game (a few hundred tokens), never with others, and
shared by both engines (Delta's own agent and Claude Code). The notes ask for the whole result in few, complete file
writes, which is also what keeps a new page or game cheap in tokens.
"""

from __future__ import annotations

import re

DESIGN = """DESIGN (this request makes or changes a page): make it look designed, calm and modern, not a template.
- Structure: semantic HTML (header, main, section, nav, footer, button, label); real content that fits the request,
  never lorem ipsum. Files: index.html, styles.css, script.js (one folder, relative links) unless the project has its
  own structure.
- Look: one clear hierarchy (one h1, a short intro, sections), generous whitespace on an 8px scale, a readable measure
  (~65 characters), system font stack (or one well-chosen web font), 2 or 3 type sizes with real contrast.
- Colour: CSS custom properties on :root; neutral surfaces, one accent used sparingly, text contrast at least 4.5:1;
  a dark theme through @media (prefers-color-scheme: dark) redefining the same variables.
- Detail: 8 to 12px radii, soft borders or a light shadow (not both everywhere), consistent spacing, hover and
  :focus-visible states, transitions of 150 to 250ms, prefers-reduced-motion respected.
- Layout: mobile first, flex/grid, max-width container, no horizontal scroll at 360px; images with width/height and alt.
- No frameworks or CDNs unless asked. Write each file once, complete (write_file), then show it (start_preview) and
  check it (check_preview)."""

GAME = """GAME (this request makes or changes a game): a small, polished, playable game.
- One page: index.html with a <canvas>, style.css, game.js (or one HTML file for a tiny game). No libraries, no image
  or sound files: draw with shapes and colour; make sounds with the Web Audio API only if asked.
- Loop: requestAnimationFrame with delta time (movement in pixels per second, so speed is the same on every screen);
  states: title, playing, paused (P or Esc), game over with restart (Enter or tap).
- Input: keyboard (arrows / WASD / Space) and touch (tap or swipe) so it works on a phone; the canvas scales to the
  window and keeps its aspect ratio (devicePixelRatio for sharp drawing).
- Feel: clear goal on the title screen, score on screen, best score kept in localStorage, difficulty rising slowly,
  small effects (screen shake, flashes, particles) kept cheap.
- Code: a handful of small functions (update, draw, reset, input), constants at the top for tuning, no dead code.
- Write each file once, complete (write_file), then start_preview and check_preview; fix any error from the page."""

_GAME = re.compile(r"\bgames?\b|\bplayable\b|snake|tetris|pong|breakout|flappy|platformer|shooter|arcade|puzzle game|"
                   r"tic.?tac.?toe|chess|memory game|quiz game|endless runner|canvas game", re.I)
_DESIGN = re.compile(r"\b(website|web ?site|web ?page|landing|page|ui|ux|design|redesign|layout|portfolio|dashboard|"
                     r"html|css|style|styling|theme|frontend|front-end|hero|navbar|homepage|home page|form|component|"
                     r"responsive|beautiful|modern|pretty|look)\b", re.I)


def notes_for(request: str) -> str:
    """The craft notes this request needs ("" for none)."""
    parts = []
    if _GAME.search(request or ""):
        parts.append(GAME)
    if _DESIGN.search(request or ""):
        parts.append(DESIGN)
    return "\n\n".join(parts)
