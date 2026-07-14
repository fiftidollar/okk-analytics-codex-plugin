"""OAuth security primitives with no raw credential/token persistence."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
from urllib.parse import urlparse

ALLOWED_SCOPES = frozenset({"okk.statistics.read", "okk.scenarios.read"})
DEFAULT_SCOPES = "okk.statistics.read okk.scenarios.read"


def token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def random_token() -> str:
    return secrets.token_urlsafe(64)


def validate_scopes(scope: str | None) -> str:
    requested = set((scope or DEFAULT_SCOPES).split())
    if not requested or not requested.issubset(ALLOWED_SCOPES):
        raise ValueError("invalid_scope")
    return " ".join(sorted(requested))


def validate_redirect_uri(uri: str) -> str:
    """Allow HTTPS and RFC 8252 loopback HTTP redirect URIs only."""

    if len(uri) > 1000 or "#" in uri:
        raise ValueError("invalid_redirect_uri")
    parsed = urlparse(uri)
    if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid_redirect_uri")
    if parsed.scheme == "https":
        return uri
    if parsed.scheme != "http":
        raise ValueError("invalid_redirect_uri")
    try:
        address = ipaddress.ip_address(parsed.hostname)
        is_loopback = address.is_loopback
    except ValueError:
        is_loopback = parsed.hostname.lower() == "localhost"
    if not is_loopback:
        raise ValueError("invalid_redirect_uri")
    return uri


def verify_pkce(verifier: str, challenge: str) -> bool:
    if not 43 <= len(verifier) <= 128:
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    calculated = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return hmac.compare_digest(calculated, challenge)


def valid_pkce_challenge(challenge: str, method: str) -> bool:
    if method != "S256" or not 43 <= len(challenge) <= 128:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
    return all(char in allowed for char in challenge)
