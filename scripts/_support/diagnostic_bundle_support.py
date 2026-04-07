from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _resolve_ai_engine_python(repo_root: Path) -> Path:
    candidates = (
        repo_root / "ai-engine-v2" / ".venv" / "bin" / "python",
        repo_root / "ai-engine-v2" / ".venv312" / "bin" / "python",
        repo_root / ".venv" / "bin" / "python",
        repo_root / "ai-engine" / ".venv312" / "bin" / "python",
        repo_root / "ai-engine" / ".venv" / "bin" / "python",
    )
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    raise RuntimeError("diagnostic_export_runtime_missing")


def _resolve_ai_engine_env_file(repo_root: Path) -> Path:
    configured = _safe_str(os.getenv("AI_ENGINE_V2_ENV_FILE"))
    candidates = tuple(
        path
        for path in (
            Path(configured).expanduser() if configured else None,
            repo_root / "ai-engine-v2" / ".local" / "local-ai-engine-stack.env",
            repo_root / "infra-live" / ".local" / "aliyun-remote.env",
            repo_root / "infra-live" / ".local" / "env.local",
        )
        if path is not None
    )
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    raise RuntimeError("diagnostic_export_env_missing")


def _resolve_ai_engine_export_script(repo_root: Path) -> Path:
    candidates = (
        repo_root / "ai-engine-v2" / "scripts" / "export_debug_bundle.py",
    )
    for path in candidates:
        if path.exists() and path.is_file():
            return path
    raise RuntimeError("diagnostic_export_script_missing")


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = str(raw_line or "").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        token = _safe_str(key)
        if token:
            out[token] = value.strip()
    return out


def _thread_id_from_session(session_id: str) -> str:
    token = _safe_str(session_id)
    if not token:
        return ""
    if token.startswith("session:"):
        return token
    return f"session:{token}"


def _export_bundle_via_ai_engine_runtime(
    *,
    repo_root: Path,
    session_id: str,
    matter_id: str,
    reason: str,
) -> str:
    python_bin = _resolve_ai_engine_python(repo_root)
    env_file = _resolve_ai_engine_env_file(repo_root)
    script_path = _resolve_ai_engine_export_script(repo_root)
    command = [
        str(python_bin),
        str(script_path),
        "--reason",
        _safe_str(reason) or "e2e_failure",
    ]
    thread_id = _thread_id_from_session(session_id)
    if thread_id:
        command.extend(["--thread-id", thread_id])
    if _safe_str(session_id):
        command.extend(["--session-id", _safe_str(session_id)])
    if _safe_str(matter_id):
        command.extend(["--matter-id", _safe_str(matter_id)])
    env = os.environ.copy()
    env.update(_load_env_file(env_file))
    env["AI_ENGINE_V2_ENV_FILE"] = str(env_file)
    result = subprocess.run(
        command,
        cwd=script_path.parent.parent,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        detail = _safe_str(result.stderr) or _safe_str(result.stdout) or f"exit={result.returncode}"
        raise RuntimeError(f"diagnostic_export_runtime_failed:{detail}")
    bundle_dir = _safe_str(result.stdout).splitlines()
    bundle_path = _safe_str(bundle_dir[-1] if bundle_dir else "")
    if not bundle_path:
        raise RuntimeError("diagnostic_export_runtime_failed:missing_bundle_dir")
    return bundle_path


def export_failure_bundle(
    *,
    repo_root: Path,
    session_id: str = "",
    matter_id: str = "",
    reason: str,
    current_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if current_state:
        raise RuntimeError("diagnostic_export_inline_state_unsupported")
    bundle_dir = _export_bundle_via_ai_engine_runtime(
        repo_root=repo_root,
        session_id=_safe_str(session_id),
        matter_id=_safe_str(matter_id),
        reason=_safe_str(reason) or "e2e_failure",
    )
    summary_path = Path(bundle_dir) / "failure_summary.json"
    if not summary_path.exists():
        raise RuntimeError("observability_contract_missing_reason_code")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not _safe_str(summary.get("primary_reason_code")) or not _safe_str(summary.get("failure_class")):
        raise RuntimeError("observability_contract_missing_reason_code")
    return {"bundle_dir": bundle_dir, "summary": summary}


def export_observability_bundle(
    *,
    repo_root: Path,
    session_id: str = "",
    matter_id: str = "",
    reason: str,
) -> dict[str, Any]:
    bundle_dir = _export_bundle_via_ai_engine_runtime(
        repo_root=repo_root,
        session_id=_safe_str(session_id),
        matter_id=_safe_str(matter_id),
        reason=_safe_str(reason) or "e2e_observability",
    )
    summary_path = Path(bundle_dir) / "failure_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    return {"bundle_dir": bundle_dir, "summary": summary if isinstance(summary, dict) else {}}


def format_first_bad_line(summary: dict[str, Any]) -> str:
    regressions = [
        _safe_str(item)
        for item in (summary.get("focus_regressions") if isinstance(summary.get("focus_regressions"), list) else [])
        if _safe_str(item)
    ]
    return (
        "FIRST_BAD "
        f"node={_safe_str(summary.get('first_bad_node')) or '-'} "
        f"focus_node={_safe_str(summary.get('first_bad_focus_node')) or '-'} "
        f"class={_safe_str(summary.get('failure_class')) or '-'} "
        f"reason={_safe_str(summary.get('primary_reason_code')) or '-'} "
        f"regressions={','.join(regressions) or '-'} "
        f"bundle={_safe_str(summary.get('bundle_dir')) or '-'}"
    )
