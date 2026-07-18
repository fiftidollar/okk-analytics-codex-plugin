# Changelog

## 1.1.0 - Unreleased

- Added three dedicated transcript tools: paginated call/transcript discovery,
  full raw/diarized/segment reading and bounded full-text search with excerpts.
  They use the new `okk.transcripts.read` OAuth scope, upstream call ACL plus a
  second live department/employee guard, safe call projections and explicit
  scan/result completeness metadata.
- Added a native Claude Code plugin manifest, marketplace, standard remote HTTP
  MCP configuration and `/okk-analytics:check-connection` command alongside the
  existing Codex package. Both clients share one skill and backend.
- Dynamic client registration now omits an absent optional `client_uri` instead
  of serializing JSON `null`, preserving compatibility with strict Claude Code
  OAuth metadata validation.
- Added one ACL-safe department resolver for UUIDs, exact codes and normalized
  names across every department-scoped tool.
- Department discovery is explicitly live and data-driven: the plugin carries
  no fixed department catalog, and newly added or renamed visible departments
  require no plugin update.
- Failed named selectors now return `not_available` and can never broaden into
  an all-department query; employee/department cross-filter mismatches fail
  closed as well.
- Expanded department cards with complete ranking, department summary and
  employee trend sources, while filtering overview rollups to the effective
  department.
- Fixed historical scenario/search forwarding, employee/call cap reporting,
  direct employee lookup beyond list caps, historical CRM-date labeling,
  partial CRM coverage, distinct-employee AI insight counts, criterion output
  limits, structured AI insight values and timezone-safe mentoring deadlines.
- Single-employee and single-scenario requests now derive and report their
  actual visible department in `effective_scope` even when the caller supplied
  only the entity ID.
- Added redacted structured tool-call traces and Windows timezone data to the
  declared runtime dependencies.
- Added an authenticated connection confirmation (`authenticated=true` plus a
  user-facing message), mandatory first-use scope verification in the skill,
  and a one-click starter prompt for non-technical users.
- The login page now explains the Codex callback handoff, while a direct,
  refreshed or stale `/authorize` URL renders a friendly recovery page instead
  of FastAPI validation JSON.
- Moved the published package to the canonical
  `plugins/okk-analytics/` marketplace layout and corrected update guidance.

## 1.0.2 - 2026-07-15

- Replaced the over-limit starter prompt with three short official-style
  prompts that Codex loads instead of ignoring.

## 1.0.1 - 2026-07-15

- Matched the official Codex remote-MCP package shape by declaring the exact
  OAuth resource in `.mcp.json` and starting authentication during install.
- Restored the standard OAuth completion contract: the authorization endpoint
  redirects directly to the exact Codex callback, while the validated callback
  origin is narrowly included in the login page's `form-action` CSP.
- Removed the custom JavaScript callback handoff; Codex now owns the listener,
  token exchange and authenticated MCP state end to end.

## 1.0.0 - 2026-07-14

- Initial standalone community plugin.
- 19 typed read-only analytics tools.
- Native OKK login bridged to OAuth 2.1 Authorization Code + PKCE.
- Live ACL revalidation, encrypted upstream sessions and hashed MCP tokens.
- Flow-scoped stateless CSRF nonces keep parallel or retried OAuth login pages
  independent without relying on browser cookies. If an old form submits a
  cookie-era request, the gateway refreshes it into the stateless format instead
  of trapping the user on a dead recovery page.
