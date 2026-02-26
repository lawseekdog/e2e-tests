"""Run real contract-review workflow via consultations-service WebSocket (no mock LLM)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import sys

E2E_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = E2E_ROOT.parent
sys.path.insert(0, str(E2E_ROOT))

from client.api_client import ApiClient
from tests.lawyer_workbench._support.docx import (
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from tests.lawyer_workbench._support.flow_runner import WorkbenchFlow, is_session_busy_sse
from tests.lawyer_workbench._support.utils import unwrap_api_response


REQUIRED_DOC_OUTPUT_KEYS = (
    "contract_review_report",
    "modification_suggestion",
    "redline_comparison",
)
SUMMARY_OUTPUT_KEYS = (
    "phase_summary__contract_output",
    "phase_summary__contract_analyze",
)

DEFAULT_KICKOFF = (
    "请审查已上传合同并输出结构化结论：整体风险等级、合同类型、审查摘要、风险条款清单。"
    "重点关注违约责任、争议解决、免责条款与付款条件。"
)

FLOW_OVERRIDES = {
    "profile.client_role": "applicant",
    "profile.review_scope": "full",
}


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _select_contract_file(cli_value: str) -> Path:
    if _safe_str(cli_value):
        p = Path(cli_value).expanduser().resolve()
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"contract file not found: {p}")
        return p

    candidates = [
        REPO_ROOT / "已征收闲置土地垃圾清运.docx",
        E2E_ROOT / "tests/lawyer_workbench/contract_review/evidence/sample_contract.txt",
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    raise FileNotFoundError("未找到可用合同文件，请通过 --contract-file 显式指定。")


def _event_counts(sse: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    events = sse.get("events") if isinstance(sse.get("events"), list) else []
    for row in events:
        if not isinstance(row, dict):
            continue
        name = _safe_str(row.get("event")) or "unknown"
        out[name] = int(out.get(name) or 0) + 1
    return out


async def _fetch_snapshot(client: ApiClient, matter_id: str) -> dict[str, Any] | None:
    try:
        resp = await client.get(f"/matter-service/lawyer/matters/{matter_id}/workbench/snapshot")
    except Exception:
        return None
    payload = unwrap_api_response(resp)
    return payload if isinstance(payload, dict) else None


def _extract_contract_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    analysis = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    view = analysis.get("contract_review_view") if isinstance(analysis.get("contract_review_view"), dict) else {}
    if view:
        return view
    goals = analysis.get("goal_views") if isinstance(analysis.get("goal_views"), dict) else {}
    fallback = goals.get("contract_review_view") if isinstance(goals.get("contract_review_view"), dict) else {}
    return fallback if isinstance(fallback, dict) else {}


async def _list_deliverables(client: ApiClient, matter_id: str) -> dict[str, dict[str, Any]]:
    try:
        resp = await client.list_deliverables(matter_id)
    except Exception:
        return {}
    data = unwrap_api_response(resp)
    rows = data.get("deliverables") if isinstance(data, dict) and isinstance(data.get("deliverables"), list) else []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = _safe_str(row.get("output_key"))
        if key:
            out[key] = row
    return out


async def _list_session_messages(client: ApiClient, session_id: str) -> list[dict[str, Any]]:
    try:
        resp = await client.get(
            f"/consultations-service/consultations/sessions/{session_id}/messages",
            params={"page": 1, "size": 200},
        )
    except Exception:
        return []
    data = unwrap_api_response(resp)
    rows = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def _latest_assistant_message(rows: list[dict[str, Any]]) -> str:
    for row in reversed(rows):
        if _safe_str(row.get("role")).lower() != "assistant":
            continue
        text = _safe_str(row.get("content"))
        if text:
            return text
    return ""


async def _try_apply_clause_decisions(
    *,
    client: ApiClient,
    flow: WorkbenchFlow,
    matter_id: str,
) -> bool:
    snapshot = await _fetch_snapshot(client, matter_id)
    view = _extract_contract_view(snapshot)
    clauses = view.get("clauses") if isinstance(view.get("clauses"), list) else []
    clause_ids = [
        _safe_str(row.get("clause_id"))
        for row in clauses
        if isinstance(row, dict) and _safe_str(row.get("clause_id"))
    ]
    accepted = clause_ids[: min(len(clause_ids), 8)]
    if not accepted:
        return False

    sse = await client.workflow_action(
        flow.session_id,
        workflow_action="contract_review_apply_decisions",
        workflow_action_params={
            "accepted_clause_ids": accepted,
            "ignored_clause_ids": [],
            "overrides": {},
            "regenerate_documents": True,
        },
        max_loops=36,
    )
    if isinstance(sse, dict):
        flow.last_sse = sse
        flow.seen_sse.append(sse)
    if is_session_busy_sse(sse if isinstance(sse, dict) else {}):
        return False
    return True


async def run(args: argparse.Namespace) -> int:
    load_dotenv(REPO_ROOT / ".env", override=False)
    load_dotenv(E2E_ROOT / ".env", override=False)

    base_url = _safe_str(args.base_url) or _safe_str(os.getenv("BASE_URL")) or "http://localhost:18001/api/v1"
    username = _safe_str(args.username) or _safe_str(os.getenv("LAWYER_USERNAME")) or "lawyer1"
    password = _safe_str(args.password) or _safe_str(os.getenv("LAWYER_PASSWORD")) or "lawyer123456"
    kickoff = _safe_str(args.kickoff) or DEFAULT_KICKOFF
    contract_file = _select_contract_file(args.contract_file)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = (Path(args.output_dir).expanduser() if _safe_str(args.output_dir) else REPO_ROOT / f"output/contract-review-chain/{ts}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[config] base_url={base_url}")
    print(f"[config] user={username}")
    print(f"[config] contract_file={contract_file}")
    print(f"[config] output_dir={out_dir}")

    async with ApiClient(base_url) as client:
        await client.login(username, password)
        print(f"[login] ok user_id={client.user_id} org_id={client.organization_id}")

        upload = await client.upload_file(str(contract_file), purpose="consultation")
        file_id = _safe_str(((upload.get("data") or {}) if isinstance(upload, dict) else {}).get("id"))
        if not file_id:
            raise RuntimeError(f"upload_file failed: {upload}")
        print(f"[upload] ok file_id={file_id}")

        sess = await client.create_session(service_type_id="contract_review", client_role="applicant")
        sess_data = (sess.get("data") if isinstance(sess, dict) else {}) or {}
        session_id = _safe_str(sess_data.get("id"))
        matter_id = _safe_str(sess_data.get("matter_id"))
        if not session_id:
            raise RuntimeError(f"create_session failed: {sess}")
        print(f"[session] id={session_id} matter_id={matter_id or '-'}")

        flow = WorkbenchFlow(
            client=client,
            session_id=session_id,
            uploaded_file_ids=[file_id],
            overrides=dict(FLOW_OVERRIDES),
            matter_id=matter_id or None,
        )

        kickoff_sse = await flow.nudge(kickoff, attachments=[file_id], max_loops=max(1, int(args.kickoff_max_loops)))
        kickoff_counts = _event_counts(kickoff_sse if isinstance(kickoff_sse, dict) else {})
        print(f"[kickoff] event_counts={kickoff_counts}")
        (out_dir / "kickoff.sse.json").write_text(
            json.dumps(kickoff_sse if isinstance(kickoff_sse, dict) else {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        async def _deliverables_ready(f: WorkbenchFlow) -> bool:
            await f.refresh()
            mid = _safe_str(f.matter_id)
            if not mid:
                return False
            by_key = await _list_deliverables(client, mid)
            if not all(key in by_key for key in REQUIRED_DOC_OUTPUT_KEYS):
                return False
            if not any(key in by_key for key in SUMMARY_OUTPUT_KEYS):
                return False
            for key in ("contract_review_report", "modification_suggestion", "redline_comparison"):
                file_ref = _safe_str((by_key.get(key) or {}).get("file_id"))
                if not file_ref:
                    return False
            return True

        if args.apply_decisions:
            await flow.refresh()
            mid = _safe_str(flow.matter_id)
            if mid:
                applied = await _try_apply_clause_decisions(client=client, flow=flow, matter_id=mid)
                print(f"[workflow_action] contract_review_apply_decisions applied={applied}")

        try:
            await flow.run_until(
                _deliverables_ready,
                max_steps=max(1, int(args.max_steps)),
                description="contract review deliverables ready",
            )
        except Exception as e:
            await flow.refresh()
            fail_matter_id = _safe_str(flow.matter_id) or matter_id
            fail_snapshot = await _fetch_snapshot(client, fail_matter_id) if fail_matter_id else {}
            fail_deliverables = await _list_deliverables(client, fail_matter_id) if fail_matter_id else {}
            fail_messages = await _list_session_messages(client, session_id)
            fail_contract_view = _extract_contract_view(fail_snapshot if isinstance(fail_snapshot, dict) else {})
            fail_analysis = (
                fail_snapshot.get("analysis_state")
                if isinstance(fail_snapshot, dict) and isinstance(fail_snapshot.get("analysis_state"), dict)
                else {}
            )

            failure_diag = {
                "error": str(e),
                "base_url": base_url,
                "session_id": session_id,
                "matter_id": fail_matter_id,
                "uploaded_file_id": file_id,
                "kickoff_event_counts": kickoff_counts,
                "deliverable_keys": sorted(fail_deliverables.keys()),
                "analysis_state_keys": sorted(fail_analysis.keys()) if isinstance(fail_analysis, dict) else [],
                "contract_view_keys": sorted(fail_contract_view.keys()) if isinstance(fail_contract_view, dict) else [],
                "latest_assistant_message": _latest_assistant_message(fail_messages),
                "messages_tail": fail_messages[-20:],
                "seen_cards": len(flow.seen_cards),
                "seen_sse_rounds": len(flow.seen_sse),
            }
            (out_dir / "failure_diagnostics.json").write_text(
                json.dumps(failure_diag, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (out_dir / "deliverables.failure.json").write_text(
                json.dumps(fail_deliverables, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if isinstance(fail_snapshot, dict) and fail_snapshot:
                (out_dir / "snapshot.failure.json").write_text(
                    json.dumps(fail_snapshot, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            raise

        await flow.refresh()
        final_matter_id = _safe_str(flow.matter_id)
        if not final_matter_id:
            raise RuntimeError("matter_id missing after workflow run")

        deliverables = await _list_deliverables(client, final_matter_id)
        snapshot = await _fetch_snapshot(client, final_matter_id) or {}
        contract_view = _extract_contract_view(snapshot)

        report_file_id = _safe_str((deliverables.get("contract_review_report") or {}).get("file_id"))
        report_text = ""
        if report_file_id:
            raw = await client.download_file_bytes(report_file_id)
            report_text = extract_docx_text(raw)
            if args.assert_docx:
                assert_docx_has_no_template_placeholders(report_text)

        summary = {
            "base_url": base_url,
            "session_id": session_id,
            "matter_id": final_matter_id,
            "uploaded_file_id": file_id,
            "kickoff_event_counts": kickoff_counts,
            "deliverable_keys": sorted(deliverables.keys()),
            "summary_output_key": next(
                (key for key in SUMMARY_OUTPUT_KEYS if key in deliverables),
                "",
            ),
            "report_file_id": report_file_id,
            "contract_view": {
                "overall_risk_level": _safe_str(contract_view.get("overall_risk_level")),
                "contract_type": _safe_str(contract_view.get("contract_type")),
                "summary_len": len(_safe_str(contract_view.get("summary"))),
                "clauses_count": len(contract_view.get("clauses")) if isinstance(contract_view.get("clauses"), list) else 0,
            },
            "seen_cards": len(flow.seen_cards),
            "seen_sse_rounds": len(flow.seen_sse),
        }

        (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "deliverables.json").write_text(json.dumps(deliverables, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        if report_text:
            (out_dir / "contract_review_report.txt").write_text(report_text, encoding="utf-8")

    print("[done] contract review workflow completed")
    print(f"[artifacts] {out_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run contract review workflow via consultations WS (real LLM).")
    parser.add_argument("--base-url", default="", help="Gateway base URL, e.g. http://host/api/v1")
    parser.add_argument("--username", default="", help="Lawyer username")
    parser.add_argument("--password", default="", help="Lawyer password")
    parser.add_argument("--contract-file", default="", help="Contract file path (.docx/.txt)")
    parser.add_argument("--kickoff", default=DEFAULT_KICKOFF, help="Initial user query")
    parser.add_argument("--kickoff-max-loops", type=int, default=24, help="kickoff max_loops")
    parser.add_argument("--max-steps", type=int, default=220, help="run_until max steps")
    parser.add_argument(
        "--apply-decisions",
        action="store_true",
        default=True,
        help="Send workflow_action=contract_review_apply_decisions before deliverables polling",
    )
    parser.add_argument(
        "--no-apply-decisions",
        dest="apply_decisions",
        action="store_false",
        help="Disable workflow_action=contract_review_apply_decisions",
    )
    parser.add_argument(
        "--assert-docx",
        action="store_true",
        default=True,
        help="Assert generated DOCX has no template placeholders",
    )
    parser.add_argument(
        "--no-assert-docx",
        dest="assert_docx",
        action="store_false",
        help="Skip DOCX placeholder assertion",
    )
    parser.add_argument("--output-dir", default="", help="Artifacts output directory")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("[abort] interrupted by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
