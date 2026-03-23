from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("E2E_STRICT_CARD_DRIVEN", "0")

from client.api_client import ApiClient
from support.workbench.flow_runner import auto_answer_card
from support.workbench.utils import unwrap_api_response


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect or step a workbench session")
    parser.add_argument("--session-id", required=True, help="Consultation session id")
    parser.add_argument("--resume-once", action="store_true", help="Auto-answer current pending card once")
    parser.add_argument("--chat-text", default="", help="Send one chat message to the session and print SSE response")
    parser.add_argument("--uploaded-file-id", action="append", default=[], help="Optional uploaded file ids for card auto-answer")
    parser.add_argument("--max-loops", type=int, default=12, help="Resume max_loops")
    return parser.parse_args()


def _first_dict(items: Any) -> dict[str, Any]:
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            return first
    return {}


async def main() -> None:
    args = _parse_args()
    load_dotenv(ROOT / ".env", override=False)
    base_url = str(os.getenv("BASE_URL", "http://localhost:18001/api/v1") or "http://localhost:18001/api/v1").rstrip("/")
    username = os.getenv("LAWYER_USERNAME", "lawyer1")
    password = os.getenv("LAWYER_PASSWORD", "lawyer123456")

    async with ApiClient(base_url) as client:
        await client.login(username, password)

        session_resp = await client.get_session(args.session_id)
        session = unwrap_api_response(session_resp)
        if not isinstance(session, dict):
            raise RuntimeError(f"unexpected session payload: {session_resp}")
        matter_id = str(session.get("matter_id") or "").strip()

        pending_resp = await client.get_pending_card(args.session_id)
        pending = unwrap_api_response(pending_resp)
        pending_card = pending if isinstance(pending, dict) and pending else None

        try:
            session_timeline_resp = await client.get_session_timeline(args.session_id, limit=10)
            session_timeline_data = unwrap_api_response(session_timeline_resp)
            raw_session_timeline = session_timeline_data.get("items") if isinstance(session_timeline_data, dict) else None
            session_timeline = [it for it in (raw_session_timeline or []) if isinstance(it, dict)]
        except httpx.HTTPStatusError:
            session_timeline = []

        try:
            session_trace_resp = await client.list_session_traces(args.session_id, limit=5)
            session_trace_data = unwrap_api_response(session_trace_resp)
            raw_session_traces = session_trace_data.get("traces") if isinstance(session_trace_data, dict) else None
            session_traces = [it for it in (raw_session_traces or []) if isinstance(it, dict)]
        except httpx.HTTPStatusError:
            session_traces = []

        phase_timeline: dict[str, Any] = {}
        deliverables: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        workflow_profile: dict[str, Any] = {}
        snapshot: dict[str, Any] = {}
        if matter_id:
            phase_resp = await client.get_matter_phase_timeline(matter_id)
            phase_data = unwrap_api_response(phase_resp)
            phase_timeline = phase_data if isinstance(phase_data, dict) else {}

            try:
                profile_resp = await client.get_workflow_profile(matter_id)
                profile_data = unwrap_api_response(profile_resp)
                workflow_profile = profile_data if isinstance(profile_data, dict) else {}
            except httpx.HTTPStatusError:
                workflow_profile = {}

            try:
                snapshot_resp = await client.get(f"/matter-service/lawyer/matters/{matter_id}/workbench/snapshot")
                snapshot_data = unwrap_api_response(snapshot_resp)
                snapshot = snapshot_data if isinstance(snapshot_data, dict) else {}
            except httpx.HTTPStatusError:
                snapshot = {}

            deliverable_resp = await client.list_deliverables(matter_id)
            deliverable_data = unwrap_api_response(deliverable_resp)
            raw_deliverables = deliverable_data.get("deliverables") if isinstance(deliverable_data, dict) else None
            deliverables = [it for it in (raw_deliverables or []) if isinstance(it, dict)]

            try:
                trace_resp = await client.list_traces(matter_id, limit=5)
                trace_data = unwrap_api_response(trace_resp)
                raw_traces = trace_data.get("traces") if isinstance(trace_data, dict) else None
                traces = [it for it in (raw_traces or []) if isinstance(it, dict)]
            except httpx.HTTPStatusError:
                traces = []

        summary = {
            "session_id": args.session_id,
            "session_status": str(session.get("status") or ""),
            "matter_id": matter_id,
            "current_phase": str(phase_timeline.get("current_phase") or phase_timeline.get("currentPhase") or ""),
            "pending_card": {
                "id": str((pending_card or {}).get("id") or (pending_card or {}).get("card_id") or ""),
                "skill_id": str((pending_card or {}).get("skill_id") or ""),
                "task_key": str((pending_card or {}).get("task_key") or ""),
                "review_type": str((pending_card or {}).get("review_type") or ""),
                "title": str((pending_card or {}).get("title") or ""),
                "prompt": str((pending_card or {}).get("prompt") or (pending_card or {}).get("message") or "")[:500],
                "questions": (pending_card or {}).get("questions") if isinstance((pending_card or {}).get("questions"), list) else [],
            },
            "latest_trace": {
                "id": str(_first_dict(traces).get("id") or ""),
                "node_id": str(_first_dict(traces).get("node_id") or _first_dict(traces).get("nodeId") or ""),
                "task_id": str(_first_dict(traces).get("task_id") or _first_dict(traces).get("taskId") or ""),
                "status": str(_first_dict(traces).get("status") or _first_dict(traces).get("state") or ""),
            },
            "latest_session_trace": {
                "id": str(_first_dict(session_traces).get("id") or ""),
                "node_id": str(_first_dict(session_traces).get("node_id") or _first_dict(session_traces).get("nodeId") or ""),
                "task_id": str(_first_dict(session_traces).get("task_id") or _first_dict(session_traces).get("taskId") or ""),
                "status": str(_first_dict(session_traces).get("status") or _first_dict(session_traces).get("state") or ""),
            },
            "latest_timeline_item": {
                "type": str(_first_dict(session_timeline).get("type") or _first_dict(session_timeline).get("event_type") or ""),
                "title": str(_first_dict(session_timeline).get("title") or ""),
                "content": str(_first_dict(session_timeline).get("content") or _first_dict(session_timeline).get("message") or "")[:300],
            },
            "workflow_profile": {
                "service_type_id": str(workflow_profile.get("service_type_id") or workflow_profile.get("serviceTypeId") or ""),
                "client_role": str(workflow_profile.get("client_role") or workflow_profile.get("clientRole") or ""),
                "cause_of_action_code": str(workflow_profile.get("cause_of_action_code") or workflow_profile.get("causeOfActionCode") or ""),
                "cause_of_action_name": str(workflow_profile.get("cause_of_action_name") or workflow_profile.get("causeOfActionName") or ""),
            },
            "analysis_state": {
                "current_task_id": str((((snapshot.get("analysis_state") or {}) if isinstance(snapshot.get("analysis_state"), dict) else {}).get("identity") or {}).get("current_task_id") or ""),
                "service_type_id": str((((snapshot.get("analysis_state") or {}) if isinstance(snapshot.get("analysis_state"), dict) else {}).get("case") or {}).get("profile", {}).get("service_type_id") or ""),
                "cause_of_action_code": str((((snapshot.get("analysis_state") or {}) if isinstance(snapshot.get("analysis_state"), dict) else {}).get("case") or {}).get("profile", {}).get("cause_of_action_code") or ""),
                "recommended_documents": ((((snapshot.get("analysis_state") or {}) if isinstance(snapshot.get("analysis_state"), dict) else {}).get("case") or {}).get("data", {}).get("work_product", {}).get("recommended_documents") or []),
            },
            "deliverable_keys": [
                str(it.get("output_key") or it.get("outputKey") or "")
                for it in deliverables
                if str(it.get("output_key") or it.get("outputKey") or "")
            ],
        }

        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

        if args.chat_text.strip():
            chat_resp = await client.chat(
                args.session_id,
                args.chat_text.strip(),
                attachments=[str(fid).strip() for fid in args.uploaded_file_id if str(fid).strip()],
                max_loops=max(1, int(args.max_loops)),
            )
            print("[inspect-session] chat response", flush=True)
            print(json.dumps(chat_resp, ensure_ascii=False, indent=2), flush=True)

        if not args.resume_once:
            return
        if not pending_card:
            print("[inspect-session] no pending card to resume", flush=True)
            return

        user_response = auto_answer_card(
            pending_card,
            overrides={
                "profile.cause_of_action_code": "private_lending",
                "cause_of_action_code": "private_lending",
                "profile.cause_of_action_name": "民间借贷纠纷",
                "cause_of_action_name": "民间借贷纠纷",
            },
            uploaded_file_ids=[str(fid).strip() for fid in args.uploaded_file_id if str(fid).strip()],
        )
        print("[inspect-session] resume payload", json.dumps(user_response, ensure_ascii=False), flush=True)
        resume_resp = await client.resume(
            args.session_id,
            user_response,
            pending_card=pending_card,
            max_loops=max(1, int(args.max_loops)),
        )
        print(json.dumps(resume_resp, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
