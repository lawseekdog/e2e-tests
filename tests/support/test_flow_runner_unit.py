from __future__ import annotations

import asyncio

import pytest

from support.workbench.flow_runner import (
    WorkbenchFlow,
    auto_answer_card,
    card_signature,
)


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

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_pending_card = _get_pending_card  # type: ignore[method-assign]
    flow.nudge = _nudge  # type: ignore[method-assign]

    result = await flow.step(
        stop_on_pending_card=lambda card: str(card.get("skill_id")) == "goal-completion",
    )

    assert result is None


@pytest.mark.asyncio
async def test_step_does_not_resume_sse_card_when_pending_poll_returned_empty() -> None:
    review_card = {
        "skill_id": "civil-analysis-intake",
        "task_key": "intake_clarify",
        "questions": [{"field_key": "profile.summary", "input_type": "text"}],
    }
    flow = WorkbenchFlow(client=object(), session_id="session-2b")

    async def _refresh() -> None:
        return None

    async def _get_pending_card():
        return None

    async def _nudge(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {"events": [{"event": "card", "data": review_card}], "output": "需要补充案件信息。"}

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_pending_card = _get_pending_card  # type: ignore[method-assign]
    flow.nudge = _nudge  # type: ignore[method-assign]

    result = await flow.step()

    assert result is None


def test_auto_answer_card_covers_basic_card_input_types() -> None:
    card = {
        "skill_id": "test-skill",
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
        "skill_id": "contract-review",
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
        "skill_id": "skill-error-analysis",
        "task_key": "doc_draft_retry",
        "prompt": "合同审查报告法条引用不足",
        "questions": [{"field_key": "data.workbench.skill_error_acknowledged", "input_type": "boolean", "required": True}],
    }
    flow = WorkbenchFlow(client=object(), session_id="session-3", strict_card_driven=True)

    async def _refresh() -> None:
        return None

    async def _get_pending_card() -> dict[str, object]:
        return card

    async def _nudge(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("strict card mode should not send hidden remediation nudges")

    async def _resume_card(*args, **kwargs):  # type: ignore[no-untyped-def]
        return {"pending_card": card}

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_pending_card = _get_pending_card  # type: ignore[method-assign]
    flow.nudge = _nudge  # type: ignore[method-assign]
    flow.resume_card = _resume_card  # type: ignore[method-assign]
    flow._repeat_card_signature = card_signature(card)
    flow._repeat_card_count = 1

    result = await flow.step()

    assert isinstance(result, dict)
    assert result.get("pending_card") == card


@pytest.mark.asyncio
async def test_request_documents_uses_chat_requested_documents_contract() -> None:
    called: dict[str, object] = {}

    class _Client:
        async def request_documents(self, session_id: str, **kwargs):  # type: ignore[no-untyped-def]
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
    result = await flow.request_documents(
        [{"document_kind": "legal_opinion_report", "instance_key": ""}],
        max_loops=18,
    )

    assert isinstance(result, dict)
    assert called["session_id"] == "session-4"
    assert called["requested_documents"] == [{"document_kind": "legal_opinion_report", "instance_key": ""}]
    assert called["attachments"] == ["file-1"]
    assert called["max_loops"] == 18


@pytest.mark.asyncio
async def test_request_documents_passes_settle_mode() -> None:
    called: dict[str, object] = {}

    class _Client:
        async def request_documents(self, session_id: str, **kwargs):  # type: ignore[no-untyped-def]
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
    result = await flow.request_documents(
        [{"document_kind": "civil_complaint_document", "instance_key": "50"}],
        max_loops=18,
        settle_mode="fire_and_poll",
    )

    assert isinstance(result, dict)
    assert called["session_id"] == "session-4"
    assert called["settle_mode"] == "fire_and_poll"


@pytest.mark.asyncio
async def test_request_documents_notifies_progress_observer() -> None:
    observed: list[dict[str, object]] = []

    class _Client:
        async def request_documents(self, session_id: str, **kwargs):  # type: ignore[no-untyped-def]
            return {"events": [{"event": "chat"}], "output": "ok"}

        async def get_matter_phase_timeline(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {"current_phase": "references", "phases": [{"id": "references", "status": "active"}]}}

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
    await flow.request_documents([{"document_kind": "case_analysis_report", "instance_key": ""}])

    assert observed
    row = observed[-1]
    assert row["label"] == "request:case_analysis_report"
    assert row["matter_id"] == "matter-1"
    assert row["phase"] == "references"
    assert row["trace_node"] == "references.query"


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
async def test_resume_card_raises_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
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
                        "pending_cards": None,
                    }
                },
            }

    monkeypatch.setattr("support.workbench.flow_runner._CARD_RESUME_SETTLE_TIMEOUT_S", 0.01)
    flow = WorkbenchFlow(client=_Client(), session_id="session-timeout", matter_id="matter-timeout")
    flow._emit_progress = lambda *args, **kwargs: asyncio.sleep(0)  # type: ignore[method-assign]
    card = {
        "id": "card-timeout",
        "skill_id": "legal_opinion-intake-gate",
        "task_key": "workflow_input_intake_gate_legal_opinion-intake-gate",
        "questions": [{"field_key": "profile.background", "input_type": "text", "required": True}],
    }

    with pytest.raises(asyncio.TimeoutError):
        await flow.resume_card(card, max_loops=4)


@pytest.mark.asyncio
async def test_resume_card_raises_after_timeout_even_when_snapshot_pending_count_zero(
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
        "id": "card-timeout-2",
        "skill_id": "civil-analysis-intake",
        "task_key": "workflow_input_case_intake_civil-analysis-intake",
        "questions": [{"field_key": "profile.claims", "input_type": "text", "required": True}],
    }

    with pytest.raises(asyncio.TimeoutError):
        await flow.resume_card(card, max_loops=4)


@pytest.mark.asyncio
async def test_resume_card_first_event_mode_accepts_progress_without_user_message(
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
        "id": "card-first-event",
        "skill_id": "legal_opinion-intake-gate",
        "task_key": "workflow_input_intake_gate_legal_opinion-intake-gate",
        "review_type": "clarify",
        "questions": [{"field_key": "profile.background", "input_type": "text", "required": True}],
    }

    result = await flow.resume_card(card, max_loops=4)
    assert isinstance(result, dict)
    events = result.get("events")
    assert isinstance(events, list)
    assert any(
        isinstance(row, dict)
        and row.get("event") == "progress"
        and isinstance(row.get("data"), dict)
        and row.get("data", {}).get("phase") == "intake"
        for row in events
    )


@pytest.mark.asyncio
async def test_resume_card_skill_error_confirm_uses_fire_and_poll(
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
        "id": "card-fire-poll",
        "skill_id": "skill-error-analysis",
        "task_key": "workflow_confirm_legal_opinion_evidence_semantic_events_skill-error-analysis",
        "review_type": "confirm",
        "questions": [{"field_key": "data.workbench.skill_error_acknowledged", "input_type": "boolean", "required": True}],
    }

    result = await flow.resume_card(card, max_loops=4)
    assert isinstance(result, dict)
    assert result.get("output") == "resume submitted"


@pytest.mark.asyncio
async def test_get_pending_card_returns_api_card_without_snapshot_filtering() -> None:
    card = {
        "id": "card-1",
        "skill_id": "legal_opinion-intake-gate",
        "task_key": "workflow_input_intake_gate_legal_opinion-intake-gate",
    }

    class _Client:
        async def get_pending_card(self, _session_id: str):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": card}

    flow = WorkbenchFlow(client=_Client(), session_id="session-5", matter_id="matter-5")
    card = await flow.get_pending_card()
    assert card == {
        "id": "card-1",
        "skill_id": "legal_opinion-intake-gate",
        "task_key": "workflow_input_intake_gate_legal_opinion-intake-gate",
    }


@pytest.mark.asyncio
async def test_actionable_card_from_sse_returns_none_without_pending_card_api_confirmation() -> None:
    card = {
        "id": "card-2",
        "skill_id": "skill-error-analysis",
        "task_key": "workflow_confirm_evidence_conflicts_skill-error-analysis",
    }

    sse_card = {
        "id": "card-live",
        "skill_id": "goal-completion",
        "task_key": "goal_completion",
    }

    class _Client:
        async def get_pending_card(self, _session_id: str):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {**sse_card, "title": "authoritative"}}

    flow = WorkbenchFlow(client=_Client(), session_id="session-8", matter_id="matter-8")
    sse = {"events": [{"event": "card", "data": sse_card}]}

    resolved = await flow.actionable_card_from_sse(sse)

    assert resolved is None


@pytest.mark.asyncio
async def test_actionable_card_from_sse_uses_sse_card_when_pending_card_api_is_empty() -> None:
    sse_card = {
        "id": "card-live",
        "skill_id": "cause-recommendation",
        "task_key": "confirm_claim_path",
    }

    class _Client:
        async def get_pending_card(self, _session_id: str):  # type: ignore[no-untyped-def]
            return {"code": 0, "data": {}}

    flow = WorkbenchFlow(client=_Client(), session_id="session-8a", matter_id="matter-8a")
    sse = {"events": [{"event": "card", "data": sse_card}]}

    resolved = await flow.actionable_card_from_sse(sse)

    assert resolved == sse_card


@pytest.mark.asyncio
async def test_step_resumes_authoritative_pending_card_before_nudge() -> None:
    pending_card = {
        "id": "card-step",
        "skill_id": "legal_opinion-intake-gate",
        "task_key": "workflow_input_intake_gate_legal_opinion-intake-gate",
    }
    flow = WorkbenchFlow(client=object(), session_id="session-8b", matter_id="matter-8b")

    async def _refresh() -> None:
        return None

    async def _get_pending_card() -> dict[str, object]:
        return pending_card

    async def _resume_card(card, *args, **kwargs):  # type: ignore[no-untyped-def]
        return {"pending_card": card}

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_pending_card = _get_pending_card  # type: ignore[method-assign]
    flow.resume_card = _resume_card  # type: ignore[method-assign]

    result = await flow.step()

    assert isinstance(result, dict)
    assert result.get("pending_card") == pending_card


@pytest.mark.asyncio
async def test_step_resumes_card_from_last_sse_when_pending_poll_is_empty() -> None:
    pending_card = {
        "id": "card-sse",
        "skill_id": "cause-recommendation",
        "task_key": "confirm_claim_path",
        "questions": [{"field_key": "profile.subject_code", "input_type": "select"}],
    }
    flow = WorkbenchFlow(client=object(), session_id="session-8c", matter_id="matter-8c")
    flow.last_sse = {"events": [{"event": "card", "data": pending_card}]}

    async def _refresh() -> None:
        return None

    async def _get_pending_card():
        return None

    async def _resume_card(card, *args, **kwargs):  # type: ignore[no-untyped-def]
        return {"pending_card": card}

    flow.refresh = _refresh  # type: ignore[method-assign]
    flow.get_pending_card = _get_pending_card  # type: ignore[method-assign]
    flow.resume_card = _resume_card  # type: ignore[method-assign]

    result = await flow.step()

    assert isinstance(result, dict)
    assert result.get("pending_card") == pending_card
