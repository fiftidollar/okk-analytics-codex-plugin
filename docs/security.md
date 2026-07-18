# Security contract

## Credentials and tokens

- Codex never receives the OKK password.
- The gateway accepts it only on the authorization form, forwards it to
  `/auth/login` and discards it before responding.
- MCP authorization codes/access/refresh tokens are opaque; only SHA-256 hashes
  are stored.
- Upstream OKK access and rotating refresh tokens are encrypted with Fernet
  using `MCP_SESSION_ENCRYPTION_SECRET`.
- `MCP_OAUTH_SECRET`, `MCP_SESSION_ENCRYPTION_SECRET`, database passwords and
  Redis credentials belong only in the deployment secret store.
- Refresh-token reuse revokes the whole MCP family and the upstream OKK session.
- A background cleanup revokes orphaned upstream sessions and deletes expired
  OAuth rows only after the configured audit-retention window.

## ACL behavior

- Admin: all departments returned by the live account.
- Viewer: only live `department_ids` and entities underneath them.
- The gateway intersects `/departments` with the viewer's revalidated live
  `department_ids` as defense in depth even though the upstream route is also
  ACL-aware.
- Viewer with an empty ACL: successful empty/no-data responses, never 500/403
  noise.
- Direct inaccessible or missing IDs: identical `not_available` response.
- Exact department names/codes are resolved only inside the live visible ACL.
  A failed or ambiguous named selector returns `not_available` and is never
  treated as an absent filter.
- When employee and department filters are combined, a cross-department
  mismatch returns `not_available` before statistics endpoints are called.
- Mixed ID filters: accessible rows plus only an omitted count.
- Deactivation/role/department changes take effect on the next MCP request via
  `/auth/me`.

## Data minimization

Safe projections exclude email, password/PBX fields, phone numbers, audio URLs,
transcripts, raw prompts, prompt runtime, raw reasoning, scripts, Megafon,
routing and pipeline state. Nothing from an analytics response is persisted.

Operational observability is deliberately metadata-only. Structured traces
record filter presence/counts, timing, status and safe completeness markers,
but never raw selectors, UUIDs, names or response payloads.

Criterion aggregation currently has to read the existing OKK call-detail
response because the platform does not yet expose a criteria-only endpoint. The
gateway immediately selects evaluation item IDs/scores in memory and never
serializes or stores transcript/reasoning/audio fields. A future platform
criteria-only GET endpoint should replace this compatibility path.

## OAuth requirements

- Authorization Code only, PKCE S256 required.
- Exact registered redirect URI matching.
- HTTPS redirects, except RFC 8252 loopback HTTP clients.
- The credential form submits only to the gateway. Its `form-action` CSP also
  includes the injection-safe origin of the exact validated callback because
  Chromium enforces the directive across the final redirect. No wildcard or
  user-controlled CSP source is allowed.
- A successful authorization POST returns a no-store `302` directly to the
  registered URI. Codex validates `state`, exchanges the code and records the
  authenticated MCP state.
- The redirect itself is never presented as proof of a completed login. The
  definitive check is an authenticated `get_access_context` call: token
  verification reloads `/auth/me`, and only then may the response contain
  `authenticated=true`, the current role and visible departments. No token,
  password or upstream session identifier is included in that confirmation.
- A bare or refreshed `/authorize` URL without a complete PKCE request renders
  a no-store recovery page and tells the user to restart authentication from
  Codex; it does not manufacture an authorization session.
- Exact MCP resource indicator.
- Public clients only (`token_endpoint_auth_method=none`).
- Flow-scoped CSRF binding, signed ten-minute authorization request and
  fail-closed Redis login throttling. Parallel OAuth pages use a per-form nonce
  carried inside the signed authorization request, so the submit path does not
  depend on browser cookies. Missing/tampered nonces are recovered only by
  re-rendering the same still-valid signed authorization request with a fresh
  nonce; invalid or expired signed requests still fail closed.
