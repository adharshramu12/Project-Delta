# Dashboard design system

The dashboard is the page Delta's app window opens (`http://127.0.0.1:<port>/`). It is plain HTML, CSS and
JavaScript served by Delta's local API: no framework, no build step, nothing fetched from the internet, so it works
offline on a fresh Windows PC. This page says how it is put together so changes stay consistent.

## Principles

- **Calm and familiar.** Neutral greys, one blue accent, the Windows font, line icons. No gradients, glow,
  glass effects or emoji. It should feel like the Windows Settings app or a well-made work tool.
- **Say what will happen.** Approval cards state the exact action and why Delta asks. Buttons say what they do
  ("Approve", "Allow for this task", "Deny"), and errors say what went wrong in plain words.
- **One clear next step.** Every empty page says what fills it and offers one action. A fresh install shows
  Get started: connect an AI model, pick a browser, try an example.
- **Light on old laptops.** About 207 KB of code and styles in total (56 KB gzipped), no animation that runs while idle,
  long lists shown a page at a time, polling paused while the window is hidden.

## Files (`src/delta/api/static/`)

| File | What it holds |
|---|---|
| `index.html` | The page frame: sidebar, top bar, every view's static markup, the setup dialog. No inline script or style. |
| `tokens.css` | All design tokens: colours for light and dark, type scale, spacing, radii, motion. |
| `app.css` | Layout and components. Uses tokens only. |
| `theme.js` | Picks light or dark before the page paints (follows Windows unless the user chose one). |
| `core.js` | Shared helpers on `window.Delta`: token, `api()`, the safe element builder `h()`, icons, views, toasts, dialogs, the WebSocket, polling. |
| `chat.js` | Chats, messages, the step trail, the Task progress panel, approvals, questions, proof, documents in chat. |
| `tasks.js`, `schedules.js`, `apps.js`, `documents.js`, `memory.js`, `settings.js` | One view each. |
| `setup.js` | First-run setup. |
| `main.js` | Start-up, top bar status, sidebar, keyboard shortcuts, shutdown. Loaded last. |

`/ui/<name>` serves the `.js` and `.css` files in that folder by exact name (a list made at start-up).
`/tasks.js`, `/schedules.js` and `/apps.js` keep their older addresses.

## Tokens

| Group | Tokens | Notes |
|---|---|---|
| Surfaces | `--bg`, `--surface`, `--surface-2`, `--surface-3` | `--bg` for the sidebar and panels, `--surface` for content. |
| Lines | `--border`, `--border-strong` | Hairlines only; no heavy shadows. |
| Text | `--text`, `--text-2`, `--text-3` | Primary, secondary, muted. All meet WCAG AA (4.5:1) on every surface. |
| Accent | `--accent`, `--accent-hover`, `--accent-fg`, `--accent-soft`, `--accent-text`, `--focus` | The one brand colour. Primary buttons, selection, focus ring. |
| Status | `--ok`, `--warn`, `--danger` and their `-soft` backgrounds | Status pills, approval cards, errors. Never used for decoration. |
| Type | `--font` (Segoe UI Variable, then system fonts), `--mono` (Cascadia Mono, Consolas), `--fs-xs` 12 to `--fs-2xl` 26 | Base size 14 px. |
| Spacing | `--s1` 4 px to `--s12` 48 px | A 4 px grid; most gaps are 8, 12 or 16. |
| Shape | `--r-sm` 6, `--r-md` 8, `--r-lg` 12, `--control-h` 32, `--control-h-sm` 28 | |
| Motion | `--t-fast` 120 ms, `--t` 180 ms | Set to 0 when Windows asks for reduced motion. |

`tests/test_dashboard_ui.py` checks the text colour pairs for AA contrast in both themes.

## Components

- **Buttons:** `.btn` (secondary), `.btn.primary` (one per area), `.btn.danger` (inside confirm dialogs only),
  `.btn.danger-outline` (destructive actions in lists), `.icon-btn` (always with `aria-label`).
- **Pills:** `.pill` with `ok`, `warn`, `danger`, `accent` for states (Done, Waiting, Failed, Working).
- **Cards and lists:** `.card` for grouped settings and apps; `.table-list` rows for tasks, schedules,
  documents and memory.
- **States:** `Delta.empty()` (icon, title, one sentence, one action), `Delta.skeleton()` while loading,
  `Delta.failed()` with Try again, `Delta.toast()` for results of an action.
- **Dialogs:** native `<dialog>` for setup and confirmations (focus is trapped and Escape closes), a side sheet
  for a task's timeline and document previews.

## Accessibility

- Every control is a real `button`, `a`, `input` or `select`, reachable with Tab, with a visible focus ring.
- Shortcuts: Alt+1 to Alt+7 open the views in sidebar order, Ctrl+B collapses the sidebar, Ctrl+Shift+O starts a
  new chat, Enter sends and Shift+Enter adds a line, Escape closes dialogs and popovers.
- Live areas (`aria-live`) announce new messages and status changes; tabs and segmented filters use ARIA roles.
- Windows high contrast (`forced-colors`) keeps borders and focus visible; reduced motion stops spinners.

## Security rules for changes

- Never use `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `eval` or inline event handlers. Build elements with
  `Delta.h()`; text goes in with `textContent`. A test fails the build otherwise.
- No `<script>` or `<style>` in `index.html` and no `style=` attributes: the page's Content-Security-Policy
  (`script-src 'self'; style-src 'self'`) would block them.
- Never load anything from another site (fonts, icons, scripts, images). Links people open themselves are fine.
- The access token stays as it is: read once from the URL fragment, kept in `sessionStorage`, sent as a bearer
  header (and as `?token=` on the WebSocket). Files and screenshots are fetched as blobs, never by URL.

## Screenshots

Update the README pictures (`docs/images/`) when a view changes visibly. Keep each under about 100 KB
(1280 x 800, saved as a palette PNG).
