from __future__ import annotations

import sys
from pathlib import Path

from scripts.run_legal_opinion_real_flow import (
    _analysis_allows_auto_review_card,
    _analysis_auto_review_card_target,
    _analysis_nudge_text,
    _analysis_should_refresh_references,
    _capability_gap_card_matches_overrides,
    _pick_analysis_auto_action,
    _resolve_fixture_paths,
    parse_args,
)


def test_capability_gap_card_matches_supported_overrides() -> None:
    card = {
        "skill_id": "legal-opinion-capability-gap",
        "questions": [
            {
                "field_key": "profile.opinion_topic_primary",
                "input_type": "select",
                "options": [
                    {"label": "合同争议", "value": "contract_dispute"},
                    {"label": "劳动用工", "value": "labor_employment"},
                ],
            },
            {
                "field_key": "profile.opinion_subtype",
                "input_type": "select",
                "options": [
                    {"label": "纠纷处置/事故应对型", "value": "dispute_response"},
                ],
            },
        ],
    }

    assert _capability_gap_card_matches_overrides(
        card,
        {
            "profile.opinion_topic_primary": "contract_dispute",
            "profile.opinion_subtype": "dispute_response",
        },
    )


def test_capability_gap_card_rejects_unsupported_overrides() -> None:
    card = {
        "skill_id": "legal-opinion-capability-gap",
        "questions": [
            {
                "field_key": "profile.opinion_topic_primary",
                "input_type": "select",
                "options": [
                    {"label": "合同争议", "value": "contract_dispute"},
                ],
            },
            {
                "field_key": "profile.opinion_subtype",
                "input_type": "select",
                "options": [
                    {"label": "纠纷处置/事故应对型", "value": "dispute_response"},
                ],
            },
        ],
    }

    assert not _capability_gap_card_matches_overrides(
        card,
        {
            "profile.opinion_topic_primary": "contract_performance",
            "profile.opinion_subtype": "dispute_response",
        },
    )


def test_resolve_fixture_paths_falls_back_to_e2e_root() -> None:
    resolved = _resolve_fixture_paths(
        ("scripts/_support/fixtures/legal_opinion_supply_contract.txt",)
    )

    assert resolved
    assert isinstance(resolved[0], Path)
    assert resolved[0].name == "legal_opinion_supply_contract.txt"


def test_parse_args_disables_auto_nudge_by_default(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_legal_opinion_real_flow.py"])

    args = parse_args()

    assert args.allow_nudge is False


def test_analysis_auto_action_prefers_legal_opinion_core_missing() -> None:
    legal_view = {
        "status": "pending",
        "next_actions": [
            {
                "goal": "legal_opinion",
                "type": "open_review_card",
                "auto_trigger": True,
                "payload": {
                    "action": "open_review_card",
                    "target": "legal_opinion_analyze",
                    "reason_codes": ["legal_opinion_core_missing"],
                },
            }
        ],
    }

    action = _pick_analysis_auto_action({}, legal_view)

    assert action
    assert action["payload"]["target"] == "legal_opinion_analyze"
    assert _analysis_should_refresh_references(snapshot={}, legal_view=legal_view) is False
    assert "法律意见分析结果" in _analysis_nudge_text({}, legal_view)
    assert _analysis_auto_review_card_target(action) == "legal_opinion_analyze"


def test_analysis_allows_reference_refresh_only_after_core_ready() -> None:
    snapshot = {
        "analysis_state": {
            "next_actions": [],
            "active_scope_id": "analysis:litigation",
            "goal_scopes": {
                "analysis:litigation": {
                    "references": {
                        "meta": {"status": "blocked", "reason_codes": ["authority_pending"]},
                    }
                }
            },
            "references_diagnostics_summary": {"final_reason": "authority_pending"},
        }
    }
    legal_view = {
        "summary": "已形成初步法律意见摘要",
        "issues": ["尾款抗辩"],
        "next_actions": [],
    }

    assert _analysis_should_refresh_references(snapshot=snapshot, legal_view=legal_view) is True


def test_analysis_allows_reference_refresh_when_references_stage_is_explicitly_blocked() -> None:
    snapshot = {
        "analysis_state": {
            "current_subgraph": "analysis",
            "current_task_id": "references_finalize",
            "active_scope_id": "analysis:litigation",
            "goal_scopes": {
                "analysis:litigation": {
                    "references": {
                        "meta": {
                            "status": "blocked",
                            "reason_codes": ["references_grounding_law_rows_missing"],
                        }
                    }
                }
            },
            "references_diagnostics_summary": {
                "final_reason": "retrieval_no_hit",
            },
        }
    }
    legal_view = {
        "status": "pending",
        "summary": "",
        "issues": [],
        "next_actions": [
            {
                "goal": "legal_opinion",
                "type": "open_review_card",
                "auto_trigger": True,
                "payload": {
                    "action": "open_review_card",
                    "target": "legal_opinion_analyze",
                    "reason_codes": ["legal_opinion_core_missing"],
                },
            }
        ],
    }

    assert _analysis_should_refresh_references(snapshot=snapshot, legal_view=legal_view) is True


def test_analysis_auto_review_card_waits_while_evidence_pipeline_is_running() -> None:
    snapshot = {
        "analysis_state": {
            "current_task_id": "evidence_sufficiency_seed",
            "current_subgraph": "analysis",
        }
    }

    assert _analysis_allows_auto_review_card(snapshot) is False


def test_analysis_auto_review_card_allows_stale_evidence_task_after_handoff_ready() -> None:
    snapshot = {
        "analysis_state": {
            "active_scope_id": "analysis:litigation",
            "goal_scopes": {
                "analysis:litigation": {
                    "evidence": {
                        "runtime": {
                            "readiness": {
                                "status": "ready",
                                "next_route": "finish",
                                "phase_terminal": False,
                            }
                        }
                    }
                }
            },
            "current_task_id": "evidence_list_seed",
            "current_subgraph": "analysis",
        }
    }

    assert _analysis_allows_auto_review_card(snapshot) is True
