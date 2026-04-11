from __future__ import annotations

import json
import pytest

from scripts._support.run_status import (
    RunStatusSupervisor,
    format_run_status_line,
    resolve_status_path,
)

pytestmark = pytest.mark.skip_seed_bootstrap


def test_resolve_status_path_from_directory(tmp_path) -> None:
    assert resolve_status_path(tmp_path) == tmp_path / "run_status.json"


def test_run_status_supervisor_writes_status_and_latest_payloads(tmp_path) -> None:
    supervisor = RunStatusSupervisor(out_dir=tmp_path, flow_id="analysis")

    supervisor.update(
        status="running",
        progress_label="poll.analysis_readiness",
        session_id="session:1",
        matter_id="100",
        snapshot={
            "analysis_state": {
                "current_task_id": "dispute_analysis_authority_bundle",
                "current_node": "dispute_analysis_authority_bundle",
                "current_subgraph": "dispute_analysis_authority_bundle",
            }
        },
        execution_snapshot={
            "status": "running",
            "workflow": {
                "status": "running",
                "current_node": "judge_authority_relevance",
                "current_subgraph": "references",
                "phases": [
                    {"phase_id": "dispute_analysis_source_pack", "label": "事实与来源整理", "status": "completed"},
                    {"phase_id": "dispute_analysis_matter_frame", "label": "事项框架整理", "status": "completed"},
                    {"phase_id": "dispute_analysis_authority_bundle", "label": "法律依据检索", "status": "running", "current": True},
                ],
            },
        },
        execution_traces=[
            {
                "node_id": "dispute_analysis_program:dispute_analysis_source_pack",
                "node_name": "dispute_analysis_source_pack",
                "phase": "dispute_analysis_source_pack",
                "status": "completed",
            },
            {
                "node_id": "dispute_analysis_program:dispute_analysis_matter_frame",
                "node_name": "dispute_analysis_matter_frame",
                "phase": "dispute_analysis_matter_frame",
                "status": "running",
            },
        ],
        blocker_card={"type": "awaiting_review", "interruption_id": "awaiting_review:dispute_analysis_authority_bundle", "reason_code": "retrieval_low_coverage"},
        current_blocker={
            "type": "awaiting_review",
            "interruption_id": "awaiting_review:dispute_analysis_authority_bundle",
            "reason_code": "retrieval_low_coverage",
            "summary": "issues_ready",
        },
        next_action="continue_poll",
        latest_payloads={"snapshot": {"foo": "bar"}},
        extra={"seen": 1},
    )

    payload = json.loads((tmp_path / "run_status.json").read_text(encoding="utf-8"))
    assert payload["contract_version"] == "live_run_status.v2"
    assert payload["flow_id"] == "analysis"
    assert payload["status"] == "running"
    assert payload["progress_label"] == "poll.analysis_readiness"
    assert payload["session_id"] == "session:1"
    assert payload["matter_id"] == "100"
    assert payload["phase_id"] == "dispute_analysis_authority_bundle"
    assert payload["phase_label"] == "法律依据检索"
    assert payload["current_subgraph"] == "dispute_analysis_authority_bundle"
    assert payload["execution_status"] == "running"
    assert payload["last_completed_phase"] == "dispute_analysis_matter_frame"
    assert payload["current_node"] == "dispute_analysis_authority_bundle"
    assert payload["current_task_id"] == "dispute_analysis_authority_bundle"
    assert payload["current_blocker"] == {
        "type": "awaiting_review",
        "interruption_id": "awaiting_review:dispute_analysis_authority_bundle",
        "reason_code": "retrieval_low_coverage",
        "summary": "issues_ready",
    }
    assert payload["blocker_card"] == {
        "interruption_id": "awaiting_review:dispute_analysis_authority_bundle",
        "type": "awaiting_review",
        "reason_kind": "",
        "reason_code": "retrieval_low_coverage",
        "question_count": 0,
    }
    assert payload["next_action"] == "continue_poll"
    assert payload["artifacts"]["status_file"] == str(tmp_path / "run_status.json")
    assert json.loads((tmp_path / "snapshot.latest.json").read_text(encoding="utf-8")) == {"foo": "bar"}
    assert payload["execution_traces_digest"]["trace_count"] == 2


def test_format_run_status_line_includes_key_progress_fields() -> None:
    line = format_run_status_line(
        {
            "flow_id": "analysis",
            "status": "running",
            "progress_label": "poll.analysis_readiness",
            "session_id": "session:1",
            "matter_id": "100",
            "execution_status": "running",
            "phase_id": "dispute_analysis_authority_bundle",
            "phase_label": "法律依据检索",
            "current_subgraph": "dispute_analysis_authority_bundle",
            "current_node": "dispute_analysis_authority_bundle",
            "last_completed_phase": "dispute_analysis_matter_frame",
            "blocker_card": {"interruption_id": "blocked:authority", "type": "blocked", "reason_code": "authority_missing"},
            "current_blocker": {"type": "blocked", "interruption_id": "blocked:authority", "reason_code": "authority_missing", "summary": "authority_missing"},
            "next_action": "continue_poll",
        }
    )
    assert "flow=analysis" in line
    assert "status=running" in line
    assert "exec=running" in line
    assert "phase=dispute_analysis_authority_bundle" in line
    assert "phase_name=法律依据检索" in line
    assert "subgraph=dispute_analysis_authority_bundle" in line
    assert "node=dispute_analysis_authority_bundle" in line
    assert "last_ok=dispute_analysis_matter_frame" in line
    assert "blocker=blocked:blocked:authority" in line


def test_run_status_uses_traces_when_execution_snapshot_is_still_planning(tmp_path) -> None:
    supervisor = RunStatusSupervisor(out_dir=tmp_path, flow_id="analysis")

    with pytest.raises(ValueError, match="workflow_current_phase_invalid"):
        supervisor.update(
            status="running",
            progress_label="poll.analysis_readiness",
            session_id="session:2",
            matter_id="200",
            execution_snapshot={
                "status": "planning",
                "workflow": {
                    "status": "planning",
                    "current_node": "",
                    "current_subgraph": "",
                    "phases": [],
                },
            },
            execution_traces=[
                {
                    "node_id": "dispute_analysis_program:dispute_analysis_source_pack",
                    "node_name": "dispute_analysis_source_pack",
                    "phase": "dispute_analysis_source_pack",
                    "status": "completed",
                },
                {
                    "node_id": "dispute_analysis_program:dispute_analysis_matter_frame",
                    "node_name": "dispute_analysis_matter_frame",
                    "phase": "dispute_analysis_matter_frame",
                    "status": "running",
                },
            ],
            current_blocker={"type": "blocked", "summary": "analysis_not_ready"},
            next_action="continue_poll",
        )


def test_run_status_fails_when_workflow_missing_current_phase(tmp_path) -> None:
    supervisor = RunStatusSupervisor(out_dir=tmp_path, flow_id="analysis")

    with pytest.raises(ValueError, match="workflow_current_phase_invalid"):
        supervisor.update(
            status="running",
            progress_label="poll.analysis_readiness",
            session_id="session:3",
            matter_id="300",
            execution_snapshot={
                "status": "planning",
                "workflow": {"status": "planning", "phases": []},
            },
            execution_traces=[
                {
                    "node_id": "dispute_analysis_program:dispute_analysis_source_pack",
                    "node_name": "dispute_analysis_source_pack",
                    "status": "completed",
                },
                {
                    "node_id": "dispute_analysis_program:dispute_analysis_matter_frame",
                    "node_name": "dispute_analysis_matter_frame",
                    "status": "running",
                },
            ],
        )
