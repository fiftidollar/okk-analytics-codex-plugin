"""Client ID Metadata Documents: what a document may and may not do, and where we connect."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from okk_mcp import client_metadata, oauth
from okk_mcp.client_metadata import ClientMetadataError, load_client_metadata
from okk_mcp.config import Settings

CLIENT_ID = "https://chatgpt.com/oauth/client.json"
STABLE_CHATGPT = "https://chatgpt.com/connector_platform_oauth_redirect"
PUBLIC_ADDRESS = "104.18.32.47"
# ChatGPT's published document (2026-10-27), shortened.
CHATGPT_DOCUMENT = {
    "client_id": CLIENT_ID,
    "client_uri": "https://chatgpt.com/",
    "redirect_uris": [STABLE_CHATGPT],
    "token_endpoint_auth_method": "private_key_jwt",
    "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "client_name": "ChatGPT",
}


@pytest.fixture(autouse=True)
def network(monkeypatch):
    """A fake DNS and web server. Records where the gateway connected."""

    state = SimpleNamespace(addresses=[PUBLIC_ADDRESS], response=httpx.Response(200, json=CHATGPT_DOCUMENT))
    state.requests = []

    async def resolve(host, port):
        return list(state.addresses)

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        return state.response

    monkeypatch.setattr(client_metadata, "_resolve", resolve)
    monkeypatch.setattr(client_metadata, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(client_metadata, "_cache", {})
    return state


def _load(client_id: str = CLIENT_ID):
    return asyncio.run(load_client_metadata(client_id))


def test_document_is_fetched_from_the_vetted_address_with_the_name_kept_for_tls(network):
    metadata = _load()
    request = network.requests[0]

    assert metadata.redirect_uris == (STABLE_CHATGPT,)
    assert metadata.client_name == "ChatGPT"
    # Connect to the address that was checked, not to a name a second DNS answer could redirect.
    assert request.url.host == PUBLIC_ADDRESS
    assert request.headers["host"] == "chatgpt.com"
    assert request.extensions["sni_hostname"] == "chatgpt.com"
    # Cached: a second authorization does not fetch again.
    _load()
    assert len(network.requests) == 1


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.5"],
        ["192.168.1.10"],
        ["169.254.169.254"],  # cloud metadata
        ["100.64.0.1"],  # carrier-grade NAT
        ["::1"],
        ["fd00::1"],
        ["fe80::1%eth0"],
        ["::ffff:10.0.0.1"],
        # One internal answer is enough: the name must not choose which network we reach.
        [PUBLIC_ADDRESS, "10.0.0.5"],
    ],
)
def test_client_id_resolving_to_an_internal_address_is_never_fetched(network, addresses):
    network.addresses = addresses
    with pytest.raises(ClientMetadataError, match="non_public_address"):
        _load()
    assert network.requests == []


@pytest.mark.parametrize(
    "client_id",
    [
        "http://chatgpt.com/oauth/client.json",
        "https://chatgpt.com",
        "https://chatgpt.com/",
        "https://chatgpt.com/oauth/client.json?x=1",
        "https://chatgpt.com/oauth/client.json#x",
        "https://chatgpt.com/oauth/../client.json",
        "https://user@chatgpt.com/oauth/client.json",
        "https://chatgpt.com/" + "a" * 200,
    ],
)
def test_client_id_must_be_a_plain_https_url_with_a_path(network, client_id):
    with pytest.raises(ClientMetadataError, match="invalid_client_id_url"):
        _load(client_id)
    assert network.requests == []


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (httpx.Response(302, headers={"location": "http://10.0.0.5/doc"}), "http_302"),
        (httpx.Response(404), "http_404"),
        (httpx.Response(200, content=b"<html>challenge</html>"), "invalid_json"),
        (httpx.Response(200, json={**CHATGPT_DOCUMENT, "client_id": "https://evil.example/doc"}), "mismatch"),
        (httpx.Response(200, json={**CHATGPT_DOCUMENT, "client_secret": "s"}), "client_secret"),
        (httpx.Response(200, json={**CHATGPT_DOCUMENT, "redirect_uris": []}), "invalid_redirect_uris"),
        (
            httpx.Response(
                200,
                json={**CHATGPT_DOCUMENT, "token_endpoint_auth_methods_supported": ["private_key_jwt"]},
            ),
            "unsupported_token_endpoint_auth_method",
        ),
        (
            httpx.Response(200, json={**CHATGPT_DOCUMENT, "grant_types": ["client_credentials"]}),
            "unsupported_oauth_flow",
        ),
        (
            httpx.Response(200, content=json.dumps({**CHATGPT_DOCUMENT, "pad": "x" * 6000}).encode()),
            "document_too_large",
        ),
    ],
)
def test_unusable_document_identifies_no_client(network, response, reason):
    network.response = response
    with pytest.raises(ClientMetadataError, match=reason):
        _load()


def _load_client(redirect_uri: str, settings: Settings | None = None):
    db = SimpleNamespace(get=AsyncMock(return_value=None))
    return asyncio.run(oauth._load_client(db, CLIENT_ID, redirect_uri, settings or Settings()))


def test_document_redirects_pass_through_our_redirect_rule(network):
    network.response = httpx.Response(
        200, json={**CHATGPT_DOCUMENT, "redirect_uris": [STABLE_CHATGPT, "https://evil.example/cb"]}
    )

    client = _load_client(STABLE_CHATGPT)
    assert client.client_name == "ChatGPT (chatgpt.com)"
    assert client.token_endpoint_auth_method == "none"
    # Listed in the document but not allowed for a registered client either: the document widens nothing.
    with pytest.raises(HTTPException):
        _load_client("https://evil.example/cb")
    # Allowed by us, but the client did not list it.
    with pytest.raises(HTTPException):
        _load_client("https://claude.ai/api/mcp/auth_callback")


def test_metadata_documents_can_be_switched_off(network):
    settings = Settings(mcp_client_metadata_documents_enabled=False)
    with pytest.raises(HTTPException):
        _load_client(STABLE_CHATGPT, settings)
    assert network.requests == []
    assert oauth._authorization_metadata(settings)["client_id_metadata_document_supported"] is False
