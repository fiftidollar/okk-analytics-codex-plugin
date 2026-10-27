---
name: okk-analytics
description: Reads OKK call-quality analytics (ОКК) of the connected account through the okk-analytics MCP server. Use when the user asks about ОКК, звонки, оценки и средний балл, отделы, сотрудники и их карточки, KPI, план звонков, клиенты, CRM, критерии и сценарии, расшифровки и поиск по разговорам, руководители.
---

# OKK Analytics

The `okk-analytics` MCP server gives read-only access to OKK, the system that
evaluates employees' calls. Each tool description says what it returns; this
skill says in which order to use them and how to read the numbers.

## Connection and safety

- Start every task with `get_access_context`. Only its answer with
  `authenticated=true` proves the connection; a browser redirect alone does
  not. Then say `OKK подключён`, the role and the visible departments.
- Authentication happens only on the OKK authorization page (OAuth). Never ask
  for or accept an OKK login or password in chat, and never put credentials into
  tool arguments, files or environment variables.
- Every tool is read-only. Plans, scores, people and settings cannot be
  changed here: say so instead of proposing a write action.
- Out of scope: audio, raw prompts and prompt runtime, raw AI reasoning,
  scripts, Megafon administration, processing pipeline state, routing, bulk
  operations.
- Transcripts are sensitive: read them only with the transcript tools
  (`list_call_transcripts`, `get_call_transcript`, `search_call_transcripts`,
  scope `okk.transcripts.read`), and copy them into a file only when the user
  asks for one. Client phone numbers come only from `list_call_phone_records`.

## Departments

- `get_access_context.data.departments` is the live catalog with each
  department's norm (`quality_threshold`) and minimum call duration. Never
  hardcode department names or rely on remembered ones.
- Pass the department the user named as `department` (code or name from that
  catalog). If the wording matches no single visible department, show the
  visible choices and ask. Never drop a requested department or widen it to
  all departments.
- `not_available` for a named department: say it is not available to this
  account, list the visible ones, and stop. It never tells whether something
  exists elsewhere.

## Department report

For «отчёт по отделу», «сотрудники отдела и их оценки» call
`get_department_statistics(department=...)` once. It holds the summary, every
employee's metrics, the plan and `reporting_contract` (field meanings). Name
only people from `employee_roster_grounding` of that answer — never from
memory, examples or files. If a name is disputed, look it up again with
`list_employees` for the same department and rebuild the answer from the new
response only.

## People

- An employee: `list_employees(search=...)`, then `get_employee_card` or
  `compare_employees` with the returned ids. A name that is not found is not
  in OKK: say so; never substitute a similar person.
- Supervisors (`Руководители`) are a separate private section, shown in
  `get_access_context.data.available_sections.supervisors`. Only when it is
  available, also search `list_supervisors`. Supervisors are
  `transcription_only`: use `get_supervisor_call_statistics`,
  `list_supervisor_call_transcripts`, `get_supervisor_call_transcript` and
  `search_supervisor_call_transcripts`; they have no scores. Never hardcode a
  supervisor or grant.
- OKK keeps no mentoring tasks and no saved focus.

## Calls and transcripts

- To find a call: `list_call_transcripts` (filters, newest first). To read it:
  `get_call_transcript`, repeating with `next_start_char` until it is null —
  a preview or one chunk is not the whole text.
- To find words: `search_call_transcripts`. It searches every call of the
  period; `matching_calls_total` is the exact count. If `result_complete=false`,
  read the next `page`.
- Calls by phone number: `list_call_phone_records`; the count is
  `matching_calls_total`, and the default period is the whole history.

## Reading the numbers

- No period from the user: `month`, and say it was the current month. Any
  other range: `start_date` and `end_date`, without `period`.
- `no_data`, `not_available`, `null` and `partial` are not zero. Say what is
  missing instead of printing 0.
- Successful calls (`calls_total`), calls from the minimum duration
  (`total_calls`) and evaluated calls (`calls_evaluated`) are different
  counts; the average score is over evaluated calls, never an average of
  employee averages.
- A call plan is a list of items; report each item, never add them up.
  `mode=null` is no plan, `fact=null` is no data.
- Criteria: `data.criteria` are ordinary criteria (average percent of the
  maximum, weakest first). Critical errors (стоп-слова, мат, грубость) are in
  `data.critical_errors` as `violations` — calls with a deduction.
- CRM is the latest snapshot per employee; an unlinked or unsynced CRM is
  unavailable, not zero.
- A scenario counts all its versions; non-zero
  `effective_scope.excluded_evaluations` means some evaluations could not be
  attributed — say so.
