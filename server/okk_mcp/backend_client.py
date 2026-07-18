"""ACL-aware aggregation over the existing, read-only OKK HTTP API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from mcp.server.auth.middleware.auth_context import get_access_token

from okk_mcp.config import Settings
from okk_mcp.platform_client import (
    AccountContext,
    OKKAuthenticationError,
    OKKNotAvailable,
    OKKPlatformClient,
    OKKUnavailable,
)

MSK = ZoneInfo("Europe/Moscow")
LOGGER = logging.getLogger("okk_mcp.analytics_trace")
UUID_IN_PATH = re.compile(
    r"/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    flags=re.IGNORECASE,
)


class BackendUnavailable(RuntimeError):
    pass


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _insight_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("text", "name", "criterion", "description", "title"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _aware_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MSK)
    return parsed.astimezone(MSK)


def _period_bounds(period: str, start_date: str | None, end_date: str | None) -> tuple[date, date]:
    today = datetime.now(MSK).date()
    if period == "custom":
        if not start_date or not end_date:
            raise ValueError("custom period requires start_date and end_date")
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    elif period == "today":
        start = end = today
    elif period == "yesterday":
        start = end = today - timedelta(days=1)
    elif period == "week":
        start, end = today - timedelta(days=today.weekday()), today
    elif period == "prev_week":
        end = today - timedelta(days=today.weekday() + 1)
        start = end - timedelta(days=6)
    elif period == "month":
        start, end = today.replace(day=1), today
    elif period == "prev_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif period == "quarter":
        start = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
        end = today
    elif period == "year":
        start, end = date(today.year, 1, 1), today
    elif period == "all":
        start, end = date(2020, 1, 1), today
    else:
        raise ValueError(f"Unsupported period: {period}")
    if end < start:
        raise ValueError("end_date must be on or after start_date")
    return start, end


def _period_query(period: str, start_date: str | None, end_date: str | None) -> dict[str, str]:
    result = {"period": period}
    if period == "custom":
        result.update({"start_date": str(start_date), "end_date": str(end_date)})
    return result


def _query(**values: Any) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            result.append((key, "true" if value else "false"))
        elif isinstance(value, list | tuple | set):
            result.extend((key, str(item)) for item in value)
        else:
            result.append((key, _iso(value) or ""))
    return result


def _selector_key(value: Any) -> str:
    """Normalize a human department selector without fuzzy guessing."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return "".join(character for character in normalized if character.isalnum())


def _department_selector_keys(row: dict[str, Any]) -> set[str]:
    name = str(row.get("name") or "").strip()
    code = str(row.get("code") or "").strip()
    keys = {_selector_key(name), _selector_key(code)}
    words = [word for word in re.findall(r"[\w]+", name, flags=re.UNICODE) if word]
    if len(words) >= 2:
        keys.add(_selector_key("".join(word[0] for word in words)))
    if words and (words[0].isupper() or any(character.isdigit() for character in words[0])):
        keys.add(_selector_key(words[0]))
    return {key for key in keys if key}


def _select_visible(requested: list[str], visible: set[str]) -> tuple[list[str], int]:
    effective: list[str] = []
    omitted = 0
    for value in requested:
        if value not in visible:
            omitted += 1
        elif value not in effective:
            effective.append(value)
    return effective, omitted


class AnalyticsAdapter:
    def __init__(
        self,
        platform: OKKPlatformClient,
        session_id: str,
        settings: Settings,
        validated_context: AccountContext | None = None,
    ):
        self.platform = platform
        self.session_id = session_id
        self.settings = settings
        self._context = validated_context
        self._departments: list[dict[str, Any]] | None = None
        self._semaphore = asyncio.Semaphore(settings.analytics_parallel_requests)

    async def _get(self, path: str, **params: Any) -> Any:
        context = await self.context()
        return await self.platform.get_with_context(context, path, params=_query(**params))

    async def _bounded(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        async with self._semaphore:
            return await factory()

    async def context(self) -> AccountContext:
        if self._context is None:
            self._context = await self.platform.live_context(self.session_id)
        return self._context

    async def departments(self) -> list[dict[str, Any]]:
        if self._departments is None:
            context = await self.context()
            if not context.is_admin and not context.department_ids:
                self._departments = []
            else:
                rows = await self._get("/departments")
                if not context.is_admin:
                    allowed = set(context.department_ids)
                    rows = [row for row in rows if str(row.get("id")) in allowed]
                self._departments = [self._safe_department(row) for row in rows]
        return self._departments

    async def access_context(self) -> dict[str, Any]:
        context = await self.context()
        return {
            "role": context.role,
            "all_departments": context.is_admin,
            "departments": [
                {"id": row["id"], "name": row["name"], "code": row["code"]}
                for row in await self.departments()
            ],
        }

    async def resolve_department(
        self,
        *,
        department_id: Any = None,
        department_ref: Any = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Resolve only against ACL-visible departments; never broaden a failed selector."""
        selectors = [value for value in (department_id, department_ref) if value not in (None, "")]
        if not selectors:
            return None, False
        departments = await self.departments()
        resolved: list[dict[str, Any]] = []
        for selector in selectors:
            raw = str(selector).strip()
            matches = [row for row in departments if str(row.get("id")) == raw]
            if not matches:
                key = _selector_key(raw)
                matches = [row for row in departments if key and key in _department_selector_keys(row)]
            if len(matches) != 1:
                return None, True
            resolved.append(matches[0])
        if len({str(row.get("id")) for row in resolved}) != 1:
            return None, True
        return resolved[0], True

    @staticmethod
    def department_scope(department: dict[str, Any] | None) -> dict[str, Any]:
        if not department:
            return {"department_id": None, "department_code": None, "department_name": None}
        return {
            "department_id": str(department.get("id")),
            "department_code": department.get("code"),
            "department_name": department.get("name"),
        }

    async def unavailable_department(self) -> dict[str, Any]:
        return await self.envelope(
            {"reason": "department_not_in_access_scope"},
            status="not_available",
            scope={"department_resolution": "not_available"},
        )

    async def envelope(
        self,
        data: Any,
        *,
        status: str = "ok",
        scope: dict[str, Any] | None = None,
        period: tuple[date, date] | None = None,
        omitted: int = 0,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "access_context": await self.access_context(),
            "effective_scope": scope or {},
            "period": {"start": period[0].isoformat(), "end": period[1].isoformat()} if period else None,
            "omitted_filters_count": omitted,
            "data": data,
        }

    @staticmethod
    def _safe_department(row: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "id",
            "name",
            "code",
            "description",
            "is_active",
            "min_call_duration",
            "passing_score",
            "weekly_call_plan",
            "created_at",
            "updated_at",
        )
        return {key: row.get(key) for key in keys if key in row}

    @staticmethod
    def _safe_employee(row: dict[str, Any]) -> dict[str, Any]:
        department = row.get("department")
        return {
            "id": str(row.get("id")),
            "full_name": row.get("full_name"),
            "department_id": str(row.get("department_id")) if row.get("department_id") else None,
            "department": {
                "id": str(department.get("id")),
                "name": department.get("name"),
                "code": department.get("code"),
            }
            if isinstance(department, dict)
            else None,
            "position": row.get("position"),
            "is_active": bool(row.get("is_active", True)),
            "focus_text": row.get("focus_text"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def _safe_task(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "id",
                "employee_id",
                "call_id",
                "focus_area",
                "description",
                "status",
                "ai_recommendation",
                "due_date",
                "completed_at",
                "created_at",
                "updated_at",
            )
        }

    @staticmethod
    def _safe_crm(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "status",
                "employee_id",
                "snapshot_date",
                "fetched_at",
                "total_open_deals",
                "deals_without_tasks",
                "deals_with_overdue_tasks",
                "b2b_responsible_assigned",
                "distribution_in_work_qualification",
                "networks_unprocessed",
                "networks_open_total",
                "stage_counts",
                "funnel_counts",
                "stage_labels",
                "funnel_labels",
                "card_completeness_status",
            )
        }

    @staticmethod
    def _safe_scenario(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: row.get(key)
            for key in (
                "id",
                "department_id",
                "name",
                "code",
                "description",
                "passing_score",
                "is_active",
                "is_default",
                "calls_evaluated",
                "avg_score",
                "created_at",
                "updated_at",
            )
        }

    @staticmethod
    def _criteria(row: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for category in sorted(row.get("categories") or [], key=lambda item: item.get("sort_order", 0)):
            category_safe = {
                key: category.get(key)
                for key in (
                    "id",
                    "name",
                    "description",
                    "is_critical",
                    "sort_order",
                )
            }
            for item in sorted(category.get("items") or [], key=lambda value: value.get("sort_order", 0)):
                result.append(
                    {
                        "category": category_safe,
                        **{
                            key: item.get(key)
                            for key in (
                                "id",
                                "name",
                                "description",
                                "max_score",
                                "scoring_type",
                                "scoring_config",
                                "positive_indicators",
                                "negative_indicators",
                                "is_required",
                                "is_critical",
                                "sort_order",
                                "not_applicable_condition",
                            )
                        },
                    }
                )
        return result

    async def employees(
        self,
        *,
        department_id: str | None = None,
        search: str | None = None,
        include_inactive: bool = False,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        visible_departments = {str(row["id"]) for row in await self.departments()}
        if department_id and department_id not in visible_departments:
            return [], False, 0
        context = await self.context()
        if not context.is_admin and not context.department_ids:
            return [], True, 0
        rows: list[dict[str, Any]] = []
        page = 1
        total = 0
        while len(rows) < self.settings.analytics_max_employees:
            payload = await self._get(
                "/employees",
                department_id=department_id,
                search=search,
                is_active=None if include_inactive else True,
                page=page,
                page_size=100,
            )
            total = int(payload.get("total") or 0)
            rows.extend(self._safe_employee(item) for item in payload.get("items") or [])
            if len(rows) >= self.settings.analytics_max_employees and (
                page < int(payload.get("pages") or 1) or len(rows) > self.settings.analytics_max_employees
            ):
                return rows[: self.settings.analytics_max_employees], False, total or len(rows)
            if page >= int(payload.get("pages") or 1):
                return rows, True, total or len(rows)
            page += 1
        return rows[: self.settings.analytics_max_employees], False, total or len(rows)

    async def employee(self, employee_id: str) -> dict[str, Any] | None:
        try:
            row = self._safe_employee(await self._get(f"/employees/{employee_id}"))
        except OKKNotAvailable:
            return None
        visible_departments = {str(item["id"]) for item in await self.departments()}
        if row.get("department_id") not in visible_departments:
            return None
        return row

    async def scoped_employee(
        self,
        employee_id: str | None,
        department: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not employee_id:
            return None
        row = await self.employee(employee_id)
        if not row:
            return None
        if department and row.get("department_id") != str(department.get("id")):
            return None
        return row

    async def resolve_entity_scope(
        self,
        *,
        department_id: Any = None,
        department_ref: Any = None,
        employee_id: Any = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
        department, selector_supplied = await self.resolve_department(
            department_id=department_id, department_ref=department_ref
        )
        if selector_supplied and not department:
            return None, None, True
        employee = None
        if employee_id:
            employee = await self.scoped_employee(str(employee_id), department)
            if not employee:
                return department, None, True
            if department is None:
                employee_department_id = str(employee.get("department_id") or "")
                department = next(
                    (row for row in await self.departments() if str(row.get("id")) == employee_department_id),
                    None,
                )
                if department is None:
                    return None, None, True
        return department, employee, False

    async def scenarios(
        self, department_id: str | None = None, *, include_inactive: bool = False
    ) -> tuple[list[dict[str, Any]], bool]:
        visible = {str(row["id"]) for row in await self.departments()}
        if department_id and department_id not in visible:
            return [], False
        context = await self.context()
        include_archive = include_inactive and context.is_admin
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = await self._get(
                "/scenarios",
                department_id=department_id,
                include_inactive=include_archive,
                page=page,
                page_size=100,
            )
            rows.extend(payload.get("items") or [])
            if page >= int(payload.get("pages") or 1):
                return rows, True
            page += 1

    async def calls(
        self,
        *,
        start: date,
        end: date,
        department_id: str | None = None,
        employee_id: str | None = None,
        scenario_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        rows: list[dict[str, Any]] = []
        page = 1
        total = 0
        while len(rows) < self.settings.analytics_max_calls:
            payload = await self._get(
                "/calls",
                department_id=department_id,
                employee_id=employee_id,
                scenario_id=scenario_id,
                date_from=start,
                date_to=end,
                page=page,
                page_size=100,
            )
            total = int(payload.get("total") or 0)
            rows.extend(payload.get("items") or [])
            if len(rows) >= self.settings.analytics_max_calls and (
                page < int(payload.get("pages") or 1) or len(rows) > self.settings.analytics_max_calls
            ):
                return rows[: self.settings.analytics_max_calls], total, False
            if page >= int(payload.get("pages") or 1):
                return rows, total, True
            page += 1
        return rows[: self.settings.analytics_max_calls], total, False

    async def dispatch(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        routes = {
            "/mcp-read/access-context": self.get_access_context,
            "/mcp-read/statistics-catalog": self.get_statistics_catalog,
            "/mcp-read/overview-statistics": self.get_overview_statistics,
            "/mcp-read/departments": self.list_departments,
            "/mcp-read/department-statistics": self.get_department_statistics,
            "/mcp-read/compare-departments": self.compare_departments,
            "/mcp-read/employees": self.list_employees,
            "/mcp-read/compare-employees": self.compare_employees,
            "/mcp-read/call-statistics": self.get_call_statistics,
            "/mcp-read/plan-fact-statistics": self.get_plan_fact_statistics,
            "/mcp-read/client-statistics": self.get_client_statistics,
            "/mcp-read/crm-statistics": self.get_crm_statistics,
            "/mcp-read/growth-insights": self.get_growth_insights,
            "/mcp-read/mentoring-statistics": self.get_mentoring_statistics,
            "/mcp-read/scenarios": self.list_scenarios,
            "/mcp-read/scenario-performance": self.get_scenario_performance,
            "/mcp-read/criterion-performance": self.get_criterion_performance,
        }
        if path.startswith("/mcp-read/department-statistics/"):
            params["department_id"] = path.rsplit("/", 1)[-1]
            return await self.get_department_statistics(**params)
        if path.startswith("/mcp-read/employee-card/"):
            params["employee_id"] = path.rsplit("/", 1)[-1]
            return await self.get_employee_card(**params)
        if path.startswith("/mcp-read/scenario-criteria/"):
            params["scenario_id"] = path.rsplit("/", 1)[-1]
            return await self.get_scenario_criteria(**params)
        handler = routes.get(path)
        if not handler:
            raise ValueError("Unknown read-only analytics route")
        return await handler(**params)

    async def get_access_context(self, **_: Any) -> dict[str, Any]:
        return await self.envelope(await self.access_context())

    async def get_statistics_catalog(self, **_: Any) -> dict[str, Any]:
        domains = [
            {"domain": "overview", "metrics": ["calls", "quality", "duration", "rankings", "trends"]},
            {
                "domain": "clients",
                "metrics": ["new", "regular", "contacts", "repeats", "missed", "no_answer"],
            },
            {"domain": "employees", "metrics": ["card", "strengths", "growth_areas", "focus", "mentoring"]},
            {"domain": "plans", "metrics": ["total", "inbound", "outbound", "new", "regular", "daily"]},
            {
                "domain": "scenarios",
                "metrics": ["catalog", "criteria", "scenario_performance", "criterion_performance"],
            },
            {"domain": "crm", "metrics": ["deals", "tasks", "overdue", "stages", "funnels", "coverage"]},
        ]
        return await self.envelope(
            {
                "domains": domains,
                "tool_routing": [
                    {"tool": "get_access_context", "use_for": "current role and visible departments"},
                    {"tool": "get_statistics_catalog", "use_for": "available metrics and contracts"},
                    {"tool": "get_overview_statistics", "use_for": "overall or one-department dashboard"},
                    {"tool": "list_departments", "use_for": "visible department directory and KPI settings"},
                    {
                        "tool": "get_department_statistics",
                        "use_for": "one department KPI, ranking, trend and plan/fact",
                    },
                    {
                        "tool": "compare_departments",
                        "use_for": "comparison of two or more visible departments",
                    },
                    {"tool": "list_employees", "use_for": "ACL-scoped employee directory"},
                    {"tool": "get_employee_card", "use_for": "one employee KPI, insights, mentoring and CRM"},
                    {
                        "tool": "compare_employees",
                        "use_for": "employee comparison, optionally guarded by department",
                    },
                    {"tool": "get_call_statistics", "use_for": "call volume, quality and daily trend"},
                    {"tool": "get_plan_fact_statistics", "use_for": "employee and department call plan/fact"},
                    {"tool": "get_client_statistics", "use_for": "client/contact/repeat/no-answer metrics"},
                    {"tool": "get_crm_statistics", "use_for": "latest Bitrix snapshots only"},
                    {"tool": "get_growth_insights", "use_for": "aggregated AI strengths and growth areas"},
                    {"tool": "get_mentoring_statistics", "use_for": "bounded mentoring-task window"},
                    {"tool": "list_scenarios", "use_for": "visible scenario catalog"},
                    {"tool": "get_scenario_criteria", "use_for": "business criteria of one visible scenario"},
                    {
                        "tool": "get_scenario_performance",
                        "use_for": "scenario score and pass-rate comparison",
                    },
                    {
                        "tool": "get_criterion_performance",
                        "use_for": "criterion-level observations and scores",
                    },
                ],
                "department_filter_contract": {
                    "department_id": "visible department UUID when already known",
                    "department_ref": "exact visible code or name from the user's request, case-insensitive",
                    "resolution": "ACL-visible departments only; UUID, exact code/name, or unique displayed acronym",
                    "failed_resolution": "status=not_available; never fall back to all departments",
                    "cross_filter": "an employee must belong to the resolved department",
                    "verification": "read effective_scope.department_id/code/name before describing results",
                },
                "status_contract": {
                    "ok": "complete result for the exposed source",
                    "partial": "bounded upstream history or configured population/call cap",
                    "no_data": "scope is accessible but contains no matching observations",
                    "not_available": "selector/entity is outside the visible scope or source cannot supply it",
                    "temporarily_unavailable": "retryable upstream outage",
                },
                "completeness_contract": {
                    "employees": "source_total/source_complete distinguish full population from configured cap",
                    "calls": "source_calls/source_calls_total and partial identify truncation",
                    "mentoring": "active 5 and completed 10 tasks per employee; always a bounded window",
                    "crm": "latest snapshot per employee only; requested historical dates are never mislabeled",
                    "historical_scenarios": "available only when the connected OKK role exposes them",
                },
                "explicit_exclusions": [
                    "audio",
                    "transcripts",
                    "raw_prompts",
                    "prompt_runtime",
                    "raw_reasoning",
                    "scripts",
                    "megafon",
                    "pipeline",
                    "routing",
                    "bulk_operations",
                    "writes",
                ],
                "source": "existing OKK read-only HTTP API",
            }
        )

    async def get_overview_statistics(
        self,
        period: str = "month",
        department_id: Any = None,
        department_ref: Any = None,
        start_date: str | None = None,
        end_date: str | None = None,
        top_limit: int = 20,
        **_: Any,
    ) -> dict[str, Any]:
        bounds = _period_bounds(period, start_date, end_date)
        department_row, selector_supplied = await self.resolve_department(
            department_id=department_id, department_ref=department_ref
        )
        if selector_supplied and not department_row:
            return await self.unavailable_department()
        department = str(department_row["id"]) if department_row else None
        context = await self.context()
        if not context.is_admin and not context.department_ids:
            return await self.envelope({}, status="no_data", period=bounds)
        common = _period_query(period, start_date, end_date)
        summary, trend, ranking, departments = await asyncio.gather(
            self._get("/dashboard/summary", department_id=department, **common),
            self._get("/dashboard/calls-trend", department_id=department, **common),
            self._get("/dashboard/top-employees", department_id=department, limit=top_limit, **common),
            self._get("/dashboard/by-department", **common),
        )
        if department:
            departments = [
                row for row in departments if str(row.get("department_id") or row.get("id")) == department
            ]
        return await self.envelope(
            {
                "summary": summary,
                "daily_trend": trend,
                "employee_ranking": ranking,
                "departments": departments,
            },
            scope=self.department_scope(department_row),
            period=bounds,
        )

    async def list_departments(self, **_: Any) -> dict[str, Any]:
        rows = await self.departments()
        return await self.envelope(rows, status="ok" if rows else "no_data")

    async def get_department_statistics(
        self,
        department_id: Any = None,
        department_ref: Any = None,
        period: str = "month",
        start_date: str | None = None,
        end_date: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        department_row, selector_supplied = await self.resolve_department(
            department_id=department_id, department_ref=department_ref
        )
        if not selector_supplied or not department_row:
            return await self.unavailable_department()
        resolved_id = str(department_row["id"])
        bounds = _period_bounds(period, start_date, end_date)
        common = _period_query(period, start_date, end_date)
        (
            summary,
            trend,
            employees,
            department_summary,
            department_ranking,
            department_trends,
        ) = await asyncio.gather(
            self._get("/dashboard/summary", department_id=resolved_id, **common),
            self._get("/dashboard/calls-trend", department_id=resolved_id, **common),
            self._get("/dashboard/top-employees", department_id=resolved_id, limit=50, **common),
            self._get(
                f"/departments/{resolved_id}/summary",
                period="custom",
                start_date=bounds[0],
                end_date=bounds[1],
            ),
            self._get(
                f"/departments/{resolved_id}/ranking",
                period="custom",
                start_date=bounds[0],
                end_date=bounds[1],
            ),
            self._get(
                f"/departments/{resolved_id}/trends",
                start_date=bounds[0],
                end_date=bounds[1],
            ),
        )
        plan = await self.get_plan_fact_statistics(
            start_date=bounds[0], end_date=bounds[1], department_id=resolved_id
        )
        return await self.envelope(
            {
                "department": department_row,
                "summary": summary,
                "daily_trend": trend,
                "employee_ranking": employees,
                "department_summary": department_summary,
                "complete_employee_ranking": department_ranking,
                "department_and_employee_trends": department_trends,
                "plan_fact": plan["data"],
                "plan_fact_status": plan["status"],
            },
            status="partial" if plan["status"] == "partial" else "ok",
            scope=self.department_scope(department_row),
            period=bounds,
        )

    async def compare_departments(
        self,
        department_ids: Any = None,
        department_refs: Any = None,
        period: str = "month",
        start_date: str | None = None,
        end_date: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        bounds = _period_bounds(period, start_date, end_date)
        visible_rows = await self.departments()
        visible = {str(row["id"]): row for row in visible_rows}
        requested = [(value, None) for value in department_ids or []] + [
            (None, value) for value in department_refs or []
        ]
        effective_rows: list[dict[str, Any]] = []
        omitted = 0
        for requested_id, requested_ref in requested:
            row, _ = await self.resolve_department(department_id=requested_id, department_ref=requested_ref)
            if not row:
                omitted += 1
            elif str(row["id"]) not in {str(item["id"]) for item in effective_rows}:
                effective_rows.append(row)
        effective = [str(row["id"]) for row in effective_rows] if requested else sorted(visible)
        if not effective:
            return await self.envelope(
                {"reason": "departments_not_in_access_scope"} if requested else [],
                status="not_available" if requested else "no_data",
                period=bounds,
                omitted=omitted,
            )
        common = _period_query(period, start_date, end_date)
        stats, trends = await asyncio.gather(
            self._get("/dashboard/by-department", **common),
            self._get("/dashboard/departments-trend", **common),
        )
        data = {
            "statistics": [
                row for row in stats if str(row.get("department_id") or row.get("id")) in effective
            ],
            "trends": [row for row in trends if str(row.get("department_id") or row.get("id")) in effective],
        }
        return await self.envelope(
            data,
            scope={
                "department_ids": effective,
                "departments": [self.department_scope(visible[value]) for value in effective],
            },
            period=bounds,
            omitted=omitted,
        )

    async def list_employees(
        self,
        department_id: Any = None,
        department_ref: Any = None,
        search: str | None = None,
        include_inactive: bool = False,
        page: int = 1,
        page_size: int = 50,
        **_: Any,
    ) -> dict[str, Any]:
        department_row, selector_supplied = await self.resolve_department(
            department_id=department_id, department_ref=department_ref
        )
        if selector_supplied and not department_row:
            return await self.unavailable_department()
        department = str(department_row["id"]) if department_row else None
        rows, complete, source_total = await self.employees(
            department_id=department, search=search, include_inactive=include_inactive
        )
        start = (page - 1) * page_size
        data = {
            "items": rows[start : start + page_size],
            "total": len(rows),
            "returned_population": len(rows),
            "source_total": source_total,
            "source_complete": complete,
            "page": page,
            "page_size": page_size,
            "pages": math.ceil(len(rows) / page_size) if rows else 0,
        }
        return await self.envelope(
            data,
            status="partial" if rows and not complete else ("ok" if rows else "no_data"),
            scope=self.department_scope(department_row),
        )

    async def _page_data(self, employee_id: str, start: date, end: date) -> dict[str, Any]:
        return await self._get(f"/employees/{employee_id}/page-data", start_date=start, end_date=end)

    async def get_employee_card(
        self,
        employee_id: str,
        department_id: Any = None,
        department_ref: Any = None,
        period: str = "month",
        start_date: str | None = None,
        end_date: str | None = None,
        task_page_size: int = 100,
        **_: Any,
    ) -> dict[str, Any]:
        department_row, selector_supplied = await self.resolve_department(
            department_id=department_id, department_ref=department_ref
        )
        if selector_supplied and not department_row:
            return await self.unavailable_department()
        employee = await self.scoped_employee(employee_id, department_row)
        if not employee:
            return await self.envelope({}, status="not_available")
        if department_row is None:
            department_row = next(
                (
                    row
                    for row in await self.departments()
                    if str(row.get("id")) == str(employee.get("department_id"))
                ),
                None,
            )
            if department_row is None:
                return await self.envelope({}, status="not_available")
        bounds = _period_bounds(period, start_date, end_date)
        common = _period_query(period, start_date, end_date)
        page_data, summary, plan, crm = await asyncio.gather(
            self._page_data(employee_id, *bounds),
            self._get(f"/employees/{employee_id}/summary", **common),
            self._get(f"/plans/employee/{employee_id}/total", start_date=bounds[0], end_date=bounds[1]),
            self._get(f"/employees/{employee_id}/bitrix-metrics"),
        )
        active = [self._safe_task(row) for row in page_data.get("mentoring_tasks") or []]
        completed = [self._safe_task(row) for row in page_data.get("completed_tasks") or []]
        data = {
            "employee": employee,
            "kpi": page_data.get("kpi") or {},
            "period_summary": summary,
            "plan_fact": plan,
            "ai_insights": {
                "strengths": page_data.get("aggregated_strengths") or [],
                "growth_areas": page_data.get("aggregated_improvements") or [],
                "weekly_focus": page_data.get("weekly_focus") or [],
                "saved_focus": page_data.get("focus_text"),
                "week_start": page_data.get("week_start"),
                "week_end": page_data.get("week_end"),
            },
            "active_mentoring_tasks": active[:task_page_size],
            "completed_mentoring_tasks": completed[:task_page_size],
            "mentoring_history_window": {"active_limit": 5, "completed_limit": 10, "complete": False},
            "crm": self._safe_crm(crm),
        }
        return await self.envelope(
            data,
            status="partial",
            scope={**self.department_scope(department_row), "employee_id": employee_id},
            period=bounds,
        )

    async def compare_employees(
        self,
        employee_ids: list[Any],
        department_id: Any = None,
        department_ref: Any = None,
        period: str = "month",
        start_date: str | None = None,
        end_date: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        bounds = _period_bounds(period, start_date, end_date)
        department_row, selector_supplied = await self.resolve_department(
            department_id=department_id, department_ref=department_ref
        )
        if selector_supplied and not department_row:
            return await self.unavailable_department()
        requested = [str(value) for value in employee_ids]
        await self.departments()

        async def resolve(employee_id: str) -> dict[str, Any] | None:
            return await self._bounded(lambda: self.scoped_employee(employee_id, department_row))

        unique_requested = list(dict.fromkeys(requested))
        resolved = await asyncio.gather(*(resolve(value) for value in unique_requested))
        index = {row["id"]: row for row in resolved if row}
        effective, omitted = _select_visible(requested, set(index))

        async def load(employee_id: str) -> dict[str, Any]:
            page = await self._bounded(lambda: self._page_data(employee_id, *bounds))
            return {
                "employee": index[employee_id],
                "kpi": page.get("kpi") or {},
                "strengths": page.get("aggregated_strengths") or [],
                "growth_areas": page.get("aggregated_improvements") or [],
                "weekly_focus": page.get("weekly_focus") or [],
                "saved_focus": page.get("focus_text"),
                "active_tasks": len(page.get("mentoring_tasks") or []),
                "completed_tasks_recent": len(page.get("completed_tasks") or []),
            }

        data = await asyncio.gather(*(load(value) for value in effective))
        if requested and not effective:
            return await self.envelope(
                {"reason": "employees_not_in_effective_scope"},
                status="not_available",
                scope=self.department_scope(department_row),
                period=bounds,
                omitted=omitted,
            )
        return await self.envelope(
            data,
            status="ok" if data else "no_data",
            scope={**self.department_scope(department_row), "employee_ids": effective},
            period=bounds,
            omitted=omitted,
        )

    async def get_call_statistics(
        self,
        period: str = "month",
        department_id: Any = None,
        department_ref: Any = None,
        employee_id: Any = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        bounds = _period_bounds(period, start_date, end_date)
        department_row, employee_row, invalid = await self.resolve_entity_scope(
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
        )
        if invalid:
            return (
                await self.unavailable_department()
                if not employee_id
                else await self.envelope(
                    {"reason": "employee_not_in_effective_department"}, status="not_available"
                )
            )
        department = str(department_row["id"]) if department_row else None
        employee = str(employee_row["id"]) if employee_row else None
        summary, trend = await asyncio.gather(
            self._get(
                "/calls/stats/summary",
                department_id=department,
                employee_id=employee,
                date_from=bounds[0],
                date_to=bounds[1],
            ),
            self._get(
                "/dashboard/calls-trend",
                department_id=department,
                employee_id=employee,
                period="custom",
                start_date=bounds[0],
                end_date=bounds[1],
            ),
        )
        return await self.envelope(
            {"summary": summary, "daily_trend": trend},
            scope={**self.department_scope(department_row), "employee_id": employee},
            period=bounds,
        )

    async def get_plan_fact_statistics(
        self,
        start_date: Any,
        end_date: Any,
        department_id: Any = None,
        department_ref: Any = None,
        employee_id: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        start, end = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
        if end < start:
            raise ValueError("end_date must be on or after start_date")
        department_row, employee_row, invalid = await self.resolve_entity_scope(
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
        )
        if invalid:
            return await self.envelope({"reason": "requested_scope_not_available"}, status="not_available")
        employee = str(employee_row["id"]) if employee_row else None
        department = str(department_row["id"]) if department_row else None
        if employee_row:
            visible_employees, complete = [employee_row], True
        else:
            visible_employees, complete, _ = await self.employees(
                department_id=department, include_inactive=False
            )
        department_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in visible_employees:
            if row.get("department_id"):
                department_groups[row["department_id"]].append(row)
        summaries: list[dict[str, Any]] = []
        for department_key in department_groups:
            rows = await self._get(
                "/plans/summary", start_date=start, end_date=end, department_id=department_key
            )
            allowed = {item["id"] for item in department_groups[department_key]}
            summaries.extend(row for row in rows if str(row.get("employee_id")) in allowed)
        if employee:
            summaries = [row for row in summaries if str(row.get("employee_id")) == employee]
        totals: dict[str, float] = defaultdict(float)
        for row in summaries:
            for key in (
                "plan_total",
                "plan_outbound",
                "plan_inbound",
                "plan_outbound_new",
                "plan_outbound_regular",
            ):
                totals[key] += _number(row.get(key))
        return await self.envelope(
            {"totals": dict(totals), "employees": summaries},
            status="partial" if summaries and not complete else ("ok" if summaries else "no_data"),
            scope={**self.department_scope(department_row), "employee_id": employee},
            period=(start, end),
        )

    async def get_client_statistics(
        self,
        period: str = "month",
        department_id: Any = None,
        department_ref: Any = None,
        employee_id: Any = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        bounds = _period_bounds(period, start_date, end_date)
        department_row, employee_row, invalid = await self.resolve_entity_scope(
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
        )
        if invalid:
            return await self.envelope({"reason": "requested_scope_not_available"}, status="not_available")
        department = str(department_row["id"]) if department_row else None
        employee = str(employee_row["id"]) if employee_row else None
        if employee:
            source = await self._get(
                f"/employees/{employee}/summary",
                period="custom",
                start_date=bounds[0],
                end_date=bounds[1],
            )
        else:
            source = await self._get(
                "/dashboard/summary",
                department_id=department,
                period="custom",
                start_date=bounds[0],
                end_date=bounds[1],
            )
        keys = [
            key
            for key in source
            if any(
                marker in key.lower()
                for marker in (
                    "client",
                    "new_",
                    "regular",
                    "missed",
                    "no_answer",
                    "outbound",
                    "inbound",
                    "contact",
                    "repeat",
                )
            )
        ]
        return await self.envelope(
            {key: source[key] for key in keys},
            scope={**self.department_scope(department_row), "employee_id": employee},
            period=bounds,
        )

    async def get_crm_statistics(
        self,
        department_id: Any = None,
        department_ref: Any = None,
        employee_id: Any = None,
        snapshot_date: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        department_row, employee_row, invalid = await self.resolve_entity_scope(
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
        )
        if invalid:
            return await self.envelope({"reason": "requested_scope_not_available"}, status="not_available")
        department = str(department_row["id"]) if department_row else None
        employee = str(employee_row["id"]) if employee_row else None
        if employee_row:
            employees, complete, source_total = [employee_row], True, 1
        else:
            employees, complete, source_total = await self.employees(
                department_id=department, include_inactive=False
            )

        async def load(row: dict[str, Any]) -> dict[str, Any]:
            value = await self._bounded(lambda: self._get(f"/employees/{row['id']}/bitrix-metrics"))
            return {"employee": row, "metrics": self._safe_crm(value)}

        rows = await asyncio.gather(*(load(row) for row in employees))
        requested_snapshot = str(snapshot_date) if snapshot_date else None
        available_snapshot_dates = sorted(
            {str(row["metrics"].get("snapshot_date")) for row in rows if row["metrics"].get("snapshot_date")}
        )
        if requested_snapshot:
            rows = [
                row for row in rows if str(row["metrics"].get("snapshot_date") or "") == requested_snapshot
            ]
            if not rows:
                return await self.envelope(
                    {
                        "reason": "historical_crm_snapshot_not_exposed_by_upstream_api",
                        "requested_snapshot_date": requested_snapshot,
                        "available_latest_snapshot_dates": available_snapshot_dates,
                        "source_mode": "latest_snapshot_per_employee_only",
                    },
                    status="not_available",
                    scope={**self.department_scope(department_row), "employee_id": employee},
                )
        totals: dict[str, float] = defaultdict(float)
        stages: Counter[str] = Counter()
        funnels: Counter[str] = Counter()
        available = 0
        for row in rows:
            metrics = row["metrics"]
            if metrics.get("status") == "ok":
                available += 1
            for key in (
                "total_open_deals",
                "deals_without_tasks",
                "deals_with_overdue_tasks",
                "b2b_responsible_assigned",
                "distribution_in_work_qualification",
                "networks_unprocessed",
                "networks_open_total",
            ):
                totals[key] += _number(metrics.get(key))
            stages.update({str(k): int(v) for k, v in (metrics.get("stage_counts") or {}).items()})
            funnels.update({str(k): int(v) for k, v in (metrics.get("funnel_counts") or {}).items()})
        data = {
            "requested_snapshot_date": requested_snapshot,
            "available_latest_snapshot_dates": available_snapshot_dates,
            "source_mode": "latest_snapshot_per_employee_only",
            "coverage": {
                "source_total": source_total,
                "source_complete": complete,
                "employees": len(rows),
                "available": available,
                "unavailable": len(rows) - available,
            },
            "totals": dict(totals),
            "stage_counts": dict(stages),
            "funnel_counts": dict(funnels),
            "employees": rows,
        }
        return await self.envelope(
            data,
            status="partial"
            if rows and (not complete or bool(requested_snapshot) or available < len(rows))
            else ("ok" if rows else "no_data"),
            scope={**self.department_scope(department_row), "employee_id": employee},
        )

    async def get_growth_insights(
        self,
        period: str = "month",
        department_id: Any = None,
        department_ref: Any = None,
        employee_id: Any = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 20,
        **_: Any,
    ) -> dict[str, Any]:
        bounds = _period_bounds(period, start_date, end_date)
        department_row, employee_row, invalid = await self.resolve_entity_scope(
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
        )
        if invalid:
            return await self.envelope({"reason": "requested_scope_not_available"}, status="not_available")
        department = str(department_row["id"]) if department_row else None
        employee = str(employee_row["id"]) if employee_row else None
        if employee_row:
            employees, complete = [employee_row], True
        else:
            employees, complete, _ = await self.employees(department_id=department, include_inactive=False)

        async def load(row: dict[str, Any]) -> dict[str, Any]:
            page = await self._bounded(lambda: self._page_data(row["id"], *bounds))
            return {
                "employee": row,
                "strengths": page.get("aggregated_strengths") or [],
                "growth_areas": page.get("aggregated_improvements") or [],
                "weekly_focus": page.get("weekly_focus") or [],
                "saved_focus": page.get("focus_text"),
            }

        rows = await asyncio.gather(*(load(row) for row in employees))

        def employee_mentions(field: str) -> list[dict[str, Any]]:
            labels: dict[str, str] = {}
            mentions: dict[str, set[str]] = defaultdict(set)
            for row in rows:
                employee_key = str(row["employee"]["id"])
                for value in row[field]:
                    text = _insight_text(value)
                    if not text:
                        continue
                    key = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
                    labels.setdefault(key, text)
                    mentions[key].add(employee_key)
            ranked = sorted(
                mentions,
                key=lambda key: (-len(mentions[key]), labels[key].casefold()),
            )
            return [{"text": labels[key], "employee_mentions": len(mentions[key])} for key in ranked[:limit]]

        data = {
            "strengths": employee_mentions("strengths"),
            "growth_areas": employee_mentions("growth_areas"),
            "employees": rows,
        }
        return await self.envelope(
            data,
            status="partial" if not complete else ("ok" if rows else "no_data"),
            scope={**self.department_scope(department_row), "employee_id": employee},
            period=bounds,
        )

    async def get_mentoring_statistics(
        self,
        period: str = "month",
        department_id: Any = None,
        department_ref: Any = None,
        employee_id: Any = None,
        task_status: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        page: int = 1,
        page_size: int = 50,
        **_: Any,
    ) -> dict[str, Any]:
        bounds = _period_bounds(period, start_date, end_date)
        department_row, employee_row, invalid = await self.resolve_entity_scope(
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
        )
        if invalid:
            return await self.envelope({"reason": "requested_scope_not_available"}, status="not_available")
        department = str(department_row["id"]) if department_row else None
        employee = str(employee_row["id"]) if employee_row else None
        if employee_row:
            employees, complete = [employee_row], True
        else:
            employees, complete, _ = await self.employees(department_id=department, include_inactive=True)

        async def load(row: dict[str, Any]) -> list[dict[str, Any]]:
            data = await self._bounded(lambda: self._page_data(row["id"], *bounds))
            tasks = (data.get("mentoring_tasks") or []) + (data.get("completed_tasks") or [])
            return [{"employee": row, **self._safe_task(task)} for task in tasks]

        nested = await asyncio.gather(*(load(row) for row in employees))
        source_tasks = [task for group in nested for task in group]
        tasks: list[dict[str, Any]] = []
        unfilterable_timestamps = 0
        for task in source_tasks:
            timestamp = (
                task.get("completed_at") if task.get("status") == "completed" else task.get("created_at")
            )
            parsed = _aware_datetime(timestamp)
            if not parsed:
                unfilterable_timestamps += 1
                continue
            if bounds[0] <= parsed.date() <= bounds[1]:
                tasks.append(task)
        if task_status:
            tasks = [task for task in tasks if task.get("status") == task_status]
        statuses = Counter(str(task.get("status") or "unknown") for task in tasks)
        now = datetime.now(MSK)
        overdue = 0
        durations: list[float] = []
        for task in tasks:
            due = task.get("due_date")
            if due and task.get("status") != "completed":
                parsed_due = _aware_datetime(due)
                if parsed_due and parsed_due < now:
                    overdue += 1
            if task.get("created_at") and task.get("completed_at"):
                created = _aware_datetime(task["created_at"])
                completed = _aware_datetime(task["completed_at"])
                if created and completed:
                    durations.append((completed - created).total_seconds() / 86400)
        start = (page - 1) * page_size
        data = {
            "summary": {
                "status_counts": dict(statuses),
                "overdue": overdue,
                "completion_rate": round(statuses.get("completed", 0) / len(tasks) * 100, 1)
                if tasks
                else None,
                "avg_completion_days": round(sum(durations) / len(durations), 1) if durations else None,
            },
            "items": tasks[start : start + page_size],
            "total_in_period": len(tasks),
            "source_tasks_in_bounded_api_window": len(source_tasks),
            "page": page,
            "page_size": page_size,
            "pages": math.ceil(len(tasks) / page_size) if tasks else 0,
            "task_period_basis": "completed_at for completed tasks; created_at otherwise",
            "source_tasks_without_filterable_timestamp": unfilterable_timestamps,
            "history_window_per_employee": {"active_limit": 5, "completed_limit": 10, "complete": False},
        }
        return await self.envelope(
            data,
            status="partial" if employees else "no_data",
            scope={
                **self.department_scope(department_row),
                "employee_id": employee,
                "employee_population_complete": complete,
            },
            period=bounds,
        )

    async def list_scenarios(
        self,
        department_id: Any = None,
        department_ref: Any = None,
        search: str | None = None,
        include_historical: bool = False,
        page: int = 1,
        page_size: int = 50,
        **_: Any,
    ) -> dict[str, Any]:
        department_row, selector_supplied = await self.resolve_department(
            department_id=department_id, department_ref=department_ref
        )
        if selector_supplied and not department_row:
            return await self.unavailable_department()
        department = str(department_row["id"]) if department_row else None
        context = await self.context()
        historical_applied = include_historical and context.is_admin
        rows, _ = await self.scenarios(department, include_inactive=historical_applied)
        safe = [self._safe_scenario(row) for row in rows]
        if search:
            needle = search.casefold()
            safe = [row for row in safe if needle in str(row.get("name") or "").casefold()]
        start = (page - 1) * page_size
        data = {
            "items": safe[start : start + page_size],
            "total": len(safe),
            "page": page,
            "page_size": page_size,
            "pages": math.ceil(len(safe) / page_size) if safe else 0,
            "include_historical_requested": include_historical,
            "include_historical_applied": historical_applied,
        }
        return await self.envelope(
            data,
            status="ok" if safe else "no_data",
            scope=self.department_scope(department_row),
            omitted=int(include_historical and not historical_applied),
        )

    async def get_scenario_criteria(
        self,
        scenario_id: str,
        department_id: Any = None,
        department_ref: Any = None,
        **_: Any,
    ) -> dict[str, Any]:
        department_row, selector_supplied = await self.resolve_department(
            department_id=department_id, department_ref=department_ref
        )
        if selector_supplied and not department_row:
            return await self.unavailable_department()
        department = str(department_row["id"]) if department_row else None
        rows, _ = await self.scenarios(department, include_inactive=True)
        row = next((item for item in rows if str(item.get("id")) == scenario_id), None)
        if not row:
            return await self.envelope({}, status="not_available")
        if department_row is None:
            scenario_department_id = str(row.get("department_id") or "")
            department_row = next(
                (
                    visible
                    for visible in await self.departments()
                    if str(visible.get("id")) == scenario_department_id
                ),
                None,
            )
            if department_row is None:
                return await self.envelope({}, status="not_available")
        return await self.envelope(
            {"scenario": self._safe_scenario(row), "criteria": self._criteria(row)},
            scope={**self.department_scope(department_row), "scenario_id": scenario_id},
        )

    async def get_scenario_performance(
        self,
        period: str = "month",
        scenario_ids: Any = None,
        department_id: Any = None,
        department_ref: Any = None,
        employee_id: Any = None,
        start_date: str | None = None,
        end_date: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        bounds = _period_bounds(period, start_date, end_date)
        department_row, employee_row, invalid = await self.resolve_entity_scope(
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
        )
        if invalid:
            return await self.envelope({"reason": "requested_scope_not_available"}, status="not_available")
        department = str(department_row["id"]) if department_row else None
        employee = str(employee_row["id"]) if employee_row else None
        requested = [str(value) for value in scenario_ids or []]
        catalog, _ = await self.scenarios(department, include_inactive=bool(requested))
        index = {str(row.get("id")): row for row in catalog}
        effective, omitted = _select_visible(requested, set(index)) if requested else (list(index), 0)
        if requested and not effective:
            return await self.envelope(
                {"reason": "scenarios_not_in_effective_scope"},
                status="not_available",
                scope={**self.department_scope(department_row), "employee_id": employee},
                period=bounds,
                omitted=omitted,
            )
        effective_department_ids = {str(index[value].get("department_id") or "") for value in effective} - {
            ""
        }
        if department_row is None and len(effective_department_ids) == 1:
            effective_department_id = next(iter(effective_department_ids))
            department_row = next(
                (
                    visible
                    for visible in await self.departments()
                    if str(visible.get("id")) == effective_department_id
                ),
                None,
            )
            if department_row is not None:
                department = str(department_row["id"])
        calls, total, complete = await self.calls(
            start=bounds[0],
            end=bounds[1],
            department_id=department,
            employee_id=employee,
        )
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for call in calls:
            scenario_id = str(call.get("scenario_id") or "")
            if scenario_id in effective and call.get("quality_score") is not None:
                groups[scenario_id].append(call)
        data = []
        for scenario_id in effective:
            rows = groups.get(scenario_id, [])
            scores = [_number(row.get("quality_score")) for row in rows]
            passing = _number(index[scenario_id].get("passing_score"))
            data.append(
                {
                    "scenario": self._safe_scenario(index[scenario_id]),
                    "evaluations": len(scores),
                    "employees": len({str(row.get("employee_id")) for row in rows}),
                    "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
                    "min_score": min(scores) if scores else None,
                    "max_score": max(scores) if scores else None,
                    "pass_rate": round(sum(score >= passing for score in scores) / len(scores) * 100, 1)
                    if scores
                    else None,
                }
            )
        return await self.envelope(
            data,
            status="partial" if not complete else ("ok" if data else "no_data"),
            scope={
                "scenario_ids": effective,
                **self.department_scope(department_row),
                "employee_id": employee,
                "source_calls": len(calls),
                "source_calls_total": total,
            },
            period=bounds,
            omitted=omitted,
        )

    async def get_criterion_performance(
        self,
        period: str = "month",
        scenario_id: Any = None,
        criterion_ids: Any = None,
        department_id: Any = None,
        department_ref: Any = None,
        employee_id: Any = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 100,
        **_: Any,
    ) -> dict[str, Any]:
        bounds = _period_bounds(period, start_date, end_date)
        department_row, employee_row, invalid = await self.resolve_entity_scope(
            department_id=department_id,
            department_ref=department_ref,
            employee_id=employee_id,
        )
        if invalid:
            return await self.envelope({"reason": "requested_scope_not_available"}, status="not_available")
        department = str(department_row["id"]) if department_row else None
        employee = str(employee_row["id"]) if employee_row else None
        scenario = str(scenario_id) if scenario_id else None
        catalog, _ = await self.scenarios(department, include_inactive=bool(scenario))
        scenario_row = next(
            (row for row in catalog if str(row.get("id")) == scenario),
            None,
        )
        if scenario and scenario_row is None:
            return await self.envelope(
                {"reason": "scenario_not_in_effective_scope"},
                status="not_available",
                scope={**self.department_scope(department_row), "employee_id": employee},
                period=bounds,
            )
        if scenario_row is not None and department_row is None:
            scenario_department_id = str(scenario_row.get("department_id") or "")
            department_row = next(
                (
                    visible
                    for visible in await self.departments()
                    if str(visible.get("id")) == scenario_department_id
                ),
                None,
            )
            if department_row is None:
                return await self.envelope(
                    {"reason": "scenario_not_in_effective_scope"},
                    status="not_available",
                    period=bounds,
                )
            department = str(department_row["id"])
        visible_criteria = {
            str(item["id"]): item
            for row in catalog
            if not scenario or str(row.get("id")) == scenario
            for item in self._criteria(row)
        }
        requested = [str(value) for value in criterion_ids or []]
        effective, omitted = (
            _select_visible(requested, set(visible_criteria)) if requested else (list(visible_criteria), 0)
        )
        if requested and not effective:
            return await self.envelope(
                {"reason": "criteria_not_in_effective_scope"},
                status="not_available",
                scope={
                    **self.department_scope(department_row),
                    "scenario_id": scenario,
                    "employee_id": employee,
                },
                period=bounds,
                omitted=omitted,
            )
        calls, total, complete = await self.calls(
            start=bounds[0],
            end=bounds[1],
            department_id=department,
            employee_id=employee,
            scenario_id=scenario,
        )

        async def detail(call: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
            if call.get("quality_score") is None:
                return call, None
            value = await self._bounded(lambda: self._get(f"/calls/{call['id']}"))
            return call, value

        detailed = await asyncio.gather(*(detail(call) for call in calls))
        aggregate: dict[str, dict[str, Any]] = {}
        for call, detail_row in detailed:
            evaluation = (detail_row or {}).get("evaluation") or {}
            for item in evaluation.get("items") or []:
                criterion_id = str(item.get("checklist_item_id") or "")
                if criterion_id not in effective:
                    continue
                row = aggregate.setdefault(
                    criterion_id,
                    {
                        "criterion": visible_criteria[criterion_id],
                        "observations": 0,
                        "score_sum": 0.0,
                        "max_score_sum": 0.0,
                        "penalties": 0,
                        "employees": set(),
                        "scenarios": set(),
                        "last_observation": None,
                    },
                )
                row["observations"] += 1
                row["score_sum"] += _number(item.get("score"))
                row["max_score_sum"] += _number(item.get("max_score"))
                row["penalties"] += int(bool(item.get("is_penalty")))
                row["employees"].add(str(call.get("employee_id")))
                row["scenarios"].add(str(call.get("scenario_id")))
                started = call.get("started_at")
                if started and (row["last_observation"] is None or str(started) > row["last_observation"]):
                    row["last_observation"] = str(started)
        data = []
        for row in aggregate.values():
            maximum = row.pop("max_score_sum")
            score = row.pop("score_sum")
            row["employees"] = len(row["employees"])
            row["scenarios"] = len(row["scenarios"])
            row["avg_score_percent"] = round(score / maximum * 100, 2) if maximum else None
            data.append(row)
        data.sort(key=lambda row: (row["avg_score_percent"] is None, row["avg_score_percent"] or 0))
        matching_criteria_total = len(data)
        truncated = not complete or total > len(calls) or matching_criteria_total > limit
        return await self.envelope(
            data[:limit],
            status="partial" if truncated else ("ok" if data else "no_data"),
            scope={
                "scenario_id": scenario,
                "criterion_ids": effective,
                **self.department_scope(department_row),
                "employee_id": employee,
                "source_calls": len(calls),
                "source_calls_total": total,
                "matching_criteria_total": matching_criteria_total,
                "returned_criteria": min(matching_criteria_total, limit),
                "criteria_limit": limit,
            },
            period=bounds,
            omitted=omitted,
        )


class BackendClient:
    """Compatibility facade consumed by the typed MCP server."""

    def __init__(self, settings: Settings, platform: OKKPlatformClient):
        self.settings = settings
        self.platform = platform
        if settings.analytics_trace_enabled:
            LOGGER.setLevel(logging.INFO)

    async def close(self) -> None:
        # The shared platform client is closed by the ASGI lifespan.
        return None

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        access_token = get_access_token()
        if not access_token or not access_token.subject:
            raise PermissionError("Authenticated OKK account is required")
        claims = access_token.claims or {}
        upstream_access = claims.get("_upstream_access_token")
        validated_context = None
        if upstream_access:
            validated_context = AccountContext(
                session_id=UUID(access_token.subject),
                user_id=str(claims.get("okk_user_id") or ""),
                role=str(claims.get("role") or ""),
                department_ids=tuple(str(value) for value in claims.get("department_ids") or []),
                access_token=str(upstream_access),
            )
        adapter = AnalyticsAdapter(
            self.platform,
            access_token.subject,
            self.settings,
            validated_context=validated_context,
        )
        request_id = str(uuid4())
        started = time.perf_counter()
        try:
            result = await adapter.dispatch(path, params)
            result["request_id"] = request_id
            self._trace(
                request_id=request_id,
                subject=access_token.subject,
                path=path,
                params=params,
                result=result,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return result
        except OKKNotAvailable:
            result = await adapter.envelope({}, status="not_available")
            result["request_id"] = request_id
            self._trace(
                request_id=request_id,
                subject=access_token.subject,
                path=path,
                params=params,
                result=result,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            return result
        except OKKAuthenticationError as exc:
            self._trace(
                request_id=request_id,
                subject=access_token.subject,
                path=path,
                params=params,
                result={"status": "not_available", "data": {}, "effective_scope": {}},
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise PermissionError("OKK account session is no longer valid") from exc
        except OKKUnavailable as exc:
            self._trace(
                request_id=request_id,
                subject=access_token.subject,
                path=path,
                params=params,
                result={
                    "status": "temporarily_unavailable",
                    "data": {},
                    "effective_scope": {},
                },
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise BackendUnavailable("OKK analytics is temporarily unavailable") from exc

    def _trace(
        self,
        *,
        request_id: str,
        subject: str,
        path: str,
        params: dict[str, Any],
        result: dict[str, Any],
        duration_ms: float,
    ) -> None:
        """Emit a redacted operational trace without raw business data or identifiers."""
        if not self.settings.analytics_trace_enabled:
            return
        data = result.get("data")
        if isinstance(data, list):
            result_items = len(data)
        elif isinstance(data, dict) and isinstance(data.get("items"), list):
            result_items = len(data["items"])
        else:
            result_items = None
        scope = result.get("effective_scope") or {}
        safe_inputs = {
            "period": params.get("period"),
            "custom_date_range": bool(params.get("start_date") or params.get("end_date")),
            "department_filter": bool(params.get("department_id") or params.get("department_ref")),
            "employee_filter": bool(params.get("employee_id") or params.get("employee_ids")),
            "scenario_filter_count": len(params.get("scenario_ids") or [])
            + int(bool(params.get("scenario_id"))),
            "criterion_filter_count": len(params.get("criterion_ids") or []),
            "search_filter": bool(params.get("search")),
            "page": params.get("page"),
            "page_size": params.get("page_size"),
        }
        event = {
            "event": "okk_analytics_tool_call",
            "request_id": request_id,
            "actor_hash": hashlib.sha256(subject.encode("utf-8")).hexdigest()[:16],
            "tool_path": UUID_IN_PATH.sub("/{entity_id}", path),
            "inputs": {key: value for key, value in safe_inputs.items() if value is not None},
            "duration_ms": round(duration_ms, 1),
            "result": {
                "status": result.get("status"),
                "omitted_filters_count": result.get("omitted_filters_count", 0),
                "department_code": scope.get("department_code"),
                "source_complete": (data.get("source_complete") if isinstance(data, dict) else None),
                "returned_items": result_items,
            },
        }
        LOGGER.info(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
