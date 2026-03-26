from __future__ import annotations

from pathlib import Path

import pytest

import scripts._support.diagnostic_bundle_support as diagnostic_bundle_support
from scripts._support.diagnostic_bundle_support import export_failure_bundle, format_first_bad_line


def test_format_first_bad_line_renders_compact_summary() -> None:
    line = format_first_bad_line(
        {
            "first_bad_skill": "analysis-evidence-semantic-events",
            "first_bad_stage": "validate_raw",
            "failure_class": "contract_mismatch",
            "primary_reason_code": "fact_graph_draft_event_roles_missing",
            "bundle_dir": "/tmp/bundle",
        }
    )

    assert "FIRST_BAD" in line
    assert "skill=analysis-evidence-semantic-events" in line
    assert "reason=fact_graph_draft_event_roles_missing" in line


def test_export_failure_bundle_requires_failure_summary_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = Path("/Users/xiangxiansenzhangxiaojie/workspaces/lawseekdog")
    bundle_path = tmp_path / "bundle"
    bundle_path.mkdir(parents=True)
    (bundle_path / "failure_summary.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        diagnostic_bundle_support,
        "_export_bundle_via_ai_engine_runtime",
        lambda **kwargs: str(bundle_path),
    )

    with pytest.raises(RuntimeError, match="observability_contract_missing_reason_code"):
        export_failure_bundle(
            repo_root=repo_root,
            session_id="session:1",
            matter_id="1",
            reason="unit_test",
        )


def test_export_failure_bundle_requires_ai_engine_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)

    monkeypatch.setattr(
        diagnostic_bundle_support,
        "_resolve_ai_engine_python",
        lambda _repo_root: (_ for _ in ()).throw(RuntimeError("diagnostic_export_runtime_missing")),
    )

    with pytest.raises(RuntimeError, match="diagnostic_export_runtime_missing"):
        export_failure_bundle(
            repo_root=repo_root,
            session_id="session:2",
            matter_id="2",
            reason="unit_test",
        )
