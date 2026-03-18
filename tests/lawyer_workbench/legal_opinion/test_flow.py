from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from tests.lawyer_workbench._support.docx import (
    assert_docx_contains,
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, is_session_busy_sse
from tests.lawyer_workbench._support.profile import assert_service_type
from tests.lawyer_workbench._support.sse import (
    assert_has_end,
    assert_has_progress,
    assert_task_lifecycle,
)
from tests.lawyer_workbench._support.utils import unwrap_api_response


_LEGAL_OPINION_CAUSE_CODE = str(os.getenv("E2E_LEGAL_OPINION_CAUSE_CODE", "personal_injury_tort") or "personal_injury_tort").strip()
_RETRYABLE_HTTP_STATUS = {404, 409, 429, 500, 502, 503, 504}
_FLOW_MAX_ATTEMPTS = int(os.getenv("E2E_FLOW_MAX_ATTEMPTS", "3") or 3)
_FLOW_OVERRIDES = {
    # intent-route-v3 clarify cards ask profile.client_role before routing.
    # For legal-opinion flow, "applicant" avoids litigation-role ambiguity.
    "profile.client_role": "applicant",
    # evidence-gap-clarify can loop indefinitely in remote runs unless we explicitly stop asking.
    "data.evidence.evidence_gap_stop_ask": True,
    "data.evidence.evidence_gap_notes": "当前暂无新增材料，请基于现有事实与证据继续完成法律意见分析。",
    # Keep opinion references retrieval on-topic; generic debt query causes exhausted-loop retries.
    "data.search.query": "工伤认定 视同工伤 非因工死亡 宿舍猝死 劳动关系 未签劳动合同 双倍工资 社保待遇 人道主义补偿",
}


def _is_retryable_http_error(err: httpx.HTTPStatusError) -> bool:
    code = err.response.status_code if err.response is not None else None
    return code in _RETRYABLE_HTTP_STATUS


def _is_retryable_initial_sse_error(sse: dict[str, object] | None) -> bool:
    if not isinstance(sse, dict):
        return False
    events = sse.get("events") if isinstance(sse.get("events"), list) else []
    if not events:
        return False
    saw_error = False
    for row in events:
        if not isinstance(row, dict):
            continue
        if str(row.get("event") or "").strip() != "error":
            continue
        saw_error = True
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        msg = " ".join(
            [
                str(data.get("message") or ""),
                str(data.get("error") or ""),
            ]
        )
        if ("事项不存在" in msg) or ("404" in msg) or ("timeout" in msg.lower()):
            return True
    return saw_error


@pytest.mark.e2e
@pytest.mark.slow
async def test_legal_opinion_generates_opinion_doc(lawyer_client):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    bg_path = evidence_dir / "background_materials.txt"

    up = await lawyer_client.upload_file(str(bg_path), purpose="consultation")
    bg_file_id = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
    assert bg_file_id, up

    kickoff = (
        "委托人：某监理公司E2E。\n"
        "事件：员工赵丽珍非因工死亡（宿舍猝死），家属主张工伤赔偿并要求一次性补偿。\n"
        "目标：评估是否构成工伤/视同工伤，梳理公司风险与应对策略，给出证据保全与谈判建议。\n"
        "（不要起诉，只需要法律意见书）"
    )

    required_output_keys = {"legal_opinion"}

    async def _build_flow() -> WorkbenchFlow:
        sess = await lawyer_client.create_session(
            service_type_id="legal_opinion",
            client_role="applicant",
            cause_of_action_code=_LEGAL_OPINION_CAUSE_CODE,
        )
        session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
        assert session_id, sess
        matter_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("matter_id") or "").strip()
        return WorkbenchFlow(
            client=lawyer_client,
            session_id=session_id,
            uploaded_file_ids=[bg_file_id],
            overrides=dict(_FLOW_OVERRIDES),
            matter_id=matter_id or None,
        )

    flow: WorkbenchFlow | None = None
    for flow_attempt in range(1, max(1, _FLOW_MAX_ATTEMPTS) + 1):
        flow = await _build_flow()

        async def _matter_accessible() -> bool:
            await flow.refresh()
            if not flow.matter_id:
                return False
            try:
                snapshot_resp = await lawyer_client.get(
                    f"/matter-service/lawyer/matters/{flow.matter_id}/workbench/snapshot"
                )
            except httpx.HTTPStatusError as e:
                if _is_retryable_http_error(e):
                    return False
                raise
            snapshot = unwrap_api_response(snapshot_resp)
            return isinstance(snapshot, dict) and bool(snapshot)

        first_sse: dict[str, object] = {}
        for attempt in range(1, 4):
            if attempt > 1:
                flow = await _build_flow()
            first_sse = await flow.nudge(kickoff, attachments=[bg_file_id], max_loops=3)
            if _is_retryable_initial_sse_error(first_sse):
                await flow.refresh()
                await asyncio.sleep(min(4.0, 0.8 * attempt))
                continue
            if not await _matter_accessible():
                await flow.refresh()
                await asyncio.sleep(min(4.0, 0.8 * attempt))
                continue
            break

        try:
            # Remote integration may complete an initial nudge with progress/task events only.
            # Keep strict lifecycle checks, and validate deliverables/snapshot downstream.
            assert_has_end(first_sse)
            if not is_session_busy_sse(first_sse):
                assert_has_progress(first_sse)
                assert_task_lifecycle(first_sse)

            async def _opinion_ready(f: WorkbenchFlow) -> bool:
                await f.refresh()
                if not f.matter_id:
                    return False
                try:
                    resp = await f.client.list_deliverables(f.matter_id)
                    data = unwrap_api_response(resp)
                except httpx.HTTPStatusError as e:
                    if _is_retryable_http_error(e):
                        return False
                    raise
                if not isinstance(data, dict):
                    return False
                rows = data.get("deliverables") if isinstance(data.get("deliverables"), list) else []
                by_key = {
                    str(it.get("output_key") or "").strip(): it
                    for it in rows
                    if isinstance(it, dict) and str(it.get("output_key") or "").strip()
                }
                if not required_output_keys.issubset(set(by_key.keys())):
                    return False
                if not str((by_key.get("legal_opinion") or {}).get("file_id") or "").strip():
                    return False
                summary = by_key.get("phase_summary__opinion_output") or {}
                content = summary.get("content") if isinstance(summary.get("content"), dict) else {}
                md = str(content.get("markdown") or content.get("md") or content.get("content") or "").strip()
                return len(md) > 30

            await flow.run_until(_opinion_ready, max_steps=25, description="legal_opinion + phase summary deliverables")
            break
        except AssertionError:
            if flow_attempt >= max(1, _FLOW_MAX_ATTEMPTS):
                raise
            await asyncio.sleep(min(6.0, 1.2 * flow_attempt))

    assert flow is not None
    assert flow.matter_id

    prof_resp = await lawyer_client.get_workflow_profile(flow.matter_id)
    prof = unwrap_api_response(prof_resp)
    assert isinstance(prof, dict), prof_resp
    assert_service_type(prof, "legal_opinion")

    snapshot_resp = await lawyer_client.get(f"/matter-service/lawyer/matters/{flow.matter_id}/workbench/snapshot")
    snapshot = unwrap_api_response(snapshot_resp)
    assert isinstance(snapshot, dict), snapshot_resp
    analysis_state = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    opinion_view = analysis_state.get("legal_opinion_view") if isinstance(analysis_state, dict) else {}
    assert isinstance(opinion_view, dict), snapshot_resp
    assert str(opinion_view.get("summary") or "").strip(), opinion_view

    dels_resp = await lawyer_client.list_deliverables(flow.matter_id)
    dels = unwrap_api_response(dels_resp)
    items = (dels.get("deliverables") if isinstance(dels, dict) else None) or []
    by_key = {
        str(it.get("output_key") or "").strip(): it
        for it in items
        if isinstance(it, dict) and str(it.get("output_key") or "").strip()
    }
    assert required_output_keys.issubset(set(by_key.keys())), sorted(by_key.keys())
    d0 = by_key.get("legal_opinion") if isinstance(by_key.get("legal_opinion"), dict) else {}
    file_id = str((d0 or {}).get("file_id") or "").strip()
    assert file_id, d0
    docx_bytes = await lawyer_client.download_file_bytes(file_id)
    text = extract_docx_text(docx_bytes)
    assert_docx_has_no_template_placeholders(text)
    assert_docx_contains(text, must_include=["法律意见", "赵丽珍"])
