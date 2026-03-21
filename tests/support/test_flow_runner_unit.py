from __future__ import annotations

import pytest

from support.workbench.flow_runner import WorkbenchFlow


@pytest.mark.asyncio
async def test_step_intercepts_pending_goal_completion_card_before_resume() -> None:
    goal_card = {
        "skill_id": "goal-completion",
        "task_key": "goal_completion",
        "questions": [{"field_key": "data.workbench.goal", "input_type": "select"}],
    }
    flow = WorkbenchFlow(client=object(), session_id="session-1")

    async def _refresh() -> None:
        return None

    async def _get_pending_card() -> dict[str, object]:
        return goal_card

    async def _resume_card(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("resume_card should not run for intercepted goal-completion card")

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_pending_card = _get_pending_card  # type: ignore[method-assign]
    flow.resume_card = _resume_card  # type: ignore[method-assign]

    result = await flow.step(
        stop_on_pending_card=lambda card: str(card.get("skill_id")) == "goal-completion",
    )

    assert isinstance(result, dict)
    assert result.get("pending_card") == goal_card


@pytest.mark.asyncio
async def test_step_intercepts_goal_completion_card_emitted_from_nudge() -> None:
    goal_card = {
        "skill_id": "goal-completion",
        "task_key": "goal_completion",
        "questions": [{"field_key": "data.workbench.goal", "input_type": "select"}],
    }
    flow = WorkbenchFlow(client=object(), session_id="session-2")

    async def _refresh() -> None:
        return None

    async def _get_pending_card():
        return None

    async def _nudge(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {"events": [{"event": "card", "data": goal_card}], "output": "合同审查已完成。"}

    async def _resume_card(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("resume_card should not run for nudge-intercepted goal-completion card")

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_pending_card = _get_pending_card  # type: ignore[method-assign]
    flow.nudge = _nudge  # type: ignore[method-assign]
    flow.resume_card = _resume_card  # type: ignore[method-assign]

    result = await flow.step(
        stop_on_pending_card=lambda card: str(card.get("skill_id")) == "goal-completion",
    )

    assert isinstance(result, dict)
    assert result.get("pending_card") == goal_card
