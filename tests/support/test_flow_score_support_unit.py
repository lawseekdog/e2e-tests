from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts._support.flow_score_support import (
    build_flow_scores,
    build_legal_opinion_formal_ready_report,
    collect_flow_observability,
    score_unexpected_cards,
)


def test_score_unexpected_cards_flags_forbidden_skill_and_data_group() -> None:
    score = score_unexpected_cards(
        flow_id="analysis",
        seen_cards=[
            {
                "type": "awaiting_review",
                "interruption_id": "awaiting_review:skill_error",
                "reason_kind": "human_confirmation",
                "reason_code": "skill_error_analysis",
                "questions": [{"field_key": "data.unknown.bad", "input_type": "text"}],
            }
        ],
    )

    assert score["passed"] is False
    assert score["unexpected_count"] == 1
    assert "forbidden_reason_code:skill_error_analysis" in score["unexpected_cards"][0]["reasons"]


def test_build_flow_scores_produces_all_four_scores_for_analysis() -> None:
    snapshot = {
        "workflow": {
            "phases": [{"phase_id": "render_deliverable", "label": "交付物渲染", "status": "running", "current": True}]
        }
    }
    analysis_view = {
        "status": "ready",
        "summary": "这是一个足够长的案件分析摘要。" * 8,
        "sections": [
            {"section_type": "issues", "data": {"items": [{"issue_id": "i1"}]}},
            {"section_type": "strategy_matrix", "data": {"items": [{"strategy_id": "s1"}]}},
            {"section_type": "risks", "data": {"items": [{"risk_id": "r1", "title": "证据风险"}]}},
        ],
    }
    scores = build_flow_scores(
        flow_id="analysis",
        seen_cards=[
            {
                "type": "awaiting_review",
                "interruption_id": "awaiting_review:goal_completion",
                "interruption_key": "goal_completion",
                "questions": [{"field_key": "data.workbench.goal"}],
            }
        ],
        current_blocker={"type": "awaiting_review", "interruption_id": "awaiting_review:goal_completion", "interruption_key": "goal_completion", "questions": [{"field_key": "data.workbench.goal"}]},
        snapshot=snapshot,
        current_view=analysis_view,
        aux_views={},
        deliverables={},
        artifact_status="ready",
        observability={
            "matter_traces": [{"node_id": "dispute_analysis_program:dispute_analysis_source_pack", "task_id": "dispute_analysis_source_pack"}],
            "session_traces": [{"node_id": "dispute_analysis_program:render_deliverable", "task_id": "render_deliverable"}],
            "phase_timeline": {"phases": [{"id": "render_deliverable", "status": "completed"}]},
            "matter_timeline": {"rounds": [{"content": {"produced_output_keys": ["analysis_view"]}}]},
            "session_timeline": {"entries": [{"phase_id": "dispute_analysis_issue_matrix", "phase": "dispute_analysis_issue_matrix", "status": "completed", "event_type": "result.merge", "payload": {"work_product_type": "issue_matrix"}}]},
            "errors": {},
        },
        goal_completion_mode="chat_run",
    )

    assert "unexpected_card_score" in scores
    assert "node_path_score" in scores
    assert "snapshot_progress_score" in scores
    assert "deliverable_quality_score" in scores
    assert scores["snapshot_progress_score"]["passed"] is True
    assert scores["deliverable_quality_score"]["passed"] is True
    assert scores["overall_e2e_score"]["passed"] is True


@pytest.mark.asyncio
async def test_collect_flow_observability_falls_back_to_ai_engine_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path
    bundle_dir = repo_root / "output" / "ai-debug-bundles" / "session:demo"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "timeline.json").write_text(
        json.dumps(
            {
                "thread_id": "session:demo",
                "entries": [
                    {
                        "event_type": "result.merge",
                        "status": "completed",
                        "phase_id": "dispute_analysis_source_pack",
                        "phase": "dispute_analysis_source_pack",
                        "payload": {"work_product_type": "source_pack"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "execution_traces.json").write_text(
        json.dumps(
            {
                "thread_id": "session:demo",
                "traces": [
                    {
                        "id": "session:demo::dispute_analysis_program:render_deliverable",
                        "node_id": "dispute_analysis_program:render_deliverable",
                        "node_type": "result.merge",
                        "sequence": 2,
                        "status": "completed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts._support.flow_score_support._repo_root", lambda: repo_root)

    class StubClient:
        async def get_matter_timeline(self, matter_id: str, limit: int | None = None) -> dict:
            raise RuntimeError("matter_timeline_missing")

        async def get_matter_phase_timeline(self, matter_id: str) -> dict:
            return {"data": {"phases": []}}

        async def list_traces(self, matter_id: str, limit: int | None = None) -> dict:
            raise RuntimeError("matter_traces_missing")

        async def get_session_timeline(self, session_id: str, limit: int | None = None) -> dict:
            raise RuntimeError("session_timeline_missing")

        async def list_session_traces(self, session_id: str, limit: int | None = None) -> dict:
            raise RuntimeError("session_traces_missing")

    observability = await collect_flow_observability(StubClient(), matter_id="1402", session_id="demo")

    assert observability["session_timeline"]["thread_id"] == "session:demo"
    assert len(observability["session_traces"]) == 1
    assert len(observability["matter_traces"]) == 1
    assert "session_timeline" not in observability["errors"]
    assert "session_traces" not in observability["errors"]


def test_build_flow_scores_enforces_contract_review_v2_expectations() -> None:
    snapshot = {
        "workflow": {
            "phases": [
                {"phase_id": "contract_review", "label": "合同审查", "status": "running", "current": True}
            ]
        },
        "analysis_state": {"current_node": "goal_completion"},
    }
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
        "required_output_keys": ["contract_review_report"],
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
        seen_cards=[
            {
                "type": "awaiting_review",
                "interruption_id": "awaiting_review:goal_completion",
                "interruption_key": "goal_completion",
                "questions": [{"field_key": "data.workbench.goal"}],
            }
        ],
        current_blocker={"type": "awaiting_review", "interruption_id": "awaiting_review:goal_completion", "interruption_key": "goal_completion", "questions": [{"field_key": "data.workbench.goal"}]},
        snapshot=snapshot,
        current_view=contract_view,
        deliverables={
            "contract_review_report": {"status": "completed"},
        },
        deliverable_text=report_text,
        artifact_status="completed",
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
            "typed_render_state": {
                "formal_gate_blocked": True,
                "formal_gate_reason_codes": ["formal_opinion_authority_pending"],
            },
        },
        deliverable_text="关于contract_dispute事项的法律意见书\n当前结论来源：陈述泳道。",
        artifact_status="completed",
    )

    assert report["passed"] is False
    assert any(str(item).startswith("legal_opinion_pollution:") for item in report["failures"])


def test_build_flow_scores_uses_formal_ready_signals_for_legal_opinion() -> None:
    scores = build_flow_scores(
        flow_id="legal_opinion",
        seen_cards=[],
        current_blocker={},
        snapshot={
            "workflow": {
                "phases": [
                    {"phase_id": "legal_opinion", "label": "法律意见", "status": "running", "current": True}
                ]
            },
            "analysis_state": {"current_node": "goal_completion"},
        },
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
            "typed_render_state": {
                "formal_gate_blocked": True,
                "formal_gate_reason_codes": ["formal_opinion_authority_pending"],
            },
        },
        deliverables={"legal_opinion": {"status": "completed"}},
        deliverable_text="法律意见书\n事实基础\n规则依据\n分析论证\n结论意见\n风险提示\n应对建议\n《中华人民共和国民法典》第509条\n第8.2款\n1. 补齐验收材料。",
        artifact_status="completed",
        observability={
            "matter_traces": [{"node_id": "legal_opinion_output", "task_id": "goal_completion"}],
            "session_traces": [{"node_id": "typed_render_prepare", "task_id": "document_render"}],
            "phase_timeline": {"phases": [{"id": "legal_opinion", "status": "completed"}]},
            "matter_timeline": {"rounds": [{"content": {"produced_output_keys": ["analysis_projection", "typed_render_state"]}}]},
            "session_timeline": {"rounds": []},
            "errors": {},
        },
        goal_completion_mode="chat_run",
    )

    assert scores["deliverable_quality_score"]["details"]["formal_ready"]["blocking_reason_codes"] == [
        "formal_opinion_authority_pending"
    ]
