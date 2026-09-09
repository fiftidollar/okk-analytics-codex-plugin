"""Явная семантика показателей для клиентских отчётов."""

from typing import Any


def department_report_contract() -> dict[str, Any]:
    """Названия похожих полей не должны подменять разные выборки звонков."""
    return {
        "source_policy": "Use only this response's live roster and metrics for the requested department and period.",
        "metric_definitions": {
            "summary.calls_total": "Состоявшиеся звонки любой длительности; не все записи журнала. Без недозвонов и пропущенных.",
            "summary.calls_outbound": "Состоявшиеся исходящие любой длительности.",
            "summary.calls_inbound": "Состоявшиеся входящие любой длительности.",
            "summary.total_calls": "Состоявшиеся звонки от порога длительности отдела; это не summary.calls_total.",
            "summary.calls_evaluated": "Звонки, реально оценённые для KPI: состоявшиеся, от порога длительности, с оценкой и без skip_reason.",
            "summary.average_score": "Средний балл реально оценённых звонков; не среднее арифметическое средних баллов сотрудников.",
            "summary.avg_duration": "Секунды; средняя длительность состоявшихся звонков от порога длительности, независимо от наличия оценки.",
            "summary.passing_rate": "Процент реально оценённых звонков с баллом не ниже quality_threshold.",
            "complete_employee_ranking.employees.metrics.calls_total": "Состоявшиеся звонки сотрудника любой длительности, без недозвонов и пропущенных.",
            "complete_employee_ranking.employees.metrics.actual_total": "Состоявшиеся звонки сотрудника от порога длительности; не обязательно все имеют оценку.",
            "plan_fact.totals": "null означает отсутствие заданных значений; 0 является заданным нулевым планом. Проверять coverage для каждой метрики.",
        },
        "response_rules": [
            "Use precise Russian labels from metric_definitions; never label successful calls as all journal records.",
            "Keep null scores and plans unavailable; do not fabricate zero, completion percentages or missing employees.",
            "After a disputed name, recheck the same department directory. Do not offer external files as a way to establish OKK membership or scores.",
            "Do not infer causes of low scores from aggregates; use actual call or criterion evidence before making causal claims.",
        ],
    }


def aggregate_plan_totals(rows: list[dict[str, Any]], expected_employees: int) -> dict[str, Any]:
    """Сохраняет отсутствие плана и покрытие вместо искусственных нулей."""
    totals: dict[str, float | None] = {}
    coverage: dict[str, dict[str, int]] = {}
    for key in ("plan_total", "plan_outbound", "plan_inbound", "plan_outbound_new", "plan_outbound_regular"):
        values = [
            row[key]
            for row in rows
            if isinstance(row.get(key), int | float) and not isinstance(row[key], bool)
        ]
        totals[key] = sum(values) if values else None
        coverage[key] = {
            "employees_with_plan": len(values),
            "employees_without_plan": max(0, expected_employees - len(values)),
        }
    return {"totals": totals, "coverage": coverage}
