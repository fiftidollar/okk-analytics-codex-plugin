# Tool catalog

All tools have `readOnlyHint=true`, `destructiveHint=false`,
`idempotentHint=true` and `openWorldHint=false`.

| Tool | Data |
|---|---|
| `get_access_context` | Authenticated connection proof, current role, visible departments with their norm (`quality_threshold`), `min_call_duration_seconds` and tracked client metrics; the only answer that carries the department catalog |
| `get_overview_statistics` | Overall KPI, clients, department rollup, ranking and trend; a department-filtered employee ranking is grounded to the live roster |
| `get_department_statistics` | One department's summary, every employee's metrics (`complete_employee_ranking`), plan/fact and `reporting_contract`, with an authoritative live employee roster; `detail=full` adds department and employee trends |
| `compare_departments` | Visible department metrics and trends |
| `list_employees` | Safe employee directory without credentials or phone fields; locally rechecked against live department membership |
| `list_supervisors` | Live explicit-grant catalog for the private `Руководители` section |
| `get_supervisor_call_statistics` | Basic call volume, duration, direction and day trend for one accessible transcription-only supervisor |
| `get_employee_card` | KPI, plan/client/CRM, strengths, growth areas and weekly focus for one date range |
| `compare_employees` | KPI, strengths, growth areas and weekly focus side by side |
| `get_call_statistics` | Call volume, evaluated count, scores, pass rate, duration and trend |
| `list_call_phone_records` | ACL-scoped client phone page or exact all-status call count/existence by normalized number; requires `okk.phones.read` |
| `list_call_transcripts` | ACL-scoped call journal with the OKK journal `filters`, exact `total_calls`, transcript availability, bounded previews and the critical error phrase |
| `get_call_filter_options` | Values and OKK labels of the journal filters: outcomes, norm, reasons a call is not evaluated, critical error and criterion keys of the active scenarios (of one department when given) |
| `get_call_transcript` | Raw/diarized full text or safe speaker segments for one accessible call |
| `search_call_transcripts` | Phrase/all-term/any-term search over every visible call of the period, exact count, paged results with excerpts |
| `list_supervisor_call_transcripts` | Private-supervisor call catalog, transcript availability and bounded previews |
| `get_supervisor_call_transcript` | Raw/diarized text or safe speaker segments for one call belonging to an accessible supervisor |
| `search_supervisor_call_transcripts` | The same search across every call of one accessible supervisor, exact count, paged |
| `get_plan_fact_statistics` | Weekly call-plan items (direction × clients × counting) with plan and fact for the period, per department or one employee |
| `get_client_statistics` | New/regular contacts, missed/no-answer, plus first/repeat touches and returns from repeat to first after the reset period for departments that track client touches |
| `get_crm_statistics` | Bitrix deals, tasks, overdue, stages, funnels and employee coverage |
| `get_growth_insights` | OKK's own period insights (block 11) in OKK's order: growth zones by points lost per call, strengths, critical errors; a department lists who loses most (first five, with a total); an employee adds last week's focus; without a department — per visible department. No model explanations |
| `list_scenarios` | Safe scenario catalog; archived rows are admin-only. No evaluation counts: OKK no longer keeps the legacy `calls_evaluated` / `avg_score`, use `get_scenario_performance` |
| `get_scenario_criteria` | Categories, maximum scores, scales, indicators and applicability |
| `get_scenario_performance` | Per scenario (all its versions): evaluation count, per-version counts, employee coverage, score distribution and pass rate: share of evaluations at or above the norm; `pass_rate_basis` says which norm (`department_norm` — per-call `quality_status` from OKK; `scenario_threshold` — scenario `passing_score` on an older OKK API; `null` — unknown, never a guessed 100%) |
| `get_criterion_performance` | Per criterion code within a scenario (all its versions). `data.criteria`: ordinary criteria with average percent of the maximum, weakest first. `data.critical_errors`: critical errors (deductions such as stop words) with `violations` — calls where points were deducted — and `violation_rate_percent`; a percent of the maximum is meaningless for them. Criteria are labelled (id, code, name, category, max score, critical); their full definition is `get_scenario_criteria` |

## Journal filters (1.5.0)

`list_call_transcripts`, `search_call_transcripts` and
`list_call_phone_records` take one `filters` object, passed to OKK as its own
journal parameters; OKK applies every rule, the gateway decides nothing:
`quality` (`below_norm` is exactly the KPI «Требуют внимания»), `result`
(`successful`, `missed`, `no_answer` — the KPI outcomes), `direction`,
`evaluation` with `not_evaluated_reason`, `critical_errors` (calls with any of
the keys) or `any_critical_error`, `criteria_below` («the criterion scored
less than N points», all conditions at once), `score_from` / `score_to`,
`min_duration_seconds`. Keys and labels come from `get_call_filter_options`.
The applied filters are echoed in `effective_scope.filters`.

Trends ask OKK for `step=auto`: points by day up to 45 days, then by week,
month or quarter; each point carries `date` (its first day) and `step`. The
field is `trend` (until 1.5.0 `daily_trend`, always daily).

Scenario and criterion results read only the evaluated calls of the
indicators (`kpi_evaluated=true` — the summary's «Оценено»), so a scenario's
average equals its department's when the department has one scenario. Until
1.5.0 any call with a score counted, manually excluded and short ones too.

OKK keeps no mentoring tasks: 1.4.0 removed `get_mentoring_statistics`, and
the department catalog with norms moved from `list_departments` into
`get_access_context`; `get_statistics_catalog` repeated the tool descriptions.

## Department selection contract

Every department-scoped tool takes one `department` parameter: the code, the
name or the id of a visible department (`compare_departments` takes a list,
`departments`). Before 1.4.0 there were two selectors, `department_id` and
`department_ref`, that could disagree.

The gateway resolves the selector only against departments returned to the
current account by the live OKK ACL. There is no department allowlist or
bundled name-to-ID mapping in the plugin: additions and renames become visible
from the platform without a plugin release. Matching is case-insensitive and
exact after normalizing spaces and punctuation; a unique acronym derived from
the currently displayed name is also accepted, but the resolver does not guess
by substring. If a
selector is absent from the visible scope or ambiguous, the result is
`not_available`. A supplied empty or blank `department` names no department
and is `not_available` too; to
mean "no department filter", omit the field. The gateway never falls back to
all visible departments.

At the conversation layer, Codex matches the user's wording against the live
catalog returned by `get_access_context`. If more than one current department
could be intended, it asks the user to choose; it must not guess or select the
first row.

`effective_scope` returns the resolved `department_id`, `department_code` and
`department_name`. A model must verify these fields before attributing employee
or KPI rows to the requested department. If `not_available` is returned, the
model may name only departments already present in `get_access_context`.

When both employee and department filters are supplied, the employee must
belong to that resolved department. Employee cards/comparisons, calls, clients,
plans, CRM, growth, scenarios and criteria all apply this guard.

## Private Supervisors contract

`Руководители` is a separate personal-access section, not a department and not
an admin-wide dataset. `list_supervisors` reads the live upstream
`/employees/restricted` catalog and safely projects only ID, name, position and
active state. No name, email or UUID is hardcoded in the gateway.

When a user names a person without saying which section they belong to, the
bundled skill searches both `list_employees` and `list_supervisors`. It proceeds
only after one unambiguous visible match. Ordinary employee tools never accept
a supervisor as a substitute, and supervisor tools validate their UUID against
the private catalog before any `/calls` request. An inaccessible or removed
grant returns neutral `not_available` without looking up calls.

Supervisor rows are transcription-only. `get_supervisor_call_statistics`
returns call counts, loaded duration/direction/day aggregates and explicit
source completeness. Quality scores, scenarios, client/CRM data and growth
areas are intentionally unavailable rather than reported as zero. The three supervisor transcript tools mirror the normal transcript
formats and completeness contract without merging supervisors into department
results.

Transcript tools apply the same guard to department, employee and scenario
filters. A direct call ID is first checked through the ACL-protected call-detail
endpoint and then checked again against the gateway's live department catalog
before the transcript endpoint is called. Missing and inaccessible call IDs
therefore have the same neutral `not_available` result.

Call detail uses an employee response without a nested department. The gateway
resolves that employee through the live directory before reading the transcript;
this keeps a call discoverable from the catalog readable by its UUID. Long text
and segment responses expose `next_start_char` / `next_start_segment`; repeat
the request with that offset until null. Text chunks include `source_sha256`
to detect a transcript changed between reads.

`list_call_phone_records` uses the indexed upstream `phone_number` filter
through read-only `POST /calls/phone-lookup`; the number is in the JSON body,
outside access-log URLs. From 10 digits OKK matches the whole number, 4–9
digits match the number ending (`phone_match`: `whole_number` or
`number_ending`); fewer than 4 digits is not a search. Period dates go in the
body as ISO strings.
Its `matching_calls_total` counts all ACL-visible journal records for the number,
including missed, short and in-process calls. `has_call` answers existence. The
default `all` period sends no date bounds, so the full available journal is
searched. A response page is only a sample of that total. A platform revision
without the phone field/filter fails closed with the «temporarily unavailable»
tool error.
Numbers never enter the other analytics or transcript tools.

## Common response envelope

- `status`: `ok`, `partial`, `no_data` or `not_available`. When OKK itself is
  down, the tool returns an error (`isError`) saying OKK is temporarily
  unavailable — never an empty or zero result.
- Period: `period` by name (Moscow time), or `start_date` and `end_date`
  (`YYYY-MM-DD`) for any other range. Dates without `period` are a custom
  range; a named `period` together with dates is an error.
- `effective_scope`: filters actually applied. The caller's role and visible
  departments come only from `get_access_context`; other answers do not repeat
  them (1.4.0).
- `period`: exact inclusive dates.
- `omitted_filters_count`: number of inaccessible mixed-list filters, without
  echoing their IDs.
- The correlation ID of the gateway's redacted operational trace travels in
  the result `_meta` (`okk/request_id`), not in the answer the model reads
  (1.6.0; until then `request_id` in every answer).
- `employee_roster_grounding`: for employee-bearing department responses, the
  exclusive live employee ID/name allowlist, its exact department, source
  completeness, and counts of rejected or normalized upstream rows.
- `data`: the business payload.

The tools declare no output schema (1.6.0): the same envelope schema on every
tool was 58% of the tool list the model reads in each chat. Every answer is
still validated against the envelope before it leaves the gateway, and the
envelope is named once in the server instructions; a test keeps the published
tool list within 50,000 characters. `securitySchemes` is also a field of each
tool, as the OpenAI Apps SDK reads it (the copy in `_meta` stays).

Limits (1.6.0): at most `MCP_TOOL_CALLS_PER_MINUTE` (60) tool calls per
connection per minute — over it the tool answers an error with the seconds to
wait; one call stops after `MCP_TOOL_TIME_BUDGET_SECONDS` (50; Codex cuts a
call at 60 s) with an error asking to narrow the period or filters. If Redis is
down the call limit lets calls through (the login limit stays fail-closed).

Call texts — transcript, preview, excerpts, segments, `call_summary`,
`violation_phrase` — lose invisible format and control characters (zero-width,
bidirectional, tag characters) before they reach the model (MCP: servers MUST
sanitize tool outputs; GitHub's MCP server does the same for issue text), and
the server instructions say these texts are what people said: data, never
instructions. `source_sha256` is the hash of the cleaned text.

In `get_department_statistics` every employee-bearing subsource (the complete
ranking, employee trends and plan/fact rows) is intersected with the live
roster IDs. A supplied name that conflicts with the directory is overwritten
with the directory value; a row that names its employee only by id gets
`employee_name`. A rejected or normalized source row makes the response
`partial`; it can never reappear in a model-generated report as a plausible
employee. The answer has one summary and one complete ranking: until 1.4.0 it
also repeated the department summary, the top-50 dashboard ranking and the
roster as a second list.

`get_access_context` is the canonical post-login check. Because the tool can be
called only with a valid MCP OAuth token and revalidates `/auth/me`, its
`data.authenticated=true` result proves that the connection completed. In a new
task Codex must call it first and explicitly tell the user `OKK подключён`, then
show only the role and departments returned by that call. A browser redirect
alone is not treated as proof.

Its `data.available_sections.supervisors` field reports only whether the
private section is available and its visible row count. Names are returned only
when `list_supervisors` is called.

`no_data` means the scope is accessible but has no matching observations.
`not_available` means the requested scope/entity cannot be supplied. Neither
status permits substituting results from a broader query.

Employee populations expose `source_total`, `returned_population` and
`source_complete`. Call-derived tools expose source-call counts. Configured
caps return `partial`; they never claim a complete ranking. CRM exposes only
the latest snapshot per employee and explicitly rejects an unavailable
historical date instead of relabeling current data. Each employee snapshot
is classified by its OKK status: `ready` — usable, the only one whose counters
enter `totals`, `stage_counts` and `funnel_counts`; `no_bitrix_id` — the
employee is not linked to Bitrix, CRM does not apply (`not_applicable`, not a
gap); `not_configured`, `not_synced`, `error` — no usable snapshot
(`unavailable`); any other value — `unknown_status`. `coverage` reports these
counts and `by_status`. Unavailable or unknown snapshots make the result
`partial`; no usable snapshot at all is `not_available`, or `no_data` when no
employee is linked to Bitrix. Criterion output limits report the
matching and returned counts and cannot silently truncate an `ok` result.

Transcript search is exhaustive: OKK matches the shown transcript of every
visible call of the period (`POST /calls/transcript-search`, case-insensitive
substring; `all_terms`/`any_terms` split the query into words) and returns the
exact count. Every response reports `matching_calls_total`, `page`,
`page_size`, `pages`, `returned_matches`, `source_complete` and
`result_complete`; `result_complete=false` means more pages, read them with
`page`. A call whose transcript disappeared after the match stays in the page
with `match_count=0` and no excerpts. A call that is matched but no longer
visible is counted in `omitted` and makes the result `partial`. Until OKK
serves the route, the tool answers `unavailable` (OKK 404). Transcript payloads
include no structured phone/audio/PBX fields, and segment output is projected
to `speaker`, `text`, `start` and `end` only.

Aggregate strengths and growth areas count distinct employees mentioning a
normalized observation. Repeated or differently cased copies of the same
observation inside one employee card do not inflate `employee_mentions`. When
OKK sends structured insights (`insights.summary`), the criterion name is
counted: the text lines of the new OKK carry each employee's own numbers.

OKK keeps no mentoring tasks or saved focus; the employee tools do not mention
them (until 1.4.0 they answered `mentoring_available=false` and null task
lists for the older OKK API).

`get_employee_card` reads the employee page, the period summary and the plan
for the same explicit dates — the response `period` — as the OKK employee page
does; a named period is never passed on for OKK to resolve differently. The
card is `ok` when every part loaded and `partial` only for a platform without
the call-plan API (`plan_fact.status=not_available`) or a CRM snapshot that is
`unavailable` or `unknown_status` by the CRM classification above;
`no_bitrix_id` is no gap.

## Scenario versions

OKK republishes a scenario as a new version under the same code and archives
the replaced version under `<code>__legacy_<timestamp>`; an evaluation keeps the
version it was made on. `get_scenario_performance`,
`get_criterion_performance` and the `scenario_id` filter of
`list_call_transcripts` / `search_call_transcripts` therefore treat a scenario
as all its versions:

- An evaluation joins its scenario by the version's code when the account can
  read that version, otherwise by the code OKK shows on the call row (the code
  scenario selection chose). Archived versions are readable only by an admin,
  so for other accounts the call row decides. An archived version takes the
  code OKK shows on its calls, because OKK cuts a code longer than 21
  characters in the archived name.
- A requested `scenario_ids` / `scenario_id` value stands for its whole
  scenario, also when it names an archived version (admin). Each scenario row is
  shown by its current version and lists `versions[]` with
  `scenario_id`, `current` and `evaluations`.
- Criteria are joined across versions by their criterion `code` within the
  scenario; a criterion without a code stays separate under its ID. Each row
  carries `scenario` (current version ID, code, name) and `scenario_versions`
  (how many versions contributed).
- An evaluation that cannot be attributed is never dropped silently:
  `effective_scope.excluded_evaluations` counts it,
  `excluded_evaluation_reasons` names why, and the result is `partial`.
  Reasons: `scenario_unknown` (a score without a scenario),
  `scenario_not_in_current_catalog` (no current visible scenario has that code,
  e.g. a retired scenario; only without a scenario filter),
  `criteria_version_not_readable` (the version's criteria are not readable for
  this account — an archived version for a non-admin; `excluded_observations`
  counts its criterion observations), `evaluation_items_unavailable` (OKK
  returned no criterion results for a scored call).
- The gateway never sends `scenario_id` to OKK `/calls`: OKK filters by the
  exact version. It reads the scope (bounded by `ANALYTICS_MAX_CALLS`) and
  takes the scenario's calls itself. For the transcript tools this means
  `list_call_transcripts` pages those calls itself (`total_calls` counts them,
  `data.source_complete=false` and `partial` when the scope was capped) and
  `search_call_transcripts` reads every matching call of the scope from OKK
  (bounded by `ANALYTICS_MAX_CALLS`) and pages the scenario's calls itself.
  A call on a version this account cannot read that OKK names without a code
  is counted in `effective_scope.excluded_calls` /
  `excluded_call_reasons` (`scenario_unknown`) and makes the result `partial`;
  a call with no scenario version belongs to no scenario.

Department records carry the norm as `quality_threshold` (new OKK API) or
`passing_score` (older API). Scenario and criterion fields the platform no
longer sends (`passing_score`, `scoring_type`, `is_required`) are omitted, not
null. Call rows of the new API carry `quality_status`, `quality_threshold` and
`client_status` (new or existing client) instead of a client card.
# Department reporting contract (1.2.3 candidate)

`get_department_statistics.data.reporting_contract` supplies source policy,
metric definitions and response rules alongside the existing full department
payload. No tool or OAuth scope is added. `calls_total` is the successful-call
population; `total_calls` is duration-qualified, while `calls_evaluated` requires
an actual KPI score. Average duration does not require a score. Average quality
must not be recomputed by averaging employee means.

# Weekly call plan (1.3.0)

Plans come from the OKK call-plan constructor (`GET /call-plans/plan-fact`).
A department has one plan per week in one of two modes: `department` (one plan
for the whole department; the fact counts every employee's calls) or
`employees` (personal plans; the department plan is their sum). A plan is a
list of items; each item is direction (`all`/`outbound`/`inbound`) × clients
(`all`/`new`/`existing`) × counting (`connected` — the telephony reports the
call was answered; `successful` — a live manager–client conversation;
`successful_from_min` — successful and not shorter than
`min_call_duration_seconds`; `evaluated` — only scored calls) × number of
calls. Items count different calls and are never added together.

For a period that covers part of a week, OKK splits the weekly plan across the
working days Monday–Friday (the remainder goes to the first days); a whole week
carries the whole plan. The gateway returns OKK's numbers as is and does not
recompute plans or facts.

`get_plan_fact_statistics` returns `data.departments[]` — one entry per
requested department (or every visible department when neither a department
nor an employee is given) with `mode` (`department`, `employees`, `mixed` for
a period whose weeks differ, `null` when no plan exists), `weeks`, `items` and
personal `employees[].items`. `get_department_statistics.data.plan_fact` and
`get_employee_card.data.plan_fact` carry the same department entry; in an
employee card under a department plan, `contribution` is that employee's calls
inside the department fact.

- `mode=null` means no plan was set; it is not a zero plan.
- `fact=null` with `fact_state=no_client_base` means OKK had no monthly client
  base for new/existing classification; it is unavailable, not zero.
- `changed=true` means the item was edited after the week started. The edit
  history and its authors are not exposed.
- If the connected OKK platform has no call-plan API, composite reports keep
  their other sections and return `plan_fact.status=not_available`;
  `get_plan_fact_statistics` skips such departments and reports `partial`.
