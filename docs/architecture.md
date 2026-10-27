# Architecture

Production runs gateway 1.3.1 (public `/health` reported `1.3.1` on
2026-10-20) against the production OKK API of the new OKK architecture.
Release history is in [CHANGELOG.md](../CHANGELOG.md),
release evidence in [deployment.md](deployment.md).

The phone-history path uses the platform's indexed exact number filter and the
gateway's live ACL checks; OAuth requires the separate `okk.phones.read` scope.
It does not scan a bounded first page and call that a full count.

```text
Codex MCP client
  -> OAuth Authorization Code + PKCE
  -> standalone OKK Analytics gateway
       -> private OAuth PostgreSQL (clients, hashes, encrypted OKK sessions)
       -> Redis (fail-closed login throttling)
       -> existing OKK /api/v1 over HTTPS
            -> /auth/login, /auth/refresh, /auth/me
            -> existing read-only analytics GET endpoints
            -> POST /calls/phone-lookup (phone in JSON body)
            -> POST /calls/transcript-search (search words in JSON body)
            -> ACL-protected /employees/restricted
            -> ACL-protected /calls/{call_id}/transcript
```

The gateway is both the OAuth authorization server and MCP resource server. It
does not share code, tables or signing secrets with the OKK backend.

The published deployment is production-only: the upstream base URL is
`https://okk-backend.akfixdev.ru/api/v1`, while the public MCP resource is
`https://okk-mcp.akfixdev.ru/mcp`.

## Login and session sequence

1. Codex dynamically registers a public OAuth client. A client that supports
   Client ID Metadata Documents (ChatGPT, newer MCP clients) skips registration
   and uses the HTTPS URL of its metadata as `client_id`; `/authorize` fetches
   that document through an SSRF-safe pinned connection (rules in
   [security.md](security.md)) and saves the client row only when a person
   signs in.
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
   `302` to that exact URI with `code`, the original `state` and `iss` (RFC
   9207; error redirects carry it too). Codex owns the
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
   and `department_ids`. If the OKK access token expires while a tool call
   runs, the first read that gets 401 renews it through the same rotation and
   repeats once; the gateway remembers the renewal per OKK session, so the
   call's later reads (and other calls still holding the old token) use the
   new token at once. A second 401 ends the call with the reconnect error.
6. Every analytics call uses only an upstream GET route. Results pass through an
   explicit safe projection before MCP serialization.

The private `Руководители` section is not represented as an ordinary
department. The gateway loads `/employees/restricted` inside each applicable
MCP request; the upstream endpoint returns only rows explicitly granted to the
connected user. The `admin` role does not bypass this catalog. Dedicated
supervisor tools require the selected UUID to exist in that live result before
calling `/calls`. A newly added, removed or re-granted supervisor therefore
takes effect without a plugin release.

Restricted supervisors are `transcription_only`. Their MCP projection contains
identity, basic call volume/duration/direction and transcript data only. It does
not expose or manufacture department membership, evaluation scores, scenarios,
clients, CRM or AI growth observations.

`get_client_statistics` projects the platform summary counters for the client
touch cycle of departments that track touches (OKK decides this per department
and reports `client_touch_metrics_applicable` and the reset period). Existing,
new-first and new-repeat call counts are mutually exclusive; the reactivated
counter is a documented subset of first touches and contains only resets from
the previous repeat-touch state. The gateway does not recompute chronology and
does not receive or serialize the underlying phone rows.

Call plans come from the OKK plan constructor (`/call-plans/plan-fact`); the
gateway passes plan items, plan and fact through without recomputing them,
drops the edit history and grounds personal items against the live roster.

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
department code and completeness/count indicators. Supervisor tools add only a
boolean `supervisor_filter` marker, never its UUID or name. It never contains raw
request selectors, entity UUIDs, employee names, AI observations, response
payloads, passwords or tokens. `ANALYTICS_TRACE_ENABLED=false` disables these
events; responses themselves are never persisted by the gateway.

This is deliberately not HR/SSO authorization. Users provisioned by HR still
need a usable local OKK password for `/auth/login`.

## Plugin packaging contract

One folder, `plugins/okk-analytics/`, serves Codex and Claude Code. Each client
reads only its own files; the rest are ignored by it. Verified on 2026-10-14 by
installing the package into isolated homes of Codex CLI 0.154.0 and Claude Code
2.1.118:

| File | Read by | Purpose |
|---|---|---|
| `plugin.json` (`$schema` AgentPlugin 1.0.0) | Codex | Identity; OpenAI settings in `extensions["com.openai"]` (`interface`, `apps`) |
| `mcp.json` | Codex | The remote server: only `type: streamable-http` and `url` |
| `.codex-plugin/plugin.json` | older Codex | Compatibility copy; Codex ignores it while the root `plugin.json` has `extensions["com.openai"]` |
| `.app.json` | ChatGPT / Codex apps | Mapping to the registered OpenAI connector |
| `.claude-plugin/plugin.json` | Claude Code | Identity, version and skills path |
| `.mcp.json` | Claude Code | The remote server: `type: http` and `url` |
| `commands/` | Claude Code | `/okk-analytics:check-connection` |
| `skills/okk-analytics/SKILL.md` | both | Shared workflow |

Constraints found by those installs:

- Codex 0.154 silently drops a server from `mcp.json` when its entry has any
  field besides `type` and `url` — including the documented
  `extensions["com.openai"].auth` block. The OAuth resource is therefore not
  declared in the package: Codex and Claude Code discover it from the
  protected-resource metadata, and the gateway binds a token to
  `MCP_RESOURCE_URL` when the client omits `resource`. The former
  `.mcp.codex.json` with `oauth_resource` was never read and is removed.
- Claude Code reads `.mcp.json` and ignores `mcp.json`, so the server is not
  registered twice.
- The version lives in each `plugin.json` and equals the gateway
  `okk_mcp.__version__`; the Claude marketplace entry carries no second copy.
  `server/tests/test_package.py` checks all of this.

Marketplace policy for Codex is `ON_INSTALL`, so Codex starts OAuth while
installing the plugin. A package file is not evidence that ChatGPT has loaded
that skill or refreshed its connector metadata; verify the installed client.

Dynamic registration includes optional client metadata only when it has a
value. In particular, an absent `client_uri` is omitted instead of emitted as
JSON `null`, because strict OAuth clients validate a present field as a URI
string. Both clients use the same tools, live ACL checks and safe projections;
the packaging files do not fork analytics logic.

Gateway 1.6.0 adds RFC 9207 `iss` in every authorization response, Client ID
Metadata Documents next to dynamic registration, and scope step-up: a tool
call without a needed scope returns an error result whose
`_meta["mcp/www_authenticate"]` holds the `insufficient_scope` challenge. With
`iss` advertised, ChatGPT switches to its stable callback
`https://chatgpt.com/connector_platform_oauth_redirect`, which is in the
default allowlist. Before a production release, connect fresh ChatGPT, Codex
and Claude Code clients to a non-production gateway: CIMD makes clients prefer
the document, and a document the gateway cannot fetch (for example behind a
bot challenge) fails the sign-in; `MCP_CLIENT_METADATA_DOCUMENTS_ENABLED=false`
returns every client to registration without a release. CIMD is therefore off
by default: on 2026-10-27 the production server could fetch neither Claude's
document (`https://claude.ai/oauth/claude-code-client-metadata`, Cloudflare
302) nor ChatGPT's (`https://chatgpt.com/oauth/client.json`, 403). Turn it on
only after both download from the gateway host.

## Scaling

MCP transport is stateless Streamable HTTP. OAuth/session state is shared in
PostgreSQL; login and registration throttles are shared in Redis (a trusted
client network such as Anthropic's egress shares one larger registration
bucket). Client metadata documents are cached per process for five minutes,
so replicas may fetch the same document once each. Each MCP
request reloads the live OKK user without a database lock or open transaction,
so parallel tool calls of one person run in parallel. Only the rotation of the
OKK refresh token is serialized with a row lock — held across the OKK refresh
call, because OKK ends its session when a rotating refresh token is used twice;
a request that waited for the lock finds the token already renewed. No sticky
sessions are required.

A read that fails in transit — a dropped connection, a timeout, or 502/503/504 —
is repeated twice, after 0.3 s and 1 s. Only reads take this path (the GET
routes and the two body-only POST reads), so a repeat cannot change anything in
OKK; a 4xx is OKK's answer and is never repeated. A final failure is reported as
OKK temporarily unavailable. One read can therefore take up to about three
`MCP_HTTP_TIMEOUT_SECONDS` (30 s by default) before it fails.

Expensive cross-employee and criterion aggregation uses bounded concurrency.
`ANALYTICS_MAX_CALLS` and `ANALYTICS_MAX_EMPLOYEES` cap a single request; a
truncated response returns `partial` rather than silently claiming completeness.
Transcript full-text search runs in OKK (`POST /calls/transcript-search`, the
words travel in the body, never in a URL): OKK filters the whole ACL-scoped
journal and returns the exact number of matching calls, the gateway reads the
transcripts of the returned page only to cut excerpts. Transcript bodies live
only for the duration of the MCP request and are not written to PostgreSQL,
Redis or operational logs.
