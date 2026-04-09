from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TERMINAL_RUN_STATUSES = {"completed", "failed", "blocked", "aborted"}


def safe_str(value: Any) -> str:
    return str(value or "").strip()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def compact_pending_card(card: dict[str, Any] | None) -> dict[str, Any]:
    pending = card if isinstance(card, dict) else {}
    questions = pending.get("questions") if isinstance(pending.get("questions"), list) else []
    return {
        "id": safe_str(pending.get("id")),
        "skill_id": safe_str(pending.get("skill_id")),
        "task_key": safe_str(pending.get("task_key")),
        "review_type": safe_str(pending.get("review_type")),
        "question_count": len(questions),
    }


def _workflow_payload(execution_snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(execution_snapshot, dict):
        return {}
    workflow = execution_snapshot.get("workflow")
    return workflow if isinstance(workflow, dict) else {}


def _latest_completed_phase(workflow: dict[str, Any]) -> tuple[str, str]:
    phases = workflow.get("phases") if isinstance(workflow.get("phases"), list) else []
    last_phase = ""
    last_label = ""
    for row in phases:
        if not isinstance(row, dict):
            continue
        if safe_str(row.get("status")).lower() != "completed":
            continue
        last_phase = safe_str(row.get("phase_id") or row.get("id"))
        last_label = safe_str(row.get("label") or row.get("name"))
    return last_phase, last_label


def _current_phase_from_snapshot(snapshot: dict[str, Any]) -> tuple[str, str]:
    analysis = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    runtime = analysis.get("workbench_runtime") if isinstance(analysis.get("workbench_runtime"), dict) else {}
    phase = safe_str(
        analysis.get("current_phase")
        or snapshot.get("current_phase")
        or snapshot.get("current_phase_id")
        or runtime.get("current_phase")
    )
    label = safe_str(snapshot.get("current_phase_name"))
    return phase, label


def _trace_phase_token(row: dict[str, Any]) -> str:
    phase = safe_str(row.get("phase"))
    if phase:
        return phase
    node_id = safe_str(row.get("node_id"))
    if ":" in node_id:
        return safe_str(node_id.rsplit(":", 1)[-1])
    return ""


def _latest_trace_progress(execution_traces: list[dict[str, Any]] | None) -> dict[str, str]:
    rows = [row for row in (execution_traces or []) if isinstance(row, dict)]
    if not rows:
        return {
            "current_task_id": "",
            "current_node": "",
            "current_phase": "",
            "current_subgraph": "",
            "last_completed_phase": "",
        }
    current_row = rows[-1]
    for row in reversed(rows):
        if safe_str(row.get("status")).lower() not in {"completed", "failed", "blocked", "aborted"}:
            current_row = row
            break

    last_completed_phase = ""
    for row in rows:
        if safe_str(row.get("status")).lower() != "completed":
            continue
        phase = _trace_phase_token(row)
        if phase:
            last_completed_phase = phase

    current_phase = _trace_phase_token(current_row)
    current_node = safe_str(current_row.get("node_name") or current_row.get("node_id"))
    return {
        "current_task_id": safe_str(current_row.get("node_id")),
        "current_node": current_node,
        "current_phase": current_phase,
        "current_subgraph": current_phase,
        "last_completed_phase": last_completed_phase,
    }


def extract_runtime_progress(
    snapshot: dict[str, Any] | None,
    *,
    execution_snapshot: dict[str, Any] | None = None,
    execution_traces: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    if not isinstance(snapshot, dict) and not isinstance(execution_snapshot, dict) and not execution_traces:
        return {
            "current_task_id": "",
            "current_node": "",
            "current_phase": "",
            "current_phase_label": "",
            "current_subgraph": "",
            "execution_status": "",
            "last_completed_phase": "",
            "last_completed_phase_label": "",
        }
    snapshot_obj = snapshot if isinstance(snapshot, dict) else {}
    workflow = _workflow_payload(execution_snapshot)
    analysis = snapshot_obj.get("analysis_state") if isinstance(snapshot_obj.get("analysis_state"), dict) else {}
    identity = analysis.get("identity") if isinstance(analysis.get("identity"), dict) else {}
    runtime = analysis.get("workbench_runtime") if isinstance(analysis.get("workbench_runtime"), dict) else {}
    trace_progress = _latest_trace_progress(execution_traces)
    current_phase, current_phase_label = _current_phase_from_snapshot(snapshot_obj)
    if workflow:
        current_phase = safe_str(
            workflow.get("current_phase_id")
            or (execution_snapshot or {}).get("current_phase_id")
            or current_phase
        )
        current_phase_label = safe_str(
            workflow.get("current_phase_label")
            or ((execution_snapshot or {}).get("phase") or {}).get("name")
            or (execution_snapshot or {}).get("current_phase_name")
            or current_phase_label
        )
    last_completed_phase, last_completed_phase_label = _latest_completed_phase(workflow)
    if not current_phase and trace_progress["current_phase"]:
        current_phase = trace_progress["current_phase"]
    current_subgraph = safe_str(
        analysis.get("current_subgraph")
        or workflow.get("current_subgraph")
        or analysis.get("runtime_node_scope")
    )
    if not current_subgraph:
        current_subgraph = trace_progress["current_subgraph"]
    current_node = safe_str(
        analysis.get("current_node")
        or workflow.get("current_node")
        or runtime.get("current_node")
    ) or trace_progress["current_node"]
    current_task_id = safe_str(
        analysis.get("current_task_id") or identity.get("current_task_id") or runtime.get("current_task_id")
    ) or trace_progress["current_task_id"]
    if not last_completed_phase:
        last_completed_phase = trace_progress["last_completed_phase"]
    return {
        "current_task_id": current_task_id,
        "current_node": current_node,
        "current_phase": current_phase,
        "current_phase_label": current_phase_label,
        "current_subgraph": current_subgraph,
        "execution_status": safe_str(
            (execution_snapshot or {}).get("status")
            or workflow.get("status")
        ),
        "last_completed_phase": last_completed_phase,
        "last_completed_phase_label": last_completed_phase_label,
    }


def resolve_status_path(path_or_dir: str | Path) -> Path:
    path = Path(path_or_dir).expanduser().resolve()
    if path.name == "run_status.json":
        return path
    return path / "run_status.json"


def format_run_status_line(payload: dict[str, Any] | None) -> str:
    row = payload if isinstance(payload, dict) else {}
    bits = [
        f"flow={safe_str(row.get('flow_id')) or '-'}",
        f"status={safe_str(row.get('status')) or '-'}",
        f"step={safe_str(row.get('current_step')) or '-'}",
    ]
    session_id = safe_str(row.get("session_id"))
    matter_id = safe_str(row.get("matter_id"))
    execution_status = safe_str(row.get("execution_status"))
    current_phase = safe_str(row.get("current_phase"))
    current_phase_label = safe_str(row.get("current_phase_label"))
    current_subgraph = safe_str(row.get("current_subgraph"))
    current_node = safe_str(row.get("current_node"))
    last_completed_phase = safe_str(row.get("last_completed_phase"))
    blocker = safe_str(row.get("current_blocker"))
    next_action = safe_str(row.get("next_action"))
    pending = row.get("pending_card") if isinstance(row.get("pending_card"), dict) else {}
    pending_skill = safe_str(pending.get("skill_id"))
    pending_task = safe_str(pending.get("task_key"))
    error = safe_str(row.get("error"))
    if session_id:
        bits.append(f"session={session_id}")
    if matter_id:
        bits.append(f"matter={matter_id}")
    if execution_status:
        bits.append(f"exec={execution_status}")
    if current_phase:
        bits.append(f"phase={current_phase}")
    if current_phase_label:
        bits.append(f"phase_name={current_phase_label}")
    if current_subgraph:
        bits.append(f"subgraph={current_subgraph}")
    if current_node:
        bits.append(f"node={current_node}")
    if last_completed_phase:
        bits.append(f"last_ok={last_completed_phase}")
    if pending_skill or pending_task:
        bits.append(f"pending={pending_skill or pending_task}")
    if blocker:
        bits.append(f"blocker={blocker}")
    if error:
        bits.append(f"error={error[:96]}")
    if next_action:
        bits.append(f"next={next_action}")
    return " ".join(bits)


@dataclass
class RunStatusSupervisor:
    out_dir: Path
    flow_id: str
    status_path: Path = field(init=False)
    started_at: str = field(default_factory=utc_now_iso)
    artifact_refs: dict[str, str] = field(default_factory=dict)
    terminal_locked: bool = False

    def __post_init__(self) -> None:
        self.out_dir = self.out_dir.resolve()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.status_path = self.out_dir / "run_status.json"
        self.artifact_refs.setdefault("output_dir", str(self.out_dir))
        self.artifact_refs.setdefault("status_file", str(self.status_path))

    def _persist_latest_payloads(self, latest_payloads: dict[str, Any] | None) -> None:
        if not isinstance(latest_payloads, dict):
            return
        for name, payload in latest_payloads.items():
            stem = safe_str(name)
            if not stem:
                continue
            write_json(self.out_dir / f"{stem}.latest.json", payload)

    def update(
        self,
        *,
        status: str,
        current_step: str,
        session_id: str = "",
        matter_id: str = "",
        snapshot: dict[str, Any] | None = None,
        execution_snapshot: dict[str, Any] | None = None,
        execution_traces: list[dict[str, Any]] | None = None,
        pending_card: dict[str, Any] | None = None,
        current_blocker: str = "",
        next_action: str = "",
        wait_round: int | None = None,
        seen_cards: int | None = None,
        seen_sse_rounds: int | None = None,
        error: str = "",
        artifact_refs: dict[str, str] | None = None,
        latest_payloads: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self.terminal_locked and safe_str(status) not in TERMINAL_RUN_STATUSES:
            return
        progress = extract_runtime_progress(
            snapshot,
            execution_snapshot=execution_snapshot,
            execution_traces=execution_traces,
        )
        artifacts = dict(self.artifact_refs)
        if isinstance(artifact_refs, dict):
            for key, value in artifact_refs.items():
                token = safe_str(value)
                if token:
                    artifacts[safe_str(key)] = token
        payload = {
            "contract_version": "live_run_status.v2",
            "flow_id": safe_str(self.flow_id),
            "status": safe_str(status),
            "current_step": safe_str(current_step),
            "session_id": safe_str(session_id),
            "matter_id": safe_str(matter_id),
            "current_task_id": progress["current_task_id"],
            "current_node": progress["current_node"],
            "current_phase": progress["current_phase"],
            "current_phase_label": progress["current_phase_label"],
            "current_subgraph": progress["current_subgraph"],
            "execution_status": progress["execution_status"],
            "last_completed_phase": progress["last_completed_phase"],
            "last_completed_phase_label": progress["last_completed_phase_label"],
            "current_blocker": safe_str(current_blocker),
            "next_action": safe_str(next_action),
            "wait_round": int(wait_round or 0),
            "seen_cards": int(seen_cards or 0),
            "seen_sse_rounds": int(seen_sse_rounds or 0),
            "pending_card": compact_pending_card(pending_card),
            "error": safe_str(error),
            "artifacts": artifacts,
            "started_at": self.started_at,
            "updated_at": utc_now_iso(),
        }
        if isinstance(execution_snapshot, dict) and execution_snapshot:
            payload["execution_snapshot_digest"] = {
                "status": safe_str(execution_snapshot.get("status")),
                "progress_pct": execution_snapshot.get("progress_pct"),
                "current_phase_id": safe_str(execution_snapshot.get("current_phase_id")),
                "current_phase_name": safe_str(execution_snapshot.get("current_phase_name")),
            }
        if execution_traces:
            latest_trace = _latest_trace_progress(execution_traces)
            payload["execution_traces_digest"] = {
                "trace_count": len([row for row in execution_traces if isinstance(row, dict)]),
                "current_phase": latest_trace["current_phase"],
                "current_node": latest_trace["current_node"],
                "last_completed_phase": latest_trace["last_completed_phase"],
            }
        if isinstance(extra, dict) and extra:
            payload["extra"] = extra
        write_json(self.status_path, payload)
        self._persist_latest_payloads(latest_payloads)
        if safe_str(status) in TERMINAL_RUN_STATUSES:
            self.terminal_locked = True

    async def observe_flow_progress(self, event: dict[str, Any]) -> None:
        if self.terminal_locked:
            return
        row = event if isinstance(event, dict) else {}
        label = safe_str(row.get("label")) or "flow.progress"
        snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
        blocker = ""
        next_action = "continue_workflow"
        if label.startswith("waiting:"):
            blocker = safe_str(label.split(":", 1)[1])
            next_action = "continue_poll"
        elif label.startswith("ready:"):
            next_action = "collect_final_outputs"
        elif label.startswith("resume:"):
            next_action = "await_resume_effect"
        elif label.startswith("nudge:"):
            next_action = "await_session_progress"
        elif label.startswith("request:"):
            next_action = "await_requested_documents"
        self.update(
            status="running",
            current_step=label,
            session_id=safe_str(row.get("session_id")),
            matter_id=safe_str(row.get("matter_id")),
            snapshot=snapshot,
            pending_card=row.get("card") if isinstance(row.get("card"), dict) else None,
            current_blocker=blocker,
            next_action=next_action,
            extra={
                "step_no": int(row.get("step_no") or 0),
                "max_steps": int(row.get("max_steps") or 0),
                "session_status": safe_str(row.get("session_status")),
                "phase_status": safe_str(row.get("phase_status")),
                "trace_node": safe_str(row.get("trace_node")),
                "trace_status": safe_str(row.get("trace_status")),
                "deliverables": safe_str(row.get("deliverables")),
                "event_summary": safe_str(row.get("event_summary")),
            },
        )
