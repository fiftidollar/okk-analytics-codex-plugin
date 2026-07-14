# Tool catalog

All tools have `readOnlyHint=true`, `destructiveHint=false`,
`idempotentHint=true` and `openWorldHint=false`.

| Tool | Data |
|---|---|
| `get_access_context` | Current role and visible departments |
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
| `get_scenario_criteria` | Categories, weights, scales, indicators and applicability |
| `get_scenario_performance` | Evaluation count, employee coverage, score distribution and pass rate |
| `get_criterion_performance` | Observation count, score %, penalties, coverage and recency |

## Common response envelope

- `status`: `ok`, `partial`, `no_data`, `not_available` or
  `temporarily_unavailable`.
- `access_context`: only the caller's current visible scope.
- `effective_scope`: filters actually applied.
- `period`: exact inclusive dates.
- `omitted_filters_count`: number of inaccessible mixed-list filters, without
  echoing their IDs.
- `data`: the business payload.

The current OKK employee-page API returns at most five active and ten completed
mentoring tasks per employee. Task tools therefore mark this window as
`complete=false` and return `partial`; they never present it as full history.
