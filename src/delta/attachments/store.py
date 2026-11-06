"""Attached files on disk: <data>/attachments/<chat>/<id><ext> plus <id>.json (name, kind, size, hash).

- Streamed to disk in small chunks while the size limit is counted, so a big upload never sits in memory and a
  lying Content-Length cannot get past the limit.
- Stored under a random id: the name the user's file had is only ever shown, never used as a path, so no name
  can climb out of the folder (path traversal) or clash with a Windows device name.
- Inside Delta's data folder, which the agent's own file tools can never write (PolicyEngine protects it).
- Removed after `retention_days`, and with their chat when the chat is deleted.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from delta.attachments.kinds import AttachmentError, detect, safe_name, too_big

ID = re.compile(r"^[a-f0-9]{12}$")
FOLDER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
UNSENT = "unsent"  # uploaded in the dashboard before the chat existed; moved into the chat when sent
INCOMING = ".incoming"
CACHE = ".cache"
CHUNK = 64 * 1024
HEAD = 8192


@dataclass
class Attachment:
    id: str
    name: str
    kind: str
    size: int
    sha256: str
    ext: str
    folder: str
    created: float = field(default_factory=time.time)
    source: str = "dashboard"  # dashboard | telegram | eval
    pages: int = 0  # filled once read: pages, slides or parts
    summary: str = ""  # one short line for the chip in the chat, e.g. "12 pages" or "photo, 1600x1200"

    def public(self) -> dict[str, Any]:
        """What the dashboard and the chat history may show (no paths)."""
        return {"id": self.id, "name": self.name, "kind": self.kind, "size": self.size, "pages": self.pages,
                "summary": self.summary}


def folder_for(chat_id: str) -> str:
    return chat_id if chat_id and FOLDER.match(chat_id) and chat_id not in (INCOMING, CACHE) else UNSENT


class AttachmentStore:
    def __init__(self, root: Path, max_mb: float = 20, retention_days: float = 30) -> None:
        self.root = root
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.max_mb = max_mb
        self.retention_days = retention_days
        self._lock = threading.Lock()

    # --- paths ------------------------------------------------------------------------------------
    def _dir(self, folder: str) -> Path:
        if not FOLDER.match(folder) or folder in (INCOMING, CACHE):
            raise ValueError("bad attachment folder")
        return self.root / folder

    def path(self, att: Attachment) -> Path:
        return self._dir(att.folder) / f"{att.id}{att.ext}"

    @property
    def cache_dir(self) -> Path:
        return self.root / CACHE

    # --- saving -----------------------------------------------------------------------------------
    def _incoming(self) -> Path:
        d = self.root / INCOMING
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{secrets.token_hex(8)}.part"

    async def save_stream(self, chunks: AsyncIterator[bytes], name: str, chat_id: str = "", source: str = "dashboard",
                          declared: int | None = None) -> Attachment:
        """Write an upload to disk chunk by chunk, counting the limit as it arrives (never trusting a declared
        size), then file it under a new id. AttachmentError (plain words) when it is too big or not supported."""
        if declared is not None and declared > self.max_bytes:
            raise AttachmentError(too_big(declared, self.max_mb))
        tmp = self._incoming()
        digest, size, head = hashlib.sha256(), 0, b""
        try:
            with tmp.open("wb") as f:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise AttachmentError(too_big(size, self.max_mb))
                    if len(head) < HEAD:
                        head += chunk[: HEAD - len(head)]
                    digest.update(chunk)
                    f.write(chunk)
            return self._file(tmp, name, head, size, digest.hexdigest(), chat_id, source)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()

    def save_file(self, src: Path, name: str, chat_id: str = "", source: str = "telegram") -> Attachment:
        """File a file already on disk (a Telegram download): copied in chunks with the same checks, then the
        caller may delete its copy."""

        def chunks() -> Iterable[bytes]:
            with src.open("rb") as f:
                while block := f.read(CHUNK):
                    yield block

        size = src.stat().st_size
        if size > self.max_bytes:
            raise AttachmentError(too_big(size, self.max_mb))
        tmp = self._incoming()
        digest, head = hashlib.sha256(), b""
        try:
            with tmp.open("wb") as f:
                for block in chunks():
                    if len(head) < HEAD:
                        head += block[: HEAD - len(head)]
                    digest.update(block)
                    f.write(block)
            return self._file(tmp, name, head, size, digest.hexdigest(), chat_id, source)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()

    def save_bytes(self, data: bytes, name: str, chat_id: str = "", source: str = "eval") -> Attachment:
        """Small files made in code (tests, evals)."""
        if len(data) > self.max_bytes:
            raise AttachmentError(too_big(len(data), self.max_mb))
        tmp = self._incoming()
        try:
            tmp.write_bytes(data)
            return self._file(tmp, name, data[:HEAD], len(data), hashlib.sha256(data).hexdigest(), chat_id, source)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _file(self, tmp: Path, name: str, head: bytes, size: int, sha: str, chat_id: str, source: str) -> Attachment:
        clean = safe_name(name)
        kind = detect(head, clean)  # raises AttachmentError for unsupported files
        ext = Path(clean).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
            ext = {"image": ".jpg", "pdf": ".pdf", "text": ".txt"}.get(kind, ".bin")
        att = Attachment(secrets.token_hex(6), clean, kind, size, sha, ext, folder_for(chat_id), source=source)
        target = self.path(att)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, target)
        self._write_meta(att)
        return att

    def _write_meta(self, att: Attachment) -> None:
        meta = self._dir(att.folder) / f"{att.id}.json"
        tmp = meta.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(att)), encoding="utf-8")
        os.replace(tmp, meta)

    def update(self, att: Attachment) -> None:
        with self._lock:
            self._write_meta(att)

    # --- finding ----------------------------------------------------------------------------------
    def get(self, att_id: str) -> Attachment | None:
        if not ID.match(att_id or ""):
            return None
        if not self.root.is_dir():
            return None
        for meta in self.root.glob(f"*/{att_id}.json"):
            if meta.parent.name in (INCOMING, CACHE):
                continue
            try:
                raw = json.loads(meta.read_text(encoding="utf-8"))
                att = Attachment(**{k: raw[k] for k in Attachment.__dataclass_fields__ if k in raw})
            except (OSError, ValueError, TypeError, KeyError):
                return None
            if att.id != att_id or att.folder != meta.parent.name or not self.path(att).is_file():
                return None
            return att
        return None

    def list(self, chat_id: str) -> list[Attachment]:
        folder = folder_for(chat_id)
        d = self.root / folder
        out = []
        for meta in sorted(d.glob("*.json")) if d.is_dir() else []:
            if (att := self.get(meta.stem)) is not None:
                out.append(att)
        return sorted(out, key=lambda a: a.created)

    def move(self, att: Attachment, chat_id: str) -> Attachment:
        """Into the chat it was sent in (uploads made before the chat existed wait in 'unsent')."""
        folder = folder_for(chat_id)
        if folder == att.folder:
            return att
        with self._lock:
            src, src_meta = self.path(att), self._dir(att.folder) / f"{att.id}.json"
            att.folder = folder
            dst = self.path(att)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dst)
            self._write_meta(att)
            with contextlib.suppress(OSError):
                src_meta.unlink()
        return att

    def delete(self, att: Attachment) -> None:
        with self._lock:
            for p in (self.path(att), self._dir(att.folder) / f"{att.id}.json"):
                with contextlib.suppress(OSError):
                    p.unlink()

    def delete_chat(self, chat_id: str) -> int:
        folder = folder_for(chat_id)
        if folder == UNSENT:
            return 0
        d = self.root / folder
        n = len(list(d.glob("*.json"))) if d.is_dir() else 0
        shutil.rmtree(d, ignore_errors=True)
        return n

    def prune(self, now: float | None = None) -> int:
        """Remove files older than retention_days, unfinished uploads older than a day, and empty chat folders.
        Never raises: an unreadable folder must not stop Delta."""
        now = now or time.time()
        removed = 0
        if not self.root.is_dir():
            return 0
        keep_s = self.retention_days * 86400
        try:
            for d in list(self.root.iterdir()):
                if not d.is_dir():
                    continue
                if d.name == INCOMING:
                    for f in d.iterdir():
                        with contextlib.suppress(OSError):
                            if now - f.stat().st_mtime > 86400:
                                f.unlink()
                    continue
                if d.name == CACHE:
                    continue
                for meta in list(d.glob("*.json")):
                    att = self.get(meta.stem)
                    created = att.created if att else meta.stat().st_mtime
                    if self.retention_days > 0 and now - created > keep_s:
                        if att:
                            self.delete(att)
                        else:
                            with contextlib.suppress(OSError):
                                meta.unlink()
                        removed += 1
                with contextlib.suppress(OSError):
                    if not any(d.iterdir()):
                        d.rmdir()
        except OSError:
            pass
        return removed
