"""Execution trace helpers (ai-engine traces proxied by matters-service)."""

from __future__ import annotations

from typing import Any


def find_latest_trace(traces: list[dict[str, Any]], *, node_id: str) -> dict[str, Any] | None:
    """Find the latest trace item by node_id.

    matters-service /traces returns items ordered by started_at desc, so the first match is the latest.
    """
    want = str(node_id or "").strip()
    if not want:
        raise ValueError("node_id is required")
    for t in traces or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("node_id") or "").strip() == want:
            return t
    return None


def tool_calls(trace: dict[str, Any]) -> list[dict[str, Any]]:
    xs = trace.get("tool_calls") if isinstance(trace, dict) else None
    if isinstance(xs, list):
        return [it for it in xs if isinstance(it, dict)]
    return []


def find_tool_call(trace: dict[str, Any], *, name: str) -> dict[str, Any] | None:
    want = str(name or "").strip()
    if not want:
        raise ValueError("name is required")
    for c in tool_calls(trace):
        if str(c.get("name") or "").strip() == want:
            return c
    return None


def extract_context_manifest(trace: dict[str, Any]) -> dict[str, Any] | None:
    c = find_tool_call(trace, name="context_manifest")
    if not c:
        return None
    res = c.get("result")
    return res if isinstance(res, dict) else None

