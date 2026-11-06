"""Perception cache: never send the model a view it already holds.

The browser already sends only the changes of a page after an action (tools/snapshot.py). This extends the
idea to the desktop and to pictures:

- screenshots: a fingerprint of each picture sent in a task (images.digest). The same window looking the
  same again is not attached a second time; the model is told it is unchanged.
- marked pictures: the fingerprint plus the legend. The same view asked the same question returns the
  answer already given, with no vision call.
- window reads (UI Automation trees) after an action: the last full read of each window. When that read is
  in the result right before, an unchanged window is one line and a changed one is sent as "+ new" /
  "- gone" lines when that is clearly smaller; then the next read is sent in full again, so a diff never
  builds on text the agent loop has already shortened.

Everything is in memory, per task, and dropped when the task ends.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

DIFF_PREFIX = "Window changes: "
SAME_WINDOW = "(no change in this window since the last read; everything listed there is still current)"
SAME_PICTURE = "(the screen looks the same as in the last screenshot, so it was not sent again)"
MAX_KEYS = 64  # views remembered per task


@dataclass
class _Task:
    pictures: dict[str, str] = field(default_factory=dict)      # view -> digest of the last picture sent
    answers: dict[str, str] = field(default_factory=dict)       # digest|question -> answer
    trees: dict[str, tuple[list[str], int]] = field(default_factory=dict)  # window -> (full read sent, seq)


class PerceptionCache:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._tasks: dict[str, _Task] = {}
        self._lock = threading.Lock()
        self.hits = 0

    def _task(self, task_id: str) -> _Task:
        t = self._tasks.get(task_id)
        if t is None:
            t = self._tasks[task_id] = _Task()
        return t

    def forget(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    # --- pictures ---------------------------------------------------------------------------------
    def picture_seen(self, task_id: str, view: str, digest: str) -> bool:
        """True when this view's last picture in this task looked the same (and records it otherwise)."""
        if not self.enabled or not task_id:
            return False
        with self._lock:
            t = self._task(task_id)
            if t.pictures.get(view) == digest:
                self.hits += 1
                return True
            if len(t.pictures) >= MAX_KEYS:
                t.pictures.pop(next(iter(t.pictures)))
            t.pictures[view] = digest
            return False

    def answer(self, task_id: str, key: str) -> str | None:
        if not self.enabled or not task_id:
            return None
        with self._lock:
            found = self._tasks.get(task_id, _Task()).answers.get(key)
            if found is not None:
                self.hits += 1
            return found

    def keep_answer(self, task_id: str, key: str, text: str) -> None:
        if not self.enabled or not task_id:
            return
        with self._lock:
            answers = self._task(task_id).answers
            if len(answers) >= MAX_KEYS:
                answers.pop(next(iter(answers)))
            answers[key] = text

    # --- window trees -------------------------------------------------------------------------------
    def window_text(self, task_id: str, window: str, text: str, seq: int) -> tuple[str, bool]:
        """What to send for a window read: (text, from_cache).

        seq numbers the tool results of the task. A diff (or "unchanged") is sent only against a full read
        sent in the result right before this one, which the agent loop always keeps whole; otherwise, or when
        a diff would not be clearly smaller, the full text is sent and becomes the new base."""
        if not self.enabled or not task_id:
            return text, False
        lines = text.splitlines()
        key = window.strip().lower()
        with self._lock:
            t = self._task(task_id)
            base = t.trees.get(key)
            if base is not None and seq - base[1] == 1:
                if base[0] == lines:
                    self.hits += 1
                    return SAME_WINDOW, True
                diff = tree_diff(base[0], lines, window)
                if len(diff) < len(text) * 0.6:
                    self.hits += 1
                    return diff, True
            if len(t.trees) >= MAX_KEYS and key not in t.trees:
                t.trees.pop(next(iter(t.trees)))
            t.trees[key] = (lines, seq)
            return text, False


def tree_diff(before: list[str], after: list[str], window: str = "") -> str:
    """"+ " lines that appeared and "- " lines that went, in order (a multiset difference, so repeated
    controls such as two "OK" buttons are counted correctly)."""
    from collections import Counter

    old, new = Counter(before), Counter(after)
    added: list[str] = []
    seen: Counter[str] = Counter()
    for line in after:
        seen[line] += 1
        if seen[line] > old.get(line, 0):
            added.append(line)
    removed: list[str] = []
    seen = Counter()
    for line in before:
        seen[line] += 1
        if seen[line] > new.get(line, 0):
            removed.append(line)
    head = f"{DIFF_PREFIX}{window or 'window'} (lines not listed are unchanged)"
    return "\n".join([head, *[f"+ {x}" for x in added], *[f"- {x}" for x in removed]])
