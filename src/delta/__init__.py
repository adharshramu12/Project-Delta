"""Delta - a personal, local-first, OS-level AI agent, created by Adharsh Kumar Bachu."""


def _version() -> str:
    """The version comes from one place, pyproject.toml. Installed (the normal case): the package metadata.
    Run from a source folder that was never installed: read pyproject.toml next to the package."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("delta-agent")
    except PackageNotFoundError:
        pass
    try:
        import tomllib
        from pathlib import Path

        with (Path(__file__).resolve().parents[2] / "pyproject.toml").open("rb") as f:
            return str(tomllib.load(f)["project"]["version"])
    except (OSError, KeyError, ValueError):
        return "0.0.0+unknown"


__version__ = _version()
# Who built Delta lives in one file, delta/identity.py, so a fork changes it in one place.
from delta.identity import CREATOR, credit_prompt  # noqa: E402, F401  (CREATOR is re-exported)

CREDIT = credit_prompt()
