"""Phase timeline helpers (matters-service /api/v1/matters/{id}/phase-timeline)."""

from __future__ import annotations

from typing import Any, Iterable

from .utils import unwrap_api_response


def unwrap_phase_timeline(resp: Any) -> dict[str, Any]:
    data = unwrap_api_response(resp)
    if isinstance(data, dict):
        return data
    raise AssertionError(f"unexpected phase timeline payload: {resp}")


def phases(phase_tl: dict[str, Any]) -> list[dict[str, Any]]:
    xs = phase_tl.get("phases") if isinstance(phase_tl, dict) else None
    if isinstance(xs, list):
        return [it for it in xs if isinstance(it, dict)]
    return []


def phase_ids(phase_tl: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for p in phases(phase_tl):
        pid = str(p.get("id") or "").strip()
        if pid:
            out.append(pid)
    return out


def phase_status(phase_tl: dict[str, Any], phase_id: str) -> str | None:
    want = str(phase_id or "").strip()
    for p in phases(phase_tl):
        if str(p.get("id") or "").strip() != want:
            continue
        s = str(p.get("status") or "").strip()
        return s or None
    return None


def deliverables(phase_tl: dict[str, Any]) -> list[dict[str, Any]]:
    xs = phase_tl.get("deliverables") if isinstance(phase_tl, dict) else None
    if isinstance(xs, list):
        return [it for it in xs if isinstance(it, dict)]
    return []


def deliverable_output_keys(phase_tl: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for d in deliverables(phase_tl):
        k = str(d.get("outputKey") or d.get("output_key") or "").strip()
        if k:
            out.add(k)
    return out


def assert_has_phases(phase_tl: dict[str, Any], *, must_include: Iterable[str]) -> None:
    have = set(phase_ids(phase_tl))
    want = {str(x).strip() for x in must_include if str(x).strip()}
    missing = sorted(want - have)
    if missing:
        raise AssertionError(f"phase_timeline missing phases={missing}. Have={sorted(have)}")


def assert_phase_status_in(phase_tl: dict[str, Any], *, phase_id: str, allowed: Iterable[str]) -> None:
    got = phase_status(phase_tl, phase_id)
    allowed_set = {str(x).strip() for x in allowed if str(x).strip()}
    if got not in allowed_set:
        raise AssertionError(f"phase_timeline phase status mismatch: phase={phase_id!r} got={got!r} allowed={sorted(allowed_set)}")


def assert_has_deliverable(phase_tl: dict[str, Any], *, output_key: str) -> None:
    want = str(output_key or "").strip()
    if not want:
        raise ValueError("output_key is required")
    for d in deliverables(phase_tl):
        ok = str(d.get("outputKey") or d.get("output_key") or "").strip()
        if ok != want:
            continue
        fid = str(d.get("fileId") or d.get("file_id") or "").strip()
        if not fid:
            raise AssertionError(f"phase_timeline deliverable missing fileId: {d}")
        st = str(d.get("status") or "").strip()
        if st and st not in {"completed", "done"}:
            raise AssertionError(f"phase_timeline deliverable status unexpected: {d}")
        return
    raise AssertionError(f"phase_timeline missing deliverable output_key={want!r}. Have={sorted(deliverable_output_keys(phase_tl))}")
