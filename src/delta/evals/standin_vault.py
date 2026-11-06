"""An in-process stand-in for Windows DPAPI, for tests, scripted evals and developer runs where DPAPI is missing.

seal() returns an opaque random handle and keeps the text in memory, so nothing written to disk is derived from
the secret: like DPAPI, and unlike a reversible "encoding" such as base64, which would also teach static analysis
that secrets flow to disk in plain text. Never used by Delta itself: the real stores use delta.system.vault.
"""

from __future__ import annotations

import secrets


class HandleSealer:
    """A stand-in for DPAPI in tests and on developer machines without it (Linux, macOS). seal() returns an opaque
    random handle and keeps the text in this process only, so nothing written to disk is derived from the secret
    (like DPAPI, and unlike a reversible "encoding"). unseal() of an unknown handle raises ValueError, which the stores
    treat like a value sealed by another Windows account."""

    def __init__(self) -> None:
        self._kept: dict[str, str] = {}

    def seal(self, text: str) -> str:
        handle = "standin:" + secrets.token_hex(16)
        self._kept[handle] = text
        return handle

    def unseal(self, sealed: str) -> str:
        try:
            return self._kept[sealed]
        except KeyError:
            raise ValueError("not sealed in this process") from None


STANDIN = HandleSealer()  # one per process, so a store reopened in the same run can read what it sealed
