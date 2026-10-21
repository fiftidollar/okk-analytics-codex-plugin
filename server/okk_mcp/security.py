"""OAuth security primitives with no raw credential/token persistence."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
from urllib.parse import urlparse

IDENTITY_SCOPES = frozenset({"openid", "email"})
ALLOWED_SCOPES = frozenset(
    {
        *IDENTITY_SCOPES,
        "okk.statistics.read",
        "okk.scenarios.read",
        "okk.transcripts.read",
        "okk.phones.read",
    }
)
DEFAULT_SCOPES = "email okk.phones.read okk.scenarios.read okk.statistics.read okk.transcripts.read openid"


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

    if (
        len(uri) > 1000
        or "#" in uri
        or "\\" in uri
        or any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in uri)
    ):
        raise ValueError("invalid_redirect_uri")
    parsed = urlparse(uri)
    if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid_redirect_uri")
    _serialize_redirect_host(parsed.hostname)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("invalid_redirect_uri") from None
    if port == 0:
        raise ValueError("invalid_redirect_uri")
    if parsed.scheme == "https":
        return uri
    if parsed.scheme != "http" or not is_loopback_host(parsed.hostname):
        raise ValueError("invalid_redirect_uri")
    return uri


def is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def validate_registered_redirect_uri(uri: str, allowed_uris: frozenset[str]) -> str:
    """Адрес возврата клиента: этот компьютер (RFC 8252, любой порт и путь) или точный адрес сайта.

    Регистрация открыта всем, поэтому без списка любой мог бы зарегистрировать «Codex»
    со своим адресом и получить код входа человека, который ввёл пароль на нашей странице.
    Сайту — только точный адрес: на claude.ai и chatgpt.com есть и чужие приложения.
    """

    validated = validate_redirect_uri(uri)
    if is_loopback_host(urlparse(validated).hostname or "") or validated in allowed_uris:
        return validated
    raise ValueError("invalid_redirect_uri")


def redirect_destination(uri: str, allowed_uris: frozenset[str]) -> tuple[str, bool]:
    """Куда браузер вернёт человека после входа: подпись для страницы входа и «известен ли адрес»."""

    if is_loopback_host(urlparse(uri).hostname or ""):
        return "в приложение на этом компьютере", True
    return uri, uri in allowed_uris


def redirect_origin(uri: str) -> str:
    """Return an injection-safe CSP origin for an already valid redirect URI."""

    validated = validate_redirect_uri(uri)
    parsed = urlparse(validated)
    host = _serialize_redirect_host(parsed.hostname or "")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme}://{host}{port}"


def _serialize_redirect_host(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            ascii_host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise ValueError("invalid_redirect_uri") from None
        labels = ascii_host.rstrip(".").split(".")
        if (
            not ascii_host
            or len(ascii_host) > 253
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in label)
                for label in labels
            )
        ):
            raise ValueError("invalid_redirect_uri") from None
        return ascii_host
    if address.version == 6:
        if address.scope_id is not None:
            raise ValueError("invalid_redirect_uri")
        return f"[{address.compressed}]"
    return address.compressed


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
