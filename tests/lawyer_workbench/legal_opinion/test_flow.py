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
from tests.lawyer_workbench._support.memory import list_case_facts
from tests.lawyer_workbench._support.phase_timeline import (
    assert_has_deliverable,
    assert_has_phases,
    assert_phase_status_in,
    unwrap_phase_timeline,
)
from tests.lawyer_workbench._support.profile import assert_service_type
from tests.lawyer_workbench._support.sse import assert_task_lifecycle, assert_visible_response
from tests.lawyer_workbench._support.timeline import assert_timeline_has_output_keys, memory_extraction_events, round_contents, unwrap_timeline
from tests.lawyer_workbench._support.traces import extract_context_manifest, find_latest_trace
from tests.lawyer_workbench._support.utils import eventually, unwrap_api_response


_MATTER_DB = PgTarget(dbname=os.getenv("E2E_MATTER_DB", "matter-service"))


@pytest.mark.e2e
@pytest.mark.slow
async def test_legal_opinion_generates_opinion_doc(lawyer_client):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    bg_path = evidence_dir / "background_materials.txt"

    up = await lawyer_client.upload_file(str(bg_path), purpose="consultation")
    bg_file_id = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
    assert bg_file_id, up

    sess = await lawyer_client.create_session(service_type_id="workbench")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=[bg_file_id],
        overrides={},
    )

    first_sse = await flow.nudge(
        "委托人：某监理公司E2E。\n"
        "事件：员工赵丽珍非因工死亡（宿舍猝死），家属主张工伤赔偿并要求一次性补偿。\n"
        "目标：评估是否构成工伤/视同工伤，梳理公司风险与应对策略，给出证据保全与谈判建议。\n"
        "（不要起诉，只需要法律意见书）",
        attachments=[bg_file_id],
        max_loops=80,
    )
    assert_visible_response(first_sse)
    assert_task_lifecycle(first_sse)

    async def _opinion_ready(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            return False
        resp = await f.client.list_deliverables(f.matter_id, output_key="legal_opinion")
        data = unwrap_api_response(resp)
        return isinstance(data, dict) and bool(data.get("deliverables"))

    await flow.run_until(_opinion_ready, max_steps=70, description="legal_opinion deliverable")
    assert flow.matter_id

    mid_int = int(flow.matter_id)
    assert await count(_MATTER_DB, "select count(1) from matters where id = %s", [mid_int]) == 1
    assert (
        await count(
            _MATTER_DB,
            "select count(1) from matter_deliverables where matter_id = %s and output_key = %s",
            [mid_int, "legal_opinion"],
        )
        == 1
    )

    traces_resp = await lawyer_client.list_traces(flow.matter_id, limit=200)
    traces_data = unwrap_api_response(traces_resp)
    traces = traces_data.get("traces") if isinstance(traces_data, dict) else None
    assert isinstance(traces, list) and traces, traces_resp
    node_ids = {str(it.get("node_id") or "").strip() for it in traces if isinstance(it, dict)}
    assert any(x in node_ids for x in {"skill:legal-opinion-intake", "legal-opinion-intake"})
    assert any(x in node_ids for x in {"skill:legal-opinion-analysis", "legal-opinion-analysis"})
    assert any(x in node_ids for x in {"skill:document-generation", "document-generation"})

    # Context manifest observability: document-generation should see some recalled memory facts.
    doc_gen_trace = find_latest_trace(traces, node_id="skill:document-generation") or find_latest_trace(traces, node_id="document-generation")
    assert isinstance(doc_gen_trace, dict), traces_resp
    manifest = extract_context_manifest(doc_gen_trace) or {}
    mem = manifest.get("memory") if isinstance(manifest.get("memory"), dict) else {}
    assert int(mem.get("limit") or 0) > 0, manifest
    assert str(mem.get("source") or "") in {"facts", "context_text", "none"}, manifest
    assert int(mem.get("selected_count") or 0) > 0, manifest

    # Memory recall should rewrite short nudges ("继续") into an anchor query for better recall.
    saw_anchor = False
    for t in traces:
        if not isinstance(t, dict) or str(t.get("node_id") or "").strip() != "memory":
            continue
        for c in (t.get("tool_calls") or []):
            if not isinstance(c, dict) or str(c.get("name") or "").strip() != "memory_recall":
                continue
            args = c.get("args") if isinstance(c.get("args"), dict) else {}
            if str(args.get("original_query") or "").strip() != "继续":
                continue
            q = str(args.get("query") or "").strip()
            if q and q != "继续" and len(q) >= 12:
                saw_anchor = True
                break
        if saw_anchor:
            break
    assert saw_anchor, "missing memory_recall with original_query='继续' (anchor recall optimization)"

    prof_resp = await lawyer_client.get_workflow_profile(flow.matter_id)
    prof = unwrap_api_response(prof_resp)
    assert isinstance(prof, dict), prof_resp
    assert_service_type(prof, "legal_opinion")

    pt_resp = await lawyer_client.get_matter_phase_timeline(flow.matter_id)
    pt = unwrap_phase_timeline(pt_resp)
    assert_has_phases(pt, must_include=["materials", "intake", "analyze", "output", "docgen"])
    assert_phase_status_in(pt, phase_id="materials", allowed=["completed", "in_progress"])
    assert_has_deliverable(pt, output_key="legal_opinion")

    tl_resp = await lawyer_client.get_matter_timeline(flow.matter_id, limit=50)
    tl = unwrap_timeline(tl_resp)
    assert_timeline_has_output_keys(tl, must_include=["legal_opinion"])
    # Per-round memory observability contract: round_summary includes memory_traces (recall/extraction).
    contents = round_contents(tl)
    assert contents, tl_resp
    for c in contents:
        mt = c.get("memory_traces")
        assert isinstance(mt, dict) and ("recall" in mt) and ("extraction" in mt), mt
    assert any(int(e.get("extracted_count") or 0) > 0 for e in memory_extraction_events(tl)), tl

    dels_resp = await lawyer_client.list_deliverables(flow.matter_id, output_key="legal_opinion")
    dels = unwrap_api_response(dels_resp)
    items = (dels.get("deliverables") if isinstance(dels, dict) else None) or []
    assert items, dels_resp
    d0 = items[0] if isinstance(items[0], dict) else {}
    file_id = str(d0.get("file_id") or "").strip()
    assert file_id, d0
    docx_bytes = await lawyer_client.download_file_bytes(file_id)
    text = extract_docx_text(docx_bytes)
    assert_docx_has_no_template_placeholders(text)
    assert_docx_contains(text, must_include=["法律意见", "赵丽珍"])

    async def _memory_has_zhao() -> list[dict] | None:
        facts = await list_case_facts(
            lawyer_client,
            user_id=int(lawyer_client.user_id),
            case_id=str(flow.matter_id),
            limit=300,
        )
        for it in facts:
            if not isinstance(it, dict):
                continue
            if "赵丽珍" in str(it.get("entity_key") or "") or "赵丽珍" in str(it.get("content") or ""):
                return facts
        return None

    facts = await eventually(_memory_has_zhao, timeout_s=120.0, interval_s=2.0, description="memory contains 赵丽珍")
    assert facts

    kb_id = "e2e_kb_legal_opinion"
    unique = f"E2E_UNIQUE_LEGAL_OPINION_{flow.matter_id}"
    await ingest_doc(
        lawyer_client,
        kb_id=kb_id,
        file_id=bg_file_id,
        content=f"{unique}\n法律意见要点：工伤认定条件、证据要点、风险处置建议。",
        doc_type="memo",
        metadata={"e2e": True, "service_type_id": "legal_opinion", "matter_id": flow.matter_id},
        overwrite=True,
    )
    await wait_for_search_hit(lawyer_client, query=unique, kb_ids=[kb_id], must_file_id=bg_file_id, timeout_s=90.0)
