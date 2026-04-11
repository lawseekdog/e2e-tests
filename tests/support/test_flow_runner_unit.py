from __future__ import annotations

import asyncio

import pytest

from support.workbench.flow_runner import (
    WorkbenchFlow,
    auto_answer_card,
    card_signature,
)

pytestmark = pytest.mark.skip_seed_bootstrap


@pytest.mark.asyncio
async def test_step_intercepts_goal_completion_blocker_before_resume() -> None:
    goal_card = {
        "type": "awaiting_review",
        "interruption_id": "awaiting_review:goal_completion",
        "interruption_key": "goal_completion",
        "questions": [{"field_key": "data.workbench.goal", "input_type": "select"}],
    }
    flow = WorkbenchFlow(client=object(), session_id="session-1")

    async def _refresh() -> None:
        return None

    async def _get_current_blocker() -> dict[str, object]:
        return goal_card

    async def _resume_blocker(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("resume_blocker should not run for intercepted goal-completion blocker")

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_current_blocker = _get_current_blocker  # type: ignore[method-assign]
    flow.resume_blocker = _resume_blocker  # type: ignore[method-assign]

    result = await flow.step(
        stop_on_blocker=lambda blocker: str(blocker.get("interruption_key")) == "goal_completion",
    )

    assert isinstance(result, dict)
    assert result.get("current_blocker") == goal_card


@pytest.mark.asyncio
async def test_step_intercepts_goal_completion_card_emitted_from_nudge() -> None:
    goal_card = {
        "type": "awaiting_review",
        "interruption_id": "awaiting_review:goal_completion",
        "interruption_key": "goal_completion",
        "questions": [{"field_key": "data.workbench.goal", "input_type": "select"}],
    }
    flow = WorkbenchFlow(client=object(), session_id="session-2")

    async def _refresh() -> None:
        return None

    async def _get_current_blocker():
        return None

    async def _nudge(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {"events": [{"event": "awaiting_review", "data": goal_card}], "output": "合同审查已完成。"}

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_current_blocker = _get_current_blocker  # type: ignore[method-assign]
    flow.nudge = _nudge  # type: ignore[method-assign]

    result = await flow.step(
        stop_on_blocker=lambda blocker: str(blocker.get("interruption_key")) == "goal_completion",
    )

    assert result is None


@pytest.mark.asyncio
async def test_step_does_not_resume_sse_card_when_pending_poll_returned_empty() -> None:
    review_card = {
        "type": "awaiting_review",
        "interruption_id": "awaiting_review:intake_clarify",
        "reason_kind": "missing_input",
        "reason_code": "intake_clarify",
        "questions": [{"field_key": "profile.summary", "input_type": "text"}],
    }
    flow = WorkbenchFlow(client=object(), session_id="session-2b")

    async def _refresh() -> None:
        return None

    async def _get_current_blocker():
        return None

    async def _nudge(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {"events": [{"event": "awaiting_review", "data": review_card}], "output": "需要补充案件信息。"}

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_current_blocker = _get_current_blocker  # type: ignore[method-assign]
    flow.nudge = _nudge  # type: ignore[method-assign]

    result = await flow.step()

    assert result is None


def test_auto_answer_card_covers_basic_card_input_types() -> None:
    card = {
        "questions": [
            {"field_key": "profile.summary", "input_type": "text", "required": True},
            {"field_key": "profile.facts", "input_type": "textarea", "required": True},
            {
                "field_key": "profile.review_scope",
                "input_type": "select",
                "required": True,
                "options": [
                    {"label": "全面审查", "value": "full", "recommended": True},
                    {"label": "风险审查", "value": "risk"},
                ],
            },
            {"field_key": "case.file_refs.pending_upload_file_ids", "input_type": "file_ids", "required": True},
            {"field_key": "profile.decisions.contract_reviewed", "input_type": "boolean", "required": True},
        ],
    }

    response = auto_answer_card(card, uploaded_file_ids=["file_1", "file_2"])
    answers = {str(item.get("field_key")): item.get("value") for item in response["answers"]}

    assert answers["profile.summary"]
    assert answers["profile.facts"]
    assert answers["profile.review_scope"] == "full"
    assert answers["case.file_refs.pending_upload_file_ids"] == ["file_1", "file_2"]
    assert answers["profile.decisions.contract_reviewed"] is True


def test_auto_answer_card_selects_all_recommended_contract_review_clauses() -> None:
    card = {
        "questions": [
            {
                "field_key": "profile.decisions.contract_review_accepted_clause_ids",
                "input_type": "multi_select",
                "required": True,
                "options": [
                    {"label": "第1条（high）", "value": "c1", "recommended": True},
                    {"label": "第2条（medium）", "value": "c2", "recommended": True},
                    {"label": "第3条（low）", "value": "c3"},
                ],
            },
            {
                "field_key": "profile.decisions.contract_review_ignored_clause_ids",
                "input_type": "multi_select",
                "required": False,
                "options": [
                    {"label": "第1条（high）", "value": "c1"},
                    {"label": "第2条（medium）", "value": "c2"},
                ],
            },
            {"field_key": "profile.decisions.contract_reviewed", "input_type": "boolean", "required": True},
            {"field_key": "data.work_product.regenerate_documents", "input_type": "boolean", "required": True, "default": True},
        ],
    }

    response = auto_answer_card(card)
    answers = {str(item.get("field_key")): item.get("value") for item in response["answers"]}

    assert answers["profile.decisions.contract_review_accepted_clause_ids"] == ["c1", "c2"]
    assert answers["profile.decisions.contract_review_ignored_clause_ids"] == []
    assert answers["profile.decisions.contract_reviewed"] is True
    assert answers["data.work_product.regenerate_documents"] is True


@pytest.mark.asyncio
async def test_step_strict_card_mode_skips_hidden_remediation_nudge() -> None:
    card = {
        "type": "awaiting_review",
        "interruption_id": "awaiting_review:doc_draft_retry",
        "reason_kind": "human_confirmation",
        "reason_code": "skill_error_analysis",
        "prompt": "合同审查报告法条引用不足",
        "questions": [{"field_key": "data.workbench.skill_error_acknowledged", "input_type": "boolean", "required": True}],
    }
    flow = WorkbenchFlow(client=object(), session_id="session-3", strict_card_driven=True)

    async def _refresh() -> None:
        return None

    async def _get_current_blocker() -> dict[str, object]:
        return card

    async def _nudge(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("strict card mode should not send hidden remediation nudges")

    async def _resume_blocker(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {"current_blocker": card}

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_current_blocker = _get_current_blocker  # type: ignore[method-assign]
    flow.nudge = _nudge  # type: ignore[method-assign]
    flow.resume_blocker = _resume_blocker  # type: ignore[method-assign]
    flow._repeat_card_signature = card_signature(card)
    flow._repeat_card_count = 1

    result = await flow.step()

    assert isinstance(result, dict)
    assert result.get("current_blocker") == card


@pytest.mark.asyncio
async def test_start_chat_run_uses_chat_chat_run_contract() -> None:
    called: dict[str, object] = {}

    class _Client:
        async def start_chat_run(self, session_id: str, **kwargs):  # type: ignore[no-untyped-def]
            called["session_id"] = session_id
            called.update(kwargs)
            return {"events": [{"event": "chat"}], "output": "ok"}

        async def get_matter_phase_timeline(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {}}

        async def list_traces(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {"traces": []}}

        async def list_deliverables(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {"deliverables": []}}

    flow = WorkbenchFlow(client=_Client(), session_id="session-4", uploaded_file_ids=["file-1"])
    result = await flow.start_chat_run(
        entry_mode="direct_drafting",
        service_type_id="legal_opinion",
        delivery_goal="formal_opinion",
        target_document_kind="legal_opinion",
        max_loops=18,
    )

    assert isinstance(result, dict)
    assert called["session_id"] == "session-4"
    assert called["entry_mode"] == "direct_drafting"
    assert called["service_type_id"] == "legal_opinion"
    assert called["delivery_goal"] == "formal_opinion"
    assert called["target_document_kind"] == "legal_opinion"
    assert called["supporting_document_kinds"] == []
    assert called["attachments"] == ["file-1"]
    assert called["max_loops"] == 18


@pytest.mark.asyncio
async def test_start_chat_run_passes_settle_mode() -> None:
    called: dict[str, object] = {}

    class _Client:
        async def start_chat_run(self, session_id: str, **kwargs):  # type: ignore[no-untyped-def]
            called["session_id"] = session_id
            called.update(kwargs)
            return {"events": [{"event": "progress", "data": {"phase": "materials"}}], "output": ""}

        async def get_matter_phase_timeline(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {}}

        async def list_traces(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {"traces": []}}

        async def list_deliverables(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {"deliverables": []}}

    flow = WorkbenchFlow(client=_Client(), session_id="session-4", uploaded_file_ids=["file-1"])
    result = await flow.start_chat_run(
        entry_mode="direct_drafting",
        service_type_id="civil_prosecution",
        delivery_goal="primary_filing",
        target_document_kind="civil_complaint_document",
        max_loops=18,
        settle_mode="fire_and_poll",
    )

    assert isinstance(result, dict)
    assert called["session_id"] == "session-4"
    assert called["settle_mode"] == "fire_and_poll"


@pytest.mark.asyncio
async def test_start_chat_run_notifies_progress_observer() -> None:
    observed: list[dict[str, object]] = []

    class _Client:
        async def start_chat_run(self, session_id: str, **kwargs):  # type: ignore[no-untyped-def]
            return {"events": [{"event": "chat"}], "output": "ok"}

        async def get_workflow_snapshot(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {
                "code": 0,
                "data": {
                    "blockers_view": {
                        "current_blocker": {
                            "type": "awaiting_review",
                            "interruption_id": "awaiting_review:references",
                            "reason_code": "retrieval_low_coverage",
                            "summary": "需要确认检索结果",
                        }
                    }
                },
            }

        async def get_matter_phase_timeline(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {
                "code": 0,
                "data": {"phases": [{"id": "references", "status": "active", "current": True}]},
            }

        async def list_traces(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {"traces": [{"node_id": "references.query", "status": "running"}]}}

        async def list_deliverables(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {"deliverables": [{"output_key": "analysis_report"}]}}

    async def _observe(event: dict[str, object]) -> None:
        observed.append(event)

    flow = WorkbenchFlow(
        client=_Client(),
        session_id="session-5",
        matter_id="matter-1",
        progress_observer=_observe,
    )
    await flow.start_chat_run(
        entry_mode="analysis",
        service_type_id="civil_prosecution",
        delivery_goal="analysis_only",
    )

    assert observed
    row = observed[-1]
    assert row["label"] == "chat_run:analysis_only"
    assert row["matter_id"] == "matter-1"
    assert row["phase"] == "references"
    assert row["current_blocker"] == {
        "type": "awaiting_review",
        "interruption_id": "awaiting_review:references",
        "reason_code": "retrieval_low_coverage",
        "summary": "需要确认检索结果",
    }
    assert row["trace_node"] == "references.query"


@pytest.mark.asyncio
async def test_start_chat_run_progress_observer_fails_without_unique_current_phase() -> None:
    observed: list[dict[str, object]] = []

    class _Client:
        async def start_chat_run(self, session_id: str, **kwargs):  # type: ignore[no-untyped-def]
            return {"events": [{"event": "chat"}], "output": "ok"}

        async def get_workflow_snapshot(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {}}

        async def get_matter_phase_timeline(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {"phases": [{"id": "references", "status": "active"}]}}

        async def list_traces(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {"traces": []}}

        async def list_deliverables(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {"deliverables": []}}

    async def _observe(event: dict[str, object]) -> None:
        observed.append(event)

    flow = WorkbenchFlow(
        client=_Client(),
        session_id="session-5b",
        matter_id="matter-1",
        progress_observer=_observe,
    )

    with pytest.raises(AssertionError, match="exactly one current=true"):
        await flow.start_chat_run(
            entry_mode="analysis",
            service_type_id="civil_prosecution",
            delivery_goal="analysis_only",
        )

    assert observed == []


@pytest.mark.asyncio
async def test_nudge_passes_settle_mode() -> None:
    called: dict[str, object] = {}

    class _Client:
        async def chat(self, session_id: str, text: str, **kwargs):  # type: ignore[no-untyped-def]
            called["session_id"] = session_id
            called["text"] = text
            called.update(kwargs)
            return {"events": [{"event": "progress", "data": {"phase": "intake"}}], "output": ""}

    flow = WorkbenchFlow(client=_Client(), session_id="session-chat", uploaded_file_ids=["file-1"])
    flow._emit_progress = lambda *args, **kwargs: asyncio.sleep(0)  # type: ignore[method-assign]

    result = await flow.nudge(
        "kickoff",
        attachments=["file-2"],
        max_loops=6,
        settle_mode="fire_and_poll",
    )

    assert isinstance(result, dict)
    assert called["session_id"] == "session-chat"
    assert called["text"] == "kickoff"
    assert called["attachments"] == ["file-2"]
    assert called["max_loops"] == 6
    assert called["settle_mode"] == "fire_and_poll"


@pytest.mark.asyncio
async def test_resume_blocker_raises_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Client:
        async def resume(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.05)
            return {"events": [{"event": "end", "data": {"output": "late"}}], "output": "late"}

        async def get_workflow_snapshot(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {
                "code": 0,
                "data": {
                    "workbench_runtime": {
                        "current_task_id": "references_finalize",
                        "current_subgraph": "analysis",
                        "has_blocker": False,
                        "blockers": [],
                    }
                },
            }

    monkeypatch.setattr("support.workbench.flow_runner._CARD_RESUME_SETTLE_TIMEOUT_S", 0.01)
    flow = WorkbenchFlow(client=_Client(), session_id="session-timeout", matter_id="matter-timeout")
    flow._emit_progress = lambda *args, **kwargs: asyncio.sleep(0)  # type: ignore[method-assign]
    card = {
        "interruption_id": "awaiting_review:card-timeout",
        "type": "awaiting_review",
        "reason_kind": "missing_input",
        "reason_code": "legal_opinion_intake_gate",
        "questions": [{"field_key": "profile.background", "input_type": "text", "required": True}],
    }

    with pytest.raises(AssertionError, match="pending_card_required_answer_missing:profile.background"):
        await flow.resume_blocker(card, max_loops=4)


@pytest.mark.asyncio
async def test_resume_blocker_raises_after_timeout_even_when_snapshot_pending_count_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        async def resume(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.05)
            return {"events": [{"event": "end", "data": {"output": "late"}}], "output": "late"}

        async def get_workflow_snapshot(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {
                "code": 0,
                "data": {
                    "matter": {"pending_task_count": 0},
                    "analysis_state": {
                        "current_task_id": "evidence_file_analysis_parallel",
                        "current_subgraph": "analysis",
                    },
                },
            }

    monkeypatch.setattr("support.workbench.flow_runner._CARD_RESUME_SETTLE_TIMEOUT_S", 0.01)
    flow = WorkbenchFlow(client=_Client(), session_id="session-timeout-2", matter_id="matter-timeout-2")
    flow._emit_progress = lambda *args, **kwargs: asyncio.sleep(0)  # type: ignore[method-assign]
    card = {
        "interruption_id": "awaiting_review:card-timeout-2",
        "type": "awaiting_review",
        "reason_kind": "missing_input",
        "reason_code": "civil_analysis_intake",
        "questions": [{"field_key": "profile.claims", "input_type": "text", "required": True}],
    }

    with pytest.raises(asyncio.TimeoutError):
        await flow.resume_blocker(card, max_loops=4)


@pytest.mark.asyncio
async def test_resume_blocker_requires_explicit_answer_for_required_text_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        async def resume(self, *_args, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs.get("settle_mode") == "first_event"
            return {"events": [{"event": "progress", "data": {"phase": "intake"}}], "output": ""}

    monkeypatch.setattr("support.workbench.flow_runner._CARD_RESUME_SETTLE_TIMEOUT_S", 0.5)
    flow = WorkbenchFlow(client=_Client(), session_id="session-first-event", matter_id="matter-first-event")
    flow._emit_progress = lambda *args, **kwargs: asyncio.sleep(0)  # type: ignore[method-assign]
    card = {
        "interruption_id": "awaiting_review:card-first-event",
        "type": "awaiting_review",
        "reason_kind": "missing_input",
        "reason_code": "legal_opinion_intake_gate",
        "questions": [{"field_key": "profile.background", "input_type": "text", "required": True}],
    }

    with pytest.raises(AssertionError, match="pending_card_required_answer_missing:profile.background"):
        await flow.resume_blocker(card, max_loops=4)


@pytest.mark.asyncio
async def test_resume_blocker_skill_error_confirm_uses_fire_and_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Client:
        async def resume(self, *_args, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs.get("settle_mode") == "fire_and_poll"
            return {
                "events": [
                    {"event": "resume_submitted", "data": {"partial": True, "settle_mode": "fire_and_poll"}}
                ],
                "output": "resume submitted",
            }

    monkeypatch.setattr("support.workbench.flow_runner._CARD_RESUME_SETTLE_TIMEOUT_S", 0.5)
    flow = WorkbenchFlow(client=_Client(), session_id="session-fire-poll", matter_id="matter-fire-poll")
    flow._emit_progress = lambda *args, **kwargs: asyncio.sleep(0)  # type: ignore[method-assign]
    card = {
        "interruption_id": "awaiting_review:card-fire-poll",
        "type": "awaiting_review",
        "reason_kind": "human_confirmation",
        "reason_code": "skill_error_analysis",
        "questions": [{"field_key": "data.workbench.skill_error_acknowledged", "input_type": "boolean", "required": True}],
    }

    result = await flow.resume_blocker(card, max_loops=4)
    assert isinstance(result, dict)
    assert result.get("output") == "resume submitted"


@pytest.mark.asyncio
async def test_get_current_blocker_returns_api_blocker_without_snapshot_filtering() -> None:
    card = {
        "interruption_id": "awaiting_review:card-1",
        "type": "awaiting_review",
        "reason_kind": "missing_input",
        "reason_code": "legal_opinion_intake_gate",
    }

    class _Client:
        async def get_blocker(self, _session_id: str):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": card}

    flow = WorkbenchFlow(client=_Client(), session_id="session-5", matter_id="matter-5")
    card = await flow.get_current_blocker()
    assert card == {"interruption_id": "awaiting_review:card-1", "type": "awaiting_review", "reason_kind": "missing_input", "reason_code": "legal_opinion_intake_gate"}


@pytest.mark.asyncio
async def test_actionable_blocker_from_sse_returns_none_without_blocker_api_confirmation() -> None:
    sse_card = {
        "interruption_id": "awaiting_review:goal_completion",
        "type": "awaiting_review",
        "interruption_key": "goal_completion",
    }

    class _Client:
        async def get_blocker(self, _session_id: str):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {**sse_card, "title": "authoritative"}}

    flow = WorkbenchFlow(client=_Client(), session_id="session-8", matter_id="matter-8")
    sse = {"events": [{"event": "awaiting_review", "data": sse_card}]}

    resolved = await flow.actionable_blocker_from_sse(sse)

    assert resolved is None


@pytest.mark.asyncio
async def test_actionable_blocker_from_sse_uses_sse_blocker_when_blocker_api_is_empty() -> None:
    sse_card = {
        "interruption_id": "awaiting_review:claim_path",
        "type": "awaiting_review",
        "reason_kind": "missing_input",
        "reason_code": "confirm_claim_path",
    }

    class _Client:
        async def get_blocker(self, _session_id: str):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {}}

    flow = WorkbenchFlow(client=_Client(), session_id="session-8a", matter_id="matter-8a")
    sse = {"events": [{"event": "awaiting_review", "data": sse_card}]}

    resolved = await flow.actionable_blocker_from_sse(sse)

    assert resolved == sse_card


@pytest.mark.asyncio
async def test_step_resumes_authoritative_blocker_before_nudge() -> None:
    current_blocker = {
        "interruption_id": "awaiting_review:card-step",
        "type": "awaiting_review",
        "reason_kind": "missing_input",
        "reason_code": "legal_opinion_intake_gate",
    }
    flow = WorkbenchFlow(client=object(), session_id="session-8b", matter_id="matter-8b")

    async def _refresh() -> None:
        return None

    async def _get_current_blocker() -> dict[str, object]:
        return current_blocker

    async def _resume_blocker(card, *args, **kwargs):  # type: ignore[no-untyped-def]
        return {"current_blocker": card}

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_current_blocker = _get_current_blocker  # type: ignore[method-assign]
    flow.resume_blocker = _resume_blocker  # type: ignore[method-assign]

    result = await flow.step()

    assert isinstance(result, dict)
    assert result.get("current_blocker") == current_blocker


@pytest.mark.asyncio
async def test_step_resumes_blocker_from_last_sse_when_blocker_poll_is_empty() -> None:
    current_blocker = {
        "interruption_id": "awaiting_review:card-sse",
        "type": "awaiting_review",
        "reason_kind": "missing_input",
        "reason_code": "confirm_claim_path",
        "questions": [{"field_key": "profile.client_role", "input_type": "select"}],
    }
    flow = WorkbenchFlow(client=object(), session_id="session-8c", matter_id="matter-8c")
    flow.last_sse = {"events": [{"event": "awaiting_review", "data": current_blocker}]}

    async def _refresh() -> None:
        return None

    async def _get_current_blocker():
        return None

    async def _resume_blocker(card, *args, **kwargs):  # type: ignore[no-untyped-def]
        return {"current_blocker": card}

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_current_blocker = _get_current_blocker  # type: ignore[method-assign]
    flow.resume_blocker = _resume_blocker  # type: ignore[method-assign]

    result = await flow.step()

    assert isinstance(result, dict)
    assert result.get("current_blocker") == current_blocker
