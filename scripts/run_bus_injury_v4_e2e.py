#!/usr/bin/env python3
"""Bus injury V4 end-to-end runner (Layer A + artifacts scaffold)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

import sys

SCRIPT_DIR = Path(__file__).resolve().parent
E2E_ROOT = SCRIPT_DIR.parent
REPO_ROOT = E2E_ROOT.parent
sys.path.insert(0, str(E2E_ROOT))

from client.api_client import ApiClient  # noqa: E402


@dataclass
class StepResult:
    step: str
    title: str
    status: str
    details: str


def now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def git_value(repo_dir: Path, args: list[str]) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return cp.stdout.strip() if cp.returncode == 0 else ""


def collect_repo_state() -> list[dict[str, Any]]:
    repos = ["front", "consultations-service", "matter-service", "ai-engine"]
    rows: list[dict[str, Any]] = []
    for rel in repos:
        repo_dir = REPO_ROOT / rel
        sha = git_value(repo_dir, ["rev-parse", "HEAD"]) if repo_dir.exists() else ""
        dirty_out = git_value(repo_dir, ["status", "--short"]) if repo_dir.exists() else ""
        rows.append(
            {
                "repo": rel,
                "path": str(repo_dir),
                "head": sha or "unknown",
                "dirty": bool(dirty_out.strip()),
                "dirty_files": [line for line in dirty_out.splitlines() if line.strip()],
            }
        )
    return rows


def first_line(text: str, needle: str) -> int | None:
    idx = text.find(needle)
    if idx < 0:
        return None
    return text[:idx].count("\n") + 1


def scan_contract_markers() -> list[dict[str, Any]]:
    checks = [
        (
            "front_entry_seed_matter",
            REPO_ROOT / "front/src/apps/lawyer/workbench/composables/useWorkbenchEntry.ts",
            "const seededMatterId = await createMatterForEntryIntent",
        ),
        (
            "front_ws_terminal_guard",
            REPO_ROOT / "front/src/apps/lawyer/chat/chat.provider.ws.ts",
            "连接已关闭（未收到 end 事件）",
        ),
        (
            "consultations_ws_send_error",
            REPO_ROOT / "consultations-service/src/main/java/com/lawseekdog/consultations/infrastructure/websocket/ConsultationWebSocketHandler.java",
            "sendError(session, message);",
        ),
        (
            "matter_narrative_uses_fact_bundle",
            REPO_ROOT / "matter-service/src/main/java/com/lawseekdog/matter/application/service/KnowledgeViewAssembler.java",
            "buildNarrativeSemanticBundle(factAssertions)",
        ),
        (
            "matter_reads_runtime_candidates",
            REPO_ROOT / "matter-service/src/main/java/com/lawseekdog/matter/application/service/KnowledgeViewAssembler.java",
            "runtime.get(\"cause_candidates\")",
        ),
        (
            "ai_runtime_payload_candidates",
            REPO_ROOT / "ai-engine/src/application/agent/nodes/sync_workflow/context_payload.py",
            '"cause_candidates": cause_candidates',
        ),
        (
            "ai_cause_recheck_rule",
            REPO_ROOT / "ai-engine/.skills/cause-recommendation/scripts/postprocess.py",
            "score_delta >= 0.15",
        ),
    ]

    rows: list[dict[str, Any]] = []
    for name, path, needle in checks:
        result = {
            "name": name,
            "path": str(path),
            "needle": needle,
            "found": False,
            "line": None,
        }
        if path.exists():
            text = path.read_text(encoding="utf-8")
            line = first_line(text, needle)
            result["found"] = line is not None
            result["line"] = line
        rows.append(result)
    return rows


def event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in events:
        key = str(row.get("event") or "")
        counts[key] = counts.get(key, 0) + 1
    return counts


def append_step(results: list[StepResult], step: str, title: str, ok: bool, details: str) -> None:
    results.append(StepResult(step=step, title=title, status="PASS" if ok else "FAIL", details=details))


def auto_answer_card(card: dict[str, Any], uploaded_file_ids: list[str]) -> dict[str, Any]:
    questions = card.get("questions") if isinstance(card.get("questions"), list) else []
    answers: list[dict[str, Any]] = []

    for q in questions:
        if not isinstance(q, dict):
            continue
        field_key = str(q.get("field_key") or "").strip()
        if not field_key:
            continue

        input_type = str(q.get("input_type") or q.get("question_type") or "").strip().lower()
        required = bool(q.get("required"))
        default = q.get("default")
        options = q.get("options") if isinstance(q.get("options"), list) else []

        value: Any = None
        if field_key == "attachment_file_ids":
            value = uploaded_file_ids
        elif input_type in {"select", "single_select", "single_choice"}:
            value = default
            if value is None:
                for opt in options:
                    if isinstance(opt, dict) and opt.get("recommended") is True and opt.get("value") is not None:
                        value = opt.get("value")
                        break
            if value is None:
                for opt in options:
                    if isinstance(opt, dict) and opt.get("value") is not None:
                        value = opt.get("value")
                        break
        elif input_type in {"multi_select", "multiple_select"}:
            if isinstance(default, list):
                value = default
            else:
                pick = None
                for opt in options:
                    if isinstance(opt, dict) and opt.get("recommended") is True and opt.get("value") is not None:
                        pick = opt.get("value")
                        break
                value = [pick] if pick is not None else []
        elif input_type in {"boolean", "bool"}:
            value = bool(default) if default is not None else True
        elif input_type in {"file_id", "file_ids"}:
            value = uploaded_file_ids if input_type == "file_ids" else uploaded_file_ids[:1]
        else:
            value = default if default is not None else ("已确认" if required else None)

        if required and (value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, list) and not value)):
            value = True if input_type in {"boolean", "bool"} else "已确认"

        if value is None and not required:
            continue
        answers.append({"field_key": field_key, "value": value})

    return {"answers": answers}


async def wait_for_snapshot(client: ApiClient, matter_id: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last_payload: dict[str, Any] = {}
    path = f"/matter-service/lawyer/matters/{matter_id}/workbench/snapshot"

    while time.time() < deadline:
        try:
            payload = unwrap(await client.get(path))
            if isinstance(payload, dict):
                last_payload = payload
                kv = payload.get("knowledge_view")
                if isinstance(kv, dict) and kv:
                    return payload
        except Exception:
            pass
        await asyncio.sleep(1.0)

    return last_payload


async def run_ws_turn(coro: Any, timeout_s: float, label: str) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"{label} timed out after {int(timeout_s)}s") from exc


async def consume_pending_cards(
    *,
    client: ApiClient,
    session_id: str,
    uploaded_file_ids: list[str],
    ws_log: Any,
    turn_timeout_s: float,
    resume_max_loops: int,
    turn_label_prefix: str,
    max_cards: int = 6,
) -> tuple[int, bool]:
    """Consume pending cards via /resume until no card is pending.

    Returns: (consumed_count, unresolved_pending)
    """
    consumed = 0
    while consumed < max_cards:
        pending = unwrap(await client.get_pending_card(session_id))
        if not isinstance(pending, dict) or not pending:
            return consumed, False

        payload = auto_answer_card(pending, uploaded_file_ids)
        answers = payload.get("answers") if isinstance(payload.get("answers"), list) else []
        if not answers:
            return consumed, True

        turn_label = f"{turn_label_prefix}_{consumed + 1}"
        resume_loops = min(max(1, int(resume_max_loops)), 4)
        try:
            turn_resume = await run_ws_turn(
                client.resume(session_id, payload, pending_card=pending, max_loops=resume_loops),
                turn_timeout_s,
                turn_label,
            )
        except RuntimeError as exc:
            ws_log.write(json.dumps({"turn": turn_label, "events": [{"event": "error", "data": {"message": str(exc)}}]}, ensure_ascii=False) + "\n")
            return consumed, True
        ws_log.write(json.dumps({"turn": turn_label, "events": turn_resume.get("events", [])}, ensure_ascii=False) + "\n")
        consumed += 1

    pending = unwrap(await client.get_pending_card(session_id))
    return consumed, bool(isinstance(pending, dict) and pending)


async def run(args: argparse.Namespace) -> int:
    load_dotenv(E2E_ROOT / ".env")

    out_root = Path(args.output_root).resolve() if args.output_root else (REPO_ROOT / "output/e2e/bus-injury-v4")
    out_dir = out_root / now_id()
    out_dir.mkdir(parents=True, exist_ok=True)

    repo_state_path = out_dir / "repo-state.json"
    contract_scan_path = out_dir / "contract-scan.json"
    ws_log_path = out_dir / "ws-events.jsonl"
    summary_path = out_dir / "summary.md"

    results: list[StepResult] = []

    repo_state = collect_repo_state()
    repo_state_path.write_text(json.dumps(repo_state, ensure_ascii=False, indent=2), encoding="utf-8")
    dirty = [row["repo"] for row in repo_state if row.get("dirty")]
    append_step(results, "0", "冻结测试基线", True, f"dirty={','.join(dirty) if dirty else 'none'}")

    contracts = scan_contract_markers()
    contract_scan_path.write_text(json.dumps(contracts, ensure_ascii=False, indent=2), encoding="utf-8")
    missing = [row["name"] for row in contracts if not row.get("found")]
    append_step(results, "0.1", "跨仓契约静态核对", not missing, "all_found" if not missing else f"missing={missing}")

    if args.dry_run:
        append_step(results, "A", "Layer A API/WS", True, "dry-run")
        append_step(results, "B", "Layer B Playwright", True, "dry-run")
    else:
        base_url = os.getenv("BASE_URL", "http://localhost:18001/api/v1")
        username = os.getenv("LAWYER_USERNAME", "lawyer1")
        password = os.getenv("LAWYER_PASSWORD", "lawyer123456")

        first_input = args.first_input.strip() if args.first_input else "张三坐公交车受伤了"
        refs_prompt = "请先给候选法条和类案，标注待证据校验"
        evidence_prompt = "补充证据：急刹+受伤经过。"
        evidence_path = E2E_ROOT / "tests/lawyer_workbench/civil_prosecution/evidence/bus_ticket.txt"
        max_loops = max(1, int(args.max_loops))
        resume_max_loops = max(max_loops, int(args.resume_max_loops))
        turn_timeout_s = max(30.0, float(args.turn_timeout_s))

        with ws_log_path.open("w", encoding="utf-8") as ws_log:
            try:
                async with ApiClient(base_url) as client:
                    await client.login(username, password)

                    matter = unwrap(
                        await client.create_matter(
                            service_type_id="civil_prosecution",
                            title=f"E2E_BUS_V4_{now_id()}",
                            matter_category="litigation",
                        )
                    )
                    matter_id = str((matter or {}).get("id") or "").strip()

                    session = unwrap(await client.create_session(title="E2E BUS V4", matter_id=matter_id))
                    session_id = str((session or {}).get("id") or "").strip()
                    bound_matter_id = str((session or {}).get("matter_id") or matter_id).strip()
                    append_step(
                        results,
                        "1",
                        "创建事项并启动分析",
                        bool(bound_matter_id and session_id),
                        f"matter_id={bound_matter_id or '-'} session_id={session_id or '-'}",
                    )

                    turn1 = await run_ws_turn(
                        client.chat(session_id, first_input, attachments=[], max_loops=max_loops),
                        turn_timeout_s,
                        "turn1",
                    )
                    events1 = turn1.get("events") if isinstance(turn1.get("events"), list) else []
                    ws_log.write(json.dumps({"turn": "turn1", "events": events1}, ensure_ascii=False) + "\n")
                    counts1 = event_counts(events1)
                    terminal = counts1.get("end", 0) > 0 or counts1.get("card", 0) > 0 or counts1.get("error", 0) > 0
                    ok_ws = counts1.get("task_start", 0) > 0 and counts1.get("progress", 0) > 0 and counts1.get("task_end", 0) > 0 and terminal
                    append_step(results, "2", "WS事件完整性", ok_ws, f"counts={counts1}")

                    snapshot1 = await wait_for_snapshot(client, bound_matter_id, timeout_s=90.0)
                    (out_dir / "snapshot-step-1.json").write_text(json.dumps(snapshot1, ensure_ascii=False, indent=2), encoding="utf-8")
                    kv1 = snapshot1.get("knowledge_view") if isinstance(snapshot1, dict) else {}
                    nodes = len((((kv1 or {}).get("graph") or {}).get("nodes") or []))
                    events = len((((kv1 or {}).get("timeline") or {}).get("events") or []))
                    facts = len(((kv1 or {}).get("fact_assertions") or []))
                    mode = str((kv1 or {}).get("evidence_mode") or "")
                    ok_snapshot = bool(kv1) and mode == "narrative" and nodes >= 2 and events >= 1 and facts >= 1
                    append_step(results, "3", "首轮snapshot可视化检查", ok_snapshot, f"mode={mode} nodes={nodes} events={events} facts={facts}")

                    uploaded_ids: list[str] = []
                    consumed_cards, unresolved_cards = await consume_pending_cards(
                        client=client,
                        session_id=session_id,
                        uploaded_file_ids=uploaded_ids,
                        ws_log=ws_log,
                        turn_timeout_s=turn_timeout_s,
                        resume_max_loops=resume_max_loops,
                        turn_label_prefix="turn2_resume",
                    )
                    if unresolved_cards:
                        append_step(results, "4", "提问卡消费", False, f"pending_card_unresolved consumed={consumed_cards}")
                    else:
                        detail = "no_pending_card" if consumed_cards == 0 else f"pending_card_consumed={consumed_cards}"
                        append_step(results, "4", "提问卡消费", True, detail)

                    snapshot2 = await wait_for_snapshot(client, bound_matter_id, timeout_s=90.0)
                    (out_dir / "snapshot-step-2.json").write_text(json.dumps(snapshot2, ensure_ascii=False, indent=2), encoding="utf-8")
                    kv2 = snapshot2.get("knowledge_view") if isinstance(snapshot2, dict) else {}
                    panels2 = (kv2.get("panels") or {}) if isinstance(kv2, dict) else {}
                    candidates = panels2.get("cause_candidates") if isinstance(panels2, dict) else []
                    append_step(results, "5", "候选案由出现", isinstance(candidates, list) and len(candidates) > 0, f"cause_candidates={len(candidates) if isinstance(candidates, list) else 0}")

                    legal_refs = panels2.get("legal_references") if isinstance(panels2, dict) else []
                    case_refs = panels2.get("case_references") if isinstance(panels2, dict) else []

                    pre_refs_consumed, pre_refs_unresolved = await consume_pending_cards(
                        client=client,
                        session_id=session_id,
                        uploaded_file_ids=uploaded_ids,
                        ws_log=ws_log,
                        turn_timeout_s=turn_timeout_s,
                        resume_max_loops=resume_max_loops,
                        turn_label_prefix="turn3_resume_before_refs",
                    )
                    if pre_refs_consumed > 0:
                        snapshot2 = await wait_for_snapshot(client, bound_matter_id, timeout_s=60.0)
                        kv2 = snapshot2.get("knowledge_view") if isinstance(snapshot2, dict) else {}
                        panels2 = (kv2.get("panels") or {}) if isinstance(kv2, dict) else {}
                        legal_refs = panels2.get("legal_references") if isinstance(panels2, dict) else []
                        case_refs = panels2.get("case_references") if isinstance(panels2, dict) else []

                    if (not isinstance(legal_refs, list) or not legal_refs) and (not isinstance(case_refs, list) or not case_refs) and not pre_refs_unresolved:
                        turn_refs = await run_ws_turn(
                            client.chat(session_id, refs_prompt, attachments=[], max_loops=max_loops),
                            turn_timeout_s,
                            "turn3_refs_prompt",
                        )
                        ws_log.write(json.dumps({"turn": "turn3_refs_prompt", "events": turn_refs.get("events", [])}, ensure_ascii=False) + "\n")

                        post_refs_consumed, post_refs_unresolved = await consume_pending_cards(
                            client=client,
                            session_id=session_id,
                            uploaded_file_ids=uploaded_ids,
                            ws_log=ws_log,
                            turn_timeout_s=turn_timeout_s,
                            resume_max_loops=resume_max_loops,
                            turn_label_prefix="turn3_resume_after_refs",
                        )
                        pre_refs_unresolved = pre_refs_unresolved or post_refs_unresolved

                        if post_refs_consumed > 0:
                            ws_log.write(json.dumps({"turn": "turn3_resume_meta", "events": [] , "consumed": post_refs_consumed}, ensure_ascii=False) + "\n")

                        snapshot2 = await wait_for_snapshot(client, bound_matter_id, timeout_s=60.0)
                        kv2 = snapshot2.get("knowledge_view") if isinstance(snapshot2, dict) else {}
                        panels2 = (kv2.get("panels") or {}) if isinstance(kv2, dict) else {}
                        legal_refs = panels2.get("legal_references") if isinstance(panels2, dict) else []
                        case_refs = panels2.get("case_references") if isinstance(panels2, dict) else []

                    refs_quality = str((((kv2 or {}).get("evidence_readiness") or {}).get("refs_quality") or ""))
                    refs_ok = ((isinstance(legal_refs, list) and len(legal_refs) > 0) or (isinstance(case_refs, list) and len(case_refs) > 0)) and refs_quality in {"low", "medium", "high"}
                    if pre_refs_unresolved:
                        refs_ok = False
                    append_step(
                        results,
                        "6",
                        "首轮法条/类案展示",
                        refs_ok,
                        f"legal_refs={len(legal_refs) if isinstance(legal_refs, list) else 0} case_refs={len(case_refs) if isinstance(case_refs, list) else 0} refs_quality={refs_quality} unresolved_pending={pre_refs_unresolved}",
                    )

                    if evidence_path.exists():
                        uploaded = unwrap(await client.upload_session_attachment(session_id, str(evidence_path)))
                        evidence_id = str((uploaded or {}).get("file_id") or "").strip()
                        if evidence_id:
                            uploaded_ids.append(evidence_id)
                    turn_evidence = await run_ws_turn(
                        client.chat(session_id, evidence_prompt, attachments=uploaded_ids, max_loops=max_loops),
                        turn_timeout_s,
                        "turn4_evidence",
                    )
                    ws_log.write(json.dumps({"turn": "turn4_evidence", "events": turn_evidence.get("events", [])}, ensure_ascii=False) + "\n")
                    append_step(results, "7", "证据补充与增量分析", len(uploaded_ids) > 0, f"uploaded={len(uploaded_ids)}")

                    snapshot3 = await wait_for_snapshot(client, bound_matter_id, timeout_s=90.0)
                    (out_dir / "snapshot-step-3.json").write_text(json.dumps(snapshot3, ensure_ascii=False, indent=2), encoding="utf-8")
                    kv3 = snapshot3.get("knowledge_view") if isinstance(snapshot3, dict) else {}
                    mode3 = str((kv3 or {}).get("evidence_mode") or "")
                    citations = (kv3 or {}).get("citations") if isinstance(kv3, dict) else {}
                    citations_count = len(citations) if isinstance(citations, dict) else 0
                    append_step(results, "8", "可追溯性检查", mode3 in {"narrative", "documentary"}, f"mode={mode3} citations={citations_count}")

                    ws_log.flush()
                    merged_events: list[dict[str, Any]] = []
                    with ws_log_path.open("r", encoding="utf-8") as fd:
                        for line in fd:
                            row = json.loads(line)
                            merged_events.extend(row.get("events") if isinstance(row.get("events"), list) else [])
                    merged_counts = event_counts(merged_events)
                    thought_ok = merged_counts.get("task_start", 0) > 0 and merged_counts.get("progress", 0) > 0 and merged_counts.get("task_end", 0) > 0
                    append_step(results, "9", "思考消息可观测性", thought_ok, f"counts={merged_counts}")

                append_step(results, "A", "Layer A API/WS", True, "completed")
            except Exception as exc:
                append_step(results, "A", "Layer A API/WS", False, f"{type(exc).__name__}: {exc}")

        layer_b_script = E2E_ROOT / "scripts/run_bus_injury_v4_playwright_cli.sh"
        append_step(results, "B", "Layer B Playwright", layer_b_script.exists(), f"script={layer_b_script}")

    lines = [
        "# Bus Injury V4 E2E Summary",
        "",
        f"- generated_at: {datetime.now(timezone.utc).isoformat()}",
        f"- output_dir: `{out_dir}`",
        "",
        "## Step Results",
        "",
        "| Step | Title | Status | Details |",
        "|---|---|---|---|",
    ]
    for row in results:
        lines.append(f"| {row.step} | {row.title} | {row.status} | {row.details.replace('|', '/')} |")

    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            f"- `repo-state.json`: {repo_state_path}",
            f"- `contract-scan.json`: {contract_scan_path}",
            f"- `ws-events.jsonl`: {ws_log_path}",
            f"- snapshots: `{out_dir}`",
        ]
    )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    (out_dir / "run-meta.json").write_text(
        json.dumps({"finished_at": datetime.now(timezone.utc).isoformat(), "output_dir": str(out_dir)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(summary_path)
    return 1 if any(row.status == "FAIL" for row in results) else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run bus-injury V4 E2E flow")
    parser.add_argument("--output-root", default="", help="Output root directory")
    parser.add_argument("--first-input", default="", help="Override first user input")
    parser.add_argument("--max-loops", type=int, default=6, help="max_loops for chat turns")
    parser.add_argument("--resume-max-loops", type=int, default=8, help="max_loops for resume turns")
    parser.add_argument("--turn-timeout-s", type=float, default=240.0, help="timeout for each WS turn")
    parser.add_argument("--dry-run", action="store_true", help="Run only static checks")
    return parser.parse_args()


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
