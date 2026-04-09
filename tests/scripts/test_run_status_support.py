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
            "current_phase_name": "法律依据检索",
            "analysis_state": {
                "current_task_id": "references_refresh",
                "current_node": "references",
                "current_phase": "references",
                "current_subgraph": "references",
            }
        },
        execution_snapshot={
            "status": "running",
            "current_phase_id": "references",
            "current_phase_name": "法律依据检索",
            "workflow": {
                "status": "running",
                "current_phase_id": "references",
                "current_phase_label": "法律依据检索",
                "current_node": "judge_authority_relevance",
                "current_subgraph": "references",
                "phases": [
                    {"phase_id": "grounding", "label": "事实整理", "status": "completed"},
                    {"phase_id": "support", "label": "支持关系整理", "status": "completed"},
                    {"phase_id": "references", "label": "法律依据检索", "status": "running"},
                ],
            },
        },
        execution_traces=[
            {
                "node_id": "civil_litigation_program:grounding",
                "node_name": "grounding",
                "phase": "grounding",
                "status": "completed",
            },
            {
                "node_id": "civil_litigation_program:support",
                "node_name": "build_support_targets",
                "phase": "support",
                "status": "running",
            },
        ],
        pending_card={"skill_id": "reference-grounding", "task_key": "references.grounding"},
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
    assert payload["current_phase_label"] == "法律依据检索"
    assert payload["current_subgraph"] == "references"
    assert payload["execution_status"] == "running"
    assert payload["last_completed_phase"] == "support"
    assert payload["current_node"] == "references"
    assert payload["current_task_id"] == "references_refresh"
    assert payload["current_blocker"] == "issues_ready"
    assert payload["next_action"] == "continue_poll"
    assert payload["artifacts"]["status_file"] == str(tmp_path / "run_status.json")
    assert json.loads((tmp_path / "snapshot.latest.json").read_text(encoding="utf-8")) == {"foo": "bar"}
    assert payload["execution_traces_digest"]["trace_count"] == 2


def test_format_run_status_line_includes_key_progress_fields() -> None:
    line = format_run_status_line(
        {
            "flow_id": "analysis",
            "status": "running",
            "current_step": "poll.analysis_readiness",
            "session_id": "session:1",
            "matter_id": "100",
            "execution_status": "running",
            "current_phase": "references",
            "current_phase_label": "法律依据检索",
            "current_subgraph": "references",
            "current_node": "references",
            "last_completed_phase": "support",
            "pending_card": {"skill_id": "reference-grounding"},
            "current_blocker": "authority_missing",
            "next_action": "continue_poll",
        }
    )
    assert "flow=analysis" in line
    assert "status=running" in line
    assert "exec=running" in line
    assert "phase=references" in line
    assert "phase_name=法律依据检索" in line
    assert "subgraph=references" in line
    assert "node=references" in line
    assert "last_ok=support" in line
    assert "pending=reference-grounding" in line
    assert "blocker=authority_missing" in line


def test_run_status_uses_traces_when_execution_snapshot_is_still_planning(tmp_path) -> None:
    supervisor = RunStatusSupervisor(out_dir=tmp_path, flow_id="analysis")

    supervisor.update(
        status="running",
        current_step="poll.analysis_readiness",
        session_id="session:2",
        matter_id="200",
        execution_snapshot={
            "status": "planning",
            "current_phase_id": "",
            "current_phase_name": "",
            "workflow": {
                "status": "planning",
                "current_phase_id": "",
                "current_phase_label": "",
                "current_node": "",
                "current_subgraph": "",
                "phases": [],
            },
        },
        execution_traces=[
            {
                "node_id": "civil_litigation_program:grounding",
                "node_name": "grounding_subgraph",
                "phase": "grounding",
                "status": "completed",
            },
            {
                "node_id": "civil_litigation_program:support",
                "node_name": "build_support_targets",
                "phase": "support",
                "status": "running",
            },
        ],
        current_blocker="analysis_not_ready",
        next_action="continue_poll",
    )

    payload = json.loads((tmp_path / "run_status.json").read_text(encoding="utf-8"))
    assert payload["execution_status"] == "planning"
    assert payload["current_phase"] == "support"
    assert payload["current_subgraph"] == "support"
    assert payload["current_node"] == "build_support_targets"
    assert payload["last_completed_phase"] == "grounding"


def test_run_status_derives_phase_from_trace_node_id_when_phase_field_missing(tmp_path) -> None:
    supervisor = RunStatusSupervisor(out_dir=tmp_path, flow_id="analysis")

    supervisor.update(
        status="running",
        current_step="poll.analysis_readiness",
        session_id="session:3",
        matter_id="300",
        execution_snapshot={
            "status": "planning",
            "workflow": {"status": "planning", "phases": []},
        },
        execution_traces=[
            {
                "node_id": "civil_litigation_program:grounding",
                "node_name": "extract_legal_facts",
                "status": "completed",
            },
            {
                "node_id": "civil_litigation_program:support",
                "node_name": "build_support_targets",
                "status": "running",
            },
        ],
    )

    payload = json.loads((tmp_path / "run_status.json").read_text(encoding="utf-8"))
    assert payload["current_phase"] == "support"
    assert payload["current_subgraph"] == "support"
    assert payload["current_node"] == "build_support_targets"
    assert payload["last_completed_phase"] == "grounding"
