"""План-факт: режим периода, безопасные пункты, неизвестный факт не превращается в ноль."""

import pytest

from okk_mcp.reporting import department_report_contract, plan_mode, safe_plan_fact
from scripts.smoke_release import validate_department_report_semantics

DEPARTMENT_ITEM = {
    "employee_id": None,
    "direction": "outbound",
    "clients": "new",
    "counting": "successful",
    "plan": 16,
    "fact": None,
    "fact_state": "no_client_base",
    "contribution": None,
    "min_call_duration_seconds": [],
    "changes": [
        {"at": "2026-10-13T09:00:00Z", "author": "Руководитель", "kind": "items", "was": 30, "now": 40}
    ],
}


@pytest.mark.parametrize(
    "modes,expected",
    [
        ([], None),
        ([None, None], None),
        (["department", None], "department"),
        (["employees", "employees"], "employees"),
        (["department", "employees"], "mixed"),
    ],
)
def test_plan_mode_of_period(modes, expected):
    assert plan_mode([{"mode": mode} for mode in modes]) == expected


def test_safe_plan_fact_hides_change_authors_and_keeps_unknown_fact():
    plan = safe_plan_fact(
        {
            "department_min_call_duration_seconds": 90,
            "weeks": [
                {
                    "week_start": "2026-10-12",
                    "mode": "department",
                    "min_call_duration_seconds": 60,
                    "min_duration_differs": True,
                }
            ],
            "items": [DEPARTMENT_ITEM],
            "employees": [],
        }
    )
    assert plan["mode"] == "department"
    item = plan["items"][0]
    assert item["fact"] is None and item["fact_state"] == "no_client_base"
    assert item["changed"] is True
    assert "changes" not in item and "Руководитель" not in str(plan)


def test_safe_plan_fact_without_plan_is_not_zero():
    plan = safe_plan_fact(
        {"weeks": [{"week_start": "2026-10-12", "mode": None}], "items": [], "employees": []}
    )
    assert plan["mode"] is None
    assert plan["items"] == []


def _department_payload(plan):
    return {
        "result": {
            "structuredContent": {
                "employee_roster_grounding": {"employee_count": 0},
                "data": {"reporting_contract": department_report_contract(), "plan_fact": plan},
            }
        }
    }


def test_release_gate_rejects_fabricated_zero_fact():
    plan = safe_plan_fact(
        {"weeks": [{"week_start": "2026-10-12", "mode": "department"}], "items": [DEPARTMENT_ITEM]}
    )
    validate_department_report_semantics(_department_payload(plan))
    plan["items"][0]["fact"] = 0
    with pytest.raises(RuntimeError, match="unavailable fact"):
        validate_department_report_semantics(_department_payload(plan))


def test_release_gate_rejects_change_history():
    plan = safe_plan_fact(
        {"weeks": [{"week_start": "2026-10-12", "mode": "department"}], "items": [DEPARTMENT_ITEM]}
    )
    plan["items"][0]["changes"] = DEPARTMENT_ITEM["changes"]
    with pytest.raises(RuntimeError, match="edit history"):
        validate_department_report_semantics(_department_payload(plan))


def test_release_gate_accepts_platform_without_call_plans():
    validate_department_report_semantics(_department_payload({"status": "not_available"}))
