"""Client ID Metadata Documents: a client identified by the HTTPS URL of its own metadata.

The document only names the client and its return addresses. It grants nothing: the gateway's
own redirect rule (exact known callbacks or this computer) still applies on top of it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

import httpx

FETCH_TIMEOUT_SECONDS = 5.0
MAX_DOCUMENT_BYTES = 5 * 1024
CACHE_SECONDS = 300
CACHE_MAX_ENTRIES = 256
# Codes and tokens reference the client by `oauth_clients.client_id` (VARCHAR 200).
MAX_CLIENT_ID_LENGTH = 200

# Tests replace the network; production always goes to the vetted address.
_transport: httpx.AsyncBaseTransport | None = None
_cache: dict[str, tuple[float, ClientMetadata]] = {}


class ClientMetadataError(Exception):
    """The URL or its document cannot identify a client. The message is a short code, safe to log."""


@dataclass(frozen=True, slots=True)
class ClientMetadata:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    client_uri: str | None


def is_metadata_document_client_id(client_id: str) -> bool:
    """Registered clients get random ids; only a metadata document URL starts with a scheme."""

    return client_id.startswith("https://")


async def load_client_metadata(client_id: str) -> ClientMetadata:
    cached = _cache.get(client_id)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    url = _validated_url(client_id)
    address = await _public_address(url.hostname or "", url.port or 443)
    metadata = _parse(await _download(url, address), client_id)
    _cache.pop(client_id, None)
    if len(_cache) >= CACHE_MAX_ENTRIES:
        _cache.pop(next(iter(_cache)))
    _cache[client_id] = (time.monotonic() + CACHE_SECONDS, metadata)
    return metadata


def _validated_url(client_id: str) -> SplitResult:
    if (
        len(client_id) > MAX_CLIENT_ID_LENGTH
        or "#" in client_id
        or "\\" in client_id
        or any(
            character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in client_id
        )
    ):
        raise ClientMetadataError("invalid_client_id_url")
    url = urlsplit(client_id)
    try:
        port = url.port
    except ValueError:
        raise ClientMetadataError("invalid_client_id_url") from None
    segments = url.path.split("/")
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or port == 0
        or url.path in {"", "/"}
        or any(segment in {".", ".."} for segment in segments)
    ):
        raise ClientMetadataError("invalid_client_id_url")
    return url


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def _public_address(host: str, port: int) -> str:
    """Resolve once and connect to that address: a second lookup could answer with an internal one."""

    try:
        addresses = [ipaddress.ip_address(address.split("%")[0]) for address in await _resolve(host, port)]
    except (OSError, ValueError):
        raise ClientMetadataError("dns_failed") from None
    if not addresses:
        raise ClientMetadataError("dns_failed")
    # Every answer must be public: otherwise the name chooses which network we reach.
    if not all(_is_public(address) for address in addresses):
        raise ClientMetadataError("non_public_address")
    return str(addresses[0])


async def _download(url: SplitResult, address: str) -> bytes:
    host = url.hostname or ""
    pinned_host = f"[{address}]" if ":" in address else address
    pinned_netloc = f"{pinned_host}:{url.port}" if url.port else pinned_host
    pinned_url = urlunsplit(("https", pinned_netloc, url.path, "", ""))
    body = bytearray()
    try:
        async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
            # trust_env=False: a proxy from the environment would connect to the name, not the address.
            async with httpx.AsyncClient(
                transport=_transport,
                timeout=FETCH_TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "GET",
                    pinned_url,
                    headers={"Host": url.netloc, "Accept": "application/json"},
                    # TLS (SNI and the certificate check) still uses the name from the client id.
                    extensions={"sni_hostname": host},
                ) as response:
                    if response.status_code != 200:
                        raise ClientMetadataError(f"http_{response.status_code}")
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_DOCUMENT_BYTES:
                            raise ClientMetadataError("document_too_large")
    except TimeoutError:
        raise ClientMetadataError("timeout") from None
    except httpx.HTTPError:
        raise ClientMetadataError("fetch_failed") from None
    return bytes(body)


def _parse(raw: bytes, client_id: str) -> ClientMetadata:
    try:
        document = json.loads(raw)
    except ValueError:
        raise ClientMetadataError("invalid_json") from None
    if not isinstance(document, dict):
        raise ClientMetadataError("invalid_json")
    if document.get("client_id") != client_id:
        raise ClientMetadataError("client_id_mismatch")
    if "client_secret" in document:
        raise ClientMetadataError("client_secret_not_allowed")
    redirect_uris = document.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or not redirect_uris
        or len(redirect_uris) > 20
        or not all(isinstance(uri, str) for uri in redirect_uris)
    ):
        raise ClientMetadataError("invalid_redirect_uris")
    # Public PKCE clients only. ChatGPT names private_key_jwt and lists `none` among the methods it
    # supports; it then uses `none`, the only method in our metadata.
    methods = {document.get("token_endpoint_auth_method") or "none"}
    supported = document.get("token_endpoint_auth_methods_supported")
    if isinstance(supported, list):
        methods.update(method for method in supported if isinstance(method, str))
    if "none" not in methods:
        raise ClientMetadataError("unsupported_token_endpoint_auth_method")
    grants = document.get("grant_types", ["authorization_code"])
    responses = document.get("response_types", ["code"])
    if (
        not isinstance(grants, list)
        or not all(isinstance(grant, str) for grant in grants)
        or "authorization_code" not in grants
        or set(grants) - {"authorization_code", "refresh_token"}
        or responses != ["code"]
    ):
        raise ClientMetadataError("unsupported_oauth_flow")
    name = document.get("client_name")
    client_uri = document.get("client_uri")
    return ClientMetadata(
        client_id=client_id,
        client_name=name.strip()[:120] if isinstance(name, str) and name.strip() else "",
        redirect_uris=tuple(dict.fromkeys(redirect_uris)),
        client_uri=client_uri[:500] if isinstance(client_uri, str) and client_uri else None,
    )
