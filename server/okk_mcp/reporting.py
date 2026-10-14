"""Явная семантика показателей для клиентских отчётов."""

from typing import Any

# План звонков отдела на неделю (OKK `/call-plans/plan-fact`): смысл полей для ответа модели.
PLAN_FACT_DEFINITIONS: dict[str, str] = {
    "plan_fact.mode": (
        "department — один план на весь отдел, факт — звонки всех сотрудников; employees — личные "
        "планы, план отдела — их сумма; mixed — в периоде недели с разными режимами; null — плана нет."
    ),
    "plan_fact.items": (
        "Пункты плана за период. Каждый пункт — направление × клиенты × что засчитывать × число "
        "звонков; пункты не складываются между собой."
    ),
    "plan_fact.items.direction": "all — все звонки, outbound — исходящие, inbound — входящие.",
    "plan_fact.items.clients": (
        "all — все клиенты, new — новые (номера нет в базе клиентов месяца), existing — действующие."
    ),
    "plan_fact.items.counting": (
        "connected — дозвоны по данным телефонии (трубку взяли, живой разговор не нужен); "
        "successful — состоявшиеся (живой разговор менеджера с клиентом); successful_from_min — "
        "состоявшиеся не короче min_call_duration_seconds; evaluated — только оценённые."
    ),
    "plan_fact.items.plan": (
        "План за период: план недели делится на рабочие дни пн–пт, остаток — на первые дни; "
        "неделя целиком — весь план."
    ),
    "plan_fact.items.fact": (
        "Факт за дни периода. null при fact_state=no_client_base: нет базы клиентов за месяц — "
        "не ноль и не выполнение."
    ),
    "plan_fact.items.contribution": "Только в карточке сотрудника при плане на отдел: его звонки в факте отдела.",
    "plan_fact.items.changed": "План пункта меняли после начала недели; неделя считается по последнему числу.",
    "plan_fact.weeks.min_duration_differs": (
        "Текущая неделя считается по прежней минимальной длительности звонка, в отделе уже другая; "
        "новая начнёт действовать со следующей недели."
    ),
}

PLAN_ITEM_FIELDS = (
    "employee_id",
    "direction",
    "clients",
    "counting",
    "min_call_duration_seconds",
    "plan",
    "fact",
    "fact_state",
    "contribution",
)
PLAN_WEEK_FIELDS = ("week_start", "mode", "min_call_duration_seconds", "min_duration_differs")


def safe_plan_item(item: dict[str, Any]) -> dict[str, Any]:
    """Пункт плана без истории правок: кто правил план — не аналитика, наружу не отдаётся."""
    safe = {key: item.get(key) for key in PLAN_ITEM_FIELDS if key in item}
    safe["changed"] = bool(item.get("changes"))
    return safe


def plan_mode(weeks: list[dict[str, Any]]) -> str | None:
    """Режим плана за период: один режим недель с планом, mixed — разные, None — плана нет."""
    modes = {week.get("mode") for week in weeks if week.get("mode")}
    if not modes:
        return None
    return modes.pop() if len(modes) == 1 else "mixed"


def safe_plan_fact(report: dict[str, Any]) -> dict[str, Any]:
    """Ответ `/call-plans/plan-fact` в форме отчёта: режим, недели, пункты, личные пункты."""
    weeks = [
        {key: week.get(key) for key in PLAN_WEEK_FIELDS}
        for week in report.get("weeks") or []
        if isinstance(week, dict)
    ]
    return {
        "mode": plan_mode(weeks),
        "department_min_call_duration_seconds": report.get("department_min_call_duration_seconds"),
        "weeks": weeks,
        "items": [safe_plan_item(item) for item in report.get("items") or [] if isinstance(item, dict)],
        "employees": [
            {
                "employee_id": str(entry.get("employee_id")),
                "full_name": entry.get("full_name"),
                "items": [
                    safe_plan_item(item) for item in entry.get("items") or [] if isinstance(item, dict)
                ],
            }
            for entry in report.get("employees") or []
            if isinstance(entry, dict) and entry.get("employee_id")
        ],
    }


PLAN_FACT_RESPONSE_RULES = [
    "Report every plan item separately; never add items together — they count different calls.",
    "fact=null with fact_state=no_client_base is unavailable, not zero and not plan completion.",
    "A department or week without a plan (mode=null) has no plan; do not present it as a zero plan.",
]


def plan_fact_report_contract() -> dict[str, Any]:
    """Смысл пунктов плана для ответа по `get_plan_fact_statistics`."""
    return {
        "source_policy": "Use only this response's plan items for the requested departments and period.",
        "metric_definitions": dict(PLAN_FACT_DEFINITIONS),
        "response_rules": list(PLAN_FACT_RESPONSE_RULES),
    }


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
            **PLAN_FACT_DEFINITIONS,
        },
        "response_rules": [
            "Use precise Russian labels from metric_definitions; never label successful calls as all journal records.",
            "Keep null scores and plans unavailable; do not fabricate zero, completion percentages or missing employees.",
            *PLAN_FACT_RESPONSE_RULES,
            "After a disputed name, recheck the same department directory. Do not offer external files as a way to establish OKK membership or scores.",
            "Do not infer causes of low scores from aggregates; use actual call or criterion evidence before making causal claims.",
        ],
    }
