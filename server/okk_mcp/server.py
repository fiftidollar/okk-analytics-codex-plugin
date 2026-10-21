"""Strictly typed, read-only MCP tools for OKK business analytics."""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, RequireAuthMiddleware
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import AnyHttpUrl, BaseModel, Field
from starlette.applications import Starlette
from starlette.routing import Route

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
TranscriptFormat = Literal["raw", "diarized", "segments"]
TranscriptMatchMode = Literal["phrase", "all_terms", "any_terms"]

# Parameters shared by many tools: one name and one meaning everywhere. Descriptions stay short:
# the tool list is sent to the model on every turn.
PeriodParam = Annotated[
    Period | None, Field(description="Moscow time; default month. Omit when giving dates.")
]
StartDate = Annotated[date | None, Field(description="YYYY-MM-DD; with end_date instead of period.")]
EndDate = Annotated[date | None, Field(description="YYYY-MM-DD, inclusive.")]
DepartmentParam = Annotated[
    str | None, Field(max_length=200, description="Code or name from get_access_context, e.g. csm.")
]
EmployeeParam = Annotated[UUID | None, Field(description="Id from list_employees.")]
ScenarioParam = Annotated[UUID | None, Field(description="Id from list_scenarios; all its versions.")]
CallIdParam = Annotated[UUID, Field(description="Call id.")]
SupervisorParam = Annotated[UUID, Field(description="Id from list_supervisors.")]
SearchParam = Annotated[str | None, Field(max_length=200, description="Part of a name.")]
PageParam = Annotated[int, Field(ge=1)]
FormatParam = Annotated[
    TranscriptFormat, Field(description="diarized — with speaker labels; segments — timed turns.")
]

# Response size limits. Claude Code warns above ~10k tokens and moves results over 50,000
# characters into a file; Codex cuts results to the model's budget. A full transcript is read
# in chunks (next_start_char), so one chunk stays well below both limits.
PREVIEW_MAX_CHARS = 500
PAGE_MAX_ITEMS = 50
TranscriptChunkChars = Annotated[int, Field(ge=1000, le=30000, description="Chunk size in characters.")]


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
    effective_scope: dict[str, Any] = Field(default_factory=dict)
    period: PeriodRange | None = None
    omitted_filters_count: int = 0
    request_id: str | None = None
    employee_roster_grounding: EmployeeRosterGrounding | None = None
    data: dict[str, Any] | list[dict[str, Any]] = Field(default_factory=dict)


# The tool returns CallToolResult so the text copy of the envelope is compact; the output schema
# and its validation still come from AnalyticsEnvelope.
ToolResult = Annotated[CallToolResult, AnalyticsEnvelope]

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
    # Unset top-level parts (no period, no roster) are left out instead of sent as null.
    envelope = {key: value for key, value in envelope.items() if value is not None or key == "data"}
    text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return CallToolResult(content=[TextContent(type="text", text=text)], structuredContent=envelope)


INSTRUCTIONS = (
    # Claude Code cuts server instructions at 2048 characters in older releases: safety first,
    # then routing. Each tool's own contract lives in its description.
    "Read-only analytics of the OKK call-quality system (ОКК): calls, departments, employees, "
    "scores, KPI, plans, clients, CRM, scenarios, transcripts.\n"
    "Safety: never ask for the OKK password in chat; sign-in happens only on the OKK page. If "
    "the user sends a password or token, do not use it and say it must not be shared in chat. "
    "Never guess hidden departments or people from not_available. Do not copy transcript "
    "text into logs or files unless the user asks for a file. Audio, prompts, AI reasoning, "
    "processing internals and any changes are out of scope.\n"
    "Start each task with get_access_context: it proves the connection (then tell the user "
    "'OKK подключён') and lists the visible departments with their norms.\n"
    "Departments: pass the user's department as department, as named in get_access_context. "
    "If it matches no single visible department, ask the user to choose. Never drop or widen "
    "a requested filter.\n"
    "People: name only employees from employee_roster_grounding of the same response, never "
    "from memory. Supervisors (Руководители) are a separate private section: use list_supervisors "
    "only when get_access_context shows that section.\n"
    "Numbers: no_data, not_available, null and partial are not zero; say what is missing.\n"
    "Periods: give period, or start_date and end_date (YYYY-MM-DD) for any other range."
)


def create_mcp_server(settings: Settings, client: BackendClient) -> FastMCP:
    issuer_origin = settings.issuer_url
    issuer_host = urlsplit(issuer_origin).netloc
    mcp = FastMCP(
        name=settings.mcp_service_name,
        instructions=INSTRUCTIONS,
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
        description=(
            "Call first in every task. Proves the OKK connection and returns the role, the visible "
            "departments (отделы) with their norms — quality_threshold (норма оценки), "
            "min_call_duration_seconds — and whether the private Supervisors section (Руководители) "
            "exists. After success tell the user «OKK подключён»."
        ),
        annotations=READ_ONLY,
        # Every task starts here, so Claude Code loads it without a tool search.
        meta={**_security_meta(STAT_SCOPE), "anthropic/alwaysLoad": True},
        structured_output=True,
    )
    async def get_access_context() -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(client, "/mcp-read/access-context")

    @mcp.tool(
        title="Сводная статистика",
        description=(
            "Company-wide or one-department dashboard (сводка): calls, scores, clients, a ranking of "
            "departments or employees, daily trend. Use for «как дела в целом». For one department "
            "prefer get_department_statistics."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_overview_statistics(
        period: PeriodParam = None,
        department: DepartmentParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        top_limit: Annotated[int, Field(ge=1, le=50, description="Rows in each ranking.")] = 20,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/overview-statistics",
            department_ref=department,
            top_limit=top_limit,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Статистика отдела",
        description=(
            "One department's report (отчёт по отделу): calls, average score (средний балл), share "
            "at or above the norm, missed calls, duration, every employee's metrics "
            "(complete_employee_ranking) and the call plan. Field meanings are in "
            "data.reporting_contract: successful calls, calls from the minimum duration and evaluated "
            "calls are different counts. detail=full adds daily trends. Name only employees from "
            "employee_roster_grounding."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_department_statistics(
        department: Annotated[
            str,
            Field(
                min_length=1,
                max_length=200,
                description="Department (отдел) code, name or id from get_access_context.",
            ),
        ],
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        detail: Annotated[
            Literal["summary", "full"],
            Field(description="full adds daily trends per department and employee."),
        ] = "summary",
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/department-statistics",
            department_ref=department,
            detail=detail,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Сравнение отделов",
        description=(
            "Compares visible departments (сравнение отделов) by call volume, scores and trend for one "
            "period. Departments that are not visible are left out and counted in "
            "omitted_filters_count."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def compare_departments(
        departments: Annotated[
            list[str] | None,
            Field(max_length=20, description="Department codes or names; empty — all visible departments."),
        ] = None,
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/compare-departments",
            department_refs=departments,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Список сотрудников",
        description=(
            "Finds employees (сотрудники, менеджеры) of visible departments by name; returns ids for "
            "other tools. No contact data. A name that is not found is not in OKK: say so, never "
            "guess another person."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def list_employees(
        department: DepartmentParam = None,
        search: SearchParam = None,
        include_inactive: Annotated[bool, Field(description="Include dismissed employees.")] = False,
        page: PageParam = 1,
        page_size: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/employees",
            department_ref=department,
            search=search,
            include_inactive=include_inactive,
            page=page,
            page_size=page_size,
        )

    @mcp.tool(
        title="Доступные руководители",
        description=(
            "The private Supervisors section (Руководители) granted to this account personally; not "
            "a department and not implied by the admin role. Supervisors have call volume and "
            "transcripts only — no scores, clients or CRM."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def list_supervisors(search: SearchParam = None) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(client, "/mcp-read/supervisors", search=search)

    @mcp.tool(
        title="Статистика звонков руководителя",
        description=(
            "Call volume, duration, directions and daily series of one supervisor from "
            "list_supervisors. Scores, scenarios, clients and CRM do not exist for supervisors."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_supervisor_call_statistics(
        supervisor_id: SupervisorParam,
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
            "One employee's card (карточка сотрудника) for one period: KPI, calls, score, call plan, "
            "client and CRM metrics, AI strengths, growth areas (зоны роста) and weekly focus. OKK "
            "keeps no mentoring tasks."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_employee_card(
        employee_id: Annotated[UUID, Field(description="Employee id from list_employees or a ranking.")],
        department: DepartmentParam = None,
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            f"/mcp-read/employee-card/{employee_id}",
            department_ref=department,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Сравнение сотрудников",
        description=(
            "Compares up to 20 employees side by side: KPI, strengths, growth areas and weekly focus "
            "for one period."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def compare_employees(
        employee_ids: Annotated[
            list[UUID], Field(min_length=1, max_length=20, description="Employee ids from list_employees.")
        ],
        department: DepartmentParam = None,
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/compare-employees",
            employee_ids=employee_ids,
            department_ref=department,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Статистика звонков",
        description=(
            "Evaluated calls only: count, average, min and max score, share at or above the norm "
            "(pass rate), duration and daily trend, for the company, a department or an employee. "
            "Missed and short calls are in get_department_statistics."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_call_statistics(
        period: PeriodParam = None,
        department: DepartmentParam = None,
        employee_id: EmployeeParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/call-statistics",
            department_ref=department,
            employee_id=employee_id,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Звонки по номеру телефона",
        description=(
            "Calls with one client phone number (номер телефона): matching_calls_total is the exact "
            "count of all records — missed, short and in processing included — not the page length. "
            "Without a number, a page of the journal with numbers. Default period: all history."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, PHONE_SCOPE),
        structured_output=True,
    )
    async def list_call_phone_records(
        phone_number: Annotated[
            str | None,
            Field(
                min_length=3, max_length=40, description="Phone number in any format, or its last 4+ digits."
            ),
        ] = None,
        period: Annotated[
            Period | None,
            Field(
                description="Named period; default all — the whole journal history. Omit it for a date range."
            ),
        ] = None,
        department: DepartmentParam = None,
        employee_id: EmployeeParam = None,
        supervisor_id: Annotated[
            UUID | None, Field(description="Supervisor id from list_supervisors.")
        ] = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        page: PageParam = 1,
        page_size: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE, PHONE_SCOPE)
        return await _read(
            client,
            "/mcp-read/call-phone-records",
            phone_number=phone_number,
            department_ref=department,
            employee_id=employee_id,
            supervisor_id=supervisor_id,
            page=page,
            page_size=page_size,
            **_period_params(period, start_date, end_date, default="all"),
        )

    @mcp.tool(
        title="Каталог транскрипций звонков",
        description=(
            "Lists calls (звонки) newest first with score, scenario, employee and a short transcript "
            "preview; filter by department, employee, scenario and period. Use it to find a call, "
            "then read it with get_call_transcript."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def list_call_transcripts(
        period: PeriodParam = None,
        department: DepartmentParam = None,
        employee_id: EmployeeParam = None,
        scenario_id: ScenarioParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        transcript_format: FormatParam = "diarized",
        preview_chars: Annotated[int, Field(ge=0, le=PREVIEW_MAX_CHARS)] = 300,
        page: PageParam = 1,
        page_size: Annotated[int, Field(ge=1, le=PAGE_MAX_ITEMS)] = 25,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE, TRANSCRIPT_SCOPE)
        return await _read(
            client,
            "/mcp-read/call-transcripts",
            department_ref=department,
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
            "Full text (расшифровка) of one call. A long text comes in chunks: repeat with "
            "start_char=next_start_char until it is null; source_sha256 stays the same. No audio."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def get_call_transcript(
        call_id: CallIdParam,
        transcript_format: FormatParam = "diarized",
        max_chars: TranscriptChunkChars = 16000,
        max_segments: Annotated[int, Field(ge=1, le=2000)] = 500,
        start_char: Annotated[int, Field(ge=0)] = 0,
        start_segment: Annotated[int, Field(ge=0)] = 0,
        department: DepartmentParam = None,
        employee_id: EmployeeParam = None,
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
            department_ref=department,
            employee_id=employee_id,
        )

    @mcp.tool(
        title="Поиск по транскрипциям",
        description=(
            "Finds a phrase or words in call transcripts (поиск по разговорам) and returns the calls "
            "with quotes. scanned_calls and result_complete show how much was searched: an incomplete "
            "search is no proof that the phrase is absent — narrow the period or add employee_id."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def search_call_transcripts(
        query: Annotated[str, Field(min_length=2, max_length=500, description="Phrase or words to find.")],
        period: PeriodParam = None,
        department: DepartmentParam = None,
        employee_id: EmployeeParam = None,
        scenario_id: ScenarioParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        match_mode: Annotated[
            TranscriptMatchMode,
            Field(description="phrase — exact phrase; all_terms — every word; any_terms — any word."),
        ] = "phrase",
        context_chars: Annotated[int, Field(ge=40, le=PREVIEW_MAX_CHARS)] = 240,
        limit: Annotated[int, Field(ge=1, le=PAGE_MAX_ITEMS)] = 25,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE, TRANSCRIPT_SCOPE)
        return await _read(
            client,
            "/mcp-read/search-call-transcripts",
            query=query,
            department_ref=department,
            employee_id=employee_id,
            scenario_id=scenario_id,
            match_mode=match_mode,
            context_chars=context_chars,
            limit=limit,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Транскрипции звонков руководителя",
        description="Lists calls of one supervisor from list_supervisors with transcript previews.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def list_supervisor_call_transcripts(
        supervisor_id: SupervisorParam,
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        transcript_format: FormatParam = "diarized",
        preview_chars: Annotated[int, Field(ge=0, le=PREVIEW_MAX_CHARS)] = 300,
        page: PageParam = 1,
        page_size: Annotated[int, Field(ge=1, le=PAGE_MAX_ITEMS)] = 25,
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
            "Full text of one supervisor call; chunked like get_call_transcript (repeat with "
            "start_char=next_start_char until null)."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def get_supervisor_call_transcript(
        supervisor_id: SupervisorParam,
        call_id: CallIdParam,
        transcript_format: FormatParam = "diarized",
        max_chars: TranscriptChunkChars = 16000,
        max_segments: Annotated[int, Field(ge=1, le=2000)] = 500,
        start_char: Annotated[int, Field(ge=0)] = 0,
        start_segment: Annotated[int, Field(ge=0)] = 0,
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
        description="Finds a phrase or words in the transcripts of one supervisor from list_supervisors.",
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, TRANSCRIPT_SCOPE),
        structured_output=True,
    )
    async def search_supervisor_call_transcripts(
        supervisor_id: SupervisorParam,
        query: Annotated[str, Field(min_length=2, max_length=500, description="Phrase or words to find.")],
        period: PeriodParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        match_mode: Annotated[
            TranscriptMatchMode,
            Field(description="phrase — exact phrase; all_terms — every word; any_terms — any word."),
        ] = "phrase",
        context_chars: Annotated[int, Field(ge=40, le=PREVIEW_MAX_CHARS)] = 240,
        limit: Annotated[int, Field(ge=1, le=PAGE_MAX_ITEMS)] = 25,
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
            "Weekly call plan (план звонков) and its fact for a date range, per department or one "
            "employee; without filters — all visible departments. Report every plan item separately, "
            "never add them up. mode=null — no plan set; fact=null — no data; neither is zero."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_plan_fact_statistics(
        start_date: Annotated[date, Field(description="First day, YYYY-MM-DD.")],
        end_date: Annotated[date, Field(description="Last day, YYYY-MM-DD, inclusive.")],
        department: DepartmentParam = None,
        employee_id: EmployeeParam = None,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        return await _read(
            client,
            "/mcp-read/plan-fact-statistics",
            start_date=start_date,
            end_date=end_date,
            department_ref=department,
            employee_id=employee_id,
        )

    @mcp.tool(
        title="Статистика клиентов",
        description=(
            "Client metrics (клиенты): calls with new and existing clients, effective contacts, "
            "missed (пропущенные) and unanswered calls; for departments that track touches "
            "(client_touch_metrics_applicable=true) — first and repeat touches."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_client_statistics(
        period: PeriodParam = None,
        department: DepartmentParam = None,
        employee_id: EmployeeParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/client-statistics",
            department_ref=department,
            employee_id=employee_id,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Статистика CRM",
        description=(
            "Latest Bitrix CRM snapshot per employee: deals, tasks, overdue tasks (просрочки), stages. "
            "coverage shows employees whose CRM is not linked or not synced — their data is "
            "unavailable, not zero."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_crm_statistics(
        department: DepartmentParam = None,
        employee_id: EmployeeParam = None,
        snapshot_date: Annotated[
            date | None, Field(description="Snapshot day, YYYY-MM-DD; default the latest one.")
        ] = None,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/crm-statistics",
            department_ref=department,
            employee_id=employee_id,
            snapshot_date=snapshot_date,
        )

    @mcp.tool(
        title="AI-сильные стороны и зоны роста",
        description=(
            "How often AI evaluations noted each strength (сильные стороны) and growth area (зоны "
            "роста, ошибки) across calls of a department or an employee."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE),
        structured_output=True,
    )
    async def get_growth_insights(
        period: PeriodParam = None,
        department: DepartmentParam = None,
        employee_id: EmployeeParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        limit: Annotated[int, Field(ge=1, le=100, description="Items per list.")] = 20,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE)
        return await _read(
            client,
            "/mcp-read/growth-insights",
            department_ref=department,
            employee_id=employee_id,
            limit=limit,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Список сценариев",
        description=(
            "Evaluation scenarios (сценарии оценки, чек-листы) of visible departments, with ids for "
            "the criteria and performance tools. No call counts or scores here."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(SCENARIO_SCOPE),
        structured_output=True,
    )
    async def list_scenarios(
        department: DepartmentParam = None,
        search: SearchParam = None,
        include_historical: Annotated[bool, Field(description="Include archived scenario versions.")] = False,
        page: PageParam = 1,
        page_size: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> ToolResult:
        _require_scopes(SCENARIO_SCOPE)
        return await _read(
            client,
            "/mcp-read/scenarios",
            department_ref=department,
            search=search,
            include_historical=include_historical,
            page=page,
            page_size=page_size,
        )

    @mcp.tool(
        title="Критерии сценария",
        description=(
            "The criteria (критерии) of one scenario: categories, max points, levels and what each "
            "level means, critical errors (критические ошибки) that deduct points."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(SCENARIO_SCOPE),
        structured_output=True,
    )
    async def get_scenario_criteria(
        scenario_id: Annotated[UUID, Field(description="Scenario id from list_scenarios.")],
        department: DepartmentParam = None,
    ) -> ToolResult:
        _require_scopes(SCENARIO_SCOPE)
        return await _read(
            client,
            f"/mcp-read/scenario-criteria/{scenario_id}",
            department_ref=department,
        )

    @mcp.tool(
        title="Эффективность сценариев",
        description=(
            "Per scenario: evaluations, employees, average, min and max score and pass rate (share at "
            "or above the norm). A scenario includes all its versions (versions[]). Evaluations that "
            "could not be attributed are counted in effective_scope.excluded_evaluations."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, SCENARIO_SCOPE),
        structured_output=True,
    )
    async def get_scenario_performance(
        period: PeriodParam = None,
        scenario_ids: Annotated[
            list[UUID] | None,
            Field(max_length=100, description="Scenario ids; empty — all visible scenarios."),
        ] = None,
        department: DepartmentParam = None,
        employee_id: EmployeeParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE, SCENARIO_SCOPE)
        return await _read(
            client,
            "/mcp-read/scenario-performance",
            scenario_ids=scenario_ids,
            department_ref=department,
            employee_id=employee_id,
            **_period_params(period, start_date, end_date),
        )

    @mcp.tool(
        title="Эффективность критериев",
        description=(
            "How criteria were scored (слабые и сильные критерии). data.criteria: average percent of "
            "the maximum, weakest first. data.critical_errors: critical errors (стоп-слова, мат, "
            "грубость) as violations — calls where points were deducted — and violation_rate_percent."
        ),
        annotations=READ_ONLY,
        meta=_security_meta(STAT_SCOPE, SCENARIO_SCOPE),
        structured_output=True,
    )
    async def get_criterion_performance(
        period: PeriodParam = None,
        scenario_id: ScenarioParam = None,
        criterion_ids: Annotated[
            list[UUID] | None, Field(max_length=100, description="Criterion ids; empty — all criteria.")
        ] = None,
        department: DepartmentParam = None,
        employee_id: EmployeeParam = None,
        start_date: StartDate = None,
        end_date: EndDate = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Criteria in the answer.")] = 100,
    ) -> ToolResult:
        _require_scopes(STAT_SCOPE, SCENARIO_SCOPE)
        return await _read(
            client,
            "/mcp-read/criterion-performance",
            scenario_id=scenario_id,
            criterion_ids=criterion_ids,
            department_ref=department,
            employee_id=employee_id,
            limit=limit,
            **_period_params(period, start_date, end_date),
        )

    _drop_schema_titles(mcp)
    return mcp


class _ScopeChallenge(RequireAuthMiddleware):
    """The SDK's bearer check with the RFC 6750 challenge the MCP specification asks for: the
    scopes to request (`scope`), and no error code when the request carried no token at all. An
    invalid or expired token keeps `error="invalid_token"`: clients refresh on it."""

    def __init__(self, inner: RequireAuthMiddleware, scope_hint: str):
        super().__init__(inner.app, inner.required_scopes, inner.resource_metadata_url)
        self.scope_hint = scope_hint

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if not isinstance(scope.get("user"), AuthenticatedUser):
            has_token = any(name.lower() == b"authorization" for name, _ in scope.get("headers") or [])
            await self._challenge(
                send, 401, "invalid_token" if has_token else None, "Authentication required"
            )
            return
        await super().__call__(scope, receive, send)

    async def _send_auth_error(self, send: Any, status_code: int, error: str, description: str) -> None:
        await self._challenge(send, status_code, error, description)

    async def _challenge(self, send: Any, status_code: int, error: str | None, description: str) -> None:
        parts = [f'error="{error}"', f'error_description="{description}"'] if error else []
        parts.append(f'scope="{self.scope_hint}"')
        if self.resource_metadata_url:
            parts.append(f'resource_metadata="{self.resource_metadata_url}"')
        body = json.dumps({"error": error or "unauthorized", "error_description": description}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", f"Bearer {', '.join(parts)}".encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def with_scope_challenge(app: Starlette, scope_hint: str) -> Starlette:
    """Put the scope hint into the 401 of the MCP route built by the SDK."""

    for route in app.routes:
        if isinstance(route, Route) and isinstance(route.app, RequireAuthMiddleware):
            route.app = _ScopeChallenge(route.app, scope_hint)
    return app


def _drop_schema_titles(mcp: FastMCP) -> None:
    """Remove the titles pydantic derives from parameter names: they repeat the name and cost
    tokens on every turn. Arguments are still validated by the tool's own model."""

    def strip(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: strip(value) for key, value in node.items() if key != "title"}
        if isinstance(node, list):
            return [strip(value) for value in node]
        return node

    for tool in mcp._tool_manager.list_tools():
        properties = tool.parameters.get("properties", {})
        tool.parameters = {
            **{key: value for key, value in tool.parameters.items() if key not in {"title", "properties"}},
            "properties": {name: strip(schema) for name, schema in properties.items()},
        }
