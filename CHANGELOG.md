# Changelog

## 1.3.0 — prepared 2026-10-14, not released

- Plugin packages match what the clients actually load (checked by installing
  into isolated homes of Codex CLI 0.154.0 and Claude Code 2.1.118): Codex
  reads the root AgentPlugin `plugin.json` and `mcp.json`, Claude Code reads
  `.claude-plugin/plugin.json` and `.mcp.json`. Removed `.mcp.codex.json`: no
  client read it, so its `oauth_resource` never applied. `mcp.json` stays
  limited to `type` and `url` because Codex 0.154 drops a server entry with any
  other field. The version lives once per manifest and equals the gateway
  version; the Claude marketplace entry no longer repeats it.
- Codex listing: `shortDescription` within 30 characters and `supportURL`.
- ChatGPT submission file: `$schema` now points to the current
  `plugins/schemas/chatgpt-app-submission.v1.json`, and the missing
  `list_call_phone_records` tool is listed; a test keeps the file equal to the
  server's tool inventory.
- Security: new OAuth clients may register only loopback return addresses or
  HTTPS addresses on `MCP_OAUTH_REDIRECT_HOSTS` (default `chatgpt.com`,
  `claude.ai`, `claude.com`). The login page names where the browser will
  return and warns about an unknown site. Previously anyone could register a
  client named «Codex» with their own return address.
- Read call plans from the OKK call-plan constructor (`/call-plans/plan-fact`)
  instead of the removed per-employee `/plans` API. `get_plan_fact_statistics`
  now returns plan items per department (direction × clients × counting, plan
  and fact for the period, plan mode and weeks); the department report and the
  employee card embed the same structure. Unknown facts stay `null`, the edit
  history and its authors are not exposed. Requires an OKK platform with the
  call-plan API; on an older platform composite reports mark only the plan
  section `not_available`.
- Department records expose `client_status_tracked` and `client_touch_tracked`
  instead of the retired `weekly_call_plan`. Client-touch metrics are described
  per department (`client_touch_metrics_applicable`, reset days from OKK)
  instead of being tied to a B2B department code; the catalog names them
  `new_client_first_touch`, `new_client_repeat_touch` and
  `new_client_reactivated_first_touch`.
- Scenario pass rate follows the norm OKK decides per call (`quality_status`:
  `norm` / `below_norm`, the department norm stored with the evaluation)
  instead of comparing scores with the scenario `passing_score`. An older OKK
  API without `quality_status` still uses `passing_score`; with neither, or a
  mix, the rate is `null`. Before, a scenario without `passing_score` compared
  every score with 0 and reported 100%. The new `pass_rate_basis` field names
  the norm used. This lets OKK drop the unused scenario threshold.

## 1.2.3 — 2026-09-09

- Preserve unset plan totals as null and expose per-metric employee coverage;
  explicitly assigned zero plans remain zero.
- Return precise report metric definitions with department data, distinguishing
  successful, duration-qualified and evaluated calls. Keep missing people and
  scores unavailable after a fresh lookup instead of proposing external data
  as replacement OKK evidence.
- Add report-semantics release checks and plan regression cases. Pytest now
  works from the repository root without a manual PYTHONPATH override.

## 1.2.2 - 2026-09-09

- Replaced the provisional red mark with the production OKK identity: a dark
  graphite tile, readable gold `OKK` monogram, quality seal and mint checkmark.
- Grounded every named employee in department reports against the live OKK
  employee directory. Foreign IDs are dropped, conflicting names are replaced
  by the canonical directory value, and the response exposes the exact
  authoritative ID/name allowlist plus completeness counters.
- Hardened filtered overview, employee-directory and plan/fact results with the
  same roster contract, and added an authenticated release-smoke assertion that
  rejects any employee row outside the resolved department.
- Strengthened both the MCP-native instructions and bundled skill so a model
  must rebuild a disputed report from live tools instead of correcting names
  from memory or earlier conversation text.

## 1.2.1 - 2026-09-09

- Bound the bundled `skills/okk-analytics` workflow to the registered ChatGPT
  MCP integration through `.app.json`, so the installable package contains the
  skill and MCP connection as one plugin instead of exposing only raw tools.
- Added the public-submission payload, exact five-positive/three-negative test
  matrix and a production PNG logo for the universal Plugins Directory review.
- Added `openid` and `email` OAuth scopes, a live `/userinfo` endpoint with the
  verified OKK account email, and a secret-backed domain-verification route for
  ChatGPT workspace domain restrictions and submission ownership checks.

## 1.2.0 - 2026-09-09

- Added five tools for the private `Руководители` section: live ACL-scoped
  discovery, basic transcription-only call statistics, transcript listing,
  one-call transcript reading and transcript search. Supervisor membership is
  never compiled into the plugin and an admin role alone does not grant access.
- Added model routing that resolves a named person across the ordinary employee
  and private supervisor catalogs before choosing tools. Supervisor UUIDs are
  never sent to KPI, scenario, CRM, client, growth or mentoring endpoints.
- Hardened direct transcript reads for department-less calls: the gateway now
  requires a match in the caller's live restricted-supervisor catalog before
  fetching transcript text.
- Extended `get_client_statistics` and the metric catalog with the B2B
  30-day first/repeat-touch cycle, including initial first touches, repeat
  touches and the subset reactivated from repeat into first touch after 30
  days. A first touch must transition to repeat before the reset becomes
  eligible. The tool still returns only aggregate counters and never exposes
  phone rows.

## 1.1.0 - 2026-07-18

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
