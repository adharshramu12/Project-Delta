"""Tokens and keys for connected apps, sealed with Windows DPAPI (delta.system.vault).

One file per app in <data dir>/connectors/secrets/<app>.bin holding one sealed JSON object: the OAuth tokens and
client registration, or the personal access token / API key the user pasted, or a local server's declared
secrets. Delta's data folder is protected (no tool can read it) and the sealed value can only be opened by the
same Windows account on the same PC.

If DPAPI is not available (pywin32 missing, or not Windows), saving is REFUSED: a connected app's token is never
written in plain text. Tests pass their own seal/unseal pair.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from delta.system import vault

NOT_SEALED = ("Delta could not encrypt this app's sign-in with Windows DPAPI, so it was not saved. Delta never "
              "stores app tokens unencrypted. Repair Delta by running the installer again or `delta update`.")


class SecretsUnavailableError(RuntimeError):
    pass


class SecretBox:
    def __init__(self, folder: Path, seal: Callable[[str], str] | None = None,
                 unseal: Callable[[str], str] | None = None) -> None:
        self.folder = folder
        self._seal = seal
        self._unseal = unseal

    def available(self) -> bool:
        return self._seal is not None or vault.available()

    def _path(self, app_id: str) -> Path:
        if not app_id.replace("_", "").isalnum():
            raise ValueError("bad app id")
        return self.folder / f"{app_id}.bin"

    def save(self, app_id: str, data: dict[str, Any]) -> None:
        text = json.dumps(data, separators=(",", ":"))
        if self._seal is not None:
            sealed = self._seal(text)
        else:
            try:
                sealed = vault.seal_strict(text)  # never returns the text: no plain-text path to the file at all
            except vault.VaultUnavailableError as e:
                raise SecretsUnavailableError(NOT_SEALED) from e
            if not sealed.startswith(vault.PREFIX):  # only a DPAPI-sealed value ("dpapi:...") is ever written
                raise SecretsUnavailableError(NOT_SEALED)
        if sealed == text or text in sealed:  # a sealer that did nothing (or kept the text) is not a sealer
            raise SecretsUnavailableError(NOT_SEALED)
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._path(app_id)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(sealed, encoding="ascii")
        os.replace(tmp, path)  # atomic: never half a token on disk

    def load(self, app_id: str) -> dict[str, Any]:
        try:
            sealed = self._path(app_id).read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            return {}
        try:
            text = self._unseal(sealed) if self._unseal is not None else vault.unseal(sealed)
            data = json.loads(text)
        except Exception:
            return {}  # sealed by another Windows account, or damaged: treated as signed out
        return data if isinstance(data, dict) else {}

    def delete(self, app_id: str) -> bool:
        path = self._path(app_id)
        if not path.exists():
            return False
        with contextlib.suppress(OSError):
            size = path.stat().st_size
            with open(path, "r+b") as f:  # overwrite before deleting: no old copy left in the file's blocks
                f.write(b"\0" * size)
                f.flush()
                os.fsync(f.fileno())
        path.unlink(missing_ok=True)
        return True

    def exists(self, app_id: str) -> bool:
        return self._path(app_id).exists()
