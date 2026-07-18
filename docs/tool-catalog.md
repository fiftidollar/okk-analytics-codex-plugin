# Tool catalog

All tools have `readOnlyHint=true`, `destructiveHint=false`,
`idempotentHint=true` and `openWorldHint=false`.

| Tool | Data |
|---|---|
| `get_access_context` | Authenticated connection proof, current role and visible departments |
| `get_statistics_catalog` | Metric domains and explicit exclusions |
| `get_overview_statistics` | Overall KPI, clients, department rollup, ranking and trend |
| `list_departments` | Visible departments and KPI settings |
| `get_department_statistics` | One department's KPI, plan/fact, employees and trend |
| `compare_departments` | Visible department metrics and trends |
| `list_employees` | Safe employee directory without credentials or phone fields |
| `get_employee_card` | KPI, plan/client/CRM, strengths, growth, focus and task windows |
| `compare_employees` | KPI, strengths, growth, focus and task-count comparison |
| `get_call_statistics` | Call volume, evaluated count, scores, pass rate, duration and trend |
| `get_plan_fact_statistics` | Total/inbound/outbound/new/regular plans and daily rows |
| `get_client_statistics` | New/regular contacts, repeats, missed/no-answer and related facts |
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

## Common response envelope

- `status`: `ok`, `partial`, `no_data`, `not_available` or
  `temporarily_unavailable`.
- `access_context`: only the caller's current visible scope.
- `effective_scope`: filters actually applied.
- `period`: exact inclusive dates.
- `omitted_filters_count`: number of inaccessible mixed-list filters, without
  echoing their IDs.
- `request_id`: correlation ID for the gateway's redacted operational trace.
- `data`: the business payload.

`get_access_context` is the canonical post-login check. Because the tool can be
called only with a valid MCP OAuth token and revalidates `/auth/me`, its
`data.authenticated=true` result proves that the connection completed. In a new
task Codex must call it first and explicitly tell the user `OKK подключён`, then
show only the role and departments returned by that call. A browser redirect
alone is not treated as proof.

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

Aggregate strengths and growth areas count distinct employees mentioning a
normalized observation. Repeated or differently cased copies of the same
observation inside one employee card do not inflate `employee_mentions`.

The current OKK employee-page API returns at most five active and ten completed
mentoring tasks per employee. Task tools therefore mark this window as
`complete=false` and return `partial`; they never present it as full history.
