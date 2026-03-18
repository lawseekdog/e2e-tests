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


_WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
_REAL_CASE_CONTRACT_DOCX = _WORKSPACE_ROOT / "已征收闲置土地垃圾清运.docx"
_RETRYABLE_HTTP_STATUS = {404, 409, 429, 500, 502, 503, 504}
_FLOW_MAX_ATTEMPTS = int(os.getenv("E2E_FLOW_MAX_ATTEMPTS", "3") or 3)
_FLOW_OVERRIDES = {
    # intent-route-v3 clarify cards ask profile.client_role before routing.
    # For non-litigation contract flows, "applicant" is the most stable neutral role.
    "profile.client_role": "applicant",
    # contract-intake validator expects enum values full/focused.
    # Use full to avoid extra focus_areas follow-up loops in unstable remote envs.
    "profile.review_scope": "full",
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
async def test_contract_review_generates_review_report(lawyer_client):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    contract_path = _REAL_CASE_CONTRACT_DOCX if _REAL_CASE_CONTRACT_DOCX.exists() else (evidence_dir / "sample_contract.txt")

    up = await lawyer_client.upload_file(str(contract_path), purpose="consultation")
    contract_file_id = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
    assert contract_file_id, up

    required_output_keys = {"contract_review_report"}
    kickoff = (
        "请审查已上传合同并输出结构化结论：整体风险等级、合同类型、审查摘要、风险条款清单。"
        "重点关注违约责任、争议解决、免责条款与付款条件。"
    )

    async def _build_flow() -> WorkbenchFlow:
        sess = await lawyer_client.create_session(service_type_id="contract_review", client_role="applicant")
        session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
        assert session_id, sess
        matter_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("matter_id") or "").strip()
        return WorkbenchFlow(
            client=lawyer_client,
            session_id=session_id,
            uploaded_file_ids=[contract_file_id],
            overrides=dict(_FLOW_OVERRIDES),
            matter_id=matter_id or None,
        )

    flow: WorkbenchFlow | None = None
    for flow_attempt in range(1, max(1, _FLOW_MAX_ATTEMPTS) + 1):
        flow = await _build_flow()
        decisions_applied = False

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
        for kickoff_attempt in range(1, 4):
            if kickoff_attempt > 1:
                flow = await _build_flow()
                decisions_applied = False
            first_sse = await flow.nudge(kickoff, attachments=[contract_file_id], max_loops=3)
            if _is_retryable_initial_sse_error(first_sse):
                await asyncio.sleep(min(4.0, 0.8 * kickoff_attempt))
                continue
            if not await _matter_accessible():
                await asyncio.sleep(min(4.0, 0.8 * kickoff_attempt))
                continue
            break

        try:
            # Remote integration may complete an initial nudge with progress/task events only.
            # Keep strict lifecycle checks, and validate deliverables/snapshot downstream.
            assert_has_end(first_sse)
            if not is_session_busy_sse(first_sse):
                assert_has_progress(first_sse)
                assert_task_lifecycle(first_sse)

            async def _try_apply_clause_decisions() -> bool:
                nonlocal decisions_applied
                if decisions_applied:
                    return True
                if not flow.matter_id:
                    return False

                try:
                    snapshot_resp = await lawyer_client.get(
                        f"/matter-service/lawyer/matters/{flow.matter_id}/workbench/snapshot"
                    )
                    snapshot = unwrap_api_response(snapshot_resp)
                except httpx.HTTPStatusError as e:
                    if _is_retryable_http_error(e):
                        return False
                    raise
                if not isinstance(snapshot, dict):
                    return False
                analysis_state = (
                    snapshot.get("analysis_state")
                    if isinstance(snapshot.get("analysis_state"), dict)
                    else {}
                )
                goal_views = (
                    analysis_state.get("goal_views")
                    if isinstance(analysis_state.get("goal_views"), dict)
                    else {}
                )
                contract_view = (
                    analysis_state.get("contract_review_view")
                    if isinstance(analysis_state, dict)
                    else {}
                )
                if not isinstance(contract_view, dict) or not contract_view:
                    contract_view = (
                        goal_views.get("contract_review_view")
                        if isinstance(goal_views, dict)
                        else {}
                    )
                clauses = (
                    contract_view.get("clauses")
                    if isinstance(contract_view, dict)
                    and isinstance(contract_view.get("clauses"), list)
                    else []
                )
                clause_ids = [
                    str(row.get("clause_id") or "").strip()
                    for row in clauses
                    if isinstance(row, dict) and str(row.get("clause_id") or "").strip()
                ]
                # Keep enough accepted clauses so downstream doc quality gates
                # (clause references + numbered suggestions) can be satisfied.
                accepted = clause_ids[: min(len(clause_ids), 8)] if clause_ids else []
                params = {
                    "accepted_clause_ids": accepted,
                    "ignored_clause_ids": [],
                    "overrides": {},
                    "regenerate_documents": True,
                }
                for attempt in range(1, 7):
                    sse = await lawyer_client.workflow_action(
                        flow.session_id,
                        workflow_action="contract_review_apply_decisions",
                        workflow_action_params=params,
                        max_loops=36,
                    )
                    if is_session_busy_sse(sse):
                        await asyncio.sleep(min(6.0, 1.0 + attempt))
                        continue
                    assert_has_end(sse)
                    events = sse.get("events") if isinstance(sse, dict) and isinstance(sse.get("events"), list) else []
                    has_progress = any(isinstance(it, dict) and str(it.get("event") or "").strip() == "progress" for it in events)
                    has_card = any(isinstance(it, dict) and str(it.get("event") or "").strip() == "card" for it in events)
                    if has_progress:
                        decisions_applied = True
                        return True
                    # Some remote builds return an immediate card/end when doc quality checks fail.
                    # Keep this best-effort and let outer polling continue.
                    if has_card:
                        return False
                return False

            async def _any_contract_doc_ready(f: WorkbenchFlow) -> bool:
                await f.refresh()
                if not f.matter_id:
                    return False
                try:
                    resp = await f.client.list_deliverables(f.matter_id)
                    data = unwrap_api_response(resp)
                except httpx.HTTPStatusError as e:
                    if _is_retryable_http_error(e):
                        if not decisions_applied:
                            await _try_apply_clause_decisions()
                        return False
                    raise
                if not isinstance(data, dict):
                    return False
                items = data.get("deliverables") if isinstance(data.get("deliverables"), list) else []
                by_key = {
                    str(it.get("output_key") or "").strip(): it
                    for it in items
                    if isinstance(it, dict) and str(it.get("output_key") or "").strip()
                }
                if not required_output_keys.issubset(set(by_key.keys())):
                    if not decisions_applied:
                        await _try_apply_clause_decisions()
                    return False
                for k in ("contract_review_report", "modification_suggestion", "redline_comparison"):
                    if not str((by_key.get(k) or {}).get("file_id") or "").strip():
                        if not decisions_applied:
                            await _try_apply_clause_decisions()
                        return False
                phase_summary = by_key.get("phase_summary__contract_output") or {}
                content = phase_summary.get("content") if isinstance(phase_summary.get("content"), dict) else {}
                md = str(content.get("markdown") or content.get("md") or content.get("content") or "").strip()
                return len(md) > 30

            await flow.run_until(_any_contract_doc_ready, max_steps=20, description="contract review deliverables ready")
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
    assert_service_type(prof, "contract_review")

    snapshot_resp = await lawyer_client.get(f"/matter-service/lawyer/matters/{flow.matter_id}/workbench/snapshot")
    snapshot = unwrap_api_response(snapshot_resp)
    assert isinstance(snapshot, dict), snapshot_resp
    analysis_state = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    contract_view = analysis_state.get("contract_review_view") if isinstance(analysis_state, dict) else {}
    assert isinstance(contract_view, dict), snapshot_resp
    assert str(contract_view.get("summary") or "").strip(), contract_view

    dels_resp = await lawyer_client.list_deliverables(flow.matter_id)
    dels = unwrap_api_response(dels_resp)
    items = (dels.get("deliverables") if isinstance(dels, dict) else None) or []
    by_key = {
        str(it.get("output_key") or "").strip(): it
        for it in items
        if isinstance(it, dict) and str(it.get("output_key") or "").strip()
    }
    assert required_output_keys.issubset(set(by_key.keys())), sorted(by_key.keys())
    for key in ("contract_review_report", "modification_suggestion", "redline_comparison"):
        assert str((by_key.get(key) or {}).get("file_id") or "").strip(), by_key.get(key)

    picked = by_key.get("contract_review_report")
    assert isinstance(picked, dict), dels_resp

    file_id = str(picked.get("file_id") or "").strip()
    assert file_id, picked
    docx_bytes = await lawyer_client.download_file_bytes(file_id)
    text = extract_docx_text(docx_bytes)
    assert_docx_has_no_template_placeholders(text)
    assert_docx_contains(text, must_include=["合同"])
    assert ("甲方" in text or "发包人" in text), text[:1200]
    assert ("乙方" in text or "承包人" in text), text[:1200]
