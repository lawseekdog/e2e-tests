from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest

from tests.lawyer_workbench._support.docx import (
    assert_contract_review_docx_benchmark,
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, is_session_busy_sse
from tests.lawyer_workbench._support.sse import assert_has_end, assert_has_progress, assert_task_lifecycle
from tests.lawyer_workbench._support.utils import unwrap_api_response


_WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
_CONTRACT_DOCX = _WORKSPACE_ROOT / "已征收闲置土地垃圾清运.docx"
_GOLD_REVIEW_DOCX = _WORKSPACE_ROOT / "《已征收闲置土地垃圾清运合同》法律审查意见书.docx"
_RETRYABLE_HTTP_STATUS = {404, 409, 429, 500, 502, 503, 504}
_FLOW_MAX_ATTEMPTS = int(os.getenv("E2E_FLOW_MAX_ATTEMPTS", "3") or 3)
_FLOW_OVERRIDES = {
    # intent-route-v3 clarify cards ask profile.client_role before routing.
    # For non-litigation contract flows, "applicant" is the most stable neutral role.
    "profile.client_role": "applicant",
    # contract-intake validator expects enum values full/focused.
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


def _must_exist(path: Path) -> None:
    assert path.exists() and path.is_file(), f"required file missing: {path}"


def _required_contract_view_fields(view: dict) -> None:
    assert isinstance(view, dict), f"contract_review_view missing: {view}"
    assert str(view.get("overall_risk_level") or "").strip() in {"low", "medium", "high", "critical"}, view
    assert str(view.get("contract_type") or "").strip(), view
    assert str(view.get("summary") or "").strip(), view
    clauses = view.get("clauses")
    assert isinstance(clauses, list) and clauses, view


@pytest.mark.e2e
@pytest.mark.slow
async def test_contract_review_benchmark_against_gold_opinion(lawyer_client):
    _must_exist(_CONTRACT_DOCX)
    _must_exist(_GOLD_REVIEW_DOCX)

    up = await lawyer_client.upload_file(str(_CONTRACT_DOCX), purpose="consultation")
    contract_file_id = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
    assert contract_file_id, up

    required_keys = {
        "phase_summary__contract_output",
        "contract_review_report",
        "modification_suggestion",
        "redline_comparison",
    }
    kickoff = (
        "请对上传的建设工程施工合同进行审查，并按法律审查意见书风格输出："
        "需包含法律依据、审查内容、问题及修改建议、声明与保留、落款。"
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
                accepted = clause_ids[: min(len(clause_ids), 8)] if clause_ids else []
                params = {
                    # Keep enough accepted clauses so report quality gates
                    # (clause references + numbered suggestions) can be satisfied.
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

            async def _deliverables_ready(f: WorkbenchFlow) -> bool:
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
                rows = data.get("deliverables") if isinstance(data.get("deliverables"), list) else []
                if not rows:
                    return False
                by_key = {
                    str(it.get("output_key") or "").strip(): it
                    for it in rows
                    if isinstance(it, dict) and str(it.get("output_key") or "").strip()
                }
                if not required_keys.issubset(set(by_key.keys())):
                    if not decisions_applied:
                        await _try_apply_clause_decisions()
                    return False
                for key in ("contract_review_report", "modification_suggestion", "redline_comparison"):
                    if not str((by_key.get(key) or {}).get("file_id") or "").strip():
                        if not decisions_applied:
                            await _try_apply_clause_decisions()
                        return False
                summary = by_key.get("phase_summary__contract_output") or {}
                content = summary.get("content") if isinstance(summary.get("content"), dict) else {}
                md = str(content.get("markdown") or content.get("md") or content.get("content") or "").strip()
                return len(md) > 30

            await flow.run_until(
                _deliverables_ready,
                max_steps=20,
                description="all contract-review deliverables with file_id and phase summary",
            )
            break
        except AssertionError:
            if flow_attempt >= max(1, _FLOW_MAX_ATTEMPTS):
                raise
            await asyncio.sleep(min(6.0, 1.2 * flow_attempt))

    assert flow is not None
    assert flow.matter_id

    snapshot_resp = await lawyer_client.get(f"/matter-service/lawyer/matters/{flow.matter_id}/workbench/snapshot")
    snapshot = unwrap_api_response(snapshot_resp)
    assert isinstance(snapshot, dict), snapshot_resp
    analysis_state = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    contract_view = analysis_state.get("contract_review_view") if isinstance(analysis_state, dict) else {}
    _required_contract_view_fields(contract_view if isinstance(contract_view, dict) else {})

    deliverables_resp = await lawyer_client.list_deliverables(flow.matter_id)
    deliverables_data = unwrap_api_response(deliverables_resp)
    rows = deliverables_data.get("deliverables") if isinstance(deliverables_data, dict) else []
    assert isinstance(rows, list) and rows, deliverables_resp

    by_key = {
        str(it.get("output_key") or "").strip(): it
        for it in rows
        if isinstance(it, dict) and str(it.get("output_key") or "").strip()
    }
    assert required_keys.issubset(set(by_key.keys())), sorted(by_key.keys())

    report = by_key.get("contract_review_report") or {}
    report_file_id = str(report.get("file_id") or "").strip()
    assert report_file_id, report

    generated_docx = await lawyer_client.download_file_bytes(report_file_id)
    generated_text = extract_docx_text(generated_docx)
    assert_docx_has_no_template_placeholders(generated_text)

    gold_text = extract_docx_text(_GOLD_REVIEW_DOCX.read_bytes())
    benchmark = assert_contract_review_docx_benchmark(generated_text, gold_text=gold_text)
    assert benchmark.passed
