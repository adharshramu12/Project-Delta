"""Encryption at rest for what Delta knows about the user (memories, chats) with Windows DPAPI.

DPAPI ties the key to the signed-in Windows account: another user on the PC, a copied disk or a stolen
database file cannot read it, and there is no key file to lose. Sealed values carry a prefix, so rows
written before encryption existed still read (and get sealed on the next start).

On Windows, encryption is not optional: if pywin32 (win32crypt) is missing or DPAPI fails, seal() raises
VaultUnavailableError and nothing is written, instead of silently storing plain text (before 0.2.0 it fell
back to plain text). Rows already on disk are left as they are. Repair: run the installer again (or
`delta update`), which reinstalls pywin32. Off Windows (Linux or macOS developer runs; not a supported
platform for real data) there is no DPAPI and text is stored as plain text; `delta doctor` says so.
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


UNAVAILABLE = ("Delta could not encrypt this with Windows DPAPI ({why}), so it was not saved. Delta never stores "
               "your chats or memories unencrypted. Repair Delta by running the installer again or `delta update`.")


class VaultUnavailableError(RuntimeError):
    """Windows, but no working DPAPI: refuse to store plain text."""


def required() -> bool:
    """Encryption at rest is required on Windows (the platform Delta supports)."""
    return sys.platform == "win32"


def problem() -> str:
    """'' when sealing works (or is not required here); else why not, for start-up warnings and doctor."""
    if not required():
        return ""
    if not available():
        return UNAVAILABLE.format(why="pywin32 is missing")
    return ""


def available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import win32crypt  # noqa: F401
    except ImportError:
        return False
    return True


def seal(text: str) -> str:
    if not text or text.startswith(PREFIX):
        return text
    if not available():
        if required():
            raise VaultUnavailableError(UNAVAILABLE.format(why="pywin32 is missing"))
        return text  # not Windows: no DPAPI here (developer runs)
    return seal_strict(text)


def seal_strict(text: str) -> str:
    """DPAPI or an error, never the text itself: for secrets written to their own files (connected apps' tokens,
    saved logins), where a fallback to plain text must be impossible rather than merely checked for afterwards."""
    if not available():
        raise VaultUnavailableError(UNAVAILABLE.format(why="pywin32 is missing"))
    import win32crypt

    try:
        blob = win32crypt.CryptProtectData(text.encode("utf-8"), "Delta", _ENTROPY, None, None, _NO_UI)
    except Exception as e:
        raise VaultUnavailableError(UNAVAILABLE.format(why=f"DPAPI failed: {type(e).__name__}")) from e
    return PREFIX + base64.b64encode(blob).decode("ascii")


def seal_or_keep(text: str) -> str:
    """For re-sealing rows already on disk at start-up: when sealing is impossible the row stays exactly as it
    was (it is already there; nothing new is written in plain text)."""
    try:
        return seal(text)
    except VaultUnavailableError:
        return text


def unseal(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value
    import win32crypt

    with contextlib.suppress(Exception):
        _, data = win32crypt.CryptUnprotectData(base64.b64decode(value[len(PREFIX):]), _ENTROPY, None, None, _NO_UI)
        return bytes(data).decode("utf-8")
    return "(encrypted by another Windows account)"
