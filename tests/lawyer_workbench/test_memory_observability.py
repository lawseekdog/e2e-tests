from __future__ import annotations

import pytest

from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, wait_for_initial_card
from tests.lawyer_workbench._support.memory import list_case_facts
from tests.lawyer_workbench._support.sse import assert_task_lifecycle, assert_visible_response
from tests.lawyer_workbench._support.timeline import memory_extraction_events, round_contents, unwrap_timeline
from tests.lawyer_workbench._support.utils import eventually, unwrap_api_response


@pytest.mark.e2e
async def test_memory_extraction_is_observable_per_round(lawyer_client):
    sess = await lawyer_client.create_session(service_type_id="legal_consultation")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        overrides={
            "profile.facts": (
                "我叫张三E2E_M0，与房东李四E2E_M0签订租赁合同，押金2000元。退租时房东以墙面污损为由拒退押金。\n"
                "我有：租赁合同、交接清单、聊天记录。诉求：退还押金2000元。"
            )
        },
    )

    first_card = await wait_for_initial_card(flow, timeout_s=90.0)
    assert str(first_card.get("skill_id") or "").strip() == "system:kickoff", first_card
    kickoff_sse = await flow.resume_card(first_card)
    assert_visible_response(kickoff_sse)
    assert_task_lifecycle(kickoff_sse)

    # Wait until the matter is created so extract-node can write case-scoped memories.
    await flow.run_until(lambda f: bool(f.matter_id), max_steps=20, description="matter_id assigned")
    assert flow.matter_id

    # Add a second, clearly-new fact so memory-extraction should have something to write this round.
    msg2 = "补充：房东微信称墙面修复费500元，并以此拒绝退还押金。"
    sse2 = None
    for _ in range(20):
        sse2 = await flow.step(nudge_text=msg2)
        assert_visible_response(sse2)
        # If there is a pending interrupt card, the server may short-circuit the SSE (card/end only).
        # We only require task_start/task_end when the stream actually ran the graph.
        if any(e.get("event") == "task_start" for e in (sse2.get("events") if isinstance(sse2.get("events"), list) else [])):
            assert_task_lifecycle(sse2)
        msgs = [e for e in (sse2.get("events") if isinstance(sse2.get("events"), list) else []) if e.get("event") == "user_message"]
        content = ""
        if msgs and isinstance(msgs[-1].get("data"), dict):
            content = str(msgs[-1]["data"].get("content") or "")
        if msg2 in content:
            break
    else:
        raise AssertionError(f"failed to send second user message: {msg2}")

    async def _timeline_ready():
        tl_resp = await lawyer_client.get_matter_timeline(flow.matter_id, limit=30)
        tl = unwrap_timeline(tl_resp)
        # Every round_summary should include memory_traces (recall/extraction may be null).
        contents = round_contents(tl)
        if len(contents) < 2:
            return None
        for c in contents:
            mt = c.get("memory_traces")
            if not isinstance(mt, dict) or ("extraction" not in mt):
                return None
        return tl

    tl = await eventually(_timeline_ready, timeout_s=120.0, interval_s=2.0, description="timeline includes memory_traces")
    assert isinstance(tl, dict)

    exts = memory_extraction_events(tl)
    assert len(exts) >= 1, tl
    assert any(int(e.get("extracted_count") or 0) > 0 for e in exts), exts

    async def _memory_has_500():
        facts = await list_case_facts(
            lawyer_client,
            user_id=int(lawyer_client.user_id),
            case_id=str(flow.matter_id),
            limit=400,
        )
        for it in facts:
            if not isinstance(it, dict):
                continue
            if "500" in str(it.get("content") or ""):
                return facts
        return None

    assert await eventually(
        _memory_has_500,
        timeout_s=120.0,
        interval_s=2.0,
        description="memory contains '500' fact",
    )
