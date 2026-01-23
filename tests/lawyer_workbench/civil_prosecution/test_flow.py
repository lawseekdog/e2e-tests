from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.lawyer_workbench._support.db import PgTarget, count
from tests.lawyer_workbench._support.docx import assert_docx_contains, extract_docx_text
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, wait_for_initial_card
from tests.lawyer_workbench._support.knowledge import ingest_doc, wait_for_search_hit
from tests.lawyer_workbench._support.memory import entity_keys, wait_for_entity_keys
from tests.lawyer_workbench._support.utils import unwrap_api_response


_MATTER_DB = PgTarget(dbname=os.getenv("E2E_MATTER_DB", "matter-service"))


def _case_facts() -> str:
    return (
        "原告：张三E2E01。\n"
        "被告：李四E2E01。\n"
        "案由：民间借贷纠纷。\n"
        "事实：2023-01-01，被告向原告借款人民币100000元，约定2023-12-31前归还；原告已通过银行转账交付。\n"
        "到期后被告未还，原告多次催收无果。\n"
        "证据：借条、转账记录、聊天记录。\n"
        "诉求：返还本金100000元，并按年利率6%支付逾期利息，承担诉讼费。"
    )


@pytest.mark.e2e
@pytest.mark.slow
async def test_civil_prosecution_private_lending_generates_civil_complaint_and_persists_state(lawyer_client):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    paths = [
        evidence_dir / "iou.txt",
        evidence_dir / "sample_transfer_record.txt",
        evidence_dir / "sample_chat_record.txt",
    ]

    uploaded_file_ids: list[str] = []
    for p in paths:
        up = await lawyer_client.upload_file(str(p), purpose="consultation")
        fid = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
        assert fid, f"upload failed: {up}"
        uploaded_file_ids.append(fid)

    sess = await lawyer_client.create_session(service_type_id="civil_prosecution")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=uploaded_file_ids,
        overrides={
            "profile.facts": _case_facts(),
            "profile.claims": "返还本金100000元，并按年利率6%支付逾期利息，承担诉讼费。",
        },
    )

    # Kickoff should generate an interrupt card quickly in a healthy system.
    first_card = await wait_for_initial_card(flow, timeout_s=90.0)
    assert str(first_card.get("skill_id") or "").strip(), first_card
    await flow.resume_card(first_card)

    async def _civil_complaint_ready(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            return False
        resp = await f.client.list_deliverables(f.matter_id, output_key="civil_complaint")
        data = unwrap_api_response(resp)
        return isinstance(data, dict) and bool(data.get("deliverables"))

    await flow.run_until(_civil_complaint_ready, max_steps=60, description="civil_complaint deliverable")
    assert flow.matter_id, "session did not bind to matter_id"

    # ========== Matter persistence (DB-level) ==========
    mid_int = int(flow.matter_id)
    assert await count(_MATTER_DB, "select count(1) from matters where id = %s", [mid_int]) == 1
    assert await count(_MATTER_DB, "select count(1) from matter_tasks where matter_id = %s", [mid_int]) > 0
    assert await count(_MATTER_DB, "select count(1) from matter_phase_progress where matter_id = %s", [mid_int]) > 0
    assert (
        await count(
            _MATTER_DB,
            "select count(1) from matter_deliverables where matter_id = %s and output_key = %s",
            [mid_int, "civil_complaint"],
        )
        == 1
    )

    # ========== Traces (skill-level audit trail) ==========
    traces_resp = await lawyer_client.list_traces(flow.matter_id, limit=200)
    traces_data = unwrap_api_response(traces_resp)
    traces = traces_data.get("traces") if isinstance(traces_data, dict) else None
    assert isinstance(traces, list) and traces, traces_resp
    node_ids = {str(it.get("node_id") or "").strip() for it in traces if isinstance(it, dict)}
    # Core happy-path nodes (playbook-dependent, but stable across envs).
    assert any(x in node_ids for x in {"skill:litigation-intake", "litigation-intake"})
    assert any(x in node_ids for x in {"skill:cause-recommendation", "cause-recommendation"})
    assert any(x in node_ids for x in {"skill:document-generation", "document-generation"})

    # ========== Deliverable content (DOCX) ==========
    dels_resp = await lawyer_client.list_deliverables(flow.matter_id, output_key="civil_complaint")
    dels = unwrap_api_response(dels_resp)
    items = (dels.get("deliverables") if isinstance(dels, dict) else None) or []
    assert items, dels_resp
    d0 = items[0] if isinstance(items[0], dict) else {}
    file_id = str(d0.get("file_id") or "").strip()
    assert file_id, d0
    docx_bytes = await lawyer_client.download_file_bytes(file_id)
    text = extract_docx_text(docx_bytes)
    assert_docx_contains(text, must_include=["张三E2E01", "李四E2E01"])
    assert any(x in text for x in ["100000", "100,000", "10万元", "10万"]), text[:2000]

    # ========== Memory (facts extracted) ==========
    facts = await wait_for_entity_keys(
        lawyer_client,
        user_id=int(lawyer_client.user_id),
        case_id=str(flow.matter_id),
        must_include=["evidence:借条", "evidence:转账记录"],
        timeout_s=120.0,
    )
    keys = entity_keys(facts)
    assert any(k.startswith("party:plaintiff:") and "张三" in k for k in keys), sorted(list(keys))[:50]
    assert any(k.startswith("party:defendant:") and "李四" in k for k in keys), sorted(list(keys))[:50]

    # ========== Knowledge ingest/search (precision baseline, no mocks) ==========
    kb_id = "e2e_kb_civil_prosecution"
    unique = f"E2E_UNIQUE_CIVIL_PROS_{flow.matter_id}"
    await ingest_doc(
        lawyer_client,
        kb_id=kb_id,
        file_id=uploaded_file_ids[0],
        content=f"{unique}\n{_case_facts()}",
        doc_type="case",
        metadata={"e2e": True, "service_type_id": "civil_prosecution", "matter_id": flow.matter_id},
        overwrite=True,
    )
    await wait_for_search_hit(
        lawyer_client,
        query=unique,
        kb_ids=[kb_id],
        must_file_id=uploaded_file_ids[0],
        timeout_s=90.0,
    )
