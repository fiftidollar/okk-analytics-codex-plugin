---
name: okk-analytics
description: Use the connected OKK account for read-only business statistics, employee cards, AI strengths and growth areas, mentoring tasks, plans, CRM, scenarios and criteria.
---

# OKK Analytics

Use this skill when the user asks about OKK statistics, calls, KPI, departments,
employees, employee cards, client work, plan/fact, CRM, AI strengths, growth
areas, weekly focus, mentoring tasks, scenarios, criteria or their performance.

## Authentication and safety

- Use only tools from the `okk-analytics` MCP server for OKK data.
- Authentication happens on the OKK authorization page through OAuth 2.1.
- Never ask the user to send an OKK login or password in chat.
- Never place credentials in tool arguments, prompts, files or environment
  variables on the user's behalf.
- Treat every tool as read-only. Do not propose or simulate writes through this
  plugin.
- Respect `access_context`, `effective_scope`, `omitted_filters_count` and
  `status` in every response.
- `not_available` deliberately does not distinguish a missing entity from an
  inaccessible one. Never infer or reveal which case occurred.
- If a viewer has no assigned departments, an empty successful response is the
  correct result.

## Tool routing

Start broad questions with `get_access_context` and
`get_statistics_catalog` when the scope or available metrics is unclear.

- Company/overall KPI, ranking and trends: `get_overview_statistics`.
- Department discovery and comparison: `list_departments`,
  `get_department_statistics`, `compare_departments`.
- Employee discovery and full card: `list_employees`, `get_employee_card`,
  `compare_employees`.
- Calls, duration, scores and day trend: `get_call_statistics`.
- Plans and actual completion: `get_plan_fact_statistics`.
- New/regular client contacts: `get_client_statistics`.
- Bitrix CRM snapshot statistics: `get_crm_statistics`.
- AI strengths and growth observations: `get_growth_insights`.
- Mentoring task history and completion statistics:
  `get_mentoring_statistics`.
- Scenario discovery and business criteria: `list_scenarios`,
  `get_scenario_criteria`.
- Scenario and criterion results: `get_scenario_performance`,
  `get_criterion_performance`.

For a full employee answer, prefer `get_employee_card`: it already includes KPI,
client/plan facts, CRM, saved and weekly focus, AI strengths and growth areas,
active tasks and the latest completed-task window exposed by the current OKK
API. Use `get_mentoring_statistics` for cross-employee rollups, and preserve its
`partial`/history-window metadata instead of claiming that older tasks were
loaded.

## Period and interpretation rules

- If the user gives no period, use `month` and say that the current month was
  used.
- Use `custom` only with both `start_date` and `end_date` in `YYYY-MM-DD`.
- Preserve OKK metric names and canonical meanings. Do not recalculate KPI from
  unrelated counters in the model.
- Explain partial availability metadata inline when it materially affects the
  answer; do not turn it into a separate data-quality report.
- For comparisons, report which requested filters were omitted only as a count.
  Do not repeat hidden IDs.

## Explicitly out of scope

Do not request or expose audio, transcripts, raw prompts, prompt runtime, raw AI
reasoning, scripts, Megafon administration, processing pipeline state, routing,
bulk operations or any write action. These exclusions are intentional even if
another OKK endpoint happens to contain such data.
