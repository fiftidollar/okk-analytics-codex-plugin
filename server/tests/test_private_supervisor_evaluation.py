"""A private supervisor whose calls OKK evaluates — on the answers OKK really sends.

OKK keeps two kinds of private (closed) supervisors: transcription-only, and one whose calls are
evaluated by the criteria of the department it keeps for that. The answers in
`fixtures/okk_private_supervisors.json` were captured from the OKK test API for synthetic people.

Catches: the calls of an evaluated supervisor fall out of the supervisors section because they carry
a department (statistics said "no data", transcripts were not listed); a holder who sees no
department loses them; the score reaches a transcription-only supervisor's answer; a closed
employee's call card comes out as an ordinary department call; an ordinary transcript list starts
asking OKK for the private catalog.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from okk_mcp.backend_client import AnalyticsAdapter
from okk_mcp.config import Settings
from okk_mcp.platform_client import AccountContext
from okk_mcp.server import AnalyticsEnvelope

OKK = json.loads(
    (Path(__file__).parent / "fixtures/okk_private_supervisors.json").read_text(encoding="utf-8")
)
EVALUATED = next(row for row in OKK["restricted"] if row["processing_mode"] == "full")
TRANSCRIPT_ONLY = next(row for row in OKK["restricted"] if row["processing_mode"] != "full")
EVALUATED_CALL = OKK["card_evaluated"]
SEPTEMBER = {"period": "custom", "start_date": "2026-09-01", "end_date": "2026-09-30"}
# What the supervisors section never carries, whatever OKK sends in the row.
NEVER_SHOWN = ("79990000002", "79990000003", "counterparty_phone_normalized", "caller_number", "audio_url")


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Okk:
    """OKK as one person sees it: fixed answers by path; a callable answer may raise."""

    def __init__(self, responses, *, role="admin", department_ids=()):
        self.context = AccountContext(
            session_id=uuid4(),
            user_id="user-1",
            email="holder@example.com",
            role=role,
            department_ids=tuple(department_ids),
            access_token="upstream-access",
        )
        self.responses = responses
        self.paths: list[str] = []

    async def live_context(self, _session_id):
        return self.context

    async def get(self, _session_id, path, *, params=None):
        self.paths.append(path)
        value = self.responses[path]
        return value(params or []) if callable(value) else value

    async def get_with_context(self, _context, path, *, params=None):
        return await self.get(None, path, params=params)

    async def phone_lookup_with_context(self, _context, _payload):
        self.paths.append("/calls/phone-lookup")
        return self.responses["/calls/phone-lookup"]

    async def transcript_search_with_context(self, _context, _payload):
        self.paths.append("/calls/transcript-search")
        return self.responses["/calls/transcript-search"]


def holder(*, role="admin", department_ids=(), calls="calls_evaluated", **extra) -> Okk:
    return Okk(
        {
            "/departments": [],
            "/employees/restricted": OKK["restricted"],
            "/calls": OKK[calls],
            "/calls/transcript-search": OKK["search_evaluated"],
            "/calls/phone-lookup": OKK["phone_evaluated"],
            f"/calls/{EVALUATED_CALL['id']}": EVALUATED_CALL,
            f"/calls/{EVALUATED_CALL['id']}/transcript": OKK["transcript_evaluated"],
            **extra,
        },
        role=role,
        department_ids=department_ids,
    )


def adapter(okk: Okk) -> AnalyticsAdapter:
    return AnalyticsAdapter(okk, str(okk.context.session_id), Settings())


def sent_to_the_client(result: dict) -> dict:
    """The answer as the MCP server sends it: every tool answer passes the envelope model."""
    return AnalyticsEnvelope.model_validate(result).model_dump(mode="json")


@pytest.mark.anyio
async def test_catalog_marks_only_the_supervisor_whose_calls_okk_evaluates():
    result = await adapter(holder()).list_supervisors()

    by_id = {row["id"]: row for row in sent_to_the_client(result)["data"]["items"]}
    assert by_id[EVALUATED["id"]]["evaluates_calls"] is True
    assert by_id[TRANSCRIPT_ONLY["id"]] == {
        "id": TRANSCRIPT_ONLY["id"],
        "full_name": TRANSCRIPT_ONLY["full_name"],
        "position": TRANSCRIPT_ONLY["position"],
        "is_active": True,
        "section": "supervisors",
    }
    # The department whose criteria are used, the provider accounts and the mode itself stay in OKK.
    assert "department_id" not in str(result) and "telephony" not in str(result)


@pytest.mark.anyio
async def test_statistics_of_an_evaluated_supervisor_count_its_calls_and_scores():
    result = await adapter(holder()).get_supervisor_call_statistics(EVALUATED["id"], **SEPTEMBER)

    data = result["data"]
    assert result["status"] == "ok"
    assert (data["calls_total"], data["loaded_calls"], data["source_complete"]) == (1, 1, True)
    assert data["evaluation_metrics_available"] is True
    assert data["evaluation_loaded"] == {
        "evaluated_calls": 1,
        "average_score": 45.0,
        "below_norm_calls": 1,
        "pass_rate": 0.0,
        "pass_rate_basis": "department_norm",
    }
    assert sent_to_the_client(result)["data"]["evaluation_loaded"] == data["evaluation_loaded"]
    assert result["effective_scope"]["section"] == "supervisors"
    assert not any(secret in str(result) for secret in NEVER_SHOWN)


@pytest.mark.anyio
async def test_holder_who_sees_no_department_still_gets_the_evaluated_supervisors_calls():
    # The grant is personal: the department OKK stamps on the call is not in this person's access.
    okk = holder(role="viewer", department_ids=())

    statistics = await adapter(okk).get_supervisor_call_statistics(EVALUATED["id"], **SEPTEMBER)
    listed = await adapter(okk).list_supervisor_call_transcripts(EVALUATED["id"], **SEPTEMBER)

    assert statistics["data"]["loaded_calls"] == 1
    assert listed["status"] == "ok" and listed["data"]["returned_calls"] == 1


@pytest.mark.anyio
async def test_transcript_tools_return_the_evaluated_supervisors_call_with_its_score():
    analytics = adapter(holder())

    listed = await analytics.list_supervisor_call_transcripts(EVALUATED["id"], **SEPTEMBER)
    loaded = await analytics.get_supervisor_call_transcript(
        call_id=EVALUATED_CALL["id"], supervisor_id=EVALUATED["id"]
    )
    searched = await analytics.search_supervisor_call_transcripts(
        "Добрый", supervisor_id=EVALUATED["id"], **SEPTEMBER
    )
    by_number = await analytics.list_call_phone_records(supervisor_id=EVALUATED["id"])

    call = sent_to_the_client(listed)["data"]["items"][0]["call"]
    assert listed["status"] == "ok"
    assert call["employee"] == {
        "id": EVALUATED["id"],
        "full_name": EVALUATED["full_name"],
        "position": EVALUATED["position"],
        "section": "supervisors",
    }
    assert (call["quality_score"], call["quality_status"], call["quality_threshold"]) == (
        "45.00",
        "below_norm",
        80,
    )
    assert loaded["status"] == "ok"
    assert loaded["data"]["transcript"] == OKK["transcript_evaluated"]["transcript"]
    assert loaded["data"]["call"]["quality_status"] == "below_norm"
    assert searched["status"] == "ok" and searched["data"]["returned_matches"] == 1
    assert by_number["status"] == "ok"
    assert by_number["data"]["items"][0]["employee"]["section"] == "supervisors"
    # The department of the criteria never names the supervisor as its ordinary employee.
    assert "department" not in call["employee"]
    assert "Сопровождение" not in str((listed, loaded, searched, by_number))


@pytest.mark.anyio
async def test_transcription_only_supervisor_gets_no_evaluation_fields():
    okk = holder(calls="calls_transcript_only")

    statistics = await adapter(okk).get_supervisor_call_statistics(TRANSCRIPT_ONLY["id"], **SEPTEMBER)

    assert statistics["status"] == "ok"
    assert statistics["data"]["loaded_calls"] == 1
    assert statistics["data"]["evaluation_metrics_available"] is False
    assert "evaluation_loaded" not in statistics["data"]
    assert "quality_score" not in str(statistics)


@pytest.mark.anyio
async def test_score_okk_does_not_show_is_not_reported_even_for_a_full_mode_supervisor():
    # An OKK that evaluates no supervisors hides the score of every closed employee: the mode of
    # the catalog record alone must not turn a kept old score into a reported one.
    hidden = json.loads(json.dumps(OKK["calls_evaluated"]))
    hidden["items"][0]["shown"] = {"evaluation": False, "call_status": False, "client": False}

    result = await adapter(holder(**{"/calls": hidden})).get_supervisor_call_statistics(
        EVALUATED["id"], **SEPTEMBER
    )

    assert result["data"]["loaded_calls"] == 1
    assert result["data"]["evaluation_loaded"]["evaluated_calls"] == 0
    assert result["data"]["evaluation_loaded"]["average_score"] is None
    assert "45.00" not in str(result)


@pytest.mark.anyio
async def test_call_card_of_an_evaluated_supervisor_stays_in_the_supervisors_section():
    # The generic transcript tool reads the card by id. The card names a department for this call:
    # it must still come out as the private supervisor's call, never as that department's.
    result = await adapter(holder()).get_call_transcript(EVALUATED_CALL["id"])

    assert result["status"] == "ok"
    assert result["data"]["call"]["employee"]["section"] == "supervisors"
    assert "department" not in result["data"]["call"]["employee"]


@pytest.mark.anyio
async def test_closed_employees_card_without_a_grant_in_the_catalog_is_not_available():
    okk = holder(**{"/employees/restricted": []})

    result = await adapter(okk).get_call_transcript(EVALUATED_CALL["id"])

    assert result["status"] == "not_available"
    assert result["data"] == {}
    assert not any(path.endswith("/transcript") for path in okk.paths)


@pytest.mark.anyio
async def test_supervisor_tool_for_another_supervisor_does_not_return_this_ones_call():
    result = await adapter(holder()).get_supervisor_call_transcript(
        call_id=EVALUATED_CALL["id"], supervisor_id=TRANSCRIPT_ONLY["id"]
    )

    assert result["status"] == "not_available"


@pytest.mark.anyio
async def test_ordinary_transcript_list_does_not_ask_okk_for_the_private_catalog():
    # Ordinary transcript reads are the most frequent requests: they must not grow by one OKK request.
    department_id, employee_id, call_id = (str(uuid4()) for _ in range(3))
    department = {"id": department_id, "name": "Продажи", "code": "sales"}
    row = {
        "id": call_id,
        "employee_id": employee_id,
        "started_at": "2026-09-09T09:00:00Z",
        "employee": {"id": employee_id, "full_name": "Иванова Анна", "department": department},
    }
    okk = Okk(
        {
            "/departments": [department],
            "/employees": [{"id": employee_id, "full_name": "Иванова Анна", "department_id": department_id}],
            "/calls": {"items": [row], "total": 1, "page": 1, "page_size": 25, "pages": 1},
            f"/calls/{call_id}/transcript": {"transcript": "Менеджер: добрый день"},
        },
        department_ids=(department_id,),
    )

    result = await adapter(okk).list_call_transcripts(department_ref="sales", **SEPTEMBER)

    assert result["status"] == "ok" and result["data"]["returned_calls"] == 1
    assert "/employees/restricted" not in okk.paths
