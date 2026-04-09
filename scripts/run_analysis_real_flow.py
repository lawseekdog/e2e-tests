"""Run real analysis workflow via consultations-service WebSocket (no mock LLM)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys

E2E_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = E2E_ROOT.parent
sys.path.insert(0, str(E2E_ROOT))

from client.api_client import ApiClient
from scripts._support.workflow_real_flow_support import (
    bootstrap_flow,
    collect_ai_debug_refs,
    configure_direct_service_mode,
    event_counts,
    fetch_execution_snapshot_by_session,
    fetch_execution_traces_by_session,
    fetch_workbench_snapshot,
    is_goal_completion_card,
    list_deliverables,
    list_session_messages,
    load_real_flow_env,
    resolve_output_dir,
    safe_str as _safe_str,
    terminate_stale_script_runs,
    upload_consultation_files,
    write_json,
)
from scripts._support.diagnostic_bundle_support import export_failure_bundle, export_observability_bundle, format_first_bad_line
from scripts._support.flow_score_support import build_flow_scores, collect_flow_observability
from scripts._support.quality_policy_support import build_bundle_quality_reports
from scripts._support.run_status import RunStatusSupervisor


START_REQUESTED_DOCUMENTS: list[dict[str, str]] = [
    {"document_kind": "case_analysis_report", "instance_key": ""},
]

FLOW_OVERRIDES: dict[str, Any] = {}

def _extract_analysis_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    view = snapshot.get("analysis_view") if isinstance(snapshot.get("analysis_view"), dict) else {}
    return view if isinstance(view, dict) else {}


def _extract_pricing_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    view = snapshot.get("pricing_plan_view") if isinstance(snapshot.get("pricing_plan_view"), dict) else {}
    return view if isinstance(view, dict) else {}


def _extract_document_generation_state(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    state = snapshot.get("document_generation_state") if isinstance(snapshot.get("document_generation_state"), dict) else {}
    return state if isinstance(state, dict) else {}


def _section_items(analysis_view: dict[str, Any] | None, section_type: str) -> list[dict[str, Any]]:
    view = analysis_view if isinstance(analysis_view, dict) else {}
    sections = view.get("sections") if isinstance(view.get("sections"), list) else []
    for row in sections:
        if not isinstance(row, dict):
            continue
        if _safe_str(row.get("section_type")) != section_type:
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        return [item for item in items if isinstance(item, dict)]
    return []


def _extract_runtime_progress(snapshot: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(snapshot, dict):
        return {"current_task_id": "", "current_node": "", "current_phase": ""}
    analysis = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    identity = analysis.get("identity") if isinstance(analysis.get("identity"), dict) else {}
    runtime = analysis.get("workbench_runtime") if isinstance(analysis.get("workbench_runtime"), dict) else {}
    return {
        "current_task_id": _safe_str(
            analysis.get("current_task_id") or identity.get("current_task_id") or runtime.get("current_task_id")
        ),
        "current_node": _safe_str(analysis.get("current_node") or runtime.get("current_node")),
        "current_phase": _safe_str(analysis.get("current_phase") or snapshot.get("current_phase") or runtime.get("current_phase")),
    }


def _compact_pending_card(card: dict[str, Any] | None) -> dict[str, Any]:
    pending = card if isinstance(card, dict) else {}
    questions = pending.get("questions") if isinstance(pending.get("questions"), list) else []
    return {
        "id": _safe_str(pending.get("id")),
        "skill_id": _safe_str(pending.get("skill_id")),
        "task_key": _safe_str(pending.get("task_key")),
        "review_type": _safe_str(pending.get("review_type")),
        "question_count": len(questions),
        "questions": questions,
    }


def _is_intake_card(card: dict[str, Any] | None) -> bool:
    if not isinstance(card, dict):
        return False
    return _safe_str(card.get("skill_id")).lower() == "civil-analysis-intake"


def _docgen_expected(docgen_state: dict[str, Any] | None) -> bool:
    state = docgen_state if isinstance(docgen_state, dict) else {}
    runtime_signals = state.get("runtime_signals") if isinstance(state.get("runtime_signals"), dict) else {}
    return bool(
        _safe_str(state.get("status"))
        or _safe_str(state.get("quality_review_decision"))
        or bool(state.get("selected_documents"))
        or bool(runtime_signals)
    )


def _docgen_terminal(docgen_state: dict[str, Any] | None) -> bool:
    state = docgen_state if isinstance(docgen_state, dict) else {}
    if not _docgen_expected(state):
        return True
    status = _safe_str(state.get("status")).lower()
    return status in {"document_ready", "repair_blocked", "review_pending", "blocked", "failed", "completed", "ready"}


def _analysis_readiness(
    analysis_view: dict[str, Any],
    pricing_view: dict[str, Any],
    docgen_state: dict[str, Any],
    *,
    require_documents: bool,
) -> dict[str, Any]:
    docgen_checks = {
        "docgen_terminal": _docgen_terminal(docgen_state),
    }
    if require_documents:
        docgen_checks = {
            "docgen_started": _docgen_expected(docgen_state),
            "docgen_terminal": _docgen_terminal(docgen_state),
        }
    checks = {
        "summary_ready": bool(_safe_str(analysis_view.get("summary"))),
        "issues_ready": bool(_section_items(analysis_view, "issues")),
        "strategy_options_ready": bool(_section_items(analysis_view, "strategy_matrix")),
        **docgen_checks,
    }
    optional_checks = {
        "pricing_ready": _safe_str(pricing_view.get("status")).lower() in {"ready", "review_pending", "completed"},
    }
    missing = [name for name, passed in checks.items() if not passed]
    return {
        "checks": checks,
        "optional_checks": optional_checks,
        "missing_requirements": missing,
        "ready": not missing,
    }


def _analysis_reference_refresh_hint(
    snapshot: dict[str, Any] | None,
    analysis_view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot_obj = snapshot if isinstance(snapshot, dict) else {}
    analysis_state = snapshot_obj.get("analysis_state") if isinstance(snapshot_obj.get("analysis_state"), dict) else {}
    analysis_obj = analysis_view if isinstance(analysis_view, dict) else _extract_analysis_view(snapshot_obj)
    reference_suite = analysis_obj.get("reference_suite") if isinstance(analysis_obj.get("reference_suite"), dict) else {}
    counts = reference_suite.get("counts") if isinstance(reference_suite.get("counts"), dict) else {}
    law_count = int(counts.get("law_count") or 0)
    case_count = int(counts.get("case_count") or 0)
    blocking_codes = [
        _safe_str(code).lower()
        for code in (reference_suite.get("blocking_reason_codes") if isinstance(reference_suite.get("blocking_reason_codes"), list) else [])
        if _safe_str(code)
    ]
    diagnostics = (
        analysis_state.get("references_diagnostics_summary")
        if isinstance(analysis_state.get("references_diagnostics_summary"), dict)
        else {}
    )
    final_reason = _safe_str(diagnostics.get("final_reason") or diagnostics.get("dominant_reason_code")).lower()
    status = _safe_str(reference_suite.get("status") or diagnostics.get("final_status")).lower()
    current_subgraph = _safe_str(analysis_state.get("current_subgraph") or analysis_state.get("runtime_node_scope")).lower()
    current_task_id = _safe_str(analysis_state.get("current_task_id")).lower()
    current_node = _safe_str(analysis_state.get("current_node")).lower()
    refreshable_reasons = {
        "retrieval_no_hit",
        "retrieval_not_attempted",
        "grounding_blocked",
        "authority_pending",
    }
    refreshable_codes = {
        "references_grounding_law_rows_missing",
        "authority_pending",
        "retrieval_no_hit",
    }
    in_references = (
        current_subgraph == "references"
        or current_task_id.startswith("references")
        or current_node.startswith("references")
        or current_task_id == "analysis_project_analysis_view"
        or current_node == "analysis_project_analysis_view"
    )
    if law_count > 0 or case_count > 0:
        return {}
    if status != "blocked" and final_reason not in refreshable_reasons:
        return {}
    if not in_references and status != "blocked":
        return {}
    if not (set(blocking_codes) & refreshable_codes or final_reason in refreshable_reasons):
        return {}
    return {
        "status": status,
        "current_subgraph": current_subgraph,
        "current_task_id": current_task_id,
        "current_node": current_node,
        "blocking_reason_codes": blocking_codes,
        "final_reason": final_reason,
    }


def _progress_fingerprint(
    snapshot: dict[str, Any] | None,
    pending_card: dict[str, Any] | None,
    analysis_view: dict[str, Any] | None,
    pricing_view: dict[str, Any] | None,
) -> str:
    snapshot_obj = snapshot if isinstance(snapshot, dict) else {}
    analysis_state = snapshot_obj.get("analysis_state") if isinstance(snapshot_obj.get("analysis_state"), dict) else {}
    evidence_readiness = (
        analysis_state.get("evidence_readiness")
        if isinstance(analysis_state.get("evidence_readiness"), dict)
        else {}
    )
    references_diag = (
        analysis_state.get("references_diagnostics_summary")
        if isinstance(analysis_state.get("references_diagnostics_summary"), dict)
        else {}
    )
    analysis_obj = analysis_view if isinstance(analysis_view, dict) else {}
    pricing_obj = pricing_view if isinstance(pricing_view, dict) else {}
    payload = {
        "runtime": _extract_runtime_progress(snapshot_obj),
        "cause_status": _safe_str(analysis_state.get("cause_status")),
        "current_subgraph": _safe_str(analysis_state.get("current_subgraph")),
        "pending_task_count": snapshot_obj.get("matter", {}).get("pending_task_count") if isinstance(snapshot_obj.get("matter"), dict) else None,
        "pending_card": _compact_pending_card(pending_card),
        "analysis_view": {
            "status": _safe_str(analysis_obj.get("status")),
            "updated_at": _safe_str(analysis_obj.get("updated_at")),
            "summary_len": len(_safe_str(analysis_obj.get("summary"))),
            "issues_count": len(_section_items(analysis_obj, "issues")),
            "strategy_options_count": len(_section_items(analysis_obj, "strategy_matrix")),
            "blocking_reason_codes": [
                _safe_str(code)
                for code in (analysis_obj.get("blocking_reason_codes") if isinstance(analysis_obj.get("blocking_reason_codes"), list) else [])
                if _safe_str(code)
            ],
        },
        "pricing_plan_view": {
            "status": _safe_str(pricing_obj.get("status")),
            "reviewed": bool(pricing_obj.get("reviewed")),
            "updated_at": _safe_str(pricing_obj.get("updated_at")),
            "blocking_reason_codes": [
                _safe_str(code)
                for code in (pricing_obj.get("blocking_reason_codes") if isinstance(pricing_obj.get("blocking_reason_codes"), list) else [])
                if _safe_str(code)
            ],
        },
        "evidence_readiness": {
            "status": _safe_str(evidence_readiness.get("status")),
            "next_route": _safe_str(evidence_readiness.get("next_route")),
            "reason_codes": [
                _safe_str(code)
                for code in (evidence_readiness.get("reason_codes") if isinstance(evidence_readiness.get("reason_codes"), list) else [])
                if _safe_str(code)
            ],
        },
        "references_diagnostics": {
            "final_status": _safe_str(references_diag.get("final_status")),
            "dominant_reason_code": _safe_str(references_diag.get("dominant_reason_code")),
            "counts": references_diag.get("counts") if isinstance(references_diag.get("counts"), dict) else {},
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _is_session_busy_sse(sse: dict[str, Any] | None) -> bool:
    if not isinstance(sse, dict):
        return False
    events = sse.get("events") if isinstance(sse.get("events"), list) else []
    for row in events:
        if not isinstance(row, dict):
            continue
        if _safe_str(row.get("event")).lower() != "error":
            continue
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        reason = _safe_str(data.get("reason") or data.get("error")).lower()
        message = _safe_str(data.get("message")).lower()
        if reason == "session_busy" or "会话正在处理中" in message or "session busy" in message:
            return True
    return "会话正在处理中" in _safe_str(sse.get("output")) or "session busy" in _safe_str(sse.get("output")).lower()


def _write_run_status(
    supervisor: RunStatusSupervisor,
    *,
    state: str,
    current_step: str,
    require_documents: bool = False,
    session_id: str,
    matter_id: str,
    wait_round: int,
    snapshot: dict[str, Any] | None,
    execution_snapshot: dict[str, Any] | None,
    execution_traces: list[dict[str, Any]] | None,
    pending_card: dict[str, Any] | None,
    analysis_view: dict[str, Any] | None,
    pricing_view: dict[str, Any] | None,
    docgen_state: dict[str, Any] | None,
    seen_cards: int,
    seen_sse_rounds: int,
    error: str = "",
    start_output: str = "",
) -> None:
    snapshot_obj = snapshot if isinstance(snapshot, dict) else {}
    analysis_obj = analysis_view if isinstance(analysis_view, dict) else {}
    pricing_obj = pricing_view if isinstance(pricing_view, dict) else {}
    docgen_obj = docgen_state if isinstance(docgen_state, dict) else {}
    runtime = _extract_runtime_progress(snapshot_obj)
    readiness = _analysis_readiness(
        analysis_obj,
        pricing_obj,
        docgen_obj,
        require_documents=require_documents,
    )
    supervisor.update(
        status=_safe_str(state),
        current_step=current_step,
        session_id=_safe_str(session_id),
        matter_id=_safe_str(matter_id),
        snapshot=snapshot_obj,
        execution_snapshot=execution_snapshot if isinstance(execution_snapshot, dict) else None,
        execution_traces=execution_traces if isinstance(execution_traces, list) else None,
        pending_card=pending_card,
        current_blocker=",".join(readiness.get("missing_requirements") or []),
        next_action="collect_final_outputs" if bool(readiness.get("ready")) else "continue_poll",
        wait_round=wait_round,
        seen_cards=seen_cards,
        seen_sse_rounds=seen_sse_rounds,
        error=_safe_str(error),
        latest_payloads={
            "snapshot": snapshot_obj,
            "analysis_view": analysis_obj,
            "pricing_plan_view": pricing_obj,
            "document_generation_state": docgen_obj,
            "execution_snapshot": execution_snapshot if isinstance(execution_snapshot, dict) else {},
            "execution_traces": execution_traces if isinstance(execution_traces, list) else [],
            "pending_card": _compact_pending_card(pending_card),
        },
        extra={
            "analysis_view": {
                "summary_len": len(_safe_str(analysis_obj.get("summary"))),
                "issues_count": len(_section_items(analysis_obj, "issues")),
                "strategy_options_count": len(_section_items(analysis_obj, "strategy_matrix")),
            },
            "pricing_plan_view": {
                "status": _safe_str(pricing_obj.get("status")),
                "reviewed": bool(pricing_obj.get("reviewed")),
                "pricing_mode": _safe_str(pricing_obj.get("pricing_mode")),
            },
            "document_generation_state": {
                "status": _safe_str(docgen_obj.get("status")),
                "formal_gate_blocked": bool(docgen_obj.get("formal_gate_blocked")),
                "quality_review_decision": _safe_str(docgen_obj.get("quality_review_decision")),
                "selected_document_count": len(
                    docgen_obj.get("selected_documents") if isinstance(docgen_obj.get("selected_documents"), list) else []
                ),
            },
            "readiness": readiness,
            "start_output": _safe_str(start_output),
            "runtime_progress": runtime,
        },
    )


def _fallback_failure_summary(
    *,
    session_id: str,
    matter_id: str,
    snapshot: dict[str, Any] | None,
    error: Exception,
) -> dict[str, Any]:
    runtime = _extract_runtime_progress(snapshot if isinstance(snapshot, dict) else {})
    message = _safe_str(error)
    reason_code = "analysis_view_not_ready" if "analysis readiness" in message else error.__class__.__name__.lower()
    return {
        "contract_version": "failure_summary.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bundle_dir": "",
        "first_bad_node": runtime.get("current_node") or runtime.get("current_task_id") or "",
        "first_bad_stage": runtime.get("current_phase") or "",
        "failure_class": "workflow_run_failed",
        "primary_reason_code": reason_code,
        "reason_code_chain": [reason_code],
        "retry_prompt": message,
    }

async def run(args: argparse.Namespace) -> int:
    load_real_flow_env(repo_root=REPO_ROOT, e2e_root=E2E_ROOT)
    terminate_stale_script_runs(script_name="run_analysis_real_flow.py")

    direct_mode = not bool(args.use_gateway)
    direct_config: dict[str, str] = {}
    if direct_mode:
        base_url, direct_config = configure_direct_service_mode(
            remote_stack_host=_safe_str(args.remote_stack_host),
            consultations_base_url=_safe_str(args.consultations_base_url),
            matter_base_url=_safe_str(args.matter_base_url),
            files_base_url=_safe_str(args.files_base_url),
            local_consultations=True,
            local_matter=True,
            direct_user_id=_safe_str(args.direct_user_id),
            direct_org_id=_safe_str(args.direct_org_id),
            direct_is_superuser=_safe_str(args.direct_is_superuser),
        )
    else:
        base_url = _safe_str(args.base_url) or _safe_str(os.getenv("BASE_URL")) or "http://localhost:18001/api/v1"
    username = _safe_str(args.username) or _safe_str(os.getenv("LAWYER_USERNAME")) or "lawyer1"
    password = _safe_str(args.password) or _safe_str(os.getenv("LAWYER_PASSWORD")) or "lawyer123456"

    fixture_dir = E2E_ROOT / "fixtures"
    evidence_files = [
        fixture_dir / "sample_iou.pdf",
        fixture_dir / "sample_chat_record.txt",
        fixture_dir / "sample_transfer_record.txt",
    ]

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = resolve_output_dir(
        repo_root=REPO_ROOT,
        output_dir=_safe_str(args.output_dir),
        default_leaf=f"output/analysis-chain/{ts}",
    )
    require_documents = False
    supervisor = RunStatusSupervisor(out_dir=out_dir, flow_id="analysis")
    supervisor.update(
        status="booting",
        current_step="bootstrap.init",
        current_blocker="",
        next_action="login",
    )

    print(f"[config] base_url={base_url}")
    print(f"[config] direct_service_mode={direct_mode}")
    if direct_mode:
        print(f"[config] consultations_base_url={direct_config.get('consultations_base_url') or '-'}")
        print(f"[config] files_base_url={direct_config.get('files_base_url') or '-'}")
        print(f"[config] matter_base_url={direct_config.get('matter_base_url') or '-'}")
        print(f"[config] direct_user_id={direct_config.get('direct_user_id') or '-'}")
        print(f"[config] direct_org_id={direct_config.get('direct_org_id') or '-'}")
    else:
        print("[config] gateway_mode=true")
    print(f"[config] user={username}")
    print(f"[config] output_dir={out_dir}")

    async with ApiClient(base_url) as client:
        await client.login(username, password)
        print(f"[login] ok user_id={client.user_id} org_id={client.organization_id}")
        supervisor.update(
            status="booting",
            current_step="bootstrap.login",
            next_action="upload_files",
        )

        uploaded_file_ids = await upload_consultation_files(client, evidence_files)
        for path, fid in zip([p for p in evidence_files if p.exists()], uploaded_file_ids):
            print(f"[upload] ok file={path.name} file_id={fid}")
        supervisor.update(
            status="booting",
            current_step="bootstrap.upload_files",
            next_action="create_session",
            extra={"uploaded_file_ids": uploaded_file_ids},
        )

        flow, session_id, matter_id = await bootstrap_flow(
            client=client,
            service_type_id="civil_prosecution",
            client_role="plaintiff",
            uploaded_file_ids=uploaded_file_ids,
            overrides=FLOW_OVERRIDES,
            preseed_profile=False,
            progress_observer=supervisor.observe_flow_progress,
        )
        print(f"[session] id={session_id} matter_id={matter_id or '-'}")
        supervisor.update(
            status="booting",
            current_step="bootstrap.session_created",
            session_id=session_id,
            matter_id=matter_id,
            next_action="start_analysis",
        )

        start_output = ""
        start_counts: dict[str, int] = {}
        start_settle_mode = "fire_and_poll"
        supervisor.update(
            status="running",
            current_step="start_request.submitting",
            session_id=session_id,
            matter_id=_safe_str(flow.matter_id) or matter_id,
            next_action="await_start_request_events",
        )
        start_sse = await flow.request_documents(
            START_REQUESTED_DOCUMENTS,
            attachments=uploaded_file_ids,
            max_loops=max(1, int(args.action_max_loops)),
            settle_mode=start_settle_mode,
            label="case_analysis_report",
        )
        start_counts = event_counts(start_sse if isinstance(start_sse, dict) else {})
        write_json(out_dir / "start_analysis.sse.json", start_sse if isinstance(start_sse, dict) else {})
        start_output = _safe_str((start_sse if isinstance(start_sse, dict) else {}).get("output"))
        wait_round = 0
        _write_run_status(
            supervisor,
            state="start_request_completed",
            current_step="start_request.completed",
            require_documents=require_documents,
            session_id=session_id,
            matter_id=_safe_str(flow.matter_id),
            wait_round=wait_round,
            snapshot={},
            execution_snapshot=await fetch_execution_snapshot_by_session(session_id),
            execution_traces=await fetch_execution_traces_by_session(session_id),
            pending_card=None,
            analysis_view={},
            pricing_view={},
            docgen_state={},
            seen_cards=len(flow.seen_cards),
            seen_sse_rounds=len(flow.seen_sse),
            start_output=start_output,
        )
        reference_refresh_attempts = 0
        reference_refresh_requests = 0
        max_no_progress_steps = max(1, int(args.max_steps))
        max_total_steps = max(max_no_progress_steps, max_no_progress_steps * 4)
        last_progress_fingerprint = ""
        stall_rounds = 0
        try:
            for step_no in range(1, max_total_steps + 1):
                wait_round += 1
                await flow.refresh()
                if not flow.matter_id:
                    next_progress_fingerprint = f"waiting_for_matter:{len(flow.seen_cards)}:{len(flow.seen_sse)}"
                    if next_progress_fingerprint == last_progress_fingerprint:
                        stall_rounds += 1
                    else:
                        last_progress_fingerprint = next_progress_fingerprint
                        stall_rounds = 0
                    _write_run_status(
                        supervisor,
                        state="waiting_for_matter",
                        current_step="poll.waiting_for_matter",
                        require_documents=require_documents,
                        session_id=session_id,
                        matter_id="",
                        wait_round=wait_round,
                        snapshot={},
                        execution_snapshot=await fetch_execution_snapshot_by_session(session_id),
                        execution_traces=await fetch_execution_traces_by_session(session_id),
                        pending_card=None,
                        analysis_view={},
                        pricing_view={},
                        docgen_state={},
                        seen_cards=len(flow.seen_cards),
                        seen_sse_rounds=len(flow.seen_sse),
                        start_output=start_output,
                    )
                    print(
                        f"[flow-progress] waiting:analysis readiness step={step_no}/{max_total_steps} "
                        f"stall={stall_rounds}/{max_no_progress_steps} session={session_id} matter=- status=waiting_for_matter",
                        flush=True,
                    )
                    if stall_rounds >= max_no_progress_steps:
                        raise AssertionError(
                            "Failed to bind matter_id while workflow was making no observable progress "
                            f"for {max_no_progress_steps} consecutive polling rounds "
                            f"(total_steps={step_no}, session_id={session_id})"
                        )
                    continue

                pending = await flow.get_pending_card()
                snapshot = await fetch_workbench_snapshot(client, flow.matter_id)
                execution_snapshot = await fetch_execution_snapshot_by_session(session_id)
                execution_traces = await fetch_execution_traces_by_session(session_id)
                analysis_view = _extract_analysis_view(snapshot)
                pricing_view = _extract_pricing_view(snapshot)
                docgen_state = _extract_document_generation_state(snapshot)
                next_progress_fingerprint = _progress_fingerprint(snapshot, pending, analysis_view, pricing_view)
                if next_progress_fingerprint == last_progress_fingerprint:
                    stall_rounds += 1
                else:
                    last_progress_fingerprint = next_progress_fingerprint
                    stall_rounds = 0
                if is_goal_completion_card(pending):
                    _write_run_status(
                        supervisor,
                        state="goal_completion_pending",
                        current_step="poll.goal_completion_pending",
                        require_documents=require_documents,
                        session_id=session_id,
                        matter_id=_safe_str(flow.matter_id),
                        wait_round=wait_round,
                        snapshot=snapshot,
                        execution_snapshot=execution_snapshot,
                        execution_traces=execution_traces,
                        pending_card=pending,
                        analysis_view=analysis_view,
                        pricing_view=pricing_view,
                        docgen_state=docgen_state,
                        seen_cards=len(flow.seen_cards),
                        seen_sse_rounds=len(flow.seen_sse),
                        start_output=start_output,
                    )
                    break
                if _is_intake_card(pending):
                    raise AssertionError(
                        "Unexpected intake card in real-entry mode "
                        f"(session_id={session_id}, matter_id={flow.matter_id}, task_key={_safe_str(pending.get('task_key'))})"
                    )

                readiness = _analysis_readiness(
                    analysis_view,
                    pricing_view,
                    docgen_state,
                    require_documents=require_documents,
                )
                ready = bool(readiness.get("ready"))
                _write_run_status(
                    supervisor,
                    state="ready" if ready else "waiting_for_views",
                    current_step="poll.analysis_readiness",
                    require_documents=require_documents,
                    session_id=session_id,
                    matter_id=_safe_str(flow.matter_id),
                    wait_round=wait_round,
                    snapshot=snapshot,
                    execution_snapshot=execution_snapshot,
                    execution_traces=execution_traces,
                    pending_card=pending,
                    analysis_view=analysis_view,
                    pricing_view=pricing_view,
                    docgen_state=docgen_state,
                    seen_cards=len(flow.seen_cards),
                    seen_sse_rounds=len(flow.seen_sse),
                    start_output=start_output,
                )
                if ready:
                    break

                refresh_hint = _analysis_reference_refresh_hint(snapshot, analysis_view)
                if (
                    refresh_hint
                    and bool(readiness.get("checks", {}).get("issues_ready"))
                    and not pending
                    and reference_refresh_attempts < max(0, int(args.max_reference_refresh))
                ):
                    reference_refresh_requests += 1
                    sse = await flow.request_documents(
                        START_REQUESTED_DOCUMENTS,
                        max_loops=max(12, int(args.action_max_loops)),
                        settle_mode="fire_and_poll",
                        label="case_analysis_report_refresh",
                    )
                    busy = _is_session_busy_sse(sse if isinstance(sse, dict) else {})
                    if not busy:
                        reference_refresh_attempts += 1
                    write_json(
                        out_dir / f"references_refresh.{reference_refresh_requests}.sse.json",
                        sse if isinstance(sse, dict) else {},
                    )
                    print(
                        f"[analysis] references_refresh_partial request={reference_refresh_requests} "
                        f"attempt={reference_refresh_attempts} busy={busy} "
                        f"reason={_safe_str(refresh_hint.get('final_reason') or refresh_hint.get('status'))} "
                        f"task={_safe_str(refresh_hint.get('current_task_id') or refresh_hint.get('current_node'))}",
                        flush=True,
                    )
                    continue

                print(
                    f"[flow-progress] waiting:analysis readiness step={step_no}/{max_total_steps} "
                    f"stall={stall_rounds}/{max_no_progress_steps} session={session_id} matter={flow.matter_id} status=active",
                    flush=True,
                )
                if stall_rounds >= max_no_progress_steps:
                    raise AssertionError(
                        "Failed to reach analysis readiness after "
                        f"{max_no_progress_steps} consecutive no-progress polling rounds "
                        f"(total_steps={step_no}, session_id={session_id}, matter_id={flow.matter_id})"
                    )
                await flow.step(stop_on_pending_card=is_goal_completion_card)
            else:
                raise AssertionError(
                    "Failed to reach analysis readiness within the total polling budget "
                    f"(total_steps={max_total_steps}, max_no_progress_steps={max_no_progress_steps}, "
                    f"session_id={session_id}, matter_id={flow.matter_id})"
                )
        except Exception as exc:
            await flow.refresh()
            fail_snapshot = await fetch_workbench_snapshot(client, _safe_str(flow.matter_id)) if flow.matter_id else {}
            fail_execution_snapshot = await fetch_execution_snapshot_by_session(session_id)
            fail_execution_traces = await fetch_execution_traces_by_session(session_id)
            fail_messages = await list_session_messages(client, session_id)
            debug_refs = await collect_ai_debug_refs(
                client,
                repo_root=REPO_ROOT,
                session_id=session_id,
                matter_id=_safe_str(flow.matter_id),
            )
            failure_diag = {
                "error": str(exc),
                "session_id": session_id,
                "matter_id": _safe_str(flow.matter_id),
                "seen_cards": len(flow.seen_cards),
                "seen_sse_rounds": len(flow.seen_sse),
                "latest_assistant_message": next(
                    (
                        _safe_str(row.get("content"))
                        for row in reversed(fail_messages)
                        if _safe_str(row.get("role")).lower() == "assistant" and _safe_str(row.get("content"))
                    ),
                    "",
                ),
                "debug_refs": debug_refs,
            }
            _write_run_status(
                supervisor,
                state="failed",
                current_step="terminal.failed",
                require_documents=require_documents,
                session_id=session_id,
                matter_id=_safe_str(flow.matter_id),
                wait_round=wait_round,
                snapshot=fail_snapshot if isinstance(fail_snapshot, dict) else {},
                execution_snapshot=fail_execution_snapshot,
                execution_traces=fail_execution_traces,
                pending_card=await flow.get_pending_card(),
                analysis_view=_extract_analysis_view(fail_snapshot if isinstance(fail_snapshot, dict) else {}),
                pricing_view=_extract_pricing_view(fail_snapshot if isinstance(fail_snapshot, dict) else {}),
                docgen_state=_extract_document_generation_state(fail_snapshot if isinstance(fail_snapshot, dict) else {}),
                seen_cards=len(flow.seen_cards),
                seen_sse_rounds=len(flow.seen_sse),
                error=str(exc),
                start_output=start_output,
            )
            fallback_summary = _fallback_failure_summary(
                session_id=session_id,
                matter_id=_safe_str(flow.matter_id),
                snapshot=fail_snapshot if isinstance(fail_snapshot, dict) else {},
                error=exc,
            )
            if isinstance(fail_snapshot, dict) and fail_snapshot:
                write_json(out_dir / "snapshot.failure.json", fail_snapshot)
            try:
                bundle = export_failure_bundle(
                    repo_root=REPO_ROOT,
                    session_id=session_id,
                    matter_id=_safe_str(flow.matter_id),
                    reason="analysis_real_flow_failed",
                )
                bundle_quality = build_bundle_quality_reports(
                    bundle_dir=bundle["bundle_dir"],
                    flow_id="analysis",
                    snapshot=fail_snapshot if isinstance(fail_snapshot, dict) else {},
                    current_view=_extract_analysis_view(fail_snapshot if isinstance(fail_snapshot, dict) else {}),
                    goal_completion_mode="none",
                )
                write_json(out_dir / "failure_summary.json", bundle["summary"])
                write_json(out_dir / "bundle_quality.failure.json", bundle_quality)
                print(format_first_bad_line(bundle["summary"]))
            except Exception as diag_exc:
                failure_diag["observability_error"] = str(diag_exc)
                write_json(out_dir / "failure_summary.json", fallback_summary)
                print(format_first_bad_line(fallback_summary))
            write_json(out_dir / "failure_diagnostics.json", failure_diag)
            raise

        await flow.refresh()
        final_matter_id = _safe_str(flow.matter_id)
        if not final_matter_id:
            raise RuntimeError("matter_id missing after workflow run")

        snapshot = await fetch_workbench_snapshot(client, final_matter_id) or {}
        execution_snapshot = await fetch_execution_snapshot_by_session(session_id)
        execution_traces = await fetch_execution_traces_by_session(session_id)
        analysis_view = _extract_analysis_view(snapshot)
        pricing_view = _extract_pricing_view(snapshot)
        docgen_state = _extract_document_generation_state(snapshot)
        issue_items = _section_items(analysis_view, "issues")
        risk_items = _section_items(analysis_view, "risks")
        strategy_items = _section_items(analysis_view, "strategy_matrix")
        pending_card = await flow.get_pending_card()
        messages = await list_session_messages(client, session_id)
        deliverables = await list_deliverables(client, final_matter_id)
        if require_documents:
            docgen_status = _safe_str(docgen_state.get("status")).lower()
            if not _docgen_terminal(docgen_state):
                raise AssertionError(
                    "Document generation did not reach terminal state for document-requesting analysis flow "
                    f"(session_id={session_id}, matter_id={final_matter_id}, status={docgen_status or '<empty>'})"
                )
            if not deliverables and docgen_status not in {"repair_blocked", "review_pending", "blocked", "failed"}:
                raise AssertionError(
                    "Document-requesting analysis flow reached terminal state without public deliverables "
                    f"(session_id={session_id}, matter_id={final_matter_id}, status={docgen_status or '<empty>'})"
                )
        bundle_export = export_observability_bundle(
            repo_root=REPO_ROOT,
            session_id=session_id,
            matter_id=final_matter_id,
            reason="analysis_real_flow_success",
        )
        observability = await collect_flow_observability(client, matter_id=final_matter_id, session_id=session_id)
        bundle_quality = build_bundle_quality_reports(
            bundle_dir=bundle_export["bundle_dir"],
            flow_id="analysis",
            snapshot=snapshot,
            current_view=analysis_view,
            goal_completion_mode="card" if is_goal_completion_card(pending_card) else "none",
        )
        debug_refs = await collect_ai_debug_refs(
            client,
            repo_root=REPO_ROOT,
            session_id=session_id,
            matter_id=final_matter_id,
        )
        quality_summary_ref = str((bundle_quality.get("refs") or {}).get("summary") or "").strip()
        if quality_summary_ref:
            bundle_refs = debug_refs.get("bundle_refs") if isinstance(debug_refs.get("bundle_refs"), list) else []
            if quality_summary_ref not in bundle_refs:
                bundle_refs.append(quality_summary_ref)
                debug_refs["bundle_refs"] = bundle_refs
        flow_scores = build_flow_scores(
            flow_id="analysis",
            seen_cards=flow.seen_cards,
            pending_card=pending_card,
            snapshot=snapshot,
            current_view=analysis_view,
            aux_views={"pricing_view": pricing_view, "document_generation_state": docgen_state},
            deliverables=deliverables,
            deliverable_text="",
            deliverable_status=_safe_str(pricing_view.get("status")),
            observability=observability,
            bundle_quality_summary=bundle_quality,
            goal_completion_mode="card" if is_goal_completion_card(pending_card) else "none",
        )

        summary = {
            "base_url": base_url,
            "session_id": session_id,
            "matter_id": final_matter_id,
            "uploaded_file_ids": uploaded_file_ids,
            "start_event_counts": start_counts,
            "analysis_view": {
                "summary_len": len(_safe_str(analysis_view.get("summary"))),
                "issues_count": len(issue_items),
                "strategy_options_count": len(strategy_items),
                "risk_count": len(risk_items),
            },
            "pricing_plan_view": {
                "status": _safe_str(pricing_view.get("status")),
                "reviewed": bool(pricing_view.get("reviewed")),
                "pricing_mode": _safe_str(pricing_view.get("pricing_mode")),
            },
            "document_generation_state": docgen_state,
            "deliverables": {
                "count": len(deliverables),
                "keys": sorted(list(deliverables.keys())),
            },
            "pending_card": {
                "skill_id": _safe_str((pending_card or {}).get("skill_id")),
                "task_key": _safe_str((pending_card or {}).get("task_key")),
                "review_type": _safe_str((pending_card or {}).get("review_type")),
            },
            "seen_cards": len(flow.seen_cards),
            "seen_sse_rounds": len(flow.seen_sse),
            "recent_messages_count": len(messages),
            "debug_refs": debug_refs,
            "bundle_quality": bundle_quality,
            "flow_scores": flow_scores,
        }

        _write_run_status(
            supervisor,
            state="completed",
            current_step="terminal.completed",
            require_documents=require_documents,
            session_id=session_id,
            matter_id=final_matter_id,
            wait_round=wait_round,
            snapshot=snapshot,
            execution_snapshot=execution_snapshot,
            execution_traces=execution_traces,
            pending_card=pending_card,
            analysis_view=analysis_view,
            pricing_view=pricing_view,
            docgen_state=docgen_state,
            seen_cards=len(flow.seen_cards),
            seen_sse_rounds=len(flow.seen_sse),
            start_output=start_output,
        )

        write_json(out_dir / "summary.json", summary)
        write_json(out_dir / "bundle_quality.json", bundle_quality)
        write_json(out_dir / "flow_scores.json", flow_scores)
        write_json(out_dir / "snapshot.json", snapshot)
        write_json(out_dir / "analysis_view.json", analysis_view)
        write_json(out_dir / "pricing_plan_view.json", pricing_view)
        write_json(out_dir / "document_generation_state.json", docgen_state)
        write_json(out_dir / "deliverables.json", {"deliverables": deliverables})
        write_json(out_dir / "deliverables.latest.json", {"deliverables": deliverables})
        write_json(out_dir / "messages.json", {"messages": messages})
        write_json(out_dir / "execution_snapshot.json", execution_snapshot if isinstance(execution_snapshot, dict) else {})
        write_json(out_dir / "diagnostics_summary.json", debug_refs.get("diagnostics_summary") if isinstance(debug_refs.get("diagnostics_summary"), dict) else {})
        write_json(out_dir / "diagnostics_events.json", {"events": debug_refs.get("diagnostics_events") if isinstance(debug_refs.get("diagnostics_events"), list) else []})
        write_json(out_dir / "debug_refs.json", debug_refs)

    print("[done] analysis workflow completed")
    print(f"[artifacts] {out_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run analysis workflow via consultations WS (real LLM).")
    parser.add_argument("--base-url", default="", help="Gateway base URL, e.g. http://host/api/v1")
    parser.add_argument("--use-gateway", action="store_true", default=False, help="Use gateway mode instead of direct local/remote service URLs")
    parser.add_argument("--consultations-base-url", default="", help="Direct consultations-service base URL")
    parser.add_argument("--files-base-url", default="", help="Direct files-service base URL")
    parser.add_argument("--matter-base-url", default="", help="Direct matter-service base URL")
    parser.add_argument("--remote-stack-host", default="", help="Remote stack host for direct non-local services")
    parser.add_argument("--direct-user-id", default="", help="Optional direct service mode user id (skip auth only when set)")
    parser.add_argument("--direct-org-id", default="", help="Optional direct service mode organization id (skip auth only when set)")
    parser.add_argument("--direct-is-superuser", default="", help="Optional direct service mode superuser flag")
    parser.add_argument("--username", default="", help="Lawyer username")
    parser.add_argument("--password", default="", help="Lawyer password")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=220,
        help="Maximum consecutive no-progress polling rounds before failing",
    )
    parser.add_argument("--action-max-loops", type=int, default=24, help="requested_documents max_loops")
    parser.add_argument("--max-reference-refresh", type=int, default=2, help="Maximum references_refresh_partial attempts")
    parser.add_argument("--cards-only", action="store_true", default=False, help="Start analysis once, then only poll and answer cards")
    parser.add_argument("--output-dir", default="", help="Artifacts output directory")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
