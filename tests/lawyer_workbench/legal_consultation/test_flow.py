from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.lawyer_workbench._support.canvas import canvas_evidence_file_ids, canvas_profile, unwrap_canvas
from tests.lawyer_workbench._support.db import PgTarget, count
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, wait_for_initial_card
from tests.lawyer_workbench._support.phase_timeline import assert_has_phases, assert_playbook_id, unwrap_phase_timeline
from tests.lawyer_workbench._support.sse import assert_task_lifecycle, assert_visible_response
from tests.lawyer_workbench._support.utils import eventually, unwrap_api_response


_MATTER_DB = PgTarget(dbname=os.getenv("E2E_MATTER_DB", "matter-service"))


def _consult_facts() -> str:
    return (
        "我叫张三E2E00，与房东李四E2E00签订租赁合同，押金2000元。退租时房东以墙面污损为由拒退押金。\n"
        "我有：租赁合同、交接清单、聊天记录。诉求：退还押金2000元并承担合理费用。"
    )


@pytest.mark.e2e
async def test_legal_consultation_can_run_and_switch_to_litigation(lawyer_client):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    note_path = evidence_dir / "consult_note.txt"

    sess = await lawyer_client.create_session(service_type_id="legal_consultation")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    # Bind attachment to session so consultation canvas can render evidence_list.
    up = await lawyer_client.upload_session_attachment(session_id, str(note_path))
    up_data = unwrap_api_response(up)
    note_file_id = str(((up_data or {}) if isinstance(up_data, dict) else {}).get("file_id") or "").strip()
    assert note_file_id, up

    flow = WorkbenchFlow(client=lawyer_client, session_id=session_id, uploaded_file_ids=[note_file_id])

    # Kickoff should always be the first interrupt card in the "start_service" flow.
    flow.overrides["profile.facts"] = _consult_facts()
    first_card = await wait_for_initial_card(flow, timeout_s=90.0)
    assert str(first_card.get("skill_id") or "").strip() == "system:kickoff", first_card
    qs = first_card.get("questions") if isinstance(first_card.get("questions"), list) else []
    fks = {str(q.get("field_key") or "").strip() for q in qs if isinstance(q, dict)}
    assert "profile.facts" in fks, first_card

    kickoff_sse = await flow.resume_card(first_card)
    assert_visible_response(kickoff_sse)
    assert_task_lifecycle(kickoff_sse)

    async def _canvas_ready(expected_service_type: str) -> dict | None:
        resp = await lawyer_client.get_session_canvas(session_id)
        canvas = unwrap_canvas(resp)
        prof = canvas_profile(canvas)
        if str(prof.get("service_type_id") or "").strip() != expected_service_type:
            return None
        if note_file_id not in canvas_evidence_file_ids(canvas):
            return None
        # timeline comes from round_summary; allow a bit of time for async persistence.
        tl = canvas.get("timeline")
        if not isinstance(tl, list) or not tl:
            return None
        return canvas

    await eventually(
        lambda: _canvas_ready("legal_consultation"),
        timeout_s=90.0,
        interval_s=2.0,
        description="session canvas (consultation) populated",
    )

    # Ensure workbench can show task progress UI (task_start/task_end).
    sse2 = await flow.step(nudge_text="继续")
    assert_visible_response(sse2)
    assert_task_lifecycle(sse2)

    async def _consult_intake_executed(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            return False
        resp = await lawyer_client.list_traces(f.matter_id, limit=200)
        data = unwrap_api_response(resp)
        traces = data.get("traces") if isinstance(data, dict) else None
        if not isinstance(traces, list) or not traces:
            return False
        node_ids = {str(it.get("node_id") or "").strip() for it in traces if isinstance(it, dict)}
        return ("skill:consult-intake" in node_ids) or ("consult-intake" in node_ids)

    await flow.run_until(_consult_intake_executed, max_steps=20, description="consult-intake trace")
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

    # Session canvas should reflect the updated matter profile (service_type_id/playbook_id).
    await eventually(
        lambda: _canvas_ready("civil_prosecution"),
        timeout_s=90.0,
        interval_s=2.0,
        description="session canvas (after switch_service_type) populated",
    )

    # Matter phase timeline should now follow the litigation playbook.
    pt_resp = await lawyer_client.get_matter_phase_timeline(flow.matter_id)
    pt = unwrap_phase_timeline(pt_resp)
    assert_playbook_id(pt, "litigation_civil_prosecution")
    assert_has_phases(pt, must_include=["kickoff", "intake", "execute"])
