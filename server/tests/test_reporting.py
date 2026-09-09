"""Регресс различий между отсутствующим, нулевым и частичным планом."""

import pytest

from okk_mcp.reporting import aggregate_plan_totals


@pytest.mark.parametrize(
    "rows,expected,total,assigned,missing",
    [
        ([], 0, None, 0, 0),
        ([{"plan_total": None}], 1, None, 0, 1),
        ([{"plan_total": 0}], 1, 0, 1, 0),
        ([{"plan_total": 10}, {"plan_total": None}], 2, 10, 1, 1),
        ([{"plan_total": 10}], 2, 10, 1, 1),
        ([{"plan_total": 10}, {"plan_total": 20}], 2, 30, 2, 0),
    ],
)
def test_plan_totals_preserve_absence_and_coverage(rows, expected, total, assigned, missing):
    result = aggregate_plan_totals(rows, expected)
    assert result["totals"]["plan_total"] == total
    assert result["coverage"]["plan_total"] == {
        "employees_with_plan": assigned,
        "employees_without_plan": missing,
    }
    assert result["totals"]["plan_inbound"] is None


def test_release_gate_rejects_fabricated_zero_plan():
    from okk_mcp.reporting import department_report_contract
    from scripts.smoke_release import validate_department_report_semantics

    plan = aggregate_plan_totals([{"plan_total": None}], 1)
    plan["employees"] = [{"plan_total": None}]
    payload = {
        "result": {
            "structuredContent": {
                "employee_roster_grounding": {"employee_count": 1},
                "data": {"reporting_contract": department_report_contract(), "plan_fact": plan},
            }
        }
    }
    validate_department_report_semantics(payload)
    plan["totals"]["plan_total"] = 0
    with pytest.raises(RuntimeError, match="unset"):
        validate_department_report_semantics(payload)
