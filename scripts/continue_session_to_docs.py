"""Continue an existing lawyer-workbench session until docx deliverables are generated.

Why:
- Long-running SSE (max_loops too large) can make a single E2E call "hang" for tens of minutes.
- This script drives the workflow in smaller chunks (max_loops per request) and polls phase/cards.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from dotenv import load_dotenv

import sys


_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from client.api_client import ApiClient
from tests.lawyer_workbench._support.docx import extract_docx_text
from tests.lawyer_workbench._support.flow_runner import auto_answer_card
from tests.lawyer_workbench._support.utils import unwrap_api_response


def _case_facts() -> str:
    # Keep this in sync with the complex evidence generator case.
    return (
        "【当事人】\n"
        "原告：张三（行人）。\n"
        "被告1：李四（驾驶人）。\n"
        "被告2：北京某保险股份有限公司（承保交强险及商业三者险，具体险种/保额待核实）。\n"
        "\n"
        "【事故经过】\n"
        "2025-12-15 18:40，北京市朝阳区某路口，李四驾驶小客车右转未礼让行人张三，发生碰撞致张三摔倒受伤。\n"
        "交警出具《交通事故认定书》（拟认定：李四主责、张三次责；比例待核实）。\n"
        "\n"
        "【伤情与治疗】\n"
        "张三右桡骨远端骨折，门诊/住院治疗，已发生医疗费约38,762.45元（以票据为准）。\n"
        "后续可能需要伤残鉴定，并计算误工/护理/营养/交通等损失。\n"
        "\n"
        "【诉求】\n"
        "请求判令李四及保险公司在交强险/商业险限额内先行赔付医疗费、误工费、护理费、营养费、交通费、鉴定费等，\n"
        "不足部分按责任比例由李四承担，并承担诉讼费；必要时考虑诉前/诉中财产保全。\n"
    )


async def _upload_and_get_id(client: ApiClient, path: Path) -> str:
    up = await client.upload_file(str(path), purpose="consultation")
    data = up.get("data") if isinstance(up, dict) else None
    fid = str((data or {}).get("id") or "").strip() if isinstance(data, dict) else ""
    if not fid:
        raise RuntimeError(f"upload failed: {up}")
    return fid


async def _pick_rendered_deliverable(client: ApiClient, matter_id: str, output_key: str) -> dict | None:
    resp = await client.list_deliverables(matter_id, output_key=output_key)
    data = unwrap_api_response(resp)
    items = (data.get("deliverables") if isinstance(data, dict) else None) or []
    for it in items:
        if not isinstance(it, dict):
            continue
        if str(it.get("file_id") or "").strip():
            return it
    return None


async def main() -> None:
    load_dotenv(_ROOT / ".env")

    base_url = os.getenv("BASE_URL", "http://localhost:18001").rstrip("/")
    user = os.getenv("LAWYER_USERNAME", "lawyer1")
    pwd = os.getenv("LAWYER_PASSWORD", "lawyer123456")

    session_id = str(os.getenv("SESSION_ID", "4")).strip()
    if not session_id:
        raise ValueError("SESSION_ID is required")

    max_steps = int(os.getenv("MAX_STEPS", "240") or 240)
    max_loops = int(os.getenv("MAX_LOOPS_PER_CALL", "6") or 6)

    # Evidence pack artifacts for rollback test.
    evidence_dir = _ROOT.parent / "tmp/e2e_complex_case_evidence"
    transcript_path = evidence_dir / "行车记录仪视频_文字转写.txt"
    supplement_path = evidence_dir / "补充证据_伤残鉴定受理回执.png"

    # Where to dump outputs.
    out_dir = _ROOT.parent / "tmp/e2e_doc_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    wanted_docs = [
        "civil_complaint",
        "litigation_strategy_report",
        "evidence_list_doc",
        "compensation_calculation",
        "preservation_application",
    ]

    overrides = {
        "profile.facts": _case_facts(),
        "profile.claims": "医疗费38,762.45元（以票据为准）及误工费、护理费、营养费、交通费、鉴定费等，保险限额内先行赔付，不足按责任比例承担。",
        # Doc selection card (multi_select): force a full pack for quality review.
        "profile.decisions.selected_documents": wanted_docs,
        # After we provide a transcript, stop re-asking about the same unsupported video.
        "data.files.preprocess_stop_ask": True,
        "data.files.preprocess_notes": "已补充上传《行车记录仪视频_文字转写.txt》，请结合转写内容理解视频证据。",
    }

    transcript_uploaded = False
    supplement_uploaded = False
    fid_supplement: str | None = None

    async with ApiClient(base_url) as c:
        await c.login(user, pwd)

        t0 = time.time()
        matter_id: str | None = None

        for step in range(1, max_steps + 1):
            sess = unwrap_api_response(await c.get_session(session_id))
            matter_id = str((sess or {}).get("matter_id") or "").strip()

            phase = "?"
            if matter_id:
                pt = unwrap_api_response(await c.get_matter_phase_timeline(matter_id))
                if isinstance(pt, dict):
                    phase = str(pt.get("current_phase") or "?")

            if step == 1 or step % 10 == 0:
                print(f"[step {step}] session_id={session_id} matter_id={matter_id} phase={phase}", flush=True)

            # Stop condition: all requested documents have rendered file deliverables.
            if matter_id:
                if all([await _pick_rendered_deliverable(c, matter_id, k) for k in wanted_docs]):
                    print(f"[ready] all doc deliverables present (matter_id={matter_id})", flush=True)
                    break

            card_resp = await c.get_pending_card(session_id)
            card = unwrap_api_response(card_resp)
            card = card if isinstance(card, dict) and card else None

            if card:
                skill_id = str(card.get("skill_id") or "").strip()
                task_key = str(card.get("task_key") or "").strip()

                # Inject supplemental evidence BEFORE confirming strategy to validate rollback.
                if (not supplement_uploaded) and (skill_id.find("strategy") >= 0 or task_key == "confirm_strategy"):
                    fid_supplement = await _upload_and_get_id(c, supplement_path)
                    supplement_uploaded = True
                    print(f"  injected supplement (file_id={fid_supplement}) before strategy confirmation", flush=True)
                    await c.chat(
                        session_id,
                        "补充证据：伤残鉴定受理回执（用于证明已启动伤残鉴定/后续费用与赔偿项目）。",
                        attachments=[fid_supplement],
                        max_loops=max_loops,
                    )
                    continue

                # If the system asks about unsupported materials, upload the transcript once.
                if (not transcript_uploaded) and skill_id in {"file-insight", "file-classify"}:
                    fid_transcript = await _upload_and_get_id(c, transcript_path)
                    transcript_uploaded = True
                    # Attach transcript to the session by sending a chat message so downstream skills can see it.
                    await c.chat(
                        session_id,
                        "补充材料：行车记录仪视频的文字转写（用于理解视频内容）。",
                        attachments=[fid_transcript],
                        max_loops=max_loops,
                    )

                if skill_id == "system:kickoff":
                    # Kickoff is optimized: chat instead of resume.
                    await c.chat(session_id, overrides["profile.facts"], attachments=[], max_loops=max_loops)
                    continue

                user_response = auto_answer_card(card, overrides=overrides, uploaded_file_ids=[])
                await c.resume(session_id, user_response, pending_card=card, max_loops=max_loops)
                continue

            # No pending card: keep nudging the workflow forward.
            await c.chat(session_id, "继续", attachments=[], max_loops=max_loops)

        if not matter_id:
            raise RuntimeError("session has no matter_id bound; cannot download deliverables")

        print("[download] dumping docx + extracted text...", flush=True)
        for out_key in wanted_docs:
            d = await _pick_rendered_deliverable(c, matter_id, out_key)
            if not d:
                print(f"  - missing rendered deliverable: {out_key}", flush=True)
                continue
            file_id = str(d.get("file_id") or "").strip()
            raw = await c.download_file_bytes(file_id)
            out_path = out_dir / f"{matter_id}_{out_key}.docx"
            out_path.write_bytes(raw)
            text = extract_docx_text(raw)
            (out_dir / f"{matter_id}_{out_key}.txt").write_text(text, encoding="utf-8")
            preview = text.replace("\n", " ")[:180]
            print(f"  - {out_key}: file_id={file_id} preview={preview!r}", flush=True)

        dt = round(time.time() - t0, 2)
        print(
            f"[done] session_id={session_id} matter_id={matter_id} elapsed={dt}s supplement_file_id={fid_supplement}",
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
