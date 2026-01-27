from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.lawyer_workbench._support.docx import (
    assert_docx_contains,
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.memory import assert_fact_content_contains, wait_for_entity_keys
from tests.lawyer_workbench._support.profile import assert_has_party, assert_service_type
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, wait_for_initial_card
from tests.lawyer_workbench._support.sse import assert_has_end, assert_has_progress, assert_no_error, assert_visible_response
from tests.lawyer_workbench._support.utils import unwrap_api_response


def _case_facts() -> str:
    return (
        "【当事人】\n"
        "原告：张三E2E_BUS01（男，1988年生，手机 13800000000），乘客。\n"
        "被告：北京某公交客运有限公司E2E（承运人/公交公司）。\n"
        "司机：王五E2E（被告雇员，驾驶公交车）。\n"
        "\n"
        "【经过】\n"
        "2026-01-20 08:10 在北京市朝阳区，张三E2E_BUS01乘坐北京市123路公交车（车票见附件）。\n"
        "车辆行驶中司机王五E2E突然急刹车/起步未稳，张三E2E_BUS01在车厢内摔倒，导致右腕受伤。\n"
        "\n"
        "【伤情与治疗】\n"
        "当日门诊诊断右桡骨远端骨折，后住院治疗（诊断证明、住院证明见附件）。\n"
        "\n"
        "【诉求】\n"
        "请求判令被告赔偿医疗费、误工费、护理费、交通费等损失并承担诉讼费。\n"
        "\n"
        "【损失】\n"
        "医疗费、误工费、护理费、交通费等（后续据票据计算）。\n"
        "\n"
        "【证据】\n"
        "公交车票、门诊诊断证明、住院证明。\n"
    )


def _write_minimal_mp4(path: Path) -> None:
    # Minimal MP4-like bytes (ftyp + mdat) so files-service can infer a video-ish mimetype in most envs.
    def _box(box_type: bytes, payload: bytes) -> bytes:
        size = 8 + len(payload)
        return int(size).to_bytes(4, "big") + box_type + payload

    ftyp = _box(b"ftyp", b"isom" + (0).to_bytes(4, "big") + b"isomiso2")
    mdat = _box(b"mdat", b"\x00\x00\x00\x00")
    path.write_bytes(ftyp + mdat)


@pytest.mark.e2e
@pytest.mark.slow
async def test_civil_prosecution_bus_passenger_injury_reaches_cause_recommendation_and_handles_dashcam_video(
    lawyer_client, tmp_path
):
    evidence_dir = Path(__file__).resolve().parent / "evidence"
    init_paths = [
        evidence_dir / "bus_ticket.txt",
        evidence_dir / "outpatient_diagnosis.txt",
        evidence_dir / "hospitalization_certificate.txt",
    ]

    uploaded_file_ids: list[str] = []
    for p in init_paths:
        up = await lawyer_client.upload_file(str(p), purpose="consultation")
        fid = str(((up.get("data") or {}) if isinstance(up, dict) else {}).get("id") or "").strip()
        assert fid, f"upload failed: {up}"
        uploaded_file_ids.append(fid)

    # Service type ids come from platform-service seeds; in current stack this is "civil_first_instance" (民事诉讼一审).
    sess = await lawyer_client.create_session(service_type_id="civil_first_instance")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=uploaded_file_ids,
        overrides={
            "profile.facts": _case_facts(),
            "profile.claims": "请求判令被告赔偿医疗费、误工费、护理费、交通费等损失并承担诉讼费。",
            # Keep E2E fast/stable: generate only a single deliverable at execute stage.
            "profile.decisions.selected_documents": ["civil_complaint"],
            # file-insight will always flag video/* as needs_user_action; allow the workflow to continue
            # in E2E by choosing the "stop asking & proceed with current materials" option.
            "data.files.preprocess_stop_ask": True,
        },
    )

    # Kickoff card should always be present; it is the only allowed way to start a matter now.
    first_card = await wait_for_initial_card(flow, timeout_s=90.0)
    assert str(first_card.get("skill_id") or "").strip() == "system:kickoff", first_card
    kickoff_sse = await flow.resume_card(first_card)
    assert_visible_response(kickoff_sse)

    async def _cause_recommendation_pending(f: WorkbenchFlow) -> bool:
        card = await f.get_pending_card()
        return bool(card) and str(card.get("skill_id") or "").strip() == "cause-recommendation"

    await flow.run_until(_cause_recommendation_pending, max_steps=60, description="cause-recommendation pending card")
    await flow.refresh()
    assert flow.matter_id, "session did not bind to matter_id"

    # Rich facts + parseable text evidence should not trigger extra clarify cards before cause selection.
    assert len(flow.seen_cards) == 1, f"unexpected extra cards before cause-recommendation: {flow.seen_card_signatures}"

    # Validate cause recommendation is produced and evidence score is non-zero (no 'all zero' regression).
    prof = unwrap_api_response(await lawyer_client.get_workflow_profile(flow.matter_id))
    assert isinstance(prof, dict), prof
    assert_service_type(prof, "civil_first_instance")
    assert_has_party(prof, role="plaintiff", name_contains="张三E2E_BUS01")
    assert_has_party(prof, role="defendant", name_contains="北京某公交客运有限公司E2E")

    # Confirm the cause selection card to continue.
    cause_card = await flow.get_pending_card()
    assert cause_card and str(cause_card.get("skill_id") or "").strip() == "cause-recommendation", cause_card
    qs = cause_card.get("questions") if isinstance(cause_card.get("questions"), list) else []
    select_q = None
    for q in qs:
        if not isinstance(q, dict):
            continue
        if str(q.get("input_type") or "").strip().lower() == "select":
            select_q = q
            break
    assert isinstance(select_q, dict), f"missing select question: {cause_card}"
    opts = select_q.get("options") if isinstance(select_q.get("options"), list) else []
    assert opts, f"missing options: {cause_card}"
    rec_opt = None
    for o in opts:
        if isinstance(o, dict) and o.get("recommended") is True:
            rec_opt = o
            break
    assert isinstance(rec_opt, dict), f"missing recommended option: {opts}"
    recommended_code = str(rec_opt.get("value") or "").strip()
    # Rich "公交乘客摔伤 + 医疗证据" 场景下，应优先推荐人身侵权（生命权、身体权、健康权纠纷），
    # 运输合同作为相邻备选保留即可，不应长期占据 Top1。
    assert recommended_code == "personal_injury_tort", rec_opt
    # Ensure the option description includes a non-zero evidence support signal (tool-derived, not LLM-made-up).
    desc = str(rec_opt.get("description") or "").strip()
    assert "证据支撑度" in desc, f"missing evidence_support hint in option description: {rec_opt}"
    assert "证据支撑度 0%" not in desc, f"unexpected evidence_support=0 in option description: {rec_opt}"
    cause_sse = await flow.resume_card(cause_card)
    # Submitting the cause card may trigger a long evidence pipeline; allow an empty assistant bubble
    # as long as the stream is alive (or ended partially) and there are no non-partial errors.
    assert_no_error(cause_sse)
    assert_has_progress(cause_sse)
    assert_has_end(cause_sse)

    # Upload a dashcam video (mp4) to simulate "driver fault" evidence.
    mp4_path = tmp_path / "dashcam_driver_fault.mp4"
    _write_minimal_mp4(mp4_path)
    up_video = await lawyer_client.upload_file(str(mp4_path), purpose="consultation")
    video_id = str(((up_video.get("data") or {}) if isinstance(up_video, dict) else {}).get("id") or "").strip()
    assert video_id, f"upload video failed: {up_video}"
    flow.uploaded_file_ids.append(video_id)

    # Nudge with the new attachment to trigger file ingest/classify/evidence pipeline.
    video_sse = await flow.nudge(
        "补充证据：行车记录仪视频（司机急刹车/未提示乘客扶稳）。",
        attachments=[video_id],
        max_loops=8,
    )
    assert_visible_response(video_sse)

    # If file-classify asks for user action (video usually unparseable), provide a transcript as a follow-up.
    pending = await flow.get_pending_card()
    if pending and str(pending.get("skill_id") or "").strip() in {"file-classify", "file-insight"}:
        transcript_path = evidence_dir / "dashcam_transcript.txt"
        up_tx = await lawyer_client.upload_file(str(transcript_path), purpose="consultation")
        tx_id = str(((up_tx.get("data") or {}) if isinstance(up_tx, dict) else {}).get("id") or "").strip()
        assert tx_id, f"upload transcript failed: {up_tx}"
        flow.uploaded_file_ids.append(tx_id)
        tx_sse = await flow.resume_card(pending)
        assert_visible_response(tx_sse)

    # Basic memory smoke: plaintiff evidence should be extracted from facts/evidence names.
    facts = await wait_for_entity_keys(
        lawyer_client,
        user_id=int(lawyer_client.user_id),
        case_id=str(flow.matter_id),
        must_include=["party:plaintiff:primary"],
        timeout_s=float(os.getenv("E2E_MEMORY_TIMEOUT_S", "120") or 120),
    )
    assert_fact_content_contains(facts, entity_key="party:plaintiff:primary", must_include=["张三E2E_BUS01"])

    # Continue through evidence -> strategy -> work_plan -> execute.
    async def _strategy_pending(f: WorkbenchFlow) -> bool:
        card = await f.get_pending_card()
        return bool(card) and str(card.get("skill_id") or "").strip() == "dispute-strategy-planning"

    await flow.run_until(_strategy_pending, max_steps=80, description="dispute-strategy-planning pending card")
    strategy_card = await flow.get_pending_card()
    assert strategy_card and str(strategy_card.get("skill_id") or "").strip() == "dispute-strategy-planning", strategy_card
    strategy_sse = await flow.resume_card(strategy_card)
    assert_visible_response(strategy_sse)

    async def _strategy_report_generated(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            return False
        resp = await f.client.list_traces(f.matter_id, limit=120)
        data = unwrap_api_response(resp)
        if not isinstance(data, dict):
            return False
        traces = data.get("traces")
        traces = traces if isinstance(traces, list) else []
        for t in traces:
            if not isinstance(t, dict):
                continue
            if str(t.get("node_id") or "").strip() == "skill:dispute-strategy-report":
                return True
        return False

    await flow.run_until(_strategy_report_generated, max_steps=40, description="strategy_report generated after confirmation")

    async def _work_plan_pending(f: WorkbenchFlow) -> bool:
        card = await f.get_pending_card()
        return bool(card) and str(card.get("skill_id") or "").strip() == "work-plan"

    await flow.run_until(_work_plan_pending, max_steps=60, description="work-plan pending card")
    wp_card = await flow.get_pending_card()
    assert wp_card and str(wp_card.get("skill_id") or "").strip() == "work-plan", wp_card
    wp_sse = await flow.resume_card(wp_card)
    assert_visible_response(wp_sse)

    async def _documents_pending(f: WorkbenchFlow) -> bool:
        card = await f.get_pending_card()
        return bool(card) and str(card.get("skill_id") or "").strip() == "documents"

    await flow.run_until(_documents_pending, max_steps=40, description="documents selection pending card")
    doc_card = await flow.get_pending_card()
    assert doc_card and str(doc_card.get("skill_id") or "").strip() == "documents", doc_card
    doc_sse = await flow.resume_card(doc_card)
    assert_visible_response(doc_sse)

    async def _strategy_report_ready(f: WorkbenchFlow) -> bool:
        await f.refresh()
        if not f.matter_id:
            return False
        resp = await f.client.list_deliverables(f.matter_id, output_key="civil_complaint")
        data = unwrap_api_response(resp)
        return isinstance(data, dict) and bool(data.get("deliverables"))

    await flow.run_until(_strategy_report_ready, max_steps=80, description="civil_complaint deliverable")

    # Verify generated DOCX has no unresolved template placeholders and contains key facts.
    deliverables_resp = unwrap_api_response(await lawyer_client.list_deliverables(flow.matter_id, output_key="civil_complaint"))
    deliverables = deliverables_resp.get("deliverables") if isinstance(deliverables_resp, dict) else None
    assert isinstance(deliverables, list) and deliverables, deliverables_resp
    d0 = deliverables[0] if isinstance(deliverables[0], dict) else {}
    file_id = str(d0.get("file_id") or "").strip()
    assert file_id, d0
    docx_bytes = await lawyer_client.download_file_bytes(file_id)
    text = extract_docx_text(docx_bytes)
    assert_docx_has_no_template_placeholders(text)
    assert_docx_contains(
        text,
        must_include=[
            "民事起诉状",
            "张三E2E_BUS01",
            "北京某公交客运有限公司E2E",
            "生命权、身体权、健康权纠纷",
            "公交",
            "急刹车",
            "右桡骨",
        ],
    )
