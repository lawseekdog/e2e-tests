from __future__ import annotations

from pathlib import Path
import sys

import pytest

from scripts._support.diagnostic_bundle_support import export_failure_bundle, format_first_bad_line


def test_format_first_bad_line_renders_compact_summary() -> None:
    line = format_first_bad_line(
        {
            "first_bad_skill": "analysis-evidence-event-binding",
            "first_bad_stage": "validate_raw",
            "failure_class": "contract_mismatch",
            "primary_reason_code": "fact_graph_draft_event_roles_missing",
            "bundle_dir": "/tmp/bundle",
        }
    )

    assert "FIRST_BAD" in line
    assert "skill=analysis-evidence-event-binding" in line
    assert "reason=fact_graph_draft_event_roles_missing" in line


def test_export_failure_bundle_requires_failure_summary_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = Path("/Users/xiangxiansenzhangxiaojie/workspaces/lawseekdog")
    for path in (repo_root / "ai-engine", repo_root / "shared-libs"):
        token = str(path)
        if token not in sys.path:
            sys.path.insert(0, token)
    import src.infrastructure.debug.runtime_debug as runtime_debug

    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir(parents=True)
    (bundle_path / "failure_summary.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(runtime_debug, "export_debug_bundle", lambda **kwargs: str(bundle_path))

    with pytest.raises(RuntimeError, match="observability_contract_missing_reason_code"):
        export_failure_bundle(
            repo_root=repo_root,
            session_id="session:1",
            matter_id="1",
            reason="unit_test",
        )
