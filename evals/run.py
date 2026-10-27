#!/usr/bin/env python3
"""Evaluation suite for the OKK Analytics MCP gateway.

The runner asks a real agent (Codex or Claude Code) the questions from evals/cases.json, records the
tool calls and the final answer, and grades the answer against values read from OKK at run time with
the same account. Nothing in cases.json holds real data: placeholders ({department_name}, {call_id},
...) and expected values are resolved from OKK reads.

Dry run (free, no model):      python evals/run.py --dry-run
Truth check (free, live OKK):  python evals/run.py --dry-run --truth-from gateway --gateway-url URL
Paid run (needs approval):     python evals/run.py --client codex --gateway-url URL --confirm-paid-run

See evals/README.md for setup, cost and the release rule.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parent
CASES_PATH = EVALS_DIR / "cases.json"
SYNTHETIC_FIXTURE = EVALS_DIR / "fixtures" / "synthetic.json"
OUT_DIR = EVALS_DIR / "out"
SERVER_NAME = "okk-analytics"  # the name the plugin gives the MCP server (plugins/okk-analytics/.mcp.json)
MSK = timezone(timedelta(hours=3))
TOOL_MODES = {"any", "all", "ordered", "none", "free"}
EXPECT_KINDS = {"number", "names", "names_exact", "text", "date", "duration"}
PRECONDITION_OPS = {"eq", "ne", "ge", "le"}
TOKEN_REFRESH_AFTER_SECONDS = 40 * 60
RUN_TIMEOUT_SECONDS = 900

MONTHS_NOM = "январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь".split()
MONTHS_PREP = "январе феврале марте апреле мае июне июле августе сентябре октябре ноябре декабре".split()
MONTHS_GEN = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()


class Precondition(Exception):
    """The live data does not fit the case (for example, no second department): the case is skipped."""


class TruthError(RuntimeError):
    """An OKK read for placeholders or expected values failed."""


# ---------------------------------------------------------------------------------------------------
# Cases: loading and validation


def load_cases(path: Path = CASES_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
PHONE_TEXT = re.compile(r"\+?\d[\d ()-]{8,}\d")


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [text for value in node.values() for text in _strings(value)]
    if isinstance(node, list):
        return [text for value in node for text in _strings(value)]
    return []


def _expressions(case: dict[str, Any]) -> list[Any]:
    truth = case.get("truth") or {}
    found: list[Any] = []
    for item in truth.get("expect") or []:
        found += [item[key] for key in ("value", "universe") if key in item]
        found += list(item.get("any_of") or [])
    found += [item.get("value") for item in truth.get("preconditions") or []]
    return found


def _sources(expression: Any) -> list[str]:
    if isinstance(expression, dict):
        return [name for part in expression.get("sub") or [] for name in _sources(part)]
    if isinstance(expression, str) and ":" in expression:
        return [expression.split(":", 1)[0]]
    return ["<missing source>"]


def validate_cases(suite: dict[str, Any], known_tools: set[str] | None = None) -> list[str]:
    """Every problem in cases.json as text; empty when the suite is usable."""

    errors: list[str] = []
    documented = set(suite.get("placeholders") or {})
    resolvable = set(RESOLVERS) | set(calendar_values(date(2026, 1, 20)))
    for name in sorted(documented - resolvable):
        errors.append(f"placeholder {name} is documented but nothing resolves it")
    raw = json.dumps(suite.get("cases"), ensure_ascii=False)
    if UUID_TEXT.search(raw) or PHONE_TEXT.search(raw):
        errors.append("cases.json holds an id or a phone number: the repo is public, use placeholders")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        return [*errors, "cases must be a non-empty list"]
    seen: set[str] = set()
    for index, case in enumerate(cases):
        where = f"case {case.get('id') or index}"
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(case.get("id") or "")):
            errors.append(f"{where}: id must be snake_case")
        if case.get("id") in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(case.get("id"))
        if not isinstance(case.get("held_out"), bool):
            errors.append(f"{where}: held_out must be true or false")
        if not re.search("[а-яё]", str(case.get("prompt") or ""), re.IGNORECASE):
            errors.append(f"{where}: prompt must be a Russian question")
        tools = case.get("tools") or {}
        if tools.get("mode") not in TOOL_MODES or not isinstance(tools.get("names"), list):
            errors.append(f"{where}: tools needs mode in {sorted(TOOL_MODES)} and a names list")
        elif tools["mode"] in {"any", "all", "ordered"} and not tools["names"]:
            errors.append(f"{where}: tools mode {tools['mode']} needs names")
        truth = case.get("truth") or {}
        reads = truth.get("reads") or {}
        for name, read in reads.items():
            if not isinstance(read, dict) or not read.get("tool") or not isinstance(read.get("args"), dict):
                errors.append(f"{where}: read {name} needs tool and args")
        for item in truth.get("expect") or []:
            if item.get("as") not in EXPECT_KINDS:
                errors.append(f"{where}: expect.as must be one of {sorted(EXPECT_KINDS)}")
            if ("value" in item) == ("any_of" in item):
                errors.append(f"{where}: expect needs exactly one of value or any_of")
            if item.get("as") == "names_exact" and "universe" not in item:
                errors.append(f"{where}: names_exact needs a universe")
        for item in truth.get("preconditions") or []:
            if item.get("op") not in PRECONDITION_OPS or "to" not in item or not item.get("reason"):
                errors.append(
                    f"{where}: precondition needs value, op in {sorted(PRECONDITION_OPS)}, to, reason"
                )
        for expression in _expressions(case):
            for source in _sources(expression):
                if source not in reads:
                    errors.append(f"{where}: expression uses unknown read {source!r}")
        answer = case.get("answer") or {}
        if not (truth.get("expect") or answer.get("any") or tools.get("mode") == "none"):
            errors.append(f"{where}: nothing to grade (no expect, answer.any or tools mode none)")
        for text in _strings({key: case.get(key) for key in ("prompt", "args_any", "truth", "answer")}):
            for name in PLACEHOLDER.findall(text):
                if name not in documented:
                    errors.append(f"{where}: placeholder {{{name}}} is not documented in placeholders")
        if known_tools is not None:
            used = set(tools.get("names") or []) | {read.get("tool") for read in reads.values()}
            for tool in sorted(used - known_tools):
                errors.append(f"{where}: tool {tool} does not exist on the server")
    held_out = sum(1 for case in cases if case.get("held_out") is True)
    if not 0.15 <= held_out / len(cases) <= 0.25:
        errors.append(f"held-out share must be about 20%, now {held_out}/{len(cases)}")
    return errors


# ---------------------------------------------------------------------------------------------------
# Truth: OKK reads for placeholders and expected values


def read_key(tool: str, args: dict[str, Any]) -> str:
    clean = {key: value for key, value in args.items() if value is not None}
    return json.dumps([tool, clean], ensure_ascii=False, sort_keys=True)


class FixtureTruth:
    """Recorded reads: the synthetic fixture in the repo or a recording made with --record-fixture."""

    def __init__(self, path: Path):
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.today = date.fromisoformat(payload["today"])
        self.reads = {read_key(row["tool"], row["args"]): row["result"] for row in payload["reads"]}
        self.path = path

    def read(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        key = read_key(tool, args)
        if key not in self.reads:
            raise TruthError(f"{self.path.name} has no recorded read {key}")
        return self.reads[key]


class TokenSource:
    """Bearer token for the gateway: OKK_EVAL_TOKEN, or a token file refreshed through /token."""

    def __init__(self, gateway_url: str, token_env: str, token_file: Path | None):
        self.issuer = gateway_url.rstrip("/").removesuffix("/mcp")
        self.token_env = token_env
        self.token_file = token_file
        self.lock = threading.Lock()

    def get(self) -> str:
        if self.token_file is None:
            token = os.environ.get(self.token_env)
            if not token:
                raise SystemExit(f"Set {self.token_env} or pass --token-file (see evals/README.md).")
            return token
        with self.lock:
            saved = json.loads(self.token_file.read_text(encoding="utf-8"))
            age = time.time() - saved.get("obtained_at", 0) / 1000
            if age > TOKEN_REFRESH_AFTER_SECONDS and saved.get("refresh_token"):
                saved = self._refresh(saved)
            return saved["access_token"]

    def _refresh(self, saved: dict[str, Any]) -> dict[str, Any]:
        import httpx

        response = httpx.post(
            f"{self.issuer}/token",
            data={
                "grant_type": "refresh_token",
                "client_id": saved["client_id"],
                "refresh_token": saved["refresh_token"],
            },
            timeout=30,
        )
        if response.status_code != 200:
            raise TruthError(f"token refresh failed with HTTP {response.status_code}")
        saved = {**saved, **response.json(), "obtained_at": int(time.time() * 1000)}
        self.token_file.write_text(json.dumps(saved), encoding="utf-8")
        return saved


class GatewayTruth:
    """Live reads through the gateway's MCP tools with the eval account: no model involved."""

    def __init__(self, gateway_url: str, tokens: TokenSource):
        self.url = gateway_url
        self.tokens = tokens

    def read(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._call(tool, {key: value for key, value in args.items() if value is not None}))

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self.tokens.get()}"}
        async with streamablehttp_client(self.url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
        text = "".join(getattr(block, "text", "") for block in result.content)
        if result.isError:
            raise TruthError(f"{tool} answered an error: {text[:300]}")
        return result.structuredContent if result.structuredContent is not None else json.loads(text)


class CachedTruth:
    """One read per tool and arguments; keeps what was read for --record-fixture."""

    def __init__(self, source: Any):
        self.source = source
        self.cache: dict[str, dict[str, Any]] = {}
        self.recorded: list[dict[str, Any]] = []

    def read(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        key = read_key(tool, args)
        if key not in self.cache:
            result = self.source.read(tool, args)
            self.cache[key] = result
            clean = {name: value for name, value in args.items() if value is not None}
            self.recorded.append({"tool": tool, "args": clean, "result": result})
        return self.cache[key]


# ---------------------------------------------------------------------------------------------------
# Placeholders


def calendar_values(today: date) -> dict[str, str]:
    first = today.replace(day=1)
    prev_end = first - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    range_first = first if today.day > 15 else prev_start
    week_start = today - timedelta(days=today.weekday())
    return {
        "month_nom": MONTHS_NOM[today.month - 1],
        "month_prep": MONTHS_PREP[today.month - 1],
        "month_year": str(today.year),
        "prev_month_prep": MONTHS_PREP[prev_start.month - 1],
        "prev_month_year": str(prev_start.year),
        "prev_month_start": prev_start.isoformat(),
        "prev_month_end": prev_end.isoformat(),
        "range_from_day": "1",
        "range_to_day": "15",
        "range_month_gen": MONTHS_GEN[range_first.month - 1],
        "range_year": str(range_first.year),
        "range_start": range_first.isoformat(),
        "range_end": range_first.replace(day=15).isoformat(),
        "week_start": week_start.isoformat(),
        "week_end": (week_start + timedelta(days=6)).isoformat(),
    }


@dataclass
class SuiteSettings:
    department: str = "csm"
    other_department: str | None = None
    invisible_department: str = "маркетплейсов"
    unknown_person: str = "Иванов Пётр"
    phrase: str = "перезвоню"


class Context:
    """Placeholder values for one run: calendar first, the rest resolved lazily from OKK reads."""

    def __init__(self, truth: CachedTruth, today: date, settings: SuiteSettings):
        self.truth = truth
        self.settings = settings
        self.values: dict[str, Any] = dict(calendar_values(today))

    def read(self, tool: str, **args: Any) -> dict[str, Any]:
        return self.truth.read(tool, {key: value for key, value in args.items() if value is not None})

    def get(self, name: str) -> Any:
        if name not in self.values:
            RESOLVERS[name](self)
        return self.values[name]

    def fill(self, node: Any) -> Any:
        if isinstance(node, str):
            whole = PLACEHOLDER.fullmatch(node)
            if whole:
                return self.get(whole.group(1))
            return PLACEHOLDER.sub(lambda match: str(self.get(match.group(1))), node)
        if isinstance(node, dict):
            return {key: self.fill(value) for key, value in node.items()}
        if isinstance(node, list):
            return [self.fill(value) for value in node]
        return node


RESOLVERS: dict[str, Callable[[Context], None]] = {}


def resolves(*names: str) -> Callable[[Callable[[Context], None]], Callable[[Context], None]]:
    def register(function: Callable[[Context], None]) -> Callable[[Context], None]:
        for name in names:
            RESOLVERS[name] = function
        return function

    return register


def _visible_departments(ctx: Context) -> list[dict[str, Any]]:
    return sorted(ctx.read("get_access_context")["data"]["departments"], key=lambda row: str(row.get("code")))


@resolves("department", "department_name")
def _main_department(ctx: Context) -> None:
    code = ctx.settings.department
    row = next((row for row in _visible_departments(ctx) if row.get("code") == code), None)
    if row is None:
        raise Precondition(f"department {code} is not visible to the eval account")
    ctx.values.update(department=code, department_name=row["name"])


@resolves("department_other", "department_other_name")
def _other_department(ctx: Context) -> None:
    main = ctx.get("department")
    wanted = ctx.settings.other_department
    others = [row for row in _visible_departments(ctx) if row.get("code") != main]
    row = next((row for row in others if wanted in (None, row.get("code"))), None)
    if row is None:
        raise Precondition("the eval account sees no second department")
    ctx.values.update(department_other=row["code"], department_other_name=row["name"])


@resolves("department_unscored_name")
def _unscored_department(ctx: Context) -> None:
    main = ctx.get("department")
    for row in _visible_departments(ctx):
        if row.get("code") == main:
            continue
        stats = ctx.read("get_department_statistics", department=row["code"], period="month")
        if stats.get("status") == "not_available":
            continue  # not readable is not «no evaluations»
        if not ((stats.get("data") or {}).get("summary") or {}).get("calls_evaluated"):
            ctx.values["department_unscored_name"] = row["name"]
            return
    raise Precondition("every other visible department has evaluated calls this month")


@resolves("invisible_department")
def _invisible_department(ctx: Context) -> None:
    phrase = ctx.settings.invisible_department
    stem = _normalize(phrase)[:-2] if len(phrase) > 6 else _normalize(phrase)
    for row in _visible_departments(ctx):
        if stem in _normalize(str(row.get("name"))) or stem in _normalize(str(row.get("code"))):
            raise Precondition(f"department «{phrase}» is visible to the eval account")
    ctx.values["invisible_department"] = phrase


def _ranking(ctx: Context) -> list[dict[str, Any]]:
    stats = ctx.read("get_department_statistics", department=ctx.get("department"), period="month")
    rows = (stats["data"].get("complete_employee_ranking") or {}).get("employees") or []
    return sorted(rows, key=lambda row: str(row.get("full_name")))


def _calls(row: dict[str, Any]) -> int:
    return int((row.get("metrics") or {}).get("calls_total") or 0)


@resolves("employee_card_id", "employee_card_name")
def _card_employee(ctx: Context) -> None:
    row = next((row for row in _ranking(ctx) if _calls(row) > 0), None)
    if row is None:
        raise Precondition("no employee of the department has calls this month")
    ctx.values.update(employee_card_id=row["id"], employee_card_name=row["full_name"])


@resolves("employee_a_id", "employee_a_name", "employee_b_id", "employee_b_name")
def _employee_pair(ctx: Context) -> None:
    busy = sorted((row for row in _ranking(ctx) if _calls(row) > 0), key=lambda row: -_calls(row))
    if len(busy) < 2:
        raise Precondition("fewer than two employees have calls this month")
    first, last = busy[0], busy[-1]
    ctx.values.update(
        employee_a_id=first["id"],
        employee_a_name=first["full_name"],
        employee_b_id=last["id"],
        employee_b_name=last["full_name"],
    )


@resolves("employee_short_id", "employee_short_name")
def _short_calls_employee(ctx: Context) -> None:
    def short(row: dict[str, Any]) -> int:
        metrics = row.get("metrics") or {}
        return int(metrics.get("calls_total") or 0) - int(metrics.get("actual_total") or 0)

    busy = [row for row in _ranking(ctx) if _calls(row) > 0]
    if not busy:
        raise Precondition("no employee of the department has calls this month")
    row = max(busy, key=short)  # the ranking is sorted by name, so ties go to the first name
    ctx.values.update(employee_short_id=row["id"], employee_short_name=row["full_name"])


@resolves("unknown_person")
def _unknown_person(ctx: Context) -> None:
    name = ctx.settings.unknown_person
    if ctx.read("list_employees", search=name)["data"].get("items"):
        raise Precondition(f"«{name}» is a real employee visible to the eval account")
    ctx.values["unknown_person"] = name


@resolves("phrase")
def _phrase(ctx: Context) -> None:
    ctx.values["phrase"] = ctx.settings.phrase


@resolves("call_id")
def _newest_call(ctx: Context) -> None:
    listing = ctx.read("list_call_transcripts", department=ctx.get("department"), period="month", page_size=1)
    items = listing["data"].get("items") or []
    if not items:
        raise Precondition("the department has no calls this month")
    ctx.values["call_id"] = items[0]["call"]["id"]


@resolves("phone")
def _frequent_phone(ctx: Context) -> None:
    page = ctx.read("list_call_phone_records", department=ctx.get("department"), period="month", page_size=25)
    numbers = Counter(
        row.get("counterparty_phone_normalized")
        for row in page["data"].get("items") or []
        if row.get("counterparty_phone_normalized")
    )
    if not numbers:
        raise Precondition("no client number is visible this month (phones scope or no calls)")
    ctx.values["phone"] = numbers.most_common(1)[0][0]


@resolves("scenario_id")
def _only_scenario(ctx: Context) -> None:
    items = ctx.read("list_scenarios", department=ctx.get("department"))["data"].get("items") or []
    if len(items) != 1:
        raise Precondition(f"the department has {len(items)} scenarios, the case needs exactly one")
    ctx.values["scenario_id"] = items[0]["id"]


# ---------------------------------------------------------------------------------------------------
# Expressions over the reads: "read:path | pipe | pipe" or {"sub": [expression, expression]}

PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[([^\]]*)\]")


def _literal(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def walk(node: Any, path: str) -> Any:
    for key, bracket in PATH_TOKEN.findall(path.strip()):
        if node is None:
            return None
        if key:
            node = node.get(key) if isinstance(node, dict) else None
        elif bracket.isdigit():
            node = node[int(bracket)] if isinstance(node, list) and int(bracket) < len(node) else None
        elif "=" in bracket and isinstance(node, list):
            field_path, _, wanted = bracket.partition("=")
            literal = _literal(wanted)
            node = [
                item
                for item in node
                if (walk(item, field_path) == literal)
                or (isinstance(literal, str) and str(walk(item, field_path)) == literal)
            ]
        else:
            return None
    return node


def _numeric(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def apply_pipe(value: Any, pipe: str) -> Any:
    name, _, argument = pipe.partition(":")
    items = value if isinstance(value, list) else []
    if name == "first":
        return items[0] if items else None
    if name == "len":
        return len(items)
    if name == "unique":
        return list(dict.fromkeys(items))
    if name == "get":
        return walk(value, argument)
    if name == "map":
        return [walk(item, argument) for item in items]
    if name == "sum":
        return sum(_numeric(walk(item, argument) if argument else item) or 0 for item in items)
    if name in {"argmax_all", "argmin"}:
        scored = [(item, _numeric(walk(item, argument))) for item in items]
        scored = [(item, score) for item, score in scored if score is not None]
        if not scored:
            return []
        best = (max if name == "argmax_all" else min)(score for _, score in scored)
        winners = [item for item, score in scored if score == best]
        return winners if name == "argmax_all" else winners[:1]
    if name == "where_contains":
        field_path, _, needle = argument.partition(":")
        return [item for item in items if _normalize(needle) in _normalize(str(walk(item, field_path) or ""))]
    raise ValueError(f"unknown pipe {name}")


def evaluate(expression: Any, documents: dict[str, Any]) -> Any:
    if isinstance(expression, dict):
        first, second = (_numeric(evaluate(part, documents)) for part in expression["sub"])
        return None if first is None or second is None else first - second
    source, _, rest = expression.partition(":")
    path, *pipes = rest.split("|")
    value = walk(documents[source], path)
    for pipe in pipes:
        value = apply_pipe(value, pipe.strip())
    return value


# ---------------------------------------------------------------------------------------------------
# Preparing cases


@dataclass
class Expectation:
    kind: str
    candidates: list[Any]
    decimals: int | None = None
    universe: list[str] = field(default_factory=list)


@dataclass
class PreparedCase:
    id: str
    held_out: bool
    status: str  # ready | skipped | truth_unavailable | error
    reason: str = ""
    prompt: str = ""
    tools: dict[str, Any] = field(default_factory=dict)
    args_any: list[dict[str, Any]] = field(default_factory=list)
    expectations: list[Expectation] = field(default_factory=list)
    answer_any: list[str] = field(default_factory=list)
    answer_none: list[str] = field(default_factory=list)

    def plan_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "held_out": self.held_out,
            "status": self.status,
            "reason": self.reason,
            "prompt": self.prompt,
            "tools": self.tools,
            "args_any": self.args_any,
            "expect": [
                {"as": item.kind, "candidates": item.candidates, "decimals": item.decimals}
                for item in self.expectations
            ],
            "answer_any": self.answer_any,
            "answer_none": self.answer_none,
        }


def _check_precondition(rule: dict[str, Any], documents: dict[str, Any]) -> None:
    value, wanted, op = evaluate(rule["value"], documents), rule["to"], rule["op"]
    ok = {
        "eq": lambda: value == wanted,
        "ne": lambda: value != wanted,
        "ge": lambda: value is not None and value >= wanted,
        "le": lambda: value is not None and value <= wanted,
    }[op]()
    if not ok:
        raise Precondition(f"{rule['reason']} ({value!r})")


def prepare_case(case: dict[str, Any], ctx: Context) -> PreparedCase:
    base = PreparedCase(id=case["id"], held_out=case["held_out"], status="ready")
    try:
        base.prompt = ctx.fill(case["prompt"])
        base.tools = case["tools"]
        base.args_any = ctx.fill(case.get("args_any") or [])
        base.answer_any = ctx.fill((case.get("answer") or {}).get("any") or [])
        base.answer_none = ctx.fill((case.get("answer") or {}).get("none") or [])
        truth = ctx.fill(case.get("truth") or {})
        documents = {
            name: ctx.truth.read(read["tool"], read["args"])
            for name, read in (truth.get("reads") or {}).items()
        }
        for rule in truth.get("preconditions") or []:
            _check_precondition(rule, documents)
        for item in truth.get("expect") or []:
            expressions = item.get("any_of") or [item["value"]]
            candidates = [evaluate(expression, documents) for expression in expressions]
            usable = [value for value in candidates if value not in (None, [])]
            if not usable:
                base.status, base.reason = "truth_unavailable", f"OKK returned no value for {expressions[0]}"
                return base
            universe = evaluate(item["universe"], documents) if "universe" in item else []
            base.expectations.append(Expectation(item["as"], usable, item.get("decimals"), universe or []))
    except Precondition as exc:
        base.status, base.reason = "skipped", str(exc)
    except TruthError as exc:
        base.status, base.reason = "error", str(exc)
    return base


# ---------------------------------------------------------------------------------------------------
# Grading


def _normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    for space in (" ", " ", " "):
        text = text.replace(space, " ")
    return re.sub(r"[«»\"“”]", "", text)


NUMBER = re.compile(r"(?<![\d.,])-?\d+(?:[.,]\d+)?")
ZERO_WORDS = ("ни одного", "не было", "нет звонков", "звонков нет", "отсутств")


def _numbers_in(answer: str) -> list[float]:
    compact = re.sub(r"(?<=\d) (?=\d{3}\b)", "", answer)
    return [float(match.replace(",", ".")) for match in NUMBER.findall(compact)]


def _accepted_numbers(value: float, decimals: int | None) -> set[float]:
    if decimals is None or float(value).is_integer():
        return (
            {round(value)} if float(value).is_integer() else {round(value, 2), round(value, 1), round(value)}
        )
    return {round(value, places) for places in range(decimals, 3)} | {round(value)}


def surname_stem(full_name: str) -> str:
    surname = _normalize(full_name).split()[0] if full_name.strip() else ""
    for ending in ("ий", "ый", "ой", "ая", "яя"):
        if len(surname) > 5 and surname.endswith(ending):
            return surname[: -len(ending)]
    if len(surname) > 4 and surname[-1] in "ая":
        return surname[:-1]
    return surname


def _flat(values: list[Any]) -> list[Any]:
    return [item for value in values for item in (value if isinstance(value, list) else [value])]


def _date_forms(value: str) -> list[str]:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    day = (moment if moment.tzinfo else moment.replace(tzinfo=UTC)).astimezone(MSK).date()
    return [f"{day.day} {MONTHS_GEN[day.month - 1]}", f"{day.day:02d}.{day.month:02d}", day.isoformat()]


def _duration_matches(seconds: float, answer: str) -> bool:
    total = round(seconds)
    minutes, rest = divmod(total, 60)
    tenths = f"{total / 60:.1f}"
    patterns = [
        rf"(?<!\d){total}\s*(с|сек)",
        rf"(?<!\d){minutes}\s*мин\w*\.?\s*{rest}\s*(с|сек)",
        rf"(?<!\d){minutes}:{rest:02d}(?!\d)",
        rf"(?<!\d)({re.escape(tenths)}|{re.escape(tenths.replace('.', ','))})\s*мин",
    ]
    return any(re.search(pattern, answer) for pattern in patterns)


def expectation_met(item: Expectation, answer: str) -> bool:
    text = _normalize(answer)
    if item.kind == "number":
        found = _numbers_in(text)
        for value in (_numeric(candidate) for candidate in item.candidates):
            if value is None:
                continue
            if value == 0 and any(word in text for word in ZERO_WORDS):
                return True
            if any(
                abs(number - accepted) < 1e-6
                for number in found
                for accepted in _accepted_numbers(value, item.decimals)
            ):
                return True
        return False
    names = [str(name) for name in _flat(item.candidates) if name]
    if item.kind in {"names", "names_exact"}:
        if not all(surname_stem(name) in text for name in names):
            return False
        expected = {surname_stem(name) for name in names}
        others = {surname_stem(str(name)) for name in item.universe if name} - expected
        return item.kind == "names" or not any(stem and stem in text for stem in others)
    if item.kind == "text":
        return all(_normalize(name)[:30] in text for name in names)
    if item.kind == "date":
        return all(any(form in text for form in _date_forms(value)) for value in names)
    if item.kind == "duration":
        return any(
            _duration_matches(value, text) for value in map(_numeric, item.candidates) if value is not None
        )
    raise ValueError(item.kind)


def tools_selected(tools: dict[str, Any], called: list[str]) -> bool | None:
    mode, names = tools["mode"], tools["names"]
    if mode == "free":
        return None
    if mode == "none":
        return not called
    if mode == "any":
        return any(name in names for name in called)
    if mode == "all":
        return set(names) <= set(called)
    position = 0  # ordered: the expected tools appear in this order, other calls may come between
    for name in called:
        if position < len(names) and name == names[position]:
            position += 1
    return position == len(names)


def args_matched(alternatives: list[dict[str, Any]], calls: list[dict[str, Any]]) -> bool | None:
    if not alternatives:
        return None
    return any(
        all(str(call["arguments"].get(key)) == str(value) for key, value in alternative.items())
        for alternative in alternatives
        for call in calls
    )


def grade(case: PreparedCase, run: dict[str, Any]) -> dict[str, Any]:
    answer = run.get("answer") or ""
    text = _normalize(answer)
    called = [call["tool"] for call in run.get("tool_calls") or []]
    checks: dict[str, bool | None] = {
        "completed": bool(answer.strip()) and not run.get("errors"),
        "tools": tools_selected(case.tools, called),
        "args": args_matched(case.args_any, run.get("tool_calls") or []),
    }
    answer_checks = [expectation_met(item, answer) for item in case.expectations]
    if case.answer_any:
        answer_checks.append(any(_normalize(phrase) in text for phrase in case.answer_any))
    if case.answer_none:
        answer_checks.append(not any(_normalize(phrase) in text for phrase in case.answer_none))
    checks["answer"] = checks["completed"] and all(answer_checks)
    passed = all(value is not False for value in checks.values())
    return {"passed": passed, "checks": checks}


# ---------------------------------------------------------------------------------------------------
# Clients: one agent session per question


@dataclass
class ClientConfig:
    client: str
    gateway_url: str
    tokens: TokenSource
    model: str | None
    reasoning: str | None
    codex_bin: str
    claude_bin: str
    extra_args: list[str]
    workdir: Path


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    """JSON events of an agent CLI; other lines (progress, warnings) are skipped."""

    events = []
    for line in stdout.splitlines():
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def _empty_run() -> dict[str, Any]:
    return {
        "answer": "",
        "tool_calls": [],
        "other_tool_calls": 0,
        "usage": {},
        "cost_usd": None,
        "errors": [],
    }


def codex_command(config: ClientConfig, prompt: str) -> list[str]:
    server = f"mcp_servers.{SERVER_NAME}"
    command = [
        config.codex_bin, "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
        "-s", "read-only", "-c", 'approval_policy="never"', "-c", "features.apps=false",
        "-c", "features.plugins=false", "-c", "features.remote_plugin=false",
        "-c", f'{server}.url="{config.gateway_url}"', "-c", f'{server}.bearer_token_env_var="OKK_EVAL_TOKEN"',
        "-c", f"{server}.tool_timeout_sec=120",
    ]  # fmt: skip
    if config.model:
        command += ["-m", config.model]
    if config.reasoning:
        command += ["-c", f'model_reasoning_effort="{config.reasoning}"']
    return [*command, *config.extra_args, prompt]


def parse_codex(stdout: str) -> dict[str, Any]:
    run = _empty_run()
    messages: list[str] = []
    for event in _json_lines(stdout):
        item = event.get("item") or {}
        if event.get("type") == "item.completed" and item.get("type") == "mcp_tool_call":
            if item.get("server") not in (None, SERVER_NAME):
                run["other_tool_calls"] += 1
                continue
            result = item.get("result") or {}
            blocks = result.get("content") or []
            text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict))
            run["tool_calls"].append(
                {
                    "tool": item.get("tool"),
                    "arguments": item.get("arguments") or {},
                    "error": bool(item.get("error")) or bool(result.get("isError")),
                    "result_chars": len(text),
                }
            )
        elif event.get("type") == "item.completed" and item.get("type") in {
            "command_execution",
            "web_search",
        }:
            run["other_tool_calls"] += 1
        elif event.get("type") == "item.completed" and item.get("type") == "agent_message":
            messages.append(item.get("text") or "")
        elif event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            run["usage"] = {
                "input_tokens": usage.get("input_tokens", 0),
                "cached_input_tokens": usage.get("cached_input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            }
        elif event.get("type") in {"error", "turn.failed"}:
            run["errors"].append(json.dumps(event, ensure_ascii=False)[:300])
    run["answer"] = messages[-1] if messages else ""
    return run


def claude_mcp_config(config: ClientConfig) -> Path:
    """The token stays in the environment: Claude Code expands ${OKK_EVAL_TOKEN} in MCP headers."""

    path = config.workdir.parent / "claude-mcp.json"
    server = {
        "type": "http",
        "url": config.gateway_url,
        "headers": {"Authorization": "Bearer ${OKK_EVAL_TOKEN}"},
    }
    path.write_text(json.dumps({"mcpServers": {SERVER_NAME: server}}, indent=2), encoding="utf-8")
    return path


def claude_command(config: ClientConfig, prompt: str) -> list[str]:
    command = [
        config.claude_bin, "-p", prompt, "--output-format", "stream-json", "--verbose",
        "--mcp-config", str(claude_mcp_config(config)), "--strict-mcp-config",
        "--allowedTools", f"mcp__{SERVER_NAME}", "--no-session-persistence", "--setting-sources", "project",
    ]  # fmt: skip
    if config.model:
        command += ["--model", config.model]
    return [*command, *config.extra_args]


def parse_claude(stdout: str) -> dict[str, Any]:
    run = _empty_run()
    prefix = f"mcp__{SERVER_NAME}__"
    pending: dict[str, dict[str, Any]] = {}
    for event in _json_lines(stdout):
        kind = event.get("type")
        content = (event.get("message") or {}).get("content")
        blocks = content if isinstance(content, list) else []
        if kind == "system" and event.get("subtype") == "init":
            for server in event.get("mcp_servers") or []:
                if server.get("name") == SERVER_NAME and server.get("status") != "connected":
                    run["errors"].append(f"MCP server status: {server.get('status')}")
        elif kind == "assistant":
            for block in blocks:
                if block.get("type") != "tool_use":
                    continue
                if not str(block.get("name")).startswith(prefix):
                    run["other_tool_calls"] += 1
                    continue
                call = {
                    "tool": block["name"][len(prefix) :],
                    "arguments": block.get("input") or {},
                    "error": False,
                    "result_chars": 0,
                }
                pending[block.get("id")] = call
                run["tool_calls"].append(call)
        elif kind == "user":
            for block in blocks:
                call = pending.get(block.get("tool_use_id")) if block.get("type") == "tool_result" else None
                if call is None:
                    continue
                result = block.get("content")
                text = (
                    result
                    if isinstance(result, str)
                    else "".join(part.get("text", "") for part in result or [] if isinstance(part, dict))
                )
                call["result_chars"] = len(text)
                call["error"] = bool(block.get("is_error"))
        elif kind == "result":
            usage = event.get("usage") or {}
            cached = usage.get("cache_read_input_tokens", 0)
            run["answer"] = event.get("result") or ""
            run["cost_usd"] = event.get("total_cost_usd")
            run["usage"] = {
                "input_tokens": usage.get("input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
                + cached,
                "cached_input_tokens": cached,
                "output_tokens": usage.get("output_tokens", 0),
            }
            if event.get("is_error"):
                run["errors"].append(
                    f"result: {event.get('subtype')}, API status {event.get('api_error_status')}"
                )
    return run


def run_agent(config: ClientConfig, prompt: str) -> dict[str, Any]:
    if config.client == "codex":
        command, parse = codex_command(config, prompt), parse_codex
    else:
        command, parse = claude_command(config, prompt), parse_claude
    env = {**os.environ, "OKK_EVAL_TOKEN": config.tokens.get()}
    started = time.perf_counter()
    try:
        process = subprocess.run(
            command, cwd=config.workdir, env=env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=RUN_TIMEOUT_SECONDS,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return {**_empty_run(), "errors": ["timeout"], "seconds": RUN_TIMEOUT_SECONDS, "returncode": None}
    run = parse(process.stdout)
    if process.returncode and not run["errors"]:
        run["errors"].append(f"exit {process.returncode}: {process.stderr.strip()[-300:]}")
    return {**run, "seconds": round(time.perf_counter() - started, 1), "returncode": process.returncode}


# ---------------------------------------------------------------------------------------------------
# Metrics and report


def _subset_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    runs = len(records)
    if not runs:
        return {"runs": 0}
    graded_tools = [record["checks"]["tools"] for record in records if record["checks"]["tools"] is not None]
    calls = [len(record["tool_calls"]) for record in records]
    usage = [record.get("usage") or {} for record in records]
    costs = [record["cost_usd"] for record in records if record.get("cost_usd") is not None]
    return {
        "runs": runs,
        "passed": sum(record["passed"] for record in records),
        "pass_rate": round(sum(record["passed"] for record in records) / runs, 3),
        "answer_accuracy": round(sum(bool(record["checks"]["answer"]) for record in records) / runs, 3),
        "tool_selection_accuracy": round(sum(graded_tools) / len(graded_tools), 3) if graded_tools else None,
        "calls_per_question": round(sum(calls) / runs, 2),
        "tool_errors": sum(call["error"] for record in records for call in record["tool_calls"]),
        "other_tool_calls": sum(record.get("other_tool_calls", 0) for record in records),
        "tool_result_chars": sum(call["result_chars"] for record in records for call in record["tool_calls"]),
        "input_tokens": sum(item.get("input_tokens", 0) for item in usage),
        "cached_input_tokens": sum(item.get("cached_input_tokens", 0) for item in usage),
        "output_tokens": sum(item.get("output_tokens", 0) for item in usage),
        "cost_usd": round(sum(costs), 4) if costs else None,
        "seconds": round(sum(record["seconds"] or 0 for record in records), 1),
    }


def build_report(
    prepared: list[PreparedCase], records: list[dict[str, Any]], meta: dict[str, Any]
) -> dict[str, Any]:
    held = {case.id for case in prepared if case.held_out}
    per_case: dict[str, Any] = {}
    for case in prepared:
        mine = [record for record in records if record["case"] == case.id]
        per_case[case.id] = {
            "held_out": case.held_out,
            "status": case.status,
            "reason": case.reason,
            "runs": len(mine),
            "passed": sum(record["passed"] for record in mine),
            "calls": [len(record["tool_calls"]) for record in mine],
        }
    return {
        "meta": meta,
        "metrics": {
            "all": _subset_metrics(records),
            "main": _subset_metrics([record for record in records if record["case"] not in held]),
            "held_out": _subset_metrics([record for record in records if record["case"] in held]),
        },
        "cases": per_case,
    }


def compare_to_baseline(report: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """The release rule: no drop on non-held-out cases compared with the previous release."""

    def rate(case: dict[str, Any]) -> float:
        return case["passed"] / case["runs"]

    now, before = report["cases"], baseline["cases"]
    shared = [
        case_id
        for case_id in now
        if case_id in before
        and not now[case_id]["held_out"]
        and now[case_id]["runs"]
        and before[case_id]["runs"]
    ]
    dropped = [case_id for case_id in shared if rate(now[case_id]) < rate(before[case_id])]
    improved = [case_id for case_id in shared if rate(now[case_id]) > rate(before[case_id])]
    now_total = sum(rate(now[case_id]) for case_id in shared)
    before_total = sum(rate(before[case_id]) for case_id in shared)
    return {
        "compared_cases": len(shared),
        "main_score_now": round(now_total, 2),
        "main_score_before": round(before_total, 2),
        "dropped": dropped,
        "improved": improved,
        "release_gate": "pass" if now_total >= before_total - 1e-9 else "fail",
    }


def summary_markdown(report: dict[str, Any]) -> str:
    meta = report["meta"]
    lines = [
        f"# OKK MCP eval: {meta['label']}",
        "",
        f"Client `{meta['client']}`, model `{meta.get('model') or 'default'}`, repeats {meta['repeat']}, "
        f"gateway `{meta['gateway_host']}`, started {meta['started_at']}.",
        "",
        "| Metric | All | Main | Held-out |",
        "|---|---|---|---|",
    ]
    names = [
        "runs", "passed", "pass_rate", "answer_accuracy", "tool_selection_accuracy", "calls_per_question",
        "tool_errors", "tool_result_chars", "input_tokens", "cached_input_tokens", "output_tokens", "cost_usd",
        "seconds",
    ]  # fmt: skip
    metrics = report["metrics"]
    for name in names:
        cells = [str(metrics[part].get(name, "-")) for part in ("all", "main", "held_out")]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "| Case | Held-out | Status | Passed | Calls |", "|---|---|---|---|---|"]
    for case_id, case in report["cases"].items():
        status = case["status"] if case["status"] == "ready" else f"{case['status']}: {case['reason']}"
        passed = f"{case['passed']}/{case['runs']}" if case["runs"] else "-"
        lines.append(
            f"| {case_id} | {'yes' if case['held_out'] else ''} | {status} | {passed} | {case['calls']} |"
        )
    if "baseline" in report:
        gate = report["baseline"]
        lines += [
            "",
            f"Release gate vs baseline: **{gate['release_gate']}** — main score {gate['main_score_now']} "
            f"(before {gate['main_score_before']}) on {gate['compared_cases']} cases; "
            f"dropped: {', '.join(gate['dropped']) or 'none'}; improved: {', '.join(gate['improved']) or 'none'}.",
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------------
# Dry run: plan and grader self-check without any model


def _ideal_answer(case: PreparedCase) -> str:
    parts: list[str] = []
    for item in case.expectations:
        value = item.candidates[0]
        if item.kind == "number":
            number = float(value)
            parts.append(
                str(round(number))
                if number.is_integer() or item.decimals is None
                else f"{number:.1f}".replace(".", ",")
            )
        elif item.kind == "date":
            parts.append(_date_forms(value)[0])
        elif item.kind == "duration":
            parts.append(f"{round(float(value))} сек")
        else:
            parts += [str(name) for name in _flat([value])]
    parts += case.answer_any[:1]
    return "Ответ: " + "; ".join(parts) + "." if parts else "Это вне возможностей подключения."


def _ideal_calls(case: PreparedCase) -> list[dict[str, Any]]:
    names = case.tools["names"][:1] if case.tools["mode"] == "any" else case.tools["names"]
    arguments = case.args_any[0] if case.args_any else {}
    return [{"tool": name, "arguments": arguments, "error": False, "result_chars": 0} for name in names]


def grader_self_check(prepared: list[PreparedCase]) -> list[str]:
    """An ideal answer must pass and an empty one must fail: catches a broken case or grader."""

    problems = []
    for case in prepared:
        if case.status != "ready":
            continue
        ideal = {"answer": _ideal_answer(case), "tool_calls": _ideal_calls(case), "errors": []}
        if not grade(case, ideal)["passed"]:
            problems.append(f"{case.id}: an ideal answer fails ({grade(case, ideal)['checks']})")
        if grade(case, {"answer": "", "tool_calls": [], "errors": []})["passed"]:
            problems.append(f"{case.id}: an empty answer passes")
    return problems


# ---------------------------------------------------------------------------------------------------
# CLI


def prepare_all(suite: dict[str, Any], ctx: Context, only: set[str], split: str) -> list[PreparedCase]:
    chosen = [
        case
        for case in suite["cases"]
        if (not only or case["id"] in only) and (split == "all" or (split == "held-out") == case["held_out"])
    ]
    return [prepare_case(case, ctx) for case in chosen]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--client", choices=["codex", "claude"], help="agent to drive (paid run)")
    parser.add_argument(
        "--dry-run", action="store_true", help="validate, resolve and print the plan; no model"
    )
    parser.add_argument(
        "--confirm-paid-run", action="store_true", help="required for a run that calls a model"
    )
    parser.add_argument(
        "--gateway-url", default=os.environ.get("OKK_EVAL_GATEWAY_URL"), help="…/mcp of the gateway"
    )
    parser.add_argument(
        "--token-env", default="OKK_EVAL_TOKEN", help="env variable with the gateway bearer token"
    )
    parser.add_argument("--token-file", type=Path, help="JSON with access_token, refresh_token, client_id")
    parser.add_argument(
        "--truth-from",
        choices=["fixture", "gateway"],
        help="default: gateway for runs, fixture for --dry-run",
    )
    parser.add_argument("--fixture", type=Path, default=SYNTHETIC_FIXTURE)
    parser.add_argument(
        "--record-fixture", type=Path, help="save the reads used for truth (keep it in evals/out/)"
    )
    parser.add_argument("--today", type=date.fromisoformat, help="Moscow date for periods; default today")
    parser.add_argument("--department", default="csm")
    parser.add_argument("--other-department")
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--split", choices=["all", "main", "held-out"], default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--model")
    parser.add_argument("--reasoning", help="Codex model_reasoning_effort, e.g. medium")
    parser.add_argument(
        "--codex-bin", default=os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"
    )
    parser.add_argument(
        "--claude-bin", default=os.environ.get("CLAUDE_BIN") or shutil.which("claude") or "claude"
    )
    parser.add_argument("--client-arg", action="append", default=[], help="extra argument for the agent CLI")
    parser.add_argument("--label", help="output folder name under evals/out/")
    parser.add_argument(
        "--baseline", type=Path, help="report.json of the previous release for the release gate"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    if not args.dry_run and not args.client:
        raise SystemExit("Choose --dry-run or --client codex|claude.")
    suite = load_cases()
    errors = validate_cases(suite)
    if errors:
        print("cases.json is invalid:\n  " + "\n  ".join(errors))
        return 1
    truth_from = args.truth_from or ("fixture" if args.dry_run else "gateway")
    tokens = None
    if truth_from == "gateway" or not args.dry_run:
        if not args.gateway_url:
            raise SystemExit("Pass --gateway-url or set OKK_EVAL_GATEWAY_URL.")
        tokens = TokenSource(args.gateway_url, args.token_env, args.token_file)
    if truth_from == "fixture":
        fixture = FixtureTruth(args.fixture)
        source, today = fixture, args.today or fixture.today
    else:
        source, today = GatewayTruth(args.gateway_url, tokens), args.today or datetime.now(MSK).date()
    truth = CachedTruth(source)
    ctx = Context(
        truth, today, SuiteSettings(department=args.department, other_department=args.other_department)
    )
    only = {item for item in args.only.split(",") if item}
    prepared = prepare_all(suite, ctx, only, args.split)
    if args.record_fixture:
        args.record_fixture.parent.mkdir(parents=True, exist_ok=True)
        payload = {"today": today.isoformat(), "reads": truth.recorded}
        args.record_fixture.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"recorded {len(truth.recorded)} reads to {args.record_fixture}")
    ready = [case for case in prepared if case.status == "ready"]
    if args.dry_run:
        return dry_run_report(prepared, args, today)
    if not args.confirm_paid_run:
        print(
            f"This run starts {len(ready) * args.repeat} paid {args.client} sessions "
            f"({len(ready)} cases x {args.repeat}). Re-run with --confirm-paid-run after the owner approves."
        )
        return 2
    return paid_run(prepared, args, tokens, today)


def dry_run_report(prepared: list[PreparedCase], args: argparse.Namespace, today: date) -> int:
    ready = [case for case in prepared if case.status == "ready"]
    for case in prepared:
        mark = " [held-out]" if case.held_out else ""
        print(f"- {case.id}{mark}: {case.status}{' — ' + case.reason if case.reason else ''}")
        if case.status == "ready":
            print(f"    prompt: {case.prompt}")
            print(
                f"    tools:  {case.tools['mode']} {case.tools['names']}"
                + (f"; args any of {case.args_any}" if case.args_any else "")
            )
            for item in case.expectations:
                print(
                    f"    expect: {item.kind} {item.candidates}"
                    + (f" (decimals {item.decimals})" if item.decimals else "")
                )
            if case.answer_any or case.answer_none:
                print(f"    phrases: any {len(case.answer_any)}, none {len(case.answer_none)}")
    problems = grader_self_check(prepared)
    counts = Counter(case.status for case in prepared)
    held = sum(case.held_out for case in prepared)
    print(
        f"\nDRY RUN ({today.isoformat()}): {len(prepared)} cases, {held} held-out; "
        + ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
    )
    print(
        f"A paid run would start {len(ready) * args.repeat} agent sessions per client "
        f"({len(ready)} ready cases x {args.repeat} repeats); no model was called."
    )
    label = args.label or f"dry-run-{datetime.now(MSK):%Y%m%d-%H%M%S}"
    out = OUT_DIR / label
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(
        json.dumps([case.plan_row() for case in prepared], ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"plan: {out / 'plan.json'}")
    if problems:
        print("GRADER SELF-CHECK FAILED:\n  " + "\n  ".join(problems))
        return 1
    print("grader self-check: ideal answers pass, empty answers fail")
    return 1 if counts.get("error") else 0


def paid_run(prepared: list[PreparedCase], args: argparse.Namespace, tokens: TokenSource, today: date) -> int:
    label = args.label or f"{args.client}-{datetime.now(MSK):%Y%m%d-%H%M%S}"
    out = OUT_DIR / label
    workdir = out / "workdir"  # empty folder: the agent sees no repository or project instructions
    workdir.mkdir(parents=True, exist_ok=True)
    config = ClientConfig(
        client=args.client,
        gateway_url=args.gateway_url,
        tokens=tokens,
        model=args.model,
        reasoning=args.reasoning,
        codex_bin=args.codex_bin,
        claude_bin=args.claude_bin,
        extra_args=args.client_arg,
        workdir=workdir,
    )
    jobs = [(case, repeat) for case in prepared if case.status == "ready" for repeat in range(args.repeat)]
    lock = threading.Lock()
    runs_path = out / "runs.jsonl"

    def one(job: tuple[PreparedCase, int]) -> dict[str, Any]:
        case, repeat = job
        run = run_agent(config, case.prompt)
        record = {
            "case": case.id,
            "repeat": repeat,
            "held_out": case.held_out,
            "prompt": case.prompt,
            **grade(case, run),
            **run,
        }
        with lock, runs_path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")
        verdict = "PASS" if record["passed"] else "FAIL"
        print(
            f"{case.id} #{repeat}: {verdict} {record['checks']} calls={len(run['tool_calls'])} {run['seconds']}s",
            flush=True,
        )
        return record

    started = datetime.now(MSK).isoformat(timespec="seconds")
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        records = list(pool.map(one, jobs))
    meta = {
        "label": label,
        "client": args.client,
        "model": args.model,
        "reasoning": args.reasoning,
        "repeat": args.repeat,
        "gateway_host": re.sub(r"^https?://", "", args.gateway_url).split("/")[0],
        "department": args.department,
        "today": today.isoformat(),
        "started_at": started,
    }
    report = build_report(prepared, records, meta)
    if args.baseline:
        report["baseline"] = compare_to_baseline(
            report, json.loads(args.baseline.read_text(encoding="utf-8"))
        )
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "summary.md").write_text(summary_markdown(report), encoding="utf-8")
    main_metrics = report["metrics"]["main"]
    print(
        f"\nmain: passed {main_metrics.get('passed')}/{main_metrics.get('runs')}; report: {out / 'report.json'}"
    )
    if report.get("baseline", {}).get("release_gate") == "fail":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
