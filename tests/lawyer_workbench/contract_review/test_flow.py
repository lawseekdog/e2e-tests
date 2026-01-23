from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.lawyer_workbench._support.db import PgTarget, count
from tests.lawyer_workbench._support.docx import assert_docx_contains, extract_docx_text
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, wait_for_initial_card
from tests.lawyer_workbench._support.knowledge import ingest_doc, wait_for_search_hit
from tests.lawyer_workbench._support.memory import list_case_facts
from tests.lawyer_workbench._support.profile import assert_service_type
from tests.lawyer_workbench._support.sse import assert_visible_response
from tests.lawyer_workbench._support.timeline import produced_output_keys, unwrap_timeline
from tests.lawyer_workbench._support.utils import eventually, unwrap_api_response


_MATTER_DB = PgTarget(dbname=os.getenv("E2E_MATTER_DB", "matter-service"))


@pytest.mark.e2e
@pytest.mark.slow
async def test_contract_review_generates_review_report(lawyer_client):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    contract_path = evidence_dir / "sample_contract.txt"

    up = await lawyer_client.upload_file(str(contract_path), purpose="consultation")
    contract_file_id = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
    assert contract_file_id, up

    sess = await lawyer_client.create_session(service_type_id="contract_review")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=[contract_file_id],
        overrides={
            "profile.facts": (
                "请审查一份采购合同：甲方北京甲方科技有限公司，乙方上海乙方供应链有限公司。"
                "重点关注：违约金是否过高、争议解决条款、免责声明条款。"
            ),
            "profile.review_focus": "违约金、争议解决、免责声明、付款与交付风险",
        },
    )

    first_card = await wait_for_initial_card(flow, timeout_s=90.0)
    assert str(first_card.get("skill_id") or "").strip() == "system:kickoff", first_card
    kickoff_sse = await flow.resume_card(first_card)
    assert_visible_response(kickoff_sse)

    async def _any_contract_doc_ready(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            return False
        resp = await f.client.list_deliverables(f.matter_id)
        data = unwrap_api_response(resp)
        if not isinstance(data, dict):
            return False
        items = data.get("deliverables") if isinstance(data.get("deliverables"), list) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            ok = str(it.get("output_key") or "").strip()
            if ok in {"contract_review_report", "modification_suggestion"}:
                return True
        return False

    await flow.run_until(_any_contract_doc_ready, max_steps=70, description="contract review document deliverable")
    assert flow.matter_id

    mid_int = int(flow.matter_id)
    assert await count(_MATTER_DB, "select count(1) from matters where id = %s", [mid_int]) == 1
    assert await count(_MATTER_DB, "select count(1) from matter_tasks where matter_id = %s", [mid_int]) > 0

    traces_resp = await lawyer_client.list_traces(flow.matter_id, limit=200)
    traces_data = unwrap_api_response(traces_resp)
    traces = traces_data.get("traces") if isinstance(traces_data, dict) else None
    assert isinstance(traces, list) and traces, traces_resp
    node_ids = {str(it.get("node_id") or "").strip() for it in traces if isinstance(it, dict)}
    assert any(x in node_ids for x in {"skill:contract-intake", "contract-intake"})
    assert any(x in node_ids for x in {"skill:contract-review", "contract-review"})
    assert any(x in node_ids for x in {"skill:document-generation", "document-generation"})

    prof_resp = await lawyer_client.get_workflow_profile(flow.matter_id)
    prof = unwrap_api_response(prof_resp)
    assert isinstance(prof, dict), prof_resp
    assert_service_type(prof, "contract_review")

    tl_resp = await lawyer_client.get_matter_timeline(flow.matter_id, limit=50)
    tl = unwrap_timeline(tl_resp)
    have = produced_output_keys(tl)
    assert have.intersection({"contract_review_report", "modification_suggestion"}), sorted(have)

    dels_resp = await lawyer_client.list_deliverables(flow.matter_id)
    dels = unwrap_api_response(dels_resp)
    items = (dels.get("deliverables") if isinstance(dels, dict) else None) or []
    picked = None
    for it in items:
        if not isinstance(it, dict):
            continue
        if str(it.get("output_key") or "").strip() == "contract_review_report":
            picked = it
            break
    if picked is None:
        for it in items:
            if isinstance(it, dict) and str(it.get("output_key") or "").strip() == "modification_suggestion":
                picked = it
                break
    assert isinstance(picked, dict), dels_resp

    file_id = str(picked.get("file_id") or "").strip()
    assert file_id, picked
    docx_bytes = await lawyer_client.download_file_bytes(file_id)
    text = extract_docx_text(docx_bytes)
    assert_docx_contains(text, must_include=["合同", "甲方", "乙方"])

    async def _memory_has_contract_party() -> list[dict] | None:
        facts = await list_case_facts(
            lawyer_client,
            user_id=int(lawyer_client.user_id),
            case_id=str(flow.matter_id),
            limit=300,
        )
        for it in facts:
            if not isinstance(it, dict):
                continue
            s = f"{it.get('entity_key') or ''} {it.get('content') or ''}"
            if "甲方科技" in s or "乙方供应链" in s:
                return facts
        return None

    assert await eventually(
        _memory_has_contract_party,
        timeout_s=120.0,
        interval_s=2.0,
        description="memory contains contract parties",
    )

    kb_id = "e2e_kb_contract_review"
    unique = f"E2E_UNIQUE_CONTRACT_{flow.matter_id}"
    await ingest_doc(
        lawyer_client,
        kb_id=kb_id,
        file_id=contract_file_id,
        content=f"{unique}\n合同审查关注点：违约金过高、免责声明、仲裁地条款。",
        doc_type="contract",
        metadata={"e2e": True, "service_type_id": "contract_review", "matter_id": flow.matter_id},
        overwrite=True,
    )
    await wait_for_search_hit(lawyer_client, query=unique, kb_ids=[kb_id], must_file_id=contract_file_id, timeout_s=90.0)
