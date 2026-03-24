from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from client.api_client import ApiClient
from support.workbench.flow_runner import WorkbenchFlow
from support.workbench.utils import unwrap_api_response


def safe_str(value: Any) -> str:
    return str(value or "").strip()


def event_counts(sse: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    events = sse.get("events") if isinstance(sse.get("events"), list) else []
    for row in events:
        if not isinstance(row, dict):
            continue
        name = safe_str(row.get("event")) or "unknown"
        out[name] = int(out.get(name) or 0) + 1
    return out


def load_real_flow_env(*, repo_root: Path, e2e_root: Path) -> None:
    load_dotenv(repo_root / ".env", override=False)
    load_dotenv(e2e_root / ".env", override=False)


def resolve_output_dir(*, repo_root: Path, output_dir: str, default_leaf: str) -> Path:
    path = Path(output_dir).expanduser() if safe_str(output_dir) else repo_root / default_leaf
    resolved = path.resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


async def upload_consultation_files(client: ApiClient, paths: list[Path]) -> list[str]:
    uploaded_file_ids: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        upload = await client.upload_file(str(path), purpose="consultation")
        file_id = safe_str(((upload.get("data") or {}) if isinstance(upload, dict) else {}).get("id"))
        if file_id:
            uploaded_file_ids.append(file_id)
    return uploaded_file_ids


async def bootstrap_flow(
    *,
    client: ApiClient,
    service_type_id: str,
    client_role: str,
    uploaded_file_ids: list[str],
    overrides: dict[str, Any],
    strict_card_driven: bool = True,
) -> tuple[WorkbenchFlow, str, str]:
    sess = await client.create_session(
        service_type_id=service_type_id,
        client_role=client_role,
        file_ids=uploaded_file_ids,
    )
    sess_data = (sess.get("data") if isinstance(sess, dict) else {}) or {}
    session_id = safe_str(sess_data.get("id"))
    matter_id = safe_str(sess_data.get("matter_id"))
    if not session_id:
        raise RuntimeError(f"create_session failed: {sess}")
    flow = WorkbenchFlow(
        client=client,
        session_id=session_id,
        uploaded_file_ids=uploaded_file_ids,
        overrides=dict(overrides),
        strict_card_driven=bool(strict_card_driven),
        matter_id=matter_id or None,
    )
    return flow, session_id, matter_id


async def fetch_workbench_snapshot(client: ApiClient, matter_id: str) -> dict[str, Any] | None:
    try:
        resp = await client.get(f"/matter-service/lawyer/matters/{matter_id}/workbench/snapshot")
    except Exception:
        return None
    payload = unwrap_api_response(resp)
    return payload if isinstance(payload, dict) else None


async def list_session_messages(client: ApiClient, session_id: str) -> list[dict[str, Any]]:
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


async def list_deliverables(client: ApiClient, matter_id: str) -> dict[str, dict[str, Any]]:
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
        key = safe_str(row.get("output_key"))
        if key:
            out[key] = row
    return out


def is_goal_completion_card(card: dict[str, Any] | None) -> bool:
    if not isinstance(card, dict):
        return False
    if safe_str(card.get("skill_id")).lower() != "goal-completion":
        return False
    for row in (card.get("questions") if isinstance(card.get("questions"), list) else []):
        if not isinstance(row, dict):
            continue
        if safe_str(row.get("field_key")) == "data.workbench.goal":
            return True
    return False


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = [
    "bootstrap_flow",
    "event_counts",
    "fetch_workbench_snapshot",
    "is_goal_completion_card",
    "list_deliverables",
    "list_session_messages",
    "load_real_flow_env",
    "resolve_output_dir",
    "safe_str",
    "upload_consultation_files",
    "write_json",
]
