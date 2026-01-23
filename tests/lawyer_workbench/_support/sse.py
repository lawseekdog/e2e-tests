"""SSE event helpers for lawyer workbench E2E (no mocks).

We assert mostly on deterministic, non-LLM parts:
- event types present (progress/card/end)
- no error events
LLM-generated text is asserted via "contains" only when needed.
"""

from __future__ import annotations

from typing import Any, Iterable


def _events(sse: dict[str, Any]) -> list[dict[str, Any]]:
    evts = sse.get("events") if isinstance(sse, dict) else None
    if isinstance(evts, list):
        return [it for it in evts if isinstance(it, dict)]
    return []


def event_types(sse: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for it in _events(sse):
        t = it.get("event")
        if isinstance(t, str) and t:
            out.append(t)
    return out


def events_of_type(sse: dict[str, Any], event: str) -> list[dict[str, Any]]:
    want = str(event or "").strip()
    if not want:
        return []
    out: list[dict[str, Any]] = []
    for it in _events(sse):
        if it.get("event") != want:
            continue
        data = it.get("data")
        out.append(data if isinstance(data, dict) else {"data": data})
    return out


def last_event_data(sse: dict[str, Any], event: str) -> dict[str, Any] | None:
    for it in reversed(events_of_type(sse, event)):
        if isinstance(it, dict) and it:
            return it
    return None


def extract_output(sse: dict[str, Any]) -> str:
    if not isinstance(sse, dict):
        return ""
    return str(sse.get("output") or "")


def extract_last_card(sse: dict[str, Any]) -> dict[str, Any] | None:
    return last_event_data(sse, "card")


def _has_partial_stream_error(sse: dict[str, Any]) -> bool:
    """httpx may terminate SSE streams abruptly (proxy/connection teardown).

    ApiClient records these as an `error` event with `partial: True` so tests can continue by polling state.
    Treat it as a non-fatal stream end signal.
    """
    for e in events_of_type(sse, "error"):
        if isinstance(e, dict) and e.get("partial") is True:
            return True
    return False


def task_starts(sse: dict[str, Any]) -> list[dict[str, Any]]:
    return events_of_type(sse, "task_start")


def task_ends(sse: dict[str, Any]) -> list[dict[str, Any]]:
    return events_of_type(sse, "task_end")


def validate_task_events(sse: dict[str, Any]) -> None:
    """Validate task_start/task_end structure when present."""
    starts = task_starts(sse)
    ends = task_ends(sse)

    started_nodes: set[str] = set()
    for it in starts:
        node = str((it or {}).get("node") or "").strip()
        if not node:
            raise AssertionError(f"task_start missing node: {it}")
        started_nodes.add(node)

    for it in ends:
        node = str((it or {}).get("node") or "").strip()
        if not node:
            raise AssertionError(f"task_end missing node: {it}")
        # ai-engine emits node_end only after node_start, but keep this as a strong contract for the UI.
        if started_nodes and node not in started_nodes:
            raise AssertionError(f"task_end node without matching task_start: node={node!r} started={sorted(started_nodes)}")

    # Best-effort contract: run_skill starts should be enriched with skill_id.
    for it in starts:
        if str((it or {}).get("node") or "").strip() != "run_skill":
            continue
        sid = str((it or {}).get("skill_id") or "").strip()
        if not sid:
            raise AssertionError(f"run_skill task_start missing skill_id: {it}")


def assert_task_lifecycle(sse: dict[str, Any], *, min_starts: int = 1) -> None:
    """Require task_start/task_end to exist (workbench progress UI contract)."""
    starts = task_starts(sse)
    ends = task_ends(sse)
    if len(starts) < int(min_starts):
        raise AssertionError(f"SSE missing task_start events (need>={min_starts}). Event types={event_types(sse)}")
    if not ends:
        raise AssertionError(f"SSE missing task_end events. Event types={event_types(sse)}")
    validate_task_events(sse)

def assert_no_error(sse: dict[str, Any]) -> None:
    errs = [e for e in events_of_type(sse, "error") if not (isinstance(e, dict) and e.get("partial") is True)]
    if errs:
        raise AssertionError(f"SSE returned error events: {errs[:2]}. Event types={event_types(sse)}")


def assert_has_end(sse: dict[str, Any]) -> None:
    if not (events_of_type(sse, "end") or events_of_type(sse, "complete") or _has_partial_stream_error(sse)):
        raise AssertionError(f"SSE missing end/complete. Event types={event_types(sse)}")


def assert_has_progress(sse: dict[str, Any], *, message_contains: str | None = None) -> None:
    msg = str(message_contains).strip() if message_contains is not None else None
    ps = events_of_type(sse, "progress")
    if not ps:
        raise AssertionError(f"SSE missing progress events. Event types={event_types(sse)}")
    if msg:
        for p in ps:
            m = str(p.get("message") or "")
            if msg in m:
                return
        raise AssertionError(f"SSE progress missing message fragment={msg!r}. Progress={ps[:3]}")


def assert_visible_response(sse: dict[str, Any], *, output_must_contain: Iterable[str] | None = None) -> None:
    """Assert the UI has something to show: output text and/or a pending card."""
    assert_no_error(sse)
    assert_has_end(sse)
    assert_has_progress(sse)
    validate_task_events(sse)

    out = extract_output(sse).strip()
    card = extract_last_card(sse)
    if not out and not card:
        raise AssertionError(f"SSE has neither output nor card. Event types={event_types(sse)}")

    if output_must_contain:
        missing: list[str] = []
        for x in output_must_contain:
            s = str(x or "").strip()
            if not s:
                continue
            if s not in out:
                missing.append(s)
        if missing:
            raise AssertionError(f"SSE output missing fragments={missing}. Output sample:\n{out[:1500]}")
