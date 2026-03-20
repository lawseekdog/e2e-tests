from __future__ import annotations

from pathlib import Path

import pytest

from tests.lawyer_workbench._support.docx import (
    assert_docx_contains,
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow
from tests.lawyer_workbench._support.profile import assert_has_party, assert_service_type
from tests.lawyer_workbench._support.sse import assert_task_lifecycle, assert_visible_response
from tests.lawyer_workbench._support.utils import unwrap_api_response


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
    print("[loan-e2e] start civil_prosecution private lending flow", flush=True)
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
        print(f"[loan-e2e] uploaded {p.name} file_id={fid}", flush=True)

    sess = await lawyer_client.create_session(service_type_id="civil_prosecution", client_role="plaintiff")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess
    print(f"[loan-e2e] session created session_id={session_id}", flush=True)

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=uploaded_file_ids,
        overrides={},
    )

    # Workbench-mode: kickoff/playbook removed. Start the workflow by sending the case facts + attachments.
    first_sse = await flow.nudge(_case_facts(), attachments=uploaded_file_ids, max_loops=80)
    raw_first_events = first_sse.get("events")
    first_event_rows = raw_first_events if isinstance(raw_first_events, list) else []
    first_events = [
        str(row.get("event") or "").strip()
        for row in first_event_rows
        if isinstance(row, dict) and str(row.get("event") or "").strip()
    ]
    print(
        f"[loan-e2e] kickoff completed session_id={session_id} events={','.join(first_events[:10])}",
        flush=True,
    )
    assert_visible_response(first_sse)
    assert_task_lifecycle(first_sse)

    async def _civil_complaint_ready(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            print(f"[loan-e2e] waiting matter bind session_id={f.session_id}", flush=True)
            return False
        resp = await f.client.list_deliverables(f.matter_id, output_key="civil_complaint")
        data = unwrap_api_response(resp)
        ready = isinstance(data, dict) and bool(data.get("deliverables"))
        print(
            f"[loan-e2e] poll civil_complaint matter_id={f.matter_id} ready={ready}",
            flush=True,
        )
        return ready

    await flow.run_until(_civil_complaint_ready, max_steps=60, description="civil_complaint deliverable")
    assert flow.matter_id, "session did not bind to matter_id"
    print(f"[loan-e2e] deliverable ready matter_id={flow.matter_id}", flush=True)

    # ========== Workflow profile (what workbench shows) ==========
    prof_resp = await lawyer_client.get_workflow_profile(flow.matter_id)
    prof = unwrap_api_response(prof_resp)
    assert isinstance(prof, dict), prof_resp
    assert_service_type(prof, "civil_prosecution")
    assert_has_party(prof, role="plaintiff", name_contains="张三")
    assert_has_party(prof, role="defendant", name_contains="李四")

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
    assert_docx_has_no_template_placeholders(text)
    assert_docx_contains(text, must_include=["张三E2E01", "李四E2E01"])
    assert any(x in text for x in ["100000", "100,000", "10万元", "10万"]), text[:2000]
