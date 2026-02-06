from __future__ import annotations

import os
import re
from pathlib import Path

import httpx
import pytest

from tests.lawyer_workbench._support.docx import (
    assert_docx_contains,
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow
from tests.lawyer_workbench._support.sse import assert_visible_response
from tests.lawyer_workbench._support.utils import eventually, unwrap_api_response


_DEBUG = str(os.getenv("E2E_FLOW_DEBUG", "") or "").strip().lower() in {"1", "true", "yes"}


def _debug(msg: str) -> None:
    if _DEBUG:
        print(msg, flush=True)


def _case_facts() -> str:
    return (
        "原告：张三E2E_TPL。\n"
        "被告：李四E2E_TPL。\n"
        "案由：民间借贷纠纷。\n"
        "事实：2023-03-01，被告向原告借款人民币80000元，约定2023-10-01前归还；"
        "原告已通过银行转账交付。\n"
        "到期后被告未还，原告多次催收无果。\n"
        "证据：借条、转账记录、聊天记录。\n"
        "诉求：返还本金80000元，并按年利率6%支付逾期利息，承担诉讼费。"
    )


def _extract_templates(payload: dict) -> list[dict]:
    data = unwrap_api_response(payload)
    if isinstance(data, dict) and isinstance(data.get("templates"), list):
        return [t for t in data.get("templates") if isinstance(t, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("templates"), list):
        return [t for t in payload.get("templates") if isinstance(t, dict)]
    return []


def _pick_template(templates: list[dict]) -> dict:
    def _out_key(t: dict) -> str:
        return str(t.get("outputKey") or t.get("output_key") or "").strip()

    def _name(t: dict) -> str:
        return str(t.get("name") or "").strip()

    for t in templates:
        if _out_key(t) == "civil_complaint":
            return t
    for t in templates:
        if "起诉状" in _name(t):
            return t
    for t in templates:
        if _out_key(t):
            return t
    raise AssertionError(f"no usable template found: {templates[:3]}")


def _has_agentic_search(traces: list[dict]) -> bool:
    for t in traces or []:
        if not isinstance(t, dict):
            continue
        for c in t.get("tool_calls") or []:
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip().lower()
            if "agentic_search" in name or "agentic-search" in name:
                return True
    return False


@pytest.mark.e2e
@pytest.mark.slow
async def test_template_action_flow_generates_docx(lawyer_client):
    evidence_dir = Path(__file__).resolve().parents[1] / "civil_prosecution" / "evidence"
    paths = [
        evidence_dir / "iou.txt",
        evidence_dir / "sample_transfer_record.txt",
        evidence_dir / "sample_chat_record.txt",
    ]

    uploaded_file_ids: list[str] = []
    for p in paths:
        _debug(f"[tpl-flow] upload {p.name}")
        up = await lawyer_client.upload_file(str(p), purpose="consultation")
        fid = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
        assert fid, f"upload failed: {up}"
        uploaded_file_ids.append(fid)
    _debug(f"[tpl-flow] uploaded {len(uploaded_file_ids)} files")

    _debug("[tpl-flow] create session service_type_id=document_drafting")
    sess = await lawyer_client.create_session(service_type_id="document_drafting")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess
    _debug(f"[tpl-flow] session_id={session_id}")

    _debug("[tpl-flow] list atomic templates")
    templates_payload = await lawyer_client.get("/templates-service/atomic/templates")
    templates = _extract_templates(templates_payload)
    assert templates, templates_payload
    tpl = _pick_template(templates)

    template_id = str(tpl.get("id") or "").strip()
    assert template_id, tpl
    title = str(tpl.get("name") or "").strip() or f"模板#{template_id}"
    output_key = f"template:{template_id}"
    _debug(f"[tpl-flow] picked template_id={template_id} output_key={output_key} title={title}")

    _debug("[tpl-flow] send workflow_action=template_draft_start")
    await lawyer_client.workflow_action(
        session_id,
        workflow_action="template_draft_start",
        workflow_action_params={
            "template_id": template_id,
            "deliverable_title": title,
            "output_key": output_key,
        },
    )
    _debug("[tpl-flow] workflow_action ok")

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=uploaded_file_ids,
        overrides={},
    )

    # Keep loop budget conservative to avoid hitting LangGraph recursion limits on long docgen pipelines.
    first_sse = await flow.nudge(_case_facts(), attachments=uploaded_file_ids, max_loops=12)
    assert_visible_response(first_sse)
    _debug("[tpl-flow] first nudge ok")

    async def _deliverable_ready(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            return False
        resp = await f.client.list_deliverables(f.matter_id, output_key=output_key)
        data = unwrap_api_response(resp)
        items = (data.get("deliverables") if isinstance(data, dict) else None) or []
        if not items:
            return False
        d0 = items[0] if isinstance(items[0], dict) else {}
        return bool(str(d0.get("file_id") or "").strip())

    # Drive the workflow until the document is generated.
    max_steps = 140
    for _ in range(max_steps):
        await flow.refresh()
        if await _deliverable_ready(flow):
            break
        card = await flow.get_pending_card()
        if card:
            skill_id = str(card.get("skill_id") or "").strip()
            if skill_id == "document-generation":
                break
            await flow.resume_card(card)
            continue
        await flow.nudge("继续", attachments=[], max_loops=12)
    else:
        raise AssertionError("document deliverable not ready after workflow steps")

    assert await _deliverable_ready(flow), "deliverable still missing after workflow steps"

    # Download docx and run quality checks (placeholders + key facts + citations).
    deliverables_resp = unwrap_api_response(await lawyer_client.list_deliverables(flow.matter_id, output_key=output_key))
    deliverables = deliverables_resp.get("deliverables") if isinstance(deliverables_resp, dict) else None
    assert isinstance(deliverables, list) and deliverables, deliverables_resp
    d0 = deliverables[0] if isinstance(deliverables[0], dict) else {}
    file_id = str(d0.get("file_id") or "").strip()
    assert file_id, d0
    docx_bytes = await lawyer_client.download_file_bytes(file_id)
    text = extract_docx_text(docx_bytes)
    assert_docx_has_no_template_placeholders(text)
    assert_docx_contains(text, must_include=["张三E2E_TPL", "李四E2E_TPL"])
    assert any(x in text for x in ["80000", "80,000", "8万元", "8万"]), text[:2000]
    citations = re.findall(r"《[^》]{2,20}》第[一二三四五六七八九十百千0-9]+条", text)
    assert len(citations) >= 2, f"missing law citations in docx: {text[:1200]}"

    # Trace should include agentic_search tool calls.
    try:
        traces_resp = await lawyer_client.list_traces(flow.matter_id, limit=200)
        traces_data = unwrap_api_response(traces_resp)
        traces = traces_data.get("traces") if isinstance(traces_data, dict) else None
        assert isinstance(traces, list) and traces, traces_resp
        assert _has_agentic_search(traces), "missing agentic_search tool call in traces"
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            # Remote gateway may not expose traces; keep the flow test green and log for manual check.
            print("WARN: traces endpoint not available; skipped agentic_search assertion")
        else:
            raise

    # Confirm archive via chat text ("确认") and ensure status becomes archived.
    pending = await flow.get_pending_card()
    if pending and str(pending.get("skill_id") or "").strip() == "document-generation":
        await lawyer_client.chat(session_id, "确认")

    async def _archived() -> bool:
        resp = await lawyer_client.list_deliverables(flow.matter_id, output_key=output_key)
        data = unwrap_api_response(resp)
        items = (data.get("deliverables") if isinstance(data, dict) else None) or []
        if not items:
            return False
        d1 = items[0] if isinstance(items[0], dict) else {}
        return str(d1.get("status") or "").strip().lower() == "archived"

    await eventually(_archived, timeout_s=120, interval_s=3, description="deliverable archived after confirm")
