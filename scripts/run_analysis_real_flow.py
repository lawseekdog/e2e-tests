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


START_CHAT_RUN: dict[str, Any] = {
    "entry_mode": "analysis",
    "service_type_id": "civil_prosecution",
    "delivery_goal": "analysis_only",
    "supporting_document_kinds": [],
}

FLOW_OVERRIDES: dict[str, Any] = {}

def _extract_analysis_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    view = snapshot.get("analysis_view") if isinstance(snapshot.get("analysis_view"), dict) else {}
    return view if isinstance(view, dict) else {}

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
        return {"current_task_id": "", "current_node": "", "phase_id": ""}
    analysis = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    identity = analysis.get("identity") if isinstance(analysis.get("identity"), dict) else {}
    runtime = analysis.get("workbench_runtime") if isinstance(analysis.get("workbench_runtime"), dict) else {}
    matter = snapshot.get("matter") if isinstance(snapshot.get("matter"), dict) else {}
    workflow = matter.get("workflow") if isinstance(matter.get("workflow"), dict) else {}
    return {
        "current_task_id": _safe_str(
            analysis.get("current_task_id") or identity.get("current_task_id") or runtime.get("current_task_id")
        ),
        "current_node": _safe_str(analysis.get("current_node") or runtime.get("current_node")),
        "phase_id": _phase_id_from_workflow(workflow),
    }


def _phase_id_from_workflow(workflow: dict[str, Any] | None) -> str:
    if not isinstance(workflow, dict):
        return ""
    raw_phases = workflow.get("phases")
    if not isinstance(raw_phases, list):
        return ""
    current_phases = [
        phase for phase in raw_phases
        if isinstance(phase, dict) and phase.get("current") is True
    ]
    if not current_phases:
        return ""
    if len(current_phases) != 1:
        raise ValueError("workflow_current_phase_invalid")
    return _safe_str(current_phases[0].get("phase_id") or current_phases[0].get("id"))


def _compact_pending_card(card: dict[str, Any] | None) -> dict[str, Any]:
    pending = card if isinstance(card, dict) else {}
    questions = pending.get("questions") if isinstance(pending.get("questions"), list) else []
    return {
        "type": _safe_str(pending.get("type")),
        "interruption_id": _safe_str(pending.get("interruption_id")),
        "interruption_key": _safe_str(pending.get("interruption_key")),
        "reason_kind": _safe_str(pending.get("reason_kind")),
        "reason_code": _safe_str(pending.get("reason_code")),
        "question_count": len(questions),
        "questions": questions,
    }


def _is_intake_card(card: dict[str, Any] | None) -> bool:
    if not isinstance(card, dict):
        return False
    return (
        _safe_str(card.get("reason_kind")).lower() == "missing_input"
        and _safe_str(card.get("reason_code")).lower() == "civil_analysis_intake"
    )


def _analysis_readiness(
    analysis_view: dict[str, Any],
    pricing_view: dict[str, Any],
) -> dict[str, Any]:
    pricing = pricing_view if isinstance(pricing_view, dict) else {}
    checks = {
        "summary_ready": bool(_safe_str(analysis_view.get("summary"))),
        "issues_ready": bool(_section_items(analysis_view, "issues")),
        "strategy_options_ready": bool(_section_items(analysis_view, "strategy_matrix")),
    }
    missing = [name for name, passed in checks.items() if not passed]
    return {
        "checks": checks,
        "optional_checks": {
            "pricing_ready": bool(_safe_str(pricing.get("status"))),
        },
        "missing_requirements": missing,
        "ready": not missing,
    }


def _progress_fingerprint(
    snapshot: dict[str, Any] | None,
    current_blocker: dict[str, Any] | None,
    analysis_view: dict[str, Any] | None,
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
    payload = {
        "runtime": _extract_runtime_progress(snapshot_obj),
        "cause_status": _safe_str(analysis_state.get("cause_status")),
        "current_subgraph": _safe_str(analysis_state.get("current_subgraph")),
        "pending_task_count": snapshot_obj.get("matter", {}).get("pending_task_count") if isinstance(snapshot_obj.get("matter"), dict) else None,
        "current_blocker": _compact_pending_card(current_blocker),
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
        if reason == "session_busy":
            return True
        if "session busy" in message or "会话正在处理中" in message:
            return True
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run analysis workflow via the unified chat run entry.")
    parser.add_argument("--base-url", default="", help="Gateway base URL, e.g. http://host/api/v1")
    parser.add_argument("--use-gateway", action="store_true", default=False, help="Use gateway mode instead of direct service URLs")
    parser.add_argument("--consultations-base-url", default="", help="Direct consultations-service base URL")
    parser.add_argument("--matter-base-url", default="", help="Direct matter-service base URL")
    parser.add_argument("--files-base-url", default="", help="Direct files-service base URL")
    parser.add_argument("--remote-stack-host", default="", help="Remote stack host for direct non-local services")
    parser.add_argument("--direct-user-id", default="", help="Optional direct service mode user id")
    parser.add_argument("--direct-org-id", default="", help="Optional direct service mode organization id")
    parser.add_argument("--direct-is-superuser", default="", help="Optional direct service mode superuser flag")
    parser.add_argument("--username", default="", help="Lawyer username")
    parser.add_argument("--password", default="", help="Lawyer password")
    parser.add_argument("--max-steps", type=int, default=220, help="Max polling steps")
    parser.add_argument("--request-max-loops", type=int, default=24, help="chat run max_loops")
    parser.add_argument("--output-dir", default="", help="Artifacts output directory")
    return parser.parse_args()
