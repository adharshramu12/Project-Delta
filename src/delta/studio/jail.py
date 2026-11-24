"""Which folders may be a project, and the check that keeps every path the agent names inside its project.

A path is checked after it is fully resolved (`Path.resolve`), so "..", a symbolic link or a junction that points
outside the project is refused like any other outside path. The model only ever sees paths relative to the
project, and every path it sends back is checked again here before anything is read or written.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Folders never walked, listed or searched: generated, huge, or someone else's (they stay readable by name).
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", "dist", "build", ".next", ".nuxt", ".turbo", ".cache", "target", "bin", "obj", ".idea",
             ".vs", "coverage", ".gradle", ".dart_tool", "Pods"}
# Files with secrets: never read or written by the agent, never shown to the model.
SECRET = re.compile(r"(^|/)(\.env(\..*)?|.*\.(pem|key|p12|pfx|keystore|jks)|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|"
                    r"\.npmrc|\.pypirc|\.netrc|credentials(\.json)?|secrets?\.(json|ya?ml|toml)|"
                    r"service[-_]account.*\.json)$", re.I)
MEMORY_FILE = ".delta/PROJECT.md"


class JailError(ValueError):
    """A path or folder the agent may not use. The message says why, in plain words."""


def forbidden_roots(data_dir: Path, app_root: Path) -> list[Path]:
    """Folders that can never be a project or contain one: Windows, the program folders, Delta itself."""
    out = [data_dir, app_root]
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA", "APPDATA"):
        if value := os.environ.get(var):
            out.append(Path(value))
    return [p.resolve() for p in out]


def check_project_root(folder: str | Path, data_dir: Path, app_root: Path) -> Path:
    """The resolved project folder, or JailError. Not a drive, not the home folder, not a system or program
    folder, not Delta's own folders, and not a folder that contains one of those."""
    raw = str(folder or "").strip().strip('"')
    if not raw:
        raise JailError("Choose a folder for the project.")
    root = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
    if not root.is_dir():
        raise JailError(f"{root} is not a folder on this PC.")
    if root.parent == root or root == Path.home().resolve():
        raise JailError("A whole drive or your home folder cannot be a project. Choose the project's own folder.")
    for bad in forbidden_roots(data_dir, app_root):
        if root == bad or bad in root.parents or root in bad.parents:
            raise JailError(f"{root} is a system, program or Delta folder (or contains one): it cannot be a project.")
    return root


def inside(root: Path, rel: str) -> Path:
    """The real path of `rel` inside the project, or JailError. Absolute paths are accepted only when they
    already point inside the project."""
    text = str(rel or "").strip().strip('"').replace("\\", "/")
    if not text or text in (".", "./"):
        return root
    if "\x00" in text:
        raise JailError("That path is not valid.")
    target = (root / text).resolve()
    if target != root and root not in target.parents:
        raise JailError(f"{rel} is outside the project. Only files inside the project folder can be used.")
    return target


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix() if path != root else "."


def is_secret(rel: str) -> bool:
    return bool(SECRET.search(rel.replace("\\", "/")))


def check_file(root: Path, rel: str) -> tuple[Path, str]:
    """(real path, project-relative path) for a file the agent may read or write."""
    path = inside(root, rel)
    rel_path = relative(root, path)
    if path == root:
        raise JailError("Name a file, not the project folder.")
    if is_secret(rel_path):
        raise JailError(f"{rel_path} holds secrets (keys, passwords, tokens): Delta Build never reads or changes it.")
    if any(part == ".git" for part in Path(rel_path).parts):
        raise JailError("The .git folder is git's own: Delta Build never changes it.")
    return path, rel_path
