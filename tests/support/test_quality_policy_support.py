from __future__ import annotations

import json
from pathlib import Path

from scripts._support.flow_score_support import build_flow_scores
from scripts._support.quality_policy_support import (
    build_bundle_quality_reports,
    merge_bundle_quality_report,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_build_bundle_quality_reports_writes_summary_and_refs(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "session:demo"
    bundle_dir.mkdir(parents=True)
    for name in ("failure_summary.json", "execution_traces.json", "timeline.json", "node_trace_timeline.json"):
        (bundle_dir / name).write_text("{}", encoding="utf-8")
    _write_jsonl(
        bundle_dir / "quality" / "raw" / "nodes.jsonl",
        [
            {
                "trace_id": "trace-1",
                "node_id": "skill_run/llm_structured",
                "node_path": "skill_run/llm_structured",
                "sequence": 10,
                "skill_name": "file-classify",
                "task_id": "materials_classify",
                "status": "retry",
                "failure_class": "upstream_dependency",
                "primary_reason_code": "llm_provider_unavailable",
                "reason_code_chain": ["llm_provider_unavailable"],
                "duration_ms": 123,
                "prompt_tokens": 10,
                "completion_tokens": 0,
                "tool_call_count": 0,
                "llm_call_count": 1,
                "provider_raw_captured": True,
                "structured_response_captured": True,
                "parser_ok": True,
                "raw_validate_ok": True,
                "final_validate_ok": True,
                "output_contract_ok": True,
                "ask_user": False,
                "human_input_required": False,
                "recovered_after_retry": True,
                "empty_output": False,
                "produced_output_keys": ["analysis_view"],
                "changed_fields": ["data.analysis.summary"],
                "state_input_ref": "",
                "state_output_ref": "",
                "skill_stage_refs": [],
                "llm_call_refs": [],
                "contract_diff_refs": [],
            }
        ],
    )
    _write_jsonl(
        bundle_dir / "quality" / "raw" / "skills.jsonl",
        [
            {
                "skill_name": "file-classify",
                "attempt_id": "attempt-1",
                "task_id": "materials_classify",
                "prepare_status": "completed",
                "preprocess_status": "completed",
                "mode_guard_status": "completed",
                "llm_stage_status": "retry",
                "envelope_action_status": "completed",
                "finalize_status": "completed",
                "prompt_contract_ok": True,
                "prefetch_ok": True,
                "llm_admission_ok": True,
                "provider_raw_captured": True,
                "parser_error": "",
                "validator_error_count": 0,
                "retry_count": 1,
                "final_action": "continue",
                "final_reason_code": "",
                "trace_ids": ["trace-1"],
                "skill_stage_dir": "/tmp/skill",
                "skill_stage_refs": [],
                "llm_call_refs": [],
            }
        ],
    )
    _write_jsonl(
        bundle_dir / "quality" / "raw" / "lanes.jsonl",
        [
            {
                "lane_id": "materials:materials_classify",
                "task_id": "materials_classify",
                "phase": "materials",
                "node_count": 1,
                "skill_count": 1,
                "retry_count": 1,
                "blocked_count": 0,
                "ask_user_count": 0,
                "first_unresolved_bad": {},
                "trace_ids": ["trace-1"],
                "skill_attempt_ids": ["attempt-1"],
            }
        ],
    )

    summary = build_bundle_quality_reports(
        bundle_dir=str(bundle_dir),
        flow_id="analysis",
        snapshot={
            "matter": {"service_type_id": "civil_prosecution", "id": "42"},
            "analysis_state": {"workflow_model": {"service_type_id": "civil_prosecution"}},
        },
        current_view={"summary": "ok"},
        goal_completion_mode="card",
    )

    assert summary["contract_version"] == "bundle_quality.v1"
    assert summary["counts"]["node_count"] == 1
    assert Path(summary["refs"]["summary"]).exists()
    assert summary["worst_node"]["trace_id"] == "trace-1"
    assert not any(str(reason).startswith("unknown_") for reason in summary["hard_fail_reasons"])


def test_document_drafting_quality_policy_matches_dynamic_lanes_and_support_profiles(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "session:template-draft"
    bundle_dir.mkdir(parents=True)
    for name in ("failure_summary.json", "execution_traces.json", "timeline.json", "node_trace_timeline.json"):
        (bundle_dir / name).write_text("{}", encoding="utf-8")

    _write_jsonl(
        bundle_dir / "quality" / "raw" / "nodes.jsonl",
        [
            {
                "trace_id": "trace-1",
                "node_id": "materials",
                "node_path": "workbench/materials",
                "sequence": 12,
                "skill_name": "",
                "task_id": "materials",
                "status": "completed",
                "failure_class": "",
                "primary_reason_code": "",
                "reason_code_chain": [],
                "duration_ms": 100,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "tool_call_count": 0,
                "llm_call_count": 0,
                "provider_raw_captured": False,
                "structured_response_captured": False,
                "parser_ok": True,
                "raw_validate_ok": True,
                "final_validate_ok": True,
                "output_contract_ok": True,
                "ask_user": False,
                "human_input_required": False,
                "recovered_after_retry": False,
                "empty_output": False,
                "produced_output_keys": ["data.files.classifications"],
                "changed_fields": ["data.files.classifications"],
                "state_input_ref": "",
                "state_output_ref": "",
                "skill_stage_refs": [],
                "llm_call_refs": [],
                "contract_diff_refs": [],
            }
        ],
    )
    _write_jsonl(
        bundle_dir / "quality" / "raw" / "skills.jsonl",
        [
            {
                "skill_name": "document-intake",
                "attempt_id": "attempt-1",
                "task_id": "",
                "prepare_status": "completed",
                "preprocess_status": "completed",
                "mode_guard_status": "completed",
                "llm_stage_status": "completed",
                "envelope_action_status": "completed",
                "finalize_status": "completed",
                "prompt_contract_ok": True,
                "prefetch_ok": True,
                "llm_admission_ok": True,
                "provider_raw_captured": True,
                "parser_error": "",
                "validator_error_count": 0,
                "retry_count": 0,
                "final_action": "continue",
                "final_reason_code": "validated",
                "trace_ids": ["trace-1"],
                "skill_stage_dir": "/tmp/skill",
                "skill_stage_refs": [],
                "llm_call_refs": [],
            }
        ],
    )
    _write_jsonl(
        bundle_dir / "quality" / "raw" / "lanes.jsonl",
        [
            {
                "lane_id": "analysis:1173",
                "task_id": "1173",
                "phase": "analysis",
                "node_count": 1,
                "skill_count": 0,
                "retry_count": 0,
                "blocked_count": 0,
                "ask_user_count": 0,
                "first_unresolved_bad": {},
                "trace_ids": ["trace-1"],
                "skill_attempt_ids": [],
            }
        ],
    )

    summary = build_bundle_quality_reports(
        bundle_dir=str(bundle_dir),
        flow_id="template_draft",
        snapshot={
            "matter": {
                "service_type_id": "document_drafting",
                "id": "1173",
                "session_id": "session:2533",
            }
        },
        current_view={"summary": "ok"},
        goal_completion_mode="none",
    )

    assert "unknown_node_profile:materials" not in summary["hard_fail_reasons"]
    assert "unknown_skill_profile:document-intake" not in summary["hard_fail_reasons"]
    assert "unknown_lane_profile:analysis:1173" not in summary["hard_fail_reasons"]


def test_merge_bundle_quality_report_and_flow_scores_include_quality_failures() -> None:
    quality_summary = {
        "refs": {"summary": "/tmp/quality/summary.json"},
        "worst_node": {"trace_id": "trace-1"},
        "worst_skill": {"skill_name": "file-classify"},
        "worst_lane": {"lane_id": "materials:materials_classify"},
        "hard_fail_reasons": ["quality_raw_missing"],
        "warnings": ["warn:trace-1:retry_recovered"],
    }
    merged = merge_bundle_quality_report(
        base_report={"score": 80, "passed": True, "failures": ["base_failure"]},
        quality_summary=quality_summary,
    )
    assert merged["passed"] is False
    assert merged["quality_summary_ref"] == "/tmp/quality/summary.json"
    assert "quality:quality_raw_missing" in merged["failures"]

    scores = build_flow_scores(
        flow_id="analysis",
        seen_cards=[],
        pending_card={},
        snapshot={"analysis_state": {"current_node": "goal_completion", "current_phase": "analysis"}},
        current_view={
            "summary": "这是一个足够长的案件分析摘要。" * 8,
            "issues": [{"issue_id": "i1"}],
            "strategy_options": [{"strategy_id": "s1"}],
            "risk_assessment": {"key_risks": [{"title": "证据风险"}]},
            "result_contract_diagnostics": {"status": "valid"},
        },
        aux_views={"pricing_view": {"status": "ready"}},
        deliverables={},
        deliverable_text="",
        deliverable_status="ready",
        observability={"matter_traces": [{"node_id": "analysis_output", "task_id": "goal_completion"}], "errors": {}},
        bundle_quality_summary=quality_summary,
        goal_completion_mode="card",
    )
    assert scores["node_path_score"]["quality_summary_ref"] == "/tmp/quality/summary.json"
    assert scores["overall_e2e_score"]["passed"] is False
    assert "quality:quality_raw_missing" in scores["overall_e2e_score"]["failures"]


def test_build_bundle_quality_reports_fails_on_analysis_chain_prompt_and_placeholder_regressions(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "session:analysis-replay"
    bundle_dir.mkdir(parents=True)
    for name in ("failure_summary.json", "execution_traces.json", "timeline.json", "node_trace_timeline.json"):
        (bundle_dir / name).write_text("{}", encoding="utf-8")

    _write_jsonl(
        bundle_dir / "quality" / "raw" / "nodes.jsonl",
        [
            {
                "trace_id": "trace-intake",
                "node_id": "intake",
                "node_path": "workbench/intake",
                "sequence": 15,
                "skill_name": "civil-analysis-intake",
                "task_id": "case_intake",
                "status": "completed",
                "failure_class": "",
                "primary_reason_code": "",
                "reason_code_chain": [],
                "duration_ms": 80,
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "tool_call_count": 0,
                "llm_call_count": 1,
                "provider_raw_captured": True,
                "structured_response_captured": True,
                "parser_ok": True,
                "raw_validate_ok": True,
                "final_validate_ok": True,
                "output_contract_ok": True,
                "prompt_ack_only_context": True,
                "prompt_latest_human_empty": False,
                "prompt_material_context_fileid_only": True,
                "prompt_has_readable_material_context": False,
                "placeholder_profile_fields": ["plaintiff", "defendant", "claims"],
                "placeholder_profile_count": 3,
                "ask_user": True,
                "human_input_required": True,
                "recovered_after_retry": False,
                "empty_output": False,
                "produced_output_keys": ["profile.summary"],
                "changed_fields": ["profile.summary"],
                "state_input_ref": "",
                "state_output_ref": "",
                "skill_stage_refs": [],
                "llm_call_refs": [],
                "contract_diff_refs": [],
            }
        ],
    )
    _write_jsonl(
        bundle_dir / "quality" / "raw" / "skills.jsonl",
        [
            {
                "skill_name": "civil-analysis-intake",
                "attempt_id": "attempt-intake",
                "task_id": "case_intake",
                "prepare_status": "completed",
                "preprocess_status": "completed",
                "mode_guard_status": "completed",
                "llm_stage_status": "completed",
                "envelope_action_status": "completed",
                "finalize_status": "completed",
                "prompt_contract_ok": True,
                "prefetch_ok": True,
                "llm_admission_ok": True,
                "provider_raw_captured": True,
                "prompt_ack_only_context": True,
                "prompt_latest_human_empty": False,
                "prompt_material_context_fileid_only": True,
                "prompt_has_readable_material_context": False,
                "placeholder_profile_fields": ["plaintiff", "defendant", "claims"],
                "placeholder_profile_count": 3,
                "parser_error": "",
                "validator_error_count": 0,
                "retry_count": 0,
                "final_action": "continue",
                "final_reason_code": "",
                "trace_ids": ["trace-intake"],
                "skill_stage_dir": "/tmp/skill",
                "skill_stage_refs": [],
                "llm_call_refs": [],
            }
        ],
    )
    _write_jsonl(
        bundle_dir / "quality" / "raw" / "lanes.jsonl",
        [
            {
                "lane_id": "intake:case_intake",
                "task_id": "case_intake",
                "phase": "intake",
                "node_count": 1,
                "skill_count": 1,
                "retry_count": 0,
                "blocked_count": 1,
                "ask_user_count": 1,
                "first_unresolved_bad": {},
                "trace_ids": ["trace-intake"],
                "skill_attempt_ids": ["attempt-intake"],
            }
        ],
    )

    summary = build_bundle_quality_reports(
        bundle_dir=str(bundle_dir),
        flow_id="analysis",
        snapshot={
            "matter": {"service_type_id": "civil_prosecution", "id": "42"},
            "rule_bundle": {"bundle_family": "analysis", "bundle_key": "private_lending"},
        },
        current_view={"summary": "ok"},
        goal_completion_mode="card",
    )

    assert summary["passed"] is False
    assert "skill_quality_failed:civil-analysis-intake" in summary["hard_fail_reasons"]
    reasons = summary["worst_skill"]["reasons"]
    assert "prompt_ack_only_context" in reasons
    assert "prompt_material_context_fileid_only" in reasons
    assert any(str(item).startswith("placeholder_profile_output:") for item in reasons)


def test_build_bundle_quality_reports_skips_unknown_profile_hard_fail_without_policy_catalog(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "session:unknown-profile"
    bundle_dir.mkdir(parents=True)
    for name in ("failure_summary.json", "execution_traces.json", "timeline.json", "node_trace_timeline.json"):
        (bundle_dir / name).write_text("{}", encoding="utf-8")

    _write_jsonl(
        bundle_dir / "quality" / "raw" / "nodes.jsonl",
        [
            {
                "trace_id": "trace-unknown",
                "node_id": "custom_uncovered_node",
                "node_path": "workbench/custom_uncovered_node",
                "sequence": 1,
                "skill_name": "",
                "task_id": "unknown_task",
                "status": "completed",
                "failure_class": "",
                "primary_reason_code": "",
                "reason_code_chain": [],
                "duration_ms": 1,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "tool_call_count": 0,
                "llm_call_count": 0,
                "provider_raw_captured": False,
                "structured_response_captured": False,
                "parser_ok": True,
                "raw_validate_ok": True,
                "final_validate_ok": True,
                "output_contract_ok": True,
                "ask_user": False,
                "human_input_required": False,
                "recovered_after_retry": False,
                "empty_output": False,
                "produced_output_keys": [],
                "changed_fields": [],
                "state_input_ref": "",
                "state_output_ref": "",
                "skill_stage_refs": [],
                "llm_call_refs": [],
                "contract_diff_refs": [],
            }
        ],
    )
    _write_jsonl(bundle_dir / "quality" / "raw" / "skills.jsonl", [])
    _write_jsonl(bundle_dir / "quality" / "raw" / "lanes.jsonl", [])

    summary = build_bundle_quality_reports(
        bundle_dir=str(bundle_dir),
        flow_id="analysis",
        snapshot={
            "matter": {"service_type_id": "civil_prosecution", "id": "42"},
            "rule_bundle": {"bundle_family": "analysis", "bundle_key": "private_lending"},
        },
        current_view={"summary": "ok"},
        goal_completion_mode="card",
    )

    assert summary["passed"] is False
    assert "quality_raw_missing" in summary["hard_fail_reasons"]
    assert "unknown_node_profile:custom_uncovered_node" not in summary["hard_fail_reasons"]
