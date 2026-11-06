"""Documents made by Delta (make_document): list, preview and download in the dashboard.

Same bearer token as every other route. Only files that sit directly in the documents folder and are
listed there can be served (matched by exact name), so no path in a request can reach anything else.
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from delta.addons.writer.document import list_documents

MEDIA = {"pdf": "application/pdf", "md": "text/markdown; charset=utf-8", "html": "text/html; charset=utf-8",
         "tex": "text/plain; charset=utf-8",
         "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def register(app: FastAPI, c: Any, auth: Any) -> None:
    def folder() -> Any:
        from delta.addons.writer.document import documents_dir

        return documents_dir(c.settings.writer.output_dir, create=False)

    @app.get("/api/documents", dependencies=[Depends(auth)])
    async def documents() -> dict[str, Any]:
        return {"folder": str(folder()), "documents": list_documents(folder())}

    @app.get("/api/documents/{name}", dependencies=[Depends(auth)])
    async def document_file(name: str, download: bool = False) -> FileResponse:
        listed = {d["name"] for d in list_documents(folder(), limit=500)}
        if name not in listed or "/" in name or "\\" in name:
            raise HTTPException(404, "no such document")  # only listed files: no path tricks
        kind = name.rsplit(".", 1)[-1].lower()
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if kind == "html":  # an HTML fallback is shown inert: no scripts, nothing fetched
            headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
        return FileResponse(folder() / name, media_type=MEDIA.get(kind, "application/octet-stream"),
                            filename=name if download else None, headers=headers,
                            content_disposition_type="attachment" if download else "inline")
