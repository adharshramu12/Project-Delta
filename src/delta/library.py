"""The Library: everything Delta made for the user in one place, whatever chat it came from. Documents, presentations,
spreadsheets and pictures (in the documents folder) and Build projects, newest first, each with the chat it was made
in, so nobody has to dig through old chats to find a file again.

Only files Delta's own tools write are listed: documents at the top of the documents folder, pictures with the names
find_pictures makes, and projects Build knows. Every action takes an id from that listing and looks the file up in
it again, so no name or path from a request can reach anything else. Delete sends the file to the Recycle Bin;
a Build project is never deleted from here (its folder is the user's code), only opened.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from delta.system.logging import log

DOC_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".md", ".html", ".tex"}
PICTURE = re.compile(r"(?:unsplash|openverse|sticker)-[0-9a-f]{12}\.(?:jpg|png)")
KIND = {".pptx": "presentation", ".xlsx": "spreadsheet"}
# a new name: letters, digits, spaces and a few marks; the extension stays what it was
NAME = re.compile(r"[\w][\w .,()&'+-]{0,99}", re.UNICODE)
MAX_ORIGINS = 2000


class LibraryError(ValueError):
    """Something the user can fix (a bad name, a file that is gone): the message says what."""


class Library:
    def __init__(self, data_dir: Path, folder: Callable[[], Path], chats: Any = None, studio: Any = None) -> None:
        self._file = data_dir / "library.json"
        self._folder = folder
        self.chats = chats
        self.studio = studio
        self._lock = threading.Lock()

    # --- where each file came from -----------------------------------------------------------------------------
    def _origins(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}

    def _save(self, origins: dict[str, dict[str, Any]]) -> None:
        if len(origins) > MAX_ORIGINS:  # the oldest go first
            keep = sorted(origins.items(), key=lambda kv: kv[1].get("made", 0))[-MAX_ORIGINS:]
            origins = dict(keep)
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(origins, indent=1), encoding="utf-8")
        tmp.replace(self._file)

    def note(self, names: list[str], chat_id: str, what: str) -> None:
        if not names:
            return
        with self._lock:
            origins = self._origins()
            for n in names:
                origins[n] = {"chat_id": chat_id, "what": what[:120], "made": time.time()}
            self._save(origins)

    async def on_event(self, event: str, data: dict[str, Any]) -> None:
        """EventBus handler: remember which chat made each document and picture."""
        chat = str(data.get("chat_id") or "")
        if event == "document.created":
            self.note([str(f) for f in data.get("files") or []], chat, str(data.get("title") or ""))
        elif event == "pictures.found":
            self.note([str(p.get("name")) for p in data.get("pictures") or [] if isinstance(p, dict)], chat,
                      str(data.get("query") or ""))

    # --- the listing -----------------------------------------------------------------------------------------
    def _files(self) -> list[tuple[str, Path]]:
        folder = self._folder()
        out: list[tuple[str, Path]] = []
        if folder.is_dir():
            out += [("doc:" + p.name, p) for p in folder.iterdir() if p.is_file() and p.suffix.lower() in DOC_EXTS]
        pics = folder / "Pictures"
        if pics.is_dir():
            out += [("pic:" + p.name, p) for p in pics.iterdir() if p.is_file() and PICTURE.fullmatch(p.name)]
        return out

    def _chat_title(self, chat_id: str) -> str:
        if not chat_id or self.chats is None:
            return ""
        try:
            chat = self.chats.get(chat_id)
        except Exception:
            return ""
        return str(chat.public().get("title", "")) if chat is not None else ""

    def items(self) -> list[dict[str, Any]]:
        origins = self._origins()
        out: list[dict[str, Any]] = []
        for iid, p in self._files():
            try:
                st = p.stat()
            except OSError:
                continue
            o = origins.get(p.name, {})
            ext = p.suffix.lower()
            kind = "picture" if iid.startswith("pic:") else KIND.get(ext, "document")
            out.append({"id": iid, "kind": kind, "name": p.name, "type": ext.lstrip("."), "size": st.st_size,
                        "modified": st.st_mtime, "what": o.get("what", ""), "chat_id": o.get("chat_id", ""),
                        "chat_title": self._chat_title(str(o.get("chat_id", ""))),
                        "sticker": p.name.startswith("sticker-")})
        if self.studio is not None:
            for proj in self.studio.store.projects():
                out.append({"id": "proj:" + proj.id, "kind": "project", "name": proj.name, "type": "project",
                            "size": 0, "modified": max(proj.opened, proj.last_run), "what": proj.path,
                            "chat_id": "", "chat_title": "", "sticker": False})
        out.sort(key=lambda d: -float(d["modified"]))
        for d in out:
            d["modified"] = datetime.fromtimestamp(float(d["modified"])).isoformat(timespec="seconds")
        return out

    def path_of(self, iid: str) -> Path:
        """The file for an id from the listing (looked up in the listing, never built from the id)."""
        found = dict(self._files()).get(iid)
        if found is None:
            raise LibraryError("That item is not in the Library any more. Refresh the list.")
        return found

    # --- actions ----------------------------------------------------------------------------------------------
    def rename(self, iid: str, new: str) -> str:
        if not iid.startswith("doc:"):
            raise LibraryError("Only documents can be renamed. Pictures keep the name that goes with their credit.")
        old = self.path_of(iid)
        stem = new.strip()
        if stem.lower().endswith(old.suffix.lower()):
            stem = stem[: -len(old.suffix)].rstrip()
        if not NAME.fullmatch(stem) or stem.endswith("."):
            raise LibraryError("Use letters, numbers, spaces and simple marks (up to 100 characters), no slashes.")
        target = old.with_name(stem + old.suffix)
        if target.exists() and target != old:
            raise LibraryError(f"There is already a file called {target.name}.")
        old.rename(target)
        with self._lock:
            origins = self._origins()
            if old.name in origins:
                origins[target.name] = origins.pop(old.name)
                self._save(origins)
        log.info("library.renamed")
        return "doc:" + target.name

    def delete(self, iid: str) -> None:
        if iid.startswith("proj:"):
            raise LibraryError("Projects are not deleted from the Library: their folder is your code. Open it in "
                               "Build.")
        p = self.path_of(iid)
        from send2trash import send2trash

        send2trash(str(p))
        with self._lock:
            origins = self._origins()
            if origins.pop(p.name, None) is not None:
                self._save(origins)
        log.info("library.deleted")
