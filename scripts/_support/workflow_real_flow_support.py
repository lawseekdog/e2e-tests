from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from client.api_client import ApiClient
from support.workbench.flow_runner import WorkbenchFlow
from support.workbench.utils import unwrap_api_response

_DEFAULT_REMOTE_STACK_HOST = "100.116.203.71"
_REMOTE_SERVICE_PORTS: dict[str, int] = {
    "auth": 18101,
    "user": 18113,
    "organization": 18110,
    "consultations": 18103,
    "files": 18104,
    "knowledge": 18106,
    "matter": 18107,
    "templates": 18112,
}


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


def terminate_stale_script_runs(*, script_name: str, current_pid: int | None = None, grace_seconds: float = 1.0) -> list[int]:
    token = safe_str(script_name)
    if not token:
        return []
    current = int(current_pid or os.getpid())
    try:
        raw = subprocess.check_output(["pgrep", "-af", token], text=True)
    except Exception:
        return []
    victims: list[int] = []
    for line in raw.splitlines():
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except Exception:
            continue
        if pid == current:
            continue
        cmd = parts[1] if len(parts) > 1 else ""
        if token not in cmd:
            continue
        victims.append(pid)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    if victims and grace_seconds > 0:
        time.sleep(max(0.1, float(grace_seconds)))
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    return victims


def api_url(host: str, port: int) -> str:
    return f"http://{host}:{int(port)}/api/v1"


def configure_direct_service_mode(
    *,
    remote_stack_host: str = "",
    consultations_base_url: str = "",
    matter_base_url: str = "",
    files_base_url: str = "",
    templates_base_url: str = "",
    auth_base_url: str = "",
    user_base_url: str = "",
    organization_base_url: str = "",
    knowledge_base_url: str = "",
    local_consultations: bool = True,
    local_matter: bool = True,
    local_templates: bool = False,
    direct_user_id: str = "",
    direct_org_id: str = "",
    direct_is_superuser: str = "",
) -> tuple[str, dict[str, str]]:
    host = (
        safe_str(remote_stack_host)
        or safe_str(os.getenv("LAWSEEKDOG_REMOTE_STACK_HOST"))
        or safe_str(os.getenv("REMOTE_STACK_HOST"))
        or _DEFAULT_REMOTE_STACK_HOST
    )

    resolved_auth = safe_str(auth_base_url) or safe_str(os.getenv("E2E_AUTH_BASE_URL")) or api_url(host, _REMOTE_SERVICE_PORTS["auth"])
    resolved_user = safe_str(user_base_url) or safe_str(os.getenv("E2E_USER_BASE_URL")) or api_url(host, _REMOTE_SERVICE_PORTS["user"])
    resolved_org = safe_str(organization_base_url) or safe_str(os.getenv("E2E_ORG_BASE_URL")) or api_url(host, _REMOTE_SERVICE_PORTS["organization"])
    resolved_consultations = (
        safe_str(consultations_base_url)
        or safe_str(os.getenv("E2E_CONSULTATIONS_BASE_URL"))
        or api_url("127.0.0.1", 18021)
        if local_consultations
        else api_url(host, _REMOTE_SERVICE_PORTS["consultations"])
    )
    resolved_matter = (
        safe_str(matter_base_url)
        or safe_str(os.getenv("E2E_MATTER_BASE_URL"))
        or api_url("127.0.0.1", 18020)
        if local_matter
        else api_url(host, _REMOTE_SERVICE_PORTS["matter"])
    )
    resolved_files = safe_str(files_base_url) or safe_str(os.getenv("E2E_FILES_BASE_URL")) or api_url(host, _REMOTE_SERVICE_PORTS["files"])
    resolved_knowledge = safe_str(knowledge_base_url) or safe_str(os.getenv("E2E_KNOWLEDGE_BASE_URL")) or api_url(host, _REMOTE_SERVICE_PORTS["knowledge"])
    resolved_templates = (
        safe_str(templates_base_url)
        or safe_str(os.getenv("E2E_TEMPLATES_BASE_URL"))
        or (api_url("127.0.0.1", 18022) if local_templates else api_url(host, _REMOTE_SERVICE_PORTS["templates"]))
    )

    os.environ["E2E_AUTH_BASE_URL"] = resolved_auth
    os.environ["E2E_USER_BASE_URL"] = resolved_user
    os.environ["E2E_ORG_BASE_URL"] = resolved_org
    os.environ["E2E_CONSULTATIONS_BASE_URL"] = resolved_consultations
    os.environ["E2E_MATTER_BASE_URL"] = resolved_matter
    os.environ["E2E_FILES_BASE_URL"] = resolved_files
    os.environ["E2E_KNOWLEDGE_BASE_URL"] = resolved_knowledge
    os.environ["E2E_TEMPLATES_BASE_URL"] = resolved_templates

    uid = safe_str(direct_user_id) or safe_str(os.getenv("E2E_DIRECT_USER_ID")) or "2"
    oid = safe_str(direct_org_id) or safe_str(os.getenv("E2E_DIRECT_ORG_ID")) or "1"
    superuser = safe_str(direct_is_superuser) or safe_str(os.getenv("E2E_DIRECT_IS_SUPERUSER")) or "false"
    if uid:
        os.environ["E2E_DIRECT_USER_ID"] = uid
    else:
        os.environ.pop("E2E_DIRECT_USER_ID", None)
    if oid:
        os.environ["E2E_DIRECT_ORG_ID"] = oid
    else:
        os.environ.pop("E2E_DIRECT_ORG_ID", None)
    if superuser:
        os.environ["E2E_DIRECT_IS_SUPERUSER"] = superuser
    else:
        os.environ.pop("E2E_DIRECT_IS_SUPERUSER", None)

    config = {
        "remote_stack_host": host,
        "auth_base_url": resolved_auth,
        "user_base_url": resolved_user,
        "organization_base_url": resolved_org,
        "consultations_base_url": resolved_consultations,
        "matter_base_url": resolved_matter,
        "files_base_url": resolved_files,
        "knowledge_base_url": resolved_knowledge,
        "templates_base_url": resolved_templates,
        "direct_user_id": uid,
        "direct_org_id": oid,
    }
    return resolved_consultations, config


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


async def collect_ai_debug_refs(
    client: ApiClient,
    *,
    repo_root: Path,
    session_id: str,
    matter_id: str = "",
) -> dict[str, Any]:
    session_token = safe_str(session_id)
    matter_token = safe_str(matter_id)
    thread_id = session_token if session_token.startswith("session:") else (f"session:{session_token}" if session_token else "")
    internal_api_key = safe_str(os.getenv("INTERNAL_API_KEY"))
    summary: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    if thread_id and internal_api_key:
        headers = {**client.headers, "X-Internal-Api-Key": internal_api_key}
        try:
            resp = await client.get(
                "/ai-platform-service/internal/ai/diagnostics/summary",
                params={"thread_id": thread_id},
                headers=headers,
            )
            payload = unwrap_api_response(resp)
            summary = payload.get("summary") if isinstance(payload, dict) and isinstance(payload.get("summary"), dict) else {}
        except Exception as exc:  # noqa: BLE001
            errors["diagnostics_summary"] = str(exc)
        try:
            resp = await client.get(
                "/ai-platform-service/internal/ai/diagnostics/events",
                params={"thread_id": thread_id, "limit": 20},
                headers=headers,
            )
            payload = unwrap_api_response(resp)
            rows = payload.get("events") if isinstance(payload, dict) else None
            events = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        except Exception as exc:  # noqa: BLE001
            errors["diagnostics_events"] = str(exc)

    bundle_dir = (repo_root / "output" / "ai-debug-bundles" / thread_id).resolve() if thread_id else None
    bundle_refs: list[str] = []
    if bundle_dir and bundle_dir.exists():
        for rel in ("failure_summary.json", "execution_traces.json", "timeline.json", "node_trace_timeline.json"):
            target = bundle_dir / rel
            if target.exists():
                bundle_refs.append(str(target))
        for rel_dir in ("skill_stages", "contract_diffs", "llm_calls", "states"):
            target_dir = bundle_dir / rel_dir
            if target_dir.exists():
                bundle_refs.append(str(target_dir))

    return {
        "thread_id": thread_id,
        "session_id": session_token or None,
        "matter_id": matter_token or None,
        "diagnostics_summary": summary,
        "diagnostics_events": events,
        "bundle_dir": str(bundle_dir) if bundle_dir else "",
        "bundle_refs": bundle_refs,
        "errors": errors,
    }


__all__ = [
    "api_url",
    "bootstrap_flow",
    "collect_ai_debug_refs",
    "configure_direct_service_mode",
    "event_counts",
    "fetch_workbench_snapshot",
    "is_goal_completion_card",
    "list_deliverables",
    "list_session_messages",
    "load_real_flow_env",
    "resolve_output_dir",
    "safe_str",
    "terminate_stale_script_runs",
    "upload_consultation_files",
    "write_json",
]
