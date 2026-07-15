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
- Viewer with an empty ACL: successful empty/no-data responses, never 500/403
  noise.
- Direct inaccessible or missing IDs: identical `not_available` response.
- Mixed ID filters: accessible rows plus only an omitted count.
- Deactivation/role/department changes take effect on the next MCP request via
  `/auth/me`.

## Data minimization

Safe projections exclude email, password/PBX fields, phone numbers, audio URLs,
transcripts, raw prompts, prompt runtime, raw reasoning, scripts, Megafon,
routing and pipeline state. Nothing from an analytics response is persisted.

Criterion aggregation currently has to read the existing OKK call-detail
response because the platform does not yet expose a criteria-only endpoint. The
gateway immediately selects evaluation item IDs/scores in memory and never
serializes or stores transcript/reasoning/audio fields. A future platform
criteria-only GET endpoint should replace this compatibility path.

## OAuth requirements

- Authorization Code only, PKCE S256 required.
- Exact registered redirect URI matching.
- HTTPS redirects, except RFC 8252 loopback HTTP clients.
- Exact MCP resource indicator.
- Public clients only (`token_endpoint_auth_method=none`).
- Flow-scoped CSRF binding, signed ten-minute authorization request and
  fail-closed Redis login throttling. Parallel OAuth pages use different cookie
  names derived from their signed authorization request and per-form token.
