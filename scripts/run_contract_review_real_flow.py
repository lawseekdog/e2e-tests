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
    collect_ai_debug_refs,
    configure_direct_service_mode,
    event_counts as _shared_event_counts,
    fetch_execution_snapshot_by_session as _shared_fetch_execution_snapshot_by_session,
    fetch_workbench_snapshot as _shared_fetch_workbench_snapshot,
    list_session_messages as _shared_list_session_messages,
    load_real_flow_env,
    resolve_output_dir,
    upload_consultation_files,
    terminate_stale_script_runs,
    write_json,
)
from scripts._support.diagnostic_bundle_support import export_failure_bundle, export_observability_bundle
from scripts._support.flow_score_support import build_flow_scores, collect_flow_observability
from scripts._support.quality_policy_support import build_bundle_quality_reports
from scripts._support.run_status import RunStatusSupervisor


REQUIRED_DOC_OUTPUT_KEYS = ("contract_review_report",)

DEFAULT_KICKOFF = (
    "请审查已上传合同并输出结构化结论：整体风险等级、合同类型、审查摘要、风险条款清单。"
    "重点关注违约责任、争议解决、免责条款与付款条件。"
)

FLOW_OVERRIDES = {
    "profile.client_role": "applicant",
    "profile.summary": "合同审查，重点关注付款条件、违约责任、争议解决与免责条款。",
}
START_REQUESTED_DOCUMENTS = [
    {"document_kind": "contract_review_report", "instance_key": ""},
]


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _start_requested_documents() -> list[dict[str, str]]:
    return [dict(item) for item in START_REQUESTED_DOCUMENTS]


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


async def _fetch_execution_snapshot(session_id: str) -> dict[str, Any] | None:
    return await _shared_fetch_execution_snapshot_by_session(session_id)


def _section_items(view: dict[str, Any], section_type: str) -> list[dict[str, Any]]:
    sections = view.get("sections") if isinstance(view.get("sections"), list) else []
    for section in sections:
        if not isinstance(section, dict):
            continue
        if _safe_str(section.get("section_type")) != section_type:
            continue
        data = section.get("data") if isinstance(section.get("data"), dict) else {}
        items = data.get("items") if isinstance(data.get("items"), list) else []
        return [row for row in items if isinstance(row, dict)]
    return []


def _extract_analysis_view(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {}
    view = snapshot.get("analysis_view") if isinstance(snapshot.get("analysis_view"), dict) else {}
    return view if isinstance(view, dict) else {}


def _issue_type_from_title(title: str) -> str:
    text = _safe_str(title)
    mappings = (
        ("payment", ("付款", "工程款", "价款", "结算")),
        ("tax_invoice", ("发票", "税票")),
        ("delivery_acceptance", ("验收", "交付")),
        ("quality", ("质量", "质保", "保证金")),
        ("change_order", ("变更", "签证")),
        ("delay", ("工期", "顺延", "延期")),
        ("liability", ("违约", "责任")),
        ("indemnity", ("赔偿", "补偿", "免责")),
        ("termination", ("解除", "终止")),
        ("compliance", ("招标", "合规", "审批")),
        ("dispute_resolution", ("争议", "仲裁", "诉讼", "管辖")),
        ("notice", ("通知", "送达")),
        ("effectiveness", ("效力", "生效", "冲突")),
    )
    for issue_type, keywords in mappings:
        if any(keyword in text for keyword in keywords):
            return issue_type
    return "general"


def _risk_rank(level: str) -> int:
    return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(_safe_str(level).lower(), 0)


def _extract_inline_artifact_body(artifact_refs: Any) -> str:
    rows = artifact_refs if isinstance(artifact_refs, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        body = _safe_str(metadata.get("body"))
        if body:
            return body
    return ""


def _extract_runtime_deliverables(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(snapshot, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    run_status = _safe_str(snapshot.get("status")).lower()
    rows = snapshot.get("deliverables") if isinstance(snapshot.get("deliverables"), list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        kind = (
            _safe_str(row.get("deliverable_kind"))
            or _safe_str(row.get("document_kind"))
            or _safe_str(payload.get("document_kind"))
        )
        if kind:
            entry = out.setdefault(kind, {"output_key": kind})
            full_text = _safe_str(payload.get("full_text"))
            if full_text:
                entry["full_text"] = full_text
            title = _safe_str(row.get("title"))
            if title:
                entry["title"] = title
            summary = _safe_str(row.get("summary"))
            if summary:
                entry["summary"] = summary
            if run_status:
                entry.setdefault("status", run_status)
        outputs = row.get("outputs") if isinstance(row.get("outputs"), list) else []
        for output in outputs:
            if not isinstance(output, dict):
                continue
            output_kind = _safe_str(output.get("deliverable_kind")) or _safe_str(output.get("document_kind"))
            if not output_kind:
                continue
            entry = out.setdefault(output_kind, {"output_key": output_kind})
            status = _safe_str(output.get("render_status")) or _safe_str(row.get("status")) or run_status
            if status:
                entry["status"] = status
            for key in ("file_id", "preview_file_id", "deliverable_id", "document_id", "render_format"):
                value = _safe_str(output.get(key))
                if value:
                    entry[key] = value
            inline_body = _extract_inline_artifact_body(output.get("artifact_refs"))
            if inline_body:
                entry.setdefault("full_text", inline_body)
            artifact_refs = output.get("artifact_refs") if isinstance(output.get("artifact_refs"), list) else []
            if artifact_refs:
                entry["artifact_refs"] = artifact_refs
    return out


def _build_contract_view(
    snapshot: dict[str, Any] | None,
    *,
    contract_type_id: str,
    review_scope: str,
) -> dict[str, Any]:
    analysis_view = _extract_analysis_view(snapshot)
    if not analysis_view:
        return {}
    issues = _section_items(analysis_view, "issues")
    risks = _section_items(analysis_view, "risks")
    strategies = _section_items(analysis_view, "strategy_matrix")
    risk_by_focus: dict[str, dict[str, Any]] = {}
    for risk in risks:
        focus_refs = risk.get("focus_refs") if isinstance(risk.get("focus_refs"), list) else []
        for ref in focus_refs:
            token = _safe_str(ref)
            if token and token not in risk_by_focus:
                risk_by_focus[token] = risk
    clauses: list[dict[str, Any]] = []
    for issue in issues:
        issue_id = _safe_str(issue.get("issue_id")) or f"issue:{len(clauses) + 1}"
        title = _safe_str(issue.get("issue_title") or issue.get("title")) or issue_id
        risk = risk_by_focus.get(issue_id, {})
        authority_refs = [token for token in (issue.get("authority_refs") if isinstance(issue.get("authority_refs"), list) else []) if _safe_str(token)]
        clauses.append(
            {
                "clause_id": issue_id,
                "title": title,
                "risk_type": _issue_type_from_title(title),
                "risk_level": _safe_str(risk.get("level")).lower() or "medium",
                "analysis": _safe_str(issue.get("analysis")),
                "anchor_refs": [{"anchor_id": _safe_str(ref)} for ref in (issue.get("evidence_refs") if isinstance(issue.get("evidence_refs"), list) else []) if _safe_str(ref)],
                "law_ref_ids": authority_refs,
                "authority_titles": [token for token in (issue.get("authority_titles") if isinstance(issue.get("authority_titles"), list) else []) if _safe_str(token)],
                "mitigation": _safe_str(risk.get("mitigation")),
            }
        )
    overall_risk_level = "low"
    for risk in risks:
        level = _safe_str(risk.get("level")).lower()
        if _risk_rank(level) >= _risk_rank(overall_risk_level):
            overall_risk_level = level or overall_risk_level
    return {
        "title": _safe_str(analysis_view.get("title")) or "合同审查",
        "summary": _safe_str(analysis_view.get("summary")),
        "status": _safe_str(analysis_view.get("status")),
        "contract_type_id": _safe_str(contract_type_id),
        "review_scope": _safe_str(review_scope),
        "overall_risk_level": overall_risk_level,
        "clauses": clauses,
        "strategy_options": [row for row in strategies if isinstance(row, dict)],
        "result_contract_diagnostics": {"status": "valid" if clauses else "invalid"},
    }


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
    terminate_stale_script_runs(script_name="run_contract_review_real_flow.py")

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
    supervisor = RunStatusSupervisor(out_dir=out_dir, flow_id="contract_review")
    supervisor.update(status="booting", current_step="bootstrap.init", next_action="login")

    print(f"[config] base_url={base_url}")
    print(f"[config] direct_service_mode={direct_mode}")
    if direct_mode:
        print(f"[config] auth_base_url={direct_config.get('auth_base_url') or '-'}")
        print(f"[config] consultations_base_url={direct_config.get('consultations_base_url') or '-'}")
        print(f"[config] matter_base_url={direct_config.get('matter_base_url') or '-'}")
        print(f"[config] files_base_url={direct_config.get('files_base_url') or '-'}")
    else:
        print("[config] gateway_mode=true")
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
        supervisor.update(status="booting", current_step="bootstrap.login", next_action="upload_contract")

        uploaded_file_ids = await upload_consultation_files(client, [contract_file])
        file_id = uploaded_file_ids[0] if uploaded_file_ids else ""
        if not file_id:
            raise RuntimeError(f"upload_file failed: {contract_file}")
        print(f"[upload] ok file_id={file_id}")
        supervisor.update(
            status="booting",
            current_step="bootstrap.upload_contract",
            next_action="create_session",
            extra={"uploaded_file_ids": uploaded_file_ids},
        )

        flow, session_id, matter_id = await bootstrap_flow(
            client=client,
            service_type_id="contract_review",
            client_role="applicant",
            uploaded_file_ids=[file_id],
            overrides=flow_overrides,
            strict_card_driven=True,
            progress_observer=supervisor.observe_flow_progress,
        )
        print(f"[session] id={session_id} matter_id={matter_id or '-'}")
        supervisor.update(
            status="booting",
            current_step="bootstrap.session_created",
            session_id=session_id,
            matter_id=matter_id,
            next_action="kickoff",
        )

        kickoff_settle_mode = "fire_and_poll" if args.cards_only else "full"
        supervisor.update(
            status="running",
            current_step="kickoff.submitting",
            session_id=session_id,
            matter_id=_safe_str(flow.matter_id) or matter_id,
            next_action="await_kickoff_events",
        )
        kickoff_sse = await flow.request_documents(
            _start_requested_documents(),
            user_query=kickoff,
            attachments=[file_id],
            max_loops=max(1, int(args.kickoff_max_loops)),
            settle_mode=kickoff_settle_mode,
            label="contract_review_report",
        )
        kickoff_counts = _event_counts(kickoff_sse if isinstance(kickoff_sse, dict) else {})
        print(f"[kickoff] event_counts={kickoff_counts}")
        write_json(out_dir / "kickoff.sse.json", kickoff_sse if isinstance(kickoff_sse, dict) else {})
        supervisor.update(
            status="running",
            current_step="kickoff.completed",
            session_id=session_id,
            matter_id=_safe_str(flow.matter_id) or matter_id,
            next_action="wait_deliverables",
            extra={"kickoff_event_counts": kickoff_counts},
        )
        supervisor.update(
            status="running",
            current_step="deliverables.waiting",
            session_id=session_id,
            matter_id=_safe_str(flow.matter_id) or matter_id,
            next_action="continue_poll",
        )

        async def _deliverables_ready(f: WorkbenchFlow) -> bool:
            await f.refresh()
            runtime_snapshot = await _fetch_execution_snapshot(session_id)
            by_key = _extract_runtime_deliverables(runtime_snapshot)
            report = by_key.get("contract_review_report") or {}
            if not report:
                return False
            status = _safe_str(report.get("status")).lower()
            if status and status not in {"completed", "ready"}:
                return False
            file_ref = _safe_str(report.get("file_id"))
            inline_text = _safe_str(report.get("full_text"))
            return bool(file_ref or inline_text)

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
            fail_runtime_snapshot = await _fetch_execution_snapshot(session_id)
            fail_deliverables = _extract_runtime_deliverables(fail_runtime_snapshot)
            fail_messages = await _list_session_messages(client, session_id)
            debug_refs = await collect_ai_debug_refs(
                client,
                repo_root=REPO_ROOT,
                session_id=session_id,
                matter_id=fail_matter_id,
            )
            fail_contract_view = _build_contract_view(
                fail_snapshot if isinstance(fail_snapshot, dict) else {},
                contract_type_id=contract_type_id,
                review_scope=review_scope,
            )
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
                "debug_refs": debug_refs,
            }
            write_json(out_dir / "failure_diagnostics.json", failure_diag)
            write_json(out_dir / "deliverables.failure.json", fail_deliverables)
            if isinstance(fail_snapshot, dict) and fail_snapshot:
                write_json(out_dir / "snapshot.failure.json", fail_snapshot)
            if isinstance(fail_runtime_snapshot, dict) and fail_runtime_snapshot:
                write_json(out_dir / "execution_snapshot.failure.json", fail_runtime_snapshot)
            bundle = export_failure_bundle(
                repo_root=REPO_ROOT,
                session_id=session_id,
                matter_id=fail_matter_id,
                reason="contract_review_real_flow_failed",
            )
            bundle_quality = build_bundle_quality_reports(
                repo_root=REPO_ROOT,
                bundle_dir=bundle["bundle_dir"],
                flow_id="contract_review",
                snapshot=fail_snapshot if isinstance(fail_snapshot, dict) else {},
                current_view=_extract_contract_view(fail_snapshot if isinstance(fail_snapshot, dict) else {}),
                goal_completion_mode="none",
            )
            write_json(out_dir / "failure_summary.json", bundle["summary"])
            write_json(out_dir / "bundle_quality.failure.json", bundle_quality)
            supervisor.update(
                status="failed",
                current_step="terminal.failed",
                session_id=session_id,
                matter_id=fail_matter_id,
                current_blocker="contract_review_real_flow_failed",
                next_action="inspect_failure_summary",
                error=str(e),
                artifact_refs={"failure_summary": str(out_dir / "failure_summary.json")},
            )
            raise

        await flow.refresh()
        final_matter_id = _safe_str(flow.matter_id)
        if not final_matter_id:
            raise RuntimeError("matter_id missing after workflow run")

        execution_snapshot = await _fetch_execution_snapshot(session_id)
        if not isinstance(execution_snapshot, dict) or not execution_snapshot:
            raise RuntimeError("execution_snapshot_missing")
        deliverables = _extract_runtime_deliverables(execution_snapshot)
        snapshot = await _fetch_snapshot(client, final_matter_id) or {}
        contract_view = _build_contract_view(
            snapshot,
            contract_type_id=contract_type_id,
            review_scope=review_scope,
        )
        pending_card = await flow.get_pending_card()

        report_file_id = _safe_str((deliverables.get("contract_review_report") or {}).get("file_id"))
        report_text = _safe_str((deliverables.get("contract_review_report") or {}).get("full_text"))
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
            "execution_status": _safe_str(execution_snapshot.get("status")),
            "execution_phase_id": _safe_str(execution_snapshot.get("current_phase_id")),
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
        bundle_export = export_observability_bundle(
            repo_root=REPO_ROOT,
            session_id=session_id,
            matter_id=final_matter_id,
            reason="contract_review_real_flow_success",
        )
        observability = await collect_flow_observability(client, matter_id=final_matter_id, session_id=session_id)
        bundle_quality = build_bundle_quality_reports(
            repo_root=REPO_ROOT,
            bundle_dir=bundle_export["bundle_dir"],
            flow_id="contract_review",
            snapshot=snapshot,
            current_view=contract_view,
            goal_completion_mode="card" if _safe_str((pending_card or {}).get("skill_id")).lower() == "goal-completion" else "none",
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
            bundle_quality_summary=bundle_quality,
            goal_completion_mode="card" if _safe_str((pending_card or {}).get("skill_id")).lower() == "goal-completion" else "none",
        )
        summary["flow_scores"] = flow_scores
        summary["debug_refs"] = debug_refs
        summary["bundle_quality"] = bundle_quality

        if start_images and end_images and start_images != end_images and str(os.getenv("E2E_ALLOW_DEPLOYMENT_DRIFT", "") or "").strip() not in {"1", "true", "yes"}:
            raise RuntimeError(f"deployment_image_drift_detected: start={start_images} end={end_images}")

        write_json(out_dir / "summary.json", summary)
        write_json(out_dir / "bundle_quality.json", bundle_quality)
        write_json(out_dir / "flow_scores.json", flow_scores)
        write_json(out_dir / "deliverables.json", deliverables)
        write_json(out_dir / "snapshot.json", snapshot)
        write_json(out_dir / "execution_snapshot.json", execution_snapshot)
        write_json(out_dir / "diagnostics_summary.json", debug_refs.get("diagnostics_summary") if isinstance(debug_refs.get("diagnostics_summary"), dict) else {})
        write_json(out_dir / "diagnostics_events.json", {"events": debug_refs.get("diagnostics_events") if isinstance(debug_refs.get("diagnostics_events"), list) else []})
        write_json(out_dir / "debug_refs.json", debug_refs)
        if report_text:
            (out_dir / "contract_review_report.txt").write_text(report_text, encoding="utf-8")
        supervisor.update(
            status="completed",
            current_step="terminal.completed",
            session_id=session_id,
            matter_id=final_matter_id,
            next_action="inspect_summary",
            pending_card=pending_card,
            artifact_refs={
                "summary": str(out_dir / "summary.json"),
                "deliverables": str(out_dir / "deliverables.json"),
            },
            extra={
                "deliverable_keys": sorted(deliverables.keys()),
                "report_file_id": report_file_id,
            },
        )

    print("[done] contract review workflow completed")
    print(f"[artifacts] {out_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run contract review workflow via consultations WS (real LLM).")
    parser.add_argument("--base-url", default="", help="Gateway base URL, e.g. http://host/api/v1")
    parser.add_argument("--use-gateway", action="store_true", default=False, help="Use gateway mode instead of direct service URLs")
    parser.add_argument("--consultations-base-url", default="", help="Direct consultations-service base URL")
    parser.add_argument("--files-base-url", default="", help="Direct files-service base URL")
    parser.add_argument("--matter-base-url", default="", help="Direct matter-service base URL")
    parser.add_argument("--remote-stack-host", default="", help="Remote stack host for direct non-local services")
    parser.add_argument("--direct-user-id", default="", help="Optional direct service mode user id (skip auth only when set)")
    parser.add_argument("--direct-org-id", default="", help="Optional direct service mode organization id (skip auth only when set)")
    parser.add_argument("--direct-is-superuser", default="", help="Optional direct service mode superuser flag")
    parser.add_argument("--username", default="", help="Lawyer username")
    parser.add_argument("--password", default="", help="Lawyer password")
    parser.add_argument("--contract-file", default="", help="Contract file path (.docx/.txt)")
    parser.add_argument("--contract-type-id", default="construction", help="Default fixture contract_type_id when --contract-file is omitted")
    parser.add_argument("--kickoff", default=DEFAULT_KICKOFF, help="Initial user query")
    parser.add_argument("--kickoff-max-loops", type=int, default=24, help="kickoff max_loops")
    parser.add_argument("--max-steps", type=int, default=220, help="run_until max steps")
    parser.add_argument("--cards-only", action="store_true", default=False, help="Kickoff once, then only poll and answer cards")
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
