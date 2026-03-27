"""Run real analysis workflow via consultations-service WebSocket (no mock LLM)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sys

E2E_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = E2E_ROOT.parent
sys.path.insert(0, str(E2E_ROOT))

from client.api_client import ApiClient
from scripts._support.workflow_real_flow_support import (
    bootstrap_flow,
    collect_ai_debug_refs,
    configure_direct_service_mode,
    event_counts,
    fetch_workbench_snapshot,
    is_goal_completion_card,
    list_session_messages,
    load_real_flow_env,
    resolve_output_dir,
    safe_str as _safe_str,
    terminate_stale_script_runs,
    upload_consultation_files,
    write_json,
)
from scripts._support.diagnostic_bundle_support import export_failure_bundle, export_observability_bundle, format_first_bad_line
from scripts._support.flow_score_support import build_flow_scores, collect_flow_observability
from scripts._support.quality_policy_support import build_bundle_quality_reports


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


def _extract_runtime_progress(snapshot: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(snapshot, dict):
        return {"current_task_id": "", "current_node": "", "current_phase": ""}
    analysis = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    identity = analysis.get("identity") if isinstance(analysis.get("identity"), dict) else {}
    runtime = analysis.get("workbench_runtime") if isinstance(analysis.get("workbench_runtime"), dict) else {}
    return {
        "current_task_id": _safe_str(identity.get("current_task_id")),
        "current_node": _safe_str(analysis.get("current_node") or runtime.get("current_node")),
        "current_phase": _safe_str(analysis.get("current_phase") or snapshot.get("current_phase") or runtime.get("current_phase")),
    }


def _compact_pending_card(card: dict[str, Any] | None) -> dict[str, Any]:
    pending = card if isinstance(card, dict) else {}
    questions = pending.get("questions") if isinstance(pending.get("questions"), list) else []
    return {
        "id": _safe_str(pending.get("id")),
        "skill_id": _safe_str(pending.get("skill_id")),
        "task_key": _safe_str(pending.get("task_key")),
        "review_type": _safe_str(pending.get("review_type")),
        "question_count": len(questions),
        "questions": questions,
    }


def _analysis_readiness(analysis_view: dict[str, Any], pricing_view: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "summary_ready": bool(_safe_str(analysis_view.get("summary"))),
        "issues_ready": bool(isinstance(analysis_view.get("issues"), list) and analysis_view.get("issues")),
        "strategy_options_ready": bool(
            isinstance(analysis_view.get("strategy_options"), list) and analysis_view.get("strategy_options")
        ),
        "pricing_ready": _safe_str(pricing_view.get("status")).lower() in {"ready", "review_pending"},
    }
    missing = [name for name, passed in checks.items() if not passed]
    return {"checks": checks, "missing_requirements": missing, "ready": not missing}


def _write_live_status(
    out_dir: Path,
    *,
    state: str,
    session_id: str,
    matter_id: str,
    wait_round: int,
    snapshot: dict[str, Any] | None,
    pending_card: dict[str, Any] | None,
    analysis_view: dict[str, Any] | None,
    pricing_view: dict[str, Any] | None,
    seen_cards: int,
    seen_sse_rounds: int,
    error: str = "",
    kickoff_output: str = "",
) -> None:
    snapshot_obj = snapshot if isinstance(snapshot, dict) else {}
    analysis_obj = analysis_view if isinstance(analysis_view, dict) else {}
    pricing_obj = pricing_view if isinstance(pricing_view, dict) else {}
    runtime = _extract_runtime_progress(snapshot_obj)
    readiness = _analysis_readiness(analysis_obj, pricing_obj)
    payload = {
        "contract_version": "analysis_live_status.v1",
        "state": _safe_str(state),
        "session_id": _safe_str(session_id),
        "matter_id": _safe_str(matter_id),
        "wait_round": int(wait_round),
        "current_task_id": runtime["current_task_id"],
        "current_node": runtime["current_node"],
        "current_phase": runtime["current_phase"],
        "pending_card": _compact_pending_card(pending_card),
        "analysis_view": {
            "summary_len": len(_safe_str(analysis_obj.get("summary"))),
            "issues_count": len(analysis_obj.get("issues")) if isinstance(analysis_obj.get("issues"), list) else 0,
            "strategy_options_count": len(analysis_obj.get("strategy_options")) if isinstance(analysis_obj.get("strategy_options"), list) else 0,
        },
        "pricing_plan_view": {
            "status": _safe_str(pricing_obj.get("status")),
            "reviewed": bool(pricing_obj.get("reviewed")),
            "pricing_mode": _safe_str(pricing_obj.get("pricing_mode")),
        },
        "readiness": readiness,
        "seen_cards": int(seen_cards),
        "seen_sse_rounds": int(seen_sse_rounds),
        "error": _safe_str(error),
        "kickoff_output": _safe_str(kickoff_output),
    }
    write_json(out_dir / "live_status.json", payload)
    write_json(out_dir / "snapshot.latest.json", snapshot_obj)
    write_json(out_dir / "analysis_view.latest.json", analysis_obj)
    write_json(out_dir / "pricing_plan_view.latest.json", pricing_obj)
    write_json(out_dir / "pending_card.latest.json", _compact_pending_card(pending_card))


def _fallback_failure_summary(
    *,
    session_id: str,
    matter_id: str,
    snapshot: dict[str, Any] | None,
    error: Exception,
) -> dict[str, Any]:
    runtime = _extract_runtime_progress(snapshot if isinstance(snapshot, dict) else {})
    message = _safe_str(error)
    reason_code = "analysis_view_not_ready" if "analysis view + pricing plan ready" in message else error.__class__.__name__.lower()
    return {
        "contract_version": "failure_summary.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "bundle_dir": "",
        "first_bad_node": runtime.get("current_node") or runtime.get("current_task_id") or "",
        "first_bad_stage": runtime.get("current_phase") or "",
        "failure_class": "workflow_run_failed",
        "primary_reason_code": reason_code,
        "reason_code_chain": [reason_code],
        "retry_prompt": message,
    }

async def run(args: argparse.Namespace) -> int:
    load_real_flow_env(repo_root=REPO_ROOT, e2e_root=E2E_ROOT)
    terminate_stale_script_runs(script_name="run_analysis_real_flow.py")

    direct_mode = not bool(args.use_gateway)
    direct_config: dict[str, str] = {}
    if direct_mode:
        base_url, direct_config = configure_direct_service_mode(
            remote_stack_host=_safe_str(args.remote_stack_host),
            consultations_base_url=_safe_str(args.consultations_base_url),
            matter_base_url=_safe_str(args.matter_base_url),
            files_base_url=_safe_str(args.files_base_url),
            local_consultations=True,
            local_matter=True,
            direct_user_id=_safe_str(args.direct_user_id),
            direct_org_id=_safe_str(args.direct_org_id),
            direct_is_superuser=_safe_str(args.direct_is_superuser),
        )
    else:
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
    print(f"[config] direct_service_mode={direct_mode}")
    if direct_mode:
        print(f"[config] consultations_base_url={direct_config.get('consultations_base_url') or '-'}")
        print(f"[config] files_base_url={direct_config.get('files_base_url') or '-'}")
        print(f"[config] matter_base_url={direct_config.get('matter_base_url') or '-'}")
        print(f"[config] direct_user_id={direct_config.get('direct_user_id') or '-'}")
        print(f"[config] direct_org_id={direct_config.get('direct_org_id') or '-'}")
    else:
        print("[config] gateway_mode=true")
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
        kickoff_output = _safe_str((kickoff_sse if isinstance(kickoff_sse, dict) else {}).get("output"))
        wait_round = 0
        _write_live_status(
            out_dir,
            state="kickoff_completed",
            session_id=session_id,
            matter_id=_safe_str(flow.matter_id),
            wait_round=wait_round,
            snapshot={},
            pending_card=None,
            analysis_view={},
            pricing_view={},
            seen_cards=len(flow.seen_cards),
            seen_sse_rounds=len(flow.seen_sse),
            kickoff_output=kickoff_output,
        )

        async def _analysis_ready(f: WorkbenchFlow) -> bool:
            nonlocal wait_round
            wait_round += 1
            await f.refresh()
            if not f.matter_id:
                _write_live_status(
                    out_dir,
                    state="waiting_for_matter",
                    session_id=session_id,
                    matter_id="",
                    wait_round=wait_round,
                    snapshot={},
                    pending_card=None,
                    analysis_view={},
                    pricing_view={},
                    seen_cards=len(f.seen_cards),
                    seen_sse_rounds=len(f.seen_sse),
                    kickoff_output=kickoff_output,
                )
                return False
            pending = await f.get_pending_card()
            snapshot = await fetch_workbench_snapshot(client, f.matter_id)
            analysis_view = _extract_analysis_view(snapshot)
            pricing_view = _extract_pricing_view(snapshot)
            if is_goal_completion_card(pending):
                _write_live_status(
                    out_dir,
                    state="goal_completion_pending",
                    session_id=session_id,
                    matter_id=_safe_str(f.matter_id),
                    wait_round=wait_round,
                    snapshot=snapshot,
                    pending_card=pending,
                    analysis_view=analysis_view,
                    pricing_view=pricing_view,
                    seen_cards=len(f.seen_cards),
                    seen_sse_rounds=len(f.seen_sse),
                    kickoff_output=kickoff_output,
                )
                return True
            ready = bool(
                _safe_str(analysis_view.get("summary"))
                and isinstance(analysis_view.get("issues"), list)
                and analysis_view.get("issues")
                and isinstance(analysis_view.get("strategy_options"), list)
                and analysis_view.get("strategy_options")
                and _safe_str(pricing_view.get("status")).lower() in {"ready", "review_pending"}
            )
            _write_live_status(
                out_dir,
                state="ready" if ready else "waiting_for_views",
                session_id=session_id,
                matter_id=_safe_str(f.matter_id),
                wait_round=wait_round,
                snapshot=snapshot,
                pending_card=pending,
                analysis_view=analysis_view,
                pricing_view=pricing_view,
                seen_cards=len(f.seen_cards),
                seen_sse_rounds=len(f.seen_sse),
                kickoff_output=kickoff_output,
            )
            return ready

        try:
            await flow.run_until(
                _analysis_ready,
                max_steps=max(1, int(args.max_steps)),
                description="analysis view + pricing plan ready",
            )
        except Exception as exc:
            await flow.refresh()
            fail_snapshot = await fetch_workbench_snapshot(client, _safe_str(flow.matter_id)) if flow.matter_id else {}
            fail_messages = await list_session_messages(client, session_id)
            debug_refs = await collect_ai_debug_refs(
                client,
                repo_root=REPO_ROOT,
                session_id=session_id,
                matter_id=_safe_str(flow.matter_id),
            )
            failure_diag = {
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
                "debug_refs": debug_refs,
            }
            _write_live_status(
                out_dir,
                state="failed",
                session_id=session_id,
                matter_id=_safe_str(flow.matter_id),
                wait_round=wait_round,
                snapshot=fail_snapshot if isinstance(fail_snapshot, dict) else {},
                pending_card=await flow.get_pending_card(),
                analysis_view=_extract_analysis_view(fail_snapshot if isinstance(fail_snapshot, dict) else {}),
                pricing_view=_extract_pricing_view(fail_snapshot if isinstance(fail_snapshot, dict) else {}),
                seen_cards=len(flow.seen_cards),
                seen_sse_rounds=len(flow.seen_sse),
                error=str(exc),
                kickoff_output=kickoff_output,
            )
            fallback_summary = _fallback_failure_summary(
                session_id=session_id,
                matter_id=_safe_str(flow.matter_id),
                snapshot=fail_snapshot if isinstance(fail_snapshot, dict) else {},
                error=exc,
            )
            if isinstance(fail_snapshot, dict) and fail_snapshot:
                write_json(out_dir / "snapshot.failure.json", fail_snapshot)
            try:
                bundle = export_failure_bundle(
                    repo_root=REPO_ROOT,
                    session_id=session_id,
                    matter_id=_safe_str(flow.matter_id),
                    reason="analysis_real_flow_failed",
                )
                bundle_quality = build_bundle_quality_reports(
                    repo_root=REPO_ROOT,
                    bundle_dir=bundle["bundle_dir"],
                    flow_id="analysis",
                    snapshot=fail_snapshot if isinstance(fail_snapshot, dict) else {},
                    current_view=_extract_analysis_view(fail_snapshot if isinstance(fail_snapshot, dict) else {}),
                    goal_completion_mode="none",
                )
                write_json(out_dir / "failure_summary.json", bundle["summary"])
                write_json(out_dir / "bundle_quality.failure.json", bundle_quality)
                print(format_first_bad_line(bundle["summary"]))
            except Exception as diag_exc:
                failure_diag["observability_error"] = str(diag_exc)
                write_json(out_dir / "failure_summary.json", fallback_summary)
                print(format_first_bad_line(fallback_summary))
            write_json(out_dir / "failure_diagnostics.json", failure_diag)
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
        bundle_export = export_observability_bundle(
            repo_root=REPO_ROOT,
            session_id=session_id,
            matter_id=final_matter_id,
            reason="analysis_real_flow_success",
        )
        observability = await collect_flow_observability(client, matter_id=final_matter_id, session_id=session_id)
        bundle_quality = build_bundle_quality_reports(
            repo_root=REPO_ROOT,
            bundle_dir=bundle_export["bundle_dir"],
            flow_id="analysis",
            snapshot=snapshot,
            current_view=analysis_view,
            goal_completion_mode="card" if is_goal_completion_card(pending_card) else "none",
        )
        debug_refs = await collect_ai_debug_refs(
            client,
            repo_root=REPO_ROOT,
            session_id=session_id,
            matter_id=final_matter_id,
        )
        quality_summary_ref = str((bundle_quality.get("refs") or {}).get("summary") or "").strip()
        if quality_summary_ref:
            bundle_refs = debug_refs.get("bundle_refs") if isinstance(debug_refs.get("bundle_refs"), list) else []
            if quality_summary_ref not in bundle_refs:
                bundle_refs.append(quality_summary_ref)
                debug_refs["bundle_refs"] = bundle_refs
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
            bundle_quality_summary=bundle_quality,
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
            "debug_refs": debug_refs,
            "bundle_quality": bundle_quality,
            "flow_scores": flow_scores,
        }

        _write_live_status(
            out_dir,
            state="completed",
            session_id=session_id,
            matter_id=final_matter_id,
            wait_round=wait_round,
            snapshot=snapshot,
            pending_card=pending_card,
            analysis_view=analysis_view,
            pricing_view=pricing_view,
            seen_cards=len(flow.seen_cards),
            seen_sse_rounds=len(flow.seen_sse),
            kickoff_output=kickoff_output,
        )

        write_json(out_dir / "summary.json", summary)
        write_json(out_dir / "bundle_quality.json", bundle_quality)
        write_json(out_dir / "flow_scores.json", flow_scores)
        write_json(out_dir / "snapshot.json", snapshot)
        write_json(out_dir / "analysis_view.json", analysis_view)
        write_json(out_dir / "pricing_plan_view.json", pricing_view)
        write_json(out_dir / "messages.json", {"messages": messages})
        write_json(out_dir / "diagnostics_summary.json", debug_refs.get("diagnostics_summary") if isinstance(debug_refs.get("diagnostics_summary"), dict) else {})
        write_json(out_dir / "diagnostics_events.json", {"events": debug_refs.get("diagnostics_events") if isinstance(debug_refs.get("diagnostics_events"), list) else []})
        write_json(out_dir / "debug_refs.json", debug_refs)

    print("[done] analysis workflow completed")
    print(f"[artifacts] {out_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run analysis workflow via consultations WS (real LLM).")
    parser.add_argument("--base-url", default="", help="Gateway base URL, e.g. http://host/api/v1")
    parser.add_argument("--use-gateway", action="store_true", default=False, help="Use gateway mode instead of direct local/remote service URLs")
    parser.add_argument("--consultations-base-url", default="", help="Direct consultations-service base URL")
    parser.add_argument("--files-base-url", default="", help="Direct files-service base URL")
    parser.add_argument("--matter-base-url", default="", help="Direct matter-service base URL")
    parser.add_argument("--remote-stack-host", default="", help="Remote stack host for direct non-local services")
    parser.add_argument("--direct-user-id", default="", help="Optional direct service mode user id (skip auth only when set)")
    parser.add_argument("--direct-org-id", default="", help="Optional direct service mode organization id (skip auth only when set)")
    parser.add_argument("--direct-is-superuser", default="", help="Optional direct service mode superuser flag")
    parser.add_argument("--username", default="", help="Lawyer username")
    parser.add_argument("--password", default="", help="Lawyer password")
    parser.add_argument("--kickoff", default=DEFAULT_KICKOFF, help="Initial user query")
    parser.add_argument("--kickoff-max-loops", type=int, default=24, help="kickoff max_loops")
    parser.add_argument("--max-steps", type=int, default=220, help="run_until max steps")
    parser.add_argument("--cards-only", action="store_true", default=False, help="Kickoff once, then only poll and answer cards")
    parser.add_argument("--output-dir", default="", help="Artifacts output directory")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
