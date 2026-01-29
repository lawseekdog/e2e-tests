"""Debug runner: civil_prosecution complex traffic-accident flow with mixed evidence + rollback.

Goals:
- Exercise files-service parsing (PDF mixed, scanned/OCR, DOCX, PNG) + ZIP expand + video unsupported.
- Verify file-insight/file-classify ask_user happens for the video and we can satisfy via transcript.
- Upload supplemental evidence BEFORE strategy is confirmed to trigger rollback (attachments_prepare -> re-run).
- Generate multiple deliverables and download them for manual quality review.

This is a dev helper (not part of CI).
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.api_client import ApiClient


EVID_DIR = Path(__file__).resolve().parents[2] / "tmp" / "e2e_complex_case_evidence"


def _case_facts() -> str:
    return (
        "【当事人】\n"
        "原告：张三（男，1990年生，手机 13800000000）。\n"
        "被告1：李四（男，1989年生），驾驶人。\n"
        "被告2：北京某保险股份有限公司（待核实），承保车辆交强险/商业三者险。\n"
        "\n"
        "【经过】\n"
        "2026-03-15 18:40，北京市朝阳区某路口发生交通事故。李四驾驶京A12345小客车右转未礼让行人张三，发生碰撞，致张三摔倒受伤。\n"
        "交警出具《交通事故认定书》（节选见附件），初步显示李四负主要责任、张三负次要责任（比例待核实）。\n"
        "\n"
        "【伤情】\n"
        "张三右桡骨远端骨折、软组织挫伤，住院治疗并产生医疗费用（医疗材料扫描件见附件）。\n"
        "\n"
        "【争议与诉求】\n"
        "保险理赔与赔偿比例无法协商一致，拟起诉要求李四及保险公司赔偿医疗费、误工费、护理费、交通费、营养费等并承担诉讼费。\n"
        "\n"
        "【证据】\n"
        "事故认定书（图文混排PDF）、医疗材料（扫描件PDF/OCR）、现场照片、转账截图、聊天记录整理（DOCX）、误工/护理费计算表（DOCX）、保险信息摘录（DOCX）、行车记录仪视频（MP4，另附文字转写）。\n"
    )


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


def _auto_answer_card(card: dict[str, Any], overrides: dict[str, Any], uploaded_file_ids: list[str]) -> dict[str, Any]:
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

        # Special handling for preprocess stop-ask: keep asking by default (false).
        if fk == "data.files.preprocess_stop_ask":
            answers.append({"field_key": fk, "value": False})
            continue

        value: Any | None = None

        default = q.get("default")
        has_default = default is not None and not (
            (isinstance(default, str) and not default.strip())
            or (isinstance(default, list) and not default)
            or (isinstance(default, dict) and not default)
        )

        if it in {"boolean", "bool"}:
            value = default if has_default else True
        elif it in {"select", "single_select", "single_choice"}:
            value = default if has_default else _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
        elif it in {"multi_select", "multiple_select"}:
            value = default if has_default else []
            if isinstance(value, list) and not value:
                first = _pick_recommended_or_first(q.get("options") if isinstance(q.get("options"), list) else [])
                value = [first] if first is not None else []
        elif it in {"file_ids", "file_id"} or fk == "attachment_file_ids":
            # Always keep existing attachments; add more via uploaded_file_ids list.
            value = uploaded_file_ids
            if fk != "attachment_file_ids" and not required:
                # Some cards use optional file_ids; don't force.
                value = uploaded_file_ids
        else:
            value = default if has_default else ("已确认" if required else None)

        if required and (value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)):
            value = True if it in {"boolean", "bool"} else "已确认"

        # For optional questions with no value, skip.
        if value is None and not required:
            continue

        answers.append({"field_key": fk, "value": value})

    return {"answers": answers}


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    base_url = os.getenv("BASE_URL", "http://localhost:18001")
    user = os.getenv("LAWYER_USERNAME", "lawyer1")
    pwd = os.getenv("LAWYER_PASSWORD", "lawyer123456")

    if not EVID_DIR.exists():
        raise RuntimeError(f"evidence dir missing: {EVID_DIR}")

    # Initial attachments: upload the ZIP + statement text.
    init_paths = [
        EVID_DIR / "证据包.zip",
        EVID_DIR / "当事人陈述.txt",
    ]
    # Transcript is uploaded only if the workflow asks for it.
    transcript_path = EVID_DIR / "行车记录仪视频_文字转写.txt"
    supplemental_path = EVID_DIR / "补充证据_伤残鉴定受理回执.png"

    selected_docs = [
        "civil_complaint",
        "litigation_strategy_report",
        "evidence_list_doc",
        "compensation_calculation",
        "preservation_application",
    ]

    overrides: dict[str, Any] = {
        "profile.facts": _case_facts(),
        "profile.claims": "请求判令被告赔偿医疗费、误工费、护理费、交通费、营养费等损失并承担诉讼费。",
        "profile.decisions.selected_documents": selected_docs,
    }

    async with ApiClient(base_url) as c:
        await c.login(user, pwd)

        uploaded_file_ids: list[str] = []
        for p in init_paths:
            up = await c.upload_file(str(p), purpose="consultation")
            fid = str((up.get("data") or {}).get("id") or "").strip()
            print("uploaded", p.name, fid, flush=True)
            uploaded_file_ids.append(fid)

        sess = await c.create_session(service_type_id="civil_prosecution")
        sid = str((sess.get("data") or {}).get("id") or "").strip()
        print("session", sid, flush=True)

        # Kickoff via chat (facts + attachments).
        t0 = time.time()
        await c.chat(sid, str(overrides["profile.facts"]), attachments=list(uploaded_file_ids), max_loops=8)
        print("kickoff chat done", round(time.time() - t0, 2), "s", flush=True)

        transcript_uploaded = False
        supplemental_uploaded = False
        matter_id: str | None = None
        t_start = time.time()

        for i in range(320):
            sess2 = await c.get_session(sid)
            matter_id = str((sess2.get("data") or {}).get("matter_id") or "").strip() or matter_id

            # Stop if all deliverables are ready.
            if matter_id:
                all_ready = True
                for key in selected_docs:
                    dels = await c.list_deliverables(matter_id, output_key=key)
                    items = ((dels.get("data") or {}) if isinstance(dels, dict) else {}).get("deliverables") or []
                    d0 = items[0] if items and isinstance(items[0], dict) else {}
                    if not items or not str(d0.get("file_id") or "").strip():
                        all_ready = False
                        break
                if all_ready:
                    print("ALL deliverables ready at iter", i, "elapsed", round(time.time() - t_start, 2), "s", flush=True)
                    break

            pending = await c.get_pending_card(sid)
            card = pending.get("data")
            if card:
                skill_id = str(card.get("skill_id") or "").strip()
                rt = str(card.get("review_type") or "").strip()
                print("iter", i, "card", card.get("task_key"), rt, skill_id, flush=True)

                # Upload transcript when preprocess cards ask due to video.
                if skill_id in {"file-insight", "file-classify"} and not transcript_uploaded:
                    # Detect if the card has a preprocess stop-ask + attachment_file_ids question.
                    qs = card.get("questions") if isinstance(card.get("questions"), list) else []
                    has_file_q = any(str(q.get("field_key") or "").strip() == "attachment_file_ids" for q in qs if isinstance(q, dict))
                    if has_file_q:
                        up_tx = await c.upload_file(str(transcript_path), purpose="consultation")
                        tx_id = str((up_tx.get("data") or {}).get("id") or "").strip()
                        print("  uploaded transcript", tx_id, flush=True)
                        uploaded_file_ids.append(tx_id)
                        transcript_uploaded = True

                # Trigger rollback: upload supplemental evidence BEFORE strategy is confirmed.
                if skill_id == "dispute-strategy-planning" and not supplemental_uploaded:
                    up_s = await c.upload_file(str(supplemental_path), purpose="consultation")
                    sid2 = str((up_s.get("data") or {}).get("id") or "").strip()
                    print("  uploaded supplemental evidence", sid2, flush=True)
                    uploaded_file_ids.append(sid2)
                    supplemental_uploaded = True
                    t1 = time.time()
                    await c.chat(
                        sid,
                        "补充证据：已提交伤残鉴定受理回执（可能影响误工/护理/营养期与赔偿计算）。请据此回退并重新评估策略与计划。",
                        attachments=[sid2],
                        max_loops=10,
                    )
                    print("  nudge supplemental", round(time.time() - t1, 2), "s", flush=True)
                    continue

                t1 = time.time()
                await c.resume(sid, _auto_answer_card(card, overrides, uploaded_file_ids), pending_card=card)
                print("  resume", round(time.time() - t1, 2), "s", flush=True)
                continue

            # No card: nudge.
            t1 = time.time()
            await c.chat(sid, "继续", attachments=[], max_loops=12)
            print("iter", i, "no card -> chat", round(time.time() - t1, 2), "s", "matter_id", matter_id, flush=True)

        print("final matter_id", matter_id, flush=True)

        # Download deliverables for manual inspection.
        if matter_id:
            out_dir = Path(__file__).resolve().parents[2] / "tmp" / f"e2e_outputs_{int(time.time())}"
            out_dir.mkdir(parents=True, exist_ok=True)
            for key in selected_docs:
                dels = await c.list_deliverables(matter_id, output_key=key)
                items = ((dels.get("data") or {}) if isinstance(dels, dict) else {}).get("deliverables") or []
                d0 = items[0] if items and isinstance(items[0], dict) else {}
                file_id = str(d0.get("file_id") or "").strip()
                if not file_id:
                    continue
                content = await c.download_file_bytes(file_id)
                fname = f"{key}_{file_id}.docx"
                (out_dir / fname).write_bytes(content)
                print("saved", fname, "bytes", len(content), flush=True)
            print("outputs_dir", out_dir, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
