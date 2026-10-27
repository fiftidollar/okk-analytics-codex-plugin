"""The eval suite (evals/) must stay runnable against the tools the server really has."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from okk_mcp.config import Settings
from okk_mcp.server import create_mcp_server

EVALS_RUN = Path(__file__).resolve().parents[2] / "evals" / "run.py"


@pytest.fixture(scope="module")
def suite_runner():
    spec = importlib.util.spec_from_file_location("okk_evals_run", EVALS_RUN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def test_cases_name_only_existing_tools(suite_runner):
    """A renamed or removed tool would silently fail every case that expects it."""

    tools = {tool.name for tool in asyncio.run(create_mcp_server(Settings(), AsyncMock()).list_tools())}
    assert suite_runner.validate_cases(suite_runner.load_cases(), known_tools=tools) == []


def test_every_case_resolves_and_grades_on_the_synthetic_fixture(suite_runner):
    """A broken placeholder, read path or grader shows up here instead of in a paid run."""

    fixture = suite_runner.FixtureTruth(suite_runner.SYNTHETIC_FIXTURE)
    context = suite_runner.Context(
        suite_runner.CachedTruth(fixture), fixture.today, suite_runner.SuiteSettings()
    )
    prepared = suite_runner.prepare_all(suite_runner.load_cases(), context, set(), "all")
    assert [(case.id, case.reason) for case in prepared if case.status != "ready"] == []
    assert suite_runner.grader_self_check(prepared) == []
