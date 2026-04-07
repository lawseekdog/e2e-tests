from __future__ import annotations

import sys

from scripts.run_analysis_real_flow import _analysis_readiness, _analysis_reference_refresh_hint
from scripts.run_analysis_real_flow import parse_args


def test_parse_args_no_longer_exposes_chat_kickoff(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_analysis_real_flow.py"])

    args = parse_args()

    assert not hasattr(args, "kickoff")
    assert not hasattr(args, "kickoff_max_loops")


def test_analysis_reference_refresh_hint_detects_authority_pending_without_refs() -> None:
    snapshot = {
        "analysis_state": {
            "analysis_view": {
                "reference_suite": {
                    "status": "pending",
                    "blocking_reason_codes": ["authority_pending"],
                    "counts": {"law_count": 0, "case_count": 0},
                }
            },
            "current_subgraph": "references",
            "current_task_id": "analysis_project_analysis_view",
            "current_node": "analysis_project_analysis_view",
            "references_diagnostics_summary": {
                "final_status": "pending",
                "dominant_reason_code": "authority_pending",
            },
        },
    }

    hint = _analysis_reference_refresh_hint(snapshot)

    assert hint["final_reason"] == "authority_pending"
    assert hint["current_task_id"] == "analysis_project_analysis_view"


def test_analysis_reference_refresh_hint_ignores_ready_reference_suite() -> None:
    snapshot = {
        "analysis_state": {
            "analysis_view": {
                "reference_suite": {
                    "status": "ready",
                    "blocking_reason_codes": [],
                    "counts": {"law_count": 2, "case_count": 1},
                }
            },
            "current_subgraph": "analysis",
            "current_task_id": "analysis_project_analysis_view",
            "current_node": "analysis_project_analysis_view",
            "references_diagnostics_summary": {
                "final_status": "ready",
                "dominant_reason_code": "",
            },
        },
    }

    assert _analysis_reference_refresh_hint(snapshot) == {}


def test_analysis_readiness_allows_empty_pricing_when_analysis_and_docgen_are_ready() -> None:
    analysis_view = {
        "summary": "summary",
        "sections": [
            {"section_type": "issues", "data": {"items": [{"issue_id": "i1"}]}},
            {"section_type": "strategy_matrix", "data": {"items": [{"strategy_id": "s1"}]}},
        ],
    }
    pricing_view = {}
    docgen_state = {"status": "repair_blocked", "selected_documents": ["document:civil_complaint_document"]}

    readiness = _analysis_readiness(
        analysis_view,
        pricing_view,
        docgen_state,
        require_documents=True,
    )

    assert readiness["ready"] is True
    assert readiness["checks"]["docgen_terminal"] is True
    assert readiness["optional_checks"]["pricing_ready"] is False


def test_analysis_readiness_waits_for_docgen_terminal_when_dual_output_is_still_rendering() -> None:
    analysis_view = {
        "summary": "summary",
        "sections": [
            {"section_type": "issues", "data": {"items": [{"issue_id": "i1"}]}},
            {"section_type": "strategy_matrix", "data": {"items": [{"strategy_id": "s1"}]}},
        ],
    }
    pricing_view = {}
    docgen_state = {"status": "document_generating", "selected_documents": ["document:civil_complaint_document"]}

    readiness = _analysis_readiness(
        analysis_view,
        pricing_view,
        docgen_state,
        require_documents=True,
    )

    assert readiness["ready"] is False
    assert "docgen_terminal" in readiness["missing_requirements"]
