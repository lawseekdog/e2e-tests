from __future__ import annotations

from scripts._support.flow_score_support import (
    build_flow_scores,
    build_legal_opinion_formal_ready_report,
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


def test_score_unexpected_cards_reads_nested_card_questions() -> None:
    score = score_unexpected_cards(
        flow_id="template_draft",
        seen_cards=[
            {
                "skill_id": "document-intake",
                "task_key": "workflow_input_document_drafting_intake_document-intake",
                "review_type": "clarify",
                "card": {
                    "questions": [
                        {
                            "field_key": "profile.parties",
                            "input_type": "textarea",
                            "required": True,
                        }
                    ]
                },
            }
        ],
    )

    assert score["passed"] is True
    assert score["unexpected_count"] == 0


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


def test_build_flow_scores_enforces_contract_review_v2_expectations() -> None:
    snapshot = {"analysis_state": {"current_node": "goal_completion", "current_phase": "contract_review"}}
    contract_view = {
        "summary": "这是一个足够长的合同审查摘要。" * 8,
        "contract_type_id": "construction",
        "review_scope": "full",
        "overall_risk_level": "high",
        "result_contract_diagnostics": {"status": "valid"},
        "clauses": [
            {"clause_id": "c1", "risk_type": "effectiveness", "risk_level": "high", "anchor_refs": [{"anchor_id": "a1"}], "law_ref_ids": ["law_1"]},
            {"clause_id": "c2", "risk_type": "payment", "risk_level": "medium", "anchor_refs": [{"anchor_id": "a2"}], "law_ref_ids": ["law_2"]},
            {"clause_id": "c3", "risk_type": "tax_invoice", "risk_level": "low", "anchor_refs": [{"anchor_id": "a3"}], "law_ref_ids": []},
            {"clause_id": "c4", "risk_type": "delivery_acceptance", "risk_level": "low", "anchor_refs": [{"anchor_id": "a4"}], "law_ref_ids": []},
            {"clause_id": "c5", "risk_type": "quality", "risk_level": "low", "anchor_refs": [{"anchor_id": "a5"}], "law_ref_ids": []},
            {"clause_id": "c6", "risk_type": "change_order", "risk_level": "low", "anchor_refs": [{"anchor_id": "a6"}], "law_ref_ids": []},
            {"clause_id": "c7", "risk_type": "delay", "risk_level": "low", "anchor_refs": [{"anchor_id": "a7"}], "law_ref_ids": []},
            {"clause_id": "c8", "risk_type": "liability", "risk_level": "low", "anchor_refs": [{"anchor_id": "a8"}], "law_ref_ids": []},
            {"clause_id": "c9", "risk_type": "indemnity", "risk_level": "low", "anchor_refs": [{"anchor_id": "a9"}], "law_ref_ids": []},
            {"clause_id": "c10", "risk_type": "termination", "risk_level": "low", "anchor_refs": [{"anchor_id": "a10"}], "law_ref_ids": []},
            {"clause_id": "c11", "risk_type": "compliance", "risk_level": "low", "anchor_refs": [{"anchor_id": "a11"}], "law_ref_ids": []},
            {"clause_id": "c12", "risk_type": "dispute_resolution", "risk_level": "low", "anchor_refs": [{"anchor_id": "a12"}], "law_ref_ids": []},
            {"clause_id": "c13", "risk_type": "notice", "risk_level": "low", "anchor_refs": [{"anchor_id": "a13"}], "law_ref_ids": []},
        ],
    }
    expectations = {
        "contract_type_id": "construction",
        "review_scope": "full",
        "required_output_keys": ["contract_review_report", "modification_suggestion", "redline_comparison"],
        "mandatory_issue_types": [
            "effectiveness",
            "payment",
            "tax_invoice",
            "delivery_acceptance",
            "quality",
            "change_order",
            "delay",
            "liability",
            "indemnity",
            "termination",
            "compliance",
            "dispute_resolution",
            "notice",
        ],
        "required_section_markers": ["合同审查意见书", "审查范围"],
    }
    report_text = (
        "合同审查意见书\n审查范围\n法律依据：《中华人民共和国民法典》第509条。\n"
        "主要问题及修改建议\n1. 第1条 建议修改为：补充授权条款。\n2. 第2条 建议修改为：明确付款条件。\n"
        "3. 第3条 建议修改为：补充发票安排。\n4. 第4条 建议修改为：补充验收条件。\n"
        "5. 第5条 建议修改为：补充质量责任。\n6. 第6条 建议修改为：补充变更签证。\n"
        "7. 第7条 建议修改为：补充工期顺延。\n8. 第8条 建议修改为：补充违约责任。\n"
        "声明与保留\n《中华人民共和国民法典》第510条。《中华人民共和国民法典》第577条。"
    )
    scores = build_flow_scores(
        flow_id="contract_review",
        seen_cards=[{"skill_id": "goal-completion", "task_key": "goal_completion", "review_type": "select", "questions": [{"field_key": "data.workbench.goal"}]}],
        pending_card={"skill_id": "goal-completion", "task_key": "goal_completion", "review_type": "select", "questions": [{"field_key": "data.workbench.goal"}]},
        snapshot=snapshot,
        current_view=contract_view,
        deliverables={
            "contract_review_report": {"status": "completed"},
            "modification_suggestion": {"status": "completed"},
            "redline_comparison": {"status": "completed"},
        },
        deliverable_text=report_text,
        deliverable_status="completed",
        contract_review_expectations=expectations,
        observability={
            "matter_traces": [{"node_id": "contract_output_finalize", "task_id": "goal_completion"}],
            "session_traces": [{"node_id": "references_grounding", "task_id": "contract_review"}],
            "phase_timeline": {"phases": [{"id": "contract_review", "status": "completed"}]},
            "matter_timeline": {"rounds": [{"content": {"produced_output_keys": ["contract_review_view"]}}]},
            "session_timeline": {"rounds": []},
            "errors": {},
        },
        goal_completion_mode="card",
    )

    assert scores["snapshot_progress_score"]["passed"] is True
    assert scores["overall_e2e_score"]["score"] > 0


def test_build_legal_opinion_formal_ready_report_flags_internal_text_pollution() -> None:
    report = build_legal_opinion_formal_ready_report(
        current_view={
            "title": "关于contract_dispute事项的法律意见书",
            "summary": "当前结论来源：陈述泳道。" * 10,
            "confirmed_opinions": [{"title": "付款义务成立"}],
            "risks": [{"title": "风险"}],
            "action_items": [{"title": "补证"}],
            "material_gaps": [],
            "fact_gaps": [],
        },
        aux_views={
            "document_generation_view": {
                "formal_gate_blocked": True,
                "formal_gate_reason_codes": ["formal_opinion_authority_pending"],
                "formal_gate_actions": [{"action_id": "references_refresh_partial"}],
            }
        },
        deliverable_text="关于contract_dispute事项的法律意见书\n当前结论来源：陈述泳道。",
        deliverable_status="completed",
    )

    assert report["passed"] is False
    assert any(str(item).startswith("legal_opinion_pollution:") for item in report["failures"])


def test_build_flow_scores_uses_formal_ready_signals_for_legal_opinion() -> None:
    scores = build_flow_scores(
        flow_id="legal_opinion",
        seen_cards=[],
        pending_card={},
        snapshot={"analysis_state": {"current_node": "goal_completion", "current_phase": "legal_opinion"}},
        current_view={
            "title": "服务器采购合同履约争议法律意见书",
            "summary": "围绕交付迟延、质量瑕疵和尾款抗辩形成的法律意见。" * 6,
            "confirmed_opinions": [{"title": "对方逾期交付责任可以成立"}],
            "risks": [{"title": "验收事实争议"}],
            "action_items": [{"title": "补齐验收记录"}],
            "material_gaps": [],
            "fact_gaps": [],
            "issues": [{"title": "交付迟延责任"}],
            "result_contract_diagnostics": {"status": "valid"},
        },
        aux_views={
            "document_generation_view": {
                "formal_gate_blocked": True,
                "formal_gate_reason_codes": ["formal_opinion_authority_pending"],
                "formal_gate_actions": [{"action_id": "references_refresh_partial"}],
            }
        },
        deliverables={"legal_opinion": {"status": "completed"}},
        deliverable_text="法律意见书\n事实基础\n规则依据\n分析论证\n结论意见\n风险提示\n应对建议\n《中华人民共和国民法典》第509条\n第8.2款\n1. 补齐验收材料。",
        deliverable_status="completed",
        observability={
            "matter_traces": [{"node_id": "legal_opinion_output", "task_id": "goal_completion"}],
            "session_traces": [{"node_id": "docgen_prepare", "task_id": "document_generation"}],
            "phase_timeline": {"phases": [{"id": "legal_opinion", "status": "completed"}]},
            "matter_timeline": {"rounds": [{"content": {"produced_output_keys": ["legal_opinion_view", "document_generation_view"]}}]},
            "session_timeline": {"rounds": []},
            "errors": {},
        },
        goal_completion_mode="workflow_action",
    )

    assert scores["deliverable_quality_score"]["details"]["formal_ready"]["blocking_reason_codes"] == [
        "formal_opinion_authority_pending"
    ]
    assert scores["deliverable_quality_score"]["details"]["formal_ready"]["required_actions"] == [
        {"action_id": "references_refresh_partial"}
    ]
