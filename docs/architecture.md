# Architecture

```text
Codex MCP client
  -> OAuth Authorization Code + PKCE
  -> standalone OKK Analytics gateway
       -> private OAuth PostgreSQL (clients, hashes, encrypted OKK sessions)
       -> Redis (fail-closed login throttling)
       -> existing OKK /api/v1 over HTTPS
            -> /auth/login, /auth/refresh, /auth/me
            -> existing read-only analytics GET endpoints
```

The gateway is both the OAuth authorization server and MCP resource server. It
does not share code, tables or signing secrets with the OKK backend.

The published deployment is production-only: the upstream base URL is
`https://okk-backend.akfixdev.ru/api/v1`, while the public MCP resource is
`https://okk-mcp.akfixdev.ru/mcp`.

## Login and session sequence

1. Codex dynamically registers a public OAuth client.
2. `/authorize` validates exact redirect URI, resource indicator and PKCE S256,
   then displays the gateway's login form. Each authorization flow receives a
   signed short-lived CSRF nonce inside the authorization request, so retries or
   parallel Codex login tabs cannot invalidate one another and Chrome cookie
   behavior cannot block the form POST. Older cookie-era forms are refreshed
   into the stateless format while the signed request is still within its
   ten-minute window. The login form posts only to the gateway. Because Chromium
   also applies `form-action` to a form response's redirect chain, the page CSP
   additionally permits only the origin of the validated registered callback.
   After successful login, the authorization endpoint responds with a direct
   `302` to that exact URI with `code` and the original `state`. Codex owns the
   loopback listener, token exchange and resulting authenticated MCP state.
3. The password is held only for the request and forwarded to the normal OKK
   `/auth/login`. It is never logged or persisted.
4. The OKK access and refresh tokens are authenticated-encrypted in the
   gateway's database. MCP authorization codes/access/refresh tokens are stored
   only as SHA-256 hashes.
5. Each MCP request validates the opaque MCP token, calls `/auth/me`, rotates the
   upstream refresh cookie if necessary and uses the newly returned live role
   and `department_ids`.
6. Every analytics call uses only an upstream GET route. Results pass through an
   explicit safe projection before MCP serialization.

This is deliberately not HR/SSO authorization. Users provisioned by HR still
need a usable local OKK password for `/auth/login`.

## Codex plugin contract

The community package follows the same remote-MCP declaration used by official
account plugins: `.mcp.json` contains the HTTPS MCP URL and an `oauth_resource`
that exactly matches the server's protected-resource metadata. Marketplace
policy is `ON_INSTALL`, so Codex starts OAuth while installing the plugin.

Official directory plugins may also contain an OpenAI-issued `.app.json`
connector ID. This community repository intentionally does not invent one;
that file can be added only after the connector is registered with OpenAI.

## Scaling

MCP transport is stateless Streamable HTTP. OAuth/session state is shared in
PostgreSQL; login throttles are shared in Redis. Upstream refresh rotation is
serialized with a database row lock so concurrent worker processes do not reuse
the same rotating OKK refresh token. No sticky sessions are required.

Expensive cross-employee and criterion aggregation uses bounded concurrency.
`ANALYTICS_MAX_CALLS` and `ANALYTICS_MAX_EMPLOYEES` cap a single request; a
truncated response returns `partial` rather than silently claiming completeness.
