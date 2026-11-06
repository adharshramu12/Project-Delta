"""The environment Delta gives the programs it starts for the agent (run_shell, run_python).

Delta's own process may hold secrets in environment variables: `DELTA_<NAME>` overrides of stored keys
(system/secrets.py) and whatever API keys the user exported for other tools. A command or script the
model writes must not be able to read them, so child processes get a copy of the environment with every
variable that looks like a secret removed. Everything else (PATH, SystemRoot, TEMP, USERPROFILE, proxies)
is kept, so ordinary commands work exactly as before.

A name the user really wants passed through goes in `agent.child_env_allow` (exact names, case-insensitive),
for example a token a build script needs. MCP servers are already started by the MCP SDK with only a short
safe list of variables (PATH, APPDATA, USERPROFILE and similar) plus the server's own `env:` from the config,
which is the user's explicit choice."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

# Words that mark a variable as secret. Matched on name parts, so OPENAI_API_KEY, GITHUB_TOKEN, AWS_SECRET_ACCESS_KEY,
# DB_PASSWORD, AZURE_CLIENT_SECRET, NPM_AUTH_TOKEN and HF_TOKEN all go, while PATH, PATHEXT and KEYBOARD_LAYOUT stay.
_SECRET_PARTS = re.compile(
    r"(^|_)(API_?KEY|APIKEY|ACCESS_?KEY|PRIVATE_?KEY|SECRET|SECRETS|TOKEN|TOKENS|PASSWORD|PASSWD|PASSPHRASE|PWD|"
    r"CREDENTIAL|CREDENTIALS|AUTH|COOKIE|SESSION_?KEY|SIGNING_?KEY|CLIENT_?SECRET|CONNECTION_?STRING|DSN|KEY)(_|$)",
    re.I,
)
# Names that match the words above but are not secrets and that common tools need.
_KEEP = frozenset({"SSH_AUTH_SOCK", "PWD", "OLDPWD"})
# Delta's own settings that are safe to pass (paths and switches, never secrets).
_DELTA_SAFE = frozenset({"DELTA_DATA_DIR", "DELTA_CONFIG_DIR", "DELTA_E2E"})

_allow: frozenset[str] = frozenset()


def configure(allow: Iterable[str] = ()) -> None:
    """Called once at start-up with agent.child_env_allow."""
    global _allow
    _allow = frozenset(a.strip().upper() for a in allow if a and a.strip())


def is_secret_name(name: str) -> bool:
    upper = name.upper()
    if upper in _KEEP:
        return False
    if upper.startswith("DELTA_"):
        return upper not in _DELTA_SAFE  # DELTA_<NAME> overrides hold Delta's own keys
    return bool(_SECRET_PARTS.search(upper))


def scrubbed_env(extra: Mapping[str, str] | None = None, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment (or `base`) without secret-looking variables, plus `extra` on top."""
    src = os.environ if base is None else base
    env = {k: v for k, v in src.items() if k.upper() in _allow or not is_secret_name(k)}
    if extra:
        env.update(extra)
    return env


def removed(base: Mapping[str, str] | None = None) -> list[str]:
    """Names scrubbed_env() would drop (for `delta doctor` and tests; values are never shown)."""
    src = os.environ if base is None else base
    return sorted(k for k in src if k.upper() not in _allow and is_secret_name(k))
