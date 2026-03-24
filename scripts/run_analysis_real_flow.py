"""Run real analysis workflow via consultations-service WebSocket (no mock LLM)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import sys

E2E_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = E2E_ROOT.parent
sys.path.insert(0, str(E2E_ROOT))

from client.api_client import ApiClient
from scripts._support.workflow_real_flow_support import (
    bootstrap_flow,
    event_counts,
    fetch_workbench_snapshot,
    is_goal_completion_card,
    list_session_messages,
    load_real_flow_env,
    resolve_output_dir,
    safe_str as _safe_str,
    upload_consultation_files,
    write_json,
)
from scripts._support.flow_score_support import build_flow_scores, collect_flow_observability


DEFAULT_KICKOFF = (
    "我准备起诉一起民间借贷纠纷，请基于已上传材料完成案件分析，输出争点、风险、策略建议与下一步动作。"
)

FLOW_OVERRIDES = {
    "profile.client_role": "plaintiff",
    "client_role": "plaintiff",
    "profile.service_type_id": "civil_prosecution",
    "profile.summary": "原告主张被告民间借贷到期不还，请求返还本金并支付逾期利息。",
    "profile.facts": (
        "被告于2024年3月向原告借款，原告已转账交付借款。双方签有借条，约定还款期限届满后被告仍未清偿。"
    ),
    "profile.background": (
        "原告多次微信和电话催收，被告承认借款事实但一直拖延还款，现拟提起民事诉讼。"
    ),
    "profile.plaintiff": "陈某（出借人）",
    "profile.defendant": "周某（借款人）",
    "profile.claims": "1.返还借款本金；2.支付逾期利息；3.诉讼费由被告承担。",
    "profile.legal_issue": "借贷关系成立、本金返还、逾期利息支持。",
}

def _extract_analysis_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    analysis = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    direct = analysis.get("analysis_view") if isinstance(analysis.get("analysis_view"), dict) else {}
    if direct:
        return direct
    goals = analysis.get("goal_views") if isinstance(analysis.get("goal_views"), dict) else {}
    view = goals.get("analysis_view") if isinstance(goals.get("analysis_view"), dict) else {}
    if view:
        return view
    goal_views = snapshot.get("goal_views") if isinstance(snapshot.get("goal_views"), dict) else {}
    view = goal_views.get("analysis_view") if isinstance(goal_views.get("analysis_view"), dict) else {}
    return view if isinstance(view, dict) else {}


def _extract_pricing_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    analysis = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    goals = analysis.get("goal_views") if isinstance(analysis.get("goal_views"), dict) else {}
    view = goals.get("pricing_plan_view") if isinstance(goals.get("pricing_plan_view"), dict) else {}
    if view:
        return view
    goal_views = snapshot.get("goal_views") if isinstance(snapshot.get("goal_views"), dict) else {}
    view = goal_views.get("pricing_plan_view") if isinstance(goal_views.get("pricing_plan_view"), dict) else {}
    return view if isinstance(view, dict) else {}

async def run(args: argparse.Namespace) -> int:
    load_real_flow_env(repo_root=REPO_ROOT, e2e_root=E2E_ROOT)

    base_url = _safe_str(args.base_url) or _safe_str(os.getenv("BASE_URL")) or "http://localhost:18001/api/v1"
    username = _safe_str(args.username) or _safe_str(os.getenv("LAWYER_USERNAME")) or "lawyer1"
    password = _safe_str(args.password) or _safe_str(os.getenv("LAWYER_PASSWORD")) or "lawyer123456"
    kickoff = _safe_str(args.kickoff) or DEFAULT_KICKOFF

    fixture_dir = E2E_ROOT / "fixtures"
    evidence_files = [
        fixture_dir / "sample_iou.pdf",
        fixture_dir / "sample_chat_record.txt",
        fixture_dir / "sample_transfer_record.txt",
    ]

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = resolve_output_dir(
        repo_root=REPO_ROOT,
        output_dir=_safe_str(args.output_dir),
        default_leaf=f"output/analysis-chain/{ts}",
    )

    print(f"[config] base_url={base_url}")
    print(f"[config] user={username}")
    print(f"[config] output_dir={out_dir}")

    async with ApiClient(base_url) as client:
        await client.login(username, password)
        print(f"[login] ok user_id={client.user_id} org_id={client.organization_id}")

        uploaded_file_ids = await upload_consultation_files(client, evidence_files)
        for path, fid in zip([p for p in evidence_files if p.exists()], uploaded_file_ids):
            print(f"[upload] ok file={path.name} file_id={fid}")

        flow, session_id, matter_id = await bootstrap_flow(
            client=client,
            service_type_id="civil_prosecution",
            client_role="plaintiff",
            uploaded_file_ids=uploaded_file_ids,
            overrides=FLOW_OVERRIDES,
        )
        print(f"[session] id={session_id} matter_id={matter_id or '-'}")

        kickoff_sse = await flow.nudge(kickoff, attachments=uploaded_file_ids, max_loops=max(1, int(args.kickoff_max_loops)))
        kickoff_counts = event_counts(kickoff_sse if isinstance(kickoff_sse, dict) else {})
        write_json(out_dir / "kickoff.sse.json", kickoff_sse if isinstance(kickoff_sse, dict) else {})

        async def _analysis_ready(f: WorkbenchFlow) -> bool:
            await f.refresh()
            if not f.matter_id:
                return False
            pending = await f.get_pending_card()
            if is_goal_completion_card(pending):
                return True
            snapshot = await fetch_workbench_snapshot(client, f.matter_id)
            analysis_view = _extract_analysis_view(snapshot)
            pricing_view = _extract_pricing_view(snapshot)
            return bool(
                _safe_str(analysis_view.get("summary"))
                and isinstance(analysis_view.get("issues"), list)
                and analysis_view.get("issues")
                and isinstance(analysis_view.get("strategy_options"), list)
                and analysis_view.get("strategy_options")
                and _safe_str(pricing_view.get("status")).lower() in {"ready", "review_pending"}
            )

        try:
            await flow.run_until(
                _analysis_ready,
                max_steps=max(1, int(args.max_steps)),
                description="analysis view + pricing plan ready",
                allow_nudge=bool(args.allow_nudge),
            )
        except Exception as exc:
            await flow.refresh()
            fail_snapshot = await fetch_workbench_snapshot(client, _safe_str(flow.matter_id)) if flow.matter_id else {}
            fail_messages = await list_session_messages(client, session_id)
            write_json(
                out_dir / "failure_diagnostics.json",
                {
                    "error": str(exc),
                    "session_id": session_id,
                    "matter_id": _safe_str(flow.matter_id),
                    "seen_cards": len(flow.seen_cards),
                    "seen_sse_rounds": len(flow.seen_sse),
                    "latest_assistant_message": next(
                        (
                            _safe_str(row.get("content"))
                            for row in reversed(fail_messages)
                            if _safe_str(row.get("role")).lower() == "assistant" and _safe_str(row.get("content"))
                        ),
                        "",
                    ),
                },
            )
            if isinstance(fail_snapshot, dict) and fail_snapshot:
                write_json(out_dir / "snapshot.failure.json", fail_snapshot)
            raise

        await flow.refresh()
        final_matter_id = _safe_str(flow.matter_id)
        if not final_matter_id:
            raise RuntimeError("matter_id missing after workflow run")

        snapshot = await fetch_workbench_snapshot(client, final_matter_id) or {}
        analysis_view = _extract_analysis_view(snapshot)
        pricing_view = _extract_pricing_view(snapshot)
        risk_assessment = analysis_view.get("risk_assessment") if isinstance(analysis_view.get("risk_assessment"), dict) else {}
        key_risks = risk_assessment.get("key_risks") if isinstance(risk_assessment.get("key_risks"), list) else []
        pending_card = await flow.get_pending_card()
        messages = await list_session_messages(client, session_id)
        observability = await collect_flow_observability(client, matter_id=final_matter_id, session_id=session_id)
        flow_scores = build_flow_scores(
            flow_id="analysis",
            seen_cards=flow.seen_cards,
            pending_card=pending_card,
            snapshot=snapshot,
            current_view=analysis_view,
            aux_views={"pricing_view": pricing_view},
            deliverables={},
            deliverable_text="",
            deliverable_status=_safe_str(pricing_view.get("status")),
            observability=observability,
            goal_completion_mode="card" if is_goal_completion_card(pending_card) else "none",
        )

        summary = {
            "base_url": base_url,
            "session_id": session_id,
            "matter_id": final_matter_id,
            "uploaded_file_ids": uploaded_file_ids,
            "kickoff_event_counts": kickoff_counts,
            "analysis_view": {
                "summary_len": len(_safe_str(analysis_view.get("summary"))),
                "issues_count": len(analysis_view.get("issues")) if isinstance(analysis_view.get("issues"), list) else 0,
                "strategy_options_count": len(analysis_view.get("strategy_options")) if isinstance(analysis_view.get("strategy_options"), list) else 0,
                "risk_count": len(key_risks),
            },
            "pricing_plan_view": {
                "status": _safe_str(pricing_view.get("status")),
                "reviewed": bool(pricing_view.get("reviewed")),
                "pricing_mode": _safe_str(pricing_view.get("pricing_mode")),
            },
            "pending_card": {
                "skill_id": _safe_str((pending_card or {}).get("skill_id")),
                "task_key": _safe_str((pending_card or {}).get("task_key")),
                "review_type": _safe_str((pending_card or {}).get("review_type")),
            },
            "seen_cards": len(flow.seen_cards),
            "seen_sse_rounds": len(flow.seen_sse),
            "recent_messages_count": len(messages),
            "flow_scores": flow_scores,
        }

        write_json(out_dir / "summary.json", summary)
        write_json(out_dir / "flow_scores.json", flow_scores)
        write_json(out_dir / "snapshot.json", snapshot)
        write_json(out_dir / "analysis_view.json", analysis_view)
        write_json(out_dir / "pricing_plan_view.json", pricing_view)
        write_json(out_dir / "messages.json", {"messages": messages})

    print("[done] analysis workflow completed")
    print(f"[artifacts] {out_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run analysis workflow via consultations WS (real LLM).")
    parser.add_argument("--base-url", default="", help="Gateway base URL, e.g. http://host/api/v1")
    parser.add_argument("--username", default="", help="Lawyer username")
    parser.add_argument("--password", default="", help="Lawyer password")
    parser.add_argument("--kickoff", default=DEFAULT_KICKOFF, help="Initial user query")
    parser.add_argument("--kickoff-max-loops", type=int, default=24, help="kickoff max_loops")
    parser.add_argument("--max-steps", type=int, default=220, help="run_until max steps")
    parser.add_argument("--cards-only", action="store_true", default=False, help="Kickoff once, then only poll and answer cards")
    parser.add_argument("--allow-nudge", dest="allow_nudge", action="store_true", default=False, help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", default="", help="Artifacts output directory")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
