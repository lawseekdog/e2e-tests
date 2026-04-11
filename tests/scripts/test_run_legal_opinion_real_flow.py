from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts.run_legal_opinion_real_flow import (
    _analysis_allows_auto_review_card,
    _analysis_auto_focus_blocker_target,
    _capability_gap_card_matches_overrides,
    _pick_analysis_auto_action,
    LEGAL_OPINION_CHAT_RUN,
    _resolve_fixture_paths,
    parse_args,
)

pytestmark = pytest.mark.skip_seed_bootstrap


def test_capability_gap_card_matches_supported_overrides() -> None:
    card = {
        "type": "awaiting_review",
        "interruption_id": "awaiting_review:legal_opinion_capability_gap",
        "reason_kind": "missing_input",
        "reason_code": "legal_opinion_capability_gap",
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
        "type": "awaiting_review",
        "interruption_id": "awaiting_review:legal_opinion_capability_gap",
        "reason_kind": "missing_input",
        "reason_code": "legal_opinion_capability_gap",
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


def test_parse_args_no_longer_exposes_allow_nudge(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_legal_opinion_real_flow.py"])

    args = parse_args()

    assert not hasattr(args, "allow_nudge")


def test_legal_opinion_uses_explicit_chat_run_contract() -> None:
    assert LEGAL_OPINION_CHAT_RUN == {
        "entry_mode": "direct_drafting",
        "service_type_id": "legal_opinion",
        "delivery_goal": "formal_opinion",
        "target_document_kind": "legal_opinion",
        "supporting_document_kinds": [],
    }


def test_analysis_auto_action_prefers_legal_opinion_core_missing() -> None:
    legal_view = {
        "status": "pending",
            "next_actions": [
                {
                "type": "focus_blocker",
                "auto_trigger": True,
                "payload": {
                    "action": "focus_blocker",
                    "target": "legal_opinion_analyze",
                    "reason_codes": ["legal_opinion_core_missing"],
                },
            }
        ],
    }

    action = _pick_analysis_auto_action({}, legal_view)

    assert action
    assert action["payload"]["target"] == "legal_opinion_analyze"
    assert _analysis_auto_focus_blocker_target(action) == "legal_opinion_analyze"


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
