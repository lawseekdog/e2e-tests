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
from tests.lawyer_workbench._support.knowledge import ingest_doc, wait_for_search_hit
from tests.lawyer_workbench._support.memory import assert_any_fact_content_contains, wait_for_memory_facts
from tests.lawyer_workbench._support.phase_timeline import (
    assert_has_deliverable,
    assert_has_phases,
    assert_phase_status_in,
    unwrap_phase_timeline,
)
from tests.lawyer_workbench._support.profile import assert_has_party, assert_service_type
from tests.lawyer_workbench._support.sse import assert_task_lifecycle, assert_visible_response
from tests.lawyer_workbench._support.timeline import assert_timeline_has_output_keys, unwrap_timeline
from tests.lawyer_workbench._support.utils import unwrap_api_response


_MATTER_DB = PgTarget(dbname=os.getenv("E2E_MATTER_DB", "matter-service"))


@pytest.mark.e2e
@pytest.mark.slow
async def test_civil_appeal_appellee_generates_appeal_defense(lawyer_client):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    judgment_path = evidence_dir / "first_instance_judgment.txt"
    appeal_brief_path = evidence_dir / "opponent_appeal_brief.txt"

    up1 = await lawyer_client.upload_file(str(judgment_path), purpose="consultation")
    judgment_file_id = str(((up1.get("data") or {}) if isinstance(up1, dict) else {}).get("id") or "").strip()
    assert judgment_file_id, up1

    up2 = await lawyer_client.upload_file(str(appeal_brief_path), purpose="consultation")
    appeal_file_id = str(((up2.get("data") or {}) if isinstance(up2, dict) else {}).get("id") or "").strip()
    assert appeal_file_id, up2

    sess = await lawyer_client.create_session(service_type_id="civil_appeal_appellee", client_role="appellee")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=[judgment_file_id, appeal_file_id],
        overrides={},
    )

    first_sse = await flow.nudge(
        "我方（被上诉人）张三E2E03，收到对方（上诉人）李四E2E03上诉。"
        "证据：一审判决书、借条、转账记录、上诉状。"
        "请分析上诉理由并生成上诉答辩状。",
        attachments=[judgment_file_id, appeal_file_id],
        max_loops=80,
    )
    assert_visible_response(first_sse)
    assert_task_lifecycle(first_sse)

    async def _appeal_defense_ready(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            return False
        resp = await f.client.list_deliverables(f.matter_id, output_key="appeal_defense")
        data = unwrap_api_response(resp)
        return isinstance(data, dict) and bool(data.get("deliverables"))

    await flow.run_until(_appeal_defense_ready, max_steps=70, description="appeal_defense deliverable")
    assert flow.matter_id

    mid_int = int(flow.matter_id)
    assert await count(_MATTER_DB, "select count(1) from matters where id = %s", [mid_int]) == 1
    assert (
        await count(
            _MATTER_DB,
            "select count(1) from matter_deliverables where matter_id = %s and output_key = %s",
            [mid_int, "appeal_defense"],
        )
        == 1
    )

    traces_resp = await lawyer_client.list_traces(flow.matter_id, limit=200)
    traces_data = unwrap_api_response(traces_resp)
    traces = traces_data.get("traces") if isinstance(traces_data, dict) else None
    assert isinstance(traces, list) and traces, traces_resp
    node_ids = {str(it.get("node_id") or "").strip() for it in traces if isinstance(it, dict)}
    assert any(x in node_ids for x in {"skill:appeal-intake", "appeal-intake"})
    assert any(x in node_ids for x in {"skill:defense-planning", "defense-planning"})
    assert any(x in node_ids for x in {"skill:document-generation", "document-generation"})

    prof_resp = await lawyer_client.get_workflow_profile(flow.matter_id)
    prof = unwrap_api_response(prof_resp)
    assert isinstance(prof, dict), prof_resp
    assert_service_type(prof, "civil_appeal_appellee")
    assert_has_party(prof, role="plaintiff", name_contains="张三")
    assert_has_party(prof, role="defendant", name_contains="李四")

    pt_resp = await lawyer_client.get_matter_phase_timeline(flow.matter_id)
    pt = unwrap_phase_timeline(pt_resp)
    assert_has_phases(pt, must_include=["materials", "intake", "evidence", "strategy", "output", "docgen"])
    assert_phase_status_in(pt, phase_id="materials", allowed=["completed", "in_progress"])
    assert_has_deliverable(pt, output_key="appeal_defense")

    tl_resp = await lawyer_client.get_matter_timeline(flow.matter_id, limit=50)
    tl = unwrap_timeline(tl_resp)
    assert_timeline_has_output_keys(tl, must_include=["appeal_defense"])

    dels_resp = await lawyer_client.list_deliverables(flow.matter_id, output_key="appeal_defense")
    dels = unwrap_api_response(dels_resp)
    items = (dels.get("deliverables") if isinstance(dels, dict) else None) or []
    assert items, dels_resp
    d0 = items[0] if isinstance(items[0], dict) else {}
    file_id = str(d0.get("file_id") or "").strip()
    assert file_id, d0
    docx_bytes = await lawyer_client.download_file_bytes(file_id)
    text = extract_docx_text(docx_bytes)
    assert_docx_has_no_template_placeholders(text)
    assert_docx_contains(text, must_include=["答辩", "张三E2E03", "李四E2E03"])

    facts = await wait_for_memory_facts(
        lawyer_client,
        user_id=int(lawyer_client.user_id),
        case_id=str(flow.matter_id),
        must_include_content=["张三E2E03", "李四E2E03", "借条", "转账记录"],
        timeout_s=120.0,
    )
    assert_any_fact_content_contains(
        facts,
        candidate_entity_keys=["party:appellee:primary", "party:defendant:primary"],
        must_include=["张三E2E03"],
    )
    assert_any_fact_content_contains(
        facts,
        candidate_entity_keys=["party:appellant:primary", "party:plaintiff:primary"],
        must_include=["李四E2E03"],
    )

    kb_id = "e2e_kb_civil_appeal_appellee"
    unique = f"E2E_UNIQUE_APPEAL_APPELLEE_{flow.matter_id}"
    await ingest_doc(
        lawyer_client,
        kb_id=kb_id,
        file_id=appeal_file_id,
        content=f"{unique}\n二审答辩要点：围绕上诉理由逐点反驳，巩固一审有利认定。",
        doc_type="case",
        metadata={"e2e": True, "service_type_id": "civil_appeal_appellee", "matter_id": flow.matter_id},
        overwrite=True,
    )
    await wait_for_search_hit(lawyer_client, query=unique, kb_ids=[kb_id], must_file_id=appeal_file_id, timeout_s=90.0)
