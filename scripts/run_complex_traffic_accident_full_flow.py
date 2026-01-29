"""Run a full civil_prosecution flow with a complex traffic-accident evidence set.

Goals:
- Exercise attachments_prepare (ZIP expansion + parse terminal wait)
- Exercise file-insight/file-classify ask_user on unsupported video
- Inject a supplemental evidence upload before strategy confirmation to trigger rollback
- Generate multiple deliverables and dump their extracted text for quality review
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from dotenv import load_dotenv

import sys


# Allow `from client.*` / `from tests.*` when running as a script.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from client.api_client import ApiClient
from tests.lawyer_workbench._support.docx import extract_docx_text
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, wait_for_initial_card
from tests.lawyer_workbench._support.utils import unwrap_api_response


def _in_docker() -> bool:
    return Path("/.dockerenv").exists()


def _case_facts() -> str:
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
        "\n"
        "【证据】\n"
        "交通事故认定书（图文混排PDF）、医疗材料/诊断证明/票据（扫描件/图片）、保险信息摘录（DOCX）、协商聊天记录（DOCX）、\n"
        "事故现场照片、行车记录仪视频及文字转写（如需补充）。\n"
    )


async def _upload_and_get_id(client: ApiClient, path: Path) -> str:
    up = await client.upload_file(str(path), purpose="consultation")
    data = up.get("data") if isinstance(up, dict) else None
    fid = str((data or {}).get("id") or "").strip() if isinstance(data, dict) else ""
    if not fid:
        raise RuntimeError(f"upload failed: {up}")
    return fid


async def _has_deliverable(client: ApiClient, matter_id: str, output_key: str) -> bool:
    resp = await client.list_deliverables(matter_id, output_key=output_key)
    data = unwrap_api_response(resp)
    items = (data.get("deliverables") if isinstance(data, dict) else None) or []
    return bool(items)


async def main() -> None:
    load_dotenv(_ROOT / ".env")

    base_url = os.getenv("BASE_URL", "http://localhost:18001").rstrip("/")
    if _in_docker() and "localhost" in base_url:
        base_url = base_url.replace("localhost", "host.docker.internal")

    user = os.getenv("LAWYER_USERNAME", "lawyer1")
    pwd = os.getenv("LAWYER_PASSWORD", "lawyer123456")

    evidence_dir = Path("/workspace/tmp/e2e_complex_case_evidence") if _in_docker() else (_ROOT.parent / "tmp/e2e_complex_case_evidence")
    zip_path = evidence_dir / "证据包.zip"
    transcript_path = evidence_dir / "行车记录仪视频_文字转写.txt"
    supplement_path = evidence_dir / "补充证据_伤残鉴定受理回执.png"

    out_dir = Path("/workspace/tmp/e2e_doc_outputs") if _in_docker() else (_ROOT.parent / "tmp/e2e_doc_outputs")
    out_dir.mkdir(parents=True, exist_ok=True)

    async with ApiClient(base_url) as c:
        await c.login(user, pwd)

        print("[1] Upload evidence files...", flush=True)
        fid_zip = await _upload_and_get_id(c, zip_path)
        # Do NOT upload transcript yet: we want file-insight/file-classify to ask_user due to video,
        # then upload the transcript as the user's response (tests the card loop + rollback).
        fid_transcript: str | None = None

        print("[2] Create session...", flush=True)
        sess = await c.create_session(service_type_id="civil_first_instance", client_role="plaintiff")
        sid = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
        if not sid:
            raise RuntimeError(f"create_session failed: {sess}")
        print("  session_id:", sid, flush=True)

        flow = WorkbenchFlow(
            client=c,
            session_id=sid,
            uploaded_file_ids=[fid_zip],
            overrides={
                "profile.facts": _case_facts(),
                "profile.claims": "医疗费38,762.45元（以票据为准）及误工费、护理费、营养费、交通费、鉴定费等，保险限额内先行赔付，不足按责任比例承担。",
                # File gap card: allow one round of ask_user, then stop once we upload transcript.
                "data.files.preprocess_stop_ask": False,
                "data.files.preprocess_notes": "视频/图片等不可解析材料：我将补充上传文字转写/文字说明，供后续证据分析与文书写作用。",
                # Generate a full doc pack for quality review.
                "profile.decisions.selected_documents": [
                    "civil_complaint",
                    "litigation_strategy_report",
                    "evidence_list_doc",
                    "compensation_calculation",
                    "preservation_application",
                ],
            },
        )

        print("[3] Wait kickoff card...", flush=True)
        first = await wait_for_initial_card(flow, timeout_s=90.0)
        await flow.resume_card(first)

        cause_confirmed = False
        strategy_confirmed = False
        supplement_uploaded = False
        fid_supplement: str | None = None
        transcript_uploaded = False

        print("[4] Run workflow...", flush=True)
        t0 = time.time()
        for step in range(1, 260):
            await flow.refresh()

            # Periodic progress log for long-running local docker flows (LLM + OCR + rendering).
            if step == 1 or step % 10 == 0:
                mid = flow.matter_id or "?"
                print(
                    f"  step={step} matter_id={mid} cause_confirmed={cause_confirmed} "
                    f"strategy_confirmed={strategy_confirmed} supplement={supplement_uploaded}",
                    flush=True,
                )
                if flow.matter_id:
                    try:
                        pt = unwrap_api_response(await c.get_matter_phase_timeline(flow.matter_id))
                        if isinstance(pt, dict):
                            print(f"    phase={pt.get('current_phase')}", flush=True)
                    except Exception:
                        pass

            # Stop condition: all selected deliverables exist.
            if flow.matter_id:
                ok = True
                for out_key in flow.overrides["profile.decisions.selected_documents"]:
                    if not await _has_deliverable(c, flow.matter_id, out_key):
                        ok = False
                        break
                if ok:
                    print(f"  deliverables ready at step {step} (matter_id={flow.matter_id})", flush=True)
                    break

            card = await flow.get_pending_card()
            if card:
                skill_id = str(card.get("skill_id") or "").strip()
                task_key = str(card.get("task_key") or "").strip()
                # Upload transcript only after the system explicitly asks about unparseable materials.
                if (not transcript_uploaded) and skill_id in {"file-insight", "file-classify"}:
                    fid_transcript = await _upload_and_get_id(c, transcript_path)
                    transcript_uploaded = True
                    flow.uploaded_file_ids.append(fid_transcript)
                    # After we provided a transcript, stop further "补充材料" nagging.
                    flow.overrides["data.files.preprocess_stop_ask"] = True
                    flow.overrides["data.files.preprocess_notes"] = "已补充上传《行车记录仪视频_文字转写.txt》，请结合转写内容理解视频证据。"
                    print(f"  uploaded transcript in response to {skill_id} ask_user (file_id={fid_transcript})", flush=True)

                if "cause" in skill_id or task_key == "confirm_claim_path":
                    cause_confirmed = True
                if "strategy" in skill_id or task_key == "confirm_strategy":
                    # Inject supplemental evidence BEFORE confirming strategy to validate rollback.
                    if cause_confirmed and (not strategy_confirmed) and (not supplement_uploaded):
                        fid_supplement = await _upload_and_get_id(c, supplement_path)
                        supplement_uploaded = True
                        flow.uploaded_file_ids.append(fid_supplement)
                        print(f"  injected supplement (file_id={fid_supplement}) before strategy confirmation", flush=True)
                        await c.chat(
                            sid,
                            "补充证据：伤残鉴定受理回执（用于证明已启动伤残鉴定/后续费用与赔偿项目）。",
                            attachments=[fid_supplement],
                            max_loops=6,
                        )
                        continue
                    strategy_confirmed = True
                await flow.resume_card(card)
                continue

            await flow.nudge("继续", attachments=[], max_loops=12)

        if not flow.matter_id:
            raise RuntimeError("flow did not bind to matter_id")

        print("[5] Download deliverables...", flush=True)
        for out_key in flow.overrides["profile.decisions.selected_documents"]:
            resp = await c.list_deliverables(flow.matter_id, output_key=out_key)
            data = unwrap_api_response(resp)
            items = (data.get("deliverables") if isinstance(data, dict) else None) or []
            if not items:
                print(f"  - missing deliverable: {out_key}", flush=True)
                continue
            d0 = items[0] if isinstance(items[0], dict) else {}
            file_id = str(d0.get("file_id") or "").strip()
            if not file_id:
                print(f"  - deliverable {out_key} missing file_id: {d0}", flush=True)
                continue
            raw = await c.download_file_bytes(file_id)
            out_path = out_dir / f"{flow.matter_id}_{out_key}.docx"
            out_path.write_bytes(raw)
            text = extract_docx_text(raw)
            # Keep console logs short; write full text alongside docx for review.
            (out_dir / f"{flow.matter_id}_{out_key}.txt").write_text(text, encoding="utf-8")
            preview = text.replace("\n", " ")[:180]
            print(f"  - {out_key}: file_id={file_id} preview={preview!r}", flush=True)

        dt = round(time.time() - t0, 2)
        print(f"[done] matter_id={flow.matter_id} session_id={sid} elapsed={dt}s supplement_file_id={fid_supplement}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
