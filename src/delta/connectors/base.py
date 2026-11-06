"""Errors and the token record shared by the connector modules. No network libraries here, so importing
delta.connectors at start-up stays cheap (httpx loads on the first connection)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any


class McpError(Exception):
    def __init__(self, kind: str, message: str, *, www_authenticate: str = "", status: int = 0,
                 retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.www_authenticate = www_authenticate
        self.status = status
        self.retry_after_s = retry_after_s


class OAuthError(RuntimeError):
    pass


@dataclass
class TokenSet:
    access_token: str
    token_type: str = "Bearer"
    refresh_token: str = ""
    expires_at: float = 0.0  # unix time; 0 = unknown
    scope: str = ""
    client_id: str = ""
    client_secret: str = ""
    auth_method: str = "none"
    token_endpoint: str = ""
    revocation_endpoint: str = ""
    resource: str = ""
    issuer: str = ""

    def expired(self, skew_s: float = 60.0, now: float | None = None) -> bool:
        return bool(self.expires_at) and (now or time.time()) >= self.expires_at - skew_s

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TokenSet:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)
