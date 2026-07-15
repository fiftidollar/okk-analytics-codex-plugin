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
   then displays the gateway's login form. Each authorization flow receives an
   independent short-lived CSRF cookie, so retries or parallel Codex login tabs
   cannot invalidate one another. If the browser drops that cookie but the
   signed OAuth request is still within its ten-minute window, the gateway
   reissues the login form with a fresh CSRF cookie instead of requiring a new
   Codex connection attempt.
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

## Scaling

MCP transport is stateless Streamable HTTP. OAuth/session state is shared in
PostgreSQL; login throttles are shared in Redis. Upstream refresh rotation is
serialized with a database row lock so concurrent worker processes do not reuse
the same rotating OKK refresh token. No sticky sessions are required.

Expensive cross-employee and criterion aggregation uses bounded concurrency.
`ANALYTICS_MAX_CALLS` and `ANALYTICS_MAX_EMPLOYEES` cap a single request; a
truncated response returns `partial` rather than silently claiming completeness.
