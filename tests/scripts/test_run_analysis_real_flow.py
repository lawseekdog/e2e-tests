from __future__ import annotations

import sys
import pytest

from scripts.run_analysis_real_flow import _analysis_readiness
from scripts.run_analysis_real_flow import parse_args

pytestmark = pytest.mark.skip_seed_bootstrap


def test_parse_args_no_longer_exposes_chat_kickoff(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_analysis_real_flow.py"])

    args = parse_args()

    assert not hasattr(args, "kickoff")
    assert not hasattr(args, "kickoff_max_loops")


def test_analysis_readiness_allows_empty_pricing_when_analysis_products_are_ready() -> None:
    analysis_view = {
        "summary": "summary",
        "sections": [
            {"section_type": "issues", "data": {"items": [{"issue_id": "i1"}]}},
            {"section_type": "strategy_matrix", "data": {"items": [{"strategy_id": "s1"}]}},
        ],
    }
    pricing_view = {}

    readiness = _analysis_readiness(
        analysis_view,
        pricing_view,
    )

    assert readiness["ready"] is True
    assert readiness["optional_checks"]["pricing_ready"] is False


def test_analysis_readiness_requires_typed_analysis_products() -> None:
    analysis_view = {
        "summary": "summary",
        "sections": [
            {"section_type": "issues", "data": {"items": [{"issue_id": "i1"}]}},
        ],
    }
    pricing_view = {}

    readiness = _analysis_readiness(
        analysis_view,
        pricing_view,
    )

    assert readiness["ready"] is False
    assert "strategy_options_ready" in readiness["missing_requirements"]
