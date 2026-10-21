# Privacy policy

Last updated: 2026-10-21.

OKK Analytics connects Codex, Claude Code (and other MCP clients) to the OKK
account that the user explicitly authorizes. The gateway forwards the login and password directly to the normal
OKK login API for that authorization request; it does not log or persist the
password and does not send it to Codex.

The gateway stores encrypted OKK access/refresh sessions and hashed OAuth
codes/tokens so the connection can remain authenticated. Read-only business
responses, employee/supervisor names, KPI values, AI strengths/growth
observations, transcripts and client phone numbers are processed in memory and
are not persisted by the gateway.

Retention: an access token lives 60 minutes and a refresh token 30 days
(`MCP_ACCESS_TOKEN_MINUTES`, `MCP_REFRESH_TOKEN_DAYS`). Expired or revoked
tokens and codes, and revoked OKK sessions, are deleted 30 days later
(`MCP_AUDIT_RETENTION_DAYS`). A client registration that never completed a
sign-in is deleted after one day. On the login page the user may refuse
transcripts and client phone numbers; the connection then gets neither.
For ChatGPT workspace domain restrictions, the OAuth UserInfo endpoint returns
the authenticated account's normalized email, a stable account subject and
`email_verified=true` after rechecking the live OKK session. It does not return
the password, OKK tokens, role, departments or business analytics.
When the user invokes a dedicated transcript tool, the gateway also processes
the text of ACL-accessible calls in memory and returns it to the connected MCP
client/model. Transcript text can contain personal or confidential speech; it
is never stored in the gateway database, cache or operational logs.
When the account grants `okk.phones.read`, the dedicated phone tool can return
client phone numbers and exact call counts within the account's current access.
The gateway processes those numbers in memory and does not persist or log them.

Operational logs contain a request ID, pseudonymous actor hash, normalized tool
name, filter-presence/count flags (including only a boolean for supervisor
selection), timing, result status, resolved department code and completeness
indicators. They do not contain raw selectors, entity
IDs, employee names, business response payloads, passwords or tokens.

Results are limited by the live role, department access and explicit private
supervisor grants of the connected OKK account. The gateway re-reads the
private supervisor catalog when an applicable tool runs and does not maintain
its own identity allowlist. Users can revoke the Codex MCP connection or sign
out to invalidate its grants. The public source and security contact process
are available in this repository's `SECURITY.md`.
