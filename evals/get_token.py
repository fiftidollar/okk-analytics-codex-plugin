#!/usr/bin/env python3
"""Sign in to an OKK Analytics gateway through its real OAuth flow and save the MCP tokens.

    OKK_EVAL_EMAIL=... OKK_EVAL_PASSWORD=... python evals/get_token.py --gateway-url URL --out FILE

Flow: dynamic client registration -> /authorize page -> login form (every optional access box left
checked, as a browser submits it) -> code -> /token with PKCE. The email and password are read from
the environment and are never printed; FILE gets access_token, refresh_token and client_id, and
evals/run.py --token-file refreshes it. Keep FILE in evals/out/ (ignored by Git).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

REDIRECT_URI = "http://127.0.0.1:47999/callback"  # loopback: never opened, the code is read from Location


def form_fields(page: str) -> dict[str, str]:
    """Hidden inputs and checked boxes of the login page, as a browser would submit them."""

    fields: dict[str, str] = {}
    for tag in re.findall(r"<input[^>]*>", page):
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        kind = re.search(r'type="([^"]+)"', tag)
        if not name or not kind:
            continue
        if kind.group(1) == "hidden" or (kind.group(1) == "checkbox" and " checked" in tag):
            fields[name.group(1)] = html.unescape(value.group(1)) if value else ""
    return fields


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--gateway-url", required=True, help="the gateway MCP URL, e.g. http://localhost:8020/mcp"
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    email, password = os.environ.get("OKK_EVAL_EMAIL"), os.environ.get("OKK_EVAL_PASSWORD")
    if not email or not password:
        raise SystemExit(
            "Set OKK_EVAL_EMAIL and OKK_EVAL_PASSWORD in the environment (never on the command line)."
        )
    base = args.gateway_url.rstrip("/").removesuffix("/mcp")
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        registration = client.post(
            f"{base}/register", json={"client_name": "OKK eval suite", "redirect_uris": [REDIRECT_URI]}
        )
        if registration.status_code != 201:
            raise SystemExit(f"registration failed: HTTP {registration.status_code}")
        client_id = registration.json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": secrets.token_urlsafe(8),
        }
        page = client.get(f"{base}/authorize", params=query)
        login = client.post(
            f"{base}/authorize", data={**form_fields(page.text), "email": email, "password": password}
        )
        code = parse_qs(urlsplit(login.headers.get("location", "")).query).get("code", [None])[0]
        if login.status_code != 302 or not code:
            raise SystemExit(
                f"sign-in failed: HTTP {login.status_code} (check the account and the login page)"
            )
        tokens = client.post(
            f"{base}/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "code_verifier": verifier,
            },
        )
        if tokens.status_code != 200:
            raise SystemExit(f"token exchange failed: HTTP {tokens.status_code}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"client_id": client_id, **tokens.json(), "obtained_at": int(time.time() * 1000)}
    args.out.write_text(json.dumps(payload), encoding="utf-8")
    print(f"signed in; scope: {payload.get('scope')}; tokens saved to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
