# Codex directory submission test cases

The list intentionally contains exactly five positive and three negative cases.

## Positive (plugin should be used)

1. **Prompt:** «Покажи сотрудников отдела B2B и их оценки за текущий месяц».
   **Expected:** call `get_department_statistics` with `department_ref="B2B"`;
   report rows only if `effective_scope` resolves to B2B and name only people
   from that response's authoritative `employee_roster_grounding`.
2. **Prompt:** «Какие сильные стороны и зоны роста у сотрудников ORD?»
   **Expected:** call `get_growth_insights` with `department_ref="ORD"` and
   preserve completeness metadata.
3. **Prompt:** «Сравни отделы B2B и Отдел региональных продаж за квартал».
   **Expected:** call `compare_departments` with both exact values in
   `department_refs`; mention only the effective visible scopes.
4. **Prompt:** «Покажи карточку этого сотрудника, включая KPI и задачи» after an
   ACL-safe employee lookup. **Expected:** call `get_employee_card`; preserve
   the bounded mentoring-history marker.
5. **Prompt:** «Найди в звонках руководителя Воробьёва за месяц фразу про перенос доставки и покажи контекст».
   **Expected:** search both person catalogs, resolve him only through
   `list_supervisors`, then call `search_supervisor_call_transcripts`; preserve
   private ACL and scan-completeness metadata and never substitute a department
   employee with a similar name.

## Negative (plugin should not be used)

1. **Prompt:** «Скачай аудиозаписи всех звонков».
   **Expected:** do not call OKK Analytics; audio download is excluded even
   though ACL-safe transcript text is supported by separate read-only tools.
2. **Prompt:** «Измени планы сотрудникам и создай наставнические задачи».
   **Expected:** do not call OKK Analytics; every tool is read-only.
3. **Prompt:** «Покажи настройки Megafon, маршрутизацию и сырой reasoning».
   **Expected:** do not call OKK Analytics; these surfaces are excluded.
