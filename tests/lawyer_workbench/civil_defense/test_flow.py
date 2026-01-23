from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.lawyer_workbench._support.db import PgTarget, count
from tests.lawyer_workbench._support.docx import assert_docx_contains, extract_docx_text
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, wait_for_initial_card
from tests.lawyer_workbench._support.knowledge import ingest_doc, wait_for_search_hit
from tests.lawyer_workbench._support.memory import entity_keys, wait_for_entity_keys
from tests.lawyer_workbench._support.profile import assert_has_party, assert_service_type
from tests.lawyer_workbench._support.sse import assert_visible_response
from tests.lawyer_workbench._support.timeline import assert_timeline_has_output_keys, unwrap_timeline
from tests.lawyer_workbench._support.utils import unwrap_api_response


_MATTER_DB = PgTarget(dbname=os.getenv("E2E_MATTER_DB", "matter-service"))


@pytest.mark.e2e
@pytest.mark.slow
async def test_civil_defense_generates_defense_statement_and_persists_state(lawyer_client):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    complaint_path = evidence_dir / "opponent_complaint.txt"

    up = await lawyer_client.upload_file(str(complaint_path), purpose="consultation")
    complaint_file_id = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
    assert complaint_file_id, up

    sess = await lawyer_client.create_session(service_type_id="civil_defense")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=[complaint_file_id],
        overrides={
            "profile.facts": (
                "我方（被告）张三E2E02，收到原告王五E2E02起诉，主张民间借贷50,000元及利息。"
                "我方认为双方存在其他往来款，借条真实性存疑；我方已部分还款。"
            )
        },
    )

    first_card = await wait_for_initial_card(flow, timeout_s=90.0)
    assert str(first_card.get("skill_id") or "").strip() == "system:kickoff", first_card
    kickoff_sse = await flow.resume_card(first_card)
    assert_visible_response(kickoff_sse)

    async def _defense_statement_ready(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            return False
        resp = await f.client.list_deliverables(f.matter_id, output_key="defense_statement")
        data = unwrap_api_response(resp)
        return isinstance(data, dict) and bool(data.get("deliverables"))

    await flow.run_until(_defense_statement_ready, max_steps=60, description="defense_statement deliverable")
    assert flow.matter_id, "session did not bind to matter_id"

    # ========== Matter persistence (DB-level) ==========
    mid_int = int(flow.matter_id)
    assert await count(_MATTER_DB, "select count(1) from matters where id = %s", [mid_int]) == 1
    assert await count(_MATTER_DB, "select count(1) from matter_tasks where matter_id = %s", [mid_int]) > 0
    assert (
        await count(
            _MATTER_DB,
            "select count(1) from matter_deliverables where matter_id = %s and output_key = %s",
            [mid_int, "defense_statement"],
        )
        == 1
    )

    # ========== Traces (skill-level audit trail) ==========
    traces_resp = await lawyer_client.list_traces(flow.matter_id, limit=200)
    traces_data = unwrap_api_response(traces_resp)
    traces = traces_data.get("traces") if isinstance(traces_data, dict) else None
    assert isinstance(traces, list) and traces, traces_resp
    node_ids = {str(it.get("node_id") or "").strip() for it in traces if isinstance(it, dict)}
    assert any(x in node_ids for x in {"skill:complaint-analysis", "complaint-analysis"})
    assert any(x in node_ids for x in {"skill:defense-planning", "defense-planning"})
    assert any(x in node_ids for x in {"skill:document-generation", "document-generation"})

    # ========== Workflow profile (what workbench shows) ==========
    prof_resp = await lawyer_client.get_workflow_profile(flow.matter_id)
    prof = unwrap_api_response(prof_resp)
    assert isinstance(prof, dict), prof_resp
    assert_service_type(prof, "civil_defense")
    assert_has_party(prof, role="plaintiff", name_contains="王五")
    assert_has_party(prof, role="defendant", name_contains="张三")

    # ========== Timeline / round_summary (UI time line) ==========
    tl_resp = await lawyer_client.get_matter_timeline(flow.matter_id, limit=50)
    tl = unwrap_timeline(tl_resp)
    assert_timeline_has_output_keys(tl, must_include=["defense_statement"])

    # ========== Deliverable content (DOCX) ==========
    dels_resp = await lawyer_client.list_deliverables(flow.matter_id, output_key="defense_statement")
    dels = unwrap_api_response(dels_resp)
    items = (dels.get("deliverables") if isinstance(dels, dict) else None) or []
    assert items, dels_resp
    d0 = items[0] if isinstance(items[0], dict) else {}
    file_id = str(d0.get("file_id") or "").strip()
    assert file_id, d0
    docx_bytes = await lawyer_client.download_file_bytes(file_id)
    text = extract_docx_text(docx_bytes)
    assert_docx_contains(text, must_include=["答辩", "王五E2E02", "张三E2E02"])

    # ========== Memory (facts extracted) ==========
    facts = await wait_for_entity_keys(
        lawyer_client,
        user_id=int(lawyer_client.user_id),
        case_id=str(flow.matter_id),
        must_include=["evidence:借条", "evidence:转账记录"],
        timeout_s=120.0,
    )
    keys = entity_keys(facts)
    assert any(k.startswith("party:plaintiff:") and "王五" in k for k in keys), sorted(list(keys))[:50]
    assert any(k.startswith("party:defendant:") and "张三" in k for k in keys), sorted(list(keys))[:50]

    # ========== Knowledge ingest/search (precision baseline) ==========
    kb_id = "e2e_kb_civil_defense"
    unique = f"E2E_UNIQUE_CIVIL_DEF_{flow.matter_id}"
    await ingest_doc(
        lawyer_client,
        kb_id=kb_id,
        file_id=complaint_file_id,
        content=f"{unique}\n（被告侧）应诉要点：借条真实性、款项性质、部分还款。",
        doc_type="case",
        metadata={"e2e": True, "service_type_id": "civil_defense", "matter_id": flow.matter_id},
        overwrite=True,
    )
    await wait_for_search_hit(
        lawyer_client,
        query=unique,
        kb_ids=[kb_id],
        must_file_id=complaint_file_id,
        timeout_s=90.0,
    )
