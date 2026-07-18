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
   The gateway cannot truthfully replace this client-owned callback with its
   own success page: the token exchange has not completed until Codex consumes
   the code. The login page therefore explains the handoff, and the first
   authenticated `get_access_context` call supplies the definitive chat-level
   confirmation.
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

On the first OKK request in a new task, the bundled skill calls
`get_access_context`. A successful call returns `authenticated=true`, the role
and visible departments, after which Codex explicitly says `OKK подключён`.
This is intentionally an in-chat verification, not an unsolicited message:
an MCP server cannot create a user chat turn merely because OAuth completed.

## ACL-safe selector resolution

Department UUIDs are not expected in normal user language. All scoped tools
therefore share one resolver that accepts an exact visible UUID, code or name.
It loads only the caller's live `/departments` result, normalizes case and
punctuation, requires one unambiguous match, and places the resolved ID/code/name
in `effective_scope`. Any explicit selector that does not resolve fails closed
as `not_available`; it cannot become a missing filter.

The resolver has no compiled department registry. `get_access_context` exposes
the live catalog to Codex, and each subsequent tool call creates a fresh
adapter and reloads the platform catalog before resolution. A department added,
renamed or removed in OKK therefore takes effect without changing or releasing
the plugin, subject only to the account's current ACL.

Employee filters are checked against both the live employee endpoint and the
resolved department. Comparison, scenario and criterion tools use the same
scope contract, so mixing entities from different visible departments is not
accepted merely because each entity is individually accessible.

## Operational traces

Each completed or upstream-failed analytics call emits one structured JSON log
event. It contains a request ID, pseudonymous actor hash, normalized tool path,
filter-presence/count flags, period, duration, status, omitted count, resolved
department code and completeness/count indicators. It never contains raw
request selectors, entity UUIDs, employee names, AI observations, response
payloads, passwords or tokens. `ANALYTICS_TRACE_ENABLED=false` disables these
events; responses themselves are never persisted by the gateway.

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
