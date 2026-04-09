"""Run legal-opinion real flow via consultations-service WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import sys

E2E_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = E2E_ROOT.parent
sys.path.insert(0, str(E2E_ROOT))

from client.api_client import ApiClient
from support.workbench.docx import extract_docx_text
from support.workbench.flow_runner import WorkbenchFlow, is_session_busy_sse

from scripts._support.flow_score_support import (
    build_flow_scores,
    collect_flow_observability,
)
from scripts._support.template_draft_real_flow_support import (
    DEFAULT_LEGAL_OPINION_EVIDENCE_RELATIVE,
    DEFAULT_LEGAL_OPINION_FACTS,
)
from scripts._support.diagnostic_bundle_support import export_failure_bundle, export_observability_bundle, format_first_bad_line
from scripts._support.quality_policy_support import build_bundle_quality_reports, merge_bundle_quality_report
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
from scripts._support.run_status import RunStatusSupervisor


DEFAULT_KICKOFF = "请基于已上传材料形成一份结构化法律意见分析，输出结论、风险与行动建议。"
_SUCCESS_STATUSES = {"completed", "archived", "done"}
_ANALYSIS_REQUESTED_DOCUMENTS: list[dict[str, str]] = [
    {"document_kind": "case_analysis_report", "instance_key": ""},
]

FLOW_OVERRIDES: dict[str, Any] = {}


def _requested_documents_for_goal(goal: str) -> list[dict[str, str]]:
    normalized = _safe_str(goal).lower()
    if normalized in {"analysis", "case_analysis"}:
        return list(_ANALYSIS_REQUESTED_DOCUMENTS)
    if normalized in {"legal_opinion", "legal_opinion_report"}:
        return [{"document_kind": "legal_opinion_report", "instance_key": ""}]
    if normalized in {"contract_review", "contract_review_report"}:
        return [{"document_kind": "contract_review_report", "instance_key": ""}]
    raise ValueError(f"unsupported_goal_requested_documents:{goal}")


def _bundle_export_unavailable_payload(*, error: Exception) -> dict[str, Any]:
    message = _safe_str(error) or "observability_bundle_unavailable"
    return {
        "bundle_dir": "",
        "summary": {
            "contract_version": "failure_summary.v1",
            "generated_at": f"{datetime.utcnow().isoformat()}Z",
            "bundle_dir": "",
            "failure_class": "tooling_unavailable",
            "primary_reason_code": "observability_bundle_unavailable",
            "reason_code_chain": ["observability_bundle_unavailable"],
            "retry_prompt": message,
            "tool_error": message,
        },
    }


def _safe_export_observability_bundle(*, repo_root: Path, session_id: str, matter_id: str, reason: str) -> dict[str, Any]:
    try:
        return export_observability_bundle(
            repo_root=repo_root,
            session_id=session_id,
            matter_id=matter_id,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        return _bundle_export_unavailable_payload(error=exc)


def _safe_export_failure_bundle(*, repo_root: Path, session_id: str, matter_id: str, reason: str) -> dict[str, Any]:
    try:
        return export_failure_bundle(
            repo_root=repo_root,
            session_id=session_id,
            matter_id=matter_id,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        return _bundle_export_unavailable_payload(error=exc)


def _safe_build_bundle_quality_reports(
    *,
    bundle_dir: str,
    flow_id: str,
    snapshot: dict[str, Any] | None,
    current_view: dict[str, Any] | None,
    goal_completion_mode: str,
) -> dict[str, Any]:
    token = _safe_str(bundle_dir)
    if not token:
        return {
            "contract_version": "bundle_quality.v1",
            "flow_id": _safe_str(flow_id),
            "score": 0,
            "passed": True,
            "hard_fail_reasons": [],
            "warnings": ["observability_bundle_unavailable"],
            "refs": {},
            "worst_node": {},
            "worst_skill": {},
            "worst_lane": {},
        }
    try:
        return build_bundle_quality_reports(
            bundle_dir=token,
            flow_id=flow_id,
            snapshot=snapshot,
            current_view=current_view,
            goal_completion_mode=goal_completion_mode,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "contract_version": "bundle_quality.v1",
            "flow_id": _safe_str(flow_id),
            "score": 0,
            "passed": True,
            "hard_fail_reasons": [],
            "warnings": [f"observability_bundle_unavailable:{_safe_str(exc)}"],
            "refs": {},
            "worst_node": {},
            "worst_skill": {},
            "worst_lane": {},
        }


def _build_kickoff_prompt(raw_kickoff: str) -> str:
    kickoff = _safe_str(raw_kickoff) or DEFAULT_KICKOFF
    if kickoff == DEFAULT_KICKOFF:
        return (
            f"{DEFAULT_KICKOFF}\n\n"
            f"以下是当前案件背景与目标，请直接基于这些事实进入法律意见分析：\n"
            f"{DEFAULT_LEGAL_OPINION_FACTS}"
        )
    return kickoff


def _extract_analysis_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    view = snapshot.get("analysis_view") if isinstance(snapshot.get("analysis_view"), dict) else {}
    return view if isinstance(view, dict) else {}


def _section_items(analysis_view: dict[str, Any], section_type: str) -> list[dict[str, Any]]:
    sections = analysis_view.get("sections") if isinstance(analysis_view.get("sections"), list) else []
    for section in sections:
        if not isinstance(section, dict):
            continue
        if _safe_str(section.get("section_type")) != section_type:
            continue
        data = section.get("data") if isinstance(section.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        return [row for row in items if isinstance(row, dict)]
    return []


def _dedupe_strings(rows: list[str]) -> list[str]:
    out: list[str] = []
    for row in rows:
        token = _safe_str(row)
        if token and token not in out:
            out.append(token)
    return out


def _extract_legal_opinion_projection(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    analysis_view = _extract_analysis_view(snapshot)
    analysis_state = _extract_analysis_state(snapshot)
    issue_items = _section_items(analysis_view, "issues")
    risk_items = _section_items(analysis_view, "risks")
    strategy_items = _section_items(analysis_view, "strategy_matrix")
    element_items = _section_items(analysis_view, "legal_elements")

    confirmed_opinions: list[dict[str, Any]] = []
    citation_matrix: list[dict[str, Any]] = []
    issue_titles: list[str] = []
    analysis_points: list[dict[str, Any]] = []
    for item in issue_items:
        issue_id = _safe_str(item.get("issue_id")) or f"opinion:{len(confirmed_opinions) + 1}"
        title = _safe_str(item.get("issue_title") or item.get("title"))
        analysis = _safe_str(item.get("analysis")) or title
        if title:
            issue_titles.append(title)
            analysis_points.append({"title": title, "content": analysis})
        authority_titles = [
            _safe_str(token)
            for token in (item.get("authority_titles") if isinstance(item.get("authority_titles"), list) else [])
            if _safe_str(token)
        ]
        laws = [token for token in authority_titles if "案例" not in token and "判" not in token]
        cases = [token for token in authority_titles if token not in laws]
        citation_matrix.append(
            {
                "issue": title or issue_id,
                "laws": laws,
                "cases": cases,
                "anchors": [
                    _safe_str(token)
                    for token in (item.get("evidence_refs") if isinstance(item.get("evidence_refs"), list) else [])
                    if _safe_str(token)
                ],
            }
        )
        confirmed_opinions.append(
            {
                "opinion_id": issue_id,
                "title": title or issue_id,
                "conclusion": analysis,
                "legal_basis": authority_titles[0] if authority_titles else "",
            }
        )

    risks = [
        {
            "title": _safe_str(item.get("title")) or "风险",
            "trigger": _safe_str(item.get("title")),
            "mitigation": _safe_str(item.get("mitigation")),
        }
        for item in risk_items
        if _safe_str(item.get("title")) or _safe_str(item.get("mitigation"))
    ]
    action_items = [
        {
            "action_item_id": _safe_str(item.get("strategy_id")) or f"action:{index + 1}",
            "title": _safe_str(item.get("title")) or f"动作{index + 1}",
            "owner": "lawyer",
            "expected_impact": _safe_str(item.get("expected_outcome") or item.get("summary")),
            "priority": item.get("priority_rank"),
            "status": "todo",
        }
        for index, item in enumerate(strategy_items)
    ]
    material_gaps = _dedupe_strings(
        [
            *[
                _safe_str(item.get("title"))
                for item in risk_items
                if _safe_str(item.get("risk_id")) == "references_coverage_gap"
            ],
            *[
                _safe_str(gap)
                for item in strategy_items
                for gap in (item.get("blocking_gaps") if isinstance(item.get("blocking_gaps"), list) else [])
            ],
        ]
    )
    fact_gaps = _dedupe_strings(
        [
            _safe_str(item.get("title"))
            for item in element_items
            if int(item.get("gap_count") or 0) > 0
        ]
    )
    next_actions = analysis_view.get("next_actions") if isinstance(analysis_view.get("next_actions"), list) else []

    return {
        "title": _safe_str(analysis_view.get("title")) or "法律意见",
        "summary": _safe_str(analysis_view.get("summary")),
        "issues": issue_titles,
        "key_rules": [],
        "analysis_points": analysis_points,
        "risks": risks,
        "action_items": action_items,
        "conclusion_targets": [
            {"status": "confirmed", **row}
            for row in confirmed_opinions
        ],
        "confirmed_opinions": confirmed_opinions,
        "material_gaps": material_gaps,
        "fact_gaps": fact_gaps,
        "missing_materials": material_gaps,
        "citation_matrix": citation_matrix,
        "next_actions": next_actions or analysis_state.get("next_actions") if isinstance(analysis_state.get("next_actions"), list) else [],
        "result_contract_diagnostics": {
            "status": "valid" if _safe_str(analysis_view.get("summary")) and confirmed_opinions and risks and action_items else "invalid"
        },
    }


def _extract_document_generation_state(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    state = snapshot.get("document_generation_state") if isinstance(snapshot.get("document_generation_state"), dict) else {}
    signals = state.get("runtime_signals") if isinstance(state.get("runtime_signals"), dict) else {}
    return {
        "status": _safe_str(state.get("status")),
        "audit_passed": bool(state.get("audit_passed")),
        "formal_gate_blocked": bool(state.get("formal_gate_blocked")),
        "formal_gate_reason_codes": [
            _safe_str(code)
            for code in (state.get("formal_gate_reason_codes") if isinstance(state.get("formal_gate_reason_codes"), list) else [])
            if _safe_str(code)
        ],
        "formal_gate_actions": [
            row for row in (state.get("formal_gate_actions") if isinstance(state.get("formal_gate_actions"), list) else [])
            if isinstance(row, dict)
        ],
        "formal_gate_summary": _safe_str(state.get("formal_gate_summary")),
        "quality_review_decision": _safe_str(state.get("quality_review_decision")),
        "template_quality_contracts_json_exists": bool(state.get("template_quality_contracts_json_exists")),
        "docgen_repair_plan_exists": bool(state.get("repair_plan_exists")),
        "docgen_repair_contracts_json_exists": bool(state.get("repair_contracts_json_exists")),
        "rendered": bool(signals.get("rendered")),
        "synced": bool(signals.get("synced")),
        "deliverable_bindings": [
            row for row in (signals.get("deliverable_bindings") if isinstance(signals.get("deliverable_bindings"), list) else [])
            if isinstance(row, dict)
        ],
        "runtime_signals": signals,
    }


def _alias_deliverables(rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows.values():
        if not isinstance(row, dict):
            continue
        aliases: list[str] = []
        for token in (
            _safe_str(row.get("output_key")),
            _safe_str(row.get("deliverable_kind")),
            _safe_str(row.get("document_kind")),
        ):
            if not token:
                continue
            if token not in aliases:
                aliases.append(token)
            if token.endswith("_report"):
                aliases.append(token.removesuffix("_report"))
            if token.endswith("_document"):
                aliases.append(token.removesuffix("_document"))
        for alias in aliases:
            if alias and alias not in out:
                out[alias] = row
    return out


def _extract_analysis_state(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    analysis = snapshot.get("analysis_state")
    return analysis if isinstance(analysis, dict) else {}


def _extract_active_scope_state(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    analysis = _extract_analysis_state(snapshot)
    goal_scopes = analysis.get("goal_scopes") if isinstance(analysis.get("goal_scopes"), dict) else {}
    active_scope_id = _safe_str(
        analysis.get("active_scope_id")
        or (analysis.get("active_scope") or {}).get("scope_id")
        if isinstance(analysis.get("active_scope"), dict)
        else analysis.get("active_scope_id")
    )
    if active_scope_id:
        scoped = goal_scopes.get(active_scope_id)
        if isinstance(scoped, dict):
            return scoped
    if len(goal_scopes) == 1:
        only_scope = next(iter(goal_scopes.values()))
        if isinstance(only_scope, dict):
            return only_scope
    return {}


def _extract_active_scope_group(snapshot: dict[str, Any] | None, group: str) -> dict[str, Any]:
    scope_state = _extract_active_scope_state(snapshot)
    value = scope_state.get(_safe_str(group))
    return value if isinstance(value, dict) else {}


def _extract_runtime_next_actions(
    snapshot: dict[str, Any] | None,
    analysis_projection: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _append_many(rows: Any) -> None:
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            token = _json_fingerprint(
                {
                    "id": _safe_str(row.get("id")),
                    "type": _safe_str(row.get("type")),
                    "goal": _safe_str(row.get("goal")),
                    "payload": payload,
                }
            )
            if token in seen:
                continue
            seen.add(token)
            actions.append(row)

    if isinstance(analysis_projection, dict):
        _append_many(analysis_projection.get("next_actions"))
    analysis = _extract_analysis_state(snapshot)
    _append_many(analysis.get("next_actions"))
    return actions


def _pick_analysis_auto_action(
    snapshot: dict[str, Any] | None,
    analysis_projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    for row in _extract_runtime_next_actions(snapshot, analysis_projection):
        if not bool(row.get("auto_trigger")):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        action_type = _safe_str(payload.get("action") or row.get("type")).lower()
        if action_type not in {"open_review_card", "set_goal"}:
            continue
        return row
    return {}


def _analysis_auto_review_card_target(action: dict[str, Any] | None) -> str:
    if not isinstance(action, dict):
        return ""
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    action_type = _safe_str(payload.get("action") or action.get("type")).lower()
    if action_type != "open_review_card":
        return ""
    target = _safe_str(payload.get("target")).lower()
    if target in {"legal_opinion_analyze", "intake", "intake_gate"}:
        return target
    return ""


def _analysis_allows_auto_review_card(snapshot: dict[str, Any] | None) -> bool:
    analysis = _extract_analysis_state(snapshot)
    task_id = _safe_str(analysis.get("current_task_id")).lower()
    scope_evidence = _extract_active_scope_group(snapshot, "evidence")
    evidence_runtime = scope_evidence.get("runtime") if isinstance(scope_evidence.get("runtime"), dict) else {}
    evidence_readiness = evidence_runtime.get("readiness") if isinstance(evidence_runtime.get("readiness"), dict) else {}
    evidence_status = _safe_str(evidence_readiness.get("status")).lower()
    evidence_next_route = _safe_str(evidence_readiness.get("next_route")).lower()
    evidence_handoff_ready = (
        evidence_status == "ready"
        and not bool(evidence_readiness.get("phase_terminal"))
        and evidence_next_route in {"", "finish", "analyze", "legal_opinion_analyze"}
    )
    if not task_id:
        return True
    if task_id in {"legal_opinion_mode_router"}:
        return False
    blocked_prefixes = (
        "evidence_",
        "fact_graph_",
        "entity_",
        "references_",
        "material_semantic_",
    )
    if task_id.startswith(blocked_prefixes):
        return evidence_handoff_ready
    if task_id.endswith(("_seed", "_parallel", "_normalize", "_assemble", "_publish_gate")):
        return evidence_handoff_ready
    return True


def _is_auto_answerable_intake_card(card: dict[str, Any] | None) -> bool:
    if not isinstance(card, dict):
        return False
    if _safe_str(card.get("skill_id")).lower() != "legal_opinion-intake-gate":
        return False
    questions = card.get("questions") if isinstance(card.get("questions"), list) else []
    return bool(questions)


def _legal_opinion_core_ready(analysis_projection: dict[str, Any] | None) -> bool:
    if not isinstance(analysis_projection, dict):
        return False
    if _safe_str(analysis_projection.get("summary")):
        return True
    for key in (
        "issues",
        "action_items",
        "risks",
        "analysis_points",
        "key_rules",
        "conclusion_targets",
    ):
        rows = analysis_projection.get(key)
        if isinstance(rows, list) and rows:
            return True
    return False


def _analysis_should_refresh_references(
    *,
    snapshot: dict[str, Any] | None,
    analysis_projection: dict[str, Any] | None,
) -> bool:
    if _analysis_reference_refresh_hint(snapshot):
        return True
    if not _legal_opinion_core_ready(analysis_projection):
        return False
    action = _pick_analysis_auto_action(snapshot, analysis_projection)
    if not action:
        return True
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    target = _safe_str(payload.get("target")).lower()
    reason_codes = [
        _safe_str(code).lower()
        for code in (payload.get("reason_codes") if isinstance(payload.get("reason_codes"), list) else [])
        if _safe_str(code)
    ]
    if target in {"legal_opinion_analyze", "intake", "intake_gate"}:
        return False
    if "legal_opinion_core_missing" in reason_codes:
        return False
    return True


def _json_fingerprint(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _is_capability_gap_card(card: dict[str, Any] | None) -> bool:
    return isinstance(card, dict) and _safe_str(card.get("skill_id")).lower() == "legal-opinion-capability-gap"


def _analysis_reference_refresh_hint(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    analysis = _extract_analysis_state(snapshot)
    scope_references = _extract_active_scope_group(snapshot, "references")
    reference_meta = scope_references.get("meta") if isinstance(scope_references.get("meta"), dict) else {}
    diagnostics = (
        analysis.get("references_diagnostics_summary")
        if isinstance(analysis.get("references_diagnostics_summary"), dict)
        else {}
    )
    current_subgraph = _safe_str(
        analysis.get("current_subgraph") or analysis.get("runtime_node_scope")
    ).lower()
    current_task_id = _safe_str(analysis.get("current_task_id")).lower()
    in_references = current_subgraph == "references" or current_task_id.startswith("references_")
    reason_codes = [
        _safe_str(code)
        for code in (
            reference_meta.get("reason_codes")
            if isinstance(reference_meta.get("reason_codes"), list)
            else []
        )
        if _safe_str(code)
    ]
    final_reason = _safe_str(diagnostics.get("final_reason") or diagnostics.get("dominant_reason_code")).lower()
    status = _safe_str(reference_meta.get("status")).lower()
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
    if status != "blocked" and final_reason not in refreshable_reasons:
        return {}
    if not in_references and status != "blocked":
        return {}
    if not (set(reason_codes) & refreshable_codes or final_reason in refreshable_reasons):
        return {}
    return {
        "status": status,
        "current_subgraph": current_subgraph,
        "current_task_id": current_task_id,
        "reason_codes": reason_codes,
        "final_reason": final_reason,
    }


def _select_question_supports_value(question: dict[str, Any], value: Any) -> bool:
    target = _safe_str(value)
    if not target:
        return False
    options = question.get("options") if isinstance(question.get("options"), list) else []
    for option in options:
        if not isinstance(option, dict):
            continue
        for candidate in (option.get("value"), option.get("id"), option.get("label")):
            if _safe_str(candidate) == target:
                return True
    return False


def _capability_gap_card_matches_overrides(card: dict[str, Any] | None, overrides: dict[str, Any]) -> bool:
    if not _is_capability_gap_card(card):
        return False
    questions = card.get("questions") if isinstance(card.get("questions"), list) else []
    required_keys = ("profile.opinion_topic_primary", "profile.opinion_subtype")
    index = {
        _safe_str(question.get("field_key")): question
        for question in questions
        if isinstance(question, dict) and _safe_str(question.get("field_key"))
    }
    for field_key in required_keys:
        value = overrides.get(field_key)
        if value is None:
            return False
        question = index.get(field_key)
        if not isinstance(question, dict):
            return False
        if not _select_question_supports_value(question, value):
            return False
    return True


def _bundle_quality_report(
    *,
    analysis_projection: dict[str, Any],
    docgen_state: dict[str, Any],
    deliverables: dict[str, dict[str, Any]],
    deliverable_text: str,
    deliverable_status: str,
) -> dict[str, Any]:
    view = analysis_projection if isinstance(analysis_projection, dict) else {}
    summary = _safe_str(view.get("summary"))
    confirmed_rows = [
        row
        for row in (
            view.get("confirmed_opinions")
            if isinstance(view.get("confirmed_opinions"), list)
            else []
        )
        if isinstance(row, dict)
    ]
    if not confirmed_rows:
        confirmed_rows = [
            row
            for row in (
                view.get("conclusion_targets")
                if isinstance(view.get("conclusion_targets"), list)
                else []
            )
            if isinstance(row, dict) and _safe_str(row.get("status")).lower() == "confirmed"
        ]
    risks = len(view.get("risks")) if isinstance(view.get("risks"), list) else 0
    actions = len(view.get("action_items")) if isinstance(view.get("action_items"), list) else 0
    material_gaps = [
        _safe_str(item)
        for item in (view.get("material_gaps") if isinstance(view.get("material_gaps"), list) else [])
        if _safe_str(item)
    ]
    fact_gaps = [
        _safe_str(item)
        for item in (view.get("fact_gaps") if isinstance(view.get("fact_gaps"), list) else [])
        if _safe_str(item)
    ]
    pollution_hits = [
        token
        for token in ("contract_dispute", "dispute_response", "陈述泳道", "证据泳道", "client")
        if token and token.lower() in "\n".join([summary, _safe_str(deliverable_text)]).lower()
    ]
    legal_opinion_row = (
        deliverables.get("legal_opinion")
        if isinstance(deliverables.get("legal_opinion"), dict)
        else {}
    )
    if not legal_opinion_row:
        legal_opinion_row = (
            deliverables.get("legal_opinion_report")
            if isinstance(deliverables.get("legal_opinion_report"), dict)
            else {}
        )

    failures: list[str] = []
    score = 0
    if len(summary) >= 60:
        score += 20
    else:
        failures.append("legal_opinion_summary_too_short")
    if confirmed_rows:
        score += 20
    else:
        failures.append("legal_opinion_confirmed_opinions_missing")
    if risks > 0:
        score += 15
    else:
        failures.append("legal_opinion_risks_missing")
    if actions > 0:
        score += 15
    else:
        failures.append("legal_opinion_action_items_missing")
    if not material_gaps and not fact_gaps:
        score += 10
    else:
        failures.append("legal_opinion_unresolved_gaps_present")
    if not pollution_hits:
        score += 10
    else:
        failures.append(f"legal_opinion_pollution:{','.join(pollution_hits)}")
    if legal_opinion_row:
        score += 5
    else:
        failures.append("legal_opinion_deliverable_missing")
    if _safe_str(deliverable_status).lower() in _SUCCESS_STATUSES:
        score += 5
    elif _safe_str(deliverable_text):
        score += 3

    return {
        "score": min(100, score),
        "passed": score >= 70 and not failures,
        "failures": failures,
        "details": {
            "confirmed_count": len(confirmed_rows),
            "risk_count": risks,
            "action_count": actions,
            "material_gap_count": len(material_gaps),
            "fact_gap_count": len(fact_gaps),
            "deliverable_status": _safe_str(deliverable_status),
            "pollution_hits": pollution_hits,
        },
    }


def _resolve_fixture_paths(rel_paths: tuple[str, ...] | list[str]) -> list[Path]:
    resolved: list[Path] = []
    seen: set[Path] = set()
    for rel in rel_paths:
        token = _safe_str(rel)
        if not token:
            continue
        candidates = [
            (REPO_ROOT / token).resolve(),
            (E2E_ROOT / token).resolve(),
        ]
        for candidate in candidates:
            if not candidate.exists() or candidate in seen:
                continue
            seen.add(candidate)
            resolved.append(candidate)
            break
    return resolved


async def _download_primary_legal_opinion_text(
    client: ApiClient,
    *,
    deliverables: dict[str, dict[str, Any]],
    out_dir: Path,
    leaf_name: str,
) -> tuple[str, str]:
    row = deliverables.get("legal_opinion") if isinstance(deliverables.get("legal_opinion"), dict) else {}
    if not row:
        row = deliverables.get("legal_opinion_report") if isinstance(deliverables.get("legal_opinion_report"), dict) else {}
    deliverable_status = _safe_str(row.get("status"))
    file_id = _safe_str(row.get("file_id"))
    if not file_id:
        return "", deliverable_status
    raw = await client.download_file_bytes(file_id)
    content = extract_docx_text(raw)
    if content:
        (out_dir / leaf_name).write_text(content, encoding="utf-8")
    return content, deliverable_status


async def _collect_round_state(
    *,
    client: ApiClient,
    flow: WorkbenchFlow,
    session_id: str,
    out_dir: Path,
    round_no: int,
    round_label: str,
    goal_completion_mode: str,
) -> dict[str, Any]:
    await flow.refresh()
    matter_id = _safe_str(flow.matter_id)
    snapshot = await fetch_workbench_snapshot(client, matter_id) if matter_id else {}
    execution_snapshot = await fetch_execution_snapshot_by_session(session_id)
    execution_traces = await fetch_execution_traces_by_session(session_id)
    analysis_projection = _extract_legal_opinion_projection(snapshot)
    docgen_state = _extract_document_generation_state(snapshot)
    pending_card = await flow.get_pending_card()
    deliverables = _alias_deliverables(await list_deliverables(client, matter_id)) if matter_id else {}
    messages = await list_session_messages(client, session_id)
    deliverable_text, deliverable_status = await _download_primary_legal_opinion_text(
        client,
        deliverables=deliverables,
        out_dir=out_dir,
        leaf_name=f"{round_no:02d}.{round_label}.legal_opinion.txt",
    )
    bundle_export = _safe_export_observability_bundle(
        repo_root=REPO_ROOT,
        session_id=session_id,
        matter_id=matter_id,
        reason=f"legal_opinion_{round_label}",
    )
    observability = await collect_flow_observability(client, matter_id=matter_id, session_id=session_id)
    base_bundle_quality = _bundle_quality_report(
        analysis_projection=analysis_projection,
        docgen_state=docgen_state,
        deliverables=deliverables,
        deliverable_text=deliverable_text,
        deliverable_status=deliverable_status,
    )
    quality_summary = _safe_build_bundle_quality_reports(
        bundle_dir=bundle_export["bundle_dir"],
        flow_id="legal_opinion",
        snapshot=snapshot,
        current_view=analysis_projection,
        goal_completion_mode=goal_completion_mode,
    )
    bundle_quality = merge_bundle_quality_report(
        base_report=base_bundle_quality,
        quality_summary=quality_summary,
    )
    debug_refs = await collect_ai_debug_refs(
        client,
        repo_root=REPO_ROOT,
        session_id=session_id,
        matter_id=matter_id,
    )
    quality_summary_ref = str((quality_summary.get("refs") or {}).get("summary") or "").strip()
    if quality_summary_ref:
        bundle_refs = debug_refs.get("bundle_refs") if isinstance(debug_refs.get("bundle_refs"), list) else []
        if quality_summary_ref not in bundle_refs:
            bundle_refs.append(quality_summary_ref)
            debug_refs["bundle_refs"] = bundle_refs
    flow_scores = build_flow_scores(
        flow_id="legal_opinion",
        seen_cards=flow.seen_cards,
        pending_card=pending_card,
        snapshot=snapshot,
        current_view=analysis_projection,
        aux_views={"document_generation_state": docgen_state},
        deliverables=deliverables,
        deliverable_text=deliverable_text,
        deliverable_status=deliverable_status,
        observability=observability,
        bundle_quality_summary=quality_summary,
        goal_completion_mode=goal_completion_mode,
    )
    prefix = f"{round_no:02d}.{round_label}"
    write_json(out_dir / f"{prefix}.snapshot.json", snapshot if isinstance(snapshot, dict) else {})
    write_json(out_dir / f"{prefix}.execution_snapshot.json", execution_snapshot if isinstance(execution_snapshot, dict) else {})
    write_json(out_dir / f"{prefix}.execution_traces.json", {"traces": execution_traces if isinstance(execution_traces, list) else []})
    write_json(out_dir / f"{prefix}.analysis_projection.json", analysis_projection)
    write_json(out_dir / f"{prefix}.document_generation_state.json", docgen_state)
    write_json(out_dir / f"{prefix}.deliverables.json", deliverables)
    write_json(out_dir / f"{prefix}.messages.json", {"messages": messages})
    write_json(
        out_dir / f"{prefix}.diagnostics_summary.json",
        debug_refs.get("diagnostics_summary") if isinstance(debug_refs.get("diagnostics_summary"), dict) else {},
    )
    write_json(
        out_dir / f"{prefix}.diagnostics_events.json",
        {"events": debug_refs.get("diagnostics_events") if isinstance(debug_refs.get("diagnostics_events"), list) else []},
    )
    write_json(out_dir / f"{prefix}.debug_refs.json", debug_refs)
    write_json(out_dir / f"{prefix}.bundle_quality.json", bundle_quality)
    write_json(out_dir / f"{prefix}.flow_scores.json", flow_scores)
    if pending_card:
        write_json(out_dir / f"{prefix}.pending_card.json", pending_card)
    return {
        "matter_id": matter_id,
        "snapshot": snapshot,
        "execution_snapshot": execution_snapshot,
        "execution_traces": execution_traces,
        "analysis_projection": analysis_projection,
        "docgen_state": docgen_state,
        "pending_card": pending_card,
        "deliverables": deliverables,
        "deliverable_text": deliverable_text,
        "deliverable_status": deliverable_status,
        "messages": messages,
        "debug_refs": debug_refs,
        "bundle_quality": bundle_quality,
        "flow_scores": flow_scores,
    }


async def _persist_action_sse(out_dir: Path, round_no: int, label: str, sse: dict[str, Any]) -> None:
    write_json(out_dir / f"{round_no:02d}.{label}.sse.json", sse if isinstance(sse, dict) else {})


async def run(args: argparse.Namespace) -> int:
    load_real_flow_env(repo_root=REPO_ROOT, e2e_root=E2E_ROOT)
    terminate_stale_script_runs(script_name="run_legal_opinion_real_flow.py")

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
    kickoff = _build_kickoff_prompt(_safe_str(args.kickoff))

    evidence_paths = _resolve_fixture_paths(list(DEFAULT_LEGAL_OPINION_EVIDENCE_RELATIVE))
    if not evidence_paths:
        raise RuntimeError(
            "No legal-opinion evidence fixtures found; expected at least one file under "
            f"{REPO_ROOT / 'e2e-tests' / 'scripts' / '_support' / 'fixtures'}"
        )

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = resolve_output_dir(
        repo_root=REPO_ROOT,
        output_dir=_safe_str(args.output_dir),
        default_leaf=f"output/legal-opinion-chain/{ts}",
    )
    supervisor = RunStatusSupervisor(out_dir=out_dir, flow_id="legal_opinion")
    supervisor.update(status="booting", current_step="bootstrap.init", next_action="login")

    print(f"[config] base_url={base_url}")
    print(f"[config] direct_service_mode={direct_mode}")
    if direct_mode:
        print(f"[config] auth_base_url={direct_config.get('auth_base_url') or '-'}")
        print(f"[config] consultations_base_url={direct_config.get('consultations_base_url') or '-'}")
        print(f"[config] matter_base_url={direct_config.get('matter_base_url') or '-'}")
        print(f"[config] files_base_url={direct_config.get('files_base_url') or '-'}")
    else:
        print("[config] gateway_mode=true")
    print(f"[config] user={username}")
    print(f"[config] output_dir={out_dir}")

    async with ApiClient(base_url) as client:
        await client.login(username, password)
        print(f"[login] ok user_id={client.user_id} org_id={client.organization_id}")
        supervisor.update(status="booting", current_step="bootstrap.login", next_action="upload_files")

        uploaded_file_ids = await upload_consultation_files(client, evidence_paths)
        for path, fid in zip(evidence_paths, uploaded_file_ids):
            print(f"[upload] ok file={path.name} file_id={fid}")
        supervisor.update(
            status="booting",
            current_step="bootstrap.upload_files",
            next_action="create_session",
            extra={"uploaded_file_ids": uploaded_file_ids},
        )

        flow, session_id, matter_id = await bootstrap_flow(
            client=client,
            service_type_id="legal_opinion",
            client_role="applicant",
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
            next_action="kickoff",
        )

        supervisor.update(
            status="running",
            current_step="kickoff.submitting",
            session_id=session_id,
            matter_id=_safe_str(flow.matter_id) or matter_id,
            next_action="await_kickoff_events",
        )
        kickoff_sse = await flow.request_documents(
            _requested_documents_for_goal("legal_opinion"),
            user_query=kickoff,
            attachments=uploaded_file_ids,
            max_loops=max(1, int(args.kickoff_max_loops)),
            settle_mode="fire_and_poll",
            label="legal_opinion_report",
        )
        kickoff_counts = event_counts(kickoff_sse if isinstance(kickoff_sse, dict) else {})
        write_json(out_dir / "00.kickoff.sse.json", kickoff_sse if isinstance(kickoff_sse, dict) else {})
        supervisor.update(
            status="running",
            current_step="kickoff.completed",
            session_id=session_id,
            matter_id=_safe_str(flow.matter_id) or matter_id,
            next_action="wait_analysis_ready",
            extra={"kickoff_event_counts": kickoff_counts},
        )

        kickoff_card = await flow.actionable_card_from_sse(kickoff_sse if isinstance(kickoff_sse, dict) else {})
        if _is_capability_gap_card(kickoff_card):
            if not _capability_gap_card_matches_overrides(kickoff_card, FLOW_OVERRIDES):
                await flow.refresh()
                gap_round = await _collect_round_state(
                    client=client,
                    flow=flow,
                    session_id=session_id,
                    out_dir=out_dir,
                    round_no=1,
                    round_label="capability_gap",
                    goal_completion_mode="card",
                )
                summary = {
                    "status": "capability_gap",
                    "pending_card": gap_round["pending_card"] or kickoff_card,
                    "flow_scores": gap_round["flow_scores"],
                    "bundle_quality": gap_round["bundle_quality"],
                }
                write_json(out_dir / "summary.json", summary)
                supervisor.update(
                    status="blocked",
                    current_step="terminal.capability_gap",
                    session_id=session_id,
                    matter_id=_safe_str(flow.matter_id),
                    snapshot=gap_round["snapshot"] if isinstance(gap_round.get("snapshot"), dict) else {},
                    execution_snapshot=gap_round["execution_snapshot"] if isinstance(gap_round.get("execution_snapshot"), dict) else None,
                    pending_card=gap_round["pending_card"] if isinstance(gap_round.get("pending_card"), dict) else kickoff_card,
                    current_blocker="capability_gap",
                    next_action="inspect_summary",
                    artifact_refs={"summary": str(out_dir / "summary.json")},
                )
                print("[result] capability_gap")
                print(f"[artifacts] {out_dir}")
                return 3

        def _analysis_round_ready(round_state: dict[str, Any]) -> bool:
            pending = round_state["pending_card"] if isinstance(round_state.get("pending_card"), dict) else {}
            if is_goal_completion_card(pending):
                return True
            if _is_capability_gap_card(pending) and not _capability_gap_card_matches_overrides(pending, FLOW_OVERRIDES):
                return True
            view = round_state["analysis_projection"] if isinstance(round_state.get("analysis_projection"), dict) else {}
            issues = view.get("issues") if isinstance(view.get("issues"), list) else []
            action_items = view.get("action_items") if isinstance(view.get("action_items"), list) else []
            risks = view.get("risks") if isinstance(view.get("risks"), list) else []
            return bool(_safe_str(view.get("summary")) and (issues or action_items or risks))

        analysis_round: dict[str, Any] | None = None
        analysis_reference_refresh_attempts = 0
        analysis_action_cooldown = 0
        analysis_progress_token = ""
        for step_no in range(1, max(1, int(args.max_steps)) + 1):
            analysis_round = await _collect_round_state(
                client=client,
                flow=flow,
                session_id=session_id,
                out_dir=out_dir,
                round_no=step_no,
                round_label="analysis_poll",
                goal_completion_mode="card",
            )
            analysis_execution_snapshot = (
                analysis_round.get("execution_snapshot")
                if isinstance(analysis_round.get("execution_snapshot"), dict)
                else {}
            )
            current_progress_token = _json_fingerprint(
                {
                    "task": _safe_str(analysis_round["snapshot"].get("analysis_state", {}).get("current_task_id"))
                    if isinstance(analysis_round.get("snapshot"), dict)
                    else "",
                    "subgraph": _safe_str(analysis_round["snapshot"].get("analysis_state", {}).get("current_subgraph"))
                    if isinstance(analysis_round.get("snapshot"), dict)
                    else "",
                    "progress_pct": analysis_round["snapshot"].get("analysis_state", {}).get("progress_pct")
                    if isinstance(analysis_round.get("snapshot"), dict)
                    and isinstance(analysis_round["snapshot"].get("analysis_state"), dict)
                    else None,
                    "deliverable_keys": sorted((analysis_round.get("deliverables") or {}).keys()),
                }
            )
            if current_progress_token != analysis_progress_token:
                analysis_progress_token = current_progress_token
                analysis_action_cooldown = 0
            analysis_pending_card = analysis_round["pending_card"] if isinstance(analysis_round.get("pending_card"), dict) else {}
            analysis_ready = _analysis_round_ready(analysis_round)
            analysis_projection_row = (
                analysis_round["analysis_projection"]
                if isinstance(analysis_round.get("analysis_projection"), dict)
                else {}
            )
            supervisor.update(
                status="ready" if analysis_ready else "running",
                current_step="poll.analysis_ready",
                session_id=session_id,
                matter_id=_safe_str(flow.matter_id),
                snapshot=analysis_round["snapshot"] if isinstance(analysis_round.get("snapshot"), dict) else {},
                execution_snapshot=analysis_execution_snapshot,
                execution_traces=analysis_round["execution_traces"] if isinstance(analysis_round.get("execution_traces"), list) else None,
                pending_card=analysis_pending_card,
                current_blocker=_safe_str(analysis_pending_card.get("task_key")) or ("analysis_not_ready" if not analysis_ready else ""),
                next_action="collect_final_outputs" if analysis_ready else "continue_poll",
                extra={
                    "deliverable_keys": sorted((analysis_round.get("deliverables") or {}).keys()),
                    "issues_count": len(analysis_projection_row.get("issues")) if isinstance(analysis_projection_row.get("issues"), list) else 0,
                    "risk_count": len(analysis_projection_row.get("risks")) if isinstance(analysis_projection_row.get("risks"), list) else 0,
                    "action_items_count": len(analysis_projection_row.get("action_items")) if isinstance(analysis_projection_row.get("action_items"), list) else 0,
                },
            )
            if analysis_ready:
                break
            analysis_snapshot = analysis_round.get("snapshot")
            analysis_projection = analysis_round.get("analysis_projection")
            pending_card = analysis_pending_card
            if _is_auto_answerable_intake_card(pending_card):
                summary = {
                    "status": "unexpected_intake_card",
                    "pending_card": pending_card,
                    "flow_scores": analysis_round["flow_scores"],
                    "bundle_quality": analysis_round["bundle_quality"],
                }
                write_json(out_dir / "summary.json", summary)
                supervisor.update(
                    status="blocked",
                    current_step="terminal.unexpected_intake_card",
                    session_id=session_id,
                    matter_id=_safe_str(flow.matter_id),
                    snapshot=analysis_round["snapshot"] if isinstance(analysis_round.get("snapshot"), dict) else {},
                    execution_snapshot=analysis_execution_snapshot,
                    execution_traces=analysis_round["execution_traces"] if isinstance(analysis_round.get("execution_traces"), list) else None,
                    pending_card=pending_card,
                    current_blocker="unexpected_intake_card",
                    next_action="inspect_summary",
                    artifact_refs={"summary": str(out_dir / "summary.json")},
                )
                print("[result] unexpected_intake_card")
                print(f"[artifacts] {out_dir}")
                return 4
            auto_action = _pick_analysis_auto_action(analysis_snapshot, analysis_projection)
            if auto_action and analysis_action_cooldown <= 0 and _analysis_allows_auto_review_card(analysis_snapshot):
                payload = auto_action.get("payload") if isinstance(auto_action.get("payload"), dict) else {}
                action_type = _safe_str(payload.get("action") or auto_action.get("type")).lower()
                if action_type == "set_goal":
                    next_goal = _safe_str(payload.get("goal") or auto_action.get("goal")).lower()
                    if next_goal:
                        sse = await flow.request_documents(
                            _requested_documents_for_goal(next_goal),
                            max_loops=max(12, int(args.action_max_loops)),
                            settle_mode="fire_and_poll",
                            label=f"goal:{next_goal}",
                        )
                        await _persist_action_sse(out_dir, step_no, "analysis_auto_set_goal", sse)
                        await asyncio.sleep(float(args.step_sleep_s))
                        continue
                target = _analysis_auto_review_card_target(auto_action)
                if target:
                    sse = await flow.step()
                    if isinstance(sse, dict):
                        await _persist_action_sse(out_dir, step_no, f"analysis_auto_{target}", sse)
                        if is_session_busy_sse(sse):
                            analysis_action_cooldown = 5
                    await asyncio.sleep(float(args.step_sleep_s))
                    continue
            elif analysis_action_cooldown > 0:
                analysis_action_cooldown -= 1
            allow_reference_refresh = _analysis_should_refresh_references(
                snapshot=analysis_snapshot,
                analysis_projection=analysis_projection,
            )
            if (
                _safe_str(pending_card.get("skill_id")).lower() == "reference-grounding"
                and allow_reference_refresh
                and analysis_reference_refresh_attempts < int(args.max_reference_refresh)
            ):
                sse = await flow.request_documents(
                    _ANALYSIS_REQUESTED_DOCUMENTS,
                    max_loops=max(12, int(args.action_max_loops)),
                    settle_mode="fire_and_poll",
                    label="analysis_reference_refresh",
                )
                await _persist_action_sse(out_dir, step_no, "analysis_reference_refresh", sse)
                analysis_reference_refresh_attempts += 1
                await asyncio.sleep(float(args.step_sleep_s))
                continue
            refresh_hint = _analysis_reference_refresh_hint(analysis_round.get("snapshot"))
            if (
                refresh_hint
                and allow_reference_refresh
                and analysis_reference_refresh_attempts < int(args.max_reference_refresh)
            ):
                sse = await flow.request_documents(
                    _ANALYSIS_REQUESTED_DOCUMENTS,
                    max_loops=max(12, int(args.action_max_loops)),
                    settle_mode="fire_and_poll",
                    label="analysis_reference_refresh",
                )
                await _persist_action_sse(out_dir, step_no, "analysis_reference_refresh", sse)
                analysis_reference_refresh_attempts += 1
                await asyncio.sleep(float(args.step_sleep_s))
                continue
            sse = await flow.step()
            if isinstance(sse, dict):
                await _persist_action_sse(out_dir, step_no, "analysis_step", sse)
            await asyncio.sleep(float(args.step_sleep_s))
        else:
            bundle = _safe_export_failure_bundle(
                repo_root=REPO_ROOT,
                session_id=session_id,
                matter_id=_safe_str(flow.matter_id),
                reason="legal_opinion_analysis_not_ready",
            )
            bundle_quality = _safe_build_bundle_quality_reports(
                bundle_dir=bundle["bundle_dir"],
                flow_id="legal_opinion",
                snapshot=await fetch_workbench_snapshot(client, _safe_str(flow.matter_id)) if _safe_str(flow.matter_id) else {},
                current_view={},
                goal_completion_mode="none",
            )
            write_json(out_dir / "failure_summary.json", bundle["summary"])
            write_json(out_dir / "bundle_quality.failure.json", bundle_quality)
            fail_execution_snapshot = await fetch_execution_snapshot_by_session(session_id)
            fail_execution_traces = await fetch_execution_traces_by_session(session_id)
            supervisor.update(
                status="failed",
                current_step="terminal.failed",
                session_id=session_id,
                matter_id=_safe_str(flow.matter_id),
                execution_snapshot=fail_execution_snapshot,
                execution_traces=fail_execution_traces,
                current_blocker="legal_opinion_analysis_not_ready",
                next_action="inspect_failure_summary",
                error=f"Failed to reach legal opinion analysis ready after {int(args.max_steps)} steps",
                artifact_refs={"failure_summary": str(out_dir / "failure_summary.json")},
            )
            print(format_first_bad_line(bundle["summary"]))
            raise AssertionError(
                f"Failed to reach legal opinion analysis ready after {int(args.max_steps)} steps "
                f"(session_id={session_id}, matter_id={flow.matter_id})"
            )

        analysis_round = await _collect_round_state(
            client=client,
            flow=flow,
            session_id=session_id,
            out_dir=out_dir,
            round_no=1,
            round_label="analysis_ready",
            goal_completion_mode="card",
        )
        if _is_capability_gap_card(analysis_round["pending_card"]):
            summary = {
                "status": "capability_gap",
                "pending_card": analysis_round["pending_card"],
                "flow_scores": analysis_round["flow_scores"],
                "bundle_quality": analysis_round["bundle_quality"],
            }
            write_json(out_dir / "summary.json", summary)
            supervisor.update(
                status="blocked",
                current_step="terminal.capability_gap",
                session_id=session_id,
                matter_id=_safe_str(analysis_round["matter_id"]),
                snapshot=analysis_round["snapshot"] if isinstance(analysis_round.get("snapshot"), dict) else {},
                execution_snapshot=analysis_round["execution_snapshot"] if isinstance(analysis_round.get("execution_snapshot"), dict) else None,
                execution_traces=analysis_round["execution_traces"] if isinstance(analysis_round.get("execution_traces"), list) else None,
                pending_card=analysis_round["pending_card"] if isinstance(analysis_round.get("pending_card"), dict) else None,
                current_blocker="capability_gap",
                next_action="inspect_summary",
                artifact_refs={"summary": str(out_dir / "summary.json")},
            )
            print("[result] capability_gap")
            print(f"[artifacts] {out_dir}")
            return 3

        final_round = analysis_round
        ready_pending_card = (
            analysis_round["pending_card"]
            if isinstance(analysis_round.get("pending_card"), dict)
            else {}
        )
        summary = {
            "base_url": base_url,
            "session_id": session_id,
            "matter_id": final_round["matter_id"],
            "uploaded_file_ids": uploaded_file_ids,
            "kickoff_event_counts": kickoff_counts,
            "status": "analysis_ready",
            "analysis_flow_scores": analysis_round["flow_scores"],
            "final_flow_scores": final_round["flow_scores"],
            "final_bundle_quality": final_round["bundle_quality"],
            "deliverable_keys": sorted(final_round["deliverables"].keys()),
            "analysis_projection": {
                "summary_len": len(_safe_str(final_round["analysis_projection"].get("summary"))),
                "issues_count": len(final_round["analysis_projection"].get("issues")) if isinstance(final_round["analysis_projection"].get("issues"), list) else 0,
                "risk_count": len(final_round["analysis_projection"].get("risks")) if isinstance(final_round["analysis_projection"].get("risks"), list) else 0,
                "action_items_count": len(final_round["analysis_projection"].get("action_items")) if isinstance(final_round["analysis_projection"].get("action_items"), list) else 0,
            },
            "document_generation_state": final_round["docgen_state"],
            "analysis_reference_refresh_attempts": analysis_reference_refresh_attempts,
            "goal_completion_card_present": is_goal_completion_card(ready_pending_card),
            "pending_card": ready_pending_card,
            "execution_status": _safe_str((final_round.get("execution_snapshot") or {}).get("status")),
            "execution_phase_id": _safe_str((final_round.get("execution_snapshot") or {}).get("current_phase_id")),
            "success": True,
        }
        write_json(out_dir / "summary.json", summary)
        write_json(
            out_dir / "execution_snapshot.json",
            final_round["execution_snapshot"] if isinstance(final_round.get("execution_snapshot"), dict) else {},
        )
        supervisor.update(
            status="completed",
            current_step="terminal.completed",
            session_id=session_id,
            matter_id=_safe_str(final_round["matter_id"]),
            snapshot=final_round["snapshot"] if isinstance(final_round.get("snapshot"), dict) else {},
            execution_snapshot=final_round["execution_snapshot"] if isinstance(final_round.get("execution_snapshot"), dict) else None,
            execution_traces=final_round["execution_traces"] if isinstance(final_round.get("execution_traces"), list) else None,
            pending_card=ready_pending_card,
            next_action="inspect_summary",
            artifact_refs={"summary": str(out_dir / "summary.json")},
            extra={"deliverable_keys": sorted(final_round["deliverables"].keys())},
        )

    print("[done] legal opinion workflow completed")
    print(f"[artifacts] {out_dir}")
    print("[result] analysis_ready")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run legal opinion workflow via consultations WS until analysis/report is ready.")
    parser.add_argument("--base-url", default="", help="Gateway base URL, e.g. http://host/api/v1")
    parser.add_argument("--use-gateway", action="store_true", default=False, help="Use gateway mode instead of direct service URLs")
    parser.add_argument("--consultations-base-url", default="", help="Override consultations-service base URL, e.g. http://127.0.0.1:18021/api/v1")
    parser.add_argument("--matter-base-url", default="", help="Override matter-service base URL, e.g. http://127.0.0.1:18020/api/v1")
    parser.add_argument("--files-base-url", default="", help="Override files-service base URL")
    parser.add_argument("--remote-stack-host", default="", help="Remote stack host for direct non-local services")
    parser.add_argument("--direct-user-id", default="", help="Optional direct service mode user id (skip auth only when set)")
    parser.add_argument("--direct-org-id", default="", help="Optional direct service mode organization id (skip auth only when set)")
    parser.add_argument("--direct-is-superuser", default="", help="Optional direct service mode superuser flag")
    parser.add_argument("--username", default="", help="Lawyer username")
    parser.add_argument("--password", default="", help="Lawyer password")
    parser.add_argument("--kickoff", default=DEFAULT_KICKOFF, help="Initial user query")
    parser.add_argument("--kickoff-max-loops", type=int, default=24, help="Kickoff max_loops")
    parser.add_argument("--max-steps", type=int, default=220, help="Max steps until analysis is ready")
    parser.add_argument("--action-max-loops", type=int, default=24, help="requested_documents max_loops")
    parser.add_argument("--max-reference-refresh", type=int, default=2, help="Maximum references_refresh_partial attempts")
    parser.add_argument("--step-sleep-s", type=float, default=1.0, help="Sleep between analysis rounds")
    parser.add_argument("--cards-only", action="store_true", default=False, help="Reserved compatibility flag")
    parser.add_argument("--output-dir", default="", help="Artifacts output directory")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
