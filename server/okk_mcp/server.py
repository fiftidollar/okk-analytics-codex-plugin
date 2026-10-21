"""Strictly typed, read-only MCP tools for OKK business analytics."""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
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
PeriodParam = Annotated[
    Period | None,
    Field(
        description=(
            "Named period in Moscow time; week, month, quarter and year run from their first day "
            "to today. Omit it when start_date and end_date are given."
        )
    ),
]
StartDate = Annotated[
    date | None,
    Field(description="First day of a custom range, YYYY-MM-DD. Give it with end_date instead of period."),
]
EndDate = Annotated[date | None, Field(description="Last day of a custom range, YYYY-MM-DD, inclusive.")]
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
    status: Literal["ok", "partial", "no_data", "not_available"]
    access_context: AccessContext | None = None
    effective_scope: dict[str, Any] = Field(default_factory=dict)
    period: PeriodRange | None = None
    omitted_filters_count: int = 0
    request_id: str | None = None
    employee_roster_grounding: EmployeeRosterGrounding | None = None
    data: dict[str, Any] | list[dict[str, Any]] = Field(default_factory=dict)


# The tool returns CallToolResult so the text copy of the envelope is compact; the output schema
# and its validation still come from AnalyticsEnvelope.
ToolResult = Annotated[CallToolResult, AnalyticsEnvelope]

# Response size limits. Claude Code warns above ~10k tokens and moves results over 50,000
# characters into a file; Codex cuts results to the model's budget. A full transcript is read
# in chunks (next_start_char), so one chunk stays well below both limits.
PREVIEW_MAX_CHARS = 500
PAGE_MAX_ITEMS = 50
TranscriptChunkChars = Annotated[
    int,
    Field(
        ge=1000,
        le=30000,
        description="Characters of text (or of segments) in this chunk; read the rest from next_start_*.",
    ),
]

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
STAT_SCOPE = "okk.statistics.read"
SCENARIO_SCOPE = "okk.scenarios.read"
TRANSCRIPT_SCOPE = "okk.transcripts.read"
PHONE_SCOPE = "okk.phones.read"
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
    missing = sorted(set(required) - set(token.scopes if token else ()))
    if missing:
        raise PermissionError(
            f"This OKK connection was granted without {', '.join(missing)}. Reconnect OKK Analytics "
            "and allow that access on the OKK login page."
        )


def _period_params(
    period: Period | None,
    start_date: date | None,
    end_date: date | None,
    *,
    default: Period = "month",
) -> dict[str, Any]:
    """Period by name or by dates. Dates are never dropped silently: they make the range custom."""

    if start_date is None and end_date is None:
        if period == "custom":
            raise ValueError("period=custom needs start_date and end_date, e.g. 2026-09-01 and 2026-09-30.")
        return {"period": period or default, "start_date": None, "end_date": None}
    if start_date is None or end_date is None:
        raise ValueError("Give both start_date and end_date (YYYY-MM-DD), e.g. 2026-09-01 and 2026-09-30.")
    if period not in (None, "custom"):
        raise ValueError(f"Give either period={period} or start_date/end_date, not both.")
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date.")
    return {"period": "custom", "start_date": start_date.isoformat(), "end_date": end_date.isoformat()}


async def _read(client: BackendClient, path: str, **params: Any) -> CallToolResult:
    try:
        payload = await client.get(path, **params)
    except BackendUnavailable as exc:
        raise ToolError(
            "OKK is temporarily unavailable; retry in a minute. This is not an empty result."
        ) from exc
    envelope = AnalyticsEnvelope.model_validate(payload).model_dump(mode="json")
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(content=[TextContent(type="text", text=text)], structuredContent=envelope)


def create_mcp_server(settings: Settings, client: BackendClient) -> FastMCP:
    issuer_origin = settings.issuer_url
    issuer_host = urlsplit(issuer_origin).netloc
    mcp = FastMCP(
        name=settings.mcp_service_name,
        # Claude Code cuts server instructions at 2048 characters in older releases: safety first,
        # then routing. Each tool's own contract lives in its description.
        instructions=(
            "Read-only analytics of the OKK call-quality system (ОКК): calls, departments, employees, "
            "scores, KPI, plans, clients, CRM, scenarios, transcripts.\n"
            "Safety: never ask for the OKK password in chat; sign-in happens only on the OKK page. "
            "Never guess hidden departments or people from not_available. Do not copy transcript "
            "text into logs or files unless the user asks for a file. Audio, prompts, AI reasoning, "
            "processing internals and any changes are out of scope.\n"
            "Start each task with get_access_context. When authenticated=true, tell the user "
            "'OKK подключён' with the role and visible departments.\n"
            "Departments: pass the user's department name or code as department_ref exactly as in "
            "access_context.departments. If it matches no single visible department, ask the user "
            "to choose. Never drop or widen a requested filter.\n"
            "People: name only employees from employee_roster_grounding of the same response, never "
            "from memory. If it is incomplete, call list_employees with the same department_ref. "
            "For a name without a section, search list_employees and list_supervisors. Supervisors "
            "are a private section: call volume and transcripts only, through their own tools.\n"
            "Numbers: no_data, not_available, null and partial are not zero; say what is missing.\n"
            "Periods: give period, or start_date and end_date (YYYY-MM-DD) for any other range."
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
    async def get_access_context() -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(client, "/mcp-read/access-context")

    @mcp.tool(
        title="Каталог статистики ОКК",
        description="Перечисляет все доступные в плагине статистические домены, метрики и явные исключения.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_statistics_catalog() -> ToolResult:
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
        period: PeriodParam = None,
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None,
            max_length=200,
            description="Exact code or full name from the current access_context.departments catalog.",
        ),
        start_date: StartDate = None,
        end_date: EndDate = None,
        top_limit: int = Field(default=20, ge=1, le=50),
    ) -> ToolResult:
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
        description=(
            "Возвращает доступные отделы и их KPI-настройки: норму оценки (quality_threshold, в старом API "
            "OKK — passing_score), минимальную длительность звонка и какие клиентские показатели ведёт отдел "
            "(client_status_tracked, client_touch_tracked). План звонков — в get_plan_fact_statistics."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def list_departments() -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(client, "/mcp-read/departments")

    @mcp.tool(
        title="Статистика отдела",
        description="Полная карточка одного доступного отдела: KPI, план/факт, рейтинг сотрудников и дневные тренды. При запросе пользователя по названию или коду используйте department_ref; недоступный отдел возвращает not_available без данных другого отдела. В отчёте разрешено называть только сотрудников из employee_roster_grounding.employee_names: это авторитетный живой состав отдела, а все upstream-строки вне него отфильтрованы. Смысл показателей — data.reporting_contract: успешные звонки, звонки от минимальной длительности и оценённые звонки — разные множества.",
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
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
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
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
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
    ) -> ToolResult:
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
    ) -> ToolResult:
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
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
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
            "Возвращает KPI, план/факт, клиентские и CRM-метрики, AI-сильные стороны, зоны роста и "
            "недельный фокус за один и тот же период. Наставнических задач OKK не хранит: "
            "mentoring_available=false, списки задач null — это не ноль."
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
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            f"/mcp-read/employee-card/{employee_id}",
            department_id=department_id,
            department_ref=department_ref,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Сравнение сотрудников",
        description="Сравнивает до 20 доступных сотрудников по KPI, сильным сторонам, зонам роста и недельному фокусу.",
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
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
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
        period: PeriodParam = None,
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
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
        title="Звонки по номеру телефона",
        description=(
            "Показывает номера клиентов и точное число доступных звонков по заданному номеру "
            "за период: число — matching_calls_total, а не длина страницы. Без номера возвращает "
            "страницу журнала с номерами. Считает все записи "
            "звонков, включая пропущенные, короткие и ещё обрабатываемые; сохраняет ACL "
            "отдела, сотрудника и персонального раздела руководителей. По умолчанию ищет "
            "за всю доступную историю, а не только за текущий месяц."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, PHONE_SCOPE),
        structured_output=True,
    )
    async def list_call_phone_records(
        phone_number: str | None = Field(default=None, min_length=3, max_length=40),
        period: Annotated[
            Period | None,
            Field(
                description="Named period; default all — the whole journal history. Omit it for a date range."
            ),
        ] = None,
        department_id: UUID | None = None,
        department_ref: str | None = Field(default=None, max_length=200),
        employee_id: UUID | None = None,
        supervisor_id: UUID | None = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        page: int = Field(default=1, ge=1),
        page_size: int = Field(default=25, ge=1, le=100),
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE, PHONE_SCOPE)
        return await _read(
            client,
            "/mcp-read/call-phone-records",
            phone_number=phone_number,
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
            supervisor_id=supervisor_id,
            page=page,
            page_size=page_size,
            **_period_params(period, start_date, end_date, default="all"),
        )

    @mcp.tool(
        title="Каталог транскрипций звонков",
        description=(
            "Возвращает только доступные текущему аккаунту звонки, наличие транскрипции и "
            "короткое превью без телефонов и аудио. Поддерживает фильтры отдела, сотрудника, "
            "сценария и периода; недоступный фильтр никогда не расширяется до всех звонков. "
            "Фильтр сценария охватывает все его версии; звонки, которые нельзя отнести к "
            "сценарию, перечислены в effective_scope.excluded_calls."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def list_call_transcripts(
        period: PeriodParam = None,
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        scenario_id: UUID | None = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        transcript_format: TranscriptFormat = "diarized",
        preview_chars: int = Field(default=300, ge=0, le=PREVIEW_MAX_CHARS),
        page: int = Field(default=1, ge=1),
        page_size: int = Field(default=25, ge=1, le=PAGE_MAX_ITEMS),
    ) -> ToolResult:
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
            "одного доступного звонка. Длинный текст читайте кусками: передавайте "
            "next_start_char как следующий start_char до null; сверяйте source_sha256. "
            "Недоступный и несуществующий UUID дают одинаковый not_available; "
            "телефоны, аудио и внутренние поля звонка не возвращаются."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def get_call_transcript(
        call_id: UUID,
        transcript_format: TranscriptFormat = "diarized",
        max_chars: TranscriptChunkChars = 16000,
        max_segments: int = Field(default=500, ge=1, le=2000),
        start_char: int = Field(default=0, ge=0),
        start_segment: int = Field(default=0, ge=0),
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Optional exact visible department guard."
        ),
        employee_id: UUID | None = None,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE, TRANSCRIPT_SCOPE)
        return await _read(
            client,
            f"/mcp-read/call-transcript/{call_id}",
            transcript_format=transcript_format,
            max_chars=max_chars,
            max_segments=max_segments,
            start_char=start_char,
            start_segment=start_segment,
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
        )

    @mcp.tool(
        title="Поиск по транскрипциям",
        description=(
            "Ищет фразу или слова только в транскрипциях звонков из текущего ACL и возвращает "
            "контекстные фрагменты с безопасными карточками звонков. Ответ явно показывает "
            "число просмотренных звонков и полноту поиска. Фильтр сценария охватывает все его версии."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def search_call_transcripts(
        query: str = Field(min_length=2, max_length=500),
        period: PeriodParam = None,
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        scenario_id: UUID | None = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        match_mode: TranscriptMatchMode = "phrase",
        context_chars: int = Field(default=240, ge=40, le=PREVIEW_MAX_CHARS),
        limit: int = Field(default=25, ge=1, le=PAGE_MAX_ITEMS),
    ) -> ToolResult:
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
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        transcript_format: TranscriptFormat = "diarized",
        preview_chars: int = Field(default=300, ge=0, le=PREVIEW_MAX_CHARS),
        page: int = Field(default=1, ge=1),
        page_size: int = Field(default=25, ge=1, le=PAGE_MAX_ITEMS),
    ) -> ToolResult:
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
            "одновременно проверяя доступ к руководителю и принадлежность звонка ему. "
            "Для полного длинного текста повторяйте запрос с next_start_char до null."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def get_supervisor_call_transcript(
        supervisor_id: UUID,
        call_id: UUID,
        transcript_format: TranscriptFormat = "diarized",
        max_chars: TranscriptChunkChars = 16000,
        max_segments: int = Field(default=500, ge=1, le=2000),
        start_char: int = Field(default=0, ge=0),
        start_segment: int = Field(default=0, ge=0),
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE, TRANSCRIPT_SCOPE)
        return await _read(
            client,
            f"/mcp-read/supervisor-call-transcript/{call_id}",
            supervisor_id=supervisor_id,
            transcript_format=transcript_format,
            max_chars=max_chars,
            max_segments=max_segments,
            start_char=start_char,
            start_segment=start_segment,
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
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        match_mode: TranscriptMatchMode = "phrase",
        context_chars: int = Field(default=240, ge=40, le=PREVIEW_MAX_CHARS),
        limit: int = Field(default=25, ge=1, le=PAGE_MAX_ITEMS),
    ) -> ToolResult:
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
        description=(
            "Недельный план звонков отдела и факт за период: по каждому пункту плана — направление, "
            "клиенты, что засчитывается, план на дни периода и факт. Без отдела и сотрудника — все "
            "видимые отделы. Пункты не складываются между собой; mode=null — план не задан, fact=null — нет "
            "данных, и то и другое не ноль. "
            "Имена канонизированы по employee_roster_grounding; не добавляйте людей вне этого списка."
        ),
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
    ) -> ToolResult:
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
            "недозвонов и пропущенных, а для отделов, которые ведут касания "
            "(client_touch_metrics_applicable=true), — первых/повторных касаний и возвратов "
            "из повторного в первое касание после new_client_touch_reset_days дней."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_client_statistics(
        period: PeriodParam = None,
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
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
    ) -> ToolResult:
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
        period: PeriodParam = None,
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        limit: int = Field(default=20, ge=1, le=100),
    ) -> ToolResult:
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
        description=(
            "Наставнические задачи. В новом OKK наставничества нет: ответ not_available "
            "(mentoring_not_kept_by_okk), это не ноль задач. Старый API OKK отдавал ограниченное окно "
            "истории: статусы, сроки, просрочка, completion rate и скорость выполнения."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_mentoring_statistics(
        period: PeriodParam = None,
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        task_status: TaskStatus | None = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        page: int = Field(default=1, ge=1),
        page_size: int = Field(default=50, ge=1, le=100),
    ) -> ToolResult:
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
        description=(
            "Безопасный каталог доступных активных и исторических сценариев без промптов, скриптов и "
            "маршрутизации. Число оценок и средний балл сценария — в get_scenario_performance."
        ),
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
    ) -> ToolResult:
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
    ) -> ToolResult:
        _require_scopes(SCENARIO_SCOPE)
        return await _read(
            client,
            f"/mcp-read/scenario-criteria/{scenario_id}",
            department_id=department_id,
            department_ref=department_ref,
        )

    @mcp.tool(
        title="Эффективность сценариев",
        description=(
            "Сравнивает сценарии по числу оценок, охвату сотрудников, среднему/минимальному/максимальному баллу и "
            "pass rate — доле оценок не ниже нормы. pass_rate_basis: department_norm — норма отдела, сохранённая "
            "с оценкой (признак OKK у звонка); scenario_threshold — порог сценария (старый API OKK); null — "
            "нормы нет или признаки смешаны, доля неизвестна. Сценарий — все его версии: оценки архивных "
            "версий входят в строку сценария (versions[]). Оценки, которые нельзя отнести к сценарию, "
            "перечислены в effective_scope.excluded_evaluations, и ответ тогда partial."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, SCENARIO_SCOPE),
        structured_output=True,
    )
    async def get_scenario_performance(
        period: PeriodParam = None,
        scenario_ids: list[UUID] | None = Field(default=None, max_length=100),
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
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
        description=(
            "Показывает самые сильные/слабые критерии: наблюдения, охват, средний балл/процент, штрафы и "
            "последнее наблюдение. Критерий — его код в сценарии по всем версиям сценария. Наблюдения, "
            "которые нельзя отнести к критерию (например, критерии архивной версии недоступны аккаунту), "
            "перечислены в effective_scope.excluded_*, и ответ тогда partial."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, SCENARIO_SCOPE),
        structured_output=True,
    )
    async def get_criterion_performance(
        period: PeriodParam = None,
        scenario_id: UUID | None = None,
        criterion_ids: list[UUID] | None = Field(default=None, max_length=100),
        department_id: UUID | None = None,
        department_ref: str | None = Field(
            default=None, max_length=200, description="Exact visible department code or name."
        ),
        employee_id: UUID | None = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        limit: int = Field(default=100, ge=1, le=500),
    ) -> ToolResult:
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
