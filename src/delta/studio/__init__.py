"""Delta Build (the "Build" panel): a coding agent for the user's own projects, separate from Delta's PC agent.

Layout
  jail.py    which folders may be a project, and the check that keeps every path inside its project
  store.py   the project list, each project's chat, and the turn backups that make Undo possible
  tools.py   the coding tools (list, read, search, write, edit, delete, project memory), all jailed
  agent.py   the loop: the user's message, the model, the tools, until the model answers
  service.py one run at a time per project, events for the dashboard, stop and undo

What it shares with Delta, on purpose: the model router (the user's keys and backup AIs), the event bus (the
dashboard's live connection) and the audit log. What it never touches: Delta's PC tools, its memory, its task
lock and its permission levels. A Build run cannot open apps, browse, send anything or run commands (milestone 1);
it can only read and change files inside the one project folder, and every change can be undone.
"""
