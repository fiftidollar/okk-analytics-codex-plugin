# Codex directory submission test cases

The list intentionally contains exactly five positive and three negative cases.

## Positive (plugin should be used)

1. **Prompt:** «Покажи сотрудников отдела B2B и их оценки за текущий месяц».
   **Expected:** call `get_department_statistics` with `department_ref="B2B"`;
   report rows only if `effective_scope` resolves to B2B.
2. **Prompt:** «Какие сильные стороны и зоны роста у сотрудников ORD?»
   **Expected:** call `get_growth_insights` with `department_ref="ORD"` and
   preserve completeness metadata.
3. **Prompt:** «Сравни отделы B2B и Отдел региональных продаж за квартал».
   **Expected:** call `compare_departments` with both exact values in
   `department_refs`; mention only the effective visible scopes.
4. **Prompt:** «Покажи карточку этого сотрудника, включая KPI и задачи» after an
   ACL-safe employee lookup. **Expected:** call `get_employee_card`; preserve
   the bounded mentoring-history marker.
5. **Prompt:** «Какие сценарии и критерии работают хуже всего в этом месяце?»
   **Expected:** use scenario/criterion catalog and performance tools without
   requesting prompts, scripts or raw reasoning.

## Negative (plugin should not be used)

1. **Prompt:** «Скачай аудиозаписи и расшифровки всех звонков».
   **Expected:** do not call OKK Analytics; audio and transcripts are excluded.
2. **Prompt:** «Измени планы сотрудникам и создай наставнические задачи».
   **Expected:** do not call OKK Analytics; every tool is read-only.
3. **Prompt:** «Покажи настройки Megafon, маршрутизацию и сырой reasoning».
   **Expected:** do not call OKK Analytics; these surfaces are excluded.
