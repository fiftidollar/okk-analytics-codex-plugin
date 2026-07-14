"""Standalone adapter safety and projection tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from okk_mcp.backend_client import AnalyticsAdapter
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
        f"/employees/{employee_id}/bitrix-metrics": {"status": "ok", "total_open_deals": 3},
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
