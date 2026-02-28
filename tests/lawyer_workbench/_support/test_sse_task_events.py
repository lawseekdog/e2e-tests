from __future__ import annotations

import pytest

from tests.lawyer_workbench._support.sse import assert_visible_response, validate_task_events


def test_validate_task_events_accepts_mixed_run_skill_task_start_payloads() -> None:
    sse = {
        "events": [
            {"event": "task_start", "data": {"node": "run_skill", "skill_id": "document-drafting-intake"}},
            {"event": "task_start", "data": {"node": "run_skill"}},
            {"event": "task_end", "data": {"node": "run_skill"}},
        ]
    }
    validate_task_events(sse)


def test_validate_task_events_rejects_run_skill_without_any_skill_id() -> None:
    sse = {
        "events": [
            {"event": "task_start", "data": {"node": "run_skill"}},
            {"event": "task_end", "data": {"node": "run_skill"}},
        ]
    }
    with pytest.raises(AssertionError, match="run_skill task_start missing skill_id"):
        validate_task_events(sse)


def test_assert_visible_response_allows_busy_partial_without_output_or_card() -> None:
    sse = {
        "events": [
            {"event": "progress", "data": {"status": "running"}},
            {"event": "task_start", "data": {"node": "run_skill", "skill_id": "document-draft"}},
            {"event": "task_end", "data": {"node": "run_skill"}},
            {"event": "error", "data": {"partial": True, "message": "后台继续处理中，请刷新查看待办。"}},
        ],
        "output": "",
    }
    assert_visible_response(sse)


def test_assert_visible_response_rejects_non_busy_partial_without_output_or_card() -> None:
    sse = {
        "events": [
            {"event": "progress", "data": {"status": "running"}},
            {"event": "task_start", "data": {"node": "router"}},
            {"event": "task_end", "data": {"node": "router"}},
            {"event": "error", "data": {"partial": True, "message": "upstream closed"}},
        ],
        "output": "",
    }
    with pytest.raises(AssertionError, match="SSE has neither output nor card"):
        assert_visible_response(sse)


def test_assert_visible_response_allows_partial_stream_timeout_without_output_or_card() -> None:
    sse = {
        "events": [
            {"event": "progress", "data": {"status": "running"}},
            {"event": "task_start", "data": {"node": "run_skill", "skill_id": "document-draft"}},
            {"event": "task_end", "data": {"node": "run_skill"}},
            {"event": "error", "data": {"partial": True, "error": "stream_timeout", "timeout_s": 180}},
        ],
        "output": "",
    }
    assert_visible_response(sse)
