from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

from scripts._support.template_draft_real_flow_support import (
    DEFAULT_LEGAL_OPINION_EVIDENCE_RELATIVE,
    DEFAULT_LEGAL_OPINION_FACTS,
)
from tests.lawyer_workbench._support.docx import (
    assert_docx_contains,
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow
from tests.lawyer_workbench._support.sse import assert_visible_response
from tests.lawyer_workbench._support.utils import unwrap_api_response


_DEBUG = str(os.getenv("E2E_FLOW_DEBUG", "") or "").strip().lower() in {"1", "true", "yes"}


def _debug(msg: str) -> None:
    if _DEBUG:
        print(msg, flush=True)


def _case_facts() -> str:
    return DEFAULT_LEGAL_OPINION_FACTS


def _extract_templates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = unwrap_api_response(payload)
    if isinstance(data, dict):
        templates = data.get("templates")
        if isinstance(templates, list):
            return [t for t in templates if isinstance(t, dict)]
    if isinstance(payload, dict):
        templates = payload.get("templates")
        if isinstance(templates, list):
            return [t for t in templates if isinstance(t, dict)]
    return []


def _pick_template(templates: list[dict[str, Any]]) -> dict[str, Any]:
    def _out_key(t: dict[str, Any]) -> str:
        return str(t.get("outputKey") or t.get("output_key") or "").strip()

    def _name(t: dict[str, Any]) -> str:
        return str(t.get("name") or "").strip()

    for t in templates:
        if _out_key(t) == "legal_opinion_contract_dispute":
            return t
    for t in templates:
        if _out_key(t) == "legal_opinion":
            return t
    for t in templates:
        if "法律意见书" in _name(t):
            return t
    for t in templates:
        if _out_key(t):
            return t
    raise AssertionError(f"no usable template found: {templates[:3]}")


@pytest.mark.e2e
@pytest.mark.slow
async def test_template_action_flow_generates_docx(lawyer_client):
    repo_root = Path(__file__).resolve().parents[3]
    paths = [repo_root / rel for rel in DEFAULT_LEGAL_OPINION_EVIDENCE_RELATIVE]

    uploaded_file_ids: list[str] = []
    for p in paths:
        _debug(f"[tpl-flow] upload {p.name}")
        up = await lawyer_client.upload_file(str(p), purpose="consultation")
        fid = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
        assert fid, f"upload failed: {up}"
        uploaded_file_ids.append(fid)
    _debug(f"[tpl-flow] uploaded {len(uploaded_file_ids)} files")

    _debug("[tpl-flow] create matter service_type_id=document_drafting")
    matter = await lawyer_client.create_matter(
        service_type_id="document_drafting",
        title="E2E 文书起草（智能模板入口）",
        file_ids=uploaded_file_ids,
    )
    matter_id = str(((matter.get("data") or {}) if isinstance(matter, dict) else {}).get("id") or "").strip()
    assert matter_id, matter

    _debug("[tpl-flow] create session (bind to matter_id)")
    sess = await lawyer_client.create_session(matter_id=matter_id)
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
    assert_docx_contains(text, must_include=["北京云杉科技有限公司", "上海启衡数据系统有限公司"])
    assert any(x in text for x in ["360000", "360,000", "36万元", "252000", "252,000", "25.2万元"]), text[:2000]
    citations = re.findall(r"《[^》]{2,20}》第[一二三四五六七八九十百千0-9]+条", text)
    assert len(citations) >= 2, f"missing law citations in docx: {text[:1200]}"
