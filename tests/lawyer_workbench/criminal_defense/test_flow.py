from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.lawyer_workbench._support.db import PgTarget, count
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow
from tests.lawyer_workbench._support.phase_timeline import (
    assert_has_phases,
    unwrap_phase_timeline,
    phase_ids,
)
from tests.lawyer_workbench._support.profile import assert_service_type
from tests.lawyer_workbench._support.sse import assert_task_lifecycle, assert_visible_response
from tests.lawyer_workbench._support.utils import unwrap_api_response


_MATTER_DB = PgTarget(dbname=os.getenv("E2E_MATTER_DB", "matter-service"))


@pytest.mark.e2e
@pytest.mark.slow
async def test_criminal_defense_persists_phase_summary_and_has_no_cause_phase(lawyer_client):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    brief_path = evidence_dir / "case_brief.txt"

    up = await lawyer_client.upload_file(str(brief_path), purpose="consultation")
    brief_file_id = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
    assert brief_file_id, up

    sess = await lawyer_client.create_session(service_type_id="criminal_defense", client_role="criminal_defendant")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=[brief_file_id],
        overrides={},
    )

    first_sse = await flow.nudge(
        "我方赵六E2E_CRIM01涉嫌盗窃，现处于侦查阶段。"
        "请基于现有材料做证据与程序风险评估，给出侦查阶段辩护策略与下一步工作计划。",
        attachments=[brief_file_id],
        max_loops=80,
    )
    assert_visible_response(first_sse)
    assert_task_lifecycle(first_sse)

    async def _phase_summary_ready(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            return False
        resp = await f.client.list_deliverables(f.matter_id, output_key="phase_summary__case_output")
        data = unwrap_api_response(resp)
        return isinstance(data, dict) and bool(data.get("deliverables"))

    await flow.run_until(_phase_summary_ready, max_steps=70, description="phase_summary__case_output deliverable")
    assert flow.matter_id

    mid_int = int(flow.matter_id)
    assert await count(_MATTER_DB, "select count(1) from matters where id = %s", [mid_int]) == 1
    assert (
        await count(
            _MATTER_DB,
            "select count(1) from matter_deliverables where matter_id = %s and output_key = %s",
            [mid_int, "phase_summary__case_output"],
        )
        == 1
    )

    traces_resp = await lawyer_client.list_traces(flow.matter_id, limit=200)
    traces_data = unwrap_api_response(traces_resp)
    traces = traces_data.get("traces") if isinstance(traces_data, dict) else None
    assert isinstance(traces, list) and traces, traces_resp
    node_ids = {str(it.get("node_id") or "").strip() for it in traces if isinstance(it, dict)}
    assert any(x in node_ids for x in {"skill:criminal-intake", "criminal-intake"})
    assert any(x in node_ids for x in {"skill:criminal-defense-planning", "criminal-defense-planning"})
    assert any(x in node_ids for x in {"skill:evidence-analysis", "evidence-analysis"})

    prof = unwrap_api_response(await lawyer_client.get_workflow_profile(flow.matter_id))
    assert isinstance(prof, dict), prof
    assert_service_type(prof, "criminal_defense")

    pt_resp = await lawyer_client.get_matter_phase_timeline(flow.matter_id)
    pt = unwrap_phase_timeline(pt_resp)
    assert_has_phases(pt, must_include=["materials", "intake", "evidence", "strategy", "output"])
    assert "cause" not in set(phase_ids(pt)), phase_ids(pt)

