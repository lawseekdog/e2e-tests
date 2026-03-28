from __future__ import annotations

from scripts.run_template_draft_real_flow import (
    _deliverable_candidate_settled,
    _evaluate_dialogue_quality,
)


def test_evaluate_dialogue_quality_allows_successful_cardless_flow() -> None:
    result = _evaluate_dialogue_quality(
        rounds=[
            {"busy": False, "visible_ok": True, "low_signal_streak": 0},
            {"busy": False, "visible_ok": True, "low_signal_streak": 0},
        ],
        cards=[],
        strict_dialogue=True,
    )

    assert result["pass"] is True
    assert result["cardless_success"] is True


def test_deliverable_candidate_settled_waits_for_docgen_grace_window() -> None:
    snapshot = {
        "current_phase": "docgen",
        "current_task_id": "docgen_prepare",
        "docgen_node": "finish",
        "deliverable": {"status": "completed"},
        "quality_review_decision": "repair",
    }

    assert (
        _deliverable_candidate_settled(
            snapshot=snapshot,
            stable_polls=3,
            seen_for_s=10.0,
            min_stable_polls=2,
            settle_grace_s=30.0,
        )
        is False
    )
    assert (
        _deliverable_candidate_settled(
            snapshot=snapshot,
            stable_polls=3,
            seen_for_s=45.0,
            min_stable_polls=2,
            settle_grace_s=30.0,
        )
        is True
    )
