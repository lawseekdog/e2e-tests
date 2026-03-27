from __future__ import annotations

from support.workbench.sse import validate_task_events


def test_validate_task_events_allows_run_skill_without_skill_id() -> None:
    sse = {
        "events": [
            {"event": "task_start", "data": {"node": "run_skill"}},
            {"event": "task_end", "data": {"node": "run_skill"}},
            {"event": "progress", "data": {"status": "running"}},
            {"event": "end", "data": {"output": "ok"}},
        ]
    }

    validate_task_events(sse)
