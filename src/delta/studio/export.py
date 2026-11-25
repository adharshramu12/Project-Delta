"""Download a Build project as one clean zip file.

What goes in: every file of the project, in its folders, under one top folder named after the project (so unzipping
never spills files into the Downloads folder). What stays out, on purpose:
- secret files (.env, keys, certificates, credentials: delta.studio.jail.is_secret): a zip is easy to share by mistake;
- what can be made again and only makes the zip huge: node_modules, virtual environments, build output, caches
  (delta.studio.jail.SKIP_DIRS), and .git;
- links that point outside the project.
Delta's project memory (.delta/PROJECT.md) is kept: it explains the project to whoever opens it next.

The zip is written to a temporary file (never held in memory) and the list of what was left out is added as
DELTA_EXPORT.txt, so the person who unzips it knows why node_modules or .env are missing and how to get them back.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from delta.studio.jail import SKIP_DIRS, is_secret

MAX_BYTES = 2 * 1024**3  # 2 GB of files at most: past that, a zip is the wrong way to move a project
NOTE = "DELTA_EXPORT.txt"


class ExportError(ValueError):
    """The project cannot be exported; the message says why."""


@dataclass
class Export:
    path: Path        # the zip, in the temporary folder: the caller deletes it once sent
    name: str         # the file name to offer: <project>-<date>.zip
    files: int
    size: int         # bytes of files put in (before compression)
    left_out: list[str] = field(default_factory=list)


def safe_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "", name).strip(" .") or "project"
    return clean[:60]


def build(root: Path, project_name: str) -> Export:
    top = safe_name(project_name)
    base = os.path.realpath(root)
    skipped: dict[str, int] = {}
    secrets: list[str] = []
    entries: list[tuple[str, str]] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(base):
        keep = []
        for d in sorted(dirnames):
            full = os.path.join(dirpath, d)
            if d in SKIP_DIRS or os.path.islink(full):
                rel = os.path.relpath(full, base).replace("\\", "/")
                skipped[rel + "/"] = 1
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            full = os.path.realpath(os.path.join(dirpath, f))
            if not full.startswith(base + os.sep) or not os.path.isfile(full):
                continue  # a link out of the project, or something that is not a file
            rel = os.path.relpath(full, base).replace("\\", "/")
            if is_secret(rel):
                secrets.append(rel)
                continue
            total += os.path.getsize(full)
            if total > MAX_BYTES:
                raise ExportError("The project is larger than 2 GB without its generated folders: copy the folder "
                                  "instead, or remove large files you do not need.")
            entries.append((full, rel))
    if not entries:
        raise ExportError("The project has no files to download yet.")
    note = [f"Downloaded from Delta Build on {time.strftime('%d %B %Y, %H:%M')}.", "",
            f"{len(entries)} files are included."]
    if skipped:
        note += ["", "Left out because they can be made again (reinstall or rebuild them):",
                 *[f"  {s}" for s in sorted(skipped)],
                 "", "For example: npm install (node_modules), or pip install -r requirements.txt",
                 "(a virtual environment)."]
    if secrets:
        note += ["", "Left out because they hold secrets (keys, passwords); copy them by hand if you need them:",
                 *[f"  {s}" for s in secrets]]
    fd, tmp = tempfile.mkstemp(prefix="delta-export-", suffix=".zip")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for full, rel in entries:
                z.write(full, f"{top}/{rel}")
            z.writestr(f"{top}/{NOTE}", "\n".join(note) + "\n")
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return Export(Path(tmp), f"{top}-{time.strftime('%Y-%m-%d')}.zip", len(entries), total,
                  sorted(skipped) + secrets)
