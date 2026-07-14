"""Strictly typed, read-only MCP tools for OKK business analytics."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, BaseModel, Field

from okk_mcp.backend_client import BackendClient, BackendUnavailable
from okk_mcp.config import Settings
from okk_mcp.oauth import OKKTokenVerifier

Period = Literal[
    "today",
    "yesterday",
    "week",
    "prev_week",
    "month",
    "prev_month",
    "quarter",
    "year",
    "all",
    "custom",
]
TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]


class AccessDepartment(BaseModel):
    id: str
    name: str
    code: str


class AccessContext(BaseModel):
    role: str
    all_departments: bool
    departments: list[AccessDepartment] = Field(default_factory=list)


class PeriodRange(BaseModel):
    start: str
    end: str


class AnalyticsEnvelope(BaseModel):
    status: Literal["ok", "partial", "no_data", "not_available", "temporarily_unavailable"]
    access_context: AccessContext | None = None
    effective_scope: dict[str, Any] = Field(default_factory=dict)
    period: PeriodRange | None = None
    omitted_filters_count: int = 0
    data: dict[str, Any] | list[dict[str, Any]] = Field(default_factory=dict)


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
STAT_SCOPE = "okk.statistics.read"
SCENARIO_SCOPE = "okk.scenarios.read"


def _security_meta(*scopes: str) -> dict[str, Any]:
    return {
        "securitySchemes": [
            {
                "type": "oauth2",
                "scopes": list(scopes),
            }
        ]
    }


def _require_scopes(*required: str) -> None:
    token = get_access_token()
    if not token or not set(required).issubset(set(token.scopes)):
        raise PermissionError("The OKK connection does not grant the required read scope")


def _period_params(
    period: Period,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, Any]:
    if period == "custom" and (not start_date or not end_date):
        raise ValueError("custom period requires start_date and end_date in YYYY-MM-DD format")
    return {"period": period, "start_date": start_date, "end_date": end_date}


async def _read(client: BackendClient, path: str, **params: Any) -> AnalyticsEnvelope:
    try:
        return AnalyticsEnvelope.model_validate(await client.get(path, **params))
    except BackendUnavailable:
        return AnalyticsEnvelope(
            status="temporarily_unavailable",
            data={"message": "OKK analytics is temporarily unavailable; retry later."},
        )


def create_mcp_server(settings: Settings, client: BackendClient) -> FastMCP:
    issuer_origin = settings.issuer_url
    issuer_host = urlsplit(issuer_origin).netloc
    mcp = FastMCP(
        name=settings.mcp_service_name,
        instructions=(
            "Read-only OKK business analytics. Respect access_context/effective_scope. "
            "Never ask the user for their OKK password in chat: authentication happens only "
            "on the OKK authorization page. Never infer hidden departments or entities from "
            "not_available/omitted results. Do not request audio, transcripts, prompts, raw "
            "reasoning, routing, pipeline, Megafon administration or write operations."
        ),
        token_verifier=OKKTokenVerifier(settings.resource_url),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer_url),
            resource_server_url=AnyHttpUrl(settings.resource_url),
            required_scopes=[],
        ),
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[issuer_host],
            allowed_origins=[issuer_origin],
        ),
    )

    @mcp.tool(
        title="Контекст доступа ОКК",
        description="Показывает роль и только те отделы, которые доступны текущему аккаунту ОКК.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_access_context() -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(client, "/mcp-read/access-context")

    @mcp.tool(
        title="Каталог статистики ОКК",
        description="Перечисляет все доступные в плагине статистические домены, метрики и явные исключения.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_statistics_catalog() -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(client, "/mcp-read/statistics-catalog")

    @mcp.tool(
        title="Сводная статистика",
        description="Максимальная общая сводка: звонки, качество, клиенты, отделы, рейтинг и дневные тренды.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_overview_statistics(
        period: Period = "month",
        department_id: UUID | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        top_limit: int = Field(default=20, ge=1, le=50),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/overview-statistics",
            department_id=department_id,
            top_limit=top_limit,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Список отделов",
        description="Возвращает доступные отделы и их KPI-настройки: пороги, минимальную длительность и недельный план.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def list_departments() -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(client, "/mcp-read/departments")

    @mcp.tool(
        title="Статистика отдела",
        description="Полная карточка отдела: KPI, план/факт, рейтинг сотрудников и дневные тренды.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_department_statistics(
        department_id: UUID,
        period: Period = "month",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            f"/mcp-read/department-statistics/{department_id}",
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Сравнение отделов",
        description="Сравнивает доступные отделы по объёму, качеству и трендам; недоступные фильтры безопасно исключаются.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def compare_departments(
        department_ids: list[UUID] | None = None,
        period: Period = "month",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/compare-departments",
            department_ids=department_ids,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Список сотрудников",
        description="Ищет сотрудников в доступных отделах без раскрытия email, телефона, PBX и учётных данных.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def list_employees(
        department_id: UUID | None = None,
        search: str | None = Field(default=None, max_length=200),
        include_inactive: bool = False,
        page: int = Field(default=1, ge=1),
        page_size: int = Field(default=50, ge=1, le=100),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/employees",
            department_id=department_id,
            search=search,
            include_inactive=include_inactive,
            page=page,
            page_size=page_size,
        )

    @mcp.tool(
        title="Полная карточка сотрудника",
        description=(
            "Возвращает KPI, план/факт, клиентские и CRM-метрики, AI-сильные стороны, зоны роста, "
            "недельный/сохранённый фокус, активные и последние завершённые наставнические задачи."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_employee_card(
        employee_id: UUID,
        period: Period = "month",
        start_date: str | None = None,
        end_date: str | None = None,
        task_page_size: int = Field(default=100, ge=1, le=100),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            f"/mcp-read/employee-card/{employee_id}",
            task_page_size=task_page_size,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Сравнение сотрудников",
        description="Сравнивает до 20 доступных сотрудников по KPI, сильным сторонам, зонам роста, фокусу и задачам.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def compare_employees(
        employee_ids: list[UUID] = Field(min_length=1, max_length=20),
        period: Period = "month",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/compare-employees",
            employee_ids=employee_ids,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Статистика звонков",
        description="Агрегирует объём, оценённость, баллы, pass rate, длительность и дневной тренд без аудио и транскриптов.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_call_statistics(
        period: Period = "month",
        department_id: UUID | None = None,
        employee_id: UUID | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/call-statistics",
            department_id=department_id,
            employee_id=employee_id,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Статистика план/факт",
        description="Возвращает дневные планы и план/факт по общим, входящим, исходящим, новым и регулярным звонкам.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_plan_fact_statistics(
        start_date: date,
        end_date: date,
        department_id: UUID | None = None,
        employee_id: UUID | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        return await _read(
            client,
            "/mcp-read/plan-fact-statistics",
            start_date=start_date,
            end_date=end_date,
            department_id=department_id,
            employee_id=employee_id,
        )

    @mcp.tool(
        title="Статистика клиентов",
        description="Максимальная статистика новых/регулярных клиентов, эффективных контактов, повторов, недозвонов и пропущенных.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_client_statistics(
        period: Period = "month",
        department_id: UUID | None = None,
        employee_id: UUID | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/client-statistics",
            department_id=department_id,
            employee_id=employee_id,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Статистика CRM",
        description="Агрегирует последние Bitrix CRM-снимки: сделки, задачи, просрочки, стадии, воронки и покрытие сотрудников.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_crm_statistics(
        department_id: UUID | None = None,
        employee_id: UUID | None = None,
        snapshot_date: date | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/crm-statistics",
            department_id=department_id,
            employee_id=employee_id,
            snapshot_date=snapshot_date,
        )

    @mcp.tool(
        title="AI-сильные стороны и зоны роста",
        description="Частотная статистика AI-наблюдений по сильным сторонам и зонам роста без выдачи сырого reasoning.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_growth_insights(
        period: Period = "month",
        department_id: UUID | None = None,
        employee_id: UUID | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = Field(default=20, ge=1, le=100),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/growth-insights",
            department_id=department_id,
            employee_id=employee_id,
            limit=limit,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Статистика наставнических задач",
        description="Максимально доступная через текущий API история задач: рекомендации, статусы, сроки, просрочка, completion rate и скорость выполнения; ответ явно отмечает ограниченное окно истории.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_mentoring_statistics(
        period: Period = "month",
        department_id: UUID | None = None,
        employee_id: UUID | None = None,
        task_status: TaskStatus | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = Field(default=1, ge=1),
        page_size: int = Field(default=50, ge=1, le=100),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/mentoring-statistics",
            department_id=department_id,
            employee_id=employee_id,
            task_status=task_status,
            page=page,
            page_size=page_size,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Список сценариев",
        description="Безопасный каталог доступных активных и исторических сценариев без промптов, скриптов и маршрутизации.",
        annotations=READ_ONLY,
        meta=_security_meta(SCENARIO_SCOPE),
        structured_output=True,
    )
    async def list_scenarios(
        department_id: UUID | None = None,
        include_historical: bool = True,
        page: int = Field(default=1, ge=1),
        page_size: int = Field(default=50, ge=1, le=100),
    ) -> AnalyticsEnvelope:
        _require_scopes(SCENARIO_SCOPE)
        return await _read(
            client,
            "/mcp-read/scenarios",
            department_id=department_id,
            include_historical=include_historical,
            page=page,
            page_size=page_size,
        )

    @mcp.tool(
        title="Критерии сценария",
        description="Полная бизнес-конфигурация категорий и критериев: веса, шкалы, индикаторы, обязательность и применимость.",
        annotations=READ_ONLY,
        meta=_security_meta(SCENARIO_SCOPE),
        structured_output=True,
    )
    async def get_scenario_criteria(scenario_id: UUID) -> AnalyticsEnvelope:
        _require_scopes(SCENARIO_SCOPE)
        return await _read(client, f"/mcp-read/scenario-criteria/{scenario_id}")

    @mcp.tool(
        title="Эффективность сценариев",
        description="Сравнивает сценарии по числу оценок, охвату сотрудников, среднему/минимальному/максимальному баллу и pass rate.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, SCENARIO_SCOPE),
        structured_output=True,
    )
    async def get_scenario_performance(
        period: Period = "month",
        scenario_ids: list[UUID] | None = Field(default=None, max_length=100),
        department_id: UUID | None = None,
        employee_id: UUID | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE, SCENARIO_SCOPE)
        return await _read(
            client,
            "/mcp-read/scenario-performance",
            scenario_ids=scenario_ids,
            department_id=department_id,
            employee_id=employee_id,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Эффективность критериев",
        description="Показывает самые сильные/слабые критерии: наблюдения, охват, средний балл/процент, штрафы и последнее наблюдение.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, SCENARIO_SCOPE),
        structured_output=True,
    )
    async def get_criterion_performance(
        period: Period = "month",
        scenario_id: UUID | None = None,
        criterion_ids: list[UUID] | None = Field(default=None, max_length=100),
        department_id: UUID | None = None,
        employee_id: UUID | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = Field(default=100, ge=1, le=500),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE, SCENARIO_SCOPE)
        return await _read(
            client,
            "/mcp-read/criterion-performance",
            scenario_id=scenario_id,
            criterion_ids=criterion_ids,
            department_id=department_id,
            employee_id=employee_id,
            limit=limit,
            **_period_params(period, start_date, end_date),
        )

    return mcp
