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
    "review_scope": "full",
    "profile.contract_type": "建设工程施工合同",
    "profile.summary": "已征收闲置土地垃圾清运工程施工合同审查，重点关注付款条件、违约责任、争议解决与免责条款。",
}


def _read_timeout_env(name: str, default: float, *, min_value: float = 1.0) -> float:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    if value < min_value:
        return float(default)
    return value


def _read_int_env(name: str, default: int, *, min_value: int = 1) -> int:
    raw = str(os.getenv(name, "") or "").strip()
    if not raw:
        return int(default)
    try:
        value = int(raw)
    except ValueError:
        return int(default)
    if value < min_value:
        return int(default)
    return value


_WORKFLOW_ACTION_TIMEOUT_S = _read_timeout_env("E2E_WORKFLOW_ACTION_TIMEOUT_S", 1800.0)
_RESUME_STEP_TIMEOUT_S = _read_timeout_env("E2E_RESUME_STEP_TIMEOUT_S", 1800.0)
_CONTINUE_STEP_TIMEOUT_S = _read_timeout_env("E2E_CONTINUE_STEP_TIMEOUT_S", 180.0)
_CLAUSE_RESUME_MAX_LOOPS = _read_int_env("E2E_CONTRACT_REVIEW_CLAUSE_RESUME_MAX_LOOPS", 24)
_INTAKE_GATE_RESUME_MAX_LOOPS = _read_int_env("E2E_CONTRACT_REVIEW_INTAKE_GATE_RESUME_MAX_LOOPS", 4)
_CLAUSE_REPEAT_CARD_ABORT_COUNT = _read_int_env(
    "E2E_CONTRACT_REVIEW_REPEAT_CARD_ABORT_COUNT",
    6,
)


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
    clause_ids: list[str] | None = None,
) -> bool:
    if clause_ids is None:
        snapshot = await _fetch_snapshot(client, matter_id)
        view = _extract_contract_view(snapshot)
        clause_ids = _extract_clause_ids(view)
    accepted = clause_ids[: min(len(clause_ids), 8)]
    if not accepted:
        return False

    sse = await asyncio.wait_for(
        client.workflow_action(
            flow.session_id,
            workflow_action="contract_review_apply_decisions",
            workflow_action_params={
                "accepted_clause_ids": accepted,
                "ignored_clause_ids": [],
                "overrides": {},
                "regenerate_documents": True,
            },
            max_loops=36,
        ),
        timeout=_WORKFLOW_ACTION_TIMEOUT_S,
    )
    if isinstance(sse, dict):
        flow.last_sse = sse
        flow.seen_sse.append(sse)
    if is_session_busy_sse(sse if isinstance(sse, dict) else {}):
        return False
    return True


def _extract_clause_ids(contract_view: dict[str, Any] | None) -> list[str]:
    view = contract_view if isinstance(contract_view, dict) else {}
    clauses = view.get("clauses") if isinstance(view.get("clauses"), list) else []
    return [
        _safe_str(row.get("clause_id"))
        for row in clauses
        if isinstance(row, dict) and _safe_str(row.get("clause_id"))
    ]


def _is_resume_user_message_transient(exc: AssertionError) -> bool:
    return "SSE missing user_message" in _safe_str(exc)


async def _wait_for_clause_ids(
    *,
    client: ApiClient,
    flow: WorkbenchFlow,
    matter_id: str,
    max_polls: int = 360,
    poll_interval_s: float = 2.0,
    drive_every: int = 6,
    resume_max_loops: int = _CLAUSE_RESUME_MAX_LOOPS,
) -> list[str]:
    attempts = max(1, int(max_polls))
    interval = max(0.2, float(poll_interval_s))
    drive_interval = max(1, int(drive_every))
    last_card_signature = ""
    same_card_streak = 0
    intake_gate_rounds = 0
    last_card_skill = ""
    for idx in range(attempts):
        snapshot = await _fetch_snapshot(client, matter_id)
        clause_ids = _extract_clause_ids(_extract_contract_view(snapshot))
        if clause_ids:
            return clause_ids

        # Prefer explicit card resume over blind nudges.
        card = await flow.get_pending_card()
        if isinstance(card, dict) and card:
            skill_id = _safe_str(card.get("skill_id"))
            task_key = _safe_str(card.get("task_key"))
            last_card_skill = skill_id
            card_signature = f"{skill_id}:{task_key}"
            if card_signature and card_signature == last_card_signature:
                same_card_streak += 1
            else:
                last_card_signature = card_signature
                same_card_streak = 1

            resolved_resume_loops = max(1, int(resume_max_loops))
            if skill_id == "intake-gate-blocked":
                intake_gate_rounds += 1
                resolved_resume_loops = max(1, int(_INTAKE_GATE_RESUME_MAX_LOOPS))
            print(
                f"[clause_wait] resume_card skill={skill_id or '-'} task={task_key or '-'} "
                f"max_loops={resolved_resume_loops} repeat={same_card_streak} intake_round={intake_gate_rounds}"
            )

            if (
                skill_id == "intake-gate-blocked"
                and intake_gate_rounds >= max(1, int(_CLAUSE_REPEAT_CARD_ABORT_COUNT))
            ):
                analysis = snapshot.get("analysis_state") if isinstance(snapshot, dict) else {}
                phase = _safe_str(analysis.get("current_phase")) if isinstance(analysis, dict) else ""
                node = _safe_str(analysis.get("current_node")) if isinstance(analysis, dict) else ""
                raise AssertionError(
                    "Clause extraction stuck on repeated intake gate card "
                    f"(intake_round={intake_gate_rounds}, skill={skill_id}, task={task_key}, "
                    f"phase={phase or '-'}, node={node or '-'})."
                )

            try:
                await asyncio.wait_for(
                    flow.resume_card(card, max_loops=resolved_resume_loops),
                    timeout=_RESUME_STEP_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                print(
                    f"[clause_wait] resume_card timeout after {_RESUME_STEP_TIMEOUT_S:.1f}s; "
                    "continue polling"
                )
            except AssertionError as exc:
                if not _is_resume_user_message_transient(exc):
                    raise
                print(
                    "[clause_wait] resume_card missing user_message; "
                    "treat as in-flight and continue polling"
                )
        elif ((idx + 1) % drive_interval) == 0:
            # Some remote runs end the first stream with partial progress only.
            # Trigger a short "continue" round to move the workflow cursor.
            nudge_text = "继续"
            if last_card_skill == "intake-gate-blocked":
                nudge_text = _safe_str(FLOW_OVERRIDES.get("profile.summary")) or "请使用既有案件概述继续推进 intake。"
            try:
                sse = await asyncio.wait_for(
                    client.chat(flow.session_id, nudge_text, attachments=flow.uploaded_file_ids, max_loops=2),
                    timeout=_CONTINUE_STEP_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                print(
                    f"[clause_wait] continue timeout after {_CONTINUE_STEP_TIMEOUT_S:.1f}s; "
                    "treat as in-flight and keep polling"
                )
            else:
                if isinstance(sse, dict):
                    flow.last_sse = sse
                    flow.seen_sse.append(sse)
        await asyncio.sleep(interval)
    raise AssertionError(f"Failed to extract contract clauses after {attempts} polls (matter_id={matter_id})")


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
    start_images = _capture_runtime_images()
    if start_images:
        (out_dir / "runtime_images.start.json").write_text(json.dumps(start_images, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[runtime] start_images={start_images}")

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

        async def _wait_deliverables_ready_passive(
            *,
            max_polls: int,
            poll_interval_s: float = 2.0,
            drive_every: int = 5,
            resume_max_loops: int = _CLAUSE_RESUME_MAX_LOOPS,
        ) -> None:
            polls = max(1, int(max_polls))
            interval = max(0.2, float(poll_interval_s))
            drive_interval = max(1, int(drive_every))
            for idx in range(polls):
                if await _deliverables_ready(flow):
                    return

                card = await flow.get_pending_card()
                if isinstance(card, dict) and card:
                    skill_id = _safe_str(card.get("skill_id"))
                    task_key = _safe_str(card.get("task_key"))
                    resolved_resume_loops = max(1, int(resume_max_loops))
                    if skill_id == "skill-error-analysis" and "doc_draft" in task_key:
                        resolved_resume_loops = 1
                    if skill_id == "intake-gate-blocked":
                        resolved_resume_loops = max(1, int(_INTAKE_GATE_RESUME_MAX_LOOPS))
                    print(
                        f"[deliverables_wait] resume_card skill={skill_id or '-'} task={task_key or '-'} "
                        f"max_loops={resolved_resume_loops}"
                    )
                    try:
                        await asyncio.wait_for(
                            flow.resume_card(card, max_loops=resolved_resume_loops),
                            timeout=_RESUME_STEP_TIMEOUT_S,
                        )
                    except asyncio.TimeoutError:
                        print(
                            f"[deliverables_wait] resume_card timeout after {_RESUME_STEP_TIMEOUT_S:.1f}s; "
                            "treat as in-flight and keep polling"
                        )
                    except AssertionError as exc:
                        if not _is_resume_user_message_transient(exc):
                            raise
                        print(
                            "[deliverables_wait] resume_card missing user_message; "
                            "treat as in-flight and keep polling"
                        )
                elif ((idx + 1) % drive_interval) == 0:
                    try:
                        sse = await asyncio.wait_for(
                            client.chat(flow.session_id, "继续", attachments=flow.uploaded_file_ids, max_loops=2),
                            timeout=_CONTINUE_STEP_TIMEOUT_S,
                        )
                    except asyncio.TimeoutError:
                        print(
                            f"[deliverables_wait] continue timeout after {_CONTINUE_STEP_TIMEOUT_S:.1f}s; "
                            "treat as in-flight and keep polling"
                        )
                    else:
                        if isinstance(sse, dict):
                            flow.last_sse = sse
                            flow.seen_sse.append(sse)
                await asyncio.sleep(interval)
            raise AssertionError(
                "Failed to reach contract review deliverables ready "
                f"after {polls} passive polls (session_id={flow.session_id}, matter_id={flow.matter_id})"
            )

        if args.apply_decisions:
            await flow.refresh()
            mid = _safe_str(flow.matter_id)
            if not mid:
                raise RuntimeError("matter_id missing before contract_review_apply_decisions")
            clause_ids = await _wait_for_clause_ids(
                client=client,
                flow=flow,
                matter_id=mid,
                max_polls=max(60, int(args.max_steps) * 3),
                poll_interval_s=2.0,
            )
            applied = False
            for attempt in range(1, 7):
                applied = await _try_apply_clause_decisions(
                    client=client,
                    flow=flow,
                    matter_id=mid,
                    clause_ids=clause_ids,
                )
                print(f"[workflow_action] contract_review_apply_decisions attempt={attempt} applied={applied}")
                if applied:
                    break
                await asyncio.sleep(min(8.0, 1.5 * attempt))
            if not applied:
                post_action_snapshot = await _fetch_snapshot(client, mid) or {}
                post_action_analysis = (
                    post_action_snapshot.get("analysis_state")
                    if isinstance(post_action_snapshot, dict)
                    and isinstance(post_action_snapshot.get("analysis_state"), dict)
                    else {}
                )
                current_node = _safe_str(post_action_analysis.get("current_node")).lower()
                if current_node in {"contract_output", "doc_draft", "document_generation", "documents_finalize"}:
                    print(
                        "[workflow_action] contract_review_apply_decisions skipped "
                        f"(already running downstream node={current_node})"
                    )
                else:
                    raise RuntimeError("contract_review_apply_decisions failed after clauses became available")

        try:
            if args.apply_decisions:
                await _wait_deliverables_ready_passive(
                    max_polls=max(1, int(args.max_steps) * 3),
                    poll_interval_s=2.0,
                )
            else:
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
            "contract_view": {
                "overall_risk_level": _safe_str(contract_view.get("overall_risk_level")),
                "contract_type": _safe_str(contract_view.get("contract_type")),
                "summary_len": len(_safe_str(contract_view.get("summary"))),
                "clauses_count": len(contract_view.get("clauses")) if isinstance(contract_view.get("clauses"), list) else 0,
            },
            "seen_cards": len(flow.seen_cards),
            "seen_sse_rounds": len(flow.seen_sse),
            "runtime_images_start": start_images,
            "runtime_images_end": end_images,
            "runtime_images_stable": (start_images == end_images) if start_images and end_images else None,
        }

        if start_images and end_images and start_images != end_images and str(os.getenv("E2E_ALLOW_DEPLOYMENT_DRIFT", "") or "").strip() not in {"1", "true", "yes"}:
            raise RuntimeError(f"deployment_image_drift_detected: start={start_images} end={end_images}")

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
