# Privacy policy

Last updated: 2026-07-18.

OKK Analytics connects Codex to the OKK account that the user explicitly
authorizes. The gateway forwards the login and password directly to the normal
OKK login API for that authorization request; it does not log or persist the
password and does not send it to Codex.

The gateway stores encrypted OKK access/refresh sessions and hashed OAuth
codes/tokens so the connection can remain authenticated. Read-only business
responses, employee names, KPI values, AI strengths/growth observations and
mentoring data are processed in memory and are not persisted by the gateway.
When the user invokes a dedicated transcript tool, the gateway also processes
the text of ACL-accessible calls in memory and returns it to the connected MCP
client/model. Transcript text can contain personal or confidential speech; it
is never stored in the gateway database, cache or operational logs.

Operational logs contain a request ID, pseudonymous actor hash, normalized tool
name, filter-presence/count flags, timing, result status, resolved department
code and completeness indicators. They do not contain raw selectors, entity
IDs, employee names, business response payloads, passwords or tokens.

Results are limited by the live role and department access of the connected OKK
account. Users can revoke the Codex MCP connection or sign out to invalidate its
grants. The public source and security contact process are available in this
repository's `SECURITY.md`.
