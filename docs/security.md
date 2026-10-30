# Security contract

Report interpretation rules supplement server-side ACL and roster enforcement;
they do not guarantee arbitrary client-model prose. A successful browser example
does not establish the cause of a previous hallucination or universal accuracy.
Missing employee matches remain neutral; external uploads cannot establish OKK
membership or replace unavailable OKK scores.

## Credentials and tokens

- Neither Codex nor the gateway receives the OKK password: the person signs in
  on the OKK page; the gateway exchanges a one-time OKK code (60 s, single use,
  bound to the gateway's exact callback and PKCE) with `OKK_MCP_CLIENT_SECRET`.
  Production refuses to start with the development value of that secret.
- Every request to OKK carries `X-OKK-Client: codex-mcp-readonly`. OKK refuses
  it (403) while the company's MCP module is off, so switching the module off
  closes connections that are already open; switching it on opens them again.
- Consent submissions are limited per client address
  (`MCP_LOGIN_ATTEMPTS_PER_MINUTE`, fail-closed when Redis is down): each one
  keeps a pending sign-in in Redis until OKK returns.
- MCP authorization codes/access/refresh tokens are opaque; only SHA-256 hashes
  are stored.
- Upstream OKK access and rotating refresh tokens are encrypted with Fernet
  using `MCP_SESSION_ENCRYPTION_SECRET`.
- `MCP_OAUTH_SECRET`, `MCP_SESSION_ENCRYPTION_SECRET`, database passwords and
  Redis credentials belong only in the deployment secret store.
- Refresh-token reuse revokes the whole MCP family and the upstream OKK session.
- A second use of an authorization code is rejected and revokes every token
  that code issued, refreshed ones included, and its OKK session (RFC 6749
  §4.1.2). Each sign-in has its own OKK session, so the person's other
  connections keep working.
- The outbound OKK HTTP client is shared by everyone and stores no cookies.
  An OKK refresh token travels only as an explicit `Cookie` header on
  `/auth/refresh` and `/auth/logout` of its own session.
- A background cleanup revokes orphaned upstream sessions and deletes expired
  OAuth rows only after the configured audit-retention window.

## ACL behavior

- Admin: all departments returned by the live account.
- Viewer: only live `department_ids` and entities underneath them.
- Private supervisors: only rows returned by the live
  `/employees/restricted` endpoint for this exact user. Admin is not a bypass;
  explicit upstream grants are required.
- The gateway intersects `/departments` with the viewer's revalidated live
  `department_ids` as defense in depth even though the upstream route is also
  ACL-aware.
- Viewer with an empty ACL: successful empty/no-data responses, never 500/403
  noise.
- Direct inaccessible or missing IDs: identical `not_available` response
  (OKK 403, 404, or 409 for a closed restricted employee).
- Exact department names/codes are resolved only inside the live visible ACL.
  A failed or ambiguous named selector returns `not_available` and is never
  treated as an absent filter.
- When employee and department filters are combined, a cross-department
  mismatch returns `not_available` before statistics endpoints are called.
- Department employee directories are intersected again with the live visible
  department catalog even if the upstream `/employees` route claims it applied
  the filter. Department dashboards, rankings, trends and plan/fact rows are
  then intersected by ID with that canonical roster. Foreign rows are dropped;
  conflicting employee names are replaced by the employee-directory value and
  counted in `employee_roster_grounding`.
- Mixed ID filters: accessible rows plus only an omitted count.
- Deactivation/role/department changes take effect on the next MCP request via
  `/auth/me`.
- Supervisor grant changes take effect on the next supervisor request. A
  supervisor UUID is checked against the private catalog before `/calls` is
  queried, and a department-less direct call requires the same match before its
  transcript is fetched.

## Data minimization

Safe projections exclude email, password/PBX fields, structured phone numbers,
audio URLs, raw prompts, prompt runtime, raw reasoning, scripts, Megafon,
routing and pipeline state. Nothing from an analytics response is persisted.
The single phone exception is `list_call_phone_records`, gated by both
`okk.statistics.read` and `okk.phones.read`. It returns only the client-side
normalized number from calls in the caller's live department or explicit
supervisor scope. The upstream indexed exact filter supplies the count. A
gateway guard rejects a platform response that ignores the filter.
The client touch-cycle extension follows the same rule: the client-statistics
tool may expose aggregate counts and the reset-window metadata, but never a
phone, per-number transition row or hidden manager/client identity.
Call plans expose plan items, plan and fact only; the plan edit history and
its authors are dropped before the response leaves the gateway.

Transcript content is a narrowly scoped exception. Only the six dedicated
ordinary/supervisor transcript tools may serialize it, and all require both
`okk.transcripts.read` and the statistics scope. The gateway first applies the
normal upstream call ACL, then validates the call's employee/department against
the fresh live department or restricted-supervisor catalog. It returns no
phone/audio/external-call fields, never
persists or caches transcript bodies, and never includes search text, excerpts
or transcript payloads in operational traces. Transcript search sends the
words to OKK only in the JSON body of the read-only
`POST /calls/transcript-search` route, never in a URL, so they stay out of
access logs; OKK applies the same journal ACL and the gateway re-checks every
returned call.

Operational observability is deliberately metadata-only. Structured traces
record filter presence/counts, timing, status and safe completeness markers,
but never raw selectors, UUIDs, names or response payloads.

The grounding allowlist is returned to the requesting MCP client as business
data but is never copied into gateway logs. Its purpose is both enforcement and
model guidance: a department report may name only people whose IDs appear in
that response's live roster.

Criterion aggregation currently has to read the existing OKK call-detail
response because the platform does not yet expose a criteria-only endpoint. The
gateway immediately selects evaluation item IDs/scores in memory and never
serializes transcript text through a non-transcript tool or stores
transcript/reasoning/audio fields. A future platform
criteria-only GET endpoint should replace this compatibility path.

## OAuth requirements

- Authorization Code only, PKCE S256 required. A `code_verifier` outside
  RFC 7636 (43–128 characters of `A–Z a–z 0–9 - . _ ~`) is `invalid_grant`.
- Exact registered redirect URI matching.
- HTTPS redirects, except RFC 8252 loopback HTTP clients.
- Registration is open (DCR), so a client may use only loopback return
  addresses (Codex, Claude Code; any port and path, RFC 8252) or one of the
  exact HTTPS callbacks in `MCP_OAUTH_REDIRECT_URIS` (default: Claude
  `https://claude.ai/api/mcp/auth_callback` and `https://claude.com/api/mcp/auth_callback`,
  the ChatGPT callback of our app and ChatGPT's stable
  `https://chatgpt.com/connector_platform_oauth_redirect`, which ChatGPT uses
  only with servers that return `iss`). Exact addresses, not hosts: claude.ai
  and chatgpt.com also host other people's apps. Without this, anyone could
  register a client named «Codex» with their own return address, send a user
  the gateway's genuine login link and receive that user's authorization code.
  The rule is checked at registration and again at `/authorize`, so clients
  registered under an older, wider rule are bound by it too. The login page
  shows the exact return address (or «this computer» for loopback).
- Client ID Metadata Documents (CIMD, advertised as
  `client_id_metadata_document_supported`; `MCP_CLIENT_METADATA_DOCUMENTS_ENABLED`
  switches it off): a client may use the HTTPS URL of its own metadata as
  `client_id` instead of registering. The URL must be plain HTTPS with a path,
  no query, fragment, credentials or dot segments, at most 200 characters.
  The gateway resolves the host once, refuses the fetch if any answer is not
  a public address (private, loopback, link-local, CGNAT, multicast, reserved,
  IPv4-mapped variants), then connects to that vetted address with the
  original name for TLS, so a second DNS answer cannot redirect it. No
  redirects, no environment proxy, 5 s total, at most 5 KB, cached per URL for
  5 minutes. The document must repeat the URL as `client_id`, have no
  `client_secret`, allow `none` at the token endpoint (ChatGPT names
  `private_key_jwt` and lists `none` as supported; it then uses `none`) and
  only the code and refresh grants. Its `redirect_uris` only narrow: the
  requested callback must be listed there and pass the same rule as a
  registered client, so a document can never return a code to an address DCR
  would refuse. The login page names the client as «name (document host)».
  The client's row in `oauth_clients` is written only when a person signs in,
  so an unauthenticated document creates nothing; `/token` and `/revoke`
  then accept the URL as `client_id`. Dynamic registration keeps working.
- Registration is limited per client IP (`MCP_REGISTRATIONS_PER_HOUR`,
  default 20, fail-closed on Redis errors). Addresses inside
  `MCP_TRUSTED_CLIENT_NETWORKS` (default `160.79.104.0/21`, Anthropic's egress:
  claude.ai registers a new client per connection, all from that network)
  share one bucket per network with `MCP_TRUSTED_NETWORK_REGISTRATIONS_PER_HOUR`
  (default 600). The address is uvicorn's `request.client`, derived from
  forwarded headers only of `--forwarded-allow-ips` proxies; the gateway never
  reads `X-Forwarded-For` itself. If the ingress did not overwrite that header,
  anyone could already evade the per-IP limit with a fresh fake address, so the
  trusted bucket adds no new bypass. A registration that never completed a
  sign-in is deleted by maintenance after one day.
- Refresh tokens rotate. A refresh repeated within
  `MCP_REFRESH_RETRY_SECONDS` (default 30) while the issued refresh token is
  still unused gets the same new pair: the client lost the answer, or two of
  its processes refreshed at once. The pair is kept encrypted for that window
  and cleared by maintenance. Any other reuse revokes the whole token family
  and the OKK session.
- An OKK outage is not an invalid token: the MCP request goes on and the tool
  answers with an error saying OKK is temporarily unavailable. A 401 would send
  the client back to sign in.
- An OKK access token that expires during a tool call is renewed once through
  the same serialized rotation and the read is repeated. Only a second 401
  ends the call with the reconnect error.
- The credential form submits only to the gateway. Its `form-action` CSP also
  includes the injection-safe origin of the exact validated callback because
  Chromium enforces the directive across the final redirect. No wildcard or
  user-controlled CSP source is allowed.
- A successful authorization POST returns a no-store `302` directly to the
  registered URI. Codex validates `state`, exchanges the code and records the
  authenticated MCP state.
- Every redirect from `/authorize` back to the client — the code and errors
  (`unsupported_response_type`, `invalid_request` for PKCE, `invalid_scope`,
  `invalid_target`) — carries `state` and `iss` equal to the metadata `issuer`
  (RFC 9207, `authorization_response_iss_parameter_supported`), so the client
  can detect a mix-up attack. An unknown client or return address still gets
  a page, never a redirect.
- The redirect itself is never presented as proof of a completed login. The
  definitive check is an authenticated `get_access_context` call: token
  verification reloads `/auth/me`, and only then may the response contain
  `authenticated=true`, the current role and visible departments. No token,
  password or upstream session identifier is included in that confirmation.
- A bare or refreshed `/authorize` URL without a complete PKCE request renders
  a no-store recovery page and tells the user to restart authentication from
  Codex; it does not manufacture an authorization session.
- Exact MCP resource indicator.
- Transcript access is a separate `okk.transcripts.read` grant. Existing access
  and refresh tokens retain their original scope and require a fresh OAuth
  authorization before transcript tools become available.
- Phone access is a separate `okk.phones.read` grant. Older tokens require a
  fresh authorization; refresh cannot add it silently.
- The login page lists transcripts and client phone numbers as checkboxes,
  checked by default (1.4.0). An unchecked box removes that scope from the
  authorization code, so the person decides how much the client gets. A form
  of the previous page version (no `consent_version`) grants what was
  requested. A page shown again after a refreshed form keeps the boxes as the
  person left them.
- Step-up for a missing scope: the tool call ends with `isError: true`, a text
  naming the scope and telling the person to reconnect (what Codex and Claude
  Code show), and `_meta["mcp/www_authenticate"]` with
  `Bearer error="insufficient_scope", scope="…", resource_metadata="…",
  error_description="…"` (OpenAI Apps SDK; ChatGPT opens a new sign-in). The
  `scope` is the granted scopes plus the missing ones, so stepping up never
  loses access already given; the login page shows the requested optional
  scopes checked. The MCP HTTP 403 `insufficient_scope` is not used: the SDK
  runs the tool inside the JSON-RPC session after the HTTP status is fixed
  (200 in stateless JSON mode), and a 403 would need a second copy of each
  tool's scope rule in front of the SDK.
- The MCP 401 challenge carries `scope` (the scopes to request) and
  `resource_metadata`. Without a token it has no error code (RFC 6750); with an
  invalid or expired token it keeps `error="invalid_token"`, on which clients
  refresh.
- Public clients only (`token_endpoint_auth_method=none`), registered or CIMD.
- OAuth metadata advertises `openid`, `email` and `/userinfo`. The UserInfo
  response revalidates the live OKK session and returns only the account
  subject, normalized email and `email_verified=true`; no role, department,
  upstream token or internal session identifier is returned.
- The OpenAI submission-domain token is loaded only from
  `OPENAI_APPS_CHALLENGE_TOKEN` and returned verbatim from the dedicated
  well-known route. It is not committed, logged or reused as an application
  secret.
- Dynamic registration never serializes absent optional URI metadata as JSON
  `null`; fields such as `client_uri` are omitted unless they contain a
  validated value. This keeps strict Codex/Claude Code OAuth clients on the
  same public PKCE contract without inventing client metadata.
- Flow-scoped CSRF binding, signed ten-minute authorization request and
  fail-closed Redis login throttling. Parallel OAuth pages use a per-form nonce
  carried inside the signed authorization request, so the submit path does not
  depend on browser cookies. Missing/tampered nonces are recovered only by
  re-rendering the same still-valid signed authorization request with a fresh
  nonce; invalid or expired signed requests still fail closed.
