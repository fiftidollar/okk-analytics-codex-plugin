"""Standalone adapter safety and projection tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from okk_mcp.backend_client import AnalyticsAdapter, BackendClient
from okk_mcp.config import Settings
from okk_mcp.crypto import SessionCipher
from okk_mcp.models import OAuthAuthorizationCode, OAuthToken, OkkAccountSession
from okk_mcp.platform_client import AccountContext


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakePlatform:
    def __init__(self, *, role="viewer", department_ids=(), responses=None):
        self.context = AccountContext(
            session_id=uuid4(),
            user_id="user-1",
            role=role,
            department_ids=tuple(department_ids),
            access_token="upstream-access",
        )
        self.responses = responses or {}
        self.calls: list[tuple[str, list[tuple[str, str]]]] = []

    async def live_context(self, _session_id):
        return self.context

    async def get(self, _session_id, path, *, params=None):
        params = params or []
        self.calls.append((path, params))
        value = self.responses[path]
        return value(params) if callable(value) else value

    async def get_with_context(self, _context, path, *, params=None):
        return await self.get(None, path, params=params)


def adapter(platform: FakePlatform) -> AnalyticsAdapter:
    return AnalyticsAdapter(platform, str(platform.context.session_id), Settings())


@pytest.mark.anyio
async def test_access_context_is_the_definitive_authenticated_connection_confirmation():
    department_id = str(uuid4())
    platform = FakePlatform(
        role="viewer",
        department_ids=(department_id,),
        responses={
            "/departments": [{"id": department_id, "name": "Отдел региональных продаж", "code": "ord"}]
        },
    )

    result = await adapter(platform).get_access_context()

    assert result["status"] == "ok"
    assert result["data"] == {
        "authenticated": True,
        "connection_status": "connected",
        "confirmation_message": "OKK подключён. Авторизация подтверждена.",
        "role": "viewer",
        "all_departments": False,
        "departments": [{"id": department_id, "name": "Отдел региональных продаж", "code": "ord"}],
    }
    assert result["access_context"] == {
        "role": "viewer",
        "all_departments": False,
        "departments": [{"id": department_id, "name": "Отдел региональных продаж", "code": "ord"}],
    }
    assert platform.calls == [("/departments", [])]


@pytest.mark.anyio
async def test_empty_viewer_acl_is_a_valid_empty_scope_without_data_queries():
    platform = FakePlatform(role="viewer", department_ids=())
    result = await adapter(platform).list_employees()
    assert result["status"] == "no_data"
    assert result["access_context"] == {
        "role": "viewer",
        "all_departments": False,
        "departments": [],
    }
    assert platform.calls == []


@pytest.mark.anyio
async def test_viewer_defense_in_depth_drops_departments_not_present_in_live_claims():
    visible_id, leaked_id = str(uuid4()), str(uuid4())
    platform = FakePlatform(
        department_ids=(visible_id,),
        responses={
            "/departments": [
                {"id": visible_id, "name": "ORD", "code": "ord"},
                {"id": leaked_id, "name": "B2B", "code": "b2b"},
            ]
        },
    )

    result = await adapter(platform).list_departments()

    assert [row["id"] for row in result["data"]] == [visible_id]
    assert leaked_id not in str(result)


@pytest.mark.anyio
async def test_employee_projection_never_exposes_account_or_phone_fields():
    department_id = str(uuid4())
    employee_id = str(uuid4())
    platform = FakePlatform(
        department_ids=(department_id,),
        responses={
            "/departments": [{"id": department_id, "name": "Продажи", "code": "sales"}],
            "/employees": {
                "items": [
                    {
                        "id": employee_id,
                        "full_name": "Иван Иванов",
                        "department_id": department_id,
                        "department": {"id": department_id, "name": "Продажи", "code": "sales"},
                        "email": "secret@example.com",
                        "phone": "+79990000000",
                        "megafon_phone": "+79990000000",
                        "password_hash": "never",
                        "is_active": True,
                    }
                ],
                "total": 1,
                "page": 1,
                "pages": 1,
            },
        },
    )
    result = await adapter(platform).list_employees()
    serialized = str(result)
    assert "Иван Иванов" in serialized
    assert "secret@example.com" not in serialized
    assert "+79990000000" not in serialized
    assert "password_hash" not in serialized
    assert "megafon" not in serialized.lower()


@pytest.mark.anyio
async def test_hidden_direct_id_returns_neutral_not_available():
    visible_id = str(uuid4())
    hidden_id = str(uuid4())
    platform = FakePlatform(
        department_ids=(visible_id,),
        responses={
            "/departments": [{"id": visible_id, "name": "Visible", "code": "visible"}],
        },
    )
    result = await adapter(platform).get_department_statistics(hidden_id)
    assert result["status"] == "not_available"
    assert hidden_id not in str(result["data"])


@pytest.mark.anyio
async def test_scenario_projection_keeps_business_criteria_but_drops_prompts_and_scripts():
    department_id = str(uuid4())
    scenario_id = str(uuid4())
    criterion_id = str(uuid4())
    scenario = {
        "id": scenario_id,
        "department_id": department_id,
        "name": "Новый клиент",
        "code": "new_client",
        "passing_score": 80,
        "is_active": True,
        "prompts": [{"prompt_text": "secret prompt"}],
        "scripts": [{"script_text": "secret script"}],
        "categories": [
            {
                "id": str(uuid4()),
                "name": "Контакт",
                "sort_order": 1,
                "items": [
                    {
                        "id": criterion_id,
                        "name": "Выявление потребности",
                        "description": "Критерий",
                        "max_score": 10,
                        "positive_indicators": "Задал вопросы",
                        "negative_indicators": "Не выяснил",
                        "is_required": True,
                        "is_critical": False,
                        "sort_order": 1,
                    }
                ],
            }
        ],
    }
    platform = FakePlatform(
        department_ids=(department_id,),
        responses={
            "/departments": [{"id": department_id, "name": "Продажи", "code": "sales"}],
            "/scenarios": {"items": [scenario], "pages": 1},
        },
    )
    result = await adapter(platform).get_scenario_criteria(scenario_id)
    serialized = str(result)
    assert result["status"] == "ok"
    assert result["effective_scope"]["department_id"] == department_id
    assert "Выявление потребности" in serialized
    assert "Задал вопросы" in serialized
    assert "secret prompt" not in serialized
    assert "secret script" not in serialized
    assert "prompts" not in serialized
    assert "scripts" not in serialized


@pytest.mark.anyio
async def test_employee_card_contains_growth_and_tasks_without_raw_reasoning():
    department_id = str(uuid4())
    employee_id = str(uuid4())
    responses = {
        "/departments": [{"id": department_id, "name": "Продажи", "code": "sales"}],
        "/employees": {
            "items": [
                {
                    "id": employee_id,
                    "full_name": "Иван",
                    "department_id": department_id,
                    "department": {"id": department_id, "name": "Продажи", "code": "sales"},
                    "is_active": True,
                }
            ],
            "pages": 1,
        },
        f"/employees/{employee_id}": {
            "id": employee_id,
            "full_name": "Иван",
            "department_id": department_id,
            "department": {"id": department_id, "name": "Продажи", "code": "sales"},
            "is_active": True,
        },
        f"/employees/{employee_id}/page-data": {
            "kpi": {"calls_total": 12, "avg_quality_score": 88},
            "aggregated_strengths": ["Сильное открытие"],
            "aggregated_improvements": ["Уточнять потребность"],
            "weekly_focus": ["Работа с возражениями"],
            "focus_text": "Фокус наставника",
            "mentoring_tasks": [{"id": "t1", "status": "in_progress", "focus_area": "Вопросы"}],
            "completed_tasks": [{"id": "t2", "status": "completed", "focus_area": "Контакт"}],
            "evaluation_reasoning": "must never leave adapter",
            "transcript_raw": "must never leave adapter",
        },
        f"/employees/{employee_id}/summary": {"calls_total": 12, "new_client_contacts_total": 4},
        f"/plans/employee/{employee_id}/total": {"plan_total": 20},
        f"/employees/{employee_id}/bitrix-metrics": {
            "status": "ok",
            "total_open_deals": 3,
            "error_message": "internal integration debug must not leave adapter",
        },
    }
    platform = FakePlatform(department_ids=(department_id,), responses=responses)
    result = await adapter(platform).get_employee_card(employee_id)
    serialized = str(result)
    assert result["status"] == "partial"  # upstream exposes a bounded task-history window
    assert "Сильное открытие" in serialized
    assert "Уточнять потребность" in serialized
    assert "Фокус наставника" in serialized
    assert "in_progress" in serialized and "completed" in serialized
    assert "must never leave adapter" not in serialized
    assert "reasoning" not in serialized.lower()
    assert "transcript" not in serialized.lower()
    assert "internal integration debug" not in serialized


@pytest.mark.anyio
async def test_criterion_performance_aggregates_safe_items_only():
    department_id = str(uuid4())
    scenario_id = str(uuid4())
    criterion_id = str(uuid4())
    call_id = str(uuid4())
    responses = {
        "/departments": [{"id": department_id, "name": "Продажи", "code": "sales"}],
        "/scenarios": {
            "items": [
                {
                    "id": scenario_id,
                    "department_id": department_id,
                    "name": "Сценарий",
                    "code": "s",
                    "categories": [
                        {
                            "id": str(uuid4()),
                            "name": "Категория",
                            "items": [
                                {
                                    "id": criterion_id,
                                    "name": "Критерий",
                                    "max_score": 10,
                                }
                            ],
                        }
                    ],
                }
            ],
            "pages": 1,
        },
        "/calls": {
            "items": [
                {
                    "id": call_id,
                    "employee_id": str(uuid4()),
                    "scenario_id": scenario_id,
                    "quality_score": 80,
                    "started_at": "2026-07-01T10:00:00Z",
                    "audio_url": "secret",
                    "caller_number": "+70000000000",
                }
            ],
            "total": 1,
            "pages": 1,
        },
        f"/calls/{call_id}": {
            "transcript_raw": "secret transcript",
            "evaluation": {
                "evaluation_reasoning": "secret reasoning",
                "items": [
                    {
                        "checklist_item_id": criterion_id,
                        "name": "Критерий",
                        "score": 8,
                        "max_score": 10,
                        "is_penalty": False,
                        "comment": "not returned",
                    }
                ],
            },
        },
    }
    platform = FakePlatform(department_ids=(department_id,), responses=responses)
    result = await adapter(platform).get_criterion_performance(
        scenario_id=scenario_id, period="custom", start_date="2026-07-01", end_date="2026-07-02"
    )
    serialized = str(result)
    assert result["data"][0]["avg_score_percent"] == 80.0
    assert "secret" not in serialized
    assert "+70000000000" not in serialized
    assert "comment" not in serialized


@pytest.mark.anyio
async def test_unfiltered_performance_never_reports_negative_omitted_filters():
    department_id = str(uuid4())
    scenario_id = str(uuid4())
    criterion_id = str(uuid4())
    responses = {
        "/departments": [{"id": department_id, "name": "Продажи", "code": "sales"}],
        "/scenarios": {
            "items": [
                {
                    "id": scenario_id,
                    "department_id": department_id,
                    "name": "Сценарий",
                    "code": "scenario",
                    "categories": [
                        {
                            "id": str(uuid4()),
                            "name": "Категория",
                            "items": [{"id": criterion_id, "name": "Критерий", "max_score": 10}],
                        }
                    ],
                }
            ],
            "pages": 1,
        },
        "/calls": {"items": [], "total": 0, "pages": 1},
    }
    analytics = adapter(FakePlatform(department_ids=(department_id,), responses=responses))

    scenarios = await analytics.get_scenario_performance(period="today")
    criteria = await analytics.get_criterion_performance(period="today")

    assert scenarios["omitted_filters_count"] == 0
    assert criteria["omitted_filters_count"] == 0


def test_session_cipher_is_authenticated_and_models_store_hashes_not_raw_mcp_tokens():
    cipher = SessionCipher("x" * 32)
    sealed = cipher.seal("upstream-token")
    assert sealed != "upstream-token"
    assert cipher.open(sealed) == "upstream-token"
    assert "code_hash" in OAuthAuthorizationCode.__table__.columns
    assert "token_hash" in OAuthToken.__table__.columns
    assert "access_token" not in OAuthToken.__table__.columns
    assert "encrypted_access_token" in OkkAccountSession.__table__.columns
    assert "encrypted_refresh_token" in OkkAccountSession.__table__.columns


@pytest.mark.anyio
async def test_named_department_outside_acl_never_falls_back_to_visible_ord_population():
    ord_id = str(uuid4())
    platform = FakePlatform(
        department_ids=(ord_id,),
        responses={
            "/departments": [{"id": ord_id, "name": "Отдел региональных продаж", "code": "ord"}],
        },
    )

    result = await adapter(platform).list_employees(department_ref="B2B")

    assert result["status"] == "not_available"
    assert result["data"] == {"reason": "department_not_in_access_scope"}
    assert result["access_context"]["departments"] == [
        {"id": ord_id, "name": "Отдел региональных продаж", "code": "ord"}
    ]
    assert not any(path == "/employees" for path, _ in platform.calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("method_name", "kwargs"),
    [
        ("get_overview_statistics", {"department_ref": "B2B"}),
        ("get_department_statistics", {"department_ref": "B2B"}),
        ("compare_departments", {"department_refs": ["B2B"]}),
        ("list_employees", {"department_ref": "B2B"}),
        ("get_employee_card", {"employee_id": str(uuid4()), "department_ref": "B2B"}),
        ("compare_employees", {"employee_ids": [str(uuid4())], "department_ref": "B2B"}),
        ("get_call_statistics", {"department_ref": "B2B"}),
        (
            "get_plan_fact_statistics",
            {"start_date": "2026-07-01", "end_date": "2026-07-18", "department_ref": "B2B"},
        ),
        ("get_client_statistics", {"department_ref": "B2B"}),
        ("get_crm_statistics", {"department_ref": "B2B"}),
        ("get_growth_insights", {"department_ref": "B2B"}),
        ("get_mentoring_statistics", {"department_ref": "B2B"}),
        ("list_scenarios", {"department_ref": "B2B"}),
        ("get_scenario_criteria", {"scenario_id": str(uuid4()), "department_ref": "B2B"}),
        ("get_scenario_performance", {"department_ref": "B2B"}),
        ("get_criterion_performance", {"department_ref": "B2B"}),
    ],
)
async def test_every_department_scoped_tool_fails_closed_on_an_inaccessible_name(method_name, kwargs):
    ord_id = str(uuid4())
    platform = FakePlatform(
        department_ids=(ord_id,),
        responses={
            "/departments": [{"id": ord_id, "name": "ORD", "code": "ord"}],
        },
    )

    result = await getattr(adapter(platform), method_name)(**kwargs)

    assert result["status"] == "not_available", method_name
    assert {path for path, _params in platform.calls} == {"/departments"}, method_name


@pytest.mark.anyio
async def test_department_ref_resolves_exact_code_and_normalized_name_for_every_visible_department():
    b2b_id, ord_id = str(uuid4()), str(uuid4())
    platform = FakePlatform(
        role="admin",
        responses={
            "/departments": [
                {"id": b2b_id, "name": "B2B Продажи", "code": "b2b"},
                {"id": ord_id, "name": "Отдел региональных продаж", "code": "ord"},
            ],
        },
    )
    analytics = adapter(platform)

    by_code, supplied = await analytics.resolve_department(department_ref="B2B")
    by_name, _ = await analytics.resolve_department(department_ref="отдел-региональных_продаж")
    by_acronym, _ = await analytics.resolve_department(department_ref="ОРП")
    conflicting, conflict_supplied = await analytics.resolve_department(
        department_id=b2b_id, department_ref="ord"
    )

    assert supplied is True
    assert by_code["id"] == b2b_id
    assert by_name["id"] == ord_id
    assert by_acronym["id"] == ord_id
    assert conflicting is None
    assert conflict_supplied is True


@pytest.mark.anyio
async def test_new_runtime_department_requires_no_plugin_mapping_or_release():
    first_id, new_id = str(uuid4()), str(uuid4())
    suffix = uuid4().hex[:10]
    platform = FakePlatform(
        role="admin",
        responses={"/departments": [{"id": first_id, "name": "Первоначальный отдел", "code": "initial"}]},
    )

    first, _ = await adapter(platform).resolve_department(department_ref="initial")
    assert first["id"] == first_id

    runtime_name = f"Новый отдел {suffix}"
    runtime_code = f"runtime_{suffix}"
    platform.responses["/departments"] = [{"id": new_id, "name": runtime_name, "code": runtime_code}]

    discovered_by_code, _ = await adapter(platform).resolve_department(department_ref=runtime_code)
    discovered_by_name, _ = await adapter(platform).resolve_department(department_ref=runtime_name)

    assert discovered_by_code["id"] == new_id
    assert discovered_by_name["id"] == new_id
    assert [path for path, _params in platform.calls] == [
        "/departments",
        "/departments",
        "/departments",
    ]


@pytest.mark.anyio
async def test_filtered_overview_cannot_leak_other_department_rollups():
    b2b_id, ord_id = str(uuid4()), str(uuid4())
    platform = FakePlatform(
        role="admin",
        responses={
            "/departments": [
                {"id": b2b_id, "name": "B2B", "code": "b2b"},
                {"id": ord_id, "name": "ORD", "code": "ord"},
            ],
            "/dashboard/summary": {"calls": 2},
            "/dashboard/calls-trend": [],
            "/dashboard/top-employees": [],
            "/dashboard/by-department": [
                {"department_id": b2b_id, "calls": 2},
                {"department_id": ord_id, "calls": 99},
            ],
        },
    )

    result = await adapter(platform).get_overview_statistics(department_ref="b2b")

    assert result["effective_scope"]["department_code"] == "b2b"
    assert result["data"]["departments"] == [{"department_id": b2b_id, "calls": 2}]


@pytest.mark.anyio
async def test_department_card_by_name_returns_complete_department_sources_only():
    b2b_id, employee_id = str(uuid4()), str(uuid4())
    employee = {
        "id": employee_id,
        "full_name": "B2B Employee",
        "department_id": b2b_id,
        "department": {"id": b2b_id, "name": "B2B", "code": "b2b"},
    }
    platform = FakePlatform(
        department_ids=(b2b_id,),
        responses={
            "/departments": [{"id": b2b_id, "name": "B2B", "code": "b2b"}],
            "/dashboard/summary": {"calls": 3},
            "/dashboard/calls-trend": [{"date": "2026-07-18", "calls": 3}],
            "/dashboard/top-employees": [{"employee_id": employee_id, "calls": 3}],
            f"/departments/{b2b_id}/summary": {"department_id": b2b_id, "calls": 3},
            f"/departments/{b2b_id}/ranking": {"employees": [{"id": employee_id}]},
            f"/departments/{b2b_id}/trends": {"employee_trends": [{"id": employee_id}]},
            "/employees": {"items": [employee], "total": 1, "pages": 1},
            "/plans/summary": [{"employee_id": employee_id, "plan_total": 10}],
        },
    )

    result = await adapter(platform).get_department_statistics(department_ref="b2b", period="today")

    assert result["status"] == "ok"
    assert result["effective_scope"]["department_id"] == b2b_id
    assert result["data"]["complete_employee_ranking"]["employees"] == [{"id": employee_id}]
    assert result["data"]["department_and_employee_trends"]["employee_trends"] == [{"id": employee_id}]
    for path, _params in platform.calls:
        if path.startswith("/departments/"):
            assert b2b_id in path


@pytest.mark.anyio
async def test_employee_and_department_cross_filter_mismatch_is_neutral():
    b2b_id, ord_id, employee_id = str(uuid4()), str(uuid4()), str(uuid4())
    platform = FakePlatform(
        role="admin",
        responses={
            "/departments": [
                {"id": b2b_id, "name": "B2B", "code": "b2b"},
                {"id": ord_id, "name": "ORD", "code": "ord"},
            ],
            f"/employees/{employee_id}": {
                "id": employee_id,
                "full_name": "ORD Employee",
                "department_id": ord_id,
                "department": {"id": ord_id, "name": "ORD", "code": "ord"},
            },
        },
    )

    result = await adapter(platform).get_call_statistics(department_ref="b2b", employee_id=employee_id)

    assert result["status"] == "not_available"
    assert not any(path.startswith("/calls/stats") for path, _ in platform.calls)


@pytest.mark.anyio
async def test_compare_employees_resolves_direct_ids_and_omits_cross_department_rows():
    b2b_id, ord_id = str(uuid4()), str(uuid4())
    b2b_employee, ord_employee = str(uuid4()), str(uuid4())
    platform = FakePlatform(
        role="admin",
        responses={
            "/departments": [
                {"id": b2b_id, "name": "B2B", "code": "b2b"},
                {"id": ord_id, "name": "ORD", "code": "ord"},
            ],
            f"/employees/{b2b_employee}": {
                "id": b2b_employee,
                "full_name": "B2B Employee",
                "department_id": b2b_id,
                "department": {"id": b2b_id, "name": "B2B", "code": "b2b"},
            },
            f"/employees/{ord_employee}": {
                "id": ord_employee,
                "full_name": "ORD Employee",
                "department_id": ord_id,
                "department": {"id": ord_id, "name": "ORD", "code": "ord"},
            },
            f"/employees/{b2b_employee}/page-data": {"kpi": {"calls_total": 5}},
        },
    )

    result = await adapter(platform).compare_employees(
        employee_ids=[b2b_employee, ord_employee], department_ref="B2B", period="today"
    )

    assert result["status"] == "ok"
    assert [row["employee"]["id"] for row in result["data"]] == [b2b_employee]
    assert result["omitted_filters_count"] == 1
    assert not any(path == "/employees" for path, _params in platform.calls)


@pytest.mark.anyio
async def test_employee_population_cap_is_reported_as_partial_with_source_total():
    department_id = str(uuid4())
    rows = [
        {
            "id": str(uuid4()),
            "full_name": f"Employee {index}",
            "department_id": department_id,
            "department": {"id": department_id, "name": "B2B", "code": "b2b"},
        }
        for index in range(100)
    ]
    platform = FakePlatform(
        department_ids=(department_id,),
        responses={
            "/departments": [{"id": department_id, "name": "B2B", "code": "b2b"}],
            "/employees": {"items": rows, "total": 150, "page": 1, "pages": 2},
        },
    )
    analytics = AnalyticsAdapter(
        platform,
        str(platform.context.session_id),
        Settings(analytics_max_employees=100),
    )

    result = await analytics.list_employees(department_ref="b2b", page_size=100)

    assert result["status"] == "partial"
    assert result["data"]["source_total"] == 150
    assert result["data"]["source_complete"] is False
    assert result["data"]["returned_population"] == 100


@pytest.mark.anyio
async def test_scenario_search_and_historical_flag_are_actually_forwarded_for_admin():
    department_id = str(uuid4())
    active_id, archived_id = str(uuid4()), str(uuid4())

    def scenarios(params):
        assert ("include_inactive", "true") in params
        return {
            "items": [
                {"id": active_id, "department_id": department_id, "name": "New client", "is_active": True},
                {"id": archived_id, "department_id": department_id, "name": "Archived", "is_active": False},
            ],
            "pages": 1,
        }

    platform = FakePlatform(
        role="admin",
        responses={
            "/departments": [{"id": department_id, "name": "B2B", "code": "b2b"}],
            "/scenarios": scenarios,
        },
    )

    result = await adapter(platform).list_scenarios(
        department_ref="b2b", search="archive", include_historical=True
    )

    assert result["data"]["include_historical_applied"] is True
    assert [row["id"] for row in result["data"]["items"]] == [archived_id]


@pytest.mark.anyio
async def test_historical_crm_date_is_not_mislabeled_as_an_available_snapshot():
    department_id, employee_id = str(uuid4()), str(uuid4())
    employee = {
        "id": employee_id,
        "full_name": "Employee",
        "department_id": department_id,
        "department": {"id": department_id, "name": "B2B", "code": "b2b"},
    }
    platform = FakePlatform(
        department_ids=(department_id,),
        responses={
            "/departments": [{"id": department_id, "name": "B2B", "code": "b2b"}],
            "/employees": {"items": [employee], "total": 1, "pages": 1},
            f"/employees/{employee_id}/bitrix-metrics": {
                "status": "ok",
                "employee_id": employee_id,
                "snapshot_date": "2026-07-18",
            },
        },
    )

    result = await adapter(platform).get_crm_statistics(department_ref="b2b", snapshot_date="2025-01-01")

    assert result["status"] == "not_available"
    assert result["data"]["source_mode"] == "latest_snapshot_per_employee_only"
    assert result["data"]["available_latest_snapshot_dates"] == ["2026-07-18"]


@pytest.mark.anyio
async def test_date_only_mentoring_deadline_is_timezone_safe_and_counted_overdue():
    department_id, employee_id = str(uuid4()), str(uuid4())
    employee = {
        "id": employee_id,
        "full_name": "Employee",
        "department_id": department_id,
        "department": {"id": department_id, "name": "B2B", "code": "b2b"},
    }
    platform = FakePlatform(
        department_ids=(department_id,),
        responses={
            "/departments": [{"id": department_id, "name": "B2B", "code": "b2b"}],
            "/employees": {"items": [employee], "total": 1, "pages": 1},
            f"/employees/{employee_id}/page-data": {
                "mentoring_tasks": [
                    {
                        "id": "task",
                        "status": "pending",
                        "due_date": "2020-01-01",
                        "created_at": "2020-01-01T10:00:00+03:00",
                    }
                ],
                "completed_tasks": [],
            },
        },
    )

    result = await adapter(platform).get_mentoring_statistics(
        department_ref="b2b",
        period="custom",
        start_date="2020-01-01",
        end_date="2020-01-31",
    )

    assert result["data"]["summary"]["overdue"] == 1
    assert result["data"]["pages"] == 1


@pytest.mark.anyio
async def test_employee_only_filter_derives_and_reports_the_real_department_scope():
    department_id, employee_id = str(uuid4()), str(uuid4())
    platform = FakePlatform(
        department_ids=(department_id,),
        responses={
            "/departments": [{"id": department_id, "name": "B2B Sales", "code": "b2b"}],
            f"/employees/{employee_id}": {
                "id": employee_id,
                "full_name": "Employee",
                "department_id": department_id,
                "department": {"id": department_id, "name": "B2B Sales", "code": "b2b"},
            },
            "/calls/stats/summary": {"calls_total": 3},
            "/dashboard/calls-trend": [],
        },
    )

    result = await adapter(platform).get_call_statistics(employee_id=employee_id, period="today")

    assert result["status"] == "ok"
    assert result["effective_scope"]["department_id"] == department_id
    assert result["effective_scope"]["department_code"] == "b2b"
    summary_params = next(params for path, params in platform.calls if path == "/calls/stats/summary")
    assert ("department_id", department_id) in summary_params


@pytest.mark.anyio
async def test_growth_mentions_count_unique_employees_not_duplicate_text_occurrences():
    department_id = str(uuid4())
    first_id, second_id = str(uuid4()), str(uuid4())
    employees = [
        {
            "id": first_id,
            "full_name": "First",
            "department_id": department_id,
            "department": {"id": department_id, "name": "B2B", "code": "b2b"},
        },
        {
            "id": second_id,
            "full_name": "Second",
            "department_id": department_id,
            "department": {"id": department_id, "name": "B2B", "code": "b2b"},
        },
    ]
    platform = FakePlatform(
        department_ids=(department_id,),
        responses={
            "/departments": [{"id": department_id, "name": "B2B", "code": "b2b"}],
            "/employees": {"items": employees, "total": 2, "pages": 1},
            f"/employees/{first_id}/page-data": {
                "aggregated_strengths": ["Discovery", " discovery ", {"text": "DISCOVERY"}],
            },
            f"/employees/{second_id}/page-data": {"aggregated_strengths": ["DISCOVERY"]},
        },
    )

    result = await adapter(platform).get_growth_insights(department_ref="b2b", period="today")

    assert result["data"]["strengths"] == [{"text": "Discovery", "employee_mentions": 2}]


@pytest.mark.anyio
async def test_crm_coverage_with_unavailable_employee_snapshots_is_partial():
    department_id = str(uuid4())
    first_id, second_id = str(uuid4()), str(uuid4())
    employees = [
        {
            "id": employee_id,
            "full_name": employee_id,
            "department_id": department_id,
            "department": {"id": department_id, "name": "B2B", "code": "b2b"},
        }
        for employee_id in (first_id, second_id)
    ]
    platform = FakePlatform(
        department_ids=(department_id,),
        responses={
            "/departments": [{"id": department_id, "name": "B2B", "code": "b2b"}],
            "/employees": {"items": employees, "total": 2, "pages": 1},
            f"/employees/{first_id}/bitrix-metrics": {
                "status": "ok",
                "employee_id": first_id,
                "snapshot_date": "2026-07-18",
            },
            f"/employees/{second_id}/bitrix-metrics": {
                "status": "no_bitrix_id",
                "employee_id": second_id,
                "snapshot_date": "2026-07-18",
            },
        },
    )

    result = await adapter(platform).get_crm_statistics(department_ref="b2b")

    assert result["status"] == "partial"
    assert result["data"]["coverage"] == {
        "source_total": 2,
        "source_complete": True,
        "employees": 2,
        "available": 1,
        "unavailable": 1,
    }


@pytest.mark.anyio
async def test_criterion_output_limit_is_explicitly_reported_as_partial():
    department_id, scenario_id, call_id = str(uuid4()), str(uuid4()), str(uuid4())
    first_criterion, second_criterion = str(uuid4()), str(uuid4())
    scenario = {
        "id": scenario_id,
        "department_id": department_id,
        "name": "Scenario",
        "categories": [
            {
                "id": str(uuid4()),
                "name": "Category",
                "items": [
                    {"id": first_criterion, "name": "First", "max_score": 10},
                    {"id": second_criterion, "name": "Second", "max_score": 10},
                ],
            }
        ],
    }
    platform = FakePlatform(
        department_ids=(department_id,),
        responses={
            "/departments": [{"id": department_id, "name": "B2B", "code": "b2b"}],
            "/scenarios": {"items": [scenario], "pages": 1},
            "/calls": {
                "items": [
                    {
                        "id": call_id,
                        "employee_id": str(uuid4()),
                        "scenario_id": scenario_id,
                        "quality_score": 80,
                    }
                ],
                "total": 1,
                "pages": 1,
            },
            f"/calls/{call_id}": {
                "evaluation": {
                    "items": [
                        {"checklist_item_id": first_criterion, "score": 8, "max_score": 10},
                        {"checklist_item_id": second_criterion, "score": 9, "max_score": 10},
                    ]
                }
            },
        },
    )

    result = await adapter(platform).get_criterion_performance(
        scenario_id=scenario_id,
        period="today",
        limit=1,
    )

    assert result["status"] == "partial"
    assert len(result["data"]) == 1
    assert result["effective_scope"]["matching_criteria_total"] == 2
    assert result["effective_scope"]["returned_criteria"] == 1


def test_operational_trace_is_useful_but_redacts_ids_names_and_business_payload(caplog):
    department_id, employee_id = str(uuid4()), str(uuid4())
    client = BackendClient(Settings(), object())
    with caplog.at_level("INFO", logger="okk_mcp.analytics_trace"):
        client._trace(
            request_id="request-1",
            subject="secret-session-subject",
            path=f"/mcp-read/employee-card/{employee_id}",
            params={
                "department_ref": "Confidential Department Name",
                "employee_id": employee_id,
                "period": "month",
            },
            result={
                "status": "ok",
                "omitted_filters_count": 0,
                "effective_scope": {"department_id": department_id, "department_code": "b2b"},
                "data": {"employee": {"full_name": "Secret Person"}},
            },
            duration_ms=12.34,
        )

    trace = caplog.text
    assert "okk_analytics_tool_call" in trace
    assert "b2b" in trace
    assert "/{entity_id}" in trace
    for secret in (
        employee_id,
        department_id,
        "Confidential Department Name",
        "Secret Person",
        "secret-session-subject",
    ):
        assert secret not in trace
