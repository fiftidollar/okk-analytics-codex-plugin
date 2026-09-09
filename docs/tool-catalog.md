# Tool catalog

All tools have `readOnlyHint=true`, `destructiveHint=false`,
`idempotentHint=true` and `openWorldHint=false`.

| Tool | Data |
|---|---|
| `get_access_context` | Authenticated connection proof, current role and visible departments |
| `get_statistics_catalog` | Metric domains and explicit exclusions |
| `get_overview_statistics` | Overall KPI, clients, department rollup, ranking and trend; a department-filtered employee ranking is grounded to the live roster |
| `list_departments` | Visible departments and KPI settings |
| `get_department_statistics` | One department's KPI, plan/fact, employees and trend with an authoritative live employee roster |
| `compare_departments` | Visible department metrics and trends |
| `list_employees` | Safe employee directory without credentials or phone fields; locally rechecked against live department membership |
| `list_supervisors` | Live explicit-grant catalog for the private `Руководители` section |
| `get_supervisor_call_statistics` | Basic call volume, duration, direction and day trend for one accessible transcription-only supervisor |
| `get_employee_card` | KPI, plan/client/CRM, strengths, growth, focus and task windows |
| `compare_employees` | KPI, strengths, growth, focus and task-count comparison |
| `get_call_statistics` | Call volume, evaluated count, scores, pass rate, duration and trend |
| `list_call_transcripts` | ACL-scoped call catalog, transcript availability and bounded previews |
| `get_call_transcript` | Raw/diarized full text or safe speaker segments for one accessible call |
| `search_call_transcripts` | Phrase/all-term/any-term search with excerpts and explicit scan completeness |
| `list_supervisor_call_transcripts` | Private-supervisor call catalog, transcript availability and bounded previews |
| `get_supervisor_call_transcript` | Raw/diarized text or safe speaker segments for one call belonging to an accessible supervisor |
| `search_supervisor_call_transcripts` | Bounded phrase/term search across one accessible supervisor's transcripts |
| `get_plan_fact_statistics` | Total/inbound/outbound/new/regular plans and daily rows |
| `get_client_statistics` | New/regular contacts, missed/no-answer, plus B2B first/repeat touches and returns from repeat to first after 30 days |
| `get_crm_statistics` | Bitrix deals, tasks, overdue, stages, funnels and employee coverage |
| `get_growth_insights` | Employee and aggregate AI strengths/growth areas without raw reasoning |
| `get_mentoring_statistics` | Active/recent completed task window, status/overdue/completion stats |
| `list_scenarios` | Safe scenario catalog; archived rows are admin-only |
| `get_scenario_criteria` | Categories, maximum scores, scales, indicators and applicability |
| `get_scenario_performance` | Evaluation count, employee coverage, score distribution and pass rate |
| `get_criterion_performance` | Observation count, score %, penalties, coverage and recency |

## Department selection contract

Every department-scoped tool accepts both `department_id` and
`department_ref`. Use the UUID only when it is already known. When a user names
any department or supplies its current code, pass that exact value through
`department_ref`.

The gateway resolves the selector only against departments returned to the
current account by the live OKK ACL. There is no department allowlist or
bundled name-to-ID mapping in the plugin: additions and renames become visible
from the platform without a plugin release. Matching is case-insensitive and
exact after normalizing spaces and punctuation; a unique acronym derived from
the currently displayed name is also accepted, but the resolver does not guess
by substring. If a
selector is absent from the visible scope, ambiguous, or conflicts with a
simultaneously supplied UUID, the result is `not_available`. The gateway never
falls back to all visible departments.

At the conversation layer, Codex matches the user's wording against the live
catalog returned by `get_access_context`. If more than one current department
could be intended, it asks the user to choose; it must not guess or select the
first row.

`effective_scope` returns the resolved `department_id`, `department_code` and
`department_name`. A model must verify these fields before attributing employee
or KPI rows to the requested department. If `not_available` is returned, the
model may name only departments already present in `access_context`.

When both employee and department filters are supplied, the employee must
belong to that resolved department. Employee cards/comparisons, calls, clients,
plans, CRM, growth, mentoring, scenarios and criteria all apply this guard.

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
source completeness. Quality scores, scenarios, client/CRM data, growth areas
and mentoring tasks are intentionally unavailable rather than reported as
zero. The three supervisor transcript tools mirror the normal transcript
formats and completeness contract without merging supervisors into department
results.

Transcript tools apply the same guard to department, employee and scenario
filters. A direct call ID is first checked through the ACL-protected call-detail
endpoint and then checked again against the gateway's live department catalog
before the transcript endpoint is called. Missing and inaccessible call IDs
therefore have the same neutral `not_available` result.

## Common response envelope

- `status`: `ok`, `partial`, `no_data`, `not_available` or
  `temporarily_unavailable`.
- `access_context`: only the caller's current visible scope.
- `effective_scope`: filters actually applied.
- `period`: exact inclusive dates.
- `omitted_filters_count`: number of inaccessible mixed-list filters, without
  echoing their IDs.
- `request_id`: correlation ID for the gateway's redacted operational trace.
- `employee_roster_grounding`: for employee-bearing department responses, the
  exclusive live employee ID/name allowlist, its exact department, source
  completeness, and counts of rejected or normalized upstream rows.
- `data`: the business payload.

`get_department_statistics` also returns
`data.authoritative_employee_roster.items`. Every employee-bearing subsource
(`employee_ranking`, complete ranking, employee trends and plan/fact rows) is
intersected with those IDs. The gateway adds `canonical_employee_id` and
`canonical_employee_name` to each surviving metric row and overwrites a
conflicting supplied name with the live directory value. A rejected or
normalized source row makes the response `partial`; it can never reappear in a
model-generated report as a plausible employee.

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
historical date instead of relabeling current data; unavailable employee
snapshots also make coverage `partial`. Criterion output limits report the
matching and returned counts and cannot silently truncate an `ok` result.

Transcript search is deliberately bounded by `TRANSCRIPT_SEARCH_MAX_CALLS`.
Every response reports `scanned_calls`, `candidate_calls_loaded`,
`source_calls_total`, `source_complete` and `result_complete`. A cap or early
result limit produces `partial`; it must not be interpreted as an exhaustive
absence. Transcript payloads include no structured phone/audio/PBX fields, and
segment output is projected to `speaker`, `text`, `start` and `end` only.

Aggregate strengths and growth areas count distinct employees mentioning a
normalized observation. Repeated or differently cased copies of the same
observation inside one employee card do not inflate `employee_mentions`.

The current OKK employee-page API returns at most five active and ten completed
mentoring tasks per employee. Task tools therefore mark this window as
`complete=false` and return `partial`; they never present it as full history.
# Department reporting contract (1.2.3 candidate)

`get_department_statistics.data.reporting_contract` supplies source policy,
metric definitions and response rules alongside the existing full department
payload. No tool or OAuth scope is added. `calls_total` is the successful-call
population; `total_calls` is duration-qualified, while `calls_evaluated` requires
an actual KPI score. Average duration does not require a score. Average quality
must not be recomputed by averaging employee means.

`get_plan_fact_statistics.data.totals` and the department `plan_fact` preserve
null when no plan is set for a metric; assigned zero remains zero. `coverage`
reports employees with/without a plan per metric, using the visible roster as
the denominator. A sum with missing assignments is not a complete team target.
