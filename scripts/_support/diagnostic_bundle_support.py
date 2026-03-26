from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _safe_str(value: Any) -> str:
    return str(value or "").strip()


def _bootstrap_ai_engine_imports(repo_root: Path) -> None:
    for path in ((repo_root / "ai-engine").resolve(), (repo_root / "shared-libs").resolve()):
        token = str(path)
        if path.exists() and token not in sys.path:
            sys.path.insert(0, token)


def export_failure_bundle(
    *,
    repo_root: Path,
    session_id: str = "",
    matter_id: str = "",
    reason: str,
    current_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _bootstrap_ai_engine_imports(repo_root)
    from src.infrastructure.debug.runtime_debug import export_debug_bundle

    bundle_dir = export_debug_bundle(
        thread_id=_safe_str(session_id) or None,
        session_id=_safe_str(session_id) or None,
        matter_id=_safe_str(matter_id) or None,
        current_state=current_state if isinstance(current_state, dict) else None,
        reason=_safe_str(reason) or "e2e_failure",
    )
    summary_path = Path(bundle_dir) / "failure_summary.json"
    if not summary_path.exists():
        raise RuntimeError("observability_contract_missing_reason_code")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not _safe_str(summary.get("primary_reason_code")) or not _safe_str(summary.get("failure_class")):
        raise RuntimeError("observability_contract_missing_reason_code")
    return {"bundle_dir": bundle_dir, "summary": summary}


def format_first_bad_line(summary: dict[str, Any]) -> str:
    return (
        "FIRST_BAD "
        f"skill={_safe_str(summary.get('first_bad_skill')) or '-'} "
        f"stage={_safe_str(summary.get('first_bad_stage')) or '-'} "
        f"class={_safe_str(summary.get('failure_class')) or '-'} "
        f"reason={_safe_str(summary.get('primary_reason_code')) or '-'} "
        f"bundle={_safe_str(summary.get('bundle_dir')) or '-'}"
    )
