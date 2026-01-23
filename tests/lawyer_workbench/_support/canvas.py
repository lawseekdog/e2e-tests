"""Consultation canvas helpers (consultations-service /api/v1/consultations/sessions/{id}/canvas)."""

from __future__ import annotations

from typing import Any

from .utils import unwrap_api_response


def unwrap_canvas(resp: Any) -> dict[str, Any]:
    data = unwrap_api_response(resp)
    if isinstance(data, dict):
        return data
    raise AssertionError(f"unexpected canvas payload: {resp}")


def canvas_profile(canvas: dict[str, Any]) -> dict[str, Any]:
    p = canvas.get("profile") if isinstance(canvas, dict) else None
    return p if isinstance(p, dict) else {}


def canvas_evidence_file_ids(canvas: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    xs = canvas.get("evidence_list") if isinstance(canvas, dict) else None
    if not isinstance(xs, list):
        return out
    for it in xs:
        if not isinstance(it, dict):
            continue
        fid = str(it.get("file_id") or "").strip()
        if fid:
            out.add(fid)
    return out

