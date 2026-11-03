"""Encryption at rest for what Delta knows about the user (memories, chats) with Windows DPAPI.

DPAPI ties the key to the signed-in Windows account: another user on the PC, a copied disk or a stolen
database file cannot read it, and there is no key file to lose. Sealed values carry a prefix, so rows
written before encryption existed still read (and get sealed on the next start). Off Windows, or if
DPAPI fails, text is stored as before rather than lost.
"""

from __future__ import annotations

import base64
import contextlib
import sys

PREFIX = "dpapi:"
_ENTROPY = b"delta-agent/v1"  # app-specific salt: other DPAPI users of the same account get other keys
_NO_UI = 0x1  # CRYPTPROTECT_UI_FORBIDDEN: never pop up a prompt


def scrub(db: object) -> None:
    """Leave no old plain copy in the database files: deleted data is zeroed from now on, the write-ahead
    log is emptied, and free pages (which can still hold old text) are compacted away."""
    import sqlite3

    assert isinstance(db, sqlite3.Connection)
    db.execute("PRAGMA secure_delete = ON")
    with contextlib.suppress(sqlite3.Error):
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if db.execute("PRAGMA freelist_count").fetchone()[0]:
            db.execute("VACUUM")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import win32crypt  # noqa: F401
    except ImportError:
        return False
    return True


def seal(text: str) -> str:
    if not text or text.startswith(PREFIX) or not available():
        return text
    import win32crypt

    try:
        blob = win32crypt.CryptProtectData(text.encode("utf-8"), "Delta", _ENTROPY, None, None, _NO_UI)
    except Exception:
        return text
    return PREFIX + base64.b64encode(blob).decode("ascii")


def unseal(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value
    import win32crypt

    with contextlib.suppress(Exception):
        _, data = win32crypt.CryptUnprotectData(base64.b64decode(value[len(PREFIX):]), _ENTROPY, None, None, _NO_UI)
        return bytes(data).decode("utf-8")
    return "(encrypted by another Windows account)"
