from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.lawyer_workbench._support.db import PgTarget, count
from tests.lawyer_workbench._support.docx import (
    assert_docx_contains,
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow
from tests.lawyer_workbench._support.phase_timeline import (
    assert_has_phases,
    assert_phase_status_in,
    unwrap_phase_timeline,
    phase_ids,
)
from tests.lawyer_workbench._support.profile import assert_has_party, assert_service_type
from tests.lawyer_workbench._support.sse import assert_task_lifecycle, assert_visible_response
from tests.lawyer_workbench._support.timeline import assert_timeline_has_output_keys, unwrap_timeline
from tests.lawyer_workbench._support.utils import unwrap_api_response


_MATTER_DB = PgTarget(dbname=os.getenv("E2E_MATTER_DB", "matter-service"))


@pytest.mark.e2e
@pytest.mark.slow
async def test_labor_arbitration_respondent_generates_labor_defense_doc(lawyer_client):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    req_path = evidence_dir / "opponent_labor_arbitration_request.txt"

    up = await lawyer_client.upload_file(str(req_path), purpose="consultation")
    req_file_id = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
    assert req_file_id, up

    sess = await lawyer_client.create_session(service_type_id="labor_arbitration_respondent", client_role="respondent")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=[req_file_id],
        overrides={},
    )

    first_sse = await flow.nudge(
        "我方（被申请人）上海某某科技有限公司E2E_LAB01，收到申请人王五E2E_LAB01劳动仲裁申请。"
        "请先做证据与争点梳理，给出应诉策略，并生成《劳动仲裁答辩书》。",
        attachments=[req_file_id],
        max_loops=80,
    )
    assert_visible_response(first_sse)
    assert_task_lifecycle(first_sse)

    async def _labor_defense_ready(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            return False
        resp = await f.client.list_deliverables(f.matter_id, output_key="labor_defense")
        data = unwrap_api_response(resp)
        return isinstance(data, dict) and bool(data.get("deliverables"))

    await flow.run_until(_labor_defense_ready, max_steps=70, description="labor_defense deliverable")
    assert flow.matter_id

    mid_int = int(flow.matter_id)
    assert await count(_MATTER_DB, "select count(1) from matters where id = %s", [mid_int]) == 1
    assert (
        await count(
            _MATTER_DB,
            "select count(1) from matter_deliverables where matter_id = %s and output_key = %s",
            [mid_int, "labor_defense"],
        )
        == 1
    )

    traces_resp = await lawyer_client.list_traces(flow.matter_id, limit=200)
    traces_data = unwrap_api_response(traces_resp)
    traces = traces_data.get("traces") if isinstance(traces_data, dict) else None
    assert isinstance(traces, list) and traces, traces_resp
    node_ids = {str(it.get("node_id") or "").strip() for it in traces if isinstance(it, dict)}
    assert any(x in node_ids for x in {"skill:arbitration-intake", "arbitration-intake"})
    assert any(x in node_ids for x in {"skill:defense-planning", "defense-planning"})
    assert any(x in node_ids for x in {"skill:document-generation", "document-generation"})

    prof = unwrap_api_response(await lawyer_client.get_workflow_profile(flow.matter_id))
    assert isinstance(prof, dict), prof
    assert_service_type(prof, "labor_arbitration_respondent")
    assert_has_party(prof, role="applicant", name_contains="王五E2E_LAB01")
    assert_has_party(prof, role="respondent", name_contains="上海某某科技有限公司E2E_LAB01")

    pt_resp = await lawyer_client.get_matter_phase_timeline(flow.matter_id)
    pt = unwrap_phase_timeline(pt_resp)
    assert_has_phases(pt, must_include=["materials", "intake", "evidence", "strategy", "output", "docgen"])
    assert "cause" not in set(phase_ids(pt)), phase_ids(pt)
    assert_phase_status_in(pt, phase_id="materials", allowed=["completed", "in_progress"])

    tl_resp = await lawyer_client.get_matter_timeline(flow.matter_id, limit=50)
    tl = unwrap_timeline(tl_resp)
    assert_timeline_has_output_keys(tl, must_include=["labor_defense"])

    dels_resp = await lawyer_client.list_deliverables(flow.matter_id, output_key="labor_defense")
    dels = unwrap_api_response(dels_resp)
    items = (dels.get("deliverables") if isinstance(dels, dict) else None) or []
    assert items, dels_resp
    d0 = items[0] if isinstance(items[0], dict) else {}
    file_id = str(d0.get("file_id") or "").strip()
    assert file_id, d0
    docx_bytes = await lawyer_client.download_file_bytes(file_id)
    text = extract_docx_text(docx_bytes)
    assert_docx_has_no_template_placeholders(text)
    assert_docx_contains(text, must_include=["答辩", "王五E2E_LAB01", "上海某某科技有限公司E2E_LAB01"])

