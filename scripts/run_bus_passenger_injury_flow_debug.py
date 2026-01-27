"""Debug runner: civil_first_instance bus passenger injury flow.

This mirrors the E2E test but prints progress so we can see where the workflow stalls.
It is a dev helper (not part of CI).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import sys

# Allow `from client.*` when running as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.api_client import ApiClient


def _case_facts() -> str:
    return (
        "【当事人】\n"
        "原告：张三E2E_BUS_DEBUG（男，1988年生，手机 13800000000），乘客。\n"
        "被告：北京某公交客运有限公司E2E（承运人/公交公司）。\n"
        "司机：王五E2E（被告雇员，驾驶公交车）。\n"
        "\n"
        "【经过】\n"
        "2026-01-20 08:10 在北京市朝阳区，张三E2E_BUS_DEBUG乘坐北京市123路公交车（车票见附件）。\n"
        "车辆行驶中司机王五E2E突然急刹车/起步未稳，张三E2E_BUS_DEBUG在车厢内摔倒，导致右腕受伤。\n"
        "\n"
        "【伤情与治疗】\n"
        "当日门诊诊断右桡骨远端骨折，后住院治疗（诊断证明、住院证明见附件）。\n"
        "\n"
        "【诉求】\n"
        "请求判令被告赔偿医疗费、误工费、护理费、交通费等损失并承担诉讼费。\n"
        "\n"
        "【证据】\n"
        "公交车票、门诊诊断证明、住院证明。\n"
    )


def _write_minimal_mp4(path: Path) -> None:
    def _box(box_type: bytes, payload: bytes) -> bytes:
        size = 8 + len(payload)
        return int(size).to_bytes(4, "big") + box_type + payload

    ftyp = _box(b"ftyp", b"isom" + (0).to_bytes(4, "big") + b"isomiso2")
    mdat = _box(b"mdat", b"\x00\x00\x00\x00")
    path.write_bytes(ftyp + mdat)


def _pick_recommended_or_first(options: list[Any]) -> Any | None:
    if not isinstance(options, list) or not options:
        return None
    for opt in options:
        if isinstance(opt, dict) and opt.get("recommended") is True and opt.get("value") is not None:
            return opt.get("value")
    for opt in options:
        if isinstance(opt, dict) and opt.get("value") is not None:
            return opt.get("value")
    return None


def _auto_answer_card(card: dict, overrides: dict[str, Any], uploaded_file_ids: list[str]) -> dict[str, Any]:
    questions = card.get("questions") if isinstance(card.get("questions"), list) else []
    answers: list[dict[str, Any]] = []

    for q in questions:
        if not isinstance(q, dict):
            continue
        fk = str(q.get("field_key") or "").strip()
        if not fk:
            continue
        it = str(q.get("input_type") or q.get("question_type") or "").strip().lower()
        required = bool(q.get("required"))

        if fk in overrides:
            answers.append({"field_key": fk, "value": overrides[fk]})
            continue

        default = q.get("default")
        has_default = default is not None and not (
            (isinstance(default, str) and not default.strip())
            or (isinstance(default, list) and not default)
            or (isinstance(default, dict) and not default)
        )

        value: Any | None = None
        if it in {"boolean", "bool"}:
            value = default if has_default else True
        elif it in {"select", "single_select", "single_choice"}:
            value = default if has_default else _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
        elif it in {"multi_select", "multiple_select"}:
            if has_default:
                value = default
            else:
                first = _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
                value = [first] if first is not None else []
        elif it in {"file_ids", "file_id"} or fk == "attachment_file_ids":
            if fk == "attachment_file_ids":
                value = default if has_default else uploaded_file_ids
            else:
                value = default if has_default else ([] if not required else uploaded_file_ids[:1])
        else:
            value = default if has_default else ("已确认" if required else None)

        if required and (value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)):
            value = True if it in {"boolean", "bool"} else "已确认"

        answers.append({"field_key": fk, "value": value})

    return {"answers": answers}


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    base_url = os.getenv("BASE_URL", "http://localhost:18001")
    user = os.getenv("LAWYER_USERNAME", "lawyer1")
    pwd = os.getenv("LAWYER_PASSWORD", "lawyer123456")

    evidence_dir = Path(__file__).resolve().parent.parent / "tests" / "lawyer_workbench" / "civil_prosecution" / "evidence"
    init_paths = [
        evidence_dir / "bus_ticket.txt",
        evidence_dir / "outpatient_diagnosis.txt",
        evidence_dir / "hospitalization_certificate.txt",
    ]

    overrides = {
        "profile.facts": _case_facts(),
        "profile.claims": "请求判令被告赔偿医疗费、误工费、护理费、交通费等损失并承担诉讼费。",
        "profile.decisions.selected_documents": ["civil_complaint"],
    }

    async with ApiClient(base_url) as c:
        await c.login(user, pwd)

        uploaded_file_ids: list[str] = []
        for p in init_paths:
            up = await c.upload_file(str(p), purpose="consultation")
            fid = str((up.get("data") or {}).get("id") or "").strip()
            print("uploaded", p.name, fid, flush=True)
            uploaded_file_ids.append(fid)

        sess = await c.create_session(service_type_id="civil_first_instance")
        sid = str((sess.get("data") or {}).get("id") or "").strip()
        print("session", sid, flush=True)

        video_uploaded = False
        transcript_uploaded = False
        matter_id = None

        for i in range(220):
            sess2 = await c.get_session(sid)
            matter_id = (sess2.get("data") or {}).get("matter_id") or matter_id

            if matter_id:
                dels = await c.list_deliverables(str(matter_id), output_key="civil_complaint")
                data = dels.get("data") or {}
                if data.get("deliverables"):
                    print("civil_complaint deliverable ready at iter", i, flush=True)
                    break

            pending = await c.get_pending_card(sid)
            card = pending.get("data")
            if card:
                skill_id = str(card.get("skill_id") or "").strip()
                print("iter", i, "card", card.get("task_key"), card.get("review_type"), skill_id, flush=True)

                # system:kickoff is resumed by sending a normal chat (facts + attachments).
                if skill_id == "system:kickoff":
                    t0 = time.time()
                    await c.chat(sid, str(overrides["profile.facts"]), attachments=list(uploaded_file_ids), max_loops=6)
                    print("  kickoff chat", round(time.time() - t0, 2), "s", flush=True)
                    continue

                # If the workflow asks to classify the dashcam video, upload a transcript first.
                if skill_id == "file-classify" and video_uploaded and not transcript_uploaded:
                    tx_path = evidence_dir / "dashcam_transcript.txt"
                    up_tx = await c.upload_file(str(tx_path), purpose="consultation")
                    tx_id = str((up_tx.get("data") or {}).get("id") or "").strip()
                    print("  uploaded transcript", tx_id, flush=True)
                    uploaded_file_ids.append(tx_id)
                    transcript_uploaded = True

                t0 = time.time()
                await c.resume(sid, _auto_answer_card(card, overrides, uploaded_file_ids))
                print("  resume", round(time.time() - t0, 2), "s", flush=True)

                # After confirming cause selection, upload dashcam video and nudge once.
                if skill_id == "cause-recommendation" and not video_uploaded:
                    mp4_path = (Path(__file__).resolve().parent.parent / "tmp" / f"dashcam_{int(time.time())}.mp4")
                    mp4_path.parent.mkdir(parents=True, exist_ok=True)
                    _write_minimal_mp4(mp4_path)
                    up_v = await c.upload_file(str(mp4_path), purpose="consultation")
                    vid = str((up_v.get("data") or {}).get("id") or "").strip()
                    print("  uploaded dashcam video", vid, flush=True)
                    uploaded_file_ids.append(vid)
                    video_uploaded = True
                    t1 = time.time()
                    await c.chat(
                        sid,
                        "补充证据：行车记录仪视频（司机急刹车/未提示乘客扶稳）。",
                        attachments=[vid],
                        max_loops=8,
                    )
                    print("  nudge video", round(time.time() - t1, 2), "s", flush=True)
                continue

            print("iter", i, "no card -> continue", "matter_id", matter_id, flush=True)
            t0 = time.time()
            await c.chat(sid, "继续", attachments=[], max_loops=10)
            print("  chat", round(time.time() - t0, 2), "s", flush=True)

        print("final matter_id", matter_id, flush=True)


if __name__ == "__main__":
    asyncio.run(main())

