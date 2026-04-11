from __future__ import annotations

import pytest

from scripts.run_contract_review_real_flow import (
    _build_contract_view,
    _extract_runtime_deliverables,
    _start_chat_run,
)

pytestmark = pytest.mark.skip_seed_bootstrap


def test_start_chat_run_uses_contract_review_report() -> None:
    assert _start_chat_run() == {
        "entry_mode": "direct_drafting",
        "service_type_id": "contract_review",
        "delivery_goal": "contract_review",
        "target_document_kind": "contract_review_report",
        "supporting_document_kinds": [],
    }


def test_extract_runtime_deliverables_prefers_execution_snapshot_payload() -> None:
    rows = _extract_runtime_deliverables(
        {
            "status": "completed",
            "deliverables": [
                {
                    "deliverable_kind": "contract_review_report",
                    "title": "合同审查报告",
                    "payload": {
                        "document_kind": "contract_review_report",
                        "full_text": "来自 payload 的正文",
                    },
                },
                {
                    "product_type": "deliverable_artifact",
                    "outputs": [
                        {
                            "deliverable_kind": "contract_review_report",
                            "render_status": "completed",
                            "artifact_refs": [
                                {
                                    "metadata": {
                                        "body": "来自 artifact 的正文",
                                    }
                                }
                            ],
                        }
                    ],
                },
            ],
        }
    )

    report = rows["contract_review_report"]
    assert report["output_key"] == "contract_review_report"
    assert report["status"] == "completed"
    assert report["title"] == "合同审查报告"
    assert report["full_text"] == "来自 payload 的正文"


def test_build_contract_view_projects_current_analysis_view() -> None:
    snapshot = {
        "analysis_view": {
            "title": "合同审查",
            "summary": "这是最新的合同审查分析摘要。",
            "status": "ready",
            "sections": [
                {
                    "section_type": "issues",
                    "data": {
                        "items": [
                            {
                                "issue_id": "focus:1",
                                "issue_title": "工程款支付延期风险",
                                "analysis": "付款条款对乙方明显不利。",
                                "authority_refs": ["case:1"],
                                "authority_titles": ["(2024)沪0120民初17162号"],
                                "evidence_refs": ["e1"],
                            }
                        ]
                    },
                },
                {
                    "section_type": "risks",
                    "data": {
                        "items": [
                            {
                                "risk_id": "focus:1",
                                "title": "工程款支付延期风险",
                                "level": "high",
                                "mitigation": "补齐付款时限与违约金条款。",
                                "focus_refs": ["focus:1"],
                                "evidence_refs": ["e1"],
                            }
                        ]
                    },
                },
                {
                    "section_type": "strategy_matrix",
                    "data": {
                        "items": [
                            {
                                "strategy_id": "strategy:1",
                                "title": "修改付款条款",
                            }
                        ]
                    },
                },
            ],
        }
    }

    view = _build_contract_view(
        snapshot,
        contract_type_id="construction",
        review_scope="full",
    )

    assert view["contract_type_id"] == "construction"
    assert view["review_scope"] == "full"
    assert view["overall_risk_level"] == "high"
    assert view["result_contract_diagnostics"]["status"] == "valid"
    assert view["clauses"][0]["risk_type"] == "payment"
    assert view["clauses"][0]["authority_titles"] == ["(2024)沪0120民初17162号"]
