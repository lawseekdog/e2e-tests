from __future__ import annotations

from scripts._support.flow_score_support import (
    build_flow_scores,
    build_template_flow_scores,
    score_unexpected_cards,
)


def test_score_unexpected_cards_flags_forbidden_skill_and_data_group() -> None:
    score = score_unexpected_cards(
        flow_id="analysis",
        seen_cards=[
            {
                "skill_id": "skill-error-analysis",
                "task_key": "x",
                "review_type": "clarify",
                "questions": [{"field_key": "data.unknown.bad", "input_type": "text"}],
            }
        ],
    )

    assert score["passed"] is False
    assert score["unexpected_count"] == 1
    assert "forbidden_skill:skill-error-analysis" in score["unexpected_cards"][0]["reasons"]


def test_build_flow_scores_produces_all_four_scores_for_analysis() -> None:
    snapshot = {"analysis_state": {"current_node": "goal_completion", "current_phase": "analysis"}}
    analysis_view = {
        "summary": "这是一个足够长的案件分析摘要。" * 8,
        "issues": [{"issue_id": "i1"}],
        "strategy_options": [{"strategy_id": "s1"}],
        "risk_assessment": {"key_risks": [{"title": "证据风险"}]},
        "result_contract_diagnostics": {"status": "valid"},
    }
    pricing_view = {"status": "ready"}
    scores = build_flow_scores(
        flow_id="analysis",
        seen_cards=[{"skill_id": "goal-completion", "task_key": "goal_completion", "review_type": "select", "questions": [{"field_key": "data.workbench.goal"}]}],
        pending_card={"skill_id": "goal-completion", "task_key": "goal_completion", "review_type": "select", "questions": [{"field_key": "data.workbench.goal"}]},
        snapshot=snapshot,
        current_view=analysis_view,
        aux_views={"pricing_view": pricing_view},
        deliverables={},
        deliverable_status="ready",
        observability={
            "matter_traces": [{"node_id": "analysis_output", "task_id": "goal_completion"}],
            "session_traces": [{"node_id": "pricing_plan", "task_id": "analysis"}],
            "phase_timeline": {"phases": [{"id": "analysis", "status": "completed"}]},
            "matter_timeline": {"rounds": [{"content": {"produced_output_keys": ["analysis_view", "pricing_plan_view"]}}]},
            "session_timeline": {"rounds": []},
            "errors": {},
        },
        goal_completion_mode="card",
    )

    assert "unexpected_card_score" in scores
    assert "node_path_score" in scores
    assert "snapshot_progress_score" in scores
    assert "deliverable_quality_score" in scores
    assert scores["overall_e2e_score"]["score"] > 0


def test_build_template_flow_scores_uses_existing_dialogue_and_document_quality() -> None:
    scores = build_template_flow_scores(
        cards=[{"skill_id": "goal-completion", "task_key": "goal_completion", "review_type": "select", "questions": [{"field_key": "data.workbench.goal"}]}],
        pending_card={},
        node_timeline=[
            {"docgen_node": "intake"},
            {"docgen_node": "compose"},
            {"docgen_node": "render"},
            {"docgen_node": "sync"},
            {"docgen_node": "finish"},
        ],
        summary={"latest_docgen_node": "finish"},
        last_docgen_snapshot={
            "current_phase": "docgen",
            "current_task_id": "goal_completion",
            "template_quality_contracts_json_exists": True,
            "quality_review_decision": "pass",
            "deliverable": {"status": "completed"},
        },
        dialogue_quality={"pass": True},
        document_quality={"pass": True, "citation_count": 2, "fact_coverage_score": 90.0},
    )

    assert scores["node_path_score"]["passed"] is True
    assert scores["deliverable_quality_score"]["passed"] is True
    assert scores["overall_e2e_score"]["passed"] is True
