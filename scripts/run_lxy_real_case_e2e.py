"""Run a real-case E2E flow for civil_appeal_appellee using lxy.zip materials.

This helper is for local/UAT verification (not CI). It:
1) extracts key files from lxy.zip,
2) uploads them as consultation files,
3) runs the workbench flow to generate appeal_defense + phase summaries,
4) stores run artifacts under output/lxy_real_case_run/matter_<id>/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import sys
import httpx

E2E_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = E2E_ROOT.parent
sys.path.insert(0, str(E2E_ROOT))

from client.api_client import ApiClient
from tests.lawyer_workbench._support.docx import (
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow
from tests.lawyer_workbench._support.utils import unwrap_api_response

REQUIRED_KEYS = [
    "phase_summary__case_output",
    "phase_summary__judgment_output",
    "phase_summary__work_plan",
    "appeal_defense",
]


def _read_positive_int_env(name: str, default: int) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except Exception:
        return default
    return value if value > 0 else default


def _decode_zip_name(raw_name: str) -> str:
    try:
        return raw_name.encode("cp437").decode("utf-8")
    except Exception:
        return raw_name


def extract_key_materials(zip_path: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Prefer full, primary case materials to minimize "missing materials" cards:
    # - 正卷/证据卷/庭审笔录: core case record
    # - 上诉状: opponent appeal brief
    # - 答辩状: user's draft (treated as reference, not evidence)
    keywords = ("上诉状", "庭审笔录", "正卷", "证据卷", "答辩状")
    selected: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            raw_name = info.filename
            if raw_name.endswith("/") or raw_name.startswith("__MACOSX"):
                continue
            decoded = _decode_zip_name(raw_name)
            base = Path(decoded).name
            if not any(k in base for k in keywords):
                continue
            target = out_dir / base
            target.write_bytes(zf.read(info))
            selected.append(target)
    if not selected:
        raise RuntimeError(f"no key materials extracted from {zip_path}")
    return selected


async def first_deliverable(client: ApiClient, matter_id: str, output_key: str) -> dict[str, Any] | None:
    try:
        resp = await client.list_deliverables(matter_id, output_key=output_key, include_content=True)
    except httpx.HTTPStatusError as e:
        # Some environments return 404 when a matter has not produced any deliverables yet.
        if e.response is not None and e.response.status_code == 404:
            return None
        raise
    data = unwrap_api_response(resp)
    if not isinstance(data, dict):
        return None
    rows = data.get("deliverables") if isinstance(data.get("deliverables"), list) else []
    if not rows:
        return None
    first = rows[0]
    return first if isinstance(first, dict) else None


async def run_real_case(zip_path: Path, output_root: Path, *, reuse_file_ids: list[str] | None = None) -> Path:
    load_dotenv(REPO_ROOT / ".env", override=False)
    load_dotenv(E2E_ROOT / ".env", override=False)

    base_url = os.getenv("BASE_URL", "http://localhost:18001/api/v1").rstrip("/")
    user = os.getenv("LAWYER_USERNAME", "lawyer1")
    pwd = os.getenv("LAWYER_PASSWORD", "lawyer123456")
    cause_of_action_code = str(os.getenv("E2E_REAL_CASE_CAUSE_CODE", "contract_dispute") or "").strip() or None

    materials_dir = output_root / "materials"
    material_paths = extract_key_materials(zip_path, materials_dir)

    async with ApiClient(base_url) as client:
        await client.login(user, pwd)
        print(f"[login] ok user_id={client.user_id} org_id={client.organization_id}", flush=True)

        uploaded_file_ids: list[str] = []
        if reuse_file_ids:
            uploaded_file_ids = [str(x).strip() for x in reuse_file_ids if str(x).strip()]
            print(f"[upload] reuse existing file_ids={uploaded_file_ids}", flush=True)
        else:
            for p in material_paths:
                print(f"[upload] start {p.name} size={p.stat().st_size}", flush=True)
                up = await client.upload_file(str(p), purpose="consultation")
                data = (up.get("data") if isinstance(up, dict) else {}) or {}
                file_id = str(data.get("id") or "").strip()
                if not file_id:
                    raise RuntimeError(f"upload_file failed for {p}: {up}")
                uploaded_file_ids.append(file_id)
                print(
                    f"[upload] ok {p.name} file_id={file_id} parse_status={data.get('parse_status')}",
                    flush=True,
                )

        # Some non-prod environments can transiently return a matter ID that is not yet queryable
        # by consultations-service access checks; verify session readability immediately.
        session_id = ""
        matter_id = ""
        max_session_attempts = int(os.getenv("E2E_REAL_CASE_SESSION_ATTEMPTS", "6") or 6)
        last_session_error: Exception | None = None
        for attempt in range(1, max(1, max_session_attempts) + 1):
            matter_title = f"lxy-real-case-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{attempt}"
            matter = await client.create_matter(
                service_type_id="civil_appeal_appellee",
                client_role="appellee",
                cause_of_action_code=cause_of_action_code,
                title=matter_title,
            )
            matter_data = (matter.get("data") if isinstance(matter, dict) else {}) or {}
            seeded_matter_id = str(matter_data.get("id") or "").strip()
            if not seeded_matter_id:
                last_session_error = RuntimeError(f"create_matter failed: {matter}")
                continue
            print(f"[matter] seeded id={seeded_matter_id} title={matter_title}", flush=True)

            sess = await client.create_session(matter_id=seeded_matter_id)
            sess_data = (sess.get("data") if isinstance(sess, dict) else {}) or {}
            session_id = str(sess_data.get("id") or "").strip()
            matter_id = str(sess_data.get("matter_id") or "").strip()
            if not session_id:
                last_session_error = RuntimeError(f"create_session failed: {sess}")
                continue

            try:
                await client.get_session(session_id)
            except Exception as exc:  # noqa: BLE001 - keep retry tolerant for flaky env
                last_session_error = exc
                print(
                    f"[session] verify failed id={session_id} matter_id={matter_id or '-'} err={exc}",
                    flush=True,
                )
                session_id = ""
                matter_id = ""
                if attempt < max_session_attempts:
                    await asyncio.sleep(min(2.0, 0.4 * attempt))
                continue

            break

        if not session_id:
            raise RuntimeError(
                f"create_session could not produce a readable session after {max_session_attempts} attempts: {last_session_error}"
            )
        print(f"[session] id={session_id} matter_id={matter_id or '-'}", flush=True)

        flow = WorkbenchFlow(
            client=client,
            session_id=session_id,
            uploaded_file_ids=uploaded_file_ids,
            matter_id=matter_id or None,
            overrides={
                "service_type_id": "civil_appeal_appellee",
                "profile.service_type_id": "civil_appeal_appellee",
                "client_role": "appellee",
                "profile.client_role": "appellee",
                # reference-grounding requires a non-empty cause code to retrieve reviewable refs.
                "profile.cause_of_action_code": cause_of_action_code or "contract_dispute",
                "data.files.preprocess_stop_ask": True,
                "profile.facts": (
                    "上诉人：东莞奇力新公司；被上诉人：杨小英（原审被告）。"
                    "对方提起二审上诉，主张改判并要求我方承担违约责任及高额违约金。"
                    "请基于附件中的上诉状、一审材料、庭审笔录，完成二审争点分析、判决预测与答辩文书。"
                ),
                "profile.claims": "请求驳回上诉，维持原判；二审诉讼费用由上诉人承担。",
                "profile.appeal_grounds": "上诉人主张一审认定错误，请求撤销或改判。",
            },
        )

        kickoff = (
            "我方是被上诉人杨小英，对方上诉人为东莞奇力新公司。请基于真实附件材料输出："
            "1) 法律要素与关键事实时间线；"
            "2) 判决预测（最佳/最可能/最差+置信区间，明确是否维持原判）；"
            "3) 二审工作计划；"
            "4) 生成上诉答辩状。"
        )
        kickoff_max_loops = _read_positive_int_env("E2E_REAL_CASE_KICKOFF_MAX_LOOPS", 24)
        first = await flow.nudge(kickoff, attachments=uploaded_file_ids, max_loops=kickoff_max_loops)
        print(
            f"[kickoff] events={len(first.get('events') or [])} output_len={len(str(first.get('output') or ''))}",
            flush=True,
        )

        async def _appeal_defense_ready(f: WorkbenchFlow) -> bool:
            await f.refresh()
            if not f.matter_id:
                return False
            row = await first_deliverable(client, f.matter_id, "appeal_defense")
            return bool(row and str(row.get("file_id") or "").strip())

        appeal_wait_steps = _read_positive_int_env("E2E_REAL_CASE_APPEAL_MAX_STEPS", 360)
        await flow.run_until(
            _appeal_defense_ready,
            max_steps=appeal_wait_steps,
            description="appeal_defense ready",
        )
        await flow.refresh()
        matter_id = str(flow.matter_id or matter_id or "").strip()
        if not matter_id:
            raise RuntimeError("matter_id missing after flow run")
        print(f"[matter] id={matter_id}", flush=True)

        async def _summaries_ready(f: WorkbenchFlow) -> bool:
            await f.refresh()
            if not f.matter_id:
                return False
            for key in ("phase_summary__case_output", "phase_summary__judgment_output", "phase_summary__work_plan"):
                row = await first_deliverable(client, f.matter_id, key)
                if not row:
                    return False
                # Ensure content is persisted (include_content=true).
                content = row.get("content")
                if content is None:
                    return False
                if isinstance(content, str) and (not content.strip()):
                    return False
            return True

        summary_wait_steps = _read_positive_int_env("E2E_REAL_CASE_SUMMARY_MAX_STEPS", 240)
        await flow.run_until(
            _summaries_ready,
            max_steps=summary_wait_steps,
            description="phase summaries ready",
        )

        case_dir = output_root / f"matter_{matter_id}"
        case_dir.mkdir(parents=True, exist_ok=True)

        results: dict[str, Any] = {}
        for key in REQUIRED_KEYS:
            row = await first_deliverable(client, matter_id, key)
            if not row:
                print(f"[deliverable] {key} missing", flush=True)
                results[key] = {"exists": False}
                continue
            file_id = str(row.get("file_id") or "").strip()
            content_obj = row.get("content")
            summary_md = ""
            if isinstance(content_obj, dict):
                summary_md = str(content_obj.get("summary_markdown") or "").strip()
            elif isinstance(content_obj, str):
                summary_md = content_obj.strip()
            results[key] = {
                "exists": True,
                "id": row.get("id"),
                "file_id": file_id,
                "content_len": len(summary_md),
                "created_at": row.get("created_at"),
            }
            if summary_md:
                (case_dir / f"{key}.md").write_text(summary_md, encoding="utf-8")
            print(
                f"[deliverable] {key} id={row.get('id')} file_id={file_id or '-'} content_len={len(summary_md)}",
                flush=True,
            )

        appeal_meta = results.get("appeal_defense") or {}
        if appeal_meta.get("file_id"):
            raw = await client.download_file_bytes(str(appeal_meta["file_id"]))
            docx_path = case_dir / "appeal_defense.docx"
            docx_path.write_bytes(raw)
            docx_text = extract_docx_text(raw)
            assert_docx_has_no_template_placeholders(docx_text)
            (case_dir / "appeal_defense.txt").write_text(docx_text, encoding="utf-8")
            print(f"[docx] saved={docx_path} text_len={len(docx_text)}", flush=True)

        judgment_text = ""
        judgment_file = case_dir / "phase_summary__judgment_output.md"
        if judgment_file.exists():
            judgment_text = judgment_file.read_text(encoding="utf-8")

        checks = {
            "has_best": "最佳情况" in judgment_text,
            "has_likely": "最可能结果" in judgment_text,
            "has_worst": "最差情况" in judgment_text,
            "has_confidence": "置信区间" in judgment_text,
            "has_maintain_original": "维持原判" in judgment_text,
        }
        print(f"[judgment_checks] {json.dumps(checks, ensure_ascii=False)}", flush=True)

        meta = {
            "zip_path": str(zip_path),
            "session_id": session_id,
            "matter_id": matter_id,
            "uploaded_file_ids": uploaded_file_ids,
            "deliverables": results,
            "judgment_checks": checks,
        }
        meta_path = case_dir / "run_meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[done] {meta_path}", flush=True)
        return meta_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run lxy real-case E2E flow.")
    parser.add_argument(
        "--zip",
        dest="zip_path",
        default=str(REPO_ROOT / "lxy.zip"),
        help="Path to lxy.zip",
    )
    parser.add_argument(
        "--output-dir",
        dest="output_dir",
        default=str(REPO_ROOT / "output" / "lxy_real_case_run"),
        help="Directory to store run artifacts",
    )
    parser.add_argument(
        "--reuse-file-ids",
        dest="reuse_file_ids",
        default="",
        help="Comma-separated existing file IDs to reuse (skip uploading).",
    )
    return parser.parse_args()


async def _async_main() -> None:
    args = parse_args()
    zip_path = Path(args.zip_path).resolve()
    out_dir = Path(args.output_dir).resolve()
    if not zip_path.exists():
        raise FileNotFoundError(zip_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    reuse_file_ids = [x.strip() for x in str(args.reuse_file_ids or "").split(",") if x.strip()]
    await run_real_case(zip_path=zip_path, output_root=out_dir, reuse_file_ids=reuse_file_ids or None)


if __name__ == "__main__":
    asyncio.run(_async_main())
