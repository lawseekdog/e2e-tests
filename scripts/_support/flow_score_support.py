from __future__ import annotations

import re
from typing import Any

from support.workbench.docx import (
    assert_docx_has_no_template_placeholders,
    score_contract_review_docx_benchmark,
    score_legal_opinion_docx_benchmark,
)
from support.workbench.timeline import produced_output_keys, unwrap_timeline
from support.workbench.utils import unwrap_api_response


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


_ALLOWED_REVIEW_TYPES = {"clarify", "select", "confirm", "phase_done"}
_ATTACHMENT_FIELD = "case.file_refs.pending_upload_file_ids"
_FORBIDDEN_SKILL_IDS = {"skill-error-analysis"}

_FLOW_CARD_POLICY: dict[str, dict[str, Any]] = {
    "analysis": {"allowed_data_groups": {"search", "evidence", "workbench"}},
    "contract_review": {"allowed_data_groups": {"work_product", "workbench"}},
    "legal_opinion": {"allowed_data_groups": {"search", "evidence", "workbench"}},
    "template_draft": {"allowed_data_groups": {"work_product", "workbench"}},
}

_NODE_HINTS: dict[str, tuple[str, ...]] = {
    "analysis": ("analysis", "pricing", "goal_completion"),
    "contract_review": ("contract", "document", "render", "sync"),
    "legal_opinion": ("legal_opinion", "opinion", "goal_completion"),
    "template_draft": ("intake", "compose", "render", "sync", "finish"),
}

_CITATION_RE = re.compile(r"《[^》]{2,40}》第[一二三四五六七八九十百千万0-9]{1,8}条")
_CONTRACT_REVIEW_OUTPUT_KEYS = ("contract_review_report", "modification_suggestion", "redline_comparison")


def _contract_review_expected_output_keys(*, review_scope: str, expectations: dict[str, Any] | None = None) -> set[str]:
    expected = {
        _safe_str(item)
        for item in _as_list(_as_dict(expectations).get("required_output_keys"))
        if _safe_str(item)
    }
    if expected:
        return expected
    scope = _safe_str(review_scope).lower()
    if scope == "quick":
        return {"contract_review_report"}
    if scope == "risk":
        return {"contract_review_report", "modification_suggestion"}
    if scope in {"redline", "full"}:
        return set(_CONTRACT_REVIEW_OUTPUT_KEYS)
    return {"contract_review_report"}


def _contract_review_actual_output_keys(deliverables: dict[str, dict[str, Any]]) -> set[str]:
    return {
        key
        for key in deliverables
        if _safe_str(key) in _CONTRACT_REVIEW_OUTPUT_KEYS
    }


def _collect_clause_issue_types(current_view: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for row in _as_list(_as_dict(current_view).get("clauses")):
        if not isinstance(row, dict):
            continue
        token = _safe_str(row.get("risk_type"))
        if token:
            out.add(token)
    return out


def _contract_review_grounding_failures(current_view: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for row in _as_list(_as_dict(current_view).get("clauses")):
        if not isinstance(row, dict):
            continue
        clause_id = _safe_str(row.get("clause_id"))
        risk_level = _safe_str(row.get("risk_level")).lower()
        if risk_level not in {"medium", "high", "critical"}:
            continue
        anchor_refs = _as_list(row.get("anchor_refs"))
        law_ref_ids = [_safe_str(item) for item in _as_list(row.get("law_ref_ids")) if _safe_str(item)]
        if not anchor_refs:
            failures.append(f"missing_clause_anchor:{clause_id or 'unknown'}")
        if not law_ref_ids:
            failures.append(f"missing_law_ref:{clause_id or 'unknown'}")
    return failures


def _missing_section_markers(text: str, expectations: dict[str, Any] | None = None) -> list[str]:
    content = _safe_str(text)
    missing: list[str] = []
    for marker in _as_list(_as_dict(expectations).get("required_section_markers")):
        token = _safe_str(marker)
        if token and token not in content:
            missing.append(token)
    return missing


async def collect_flow_observability(
    client: Any,
    *,
    matter_id: str,
    session_id: str,
    timeline_limit: int = 80,
    trace_limit: int = 120,
) -> dict[str, Any]:
    errors: dict[str, str] = {}
    matter_timeline: dict[str, Any] = {}
    phase_timeline: dict[str, Any] = {}
    matter_traces: list[dict[str, Any]] = []
    session_timeline: dict[str, Any] = {}
    session_traces: list[dict[str, Any]] = []

    if _safe_str(matter_id):
        try:
            matter_timeline = unwrap_timeline(await client.get_matter_timeline(matter_id, limit=timeline_limit))
        except Exception as exc:  # noqa: BLE001
            errors["matter_timeline"] = str(exc)
        try:
            raw = unwrap_api_response(await client.get_matter_phase_timeline(matter_id))
            phase_timeline = raw if isinstance(raw, dict) else {}
        except Exception as exc:  # noqa: BLE001
            errors["phase_timeline"] = str(exc)
        try:
            data = unwrap_api_response(await client.list_traces(matter_id, limit=trace_limit))
            rows = data.get("traces") if isinstance(data, dict) else None
            matter_traces = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        except Exception as exc:  # noqa: BLE001
            errors["matter_traces"] = str(exc)

    if _safe_str(session_id):
        try:
            session_timeline = unwrap_timeline(await client.get_session_timeline(session_id, limit=timeline_limit))
        except Exception as exc:  # noqa: BLE001
            errors["session_timeline"] = str(exc)
        try:
            data = unwrap_api_response(await client.list_session_traces(session_id, limit=trace_limit))
            rows = data.get("traces") if isinstance(data, dict) else None
            session_traces = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        except Exception as exc:  # noqa: BLE001
            errors["session_traces"] = str(exc)

    return {
        "matter_timeline": matter_timeline,
        "phase_timeline": phase_timeline,
        "matter_traces": matter_traces,
        "session_timeline": session_timeline,
        "session_traces": session_traces,
        "errors": errors,
    }


def _card_field_issues(*, flow_id: str, card: dict[str, Any]) -> list[str]:
    policy = _FLOW_CARD_POLICY.get(flow_id, {})
    allowed_groups = set(policy.get("allowed_data_groups") or set())
    issues: list[str] = []
    questions = card.get("questions") if isinstance(card.get("questions"), list) else []
    if not questions:
        return ["empty_questions"]

    for question in questions:
        if not isinstance(question, dict):
            issues.append("non_object_question")
            continue
        fk = _safe_str(question.get("field_key"))
        if not fk:
            issues.append("missing_field_key")
            continue
        if fk == _ATTACHMENT_FIELD:
            continue
        if fk.startswith("profile.") or fk == "data.workbench.goal":
            continue
        if fk.startswith("data."):
            parts = [part for part in fk.split(".") if _safe_str(part)]
            group = parts[1] if len(parts) > 1 else ""
            if group not in allowed_groups:
                issues.append(f"unexpected_data_group:{group or 'missing'}")
            continue
        issues.append(f"unexpected_field_key:{fk}")
    return issues


def score_unexpected_cards(
    *,
    flow_id: str,
    seen_cards: list[dict[str, Any]] | None,
    pending_card: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cards = [dict(card) for card in (seen_cards or []) if isinstance(card, dict)]
    if isinstance(pending_card, dict) and pending_card:
        cards.append(dict(pending_card))

    unexpected_cards: list[dict[str, Any]] = []
    warnings: list[str] = []
    for card in cards:
        skill_id = _safe_str(card.get("skill_id")).lower()
        task_key = _safe_str(card.get("task_key"))
        review_type = _safe_str(card.get("review_type")).lower()
        reasons: list[str] = []
        if skill_id in _FORBIDDEN_SKILL_IDS:
            reasons.append(f"forbidden_skill:{skill_id}")
        if review_type and review_type not in _ALLOWED_REVIEW_TYPES:
            reasons.append(f"unexpected_review_type:{review_type}")
        reasons.extend(_card_field_issues(flow_id=flow_id, card=card))
        if skill_id and not reasons and skill_id not in {"goal-completion", "system:kickoff"}:
            warnings.append(f"unclassified_skill:{skill_id}")
        if reasons:
            unexpected_cards.append(
                {
                    "skill_id": skill_id,
                    "task_key": task_key,
                    "review_type": review_type,
                    "reasons": reasons,
                }
            )

    total = len(cards)
    unexpected = len(unexpected_cards)
    score = 100 if total == 0 else max(0, int(round(100 * (1 - (unexpected / float(total))))))
    return {
        "score": score,
        "passed": unexpected == 0,
        "card_count": total,
        "unexpected_count": unexpected,
        "unexpected_cards": unexpected_cards,
        "warnings": warnings,
    }


def _collect_node_tokens(observability: dict[str, Any] | None) -> tuple[list[str], int, int, set[str]]:
    obs = observability if isinstance(observability, dict) else {}
    tokens: list[str] = []
    trace_count = 0
    phase_count = len(_as_list(_as_dict(obs.get("phase_timeline")).get("phases")))
    produced_keys: set[str] = set()

    for trace_group in ("matter_traces", "session_traces"):
        for row in _as_list(obs.get(trace_group)):
            if not isinstance(row, dict):
                continue
            trace_count += 1
            for key in ("node_id", "nodeId", "task_id", "taskId", "status", "state"):
                token = _safe_str(row.get(key)).lower()
                if token:
                    tokens.append(token)

    for phase in _as_list(_as_dict(obs.get("phase_timeline")).get("phases")):
        if not isinstance(phase, dict):
            continue
        for key in ("id", "status"):
            token = _safe_str(phase.get(key)).lower()
            if token:
                tokens.append(token)

    for timeline_key in ("matter_timeline", "session_timeline"):
        produced_keys.update(produced_output_keys(_as_dict(obs.get(timeline_key))))

    return tokens, trace_count, phase_count, produced_keys


def score_node_path(
    *,
    flow_id: str,
    observability: dict[str, Any] | None,
    goal_completion_mode: str = "",
) -> dict[str, Any]:
    tokens, trace_count, phase_count, produced_keys = _collect_node_tokens(observability)
    unique_tokens = sorted({token for token in tokens if token})
    hints = list(_NODE_HINTS.get(flow_id, ()))
    haystack = "\n".join([*unique_tokens, *sorted(produced_keys), _safe_str(goal_completion_mode).lower()])
    matched_hints = [hint for hint in hints if hint and hint in haystack]
    missing_hints = [hint for hint in hints if hint and hint not in matched_hints]

    score = 0
    if trace_count > 0:
        score += 25
    score += min(25, int(min(len(unique_tokens), 8) / 8 * 25))
    if phase_count > 0:
        score += min(20, int(min(phase_count, 4) / 4 * 20))
    if hints:
        score += int((len(matched_hints) / float(len(hints))) * 30)

    return {
        "score": min(100, score),
        "passed": trace_count > 0 and len(matched_hints) >= max(1, min(len(hints), 2)),
        "trace_count": trace_count,
        "distinct_node_token_count": len(unique_tokens),
        "phase_count": phase_count,
        "matched_hints": matched_hints,
        "missing_hints": missing_hints,
        "produced_output_keys": sorted(produced_keys),
        "collection_errors": _as_dict(observability).get("errors") if isinstance(_as_dict(observability).get("errors"), dict) else {},
    }


def score_snapshot_progress(
    *,
    flow_id: str,
    snapshot: dict[str, Any] | None,
    current_view: dict[str, Any] | None,
    aux_views: dict[str, Any] | None = None,
    deliverables: dict[str, dict[str, Any]] | None = None,
    pending_card: dict[str, Any] | None = None,
    contract_review_expectations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snap = snapshot if isinstance(snapshot, dict) else {}
    view = current_view if isinstance(current_view, dict) else {}
    aux = aux_views if isinstance(aux_views, dict) else {}
    deliverable_map = deliverables if isinstance(deliverables, dict) else {}
    analysis_state = _as_dict(snap.get("analysis_state"))
    failures: list[str] = []
    score = 0

    current_node = _safe_str(analysis_state.get("current_node"))
    current_phase = _safe_str(analysis_state.get("current_phase") or snap.get("current_phase"))
    if current_node or current_phase:
        score += 15
    else:
        failures.append("snapshot_missing_current_node_phase")

    summary_len = len(_safe_str(view.get("summary")))
    if summary_len >= 60:
        score += 20
    else:
        failures.append("view_summary_too_short")

    diagnostics = _as_dict(view.get("result_contract_diagnostics"))
    if _safe_str(diagnostics.get("status")).lower() == "valid":
        score += 15
    else:
        failures.append("view_contract_invalid")

    pending = pending_card if isinstance(pending_card, dict) else {}
    pending_skill = _safe_str(pending.get("skill_id")).lower()
    if not pending or pending_skill == "goal-completion":
        score += 10
    else:
        failures.append(f"unfinished_pending_card:{pending_skill or 'unknown'}")

    if flow_id == "analysis":
        issues = len(_as_list(view.get("issues")))
        strategies = len(_as_list(view.get("strategy_options")))
        risks = len(_as_list(_as_dict(view.get("risk_assessment")).get("key_risks")))
        pricing = _as_dict(aux.get("pricing_view"))
        pricing_status = _safe_str(pricing.get("status")).lower()
        if issues > 0:
            score += 10
        else:
            failures.append("analysis_issues_missing")
        if strategies > 0:
            score += 10
        else:
            failures.append("analysis_strategies_missing")
        if risks > 0:
            score += 5
        if pricing_status in {"ready", "review_pending", "completed"}:
            score += 15
        else:
            failures.append(f"pricing_status:{pricing_status or 'missing'}")
    elif flow_id == "contract_review":
        clauses = len(_as_list(view.get("clauses")))
        if clauses >= 3:
            score += 20
        else:
            failures.append("contract_review_clauses_insufficient")
        if _safe_str(view.get("overall_risk_level")):
            score += 5
        else:
            failures.append("contract_review_risk_level_missing")
        contract_type_id = _safe_str(view.get("contract_type_id"))
        review_scope = _safe_str(view.get("review_scope"))
        expected_contract_type_id = _safe_str(_as_dict(contract_review_expectations).get("contract_type_id"))
        expected_review_scope = _safe_str(_as_dict(contract_review_expectations).get("review_scope"))
        if contract_type_id:
            score += 15
        else:
            failures.append("contract_review_contract_type_missing")
        if review_scope:
            score += 5
        else:
            failures.append("contract_review_review_scope_missing")
        if expected_contract_type_id and contract_type_id != expected_contract_type_id:
            failures.append(f"contract_review_contract_type_mismatch:{contract_type_id or 'missing'}")
        if expected_review_scope and review_scope != expected_review_scope:
            failures.append(f"contract_review_review_scope_mismatch:{review_scope or 'missing'}")

        expected_output_keys = _contract_review_expected_output_keys(
            review_scope=review_scope or expected_review_scope,
            expectations=contract_review_expectations,
        )
        actual_output_keys = _contract_review_actual_output_keys(deliverable_map)
        if actual_output_keys == expected_output_keys:
            score += 10
        else:
            failures.append(
                f"contract_review_deliverables_mismatch:expected={sorted(expected_output_keys)!r}:actual={sorted(actual_output_keys)!r}"
            )

        mandatory_issue_types = {
            _safe_str(item)
            for item in _as_list(_as_dict(contract_review_expectations).get("mandatory_issue_types"))
            if _safe_str(item)
        }
        actual_issue_types = _collect_clause_issue_types(view)
        missing_issue_types = sorted(mandatory_issue_types - actual_issue_types)
        if not missing_issue_types:
            score += 10
        elif mandatory_issue_types:
            failures.append(f"contract_review_issue_types_missing:{','.join(missing_issue_types)}")

        grounding_failures = _contract_review_grounding_failures(view)
        if not grounding_failures:
            score += 10
        else:
            failures.extend(grounding_failures)
    elif flow_id == "legal_opinion":
        issues = len(_as_list(view.get("issues")))
        risks = len(_as_list(view.get("risks")))
        actions = len(_as_list(view.get("action_items")))
        if issues + risks + actions >= 2:
            score += 20
        else:
            failures.append("legal_opinion_sections_insufficient")
        if "legal_opinion" in deliverable_map:
            score += 15
        else:
            failures.append("legal_opinion_deliverable_missing")
    else:
        score += 25

    return {
        "score": min(100, score),
        "passed": score >= 75 and not failures,
        "failures": failures,
        "summary_len": summary_len,
        "current_node": current_node,
        "current_phase": current_phase,
    }


def _score_analysis_output_quality(*, current_view: dict[str, Any], aux_views: dict[str, Any] | None = None) -> dict[str, Any]:
    view = current_view if isinstance(current_view, dict) else {}
    aux = aux_views if isinstance(aux_views, dict) else {}
    pricing = _as_dict(aux.get("pricing_view"))
    issues = len(_as_list(view.get("issues")))
    strategies = len(_as_list(view.get("strategy_options")))
    risks = len(_as_list(_as_dict(view.get("risk_assessment")).get("key_risks")))
    pricing_status = _safe_str(pricing.get("status")).lower()
    failures: list[str] = []
    score = 0
    if len(_safe_str(view.get("summary"))) >= 80:
        score += 30
    else:
        failures.append("analysis_summary_too_short")
    if issues > 0:
        score += 20
    else:
        failures.append("analysis_issues_missing")
    if strategies > 0:
        score += 20
    else:
        failures.append("analysis_strategies_missing")
    if risks > 0:
        score += 10
    else:
        failures.append("analysis_risks_missing")
    if pricing_status in {"ready", "review_pending", "completed"}:
        score += 20
    else:
        failures.append(f"pricing_status:{pricing_status or 'missing'}")
    return {"score": min(100, score), "passed": score >= 75 and not failures, "failures": failures, "details": {"issues": issues, "strategies": strategies, "risks": risks, "pricing_status": pricing_status}}


def _score_contract_review_output_quality(
    *,
    text: str,
    deliverable_status: str,
    current_view: dict[str, Any],
    gold_text: str = "",
    contract_review_expectations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content = _safe_str(text)
    if not content:
        clauses = len(_as_list(_as_dict(current_view).get("clauses")))
        failures = ["contract_review_doc_missing"]
        if clauses < 3:
            failures.append("contract_review_clauses_insufficient")
        return {"score": 45 if clauses >= 3 else 0, "passed": False, "failures": failures, "details": {"clauses": clauses}}

    failures: list[str] = []
    try:
        assert_docx_has_no_template_placeholders(content)
    except AssertionError as exc:
        failures.append(str(exc))
    benchmark = score_contract_review_docx_benchmark(content, gold_text=_safe_str(gold_text))
    failures.extend(list(benchmark.hard_gate_failures))
    if _safe_str(deliverable_status).lower() not in {"completed", "archived", "done"}:
        failures.append(f"deliverable_status:{deliverable_status or 'missing'}")
    missing_markers = _missing_section_markers(content, contract_review_expectations)
    if missing_markers:
        failures.append(f"contract_review_section_markers_missing:{','.join(missing_markers)}")
    return {
        "score": int(benchmark.score),
        "passed": benchmark.passed and not failures,
        "failures": failures,
        "details": {
            "legal_citation_count": benchmark.legal_citation_count,
            "clause_reference_count": benchmark.clause_reference_count,
            "numbered_suggestion_count": benchmark.numbered_suggestion_count,
            "text_length": benchmark.text_length,
        },
    }


def build_legal_opinion_formal_ready_report(
    *,
    current_view: dict[str, Any] | None,
    aux_views: dict[str, Any] | None = None,
    deliverable_text: str = "",
    deliverable_status: str = "",
) -> dict[str, Any]:
    view = current_view if isinstance(current_view, dict) else {}
    aux = aux_views if isinstance(aux_views, dict) else {}
    docgen_view = _as_dict(aux.get("document_generation_view"))
    content = _safe_str(deliverable_text)
    title = _safe_str(view.get("title"))
    summary = _safe_str(view.get("summary"))
    confirmed_rows = [
        row
        for row in _as_list(view.get("confirmed_opinions"))
        if isinstance(row, dict)
    ]
    if not confirmed_rows:
        confirmed_rows = [
            row
            for row in _as_list(view.get("conclusion_targets"))
            if isinstance(row, dict) and _safe_str(row.get("status")).lower() == "confirmed"
        ]
    risks = len(_as_list(view.get("risks")))
    actions = len(_as_list(view.get("action_items")))
    material_gaps = [_safe_str(item) for item in _as_list(view.get("material_gaps")) if _safe_str(item)]
    fact_gaps = [_safe_str(item) for item in _as_list(view.get("fact_gaps")) if _safe_str(item)]
    formal_gate_blocked = bool(docgen_view.get("formal_gate_blocked"))
    formal_gate_reason_codes = [
        _safe_str(code)
        for code in _as_list(docgen_view.get("formal_gate_reason_codes"))
        if _safe_str(code)
    ]
    formal_gate_actions = [
        row for row in _as_list(docgen_view.get("formal_gate_actions")) if isinstance(row, dict)
    ]
    pollution_hits = [
        token
        for token in ("contract_dispute", "dispute_response", "陈述泳道", "证据泳道", "client")
        if token and token.lower() in "\n".join([title, summary, content]).lower()
    ]

    failures: list[str] = []
    score = 0
    if len(summary) >= 60:
        score += 15
    else:
        failures.append("legal_opinion_summary_too_short")
    if confirmed_rows:
        score += 20
    else:
        failures.append("legal_opinion_confirmed_opinions_missing")
    if risks > 0:
        score += 10
    else:
        failures.append("legal_opinion_risks_missing")
    if actions > 0:
        score += 10
    else:
        failures.append("legal_opinion_action_items_missing")
    if not material_gaps and not fact_gaps:
        score += 15
    else:
        failures.append("legal_opinion_unresolved_gaps_present")
    if not pollution_hits:
        score += 10
    else:
        failures.append(f"legal_opinion_pollution:{','.join(pollution_hits)}")
    if formal_gate_blocked:
        if formal_gate_reason_codes:
            score += 10
        else:
            failures.append("formal_gate_reason_codes_missing")
        if formal_gate_actions:
            score += 10
        else:
            failures.append("formal_gate_actions_missing")
    elif docgen_view:
        score += 20
    else:
        failures.append("document_generation_view_missing")
    if _safe_str(deliverable_status).lower() in {"completed", "archived", "done"}:
        score += 10
    elif content:
        failures.append(f"deliverable_status:{deliverable_status or 'missing'}")

    return {
        "score": min(100, score),
        "passed": score >= 75 and not failures,
        "failures": failures,
        "blocking_reason_codes": formal_gate_reason_codes,
        "required_actions": formal_gate_actions,
        "details": {
            "confirmed_count": len(confirmed_rows),
            "risk_count": risks,
            "action_count": actions,
            "material_gap_count": len(material_gaps),
            "fact_gap_count": len(fact_gaps),
            "formal_gate_blocked": formal_gate_blocked,
            "pollution_hits": pollution_hits,
        },
    }


def _score_legal_opinion_output_quality(
    *,
    text: str,
    deliverable_status: str,
    current_view: dict[str, Any],
    gold_text: str = "",
    aux_views: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content = _safe_str(text)
    formal_ready = build_legal_opinion_formal_ready_report(
        current_view=current_view,
        aux_views=aux_views,
        deliverable_text=content,
        deliverable_status=deliverable_status,
    )
    if not content:
        issues = len(_as_list(_as_dict(current_view).get("issues")))
        risks = len(_as_list(_as_dict(current_view).get("risks")))
        actions = len(_as_list(_as_dict(current_view).get("action_items")))
        failures = ["legal_opinion_doc_missing", *[str(item) for item in _as_list(formal_ready.get("failures")) if _safe_str(item)]]
        if issues + risks + actions < 2:
            failures.append("legal_opinion_sections_insufficient")
        return {
            "score": max(int(formal_ready.get("score") or 0), 50 if issues + risks + actions >= 2 else 0),
            "passed": False,
            "failures": failures,
            "details": {
                "issues": issues,
                "risks": risks,
                "actions": actions,
                "formal_ready": formal_ready,
            },
        }

    failures: list[str] = [str(item) for item in _as_list(formal_ready.get("failures")) if _safe_str(item)]
    try:
        assert_docx_has_no_template_placeholders(content)
    except AssertionError as exc:
        failures.append(str(exc))
    benchmark = score_legal_opinion_docx_benchmark(content, gold_text=_safe_str(gold_text))
    failures.extend(list(benchmark.hard_gate_failures))
    if _safe_str(deliverable_status).lower() not in {"completed", "archived", "done"}:
        failures.append(f"deliverable_status:{deliverable_status or 'missing'}")
    return {
        "score": int(round((benchmark.score * 0.65) + (int(formal_ready.get("score") or 0) * 0.35))),
        "passed": benchmark.passed and bool(formal_ready.get("passed")) and not failures,
        "failures": failures,
        "details": {
            "legal_citation_count": benchmark.legal_citation_count,
            "clause_reference_count": benchmark.clause_reference_count,
            "numbered_item_count": benchmark.numbered_item_count,
            "has_uncertainty_notice": benchmark.has_uncertainty_notice,
            "text_length": benchmark.text_length,
            "pollution_hits": benchmark.pollution_hits,
            "formal_ready": formal_ready,
        },
    }


def score_deliverable_quality(
    *,
    flow_id: str,
    text: str = "",
    deliverable_status: str = "",
    current_view: dict[str, Any] | None = None,
    aux_views: dict[str, Any] | None = None,
    gold_text: str = "",
    contract_review_expectations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    view = current_view if isinstance(current_view, dict) else {}
    aux = aux_views if isinstance(aux_views, dict) else {}
    if flow_id == "analysis":
        return _score_analysis_output_quality(current_view=view, aux_views=aux)
    if flow_id == "contract_review":
        return _score_contract_review_output_quality(
            text=text,
            deliverable_status=deliverable_status,
            current_view=view,
            gold_text=gold_text,
            contract_review_expectations=contract_review_expectations,
        )
    if flow_id == "legal_opinion":
        return _score_legal_opinion_output_quality(
            text=text,
            deliverable_status=deliverable_status,
            current_view=view,
            gold_text=gold_text,
            aux_views=aux,
        )
    return {"score": 0, "passed": False, "failures": ["unsupported_flow"], "details": {}}


def build_flow_scores(
    *,
    flow_id: str,
    seen_cards: list[dict[str, Any]] | None,
    pending_card: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    current_view: dict[str, Any] | None,
    aux_views: dict[str, Any] | None = None,
    deliverables: dict[str, dict[str, Any]] | None = None,
    deliverable_text: str = "",
    deliverable_status: str = "",
    gold_text: str = "",
    contract_review_expectations: dict[str, Any] | None = None,
    observability: dict[str, Any] | None = None,
    goal_completion_mode: str = "",
) -> dict[str, Any]:
    unexpected = score_unexpected_cards(flow_id=flow_id, seen_cards=seen_cards, pending_card=pending_card)
    node_path = score_node_path(flow_id=flow_id, observability=observability, goal_completion_mode=goal_completion_mode)
    snapshot_progress = score_snapshot_progress(
        flow_id=flow_id,
        snapshot=snapshot,
        current_view=current_view,
        aux_views=aux_views,
        deliverables=deliverables,
        pending_card=pending_card,
        contract_review_expectations=contract_review_expectations,
    )
    deliverable_quality = score_deliverable_quality(
        flow_id=flow_id,
        text=deliverable_text,
        deliverable_status=deliverable_status,
        current_view=current_view,
        aux_views=aux_views,
        gold_text=gold_text,
        contract_review_expectations=contract_review_expectations,
    )

    overall_score = int(round(unexpected["score"] * 0.25 + node_path["score"] * 0.25 + snapshot_progress["score"] * 0.20 + deliverable_quality["score"] * 0.30))
    overall_failures: list[str] = []
    if not bool(unexpected.get("passed")):
        overall_failures.extend(
            [",".join(_as_list(row.get("reasons"))) for row in _as_list(unexpected.get("unexpected_cards")) if isinstance(row, dict)]
        )
    for name, block in (("node_path", node_path), ("snapshot_progress", snapshot_progress), ("deliverable_quality", deliverable_quality)):
        if not bool(block.get("passed")):
            block_failures = block.get("failures") if isinstance(block.get("failures"), list) else []
            if block_failures:
                overall_failures.extend([f"{name}:{_safe_str(item)}" for item in block_failures if _safe_str(item)])
            else:
                overall_failures.append(f"{name}:failed")

    return {
        "unexpected_card_score": unexpected,
        "node_path_score": node_path,
        "snapshot_progress_score": snapshot_progress,
        "deliverable_quality_score": deliverable_quality,
        "overall_e2e_score": {
            "score": overall_score,
            "passed": bool(unexpected.get("passed")) and bool(node_path.get("passed")) and bool(snapshot_progress.get("passed")) and bool(deliverable_quality.get("passed")),
            "failures": [item for item in overall_failures if _safe_str(item)],
        },
    }


def build_template_flow_scores(
    *,
    cards: list[dict[str, Any]] | None,
    pending_card: dict[str, Any] | None,
    node_timeline: list[dict[str, Any]] | None,
    summary: dict[str, Any] | None,
    last_docgen_snapshot: dict[str, Any] | None,
    dialogue_quality: dict[str, Any] | None,
    document_quality: dict[str, Any] | None,
) -> dict[str, Any]:
    unexpected = score_unexpected_cards(flow_id="template_draft", seen_cards=cards, pending_card=pending_card)
    rows = [row for row in (node_timeline or []) if isinstance(row, dict)]
    node_set = {_safe_str(row.get("docgen_node")).lower() for row in rows if _safe_str(row.get("docgen_node"))}
    expected_nodes = {"intake", "compose", "render", "sync", "finish"}
    matched_nodes = sorted(node_set & expected_nodes)
    node_path_score = {
        "score": int(round((len(matched_nodes) / float(len(expected_nodes))) * 100)) if expected_nodes else 100,
        "passed": len(matched_nodes) >= 4,
        "matched_hints": matched_nodes,
        "missing_hints": sorted(expected_nodes - node_set),
        "trace_count": len(rows),
        "distinct_node_token_count": len(node_set),
        "phase_count": 0,
        "produced_output_keys": [],
        "collection_errors": {},
    }
    snapshot_obj = last_docgen_snapshot if isinstance(last_docgen_snapshot, dict) else {}
    deliverable = _as_dict(snapshot_obj.get("deliverable"))
    snapshot_failures: list[str] = []
    snapshot_score = 0
    if _safe_str(snapshot_obj.get("current_task_id")) or _safe_str(snapshot_obj.get("current_phase")):
        snapshot_score += 20
    else:
        snapshot_failures.append("snapshot_missing_current_task_phase")
    if _safe_str(_as_dict(summary).get("latest_docgen_node")):
        snapshot_score += 20
    else:
        snapshot_failures.append("docgen_node_missing")
    if bool(snapshot_obj.get("template_quality_contracts_json_exists")):
        snapshot_score += 20
    else:
        snapshot_failures.append("template_quality_contracts_missing")
    if _safe_str(deliverable.get("status")).lower() in {"completed", "archived", "done"}:
        snapshot_score += 20
    else:
        snapshot_failures.append("deliverable_not_terminal")
    if _safe_str(snapshot_obj.get("quality_review_decision")):
        snapshot_score += 20
    else:
        snapshot_failures.append("quality_review_decision_missing")
    snapshot_progress = {
        "score": snapshot_score,
        "passed": snapshot_score >= 80 and not snapshot_failures,
        "failures": snapshot_failures,
        "summary_len": 0,
        "current_node": _safe_str(_as_dict(summary).get("latest_docgen_node")),
        "current_phase": _safe_str(snapshot_obj.get("current_phase")),
    }
    dialogue = dialogue_quality if isinstance(dialogue_quality, dict) else {}
    document = document_quality if isinstance(document_quality, dict) else {}
    deliverable_score = int(round((40 if bool(dialogue.get("pass")) else 0) + (60 if bool(document.get("pass")) else 0)))
    deliverable_failures: list[str] = []
    if not bool(dialogue.get("pass")):
        deliverable_failures.append("dialogue_quality_failed")
    if not bool(document.get("pass")):
        deliverable_failures.append("document_quality_failed")
    deliverable_quality = {
        "score": deliverable_score,
        "passed": not deliverable_failures,
        "failures": deliverable_failures,
        "details": {
            "dialogue_quality_pass": bool(dialogue.get("pass")),
            "document_quality_pass": bool(document.get("pass")),
            "citation_count": int(document.get("citation_count") or 0) if isinstance(document.get("citation_count"), (int, float)) else 0,
            "fact_coverage_score": float(document.get("fact_coverage_score") or 0.0) if isinstance(document.get("fact_coverage_score"), (int, float)) else 0.0,
        },
    }
    overall_score = int(round(unexpected["score"] * 0.20 + node_path_score["score"] * 0.25 + snapshot_progress["score"] * 0.20 + deliverable_quality["score"] * 0.35))
    overall_failures = [
        *(deliverable_failures),
        *(snapshot_failures),
        *[",".join(_as_list(row.get("reasons"))) for row in _as_list(unexpected.get("unexpected_cards")) if isinstance(row, dict)],
    ]
    return {
        "unexpected_card_score": unexpected,
        "node_path_score": node_path_score,
        "snapshot_progress_score": snapshot_progress,
        "deliverable_quality_score": deliverable_quality,
        "overall_e2e_score": {
            "score": overall_score,
            "passed": bool(unexpected.get("passed")) and bool(node_path_score.get("passed")) and bool(snapshot_progress.get("passed")) and bool(deliverable_quality.get("passed")),
            "failures": [item for item in overall_failures if _safe_str(item)],
        },
    }


__all__ = [
    "build_flow_scores",
    "build_template_flow_scores",
    "collect_flow_observability",
]
