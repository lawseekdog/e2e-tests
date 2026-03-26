from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = _safe_str(raw_line)
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_first_mapping_by_key(value: Any, target_key: str) -> dict[str, Any]:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == target_key and isinstance(child, dict):
                return child
            found = _find_first_mapping_by_key(child, target_key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first_mapping_by_key(child, target_key)
            if found:
                return found
    return {}


def _flow_from_service_type(service_type_id: str, bundle_family: str) -> str:
    token = _safe_str(service_type_id).lower()
    family = _safe_str(bundle_family).lower()
    if token == "legal_opinion" or family == "legal_opinion":
        return "legal_opinion"
    if token == "contract_review" or family == "contract_review":
        return "contract_review"
    if token == "document_drafting" or family == "document_drafting":
        return "template_draft"
    return "analysis"


def _derive_quality_context(
    *,
    flow_id: str,
    bundle_dir: Path,
    snapshot: dict[str, Any] | None,
    current_view: dict[str, Any] | None,
    goal_completion_mode: str,
) -> dict[str, Any]:
    snap = snapshot if isinstance(snapshot, dict) else {}
    view = current_view if isinstance(current_view, dict) else {}
    matter = _as_dict(snap.get("matter"))
    analysis_state = _as_dict(snap.get("analysis_state"))
    workflow_model = _as_dict(analysis_state.get("workflow_model"))
    topic_contract = _find_first_mapping_by_key(snap, "topic_contract")
    rule_bundle = _find_first_mapping_by_key(snap, "rule_bundle")

    service_type_id = _safe_str(matter.get("service_type_id")) or _safe_str(workflow_model.get("service_type_id"))
    contract_type_id = _safe_str(view.get("contract_type_id")) or _safe_str(_as_dict(_as_dict(analysis_state.get("case")).get("profile")).get("contract_type_id"))
    opinion_subtype = _safe_str(view.get("opinion_subtype")) or _safe_str(topic_contract.get("opinion_subtype"))

    bundle_family = ""
    bundle_key = ""
    if topic_contract:
        bundle_family = "legal_opinion"
        bundle_key = _safe_str(topic_contract.get("base_bundle_key")) or _safe_str(topic_contract.get("bundle_key")).split("__", 1)[0]
    elif service_type_id.lower() == "contract_review":
        bundle_family = "contract_review"
        bundle_key = contract_type_id or "other"
    elif flow_id == "template_draft" or service_type_id.lower() == "document_drafting":
        bundle_family = "document_drafting"
        bundle_key = "document_drafting_general"
    elif rule_bundle:
        bundle_family = _safe_str(rule_bundle.get("bundle_family")) or "analysis"
        bundle_key = _safe_str(rule_bundle.get("bundle_key"))

    return {
        "contract_version": "bundle_quality.v1",
        "flow_id": _safe_str(flow_id) or _flow_from_service_type(service_type_id, bundle_family),
        "service_type_id": service_type_id,
        "bundle_family": bundle_family,
        "bundle_key": bundle_key,
        "contract_type_id": contract_type_id,
        "opinion_subtype": opinion_subtype,
        "goal_completion_mode": _safe_str(goal_completion_mode),
        "thread_id": bundle_dir.name,
        "session_id": _safe_str(matter.get("session_id")),
        "matter_id": _safe_str(matter.get("id")) or _safe_str(analysis_state.get("matter_id")),
    }


def resolve_quality_context(
    *,
    bundle_dir: Path,
    flow_id: str,
    snapshot: dict[str, Any] | None,
    current_view: dict[str, Any] | None,
    goal_completion_mode: str,
) -> dict[str, Any]:
    quality_dir = bundle_dir / "quality"
    context_path = quality_dir / "context.json"
    context = _read_json(context_path)
    derived = _derive_quality_context(
        flow_id=flow_id,
        bundle_dir=bundle_dir,
        snapshot=snapshot,
        current_view=current_view,
        goal_completion_mode=goal_completion_mode,
    )
    merged = dict(context)
    for key, value in derived.items():
        if _safe_str(value) or isinstance(value, bool):
            merged[key] = value
        else:
            merged.setdefault(key, value)
    merged.setdefault("contract_version", "bundle_quality.v1")
    _write_json(context_path, merged)
    return merged


def _deep_merge_policy(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in patch.items():
        if key == "extends":
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_policy(out[key], value)
            continue
        out[key] = deepcopy(value)
    return out


def _bundles_root(repo_root: Path) -> Path:
    return repo_root / "ai-engine" / "src" / "domain" / "workbench" / "bundles"


def _resolve_policy_path(*, repo_root: Path, family: str, bundle_key: str, relative_path: str) -> Path:
    root = _bundles_root(repo_root)
    family_root = (root / _safe_str(family).lower()).resolve()
    base_dir = (family_root / _safe_str(bundle_key).lower()).resolve()
    rel = _safe_str(relative_path)
    path = (family_root / rel[len("@family/") :]).resolve() if rel.startswith("@family/") else (base_dir / rel).resolve()
    allowed_root = family_root if rel.startswith("@family/") else base_dir
    if path != allowed_root and allowed_root not in path.parents:
        raise ValueError(f"quality_policy_out_of_root:{family}:{bundle_key}:{relative_path}")
    return path


def _load_quality_policy_doc(
    *,
    repo_root: Path,
    family: str,
    bundle_key: str,
    relative_path: str,
    seen: set[Path] | None = None,
) -> dict[str, Any]:
    path = _resolve_policy_path(repo_root=repo_root, family=family, bundle_key=bundle_key, relative_path=relative_path)
    visited = seen if isinstance(seen, set) else set()
    if path in visited:
        raise ValueError(f"quality_policy_cycle:{family}:{bundle_key}:{path}")
    visited.add(path)
    payload = _read_json(path)
    merged: dict[str, Any] = {}
    extends = payload.get("extends")
    extend_rows = [extends] if isinstance(extends, str) else [row for row in extends if isinstance(row, str)] if isinstance(extends, list) else []
    for row in extend_rows:
        merged = _deep_merge_policy(
            merged,
            _load_quality_policy_doc(
                repo_root=repo_root,
                family=family,
                bundle_key=bundle_key,
                relative_path=_safe_str(row),
                seen=visited,
            ),
        )
    return _deep_merge_policy(merged, payload)


def load_quality_policy(*, repo_root: Path, context: dict[str, Any]) -> dict[str, Any]:
    family = _safe_str(context.get("bundle_family"))
    bundle_key = _safe_str(context.get("bundle_key"))
    manifest_path = _bundles_root(repo_root) / family / bundle_key / "manifest.json"
    if not family or not bundle_key or not manifest_path.exists():
        return {
            "policy_version": "quality_policy.v1",
            "selectors": {
                "flow_id": _safe_str(context.get("flow_id")),
                "service_type_ids": [_safe_str(context.get("service_type_id"))] if _safe_str(context.get("service_type_id")) else [],
                "contract_type_ids": [],
                "opinion_subtypes": [],
            },
            "node_profiles": {},
            "skill_profiles": {},
            "lane_profiles": {},
            "score_weights": {
                "bundle": {
                    "unexpected_card_score": 0.25,
                    "node_path_score": 0.25,
                    "snapshot_progress_score": 0.2,
                    "deliverable_quality_score": 0.3,
                }
            },
            "hard_fail_rules": [
                "missing_bundle_top_level_contract",
                "quality_raw_missing",
                "observability_contract_missing_reason_code",
            ],
            "warn_rules": [
                "human_input_required",
                "recovered_after_retry",
            ],
        }
    manifest = _read_json(manifest_path)
    capabilities = _as_dict(manifest.get("capabilities"))
    relative_path = _safe_str(capabilities.get("quality_policy"))
    if not relative_path:
        return {}
    return _load_quality_policy_doc(
        repo_root=repo_root,
        family=family,
        bundle_key=bundle_key,
        relative_path=relative_path,
    )


def _match_prefixes(value: str, prefixes: list[Any]) -> bool:
    token = _safe_str(value).lower()
    for row in prefixes:
        prefix = _safe_str(row).lower()
        if prefix and token.startswith(prefix):
            return True
    return False


def _select_node_profile(policy: dict[str, Any], row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    for profile_name, profile in _as_dict(policy.get("node_profiles")).items():
        match = _as_dict(_as_dict(profile).get("match"))
        if _match_prefixes(_safe_str(row.get("node_id")), _as_list(match.get("node_id_prefixes"))):
            return _safe_str(profile_name), _as_dict(profile)
        if _safe_str(row.get("node_id")) in [_safe_str(item) for item in _as_list(match.get("stage_names"))]:
            return _safe_str(profile_name), _as_dict(profile)
    return "", {}


def _status_score(status: str, *, blocked_human_input: bool = False, recovered: bool = False) -> int:
    token = _safe_str(status).lower()
    if token == "failed":
        return 0
    if token == "blocked":
        return 55 if blocked_human_input else 45
    if token == "retry":
        return 85 if recovered else 70
    return 100


def _score_node(row: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    penalties = _as_dict(profile.get("penalties"))
    score = _safe_int(profile.get("base_score"), 100)
    reasons: list[str] = []
    status = _safe_str(row.get("status")).lower()
    if status == "failed":
        score = 0
        reasons.append("status=failed")
    elif status == "blocked" and bool(row.get("human_input_required")):
        score -= _safe_int(penalties.get("blocked_human_input_required"), 45)
        reasons.append("blocked_human_input_required")
    elif status == "retry" and bool(row.get("recovered_after_retry")):
        score -= _safe_int(penalties.get("retry_recovered"), 15)
        reasons.append("retry_recovered")
    if not bool(row.get("parser_ok")):
        score -= _safe_int(penalties.get("parser_error"), 30)
        reasons.append("parser_error")
    if not bool(row.get("raw_validate_ok")):
        score -= _safe_int(penalties.get("raw_validate_failed"), 25)
        reasons.append("raw_validate_failed")
    if not bool(row.get("final_validate_ok")):
        score -= _safe_int(penalties.get("final_validate_failed"), 25)
        reasons.append("final_validate_failed")
    if bool(row.get("empty_output")):
        score -= _safe_int(penalties.get("empty_output"), 20)
        reasons.append("empty_output")
    if _safe_int(row.get("llm_call_count")) > 0 and not bool(row.get("provider_raw_captured")):
        score -= _safe_int(penalties.get("missing_provider_raw"), 10)
        reasons.append("missing_provider_raw")
    if _safe_int(row.get("llm_call_count")) > 0 and not bool(row.get("structured_response_captured")):
        score -= _safe_int(penalties.get("missing_structured_response"), 10)
        reasons.append("missing_structured_response")
    if _safe_str(row.get("skill_name")) and status == "completed" and not _as_list(row.get("produced_output_keys")) and not bool(row.get("ask_user")):
        score -= _safe_int(penalties.get("missing_produced_output_keys"), 15)
        reasons.append("missing_produced_output_keys")
    for ref_name in _as_list(profile.get("required_refs")):
        if not row.get(_safe_str(ref_name)):
            score -= 10
            reasons.append(f"missing_ref:{_safe_str(ref_name)}")
    for fact_name in _as_list(profile.get("required_facts")):
        if row.get(_safe_str(fact_name)) in (None, "", [], {}):
            score -= 10
            reasons.append(f"missing_fact:{_safe_str(fact_name)}")
    score = max(0, min(100, score))
    severity = "pass"
    if status == "failed" or score < 60:
        severity = "fail"
    elif status == "blocked" and bool(row.get("human_input_required")):
        severity = "block"
    elif score < 85 or reasons:
        severity = "warn"
    return {
        **row,
        "profile": _safe_str(profile.get("match")),
        "score": score,
        "severity": severity,
        "reasons": reasons,
    }


def _select_skill_profile(policy: dict[str, Any], row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    skill_name = _safe_str(row.get("skill_name"))
    for profile_name, profile in _as_dict(policy.get("skill_profiles")).items():
        match = _as_dict(_as_dict(profile).get("match"))
        if skill_name and skill_name in [_safe_str(item) for item in _as_list(match.get("skill_names"))]:
            return _safe_str(profile_name), _as_dict(profile)
    return "", {}


def _score_skill(row: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    weights = _as_dict(profile.get("phase_weights"))
    score_caps = _as_dict(profile.get("score_caps"))
    weighted_score = 0.0
    used_weight = 0.0
    for phase_name, weight in weights.items():
        status = _safe_str(row.get(f"{phase_name}_status"))
        if not status:
            continue
        w = float(weight or 0)
        weighted_score += _status_score(
            status,
            blocked_human_input=_safe_str(row.get("final_reason_code")) == "human_input_required",
            recovered=_safe_int(row.get("retry_count")) > 0 and _safe_str(row.get("final_action")) in {"continue", "ask_user"},
        ) * w
        used_weight += w
    score = int(round(weighted_score / used_weight)) if used_weight > 0 else 100
    if _safe_str(row.get("parser_error")) or _safe_int(row.get("validator_error_count")) > 0:
        score = min(score, _safe_int(score_caps.get("parser_or_validator_error_max"), 79))
    if _safe_str(row.get("final_action")) == "ask_user":
        score = min(score, _safe_int(score_caps.get("ask_user_valid_max"), 75))
    retry_count = _safe_int(row.get("retry_count"))
    if retry_count > 0 and _safe_str(row.get("final_action")) in {"continue", "ask_user"}:
        score = max(0, score - (retry_count * 5))
    reasons: list[str] = []
    for field in _as_list(profile.get("critical_checks")):
        token = _safe_str(field)
        if token and row.get(token) in (False, "", None, [], {}):
            reasons.append(f"critical_check_failed:{token}")
    if _safe_str(row.get("parser_error")):
        reasons.append("parser_error")
    if _safe_int(row.get("validator_error_count")) > 0:
        reasons.append(f"validator_errors:{_safe_int(row.get('validator_error_count'))}")
    severity = "pass"
    if _safe_str(row.get("final_action")) == "fail" or score < 60:
        severity = "fail"
    elif _safe_str(row.get("final_action")) == "ask_user":
        severity = "block"
    elif score < 85 or reasons:
        severity = "warn"
    return {
        **row,
        "profile": _safe_str(profile.get("match")),
        "score": max(0, min(100, score)),
        "severity": severity,
        "reasons": reasons,
    }


def _select_lane_profile(policy: dict[str, Any], row: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    task_id = _safe_str(row.get("task_id"))
    for profile_name, profile in _as_dict(policy.get("lane_profiles")).items():
        match = _as_dict(_as_dict(profile).get("match"))
        if task_id and task_id in [_safe_str(item) for item in _as_list(match.get("task_ids"))]:
            return _safe_str(profile_name), _as_dict(profile)
    return "", {}


def _score_lane(row: dict[str, Any], node_reports: list[dict[str, Any]], skill_reports: list[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any]:
    weights = _as_dict(_as_dict(profile).get("score_weights"))
    matching_skills = [item for item in skill_reports if _safe_str(item.get("task_id")) == _safe_str(row.get("task_id"))]
    matching_nodes = [item for item in node_reports if _safe_str(item.get("task_id")) == _safe_str(row.get("task_id"))]
    skill_average = int(round(sum(_safe_int(item.get("score"), 100) for item in matching_skills) / float(len(matching_skills)))) if matching_skills else 100
    unresolved_failed = any(_safe_str(item.get("severity")) == "fail" and not bool(item.get("recovered_after_retry")) for item in matching_nodes)
    blocked_human = any(bool(item.get("human_input_required")) and _safe_str(item.get("status")) == "blocked" for item in matching_nodes)
    if unresolved_failed:
        blocker_score = 0
    elif blocked_human:
        blocker_score = 70
    else:
        blocker_score = 100
    produced_keys = {
        _safe_str(item)
        for node in matching_nodes
        for item in _as_list(node.get("produced_output_keys"))
        if _safe_str(item)
    }
    coverage_score = 100 if produced_keys else (60 if any(_safe_str(item.get("status")) == "completed" for item in matching_nodes) else 0)
    density_score = max(0, 100 - int(round(((_safe_int(row.get("retry_count")) + _safe_int(row.get("blocked_count"))) / float(max(_safe_int(row.get("node_count")), 1))) * 100)))
    score = int(round(
        skill_average * float(weights.get("skill_average", 0.4))
        + blocker_score * float(weights.get("unresolved_blocker", 0.3))
        + coverage_score * float(weights.get("produced_output_coverage", 0.2))
        + density_score * float(weights.get("retry_block_density", 0.1))
    ))
    if unresolved_failed:
        score = min(score, 59)
    elif blocked_human:
        score = max(60, min(score, 75))
    severity = "pass"
    if unresolved_failed or score < 60:
        severity = "fail"
    elif blocked_human:
        severity = "block"
    elif score < 85:
        severity = "warn"
    return {
        **row,
        "profile": _safe_str(profile.get("match")),
        "score": max(0, min(100, score)),
        "severity": severity,
        "skill_average": skill_average,
        "blocker_score": blocker_score,
        "coverage_score": coverage_score,
        "density_score": density_score,
    }


def _quality_refs(bundle_dir: Path) -> dict[str, str]:
    return {
        "context": str(bundle_dir / "quality" / "context.json"),
        "manifest": str(bundle_dir / "quality" / "manifest.json"),
        "nodes": str(bundle_dir / "quality" / "reports" / "nodes.json"),
        "skills": str(bundle_dir / "quality" / "reports" / "skills.json"),
        "lanes": str(bundle_dir / "quality" / "reports" / "lanes.json"),
        "summary": str(bundle_dir / "quality" / "reports" / "summary.json"),
    }


def build_bundle_quality_reports(
    *,
    repo_root: Path,
    bundle_dir: str,
    flow_id: str,
    snapshot: dict[str, Any] | None,
    current_view: dict[str, Any] | None,
    goal_completion_mode: str = "",
) -> dict[str, Any]:
    bundle_path = Path(bundle_dir)
    context = resolve_quality_context(
        bundle_dir=bundle_path,
        flow_id=flow_id,
        snapshot=snapshot,
        current_view=current_view,
        goal_completion_mode=goal_completion_mode,
    )
    policy = load_quality_policy(repo_root=repo_root, context=context)
    raw_dir = bundle_path / "quality" / "raw"
    node_rows = _read_jsonl(raw_dir / "nodes.jsonl")
    skill_rows = _read_jsonl(raw_dir / "skills.jsonl")
    lane_rows = _read_jsonl(raw_dir / "lanes.jsonl")

    node_reports = [_score_node(row, _select_node_profile(policy, row)[1]) for row in node_rows]
    skill_reports = [_score_skill(row, _select_skill_profile(policy, row)[1]) for row in skill_rows]
    lane_reports = [_score_lane(row, node_reports, skill_reports, _select_lane_profile(policy, row)[1]) for row in lane_rows]

    node_reports.sort(key=lambda row: (_safe_int(row.get("score"), 100), _safe_int(row.get("sequence"), 0), _safe_str(row.get("trace_id"))))
    skill_reports.sort(key=lambda row: (_safe_int(row.get("score"), 100), _safe_str(row.get("skill_name")), _safe_str(row.get("attempt_id"))))
    lane_reports.sort(key=lambda row: (_safe_int(row.get("score"), 100), _safe_str(row.get("lane_id"))))

    top_level_contract_missing = [
        name
        for name in ("failure_summary.json", "execution_traces.json", "timeline.json", "node_trace_timeline.json")
        if not (bundle_path / name).exists()
    ]
    hard_fail_reasons: list[str] = []
    if top_level_contract_missing:
        hard_fail_reasons.append(f"missing_bundle_top_level_contract:{','.join(top_level_contract_missing)}")
    if not node_rows or not skill_rows or not lane_rows:
        hard_fail_reasons.append("quality_raw_missing")
    failure_summary = _read_json(bundle_path / "failure_summary.json")
    if failure_summary and (_safe_str(failure_summary.get("first_bad_node")) or _safe_str(failure_summary.get("trace_id"))):
        if not _safe_str(failure_summary.get("primary_reason_code")) or not _safe_str(failure_summary.get("failure_class")):
            hard_fail_reasons.append("observability_contract_missing_reason_code")

    warnings: list[str] = []
    for collection in (node_reports, skill_reports, lane_reports):
        for row in collection:
            if _safe_str(row.get("severity")) in {"warn", "block"}:
                label = _safe_str(row.get("trace_id")) or _safe_str(row.get("attempt_id")) or _safe_str(row.get("lane_id"))
                warnings.append(f"{_safe_str(row.get('severity'))}:{label}:{','.join(_as_list(row.get('reasons')))}")
    score_parts = []
    if node_reports:
        score_parts.append(sum(_safe_int(row.get("score"), 100) for row in node_reports) / float(len(node_reports)))
    if skill_reports:
        score_parts.append(sum(_safe_int(row.get("score"), 100) for row in skill_reports) / float(len(skill_reports)))
    if lane_reports:
        score_parts.append(sum(_safe_int(row.get("score"), 100) for row in lane_reports) / float(len(lane_reports)))
    overall_score = int(round(sum(score_parts) / float(len(score_parts)))) if score_parts else 0
    refs = _quality_refs(bundle_path)

    _write_json(bundle_path / "quality" / "reports" / "nodes.json", {"rows": node_reports})
    _write_json(bundle_path / "quality" / "reports" / "skills.json", {"rows": skill_reports})
    _write_json(bundle_path / "quality" / "reports" / "lanes.json", {"rows": lane_reports})
    summary = {
        "contract_version": "bundle_quality.v1",
        "flow_id": _safe_str(context.get("flow_id")),
        "bundle_family": _safe_str(context.get("bundle_family")),
        "bundle_key": _safe_str(context.get("bundle_key")),
        "policy_version": _safe_str(policy.get("policy_version")) or "quality_policy.v1",
        "score": overall_score,
        "passed": not hard_fail_reasons,
        "hard_fail_reasons": hard_fail_reasons,
        "warnings": warnings[:20],
        "worst_node": node_reports[0] if node_reports else {},
        "worst_skill": skill_reports[0] if skill_reports else {},
        "worst_lane": lane_reports[0] if lane_reports else {},
        "counts": {
            "node_count": len(node_reports),
            "skill_count": len(skill_reports),
            "lane_count": len(lane_reports),
        },
        "refs": refs,
    }
    _write_json(bundle_path / "quality" / "reports" / "summary.json", summary)
    manifest = _read_json(bundle_path / "quality" / "manifest.json")
    manifest.update(
        {
            "contract_version": "bundle_quality.v1",
            "generated_at": manifest.get("generated_at") or "",
            "report_refs": refs,
        }
    )
    _write_json(bundle_path / "quality" / "manifest.json", manifest)
    return summary


def merge_bundle_quality_report(*, base_report: dict[str, Any] | None, quality_summary: dict[str, Any] | None) -> dict[str, Any]:
    base = dict(base_report) if isinstance(base_report, dict) else {}
    summary = dict(quality_summary) if isinstance(quality_summary, dict) else {}
    merged = dict(base)
    merged.setdefault("score", _safe_int(summary.get("score"), 0))
    merged.setdefault("passed", not _as_list(summary.get("hard_fail_reasons")))
    merged.setdefault("failures", [])
    merged["quality_summary_ref"] = _as_dict(summary.get("refs")).get("summary")
    merged["worst_node"] = _as_dict(summary.get("worst_node"))
    merged["worst_skill"] = _as_dict(summary.get("worst_skill"))
    merged["worst_lane"] = _as_dict(summary.get("worst_lane"))
    if _as_list(summary.get("hard_fail_reasons")):
        merged["passed"] = False
        merged["failures"] = [
            *[item for item in _as_list(base.get("failures")) if _safe_str(item)],
            *[f"quality:{_safe_str(item)}" for item in _as_list(summary.get("hard_fail_reasons")) if _safe_str(item)],
        ]
    merged["warnings"] = [
        *[item for item in _as_list(base.get("warnings")) if _safe_str(item)],
        *[item for item in _as_list(summary.get("warnings")) if _safe_str(item)],
    ]
    return merged


__all__ = [
    "build_bundle_quality_reports",
    "load_quality_policy",
    "merge_bundle_quality_report",
    "resolve_quality_context",
]
