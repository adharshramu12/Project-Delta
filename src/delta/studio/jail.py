"""Which folders may be a project, and the check that keeps every path the agent names inside its project.

A path is checked after it is fully resolved (`os.path.realpath`), so "..", a symbolic link or a junction that points
outside the project is refused like any other outside path. The model only ever sees paths relative to the
project, and every path it sends back is checked again here before anything is read or written.

A project folder must be inside one of the allowed places: the user's own folder (Documents, Desktop, Music...),
Delta's projects folder, and any folder the user adds in settings (studio.extra_folders), never a drive or a system,
program or Delta folder. Both checks are the plain "resolve, then must start with the allowed folder" form.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

# Folders never walked, listed or searched: generated, huge, or someone else's (they stay readable by name).
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", "dist", "build", ".next", ".nuxt", ".turbo", ".cache", "target", "bin", "obj", ".idea",
             ".vs", "coverage", ".gradle", ".dart_tool", "Pods"}
# Files with secrets: never read or written by the agent, never shown to the model (checked by name, see is_secret).
SECRET_NAMES = {".env", ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json", "id_rsa", "id_dsa",
                "id_ecdsa", "id_ed25519", "id_rsa.pub", "id_dsa.pub", "id_ecdsa.pub", "id_ed25519.pub",
                "secret.json", "secrets.json", "secret.yml", "secrets.yml", "secret.yaml", "secrets.yaml",
                "secret.toml", "secrets.toml"}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")
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


def _real(path: str | Path) -> str:
    """The fully resolved path. On Windows realpath also gives the folders' real letter case, so two spellings of
    one folder compare equal; the case of a name that does not exist yet (a new file) is kept as written."""
    return os.path.realpath(path)


def _within(real: str, base: str) -> bool:
    """`real` is `base` or inside it (both already resolved with _real)."""
    return real == base or real.startswith(base.rstrip("\\/") + os.sep)


def check_project_root(folder: str | Path, data_dir: Path, app_root: Path,
                       allowed: Iterable[Path] | None = None) -> Path:
    """The resolved project folder, or JailError. Inside an allowed place (by default the user's own folder), not
    a drive, not the home folder itself, not a system or program folder, not Delta's own folders, and not a folder
    that contains one of those."""
    raw = str(folder or "").strip().strip('"')
    if not raw or "\x00" in raw:
        raise JailError("Choose a folder for the project.")
    real = _real(os.path.expandvars(os.path.expanduser(raw)))
    bases = [_real(b) for b in (allowed if allowed is not None else [Path.home()])]
    if not any(_within(real, b) for b in bases):
        raise JailError(f"{real} is outside the folders Delta Build may use. Keep projects in your own folder "
                        "(Documents, Desktop...), or add the folder in Settings (studio.extra_folders).")
    root = Path(real)
    if not root.is_dir():
        raise JailError(f"{root} is not a folder on this PC.")
    if root.parent == root or real in bases or root == Path.home().resolve():
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
    real = _real(os.path.join(root, text))
    if not _within(real, _real(root)):
        raise JailError(f"{rel} is outside the project. Only files inside the project folder can be used.")
    return Path(real) if real != _real(root) else root


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix() if path != root else "."


def is_secret(rel: str) -> bool:
    name = rel.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return (name in SECRET_NAMES or name.startswith(".env.") or name.endswith(SECRET_SUFFIXES)
            or (name.startswith(("service-account", "service_account")) and name.endswith(".json")))


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
