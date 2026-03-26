"""Run legal-opinion -> formal document_generation workflow via consultations-service WebSocket."""

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
    build_legal_opinion_formal_ready_report,
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


DEFAULT_KICKOFF = "请基于已上传材料形成一份结构化法律意见分析，输出结论、风险与行动建议。"
_GOAL_DOCGEN = "document_generation"
_SUCCESS_STATUSES = {"completed", "archived", "done"}

FLOW_OVERRIDES = {
    "profile.service_type_id": "legal_opinion",
    "profile.client_role": "applicant",
    "client_role": "applicant",
    "profile.summary": "服务器采购合同履约争议，需要形成法律意见分析。",
    "profile.background": DEFAULT_LEGAL_OPINION_FACTS,
    "profile.facts": DEFAULT_LEGAL_OPINION_FACTS,
    "profile.legal_issue": "暂停付款、逾期交付责任、质量责任、解除与索赔边界。",
    "profile.opinion_topic_primary": "contract_dispute",
    "profile.opinion_subtype": "dispute_response",
}


def _extract_goal_view(snapshot: dict[str, Any] | None, view_id: str) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    analysis = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    direct = analysis.get(view_id) if isinstance(analysis.get(view_id), dict) else {}
    if direct:
        return direct
    goals = analysis.get("goal_views") if isinstance(analysis.get("goal_views"), dict) else {}
    goal_view = goals.get(view_id) if isinstance(goals.get(view_id), dict) else {}
    if goal_view:
        return goal_view
    top_level = snapshot.get("goal_views") if isinstance(snapshot.get("goal_views"), dict) else {}
    view = top_level.get(view_id) if isinstance(top_level.get(view_id), dict) else {}
    return view if isinstance(view, dict) else {}


def _extract_legal_opinion_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    return _extract_goal_view(snapshot, "legal_opinion_view")


def _extract_document_generation_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    return _extract_goal_view(snapshot, "document_generation_view")


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
    legal_view: dict[str, Any] | None = None,
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

    if isinstance(legal_view, dict):
        _append_many(legal_view.get("next_actions"))
    analysis = _extract_analysis_state(snapshot)
    _append_many(analysis.get("next_actions"))
    return actions


def _pick_analysis_auto_action(
    snapshot: dict[str, Any] | None,
    legal_view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    for row in _extract_runtime_next_actions(snapshot, legal_view):
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


def _legal_opinion_core_ready(legal_view: dict[str, Any] | None) -> bool:
    if not isinstance(legal_view, dict):
        return False
    if _safe_str(legal_view.get("summary")):
        return True
    for key in (
        "issues",
        "action_items",
        "risks",
        "analysis_points",
        "key_rules",
        "conclusion_targets",
    ):
        rows = legal_view.get(key)
        if isinstance(rows, list) and rows:
            return True
    return False


def _analysis_should_refresh_references(
    *,
    snapshot: dict[str, Any] | None,
    legal_view: dict[str, Any] | None,
) -> bool:
    if _analysis_reference_refresh_hint(snapshot):
        return True
    if not _legal_opinion_core_ready(legal_view):
        return False
    action = _pick_analysis_auto_action(snapshot, legal_view)
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


def _is_reference_related(reason_codes: list[str]) -> bool:
    tokens = {_safe_str(code).lower() for code in reason_codes if _safe_str(code)}
    return bool(
        tokens
        & {
            "formal_opinion_authority_pending",
            "formal_opinion_references_not_ready",
            "formal_opinion_confirmed_missing_legal_refs",
        }
    )


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


def _formal_doc_ready(docgen_view: dict[str, Any], formal_ready: dict[str, Any]) -> bool:
    status = _safe_str(docgen_view.get("status")).lower()
    quality_decision = _safe_str(docgen_view.get("quality_review_decision")).lower()
    audit_passed = bool(docgen_view.get("audit_passed")) or quality_decision == "pass"
    deliverable_bindings = docgen_view.get("deliverable_bindings") if isinstance(docgen_view.get("deliverable_bindings"), list) else []
    has_file = any(
        isinstance(row, dict) and _safe_str(row.get("file_id")) and _safe_str(row.get("status")).lower() in _SUCCESS_STATUSES
        for row in deliverable_bindings
    )
    return (
        status == "document_ready"
        and not bool(docgen_view.get("formal_gate_blocked"))
        and bool(docgen_view.get("rendered"))
        and bool(docgen_view.get("synced"))
        and audit_passed
        and has_file
        and bool(formal_ready.get("passed"))
    )


def _bundle_quality_report(
    *,
    legal_view: dict[str, Any],
    docgen_view: dict[str, Any],
    deliverables: dict[str, dict[str, Any]],
    deliverable_text: str,
    deliverable_status: str,
) -> dict[str, Any]:
    return build_legal_opinion_formal_ready_report(
        current_view=legal_view,
        aux_views={"document_generation_view": docgen_view},
        deliverable_text=deliverable_text,
        deliverable_status=deliverable_status,
    )


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
    legal_view = _extract_legal_opinion_view(snapshot)
    docgen_view = _extract_document_generation_view(snapshot)
    pending_card = await flow.get_pending_card()
    deliverables = await list_deliverables(client, matter_id) if matter_id else {}
    messages = await list_session_messages(client, session_id)
    deliverable_text, deliverable_status = await _download_primary_legal_opinion_text(
        client,
        deliverables=deliverables,
        out_dir=out_dir,
        leaf_name=f"{round_no:02d}.{round_label}.legal_opinion.txt",
    )
    bundle_export = export_observability_bundle(
        repo_root=REPO_ROOT,
        session_id=session_id,
        matter_id=matter_id,
        reason=f"legal_opinion_{round_label}",
    )
    observability = await collect_flow_observability(client, matter_id=matter_id, session_id=session_id)
    base_bundle_quality = _bundle_quality_report(
        legal_view=legal_view,
        docgen_view=docgen_view,
        deliverables=deliverables,
        deliverable_text=deliverable_text,
        deliverable_status=deliverable_status,
    )
    quality_summary = build_bundle_quality_reports(
        repo_root=REPO_ROOT,
        bundle_dir=bundle_export["bundle_dir"],
        flow_id="legal_opinion",
        snapshot=snapshot,
        current_view=legal_view,
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
        current_view=legal_view,
        aux_views={"document_generation_view": docgen_view},
        deliverables=deliverables,
        deliverable_text=deliverable_text,
        deliverable_status=deliverable_status,
        observability=observability,
        bundle_quality_summary=quality_summary,
        goal_completion_mode=goal_completion_mode,
    )
    prefix = f"{round_no:02d}.{round_label}"
    write_json(out_dir / f"{prefix}.snapshot.json", snapshot if isinstance(snapshot, dict) else {})
    write_json(out_dir / f"{prefix}.legal_opinion_view.json", legal_view)
    write_json(out_dir / f"{prefix}.document_generation_view.json", docgen_view)
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
        "legal_view": legal_view,
        "docgen_view": docgen_view,
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
    kickoff = _safe_str(args.kickoff) or DEFAULT_KICKOFF

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

        uploaded_file_ids = await upload_consultation_files(client, evidence_paths)
        for path, fid in zip(evidence_paths, uploaded_file_ids):
            print(f"[upload] ok file={path.name} file_id={fid}")

        flow, session_id, matter_id = await bootstrap_flow(
            client=client,
            service_type_id="legal_opinion",
            client_role="applicant",
            uploaded_file_ids=uploaded_file_ids,
            overrides=FLOW_OVERRIDES,
        )
        print(f"[session] id={session_id} matter_id={matter_id or '-'}")

        kickoff_sse = await flow.nudge(kickoff, attachments=uploaded_file_ids, max_loops=max(1, int(args.kickoff_max_loops)))
        kickoff_counts = event_counts(kickoff_sse if isinstance(kickoff_sse, dict) else {})
        write_json(out_dir / "00.kickoff.sse.json", kickoff_sse if isinstance(kickoff_sse, dict) else {})

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
                print("[result] capability_gap")
                print(f"[artifacts] {out_dir}")
                return 3

        def _analysis_round_ready(round_state: dict[str, Any]) -> bool:
            pending = round_state["pending_card"] if isinstance(round_state.get("pending_card"), dict) else {}
            if is_goal_completion_card(pending):
                return True
            if _is_capability_gap_card(pending) and not _capability_gap_card_matches_overrides(pending, FLOW_OVERRIDES):
                return True
            view = round_state["legal_view"] if isinstance(round_state.get("legal_view"), dict) else {}
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
            if _analysis_round_ready(analysis_round):
                break
            analysis_snapshot = analysis_round.get("snapshot")
            analysis_legal_view = analysis_round.get("legal_view")
            auto_action = _pick_analysis_auto_action(analysis_snapshot, analysis_legal_view)
            if auto_action and analysis_action_cooldown <= 0 and _analysis_allows_auto_review_card(analysis_snapshot):
                payload = auto_action.get("payload") if isinstance(auto_action.get("payload"), dict) else {}
                action_type = _safe_str(payload.get("action") or auto_action.get("type")).lower()
                if action_type == "set_goal":
                    next_goal = _safe_str(payload.get("goal") or auto_action.get("goal")).lower()
                    if next_goal:
                        sse = await flow.workflow_action(
                            "set_goal",
                            workflow_action_params={"goal": next_goal},
                            max_loops=max(12, int(args.action_max_loops)),
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
            pending_card = analysis_round["pending_card"] if isinstance(analysis_round.get("pending_card"), dict) else {}
            allow_reference_refresh = _analysis_should_refresh_references(
                snapshot=analysis_snapshot,
                legal_view=analysis_legal_view,
            )
            if (
                _safe_str(pending_card.get("skill_id")).lower() == "reference-grounding"
                and allow_reference_refresh
                and analysis_reference_refresh_attempts < int(args.max_reference_refresh)
            ):
                sse = await flow.workflow_action(
                    "references_refresh_partial",
                    max_loops=max(12, int(args.action_max_loops)),
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
                sse = await flow.workflow_action(
                    "references_refresh_partial",
                    max_loops=max(12, int(args.action_max_loops)),
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
            bundle = export_failure_bundle(
                repo_root=REPO_ROOT,
                session_id=session_id,
                matter_id=_safe_str(flow.matter_id),
                reason="legal_opinion_analysis_not_ready",
            )
            bundle_quality = build_bundle_quality_reports(
                repo_root=REPO_ROOT,
                bundle_dir=bundle["bundle_dir"],
                flow_id="legal_opinion",
                snapshot=await fetch_workbench_snapshot(client, _safe_str(flow.matter_id)) if _safe_str(flow.matter_id) else {},
                current_view={},
                goal_completion_mode="none",
            )
            write_json(out_dir / "failure_summary.json", bundle["summary"])
            write_json(out_dir / "bundle_quality.failure.json", bundle_quality)
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
            print("[result] capability_gap")
            print(f"[artifacts] {out_dir}")
            return 3

        set_goal_sse = await flow.workflow_action(
            "set_goal",
            workflow_action_params={"goal": _GOAL_DOCGEN},
            max_loops=max(12, int(args.action_max_loops)),
        )
        await _persist_action_sse(out_dir, 2, "set_goal_document_generation", set_goal_sse)

        stable_block_fingerprint = ""
        stable_block_repeats = 0
        reference_refresh_attempts = 0
        success = False
        hard_block_summary: dict[str, Any] = {}
        for round_no in range(3, max(3, int(args.docgen_round_limit)) + 3):
            round_state = await _collect_round_state(
                client=client,
                flow=flow,
                session_id=session_id,
                out_dir=out_dir,
                round_no=round_no,
                round_label="docgen",
                goal_completion_mode="workflow_action",
            )
            docgen_view = round_state["docgen_view"] if isinstance(round_state["docgen_view"], dict) else {}
            pending_card = round_state["pending_card"] if isinstance(round_state["pending_card"], dict) else {}
            bundle_quality = round_state["bundle_quality"] if isinstance(round_state["bundle_quality"], dict) else {}

            if _formal_doc_ready(docgen_view, bundle_quality):
                success = True
                break

            if is_goal_completion_card(pending_card):
                sse = await flow.workflow_action(
                    "set_goal",
                    workflow_action_params={"goal": _GOAL_DOCGEN},
                    max_loops=max(12, int(args.action_max_loops)),
                )
                await _persist_action_sse(out_dir, round_no, "goal_completion_set_goal", sse)
                continue

            if _safe_str(pending_card.get("skill_id")).lower() == "reference-grounding" and reference_refresh_attempts < int(args.max_reference_refresh):
                sse = await flow.workflow_action("references_refresh_partial", max_loops=max(12, int(args.action_max_loops)))
                await _persist_action_sse(out_dir, round_no, "references_refresh_partial", sse)
                sse = await flow.workflow_action(
                    "set_goal",
                    workflow_action_params={"goal": _GOAL_DOCGEN},
                    max_loops=max(12, int(args.action_max_loops)),
                )
                await _persist_action_sse(out_dir, round_no, "set_goal_after_reference_refresh", sse)
                reference_refresh_attempts += 1
                continue

            formal_gate_reason_codes = [
                _safe_str(code)
                for code in (
                    docgen_view.get("formal_gate_reason_codes")
                    if isinstance(docgen_view.get("formal_gate_reason_codes"), list)
                    else docgen_view.get("blocking_reason_codes")
                ) or []
                if _safe_str(code)
            ]
            if bool(docgen_view.get("formal_gate_blocked")) and _is_reference_related(formal_gate_reason_codes) and reference_refresh_attempts < int(args.max_reference_refresh):
                sse = await flow.workflow_action("references_refresh_partial", max_loops=max(12, int(args.action_max_loops)))
                await _persist_action_sse(out_dir, round_no, "formal_gate_reference_refresh", sse)
                sse = await flow.workflow_action(
                    "set_goal",
                    workflow_action_params={"goal": _GOAL_DOCGEN},
                    max_loops=max(12, int(args.action_max_loops)),
                )
                await _persist_action_sse(out_dir, round_no, "set_goal_after_formal_gate_refresh", sse)
                reference_refresh_attempts += 1
                continue

            if bool(docgen_view.get("formal_gate_blocked")) or _safe_str(docgen_view.get("status")).lower() == "repair_blocked":
                fingerprint = _json_fingerprint(
                    {
                        "status": _safe_str(docgen_view.get("status")),
                        "terminal_reason": _safe_str(docgen_view.get("terminal_reason")),
                        "formal_gate_reason_codes": formal_gate_reason_codes,
                        "formal_gate_summary": _safe_str(docgen_view.get("formal_gate_summary")),
                        "bundle_quality_failures": [
                            _safe_str(item)
                            for item in (bundle_quality.get("failures") if isinstance(bundle_quality.get("failures"), list) else [])
                            if _safe_str(item)
                        ],
                    }
                )
                if fingerprint == stable_block_fingerprint:
                    stable_block_repeats += 1
                else:
                    stable_block_fingerprint = fingerprint
                    stable_block_repeats = 1
                if stable_block_repeats >= int(args.stable_block_repeats):
                    hard_block_summary = {
                        "status": "stable_hard_block",
                        "repeats": stable_block_repeats,
                        "docgen_view": docgen_view,
                        "bundle_quality": bundle_quality,
                        "flow_scores": round_state["flow_scores"],
                    }
                    write_json(out_dir / "hard_block_summary.json", hard_block_summary)
                    break

            sse = await flow.step(stop_on_pending_card=is_goal_completion_card)
            if isinstance(sse, dict):
                await _persist_action_sse(out_dir, round_no, "step", sse)
            await asyncio.sleep(float(args.step_sleep_s))

        final_round = await _collect_round_state(
            client=client,
            flow=flow,
            session_id=session_id,
            out_dir=out_dir,
            round_no=99,
            round_label="final",
            goal_completion_mode="workflow_action",
        )
        summary = {
            "base_url": base_url,
            "session_id": session_id,
            "matter_id": final_round["matter_id"],
            "uploaded_file_ids": uploaded_file_ids,
            "kickoff_event_counts": kickoff_counts,
            "analysis_flow_scores": analysis_round["flow_scores"],
            "final_flow_scores": final_round["flow_scores"],
            "final_bundle_quality": final_round["bundle_quality"],
            "deliverable_keys": sorted(final_round["deliverables"].keys()),
            "legal_opinion_view": {
                "summary_len": len(_safe_str(final_round["legal_view"].get("summary"))),
                "issues_count": len(final_round["legal_view"].get("issues")) if isinstance(final_round["legal_view"].get("issues"), list) else 0,
                "risk_count": len(final_round["legal_view"].get("risks")) if isinstance(final_round["legal_view"].get("risks"), list) else 0,
                "action_items_count": len(final_round["legal_view"].get("action_items")) if isinstance(final_round["legal_view"].get("action_items"), list) else 0,
            },
            "document_generation_view": final_round["docgen_view"],
            "reference_refresh_attempts": reference_refresh_attempts,
            "success": success,
            "hard_block_summary": hard_block_summary,
        }
        write_json(out_dir / "summary.json", summary)

    print("[done] legal opinion workflow completed")
    print(f"[artifacts] {out_dir}")
    if not success and hard_block_summary:
        bundle = export_failure_bundle(
            repo_root=REPO_ROOT,
            session_id=session_id,
            matter_id=_safe_str(final_round["matter_id"]),
            reason="legal_opinion_real_flow_stable_hard_block",
        )
        bundle_quality = build_bundle_quality_reports(
            repo_root=REPO_ROOT,
            bundle_dir=bundle["bundle_dir"],
            flow_id="legal_opinion",
            snapshot=final_round["snapshot"] if isinstance(final_round.get("snapshot"), dict) else {},
            current_view=final_round["legal_view"] if isinstance(final_round.get("legal_view"), dict) else {},
            goal_completion_mode="workflow_action",
        )
        write_json(out_dir / "failure_summary.json", bundle["summary"])
        write_json(out_dir / "bundle_quality.failure.json", bundle_quality)
        print(format_first_bad_line(bundle["summary"]))
        print("[result] stable_hard_block")
        return 2
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run legal opinion workflow via consultations WS and continue to formal document_generation.")
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
    parser.add_argument("--action-max-loops", type=int, default=24, help="workflow_action max_loops")
    parser.add_argument("--docgen-round-limit", type=int, default=40, help="Maximum document_generation rounds")
    parser.add_argument("--max-reference-refresh", type=int, default=2, help="Maximum references_refresh_partial attempts")
    parser.add_argument("--stable-block-repeats", type=int, default=3, help="Stable hard-block fingerprint threshold")
    parser.add_argument("--step-sleep-s", type=float, default=1.0, help="Sleep between document_generation rounds")
    parser.add_argument("--cards-only", action="store_true", default=False, help="Reserved compatibility flag")
    parser.add_argument("--output-dir", default="", help="Artifacts output directory")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
