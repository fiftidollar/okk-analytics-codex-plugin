# Changelog

## 1.6.0 — not released

Reliability release after a review of the best MCP servers (GitHub, Sentry,
Supabase, Cloudflare) and the MCP, Claude and OpenAI rules. No change to the
answers' data; update the plugin and start a new chat.

- The tools declare no output schema: the same envelope schema on 26 tools
  was 58% of the tool list the model reads in every chat (105,147 → 47,097
  characters). Answers are still validated against it; the envelope is named
  once in the server instructions; a test keeps the list within 50,000.
- `request_id` moved from the answer to the result `_meta` (OpenAI review:
  no internal identifiers for the model). `securitySchemes` is a field of
  each tool (copy in `_meta` kept); a tool without parameters accepts only an
  empty object.
- Limits (MCP: servers MUST rate limit): at most 60 tool calls per
  connection per minute; one call stops after 50 s with a reason instead of
  being cut by Codex at 60 s.
- Call texts (transcript, preview, excerpts, segments, `call_summary`,
  `violation_phrase`) lose invisible and control characters (MCP: sanitize
  outputs, as GitHub's server does); the server instructions say call texts
  are data, never instructions. The plugin README asks to keep confirmation on
  for tools that send something.
- Behaviour rules moved from tool descriptions to the server instructions;
  the `filters` parameter shows an example.
- OAuth: every `/authorize` redirect (code and errors) carries `iss` (RFC
  9207); post-validation errors return to the client. ChatGPT's stable
  `https://chatgpt.com/connector_platform_oauth_redirect` is allowed.
- OAuth: Client ID Metadata Documents, SSRF-safe (public IPs only, pinned
  TLS, no redirects, 5 s / 5 KB, 5-minute cache), redirect URIs only narrow
  our allowlist. Off by default (`MCP_CLIENT_METADATA_DOCUMENTS_ENABLED`):
  Claude picks an advertised CIMD at once, and the production server could not
  fetch Claude's or ChatGPT's documents on 2026-10-27.
- MCP: a call without a needed scope returns `isError` with
  `_meta["mcp/www_authenticate"]` (`insufficient_scope`, granted plus missing
  scopes), so the client can ask for the extra access; the reconnect text stays.
- Registration limit: Anthropic's egress `160.79.104.0/21`
  (`MCP_TRUSTED_CLIENT_NETWORKS`) shares one bucket of 600 per hour, so
  claude.ai connections no longer hit 429; other addresses keep 20 per hour.
- MCP SDK 1.27.2 → 1.30.0 (security fixes of the 1.x line).

## 1.5.0 — released 2026-10-27

Requires the OKK platform release with `POST /calls/transcript-search`,
`kpi_evaluated`, the stable journal order and the call department in the card
(`docs/deployment.md`); deploy OKK first. Update the plugin and start a new
chat: several answers changed. Found by a full review of the gateway on
2026-10-27.

- Fix: transcript search read at most `TRANSCRIPT_SEARCH_MAX_CALLS` (1000)
  calls of the period itself, so on a long period it never searched every
  call and answered `partial`. Now OKK searches every visible call of the
  period (the words travel in the JSON body, never in a URL) and returns the
  exact count. `search_call_transcripts` and
  `search_supervisor_call_transcripts` take `page`, and answer
  `matching_calls_total`, `page`, `page_size`, `pages`; `scanned_calls`,
  `candidate_calls_loaded`, `source_calls_total`, `search_call_cap` and
  `result_limit` are gone. The gateway reads only the returned page's
  transcripts, to cut excerpts. `TRANSCRIPT_SEARCH_MAX_CALLS` is removed.
- The OKK journal filters in MCP: `filters` on `list_call_transcripts`,
  `search_call_transcripts` and `list_call_phone_records` (below norm,
  outcome, direction, evaluated or not and why, critical errors, criteria
  below N points, score, duration), passed to OKK, which applies every rule.
  New tool `get_call_filter_options` (26 tools) lists the keys with OKK's
  labels. Call rows keep why a call is not evaluated or not counted, the B2B
  touch cycle and the follow-up of a missed call; the journal tool shows the
  critical error phrase.
- Trends ask OKK for `step=auto` (day, week, month or quarter by length):
  «all time» was 252 daily points. `daily_trend` is renamed `trend`.
- Fix: scenario and criterion results counted any call with a score,
  manually excluded and short ones too, so a scenario's average differed
  from its department's; now only the evaluated calls of the indicators.
- Fix: a call arriving between two journal pages was counted twice; a past
  call of a transferred employee was listed but did not open (the gateway
  checked the employee's current department, now the call's).
- Fix: `get_growth_insights` ranked by how many employees mentioned an item
  and then alphabetically, so a small `limit` dropped what loses most points,
  and answered over 100,000 characters for a large department. It now returns
  OKK's own insights in OKK's order, lists cut, no model explanations.
- Fix: the employee card dropped `quality_by_call` (a long period passed the
  50,000-character client limit) and survives CRM being down; a call deleted
  during criterion results no longer fails them; critical errors have their
  own `limit`; plan edit history no longer leaks through the department
  ranking; an OKK that ignores the phone filter answers «unavailable» instead
  of an off-schema status.
- The shared OKK HTTP client keeps no cookies, so one person's sign-in or
  refresh can no longer carry another person's OKK refresh token; it is sent
  only as an explicit `Cookie` header on `/auth/refresh` and `/auth/logout`.
- An OKK access token that expires during a tool call is renewed once and the
  read repeated instead of asking the person to reconnect; reads repeat a
  dropped connection, timeout or 502/503/504 twice (0.3 s, 1 s), never a 4xx.
- OKK 409 for a closed restricted employee is the neutral `not_available`,
  not a rejected filter.
- The login page shown again after a wrong password or a refreshed form keeps
  the person's unchecked transcript and phone boxes.
- A replayed authorization code revokes the tokens it issued and its OKK
  session (RFC 6749 §4.1.2); a `code_verifier` outside RFC 7636 is
  `invalid_grant` instead of HTTP 500.
- The plugin folder has its own README (required by the Claude plugin
  directory); stale tool counts in README and deployment docs corrected.

## 1.4.0 — released 2026-10-21

Tool design measured on realistic questions (`docs/deployment.md`). Breaking
for clients that cached the 1.3 tool list: update the plugin and start a new
chat.

- Fix: critical errors (stop words, swearing, rudeness) were counted on every
  observation (`penalties` summed `is_penalty`, the kind of criterion), so
  «стоп-слова сработали 13 раз» for 1 real violation, and they topped the
  weakest criteria with a meaningless percent. `get_criterion_performance`
  now answers `data.criteria` (ordinary criteria, average percent, weakest
  first) and `data.critical_errors` (`violations` — calls with a deduction —
  and `violation_rate_percent`). Criteria are labelled (id, code, name,
  category, max score, critical); the full definition is in
  `get_scenario_criteria`. The answer shrank from ~23,700 to ~6,000 characters
  on the stand.
- 25 tools instead of 28: `get_mentoring_statistics` (OKK keeps no
  mentoring), `get_statistics_catalog` (repeated the tool descriptions) and
  `list_departments` are gone; `get_access_context` lists the departments with
  their norm, minimum call duration and tracked client metrics.
- One `department` parameter (code, name or id) replaces `department_id` and
  `department_ref`, which could disagree; `compare_departments` takes
  `departments`.
- `get_department_statistics` returns one summary, the complete employee
  ranking, the plan and `reporting_contract`; the department summary, the
  top-50 dashboard ranking and the second copy of the roster are gone, and
  daily trends come with `detail=full` only (fewer OKK calls too).
- Answers no longer repeat the caller's department catalog
  (`access_context`); unset top-level parts are left out instead of `null`.
- Employee card and comparison: no mentoring or saved-focus fields;
  ranking rows carry `employee_name` only when they have no name.
- Tool descriptions say when to use each tool, with the Russian domain words
  users type; every parameter has a short description; pydantic titles are
  removed from input schemas. The tool list the model reads is ~27,900
  characters instead of ~30,100. `get_access_context` is marked
  `anthropic/alwaysLoad` so Claude Code loads it without a tool search.
- Server instructions: use `list_supervisors` only when `get_access_context`
  shows the Supervisors section (the model searched it for every name).
- Skill rewritten around workflows (96 lines instead of 225); tool contracts
  live in tool descriptions, safety in the server instructions.
- OAuth: the login page has checkboxes for transcripts and client phone
  numbers (checked by default); an unchecked one is not granted. The MCP 401
  challenge carries `scope`, and no error code when there is no token.

## 1.3.2 — released 2026-10-21

Fixes from a review against the MCP specification, Claude Code and Codex
guidance (`docs/deployment.md` lists the evidence).

- Dates are no longer dropped. `start_date`/`end_date` without `period` made
  the gateway answer for the current month with no error. Dates alone now
  mean a custom range; a named period together with dates, one date, or a
  non-ISO date is an error. Dates are typed (`format: date`).
- An OKK outage is no longer reported as an invalid MCP token (401 sent the
  client back to sign in): the tool answers with an error saying OKK is
  temporarily unavailable, never with an empty result. `/userinfo` answers 503.
- Each request reloads the live OKK user without a database lock, so parallel
  tool calls of one person no longer run one by one; only the OKK refresh-token
  rotation is serialized, and once for parallel requests.
- A refresh repeated within `MCP_REFRESH_RETRY_SECONDS` (default 30) while the
  new refresh token is unused gets the same pair instead of revoking the whole
  session: a lost answer or two processes refreshing at once no longer force a
  new sign-in. Other reuse still revokes the family.
- Return addresses: exact web callbacks in `MCP_OAUTH_REDIRECT_URIS` (replaces
  `MCP_OAUTH_REDIRECT_HOSTS`, which allowed any path on claude.ai, claude.com
  and chatgpt.com, where other people's apps live too); checked at
  registration and at `/authorize`. The login page shows the exact address.
- Registration: per-IP limit (`MCP_REGISTRATIONS_PER_HOUR`, default 20);
  registrations that never completed a sign-in are deleted after a day.
  Migration `20261021_01` adds two nullable columns.
- Response size: transcript chunk at most 30,000 characters (segments too),
  previews and search context at most 500 characters, pages at most 50 items.
  The text copy of a result is compact JSON (it was indented, +30%).
- Server instructions fit the 2,048 characters older Claude Code keeps (were
  3,276, so the safety rules at the end were cut); rules for one tool moved to
  its description.
- A missing permission names the scope and says to reconnect.
- Stale texts: no tasks in `compare_employees`, no `task_page_size`, no
  mentoring on the login page.
- Republishes the skill changed after the 1.3.1 deployment: Claude Code and
  Codex update a plugin only when its version changes. A test now fails when
  plugin files change without a new version (`plugins/released-packages.json`).
- Tests: OAuth token lifecycle and OKK session renewal on a real PostgreSQL
  (`OKK_MCP_TEST_DATABASE_URL`, also in CI).

## 1.3.1 — released 2026-10-20

Fixes found by a read-only check of 1.3.0 against the OKK test stand.

- Scenario and criterion statistics no longer drop evaluations made on an
  archived scenario version. OKK republishes a scenario under the same code
  and archives the old version as `<code>__legacy_<timestamp>`; without
  `scenario_ids` the gateway read only current versions and silently skipped
  the rest while answering `ok` (stand, 01.10–20.10: 8 of 13 CSM evaluations,
  86.0 / 62.5% instead of 89.0 / 76.9%; criteria 88 of 143 observations).
  A scenario is now all its versions: `get_scenario_performance` rows add
  `versions[]` (`scenario_id`, `current`, `evaluations`); criteria are joined
  by their criterion `code` within the scenario, rows add `scenario` and
  `scenario_versions` (replacing `scenarios`), and criterion projections
  include `code`. A requested archived version ID stands for its scenario.
  An archived version takes the code OKK shows on its calls, because OKK cuts
  a code longer than 21 characters in the archived name.
  `get_criterion_performance` with `scenario_id` no longer asks OKK for the
  calls of one exact version.
- Evaluations that cannot be attributed are reported, never dropped:
  `effective_scope.excluded_evaluations`, `excluded_evaluation_reasons`
  (`scenario_unknown`, `scenario_not_in_current_catalog`,
  `criteria_version_not_readable`, `evaluation_items_unavailable`) and, for
  criteria, `excluded_observations`; the result is then `partial`. A
  non-admin cannot read archived criteria, so their observations land here.
- The `scenario_id` filter of `list_call_transcripts` and
  `search_call_transcripts` covers every version of the scenario. It went to
  OKK `/calls`, which filters by the exact version, so calls evaluated on the
  scenario's other versions were missing. The gateway now never sends
  `scenario_id` to `/calls`: it reads the scope and takes the scenario's
  calls itself (`list_call_transcripts` pages them, the search applies its
  cap to them). Calls it cannot place are reported in
  `effective_scope.excluded_calls` / `excluded_call_reasons` and make the
  result `partial`; `effective_scope.scenario_code` names the scenario.
- `list_scenarios` and other scenario projections no longer return
  `calls_evaluated` / `avg_score`: the new OKK leaves them at 0 / null. Use
  `get_scenario_performance`.
- `get_employee_card` reads the page, the period summary and the plan for the
  same explicit dates (before, the summary got the named period and OKK's own
  range for `week`). It answers `ok` when every part loaded instead of always
  `partial`.
- CRM statistics read the snapshot statuses OKK actually returns. The gateway
  counted a snapshot as usable only for `status == "ok"`, which OKK has never
  sent (it writes `ready`, in the older API too), so every department showed 0
  available snapshots and `partial`. Now `ready` is usable and the only source
  of totals, stage and funnel counts; `no_bitrix_id` is "CRM does not apply"
  (`not_applicable`), not a gap; `not_configured`, `not_synced` and `error` are
  `unavailable`; any other value is `unknown_status`. `coverage` adds
  `not_applicable`, `unknown_status` and `by_status`. With no usable snapshot
  the result is `not_available` (or `no_data` when nobody is linked to
  Bitrix). The employee card is `partial` when its CRM snapshot is
  unavailable or unknown.
- An empty `department_ref` fails closed as `not_available` like a blank or
  unknown one; before, it meant "no filter" and returned all departments.
- Release smoke expects the 28 registered tools (it missed
  `list_call_phone_records` and failed against every real gateway), checks
  the phone tool's `okk.phones.read` scope, and its test now validates the
  in-process server's real inventory.
- Tool descriptions: `list_departments` no longer promises a weekly plan;
  mentoring texts say the new OKK keeps none (`not_available`).
- Skill: no longer offers mentoring tasks or a saved focus (OKK keeps
  neither: say so, never zero); scenario results count all versions and name
  evaluations that could not be attributed. Changed after the gateway
  deployment, within 1.3.1.
- Docs: the README release status and the architecture overview describe the
  live 1.3.0 instead of 1.2.2–1.2.4.

## 1.3.0 — released 2026-10-20

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
- Works with the new OKK architecture API (production switch 10.2026; test
  fixtures follow live test-stand response shapes): mentoring is gone in OKK, so the mentoring
  tool answers `not_available` and the employee card and comparison report
  mentoring as unavailable instead of empty windows and zero counts; growth
  insights count the shared criterion name from `insights.summary`, because the
  new text lines carry each employee's numbers; departments list their norm
  (`quality_threshold`); scenario and criterion fields OKK no longer sends are
  omitted instead of null; call rows add `quality_status`, `quality_threshold`
  and `client_status`; phone history accepts the number-ending search of 4–9
  digits (`phone_match`). Older OKK responses keep working as before.
- Fix: `list_call_phone_records` with a period other than `all` put `date`
  objects into the JSON body and failed (`TypeError`); dates now go as ISO
  strings.

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
