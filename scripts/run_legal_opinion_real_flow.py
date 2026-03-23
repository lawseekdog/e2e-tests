"""Run real legal-opinion workflow via consultations-service WebSocket (no mock LLM)."""

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
from support.workbench.docx import extract_docx_text

from scripts._support.template_draft_real_flow_support import (
    DEFAULT_LEGAL_OPINION_EVIDENCE_RELATIVE,
    DEFAULT_LEGAL_OPINION_FACTS,
)
from scripts._support.workflow_real_flow_support import (
    bootstrap_flow,
    event_counts,
    fetch_workbench_snapshot,
    is_goal_completion_card,
    list_deliverables,
    list_session_messages,
    load_real_flow_env,
    resolve_output_dir,
    safe_str as _safe_str,
    upload_consultation_files,
    write_json,
)
from scripts._support.flow_score_support import build_flow_scores, collect_flow_observability


DEFAULT_KICKOFF = "请基于已上传材料形成一份结构化法律意见分析，输出结论、风险与行动建议。"

FLOW_OVERRIDES = {
    "profile.service_type_id": "legal_opinion",
    "profile.client_role": "applicant",
    "client_role": "applicant",
    "profile.summary": "服务器采购合同履约争议，需要形成法律意见分析。",
    "profile.background": DEFAULT_LEGAL_OPINION_FACTS,
    "profile.facts": DEFAULT_LEGAL_OPINION_FACTS,
    "profile.legal_issue": "暂停付款、逾期交付责任、质量责任、解除与索赔边界。",
    "profile.opinion_topic_primary": "contract_performance",
    "profile.opinion_subtype": "dispute_response",
}

def _extract_legal_opinion_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    analysis = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    direct = analysis.get("legal_opinion_view") if isinstance(analysis.get("legal_opinion_view"), dict) else {}
    if direct:
        return direct
    goals = analysis.get("goal_views") if isinstance(analysis.get("goal_views"), dict) else {}
    view = goals.get("legal_opinion_view") if isinstance(goals.get("legal_opinion_view"), dict) else {}
    if view:
        return view
    goal_views = snapshot.get("goal_views") if isinstance(snapshot.get("goal_views"), dict) else {}
    view = goal_views.get("legal_opinion_view") if isinstance(goal_views.get("legal_opinion_view"), dict) else {}
    return view if isinstance(view, dict) else {}

async def run(args: argparse.Namespace) -> int:
    load_real_flow_env(repo_root=REPO_ROOT, e2e_root=E2E_ROOT)

    base_url = _safe_str(args.base_url) or _safe_str(os.getenv("BASE_URL")) or "http://localhost:18001/api/v1"
    username = _safe_str(args.username) or _safe_str(os.getenv("LAWYER_USERNAME")) or "lawyer1"
    password = _safe_str(args.password) or _safe_str(os.getenv("LAWYER_PASSWORD")) or "lawyer123456"
    kickoff = _safe_str(args.kickoff) or DEFAULT_KICKOFF

    evidence_paths = []
    for rel in DEFAULT_LEGAL_OPINION_EVIDENCE_RELATIVE:
        p = (REPO_ROOT / rel).resolve()
        if p.exists():
            evidence_paths.append(p)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = resolve_output_dir(
        repo_root=REPO_ROOT,
        output_dir=_safe_str(args.output_dir),
        default_leaf=f"output/legal-opinion-chain/{ts}",
    )

    print(f"[config] base_url={base_url}")
    print(f"[config] user={username}")
    print(f"[config] output_dir={out_dir}")

    async with ApiClient(base_url) as client:
        await client.login(username, password)
        print(f"[login] ok user_id={client.user_id} org_id={client.organization_id}")

        uploaded_file_ids = await upload_consultation_files(client, evidence_paths)
        for path, fid in zip(evidence_paths, uploaded_file_ids):
            print(f"[upload] ok file={path.name} file_id={fid}")

        flow, session_id, matter_id = await bootstrap_flow(
            client=client,
            service_type_id="legal_opinion",
            client_role="applicant",
            uploaded_file_ids=uploaded_file_ids,
            overrides=FLOW_OVERRIDES,
        )
        print(f"[session] id={session_id} matter_id={matter_id or '-'}")

        kickoff_sse = await flow.nudge(kickoff, attachments=uploaded_file_ids, max_loops=max(1, int(args.kickoff_max_loops)))
        kickoff_counts = event_counts(kickoff_sse if isinstance(kickoff_sse, dict) else {})
        write_json(out_dir / "kickoff.sse.json", kickoff_sse if isinstance(kickoff_sse, dict) else {})

        async def _legal_opinion_ready(f: WorkbenchFlow) -> bool:
            await f.refresh()
            if not f.matter_id:
                return False
            pending = await f.get_pending_card()
            if _is_goal_completion_card(pending):
                return True
            snapshot = await fetch_workbench_snapshot(client, f.matter_id)
            view = _extract_legal_opinion_view(snapshot)
            issues = view.get("issues") if isinstance(view.get("issues"), list) else []
            action_items = view.get("action_items") if isinstance(view.get("action_items"), list) else []
            risks = view.get("risks") if isinstance(view.get("risks"), list) else []
            return bool(_safe_str(view.get("summary")) and (issues or action_items or risks))

        try:
            await flow.run_until(
                _legal_opinion_ready,
                max_steps=max(1, int(args.max_steps)),
                description="legal opinion view ready",
                allow_nudge=bool(args.allow_nudge),
            )
        except Exception as exc:
            await flow.refresh()
            fail_snapshot = await fetch_workbench_snapshot(client, _safe_str(flow.matter_id)) if flow.matter_id else {}
            write_json(
                out_dir / "failure_diagnostics.json",
                {
                    "error": str(exc),
                    "session_id": session_id,
                    "matter_id": _safe_str(flow.matter_id),
                    "seen_cards": len(flow.seen_cards),
                    "seen_sse_rounds": len(flow.seen_sse),
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
        view = _extract_legal_opinion_view(snapshot)
        pending_card = await flow.get_pending_card()
        deliverables = await list_deliverables(client, final_matter_id)
        messages = await list_session_messages(client, session_id)

        output_key = next((key for key in deliverables if key == "legal_opinion"), "")
        opinion_text = ""
        if output_key:
            file_id = _safe_str((deliverables.get(output_key) or {}).get("file_id"))
            if file_id:
                raw = await client.download_file_bytes(file_id)
                opinion_text = extract_docx_text(raw)
                (out_dir / "legal_opinion.txt").write_text(opinion_text, encoding="utf-8")
        observability = await collect_flow_observability(client, matter_id=final_matter_id, session_id=session_id)
        flow_scores = build_flow_scores(
            flow_id="legal_opinion",
            seen_cards=flow.seen_cards,
            pending_card=pending_card,
            snapshot=snapshot,
            current_view=view,
            aux_views={},
            deliverables=deliverables,
            deliverable_text=opinion_text,
            deliverable_status=_safe_str((deliverables.get("legal_opinion") or {}).get("status")),
            observability=observability,
            goal_completion_mode="card" if is_goal_completion_card(pending_card) else "none",
        )

        summary = {
            "base_url": base_url,
            "session_id": session_id,
            "matter_id": final_matter_id,
            "uploaded_file_ids": uploaded_file_ids,
            "kickoff_event_counts": kickoff_counts,
            "deliverable_keys": sorted(deliverables.keys()),
            "legal_opinion_view": {
                "summary_len": len(_safe_str(view.get("summary"))),
                "issues_count": len(view.get("issues")) if isinstance(view.get("issues"), list) else 0,
                "risk_count": len(view.get("risks")) if isinstance(view.get("risks"), list) else 0,
                "action_items_count": len(view.get("action_items")) if isinstance(view.get("action_items"), list) else 0,
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
        write_json(out_dir / "legal_opinion_view.json", view)
        write_json(out_dir / "deliverables.json", deliverables)
        write_json(out_dir / "messages.json", {"messages": messages})

    print("[done] legal opinion workflow completed")
    print(f"[artifacts] {out_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run legal opinion workflow via consultations WS (real LLM).")
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
