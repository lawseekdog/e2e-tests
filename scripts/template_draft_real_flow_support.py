from __future__ import annotations

import json
from typing import Any

DEFAULT_LEGAL_OPINION_FACTS = (
    "委托人：北京云杉科技有限公司。\n"
    "相对方：上海启衡数据系统有限公司。\n"
    "文书类型：法律意见书。\n"
    "事项：服务器采购合同履约争议，对方逾期交付且交付设备存在型号不符、无法开机等质量问题，并要求我方继续支付尾款。\n"
    "争点：\n"
    "1. 我方是否有权暂停支付剩余尾款并要求更换、维修或解除合同；\n"
    "2. 合同约定的逾期交付违约责任和质量责任能否主张；\n"
    "3. 争议解决、证据保全和谈判口径应如何安排。\n"
    "时间线：\n"
    "- 2025-11-18 双方签署《服务器采购合同》，合同总价人民币360000元，约定预付款70%，验收合格后支付30%尾款。\n"
    "- 2025-11-20 我方已支付预付款252000元。\n"
    "- 2025-12-25 对方应完成首批交付，但实际于2026-01-08送达。\n"
    "- 2026-01-10 初检发现2台设备型号不符、1台无法开机，验收记录写明“整改后再复验”。\n"
    "- 2026-01-15 对方函告要求我方先支付尾款再安排维修，并主张免责条款覆盖质量责任。\n"
    "证据线索：采购合同、付款凭证、到货签收单、验收记录、催告函、邮件和微信沟通记录。\n"
    "目标：评估我方付款、拒收、解除、索赔的权利边界，给出证据保全、谈判顺序与后续动作建议，并形成可供管理层直接决策的法律意见书。"
)

DEFAULT_LEGAL_OPINION_EVIDENCE_RELATIVE = (
    "scripts/fixtures/legal_opinion_supply_contract.txt",
    "scripts/fixtures/legal_opinion_performance_timeline.txt",
    "scripts/fixtures/legal_opinion_demand_reply.txt",
)

DOCGEN_NODE_ORDER: tuple[str, ...] = (
    "intake",
    "section_contract",
    "compose",
    "hard_validate",
    "soft_validate",
    "repair",
    "render",
    "sync",
    "finish",
)
DOCGEN_STOP_NODES = frozenset(DOCGEN_NODE_ORDER)

_TASK_NODE_HINTS: tuple[tuple[str, str], ...] = (
    ("docgen_intake", "intake"),
    ("document-drafting-intake", "intake"),
    ("doc_prepare", "intake"),
    ("section_contract", "section_contract"),
    ("docgen_compose", "compose"),
    ("document-generation", "compose"),
    ("hard_validate", "hard_validate"),
    ("soft_validate", "soft_validate"),
    ("document-quality-review", "soft_validate"),
    ("doc_quality_review", "soft_validate"),
    ("docgen_repair", "repair"),
    ("document-repair", "repair"),
    ("docgen_render", "render"),
    ("render", "render"),
    ("docgen_sync", "sync"),
    ("finish", "finish"),
)


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip()
        return bool(text and text not in {"{}", "[]", "null"})
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return True


def _compact_pending_card(card: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(card, dict):
        return {}
    return {
        "id": _safe_str(card.get("id") or card.get("card_id")),
        "skill_id": _safe_str(card.get("skill_id")),
        "task_key": _safe_str(card.get("task_key")),
        "review_type": _safe_str(card.get("review_type")),
        "type": _safe_str(card.get("type")),
        "prompt_preview": _safe_str(card.get("prompt"))[:180],
    }


def _compact_deliverable(row: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {
        "id": _safe_str(row.get("id")),
        "output_key": _safe_str(row.get("output_key") or row.get("outputKey")),
        "title": _safe_str(row.get("title")),
        "status": _safe_str(row.get("status")),
        "file_id": _safe_str(row.get("file_id") or row.get("fileId")),
        "updated_at": _safe_str(row.get("updated_at") or row.get("updatedAt")),
    }


def _normalize_state_maps(raw_state: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    state = raw_state if isinstance(raw_state, dict) else {}
    data = state.get("data") if isinstance(state.get("data"), dict) else {}
    work_product = {}
    if isinstance(data.get("work_product"), dict):
        work_product = data.get("work_product")
    elif isinstance(state.get("work_product"), dict):
        work_product = state.get("work_product")
    return state, data, work_product


def _find_first_value(raw_state: Any, *keys: str) -> Any:
    state, data, work_product = _normalize_state_maps(raw_state)
    for container in (state, data, work_product):
        for key in keys:
            if not key:
                continue
            if key in container and _is_non_empty(container.get(key)):
                return container.get(key)
    return None


def _extract_trace_state_signals(traces: list[dict[str, Any]] | None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "trace_node_ids": [],
        "latest_trace_node_id": "",
        "trace_current_task_id": "",
        "template_quality_contracts_json_exists": False,
        "docgen_repair_contracts_json_exists": False,
        "docgen_repair_plan_exists": False,
        "quality_review_decision": "",
        "soft_reason_codes": [],
        "documents_fingerprint": "",
        "quality_review_fingerprint": "",
        "document_render_metrics_exists": False,
        "document_render_diagnostics_exists": False,
        "docgen": {},
        "document_generation_view": {},
    }
    rows = traces if isinstance(traces, list) else []
    seen_nodes: list[str] = []

    for trace in rows:
        if not isinstance(trace, dict):
            continue
        node_id = _safe_str(trace.get("node_id"))
        if node_id:
            seen_nodes.append(node_id)
            if not info["latest_trace_node_id"] and _trace_node_to_docgen_node(node_id):
                info["latest_trace_node_id"] = node_id
        for raw_state in (trace.get("output_state"), trace.get("input_state")):
            state, data, work_product = _normalize_state_maps(raw_state)
            if not info["trace_current_task_id"]:
                info["trace_current_task_id"] = _safe_str(state.get("current_task_id") or data.get("current_task_id"))
            if not info["template_quality_contracts_json_exists"]:
                contracts = _find_first_value(raw_state, "template_quality_contracts_json")
                info["template_quality_contracts_json_exists"] = _is_non_empty(_coerce_json_like(contracts))
            if not info["docgen_repair_contracts_json_exists"]:
                contracts = _find_first_value(raw_state, "docgen_repair_contracts_json")
                info["docgen_repair_contracts_json_exists"] = _is_non_empty(_coerce_json_like(contracts))
            if not info["docgen_repair_plan_exists"]:
                repair_plan = work_product.get("docgen_repair_plan") if isinstance(work_product.get("docgen_repair_plan"), dict) else {}
                info["docgen_repair_plan_exists"] = _repair_plan_exists(repair_plan)
            if not info["quality_review_decision"]:
                review = work_product.get("quality_review") if isinstance(work_product.get("quality_review"), dict) else {}
                info["quality_review_decision"] = _safe_str(review.get("decision")).lower()
            if not info["soft_reason_codes"]:
                docgen = work_product.get("docgen") if isinstance(work_product.get("docgen"), dict) else {}
                codes = docgen.get("soft_reason_codes") if isinstance(docgen.get("soft_reason_codes"), list) else []
                info["soft_reason_codes"] = [
                    _safe_str(item)
                    for item in codes
                    if _safe_str(item)
                ]
            if not info["documents_fingerprint"]:
                info["documents_fingerprint"] = _safe_str(work_product.get("documents_fingerprint"))
            if not info["quality_review_fingerprint"]:
                info["quality_review_fingerprint"] = _safe_str(work_product.get("quality_review_fingerprint"))
            if not info["document_render_metrics_exists"]:
                info["document_render_metrics_exists"] = isinstance(work_product.get("document_render_metrics"), dict) and bool(work_product.get("document_render_metrics"))
            if not info["document_render_diagnostics_exists"]:
                diagnostics = work_product.get("document_render_diagnostics")
                info["document_render_diagnostics_exists"] = bool(diagnostics) if isinstance(diagnostics, list) else isinstance(diagnostics, dict) and bool(diagnostics)
            if not info["docgen"]:
                docgen = work_product.get("docgen") if isinstance(work_product.get("docgen"), dict) else {}
                if docgen:
                    info["docgen"] = dict(docgen)
            if not info["document_generation_view"]:
                view = work_product.get("document_generation_view") if isinstance(work_product.get("document_generation_view"), dict) else {}
                if view:
                    info["document_generation_view"] = dict(view)
    info["trace_node_ids"] = seen_nodes
    return info


def _coerce_json_like(value: Any) -> Any:
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return value


def _repair_plan_exists(value: Any) -> bool:
    plan = value if isinstance(value, dict) else {}
    if not plan:
        return False
    docs = plan.get("documents") if isinstance(plan.get("documents"), list) else []
    if docs:
        return True
    return any(_is_non_empty(v) for k, v in plan.items() if k != "documents")


def _extract_docgen_snapshot(
    *,
    matter_id: str,
    session_id: str,
    workbench_snapshot: dict[str, Any] | None,
    workflow_snapshot: dict[str, Any] | None,
    phase_timeline: dict[str, Any] | None,
    session: dict[str, Any] | None,
    pending_card: dict[str, Any] | None,
    deliverables: list[dict[str, Any]] | None,
    traces: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    snapshot = workbench_snapshot if isinstance(workbench_snapshot, dict) else {}
    analysis_state = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    goal_views = analysis_state.get("goal_views") if isinstance(analysis_state.get("goal_views"), dict) else {}
    document_generation_view = {}
    if isinstance(analysis_state.get("document_generation_view"), dict):
        document_generation_view = dict(analysis_state.get("document_generation_view"))
    elif isinstance(goal_views.get("document_generation_view"), dict):
        document_generation_view = dict(goal_views.get("document_generation_view"))

    workflow = workflow_snapshot if isinstance(workflow_snapshot, dict) else {}
    workflow_instance = workflow.get("instance") if isinstance(workflow.get("instance"), dict) else {}
    session_data = session if isinstance(session, dict) else {}
    phase = phase_timeline if isinstance(phase_timeline, dict) else {}
    deliverable_rows = [row for row in (deliverables or []) if isinstance(row, dict)]
    deliverable_head = _compact_deliverable(deliverable_rows[0]) if deliverable_rows else {}
    trace_info = _extract_trace_state_signals(traces)
    runtime_docgen = analysis_state.get("docgen_runtime_signals") if isinstance(analysis_state.get("docgen_runtime_signals"), dict) else {}
    docgen = trace_info.get("docgen") if isinstance(trace_info.get("docgen"), dict) and trace_info.get("docgen") else {}
    if not docgen and runtime_docgen:
        docgen = dict(runtime_docgen)

    current_task_id = _safe_str(analysis_state.get("current_task_id"))
    if not current_task_id:
        current_task_id = _safe_str(workflow_instance.get("current_task_id"))
    if not current_task_id:
        current_task_id = _safe_str(trace_info.get("trace_current_task_id"))

    current_phase = _safe_str(phase.get("currentPhase") or phase.get("current_phase"))
    if not current_phase:
        current_phase = _safe_str(workflow_instance.get("current_phase"))
    if not current_phase:
        current_phase = _safe_str(analysis_state.get("current_subgraph"))

    quality_review_decision = _safe_str(trace_info.get("quality_review_decision")).lower()
    if not quality_review_decision:
        quality_review_decision = _safe_str(document_generation_view.get("quality_review_decision")).lower()

    soft_reason_codes = trace_info.get("soft_reason_codes") if isinstance(trace_info.get("soft_reason_codes"), list) else []
    normalized_docgen = {
        "terminal_reason": _safe_str(docgen.get("terminal_reason")),
        "last_failure_reason": _safe_str(docgen.get("last_failure_reason")),
        "repair_required": bool(docgen.get("repair_required")),
        "repair_round": int(docgen.get("repair_round") or 0),
        "hard_validated": bool(docgen.get("hard_validated")),
        "soft_validated": bool(docgen.get("soft_validated")),
        "rendered": bool(docgen.get("rendered")),
        "synced": bool(docgen.get("synced")),
        "section_contract_ready": bool(trace_info.get("template_quality_contracts_json_exists"))
        or bool(document_generation_view.get("template_quality_contracts_json_exists")),
        "soft_reason_codes": soft_reason_codes,
    }

    return {
        "matter_id": _safe_str(matter_id),
        "session_id": _safe_str(session_id),
        "current_phase": current_phase,
        "current_node": _safe_str(analysis_state.get("current_node")),
        "current_task_id": current_task_id,
        "workflow_current_phase": _safe_str(workflow_instance.get("current_phase")),
        "workflow_active_activities": workflow.get("activeActivities") if isinstance(workflow.get("activeActivities"), list) else workflow.get("active_activities") if isinstance(workflow.get("active_activities"), list) else [],
        "session_status": _safe_str(session_data.get("status")),
        "pending_card": _compact_pending_card(pending_card),
        "deliverable": deliverable_head,
        "docgen": normalized_docgen,
        "template_quality_contracts_json_exists": bool(trace_info.get("template_quality_contracts_json_exists"))
        or bool(document_generation_view.get("template_quality_contracts_json_exists")),
        "docgen_repair_plan_exists": bool(trace_info.get("docgen_repair_plan_exists"))
        or bool(document_generation_view.get("docgen_repair_plan_exists")),
        "docgen_repair_contracts_json_exists": bool(trace_info.get("docgen_repair_contracts_json_exists"))
        or bool(document_generation_view.get("docgen_repair_contracts_json_exists")),
        "quality_review_decision": quality_review_decision,
        "soft_reason_codes": soft_reason_codes,
        "documents_fingerprint": _safe_str(trace_info.get("documents_fingerprint")),
        "quality_review_fingerprint": _safe_str(trace_info.get("quality_review_fingerprint")),
        "document_render_metrics_exists": bool(trace_info.get("document_render_metrics_exists")),
        "document_render_diagnostics_exists": bool(trace_info.get("document_render_diagnostics_exists")),
        "document_generation_view": document_generation_view,
        "trace": {
            "latest_docgen_node_id": _safe_str(trace_info.get("latest_trace_node_id")),
            "trace_node_ids": trace_info.get("trace_node_ids") if isinstance(trace_info.get("trace_node_ids"), list) else [],
        },
    }


def _trace_node_to_docgen_node(node_id: str) -> str:
    raw = _safe_str(node_id).lower()
    if not raw:
        return ""
    for hint, node in _TASK_NODE_HINTS:
        if hint in raw:
            return node
    if raw == "hard_validate":
        return "hard_validate"
    if raw == "soft_validate":
        return "soft_validate"
    if raw == "sync":
        return "sync"
    return ""


def _detect_docgen_node(
    *,
    current_task_id: str,
    current_phase: str,
    pending_card: dict[str, Any] | None,
    deliverable: dict[str, Any] | None,
    docgen: dict[str, Any] | None,
    trace_node_ids: list[str] | None,
    template_quality_contracts_json_exists: bool = False,
    docgen_repair_plan_exists: bool = False,
    quality_review_decision: str = "",
) -> str:
    task = _safe_str(current_task_id).lower()
    for hint, node in _TASK_NODE_HINTS:
        if hint in task:
            return node

    pending = pending_card if isinstance(pending_card, dict) else {}
    pending_skill = _safe_str(pending.get("skill_id")).lower()
    pending_task = _safe_str(pending.get("task_key")).lower()
    for token in (pending_task, pending_skill):
        for hint, node in _TASK_NODE_HINTS:
            if hint in token:
                return node

    for node_id in trace_node_ids or []:
        mapped = _trace_node_to_docgen_node(_safe_str(node_id))
        if mapped:
            return mapped

    doc = docgen if isinstance(docgen, dict) else {}
    deliverable_row = deliverable if isinstance(deliverable, dict) else {}
    deliverable_status = _safe_str(deliverable_row.get("status")).lower()
    has_file = bool(_safe_str(deliverable_row.get("file_id")))

    if bool(doc.get("synced")) or (has_file and deliverable_status in {"archived", "completed", "done"}):
        return "finish"
    if bool(doc.get("rendered")):
        return "sync"
    if bool(doc.get("repair_required")) or docgen_repair_plan_exists:
        return "repair"
    if bool(doc.get("soft_validated")):
        return "render"
    if quality_review_decision or doc.get("soft_reason_codes"):
        return "soft_validate"
    if bool(doc.get("hard_validated")):
        return "soft_validate"
    documents_fingerprint = _safe_str(doc.get("documents_fingerprint")) or _safe_str(doc.get("documents_fp"))
    if documents_fingerprint or bool(template_quality_contracts_json_exists):
        return "compose"
    if bool(template_quality_contracts_json_exists):
        return "section_contract"

    phase = _safe_str(current_phase).lower()
    if phase == "docgen":
        return "section_contract"
    if pending_skill or pending_task:
        return "intake"
    return ""


def _extend_docgen_node_sequence(
    *,
    existing: list[str] | None,
    snapshot: dict[str, Any] | None,
    current_node: str,
) -> list[str]:
    seen: list[str] = [_normalize_stop_node(item) for item in (existing or []) if _normalize_stop_node(item)]
    snapshot_obj = snapshot if isinstance(snapshot, dict) else {}
    doc = snapshot_obj.get("docgen") if isinstance(snapshot_obj.get("docgen"), dict) else {}
    deliverable = snapshot_obj.get("deliverable") if isinstance(snapshot_obj.get("deliverable"), dict) else {}
    deliverable_done = bool(_safe_str(deliverable.get("file_id"))) and _safe_str(deliverable.get("status")).lower() in {"completed", "archived", "done"}

    inferred: list[str] = []
    if bool(snapshot_obj.get("template_quality_contracts_json_exists")):
        inferred.append("section_contract")
    if any(
        [
            bool(doc.get("hard_validated")),
            bool(doc.get("soft_validated")),
            bool(doc.get("rendered")),
            bool(doc.get("synced")),
            deliverable_done,
        ]
    ):
        inferred.append("compose")
    if any(
        [
            bool(doc.get("hard_validated")),
            bool(doc.get("soft_validated")),
            bool(doc.get("rendered")),
            bool(doc.get("synced")),
            deliverable_done,
        ]
    ):
        inferred.append("hard_validate")
    if any(
        [
            bool(doc.get("soft_validated")),
            bool(doc.get("rendered")),
            bool(doc.get("synced")),
            deliverable_done,
            bool(snapshot_obj.get("quality_review_decision")),
            bool(snapshot_obj.get("soft_reason_codes")),
        ]
    ):
        inferred.append("soft_validate")
    if bool(doc.get("repair_required")) or bool(snapshot_obj.get("docgen_repair_plan_exists")):
        inferred.append("repair")
    if any([bool(doc.get("rendered")), bool(doc.get("synced")), deliverable_done]):
        inferred.append("render")
    if any([bool(doc.get("synced")), deliverable_done]):
        inferred.append("sync")

    for node in inferred:
        if node and node not in seen:
            seen.append(node)
    normalized_current = _normalize_stop_node(current_node)
    if normalized_current and normalized_current not in seen:
        seen.append(normalized_current)
    return seen


def _normalize_stop_node(value: str) -> str:
    token = _safe_str(value).lower()
    return token if token in DOCGEN_STOP_NODES else ""


def _is_stop_node_reached(
    *,
    target_node: str,
    current_node: str,
    seen_nodes: list[str] | None,
) -> bool:
    target = _normalize_stop_node(target_node)
    current = _normalize_stop_node(current_node)
    if not target:
        return False
    seen = [_normalize_stop_node(item) for item in (seen_nodes or []) if _normalize_stop_node(item)]
    if target in seen or current == target:
        return True
    if target == "repair":
        return False
    if not current:
        return False
    order = {name: idx for idx, name in enumerate(DOCGEN_NODE_ORDER, start=1)}
    return order.get(current, 0) >= order.get(target, 0)


def _build_node_timeline_row(
    *,
    step: int,
    trigger: str,
    observed_at: str,
    docgen_snapshot: dict[str, Any],
    docgen_node_sequence: list[str] | None,
) -> dict[str, Any]:
    snapshot = docgen_snapshot if isinstance(docgen_snapshot, dict) else {}
    deliverable = snapshot.get("deliverable") if isinstance(snapshot.get("deliverable"), dict) else {}
    pending_card = snapshot.get("pending_card") if isinstance(snapshot.get("pending_card"), dict) else {}
    docgen = snapshot.get("docgen") if isinstance(snapshot.get("docgen"), dict) else {}
    return {
        "step": int(step),
        "observed_at": observed_at,
        "trigger": _safe_str(trigger),
        "matter_id": _safe_str(snapshot.get("matter_id")),
        "session_id": _safe_str(snapshot.get("session_id")),
        "current_phase": _safe_str(snapshot.get("current_phase")),
        "current_task_id": _safe_str(snapshot.get("current_task_id")),
        "docgen_node": _safe_str(snapshot.get("docgen_node")),
        "docgen_node_sequence": list(docgen_node_sequence or []),
        "pending_card": {
            "skill_id": _safe_str(pending_card.get("skill_id")),
            "task_key": _safe_str(pending_card.get("task_key")),
            "review_type": _safe_str(pending_card.get("review_type")),
        },
        "deliverable_status": _safe_str(deliverable.get("status")),
        "deliverable_file_id": _safe_str(deliverable.get("file_id")),
        "template_quality_contracts_json_exists": bool(snapshot.get("template_quality_contracts_json_exists")),
        "docgen_repair_plan_exists": bool(snapshot.get("docgen_repair_plan_exists")),
        "docgen_repair_contracts_json_exists": bool(snapshot.get("docgen_repair_contracts_json_exists")),
        "quality_review_decision": _safe_str(snapshot.get("quality_review_decision")),
        "soft_reason_codes": snapshot.get("soft_reason_codes") if isinstance(snapshot.get("soft_reason_codes"), list) else [],
        "documents_fingerprint": _safe_str(snapshot.get("documents_fingerprint")),
        "quality_review_fingerprint": _safe_str(snapshot.get("quality_review_fingerprint")),
        "docgen_flags": {
            "section_contract_ready": bool(docgen.get("section_contract_ready")),
            "hard_validated": bool(docgen.get("hard_validated")),
            "soft_validated": bool(docgen.get("soft_validated")),
            "repair_required": bool(docgen.get("repair_required")),
            "rendered": bool(docgen.get("rendered")),
            "synced": bool(docgen.get("synced")),
            "repair_round": int(docgen.get("repair_round") or 0),
        },
        "trace_latest_docgen_node_id": _safe_str(
            ((snapshot.get("trace") or {}) if isinstance(snapshot.get("trace"), dict) else {}).get("latest_docgen_node_id")
        ),
    }
