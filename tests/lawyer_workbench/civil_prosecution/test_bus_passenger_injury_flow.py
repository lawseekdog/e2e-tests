from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.lawyer_workbench._support.memory import wait_for_entity_keys
from tests.lawyer_workbench._support.profile import assert_has_party, assert_service_type
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, wait_for_initial_card
from tests.lawyer_workbench._support.sse import assert_visible_response
from tests.lawyer_workbench._support.utils import unwrap_api_response


def _case_facts() -> str:
    return (
        "【当事人】\n"
        "原告：张三E2E_BUS01（男，1988年生，手机 13800000000），乘客。\n"
        "被告1：北京某公交客运有限公司E2E（承运人/公交公司）。\n"
        "被告2：司机王五E2E（被告1雇员，驾驶公交车）。\n"
        "\n"
        "【经过】\n"
        "2026-01-20 08:10 在北京市朝阳区，张三E2E_BUS01乘坐北京市123路公交车（车票见附件）。\n"
        "车辆行驶中司机王五E2E突然急刹车/起步未稳，张三E2E_BUS01在车厢内摔倒，导致右腕受伤。\n"
        "\n"
        "【伤情与治疗】\n"
        "当日门诊诊断右桡骨远端骨折，后住院治疗（诊断证明、住院证明见附件）。\n"
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

    sess = await lawyer_client.create_session(service_type_id="civil_prosecution")
    session_id = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    assert session_id, sess

    flow = WorkbenchFlow(
        client=lawyer_client,
        session_id=session_id,
        uploaded_file_ids=uploaded_file_ids,
        overrides={
            "profile.facts": _case_facts(),
            "profile.claims": "请求判令被告赔偿医疗费、误工费、护理费、交通费等损失并承担诉讼费。",
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
    assert_service_type(prof, "civil_prosecution")
    assert_has_party(prof, role="plaintiff", name_contains="张三E2E_BUS01")
    assert_has_party(prof, role="defendant", name_contains="北京某公交客运有限公司E2E")

    rec = prof.get("cause_recommendation") if isinstance(prof.get("cause_recommendation"), dict) else {}
    recommended_code = str(rec.get("recommended_code") or "").strip()
    assert recommended_code, rec
    assert recommended_code in {"transport_contract", "personal_injury_tort"}, rec

    cands = prof.get("cause_candidates") if isinstance(prof.get("cause_candidates"), list) else []
    assert cands, prof.get("cause_candidates")
    top = cands[0] if isinstance(cands[0], dict) else {}
    top_support = float(top.get("evidence_support") or 0.0)
    assert top_support > 0.0, f"unexpected evidence_support=0 for top cause: {top}"

    # Confirm the cause selection card to continue.
    cause_card = await flow.get_pending_card()
    assert cause_card and str(cause_card.get("skill_id") or "").strip() == "cause-recommendation", cause_card
    cause_sse = await flow.resume_card(cause_card)
    assert_visible_response(cause_sse)

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
    if pending and str(pending.get("skill_id") or "").strip() == "file-classify":
        transcript_path = evidence_dir / "dashcam_transcript.txt"
        up_tx = await lawyer_client.upload_file(str(transcript_path), purpose="consultation")
        tx_id = str(((up_tx.get("data") or {}) if isinstance(up_tx, dict) else {}).get("id") or "").strip()
        assert tx_id, f"upload transcript failed: {up_tx}"
        flow.uploaded_file_ids.append(tx_id)
        tx_sse = await flow.resume_card(pending)
        assert_visible_response(tx_sse)

    # Basic memory smoke: plaintiff evidence should be extracted from facts/evidence names.
    await wait_for_entity_keys(
        lawyer_client,
        user_id=int(lawyer_client.user_id),
        case_id=str(flow.matter_id),
        must_include=["party:plaintiff:张三E2E_BUS01"],
        timeout_s=float(os.getenv("E2E_MEMORY_TIMEOUT_S", "120") or 120),
    )

