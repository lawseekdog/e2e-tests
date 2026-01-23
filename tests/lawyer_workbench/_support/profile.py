"""Workflow profile helpers (事项画像)."""

from __future__ import annotations

from typing import Any


def normalize_parties(profile: dict[str, Any]) -> list[dict[str, str]]:
    """Return parties as a normalized list of {role, name} for assertions."""
    out: list[dict[str, str]] = []
    raw = profile.get("parties") if isinstance(profile, dict) else None
    if isinstance(raw, list):
        for it in raw:
            if not isinstance(it, dict):
                continue
            role = str(it.get("role") or it.get("party_type") or "").strip()
            name = str(it.get("name") or it.get("entity_name") or "").strip()
            if role and name:
                out.append({"role": role, "name": name})
    # Fallback: older shapes may have top-level plaintiff/defendant strings.
    if not out and isinstance(profile, dict):
        pl = profile.get("plaintiff")
        df = profile.get("defendant")
        if isinstance(pl, str) and pl.strip():
            out.append({"role": "plaintiff", "name": pl.strip()})
        if isinstance(df, str) and df.strip():
            out.append({"role": "defendant", "name": df.strip()})
    return out


def assert_service_type(profile: dict[str, Any], expected: str) -> None:
    want = str(expected or "").strip()
    got = str((profile or {}).get("service_type_id") or "").strip()
    if want and got != want:
        raise AssertionError(f"profile.service_type_id mismatch: expected={want!r} got={got!r}")


def assert_has_party(profile: dict[str, Any], *, role: str, name_contains: str) -> None:
    r = str(role or "").strip()
    n = str(name_contains or "").strip()
    if not r or not n:
        raise ValueError("role and name_contains are required")
    for p in normalize_parties(profile):
        if p.get("role") == r and n in (p.get("name") or ""):
            return
    raise AssertionError(f"profile missing party: role={r!r} name_contains={n!r}. Parties={normalize_parties(profile)}")

