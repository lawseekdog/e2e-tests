from __future__ import annotations

import json

from scripts._support.run_status import (
    RunStatusSupervisor,
    format_run_status_line,
    resolve_status_path,
)


def test_resolve_status_path_from_directory(tmp_path) -> None:
    assert resolve_status_path(tmp_path) == tmp_path / "run_status.json"


def test_run_status_supervisor_writes_status_and_latest_payloads(tmp_path) -> None:
    supervisor = RunStatusSupervisor(out_dir=tmp_path, flow_id="analysis")

    supervisor.update(
        status="running",
        current_step="poll.analysis_readiness",
        session_id="session:1",
        matter_id="100",
        snapshot={
            "analysis_state": {
                "current_task_id": "references_refresh",
                "current_node": "references",
                "current_phase": "references",
            }
        },
        current_blocker="issues_ready",
        next_action="continue_poll",
        latest_payloads={"snapshot": {"foo": "bar"}},
        extra={"seen": 1},
    )

    payload = json.loads((tmp_path / "run_status.json").read_text(encoding="utf-8"))
    assert payload["contract_version"] == "live_run_status.v2"
    assert payload["flow_id"] == "analysis"
    assert payload["status"] == "running"
    assert payload["current_step"] == "poll.analysis_readiness"
    assert payload["session_id"] == "session:1"
    assert payload["matter_id"] == "100"
    assert payload["current_phase"] == "references"
    assert payload["current_node"] == "references"
    assert payload["current_task_id"] == "references_refresh"
    assert payload["current_blocker"] == "issues_ready"
    assert payload["next_action"] == "continue_poll"
    assert payload["artifacts"]["status_file"] == str(tmp_path / "run_status.json")
    assert json.loads((tmp_path / "snapshot.latest.json").read_text(encoding="utf-8")) == {"foo": "bar"}


def test_format_run_status_line_includes_key_progress_fields() -> None:
    line = format_run_status_line(
        {
            "flow_id": "analysis",
            "status": "running",
            "current_step": "poll.analysis_readiness",
            "session_id": "session:1",
            "matter_id": "100",
            "current_phase": "references",
            "current_node": "references",
            "current_blocker": "authority_missing",
            "next_action": "continue_poll",
        }
    )
    assert "flow=analysis" in line
    assert "status=running" in line
    assert "phase=references" in line
    assert "node=references" in line
    assert "blocker=authority_missing" in line
