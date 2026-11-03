"""send_file: give the user a file or folder from this PC in the chat they asked from.

Away from home, the user asks on Telegram "send me the Arva pitch deck"; Delta finds it and it
arrives in that chat. Folders are zipped first. It only ever goes to the person who asked, in their
own chat (the paired owner), so it is not "sending data to someone else"; blocked and protected folders
stay off-limits through the normal policy checks on the path."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from delta.core.state import CURRENT_REQUEST
from delta.tools.base import Risk, Tool, ToolResult

LIMIT_MB = {"telegram": 50}  # what a chat service accepts per file (Telegram bots: 50 MB)
CHAT_CHANNELS = {"telegram", "slack"}


class SendFileArgs(BaseModel):
    path: str = Field(description="The file or folder to send (folders are zipped)")
    caption: str = Field(default="", description="A short note shown with the file")


class SendFile(Tool[SendFileArgs]):
    name = "send_file"
    description = ("Send a file or folder from this PC to the user in the chat they wrote from (e.g. on their "
                   "phone in Telegram). Find it first with search_files/list_dir. Folders are zipped.")
    risk = Risk.ACT  # to the requesting owner only
    Args = SendFileArgs

    def __init__(self, live_channels: dict[str, Any]) -> None:
        self.live_channels = live_channels  # the Container's running channels (filled at start-up)
        self.guard: Any = None  # the Container's RemoteGuard, set at start-up

    def paths(self, args: SendFileArgs) -> list[str]:
        return [args.path]

    def summary(self, args: SendFileArgs) -> str:
        return f"send {args.path} to you in this chat"

    async def run(self, args: SendFileArgs) -> ToolResult:
        # The real name, as the user knows it (norm_path lowercases for the policy's comparisons).
        p = Path(os.path.abspath(os.path.expandvars(os.path.expanduser(args.path))))
        if not p.exists():
            return ToolResult(False, f"not found: {p}. Find it with search_files or list_dir first.")
        req = CURRENT_REQUEST.get()
        channel = self.live_channels.get(req.channel) if req else None
        if req is None or req.channel not in CHAT_CHANNELS or not hasattr(channel, "send_file"):
            # Asked on this PC: the file is already here.
            return ToolResult(True, f"The file is on this PC at {p} (open it with open_target).",
                              {"path": str(p), "delivered": False})
        g = self.guard
        if g is not None and not g.is_unlocked(req.user):  # a stolen phone must not empty the PC
            why = g.locked_message() if g.locked else (
                "Sending files needs the owner's PIN: send /unlock, enter the PIN, then ask again." if g.has_pin
                else "Set a remote PIN on the PC first (Settings > Remote security) to send files to your phone.")
            return ToolResult(False, why, {"path": str(p), "needs_pin": True})
        upload, zipped = (await asyncio.to_thread(_zip, p), True) if p.is_dir() else (p, False)
        size_mb = upload.stat().st_size / 1e6
        limit = LIMIT_MB.get(req.channel, 50)
        if size_mb > limit:
            return ToolResult(False, f"{upload.name} is {size_mb:.0f} MB; {req.channel} accepts up to {limit} MB. "
                                     "Offer the user a smaller part (fewer files, or a zip of just what they need).",
                              {"path": str(p), "size_mb": round(size_mb, 1)})
        caption = args.caption or (f"{p.name} (folder, zipped)" if zipped else p.name)
        await channel.send_file(req.user, upload, caption)  # type: ignore[union-attr]
        return ToolResult(True, f"sent {upload.name} ({size_mb:.1f} MB) to the user in {req.channel}",
                          {"path": str(p), "delivered": True, "verified": True, "size_mb": round(size_mb, 1)})


def _zip(folder: Path) -> Path:
    out = Path(tempfile.mkdtemp(prefix="delta-send-")) / folder.name
    return Path(shutil.make_archive(str(out), "zip", root_dir=folder))
