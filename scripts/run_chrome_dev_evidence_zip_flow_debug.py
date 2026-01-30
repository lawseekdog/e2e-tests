"""Debug runner: use a single evidence.zip to run lawyer-workbench flows end-to-end.

This is meant to mirror the manual Chrome Dev workflow:
- Civil first instance (plaintiff): generate a civil complaint.
- Civil first instance (defendant): generate a defense statement.

It uploads ONE zip (evidence.zip) as a session attachment so files-service expands it into child files.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

import sys


_E2E_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _E2E_ROOT.parent
sys.path.insert(0, str(_E2E_ROOT))

from client.api_client import ApiClient
from tests.lawyer_workbench._support.docx import (
    assert_docx_contains,
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import auto_answer_card
from tests.lawyer_workbench._support.utils import unwrap_api_response


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


async def _wait_for_deliverable_file_id(client: ApiClient, matter_id: str, output_key: str) -> Optional[str]:
    resp = await client.list_deliverables(matter_id, output_key=output_key)
    data = unwrap_api_response(resp)
    items = (data.get("deliverables") if isinstance(data, dict) else None) or []
    if not items:
        return None
    d0 = items[0] if isinstance(items[0], dict) else {}
    fid = str(d0.get("file_id") or "").strip()
    return fid or None


async def _run_until_doc_ready(
    *,
    client: ApiClient,
    service_type_id: str,
    output_key: str,
    overrides: dict[str, Any],
    evidence_zip: Path,
    max_steps: int = 260,
    max_loops_per_call: int = 10,
    step_sleep_s: float = 1.0,
) -> dict[str, Any]:
    sess = await client.create_session(service_type_id=service_type_id)
    sid = str(((sess.get("data") or {}) if isinstance(sess, dict) else {}).get("id") or "").strip()
    if not sid:
        raise RuntimeError(f"create_session failed: {sess}")
    print(f"[session] service_type_id={service_type_id} session_id={sid}", flush=True)

    # Mirror frontend behavior: upload an attachment bound to the session.
    up = await client.upload_session_attachment(sid, str(evidence_zip))
    up_data = up.get("data") if isinstance(up, dict) else None
    zip_file_id = str(((up_data or {}) if isinstance(up_data, dict) else {}).get("file_id") or "").strip()
    if not zip_file_id:
        raise RuntimeError(f"upload_session_attachment did not return file_id: {up}")
    print(f"[upload] zip={evidence_zip.name} file_id={zip_file_id}", flush=True)

    matter_id: str | None = None
    t0 = time.time()

    for step in range(1, max_steps + 1):
        sess2 = unwrap_api_response(await client.get_session(sid))
        matter_id = str((sess2 or {}).get("matter_id") or "").strip() if isinstance(sess2, dict) else matter_id

        if matter_id:
            fid = await _wait_for_deliverable_file_id(client, matter_id, output_key)
            if fid:
                dt = round(time.time() - t0, 2)
                print(f"[ready] output_key={output_key} matter_id={matter_id} file_id={fid} elapsed={dt}s", flush=True)
                return {"session_id": sid, "matter_id": matter_id, "file_id": fid}

        card = unwrap_api_response(await client.get_pending_card(sid))
        card = card if isinstance(card, dict) and card else None

        if card:
            skill_id = str(card.get("skill_id") or "").strip()
            task_key = str(card.get("task_key") or "").strip()
            print(f"[step {step}] pending_card skill_id={skill_id} task_key={task_key}", flush=True)

            # Ensure attachment ids are present when a card asks for them.
            user_response = auto_answer_card(card, overrides=overrides, uploaded_file_ids=[zip_file_id])
            await client.resume(sid, user_response, pending_card=card, max_loops=max_loops_per_call)
        else:
            # No pending card: nudge forward (small max_loops keeps it responsive in local dev).
            print(f"[step {step}] no card; chat nudge", flush=True)
            await client.chat(sid, "继续", attachments=[], max_loops=max_loops_per_call)

        if step_sleep_s:
            await asyncio.sleep(step_sleep_s)

    raise RuntimeError(f"timed out waiting for deliverable output_key={output_key} (session_id={sid}, matter_id={matter_id})")


async def _download_and_check_docx(
    *,
    client: ApiClient,
    file_id: str,
    out_dir: Path,
    filename_prefix: str,
    must_include: list[str],
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = await client.download_file_bytes(file_id)
    docx_path = out_dir / f"{filename_prefix}.docx"
    docx_path.write_bytes(raw)
    text = extract_docx_text(raw)
    assert_docx_has_no_template_placeholders(text)
    assert_docx_contains(text, must_include=must_include)
    (out_dir / f"{filename_prefix}.txt").write_text(text, encoding="utf-8")
    return docx_path


async def main() -> None:
    # Prefer repo root .env (docker-compose defaults) then allow e2e-tests/.env.
    load_dotenv(_REPO_ROOT / ".env", override=False)
    load_dotenv(_E2E_ROOT / ".env", override=False)

    base_url = os.getenv("BASE_URL", "http://localhost:18001").rstrip("/")
    user = os.getenv("LAWYER_USERNAME", "lawyer1")
    pwd = os.getenv("LAWYER_PASSWORD", "lawyer123456")

    evidence_zip = _REPO_ROOT / "evidence.zip"
    if not evidence_zip.exists():
        raise FileNotFoundError(f"missing evidence zip: {evidence_zip}")

    # Source texts are stored alongside the zip for human readability.
    evidence_dir = _REPO_ROOT / "tmp" / "chrome_dev_evidence"
    facts_txt = evidence_dir / "05_case_facts_and_claims.txt"
    opponent_complaint_txt = evidence_dir / "07_opponent_complaint_bus_injury.txt"
    defense_points_txt = evidence_dir / "08_defendant_defense_points.txt"

    # Keep the kickoff facts readable but deterministic: feed the full prepared text.
    plaintiff_facts = _read_text(facts_txt) if facts_txt.exists() else "起诉材料见附件。"
    defendant_facts = _read_text(opponent_complaint_txt) if opponent_complaint_txt.exists() else "对方起诉状见附件。"
    defense_points = _read_text(defense_points_txt) if defense_points_txt.exists() else ""

    out_dir = _REPO_ROOT / "tmp" / "chrome_dev_doc_outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    async with ApiClient(base_url) as c:
        await c.login(user, pwd)

        # ========== A) Plaintiff: civil complaint ==========
        prosecution = await _run_until_doc_ready(
            client=c,
            service_type_id="civil_prosecution",
            output_key="civil_complaint",
            evidence_zip=evidence_zip,
            overrides={
                "profile.facts": plaintiff_facts,
                "profile.claims": "判令被告赔偿医疗费、误工费、护理费、交通费等损失并承担诉讼费（金额以票据/计算为准）。",
                "profile.decisions.selected_documents": ["civil_complaint"],
                # If any attachment is flagged as needs_user_action, stop asking and proceed (dev-friendly).
                "data.files.preprocess_stop_ask": True,
            },
        )

        p_docx = await _download_and_check_docx(
            client=c,
            file_id=prosecution["file_id"],
            out_dir=out_dir,
            filename_prefix=f"{prosecution['matter_id']}_civil_complaint",
            must_include=[
                "民事起诉状",
                "张三",
                "北京某公交客运有限公司",
            ],
        )
        print(f"[ok] plaintiff docx: {p_docx}", flush=True)

        # ========== B) Defendant: defense statement ==========
        # Provide a short, stable defendant-side statement (plus attach the prepared defense points).
        defendant_facts_full = (defendant_facts + "\n\n" + defense_points).strip() if defense_points else defendant_facts

        defense = await _run_until_doc_ready(
            client=c,
            service_type_id="civil_defense",
            output_key="defense_statement",
            evidence_zip=evidence_zip,
            overrides={
                "profile.facts": defendant_facts_full,
                "profile.decisions.selected_documents": ["defense_statement"],
                "data.files.preprocess_stop_ask": True,
            },
        )

        d_docx = await _download_and_check_docx(
            client=c,
            file_id=defense["file_id"],
            out_dir=out_dir,
            filename_prefix=f"{defense['matter_id']}_defense_statement",
            must_include=[
                "答辩",
                "张三",
                "北京某公交客运有限公司",
            ],
        )
        print(f"[ok] defendant docx: {d_docx}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
