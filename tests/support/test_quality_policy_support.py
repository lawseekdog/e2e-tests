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
    repo_root = Path(__file__).resolve().parents[3]
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
        repo_root=repo_root,
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
