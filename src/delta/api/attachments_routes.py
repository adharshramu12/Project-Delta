"""Attachments in the dashboard chat: upload (raw body, streamed to disk), list, a small preview, remove.

Same bearer token as every other route. The upload body is the file itself (no multipart parser needed, so no
extra dependency): it is written to disk in chunks while the size limit is counted, never held in memory, and a
Content-Length over the limit is refused before reading. The file name is only displayed; files are stored under
a random id in Delta's protected data folder (delta.attachments.store).
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response

from delta.attachments.kinds import AttachmentError, too_big

PREVIEW_SIDE = 240


def register(app: FastAPI, c: Any, auth: Any) -> None:
    def service() -> Any:
        found = getattr(c.orchestrator, "attachments", None)
        if found is None:
            raise HTTPException(503, "Attachments are switched off in Delta's settings (attachments.enabled).")
        return found

    @app.put("/api/attachments", dependencies=[Depends(auth)])
    async def upload(request: Request, name: str = "", chat_id: str = "") -> dict[str, Any]:
        svc = service()
        declared = request.headers.get("content-length")
        size = int(declared) if declared and declared.isdigit() else None
        if size is not None and size > svc.store.max_bytes:
            raise HTTPException(413, too_big(size, svc.cfg.max_mb))
        chats = getattr(c, "chats", None)
        if chat_id and (chats is None or chats.get(chat_id) is None):
            chat_id = ""  # a chat that is gone: the file waits until the message is sent

        async def body() -> AsyncIterator[bytes]:
            async for chunk in request.stream():
                yield chunk

        try:
            att, ex = await svc.ingest_stream(body(), name or "file", chat_id, "dashboard", size)
        except AttachmentError as e:
            status = 413 if "The limit is" in str(e) else 400
            raise HTTPException(status, str(e)) from None
        c.audit.record("attachment.added", id=att.id, kind=att.kind, size=att.size, via="dashboard")
        return {"attachment": att.public(), "notes": ex.notes, "can_see_pictures": svc.can_see()}

    @app.get("/api/attachments", dependencies=[Depends(auth)])
    async def list_attachments(chat_id: str = "") -> dict[str, Any]:
        svc = service()
        return {"attachments": [a.public() for a in svc.store.list(chat_id)] if chat_id else [],
                "limits": {"max_mb": svc.cfg.max_mb, "max_files": svc.cfg.max_files_per_message},
                "can_see_pictures": svc.can_see()}

    @app.get("/api/attachments/{att_id}/preview", dependencies=[Depends(auth)])
    async def preview(att_id: str) -> Response:
        """A small JPEG of a picture for the chat (made on the fly, never the original file)."""
        svc = service()
        att = svc.store.get(att_id)
        if att is None or att.kind != "image":
            raise HTTPException(404, "no preview")

        def thumb() -> bytes:
            from delta.attachments.images import open_image

            img = open_image(svc.store.path(att))
            img.thumbnail((PREVIEW_SIDE, PREVIEW_SIDE))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=70)
            return buf.getvalue()

        try:
            data = await asyncio.to_thread(thumb)
        except AttachmentError:
            raise HTTPException(404, "no preview") from None
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store",
                                                                "X-Content-Type-Options": "nosniff"})

    @app.delete("/api/attachments/{att_id}", dependencies=[Depends(auth)])
    async def remove(att_id: str) -> dict[str, bool]:
        svc = service()
        att = svc.store.get(att_id)
        if att is None:
            return {"ok": False}
        svc.store.delete(att)
        c.audit.record("attachment.removed", id=att.id)
        return {"ok": True}
