"""Run real contract-review workflow via consultations-service WebSocket (no mock LLM)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import cast

import sys

E2E_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = E2E_ROOT.parent
sys.path.insert(0, str(E2E_ROOT))

from client.api_client import ApiClient
from support.workbench.docx import (
    assert_docx_has_no_template_placeholders,
    extract_docx_text,
)
from support.workbench.flow_runner import WorkbenchFlow
from scripts._support.workflow_real_flow_support import (
    bootstrap_flow,
    event_counts as _shared_event_counts,
    fetch_workbench_snapshot as _shared_fetch_workbench_snapshot,
    list_deliverables as _shared_list_deliverables,
    list_session_messages as _shared_list_session_messages,
    load_real_flow_env,
    resolve_output_dir,
    upload_consultation_files,
    write_json,
)
from scripts._support.flow_score_support import build_flow_scores, collect_flow_observability


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
    "profile.summary": "合同审查，重点关注付款条件、违约责任、争议解决与免责条款。",
}


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _select_contract_file(cli_value: str, *, contract_type_id: str) -> Path:
    if _safe_str(cli_value):
        p = Path(cli_value).expanduser().resolve()
        if not p.exists() or not p.is_file():
            raise FileNotFoundError(f"contract file not found: {p}")
        return p

    candidates = [
        E2E_ROOT / f"fixtures/workbench/contract_review/{_safe_str(contract_type_id).lower()}.txt",
        REPO_ROOT / "已征收闲置土地垃圾清运.docx",
        E2E_ROOT / "fixtures/workbench/contract_review/sample_contract.txt",
    ]
    for p in candidates:
        if p.exists() and p.is_file():
            return p
    raise FileNotFoundError("未找到可用合同文件，请通过 --contract-file 显式指定。")


def _load_contract_review_expectations(contract_file: Path) -> dict[str, Any]:
    expectation_candidates = [
        contract_file.with_suffix(".expectation.json"),
        contract_file.parent / f"{contract_file.stem}.expectation.json",
    ]
    for path in expectation_candidates:
        if not path.exists() or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"invalid_expectation_json:{path}:{exc}") from exc
        if isinstance(payload, dict):
            return payload
    return {
        "contract_type_id": "construction",
        "review_scope": "full",
        "required_output_keys": list(REQUIRED_DOC_OUTPUT_KEYS),
        "mandatory_issue_types": [
            "effectiveness",
            "payment",
            "tax_invoice",
            "delivery_acceptance",
            "quality",
            "change_order",
            "delay",
            "liability",
            "indemnity",
            "termination",
            "compliance",
            "dispute_resolution",
            "notice",
        ],
        "required_section_markers": [
            "合同审查意见书",
            "审查范围",
            "主要问题及修改建议",
            "声明与保留",
        ],
    }


def _event_counts(sse: dict[str, Any]) -> dict[str, int]:
    return _shared_event_counts(sse)


def _capture_runtime_images() -> dict[str, str]:
    kubeconfig = _safe_str(os.getenv("KUBECONFIG")) or _safe_str(os.getenv("HOME")) + "/.kube/config-lawseekdog"
    if not kubeconfig or not Path(kubeconfig).exists():
        return {}
    cmd = [
        "kubectl",
        "get",
        "deploy",
        "-n",
        "lawseekdog",
        "ai-engine",
        "consultations-service",
        "matter-service",
        "templates-service",
        "-o",
        "jsonpath={range .items[*]}{.metadata.name}{\"=\"}{.spec.template.spec.containers[0].image}{\"\\n\"}{end}",
    ]
    env = dict(os.environ)
    env["KUBECONFIG"] = kubeconfig
    try:
        raw = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, env=env, timeout=10)
    except Exception:
        return {}
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        name, image = line.split("=", 1)
        name = _safe_str(name)
        image = _safe_str(image)
        if name and image:
            out[name] = image
    return out


async def _fetch_snapshot(client: ApiClient, matter_id: str) -> dict[str, Any] | None:
    return await _shared_fetch_workbench_snapshot(client, matter_id)


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
    return await _shared_list_deliverables(client, matter_id)


async def _list_session_messages(client: ApiClient, session_id: str) -> list[dict[str, Any]]:
    return await _shared_list_session_messages(client, session_id)


def _latest_assistant_message(rows: list[dict[str, Any]]) -> str:
    for row in reversed(rows):
        if _safe_str(row.get("role")).lower() != "assistant":
            continue
        text = _safe_str(row.get("content"))
        if text:
            return text
    return ""


async def run(args: argparse.Namespace) -> int:
    load_real_flow_env(repo_root=REPO_ROOT, e2e_root=E2E_ROOT)

    base_url = _safe_str(args.base_url) or _safe_str(os.getenv("BASE_URL")) or "http://localhost:18001/api/v1"
    username = _safe_str(args.username) or _safe_str(os.getenv("LAWYER_USERNAME")) or "lawyer1"
    password = _safe_str(args.password) or _safe_str(os.getenv("LAWYER_PASSWORD")) or "lawyer123456"
    kickoff = _safe_str(args.kickoff) or DEFAULT_KICKOFF
    requested_contract_type_id = _safe_str(args.contract_type_id).lower() or "construction"
    contract_file = _select_contract_file(args.contract_file, contract_type_id=requested_contract_type_id)
    contract_review_expectations = _load_contract_review_expectations(contract_file)
    contract_type_id = _safe_str(contract_review_expectations.get("contract_type_id")).lower() or requested_contract_type_id
    review_scope = _safe_str(contract_review_expectations.get("review_scope")).lower() or "full"
    flow_overrides = {
        **FLOW_OVERRIDES,
        "profile.review_scope": review_scope,
        "review_scope": review_scope,
        "profile.contract_type_id": contract_type_id,
        "profile.summary": f"{contract_type_id} 合同审查，重点关注付款条件、违约责任、争议解决与免责条款。",
    }

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = resolve_output_dir(
        repo_root=REPO_ROOT,
        output_dir=_safe_str(args.output_dir),
        default_leaf=f"output/contract-review-chain/{ts}",
    )

    print(f"[config] base_url={base_url}")
    print(f"[config] user={username}")
    print(f"[config] contract_file={contract_file}")
    print(f"[config] output_dir={out_dir}")
    start_images = _capture_runtime_images()
    if start_images:
        (out_dir / "runtime_images.start.json").write_text(json.dumps(start_images, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[runtime] start_images={start_images}")

    async with ApiClient(base_url) as client:
        await client.login(username, password)
        print(f"[login] ok user_id={client.user_id} org_id={client.organization_id}")

        uploaded_file_ids = await upload_consultation_files(client, [contract_file])
        file_id = uploaded_file_ids[0] if uploaded_file_ids else ""
        if not file_id:
            raise RuntimeError(f"upload_file failed: {contract_file}")
        print(f"[upload] ok file_id={file_id}")

        flow, session_id, matter_id = await bootstrap_flow(
            client=client,
            service_type_id="contract_review",
            client_role="applicant",
            uploaded_file_ids=[file_id],
            overrides=flow_overrides,
            strict_card_driven=True,
        )
        print(f"[session] id={session_id} matter_id={matter_id or '-'}")

        kickoff_sse = await flow.nudge(kickoff, attachments=[file_id], max_loops=max(1, int(args.kickoff_max_loops)))
        kickoff_counts = _event_counts(kickoff_sse if isinstance(kickoff_sse, dict) else {})
        print(f"[kickoff] event_counts={kickoff_counts}")
        write_json(out_dir / "kickoff.sse.json", kickoff_sse if isinstance(kickoff_sse, dict) else {})

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

        try:
            await flow.run_until(
                _deliverables_ready,
                max_steps=max(1, int(args.max_steps)),
                description="contract review deliverables ready",
                allow_nudge=bool(args.allow_nudge),
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
            write_json(out_dir / "failure_diagnostics.json", failure_diag)
            write_json(out_dir / "deliverables.failure.json", fail_deliverables)
            if isinstance(fail_snapshot, dict) and fail_snapshot:
                write_json(out_dir / "snapshot.failure.json", fail_snapshot)
            raise

        await flow.refresh()
        final_matter_id = _safe_str(flow.matter_id)
        if not final_matter_id:
            raise RuntimeError("matter_id missing after workflow run")

        deliverables = await _list_deliverables(client, final_matter_id)
        snapshot = await _fetch_snapshot(client, final_matter_id) or {}
        contract_view = _extract_contract_view(snapshot)
        pending_card = await flow.get_pending_card()

        report_file_id = _safe_str((deliverables.get("contract_review_report") or {}).get("file_id"))
        report_text = ""
        if report_file_id:
            raw = await client.download_file_bytes(report_file_id)
            report_text = extract_docx_text(raw)
            if args.assert_docx:
                assert_docx_has_no_template_placeholders(report_text)

        end_images = _capture_runtime_images()
        if end_images:
            (out_dir / "runtime_images.end.json").write_text(json.dumps(end_images, ensure_ascii=False, indent=2), encoding="utf-8")

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
            "pending_card": {
                "skill_id": _safe_str((pending_card or {}).get("skill_id")),
                "task_key": _safe_str((pending_card or {}).get("task_key")),
                "review_type": _safe_str((pending_card or {}).get("review_type")),
            },
            "contract_view": {
                "overall_risk_level": _safe_str(contract_view.get("overall_risk_level")),
                "contract_type_id": _safe_str(contract_view.get("contract_type_id")),
                "summary_len": len(_safe_str(contract_view.get("summary"))),
                "clauses_count": len(contract_view.get("clauses")) if isinstance(contract_view.get("clauses"), list) else 0,
            },
            "seen_cards": len(flow.seen_cards),
            "seen_sse_rounds": len(flow.seen_sse),
            "runtime_images_start": start_images,
            "runtime_images_end": end_images,
            "runtime_images_stable": (start_images == end_images) if start_images and end_images else None,
        }
        observability = await collect_flow_observability(client, matter_id=final_matter_id, session_id=session_id)
        flow_scores = build_flow_scores(
            flow_id="contract_review",
            seen_cards=flow.seen_cards,
            pending_card=pending_card,
            snapshot=snapshot,
            current_view=contract_view,
            aux_views={},
            deliverables=deliverables,
            deliverable_text=report_text,
            deliverable_status=_safe_str((deliverables.get("contract_review_report") or {}).get("status")),
            gold_text=_safe_str(contract_review_expectations.get("gold_text")),
            contract_review_expectations=cast(dict[str, Any], contract_review_expectations),
            observability=observability,
            goal_completion_mode="card" if _safe_str((pending_card or {}).get("skill_id")).lower() == "goal-completion" else "none",
        )
        summary["flow_scores"] = flow_scores

        if start_images and end_images and start_images != end_images and str(os.getenv("E2E_ALLOW_DEPLOYMENT_DRIFT", "") or "").strip() not in {"1", "true", "yes"}:
            raise RuntimeError(f"deployment_image_drift_detected: start={start_images} end={end_images}")

        write_json(out_dir / "summary.json", summary)
        write_json(out_dir / "flow_scores.json", flow_scores)
        write_json(out_dir / "deliverables.json", deliverables)
        write_json(out_dir / "snapshot.json", snapshot)
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
    parser.add_argument("--contract-type-id", default="construction", help="Default fixture contract_type_id when --contract-file is omitted")
    parser.add_argument("--kickoff", default=DEFAULT_KICKOFF, help="Initial user query")
    parser.add_argument("--kickoff-max-loops", type=int, default=24, help="kickoff max_loops")
    parser.add_argument("--max-steps", type=int, default=220, help="run_until max steps")
    parser.add_argument("--cards-only", action="store_true", default=False, help="Kickoff once, then only poll and answer cards")
    parser.add_argument("--allow-nudge", dest="allow_nudge", action="store_true", default=False, help=argparse.SUPPRESS)
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
