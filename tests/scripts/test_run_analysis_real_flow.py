from __future__ import annotations

from scripts.run_analysis_real_flow import _analysis_reference_refresh_hint


def test_analysis_reference_refresh_hint_detects_authority_pending_without_refs() -> None:
    snapshot = {
        "analysis_state": {
            "current_subgraph": "references",
            "current_task_id": "analysis_project_analysis_view",
            "current_node": "analysis_project_analysis_view",
            "references_diagnostics_summary": {
                "final_status": "pending",
                "dominant_reason_code": "authority_pending",
            },
        },
        "goal_views": {
            "analysis_view": {
                "reference_suite": {
                    "status": "pending",
                    "blocking_reason_codes": ["authority_pending"],
                    "counts": {"law_count": 0, "case_count": 0},
                }
            }
        },
    }

    hint = _analysis_reference_refresh_hint(snapshot)

    assert hint["final_reason"] == "authority_pending"
    assert hint["current_task_id"] == "analysis_project_analysis_view"


def test_analysis_reference_refresh_hint_ignores_ready_reference_suite() -> None:
    snapshot = {
        "analysis_state": {
            "current_subgraph": "analysis",
            "current_task_id": "analysis_project_analysis_view",
            "current_node": "analysis_project_analysis_view",
            "references_diagnostics_summary": {
                "final_status": "ready",
                "dominant_reason_code": "",
            },
        },
        "goal_views": {
            "analysis_view": {
                "reference_suite": {
                    "status": "ready",
                    "blocking_reason_codes": [],
                    "counts": {"law_count": 2, "case_count": 1},
                }
            }
        },
    }

    assert _analysis_reference_refresh_hint(snapshot) == {}
