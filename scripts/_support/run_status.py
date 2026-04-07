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


def extract_runtime_progress(snapshot: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(snapshot, dict):
        return {"current_task_id": "", "current_node": "", "current_phase": ""}
    analysis = snapshot.get("analysis_state") if isinstance(snapshot.get("analysis_state"), dict) else {}
    identity = analysis.get("identity") if isinstance(analysis.get("identity"), dict) else {}
    runtime = analysis.get("workbench_runtime") if isinstance(analysis.get("workbench_runtime"), dict) else {}
    return {
        "current_task_id": safe_str(
            analysis.get("current_task_id") or identity.get("current_task_id") or runtime.get("current_task_id")
        ),
        "current_node": safe_str(analysis.get("current_node") or runtime.get("current_node")),
        "current_phase": safe_str(
            analysis.get("current_phase") or snapshot.get("current_phase") or runtime.get("current_phase")
        ),
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
    current_phase = safe_str(row.get("current_phase"))
    current_node = safe_str(row.get("current_node"))
    blocker = safe_str(row.get("current_blocker"))
    next_action = safe_str(row.get("next_action"))
    if session_id:
        bits.append(f"session={session_id}")
    if matter_id:
        bits.append(f"matter={matter_id}")
    if current_phase:
        bits.append(f"phase={current_phase}")
    if current_node:
        bits.append(f"node={current_node}")
    if blocker:
        bits.append(f"blocker={blocker}")
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
        progress = extract_runtime_progress(snapshot)
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
