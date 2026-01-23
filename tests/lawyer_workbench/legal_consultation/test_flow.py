from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.lawyer_workbench._support.db import PgTarget, count
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow
from tests.lawyer_workbench._support.utils import eventually, unwrap_api_response


_MATTER_DB = PgTarget(dbname=os.getenv("E2E_MATTER_DB", "matter-service"))


def _consult_facts() -> str:
    return (
        "我与房东签订租赁合同，押金2000元。退租时房东以墙面污损为由拒退押金。\n"
        "我有：租赁合同、交接清单、聊天记录。诉求：退还押金2000元并承担合理费用。"
    )


@pytest.mark.e2e
async def test_legal_consultation_can_run_and_switch_to_litigation(lawyer_client):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    note_path = evidence_dir / "consult_note.txt"

    up = await lawyer_client.upload_file(str(note_path), purpose="consultation")
    note_file_id = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
    assert note_file_id, up

    sess = await lawyer_client.create_session(service_type_id="legal_consultation")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    flow = WorkbenchFlow(client=lawyer_client, session_id=session_id, uploaded_file_ids=[note_file_id])

    # Prime the consult loop with a single rich message (consultation playbook may not always interrupt with a card).
    await flow.nudge(_consult_facts(), attachments=[note_file_id], max_loops=12)

    async def _consult_traces_ready() -> bool:
        await flow.refresh()
        if not flow.matter_id:
            return False
        resp = await lawyer_client.list_traces(flow.matter_id, limit=100)
        data = unwrap_api_response(resp)
        traces = data.get("traces") if isinstance(data, dict) else None
        if not isinstance(traces, list) or not traces:
            return False
        node_ids = {str(it.get("node_id") or "").strip() for it in traces if isinstance(it, dict)}
        return ("consult-intake" in node_ids) and ("readiness-assessment" in node_ids)

    assert await eventually(_consult_traces_ready, timeout_s=120.0, interval_s=2.0, description="consultation traces")
    assert flow.matter_id

    mid_int = int(flow.matter_id)
    assert await count(_MATTER_DB, "select count(1) from matters where id = %s", [mid_int]) == 1

    # Switching service type is the official \"consultation -> workbench service\" bridge.
    sw = await lawyer_client.switch_service_type(session_id, service_type_id="civil_prosecution", title="E2E押金纠纷")
    sw_data = unwrap_api_response(sw)
    assert isinstance(sw_data, dict) and str(sw_data.get("matter_id") or "").strip(), sw

    prof_resp = await lawyer_client.get_workflow_profile(flow.matter_id)
    prof = unwrap_api_response(prof_resp)
    assert isinstance(prof, dict), prof_resp
    assert str(prof.get("service_type_id") or "").strip() == "civil_prosecution"

