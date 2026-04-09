from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv
import httpx
from httpx import HTTPStatusError

from client.api_client import ApiClient
from support.workbench.flow_runner import WorkbenchFlow
from support.workbench.utils import unwrap_api_response

_DEFAULT_REMOTE_STACK_HOST = "8.148.207.157"
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
_DEFAULT_LOCAL_CONSULTATIONS_PORT = 18021
_DEFAULT_LOCAL_MATTER_PORT = 18020
_DEFAULT_LOCAL_TEMPLATES_PORT = 18022
_DEFAULT_LOCAL_AI_ENGINE_V2_PORT = 18086
_DEFAULT_REMOTE_AI_ENGINE_V2_PORT = 18114


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


def _find_script_run_matches(*, script_name: str, current_pid: int | None = None) -> list[tuple[int, str]]:
    token = safe_str(script_name)
    if not token:
        return []
    current = int(current_pid or os.getpid())
    try:
        raw = subprocess.check_output(["pgrep", "-af", token], text=True)
    except Exception:
        return []
    matches: list[tuple[int, str]] = []
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
        matches.append((pid, cmd))
    return matches


def terminate_stale_script_runs(*, script_name: str, current_pid: int | None = None, grace_seconds: float = 1.0) -> list[int]:
    matches = _find_script_run_matches(script_name=script_name, current_pid=current_pid)
    victims = [pid for pid, _cmd in matches]
    if victims:
        print(
            f"[cleanup] stale {script_name} pids={victims}",
            flush=True,
        )
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
    survivors = _find_script_run_matches(script_name=script_name, current_pid=current_pid)
    if survivors:
        survivor_pids = [pid for pid, _cmd in survivors]
        survivor_cmds = [cmd for _pid, cmd in survivors]
        print(
            f"[cleanup] stale {script_name} survivors={survivor_pids}",
            flush=True,
        )
        raise RuntimeError(
            f"残留脚本未清干净: {script_name} pids={survivor_pids} cmds={survivor_cmds}"
        )
    if victims:
        print(
            f"[cleanup] cleared {script_name} pids={victims}",
            flush=True,
        )
    return victims


def api_url(host: str, port: int) -> str:
    return f"http://{host}:{int(port)}/api/v1"


def _local_service_port(env_key: str, default: int) -> int:
    token = safe_str(os.getenv(env_key))
    try:
        port = int(token)
    except Exception:
        return int(default)
    return port if port > 0 else int(default)


def _local_service_api_url(env_key: str, default: int) -> str:
    return api_url("127.0.0.1", _local_service_port(env_key, default))


def _normalize_ai_engine_base_url(raw: Any) -> str:
    token = safe_str(raw).rstrip("/")
    if token.endswith("/api/v1"):
        return token[: -len("/api/v1")]
    return token


def _candidate_ai_engine_base_urls() -> tuple[str, ...]:
    remote_host = (
        safe_str(os.getenv("LAWSEEKDOG_REMOTE_STACK_HOST"))
        or safe_str(os.getenv("REMOTE_STACK_HOST"))
        or _DEFAULT_REMOTE_STACK_HOST
    )
    ordered: list[str] = []
    for raw in (
        os.getenv("AI_ENGINE_V2_BASE_URL"),
        os.getenv("AI_ENGINE_V2_LOCAL_BASE_URL"),
        os.getenv("AI_PLATFORM_URL"),
        f"http://127.0.0.1:{_DEFAULT_LOCAL_AI_ENGINE_V2_PORT}",
        f"http://{remote_host}:{_DEFAULT_REMOTE_AI_ENGINE_V2_PORT}",
    ):
        normalized = _normalize_ai_engine_base_url(raw)
        if normalized and normalized not in ordered:
            ordered.append(normalized)
    return tuple(ordered)


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
        or (
            _local_service_api_url("LOCAL_CONSULTATIONS_PORT", _DEFAULT_LOCAL_CONSULTATIONS_PORT)
            if local_consultations
            else (
                safe_str(os.getenv("E2E_CONSULTATIONS_BASE_URL"))
                or api_url(host, _REMOTE_SERVICE_PORTS["consultations"])
            )
        )
    )
    resolved_matter = (
        safe_str(matter_base_url)
        or (
            _local_service_api_url("LOCAL_MATTER_PORT", _DEFAULT_LOCAL_MATTER_PORT)
            if local_matter
            else (
                safe_str(os.getenv("E2E_MATTER_BASE_URL"))
                or api_url(host, _REMOTE_SERVICE_PORTS["matter"])
            )
        )
    )
    resolved_files = safe_str(files_base_url) or safe_str(os.getenv("E2E_FILES_BASE_URL")) or api_url(host, _REMOTE_SERVICE_PORTS["files"])
    resolved_knowledge = safe_str(knowledge_base_url) or safe_str(os.getenv("KNOWLEDGE_SERVICE_URL")) or api_url(host, _REMOTE_SERVICE_PORTS["knowledge"])
    resolved_templates = (
        safe_str(templates_base_url)
        or (
            _local_service_api_url("LOCAL_TEMPLATES_PORT", _DEFAULT_LOCAL_TEMPLATES_PORT)
            if local_templates
            else (
                safe_str(os.getenv("E2E_TEMPLATES_BASE_URL"))
                or api_url(host, _REMOTE_SERVICE_PORTS["templates"])
            )
        )
    )

    os.environ["E2E_AUTH_BASE_URL"] = resolved_auth
    os.environ["E2E_USER_BASE_URL"] = resolved_user
    os.environ["E2E_ORG_BASE_URL"] = resolved_org
    os.environ["E2E_CONSULTATIONS_BASE_URL"] = resolved_consultations
    os.environ["E2E_MATTER_BASE_URL"] = resolved_matter
    os.environ["E2E_FILES_BASE_URL"] = resolved_files
    os.environ["KNOWLEDGE_SERVICE_URL"] = resolved_knowledge
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


def _flatten_profile_override_patch(overrides: dict[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if not isinstance(overrides, dict):
        return patch
    name_aliases = {
        "plaintiff.name": "plaintiff",
        "defendant.name": "defendant",
        "appellant.name": "appellant",
        "appellee.name": "appellee",
        "applicant.name": "applicant",
        "respondent.name": "respondent",
        "suspect.name": "suspect",
    }
    for raw_key, value in overrides.items():
        key = safe_str(raw_key)
        if not key or value is None or not key.startswith("profile."):
            continue
        leaf = safe_str(key[len("profile."):])
        if not leaf:
            continue
        if leaf in {"service_type_id", "decisions"} or leaf.startswith("decisions."):
            continue
        alias = name_aliases.get(leaf)
        if alias:
            patch.setdefault(alias, value)
            continue
        if "." in leaf:
            continue
        patch[leaf] = value
    return patch


def _default_goal_from_service_dictionary(service_dictionary: dict[str, Any], *, service_type_id: str) -> str:
    want = safe_str(service_type_id)
    if not want or not isinstance(service_dictionary, dict):
        return ""
    for key in ("resolved_service_types", "service_types"):
        rows = service_dictionary.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if safe_str(row.get("id")) != want:
                continue
            goal = safe_str(row.get("default_goal"))
            if goal:
                return goal
    return safe_str(service_dictionary.get("default_goal"))


async def preseed_workflow_profile(
    client: ApiClient,
    *,
    matter_id: str,
    service_type_id: str,
    client_role: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    mid = safe_str(matter_id)
    st = safe_str(service_type_id)
    role = safe_str(client_role)
    patch = _flatten_profile_override_patch(overrides)
    if not mid or not st or not patch:
        return {}

    ui_dict_raw = unwrap_api_response(await client.get_matter_ui_dictionary())
    ui_dict = ui_dict_raw if isinstance(ui_dict_raw, dict) else {}
    dictionary_version = safe_str(ui_dict.get("dictionary_version"))
    dictionary_hash = safe_str(ui_dict.get("dictionary_hash"))
    service_dictionary = ui_dict.get("service_dictionary") if isinstance(ui_dict.get("service_dictionary"), dict) else {}
    if not dictionary_version:
        dictionary_version = safe_str(service_dictionary.get("dictionary_version"))
    if not dictionary_hash:
        dictionary_hash = safe_str(service_dictionary.get("dictionary_hash"))
    if not dictionary_version or not dictionary_hash:
        raise RuntimeError("workflow_profile_preseed_missing_dictionary_metadata")

    try:
        current_profile_raw = unwrap_api_response(await client.get_workflow_profile(mid))
    except HTTPStatusError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
        current_profile_raw = {}
    current_profile = current_profile_raw if isinstance(current_profile_raw, dict) else {}
    goal = safe_str(current_profile.get("goal")) or _default_goal_from_service_dictionary(service_dictionary, service_type_id=st)
    if not goal:
        raise RuntimeError(f"workflow_profile_preseed_missing_goal:{st}")

    payload = {
        "service_type_id": st,
        "client_role": role,
        "goal": goal,
        "decisions": {},
        "routing": {},
        "skill_execution": {},
        "diagnostics": {
            "dictionary_version": dictionary_version,
            "dictionary_hash": dictionary_hash,
            "intake_profile": patch,
        },
    }
    try:
        await client.sync_matter_workflow_all(mid, payload)
    except HTTPStatusError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
    return patch


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
    preseed_profile: bool = True,
    strict_card_driven: bool = True,
    progress_observer: Any = None,
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
    if matter_id and preseed_profile:
        await preseed_workflow_profile(
            client,
            matter_id=matter_id,
            service_type_id=service_type_id,
            client_role=client_role,
            overrides=overrides,
        )
    flow = WorkbenchFlow(
        client=client,
        session_id=session_id,
        uploaded_file_ids=uploaded_file_ids,
        overrides=dict(overrides),
        strict_card_driven=bool(strict_card_driven),
        matter_id=matter_id or None,
        progress_observer=progress_observer,
    )
    return flow, session_id, matter_id


async def fetch_workbench_snapshot(client: ApiClient, matter_id: str) -> dict[str, Any] | None:
    try:
        resp = await client.get(f"/matter-service/lawyer/matters/{matter_id}/workbench/snapshot")
    except Exception:
        return None
    payload = unwrap_api_response(resp)
    return payload if isinstance(payload, dict) else None


async def fetch_execution_snapshot_by_session(session_id: str) -> dict[str, Any] | None:
    session_token = safe_str(session_id)
    if not session_token:
        return None
    thread_id = session_token if session_token.startswith("session:") else f"session:{session_token}"
    thread_token = quote(thread_id, safe="")
    headers = {"Accept": "application/json"}
    internal_api_key = safe_str(os.getenv("INTERNAL_API_KEY"))
    if internal_api_key:
        headers["X-Internal-Api-Key"] = internal_api_key
    timeout_s = max(5.0, float(os.getenv("E2E_HTTP_REQUEST_TIMEOUT_S", "45") or 45))
    async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as raw_client:
        for base_url in _candidate_ai_engine_base_urls():
            url = f"{base_url}/api/v1/internal/executions/by-thread/{thread_token}/snapshot"
            try:
                response = await raw_client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
            except Exception:
                continue
            data = unwrap_api_response(payload)
            if isinstance(data, dict) and data:
                return data
    return None


async def fetch_execution_traces_by_session(
    session_id: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    session_token = safe_str(session_id)
    if not session_token:
        return []
    thread_id = session_token if session_token.startswith("session:") else f"session:{session_token}"
    thread_token = quote(thread_id, safe="")
    headers = {"Accept": "application/json"}
    internal_api_key = safe_str(os.getenv("INTERNAL_API_KEY"))
    if internal_api_key:
        headers["X-Internal-Api-Key"] = internal_api_key
    timeout_s = max(5.0, float(os.getenv("E2E_HTTP_REQUEST_TIMEOUT_S", "45") or 45))
    async with httpx.AsyncClient(timeout=timeout_s, trust_env=False) as raw_client:
        for base_url in _candidate_ai_engine_base_urls():
            url = f"{base_url}/api/v1/internal/executions/by-thread/{thread_token}/traces"
            try:
                response = await raw_client.get(url, params={"limit": max(1, int(limit))}, headers=headers)
                response.raise_for_status()
                payload = response.json()
            except Exception:
                continue
            data = unwrap_api_response(payload)
            traces = data.get("traces") if isinstance(data, dict) and isinstance(data.get("traces"), list) else []
            rows = [row for row in traces if isinstance(row, dict)]
            if rows:
                return rows
    return []


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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


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
    summary: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    bundle_dir = (repo_root / "output" / "ai-debug-bundles" / thread_id).resolve() if thread_id else None
    bundle_refs: list[str] = []

    if bundle_dir and bundle_dir.exists():
        failure_summary = _read_json(bundle_dir / "failure_summary.json")
        diagnosis = _read_json(bundle_dir / "diagnosis.json")
        timeline = _read_json(bundle_dir / "timeline.json")
        trace_payload = _read_json(bundle_dir / "execution_traces.json")
        entries = [row for row in (timeline.get("entries") if isinstance(timeline.get("entries"), list) else []) if isinstance(row, dict)]
        traces = [row for row in (trace_payload.get("traces") if isinstance(trace_payload.get("traces"), list) else []) if isinstance(row, dict)]
        summary = diagnosis or {
            "thread_id": thread_id,
            "session_id": session_token,
            "matter_id": matter_token,
            "run_id": safe_str(failure_summary.get("run_id")) or safe_str(timeline.get("run_id")),
            "quality_status": safe_str(failure_summary.get("quality_status")),
            "quality_summary": safe_str(failure_summary.get("quality_summary")),
            "summary": safe_str(failure_summary.get("summary")),
            "event_count": len(entries),
            "trace_count": len(traces),
        }
        events = [row for row in entries[-20:] if isinstance(row, dict)]

        for rel in ("failure_summary.json", "diagnosis.json", "execution_traces.json", "timeline.json", "node_trace_timeline.json"):
            target = bundle_dir / rel
            if target.exists():
                bundle_refs.append(str(target))
        for rel_dir in ("skill_stages", "contract_diffs", "llm_calls", "states"):
            target_dir = bundle_dir / rel_dir
            if target_dir.exists():
                bundle_refs.append(str(target_dir))
        quality_summary = bundle_dir / "quality" / "reports" / "summary.json"
        if quality_summary.exists():
            bundle_refs.append(str(quality_summary))
        quality_dir = bundle_dir / "quality"
        if quality_dir.exists():
            bundle_refs.append(str(quality_dir))

    if not summary and thread_id:
        errors["diagnostics_summary"] = "bundle_missing"
    if not events and thread_id:
        errors["diagnostics_events"] = "bundle_missing"

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
    "fetch_execution_snapshot_by_session",
    "fetch_execution_traces_by_session",
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
