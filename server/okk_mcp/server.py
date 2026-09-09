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
TranscriptFormat = Literal["raw", "diarized", "segments"]
TranscriptMatchMode = Literal["phrase", "all_terms", "any_terms"]


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


class EmployeeRosterGrounding(BaseModel):
    source: Literal["live_okk_employee_directory"]
    authoritative: bool
    department_id: str | None = None
    department_code: str | None = None
    department_name: str | None = None
    employee_count: int
    employee_ids: list[str] = Field(default_factory=list)
    employee_names: list[str] = Field(default_factory=list)
    source_complete: bool
    excluded_source_records: int = 0
    normalized_source_records: int = 0
    usage_rule: str


class AnalyticsEnvelope(BaseModel):
    status: Literal["ok", "partial", "no_data", "not_available", "temporarily_unavailable"]
    access_context: AccessContext | None = None
    effective_scope: dict[str, Any] = Field(default_factory=dict)
    period: PeriodRange | None = None
    omitted_filters_count: int = 0
    request_id: str | None = None
    employee_roster_grounding: EmployeeRosterGrounding | None = None
    data: dict[str, Any] | list[dict[str, Any]] = Field(default_factory=dict)


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
STAT_SCOPE = "okk.statistics.read"
SCENARIO_SCOPE = "okk.scenarios.read"
TRANSCRIPT_SCOPE = "okk.transcripts.read"
IDENTITY_SCOPES = ("openid", "email")


def _security_meta(*scopes: str) -> dict[str, Any]:
    return {
        "securitySchemes": [
            {
                "type": "oauth2",
                "scopes": [*IDENTITY_SCOPES, *scopes],
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
            "Read-only OKK business analytics. On the first OKK request in a task, call "
            "get_access_context. If it returns authenticated=true, explicitly tell the user "
            "'OKK подключён' and summarize only the role and visible departments. A browser "
            "redirect alone is not proof of a completed connection. Respect "
            "access_context/effective_scope. "
            "Treat access_context.departments as the live department catalog for this request; "
            "never rely on a fixed list of department names or assume an example department exists. "
            "When the user names a department, pass that exact name or code as department_ref; "
            "if the user's wording does not identify exactly one live visible department, ask them "
            "to choose instead of guessing. "
            "Never silently omit a requested department filter and never relabel another "
            "department's employees. If status is not_available, state that the requested "
            "department is outside the connected account's visible scope and name only the "
            "departments present in access_context. "
            "For every department report that names employees, use get_department_statistics "
            "and treat employee_roster_grounding.employee_ids/employee_names as the exclusive "
            "authoritative roster for that response. Never introduce, autocorrect, transliterate "
            "or infer a person outside that roster, including from conversation memory. If the "
            "grounding is absent or source_complete=false, call list_employees with the exact same "
            "department_ref before naming people and disclose incomplete coverage. "
            "The private Supervisors section is separate from departments and is discovered "
            "only through list_supervisors. When the user names a person without identifying "
            "their section, search both list_employees and list_supervisors, then use only the "
            "matching section's tools. Never treat a supervisor as a department employee or "
            "claim KPI, scenario, CRM, client, growth or mentoring data for a transcription-only "
            "supervisor. Supervisor access is determined only by the live OKK restricted catalog; "
            "never hardcode names, emails or grants. "
            "Never ask the user for their OKK password in chat: authentication happens only "
            "on the OKK authorization page. Never infer hidden departments or entities from "
            "not_available/omitted results. Transcripts may be read only through the dedicated "
            "transcript tools and only for calls in the connected account's current ACL. Do not "
            "copy transcript text into operational logs. Do not request audio, prompts, raw "
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
        title="Проверка подключения и доступа ОКК",
        description="Первый безопасный вызов после входа: успешный ответ технически подтверждает подключение ОКК, показывает роль, только доступные текущему аккаунту отделы и наличие персонального раздела «Руководители». После успеха явно сообщите пользователю: «OKK подключён».",
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
        description="Максимальная общая сводка: звонки, качество, клиенты, отделы, рейтинг и дневные тренды. Для названия или кода отдела обязательно передайте department_ref; не оставляйте фильтр пустым. В отфильтрованной сводке называйте только сотрудников из employee_roster_grounding.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_overview_statistics(
        period: Period = "month",
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None,
            max_length=200,
            description="Exact code or full name from the current access_context.departments catalog.",
        ),
        start_date: str | None = None,
        end_date: str | None = None,
        top_limit: int = Field(default=20, ge=1, le=50),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/overview-statistics",
            department_id=department_id,
            department_ref=department_ref,
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
        description="Полная карточка одного доступного отдела: KPI, план/факт, рейтинг сотрудников и дневные тренды. При запросе пользователя по названию или коду используйте department_ref; недоступный отдел возвращает not_available без данных другого отдела. В отчёте разрешено называть только сотрудников из employee_roster_grounding.employee_names: это авторитетный живой состав отдела, а все upstream-строки вне него отфильтрованы.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_department_statistics(
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None,
            max_length=200,
            description="Exact code or full name from the current access_context.departments catalog.",
        ),
        period: Period = "month",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        if not department_id and not department_ref:
            raise ValueError("department_id or department_ref is required")
        return await _read(
            client,
            "/mcp-read/department-statistics",
            department_id=department_id,
            department_ref=department_ref,
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
        department_refs: list[str] | None = Field(
            default=None,
            max_length=100,
            description="Exact visible department codes or names.",
        ),
        period: Period = "month",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/compare-departments",
            department_ids=department_ids,
            department_refs=department_refs,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Список сотрудников",
        description="Ищет сотрудников только в доступных отделах без раскрытия email, телефона, PBX и учётных данных. Если пользователь назвал отдел, передайте его точное имя или код в department_ref; не выполняйте общий поиск вместо фильтрованного. employee_roster_grounding.employee_names является единственным допустимым списком фамилий для ответа модели.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def list_employees(
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None,
            max_length=200,
            description="Exact visible department code or name. A failed match never broadens the query.",
        ),
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
            department_ref=department_ref,
            search=search,
            include_inactive=include_inactive,
            page=page,
            page_size=page_size,
        )

    @mcp.tool(
        title="Доступные руководители",
        description=(
            "Возвращает персональный ACL-каталог раздела «Руководители». Раздел отделён от "
            "обычных сотрудников и не зависит от роли admin: доступ задаётся индивидуальными "
            "правами OKK. При поиске человека по имени проверяйте этот каталог отдельно."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def list_supervisors(
        search: str | None = Field(default=None, max_length=200),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(client, "/mcp-read/supervisors", search=search)

    @mcp.tool(
        title="Статистика звонков руководителя",
        description=(
            "Возвращает только базовые read-only метрики звонков доступного руководителя: "
            "объём, длительность, направления и дневной ряд. Для этого transcription-only "
            "контура оценки, сценарии, клиенты, CRM и наставничество недоступны."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_supervisor_call_statistics(
        supervisor_id: UUID,
        period: Period = "month",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/supervisor-call-statistics",
            supervisor_id=supervisor_id,
            **_period_params(period, start_date, end_date),
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
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None,
            max_length=200,
            description="Optional department guard by exact visible code or name.",
        ),
        period: Period = "month",
        start_date: str | None = None,
        end_date: str | None = None,
        task_page_size: int = Field(default=100, ge=1, le=100),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            f"/mcp-read/employee-card/{employee_id}",
            department_id=department_id,
            department_ref=department_ref,
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
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None,
            max_length=200,
            description="Optional department guard by exact visible code or name.",
        ),
        period: Period = "month",
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/compare-employees",
            employee_ids=employee_ids,
            department_id=department_id,
            department_ref=department_ref,
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
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/call-statistics",
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Каталог транскрипций звонков",
        description=(
            "Возвращает только доступные текущему аккаунту звонки, наличие транскрипции и "
            "короткое превью без телефонов и аудио. Поддерживает фильтры отдела, сотрудника, "
            "сценария и периода; недоступный фильтр никогда не расширяется до всех звонков."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def list_call_transcripts(
        period: Period = "month",
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        scenario_id: UUID | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        transcript_format: TranscriptFormat = "diarized",
        preview_chars: int = Field(default=300, ge=0, le=2000),
        page: int = Field(default=1, ge=1),
        page_size: int = Field(default=25, ge=1, le=100),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE, TRANSCRIPT_SCOPE)
        return await _read(
            client,
            "/mcp-read/call-transcripts",
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
            scenario_id=scenario_id,
            transcript_format=transcript_format,
            preview_chars=preview_chars,
            page=page,
            page_size=page_size,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Полная транскрипция звонка",
        description=(
            "Читает полную raw/diarized транскрипцию или безопасно спроецированные сегменты "
            "одного доступного звонка. Недоступный и несуществующий UUID дают одинаковый "
            "not_available; телефоны, аудио и внутренние поля звонка не возвращаются."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def get_call_transcript(
        call_id: UUID,
        transcript_format: TranscriptFormat = "diarized",
        max_chars: int = Field(default=120000, ge=1000, le=500000),
        max_segments: int = Field(default=2000, ge=1, le=10000),
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Optional exact visible department guard."
        ),
        employee_id: UUID | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE, TRANSCRIPT_SCOPE)
        return await _read(
            client,
            f"/mcp-read/call-transcript/{call_id}",
            transcript_format=transcript_format,
            max_chars=max_chars,
            max_segments=max_segments,
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
        )

    @mcp.tool(
        title="Поиск по транскрипциям",
        description=(
            "Ищет фразу или слова только в транскрипциях звонков из текущего ACL и возвращает "
            "контекстные фрагменты с безопасными карточками звонков. Ответ явно показывает "
            "число просмотренных звонков и полноту поиска."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def search_call_transcripts(
        query: str = Field(min_length=2, max_length=500),
        period: Period = "month",
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        scenario_id: UUID | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        match_mode: TranscriptMatchMode = "phrase",
        context_chars: int = Field(default=240, ge=40, le=2000),
        limit: int = Field(default=25, ge=1, le=100),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE, TRANSCRIPT_SCOPE)
        return await _read(
            client,
            "/mcp-read/search-call-transcripts",
            query=query,
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
            scenario_id=scenario_id,
            match_mode=match_mode,
            context_chars=context_chars,
            limit=limit,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Транскрипции звонков руководителя",
        description=(
            "Показывает звонки и превью транскрипций одного руководителя из персонально "
            "доступного раздела «Руководители». Не используйте обычный каталог сотрудников "
            "для этого контура; недоступный UUID возвращает нейтральный not_available."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def list_supervisor_call_transcripts(
        supervisor_id: UUID,
        period: Period = "month",
        start_date: str | None = None,
        end_date: str | None = None,
        transcript_format: TranscriptFormat = "diarized",
        preview_chars: int = Field(default=300, ge=0, le=2000),
        page: int = Field(default=1, ge=1),
        page_size: int = Field(default=25, ge=1, le=100),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE, TRANSCRIPT_SCOPE)
        return await _read(
            client,
            "/mcp-read/supervisor-call-transcripts",
            supervisor_id=supervisor_id,
            transcript_format=transcript_format,
            preview_chars=preview_chars,
            page=page,
            page_size=page_size,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Полная транскрипция звонка руководителя",
        description=(
            "Читает raw/diarized текст или безопасные speaker-сегменты одного звонка, "
            "одновременно проверяя доступ к руководителю и принадлежность звонка ему."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def get_supervisor_call_transcript(
        supervisor_id: UUID,
        call_id: UUID,
        transcript_format: TranscriptFormat = "diarized",
        max_chars: int = Field(default=120000, ge=1000, le=500000),
        max_segments: int = Field(default=2000, ge=1, le=10000),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE, TRANSCRIPT_SCOPE)
        return await _read(
            client,
            f"/mcp-read/supervisor-call-transcript/{call_id}",
            supervisor_id=supervisor_id,
            transcript_format=transcript_format,
            max_chars=max_chars,
            max_segments=max_segments,
        )

    @mcp.tool(
        title="Поиск по транскрипциям руководителя",
        description=(
            "Ищет фразу или слова только в транскрипциях одного персонально доступного "
            "руководителя и явно сообщает полноту сканирования."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def search_supervisor_call_transcripts(
        supervisor_id: UUID,
        query: str = Field(min_length=2, max_length=500),
        period: Period = "month",
        start_date: str | None = None,
        end_date: str | None = None,
        match_mode: TranscriptMatchMode = "phrase",
        context_chars: int = Field(default=240, ge=40, le=2000),
        limit: int = Field(default=25, ge=1, le=100),
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE, TRANSCRIPT_SCOPE)
        return await _read(
            client,
            "/mcp-read/search-supervisor-call-transcripts",
            supervisor_id=supervisor_id,
            query=query,
            match_mode=match_mode,
            context_chars=context_chars,
            limit=limit,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Статистика план/факт",
        description="Возвращает дневные планы и план/факт по общим, входящим, исходящим, новым и регулярным звонкам. Имена в ответе канонизированы по employee_roster_grounding; не добавляйте людей вне этого списка.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_plan_fact_statistics(
        start_date: date,
        end_date: date,
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
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
            department_ref=department_ref,
            employee_id=employee_id,
        )

    @mcp.tool(
        title="Статистика клиентов",
        description=(
            "Максимальная статистика новых/действующих клиентов, эффективных контактов, "
            "недозвонов и пропущенных, а для B2B — первых/повторных касаний и возвратов "
            "из повторного в первое касание после 30 дней."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_client_statistics(
        period: Period = "month",
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/client-statistics",
            department_id=department_id,
            department_ref=department_ref,
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
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        snapshot_date: date | None = None,
    ) -> AnalyticsEnvelope:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/crm-statistics",
            department_id=department_id,
            department_ref=department_ref,
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
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
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
            department_ref=department_ref,
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
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
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
            department_ref=department_ref,
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
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        search: str | None = Field(default=None, max_length=200),
        include_historical: bool = False,
        page: int = Field(default=1, ge=1),
        page_size: int = Field(default=50, ge=1, le=100),
    ) -> AnalyticsEnvelope:
        _require_scopes(SCENARIO_SCOPE)
        return await _read(
            client,
            "/mcp-read/scenarios",
            department_id=department_id,
            department_ref=department_ref,
            search=search,
            include_historical=include_historical,
            page=page,
            page_size=page_size,
        )

    @mcp.tool(
        title="Критерии сценария",
        description="Полная доступная бизнес-конфигурация категорий и критериев: максимальные баллы, шкалы, индикаторы, обязательность, критичность и применимость.",
        annotations=READ_ONLY,
        meta=_security_meta(SCENARIO_SCOPE),
        structured_output=True,
    )
    async def get_scenario_criteria(
        scenario_id: UUID,
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None,
            max_length=200,
            description="Optional department guard by exact visible code or name.",
        ),
    ) -> AnalyticsEnvelope:
        _require_scopes(SCENARIO_SCOPE)
        return await _read(
            client,
            f"/mcp-read/scenario-criteria/{scenario_id}",
            department_id=department_id,
            department_ref=department_ref,
        )

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
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
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
            department_ref=department_ref,
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
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
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
            department_ref=department_ref,
            employee_id=employee_id,
            limit=limit,
            **_period_params(period, start_date, end_date),
        )

    return mcp
