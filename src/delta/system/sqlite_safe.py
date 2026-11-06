"""Opening Delta's SQLite files so that a damaged one can never stop Delta from starting.

A power cut, a full disk or a copy that went wrong can leave a file that is no longer a database (or whose first
pages are broken). sqlite3 then raises on the first read, and before this every start-up failed with a traceback.
Now the damaged file is moved aside next to the original (``memory.db.damaged-20261106-1412``, with its -wal and
-shm files), so nothing is deleted and it can still be recovered, and a new, empty file takes its place. Every file
moved aside is listed in ``SET_ASIDE`` and shown to the user as a start-up warning (delta.bootstrap).

Only damage is handled this way. A file that is merely busy or locked (sqlite3.OperationalError: another Delta
process holds it, a backup tool, no permission) is never moved: that error is raised as before.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from pathlib import Path
from typing import Any

SET_ASIDE: list[tuple[Path, Path]] = []  # (the damaged file, where it was moved), for the start-up warning


def is_damage(e: BaseException) -> bool:
    """sqlite3 reports a file that is not a database, or a malformed one, as DatabaseError; locked, busy or
    unopenable files are OperationalError (a subclass), which is not damage."""
    return isinstance(e, sqlite3.DatabaseError) and not isinstance(e, sqlite3.OperationalError)


def set_aside(path: Path) -> Path:
    """Move a damaged database (and its -wal / -shm files) aside, keeping it for recovery. Returns the new name."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.damaged-{stamp}")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.damaged-{stamp}-{n}")
    path.replace(target)
    for side in ("-wal", "-shm", "-journal"):
        extra = path.with_name(path.name + side)
        with contextlib.suppress(OSError):
            if extra.exists():
                extra.replace(target.with_name(target.name + side))
    SET_ASIDE.append((path, target))
    return target


HEADER = b"SQLite format 3\x00"


def _not_a_database(path: Path) -> bool:
    """Checked before SQLite opens the file, so it never touches (or tidies away) the -wal of a damaged one."""
    try:
        with path.open("rb") as f:
            head = f.read(len(HEADER))
    except OSError:
        return False  # missing (a new database) or unreadable: sqlite3 decides and reports it
    return bool(head) and head != HEADER


def connect(path: Path | str, **kwargs: Any) -> sqlite3.Connection:
    """sqlite3.connect, plus one read of the schema. A damaged file is moved aside and a new one is opened."""
    if str(path) == ":memory:" or str(path).startswith("file:"):
        return sqlite3.connect(path, **kwargs)
    if _not_a_database(Path(path)):
        set_aside(Path(path))
        return sqlite3.connect(path, **kwargs)
    db = sqlite3.connect(path, **kwargs)
    try:
        db.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return db
    except sqlite3.DatabaseError as e:
        db.close()
        if not is_damage(e):
            raise
    set_aside(Path(path))
    return sqlite3.connect(path, **kwargs)


def warning_text(original: Path, moved: Path) -> str:
    return (f"{original.name} in Delta's data folder could not be read (the file is damaged, for example by a power "
            f"cut or a full disk). Delta kept it as {moved.name} and started a new, empty {original.name}. "
            "Nothing else was changed.")
