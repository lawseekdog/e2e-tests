from __future__ import annotations

import re
from typing import Any, Iterable


def assert_json_structure_valid(
    data: Any, *, required_fields: Iterable[str] | None = None
) -> None:
    if not isinstance(data, dict):
        raise AssertionError(
            f"Expected dict, got {type(data).__name__}: {str(data)[:500]}"
        )

    if required_fields:
        missing = []
        for field in required_fields:
            parts = field.split(".")
            current = data
            found = True
            for part in parts:
                if not isinstance(current, dict) or part not in current:
                    found = False
                    break
                current = current[part]
            if not found:
                missing.append(field)
        if missing:
            raise AssertionError(
                f"Missing required fields: {missing}. Data keys: {list(data.keys())[:20]}"
            )


def assert_list_not_empty(data: Any, *, field_name: str = "list") -> None:
    if not isinstance(data, list):
        raise AssertionError(f"{field_name} expected list, got {type(data).__name__}")
    if not data:
        raise AssertionError(f"{field_name} is empty")


def assert_table_rows_valid(
    rows: Any, *, min_rows: int = 1, required_columns: Iterable[str] | None = None
) -> None:
    if not isinstance(rows, list):
        raise AssertionError(f"Table rows expected list, got {type(rows).__name__}")
    if len(rows) < min_rows:
        raise AssertionError(
            f"Table has {len(rows)} rows, expected at least {min_rows}"
        )

    if required_columns:
        required_set = set(required_columns)
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                raise AssertionError(f"Row {i} expected dict, got {type(row).__name__}")
            missing = required_set - set(row.keys())
            if missing:
                raise AssertionError(
                    f"Row {i} missing columns: {sorted(missing)}. Has: {list(row.keys())}"
                )


_CITATION_PATTERN = re.compile(
    r"\[(?:law|case|kb|doc|file|evidence|memory|search):[^\]]+\]", re.IGNORECASE
)

_DUPLICATE_CITATION_PATTERN = re.compile(
    r"(\[(?:law|case|kb|doc|file|evidence|memory|search):[^\]]+\])\s*\1", re.IGNORECASE
)


def extract_citations(text: str) -> list[str]:
    if not text:
        return []
    return _CITATION_PATTERN.findall(text)


def assert_citations_valid(
    text: str, *, min_citations: int = 0, allow_duplicates: bool = False
) -> None:
    citations = extract_citations(text)

    if len(citations) < min_citations:
        raise AssertionError(
            f"Found {len(citations)} citations, expected at least {min_citations}. Text sample: {text[:500]}"
        )

    if not allow_duplicates and citations:
        seen = set()
        duplicates = []
        for c in citations:
            if c in seen:
                duplicates.append(c)
            seen.add(c)
        if duplicates:
            raise AssertionError(f"Duplicate citations found: {duplicates[:5]}")

    if _DUPLICATE_CITATION_PATTERN.search(text):
        raise AssertionError(
            f"Adjacent duplicate citations detected in text. Sample: {text[:500]}"
        )


def assert_citations_deduped_and_trimmed(text: str) -> None:
    citations = extract_citations(text)
    if not citations:
        return

    seen = set()
    for c in citations:
        if c in seen:
            raise AssertionError(f"Citation not deduped: {c}")
        seen.add(c)

    for c in citations:
        if c != c.strip():
            raise AssertionError(f"Citation not trimmed: {repr(c)}")
        inner = c[1:-1]
        if "  " in inner:
            raise AssertionError(f"Citation has extra whitespace: {repr(c)}")


def assert_no_placeholder_leaks(text: str) -> None:
    placeholders = ["{{", "}}", "{%", "%}", "[[PLACEHOLDER]]", "[TODO]", "[TBD]", "___"]
    found = []
    for p in placeholders:
        if p in text:
            found.append(p)
    if found:
        raise AssertionError(
            f"Placeholder leaks detected: {found}. Text sample: {text[:500]}"
        )


def assert_deliverable_structure(deliverable: dict[str, Any]) -> None:
    required = ["id", "output_key", "file_id"]
    missing = [f for f in required if not deliverable.get(f)]
    if missing:
        raise AssertionError(
            f"Deliverable missing required fields: {missing}. Got: {deliverable}"
        )

    status = str(deliverable.get("status") or "").strip()
    if status and status not in {"completed", "done", "pending", "in_progress"}:
        raise AssertionError(f"Unexpected deliverable status: {status}")


def assert_trace_has_nodes(
    traces: list[dict[str, Any]], *, required_nodes: Iterable[str]
) -> None:
    node_ids = set()
    for t in traces:
        if isinstance(t, dict):
            nid = str(t.get("node_id") or "").strip()
            if nid:
                node_ids.add(nid)
                if ":" in nid:
                    node_ids.add(nid.split(":")[-1])

    missing = []
    for node in required_nodes:
        node_clean = node.strip()
        if node_clean not in node_ids and f"skill:{node_clean}" not in node_ids:
            missing.append(node_clean)

    if missing:
        raise AssertionError(
            f"Missing trace nodes: {missing}. Found: {sorted(node_ids)[:20]}"
        )


def assert_phase_timeline_valid(
    phase_tl: dict[str, Any], *, playbook_id: str, required_phases: Iterable[str]
) -> None:
    got_playbook = str(
        phase_tl.get("playbookId") or phase_tl.get("playbook_id") or ""
    ).strip()
    if got_playbook != playbook_id:
        raise AssertionError(
            f"Phase timeline playbook mismatch: expected={playbook_id}, got={got_playbook}"
        )

    phases = phase_tl.get("phases") if isinstance(phase_tl.get("phases"), list) else []
    phase_ids = {str(p.get("id") or "").strip() for p in phases if isinstance(p, dict)}

    missing = set(required_phases) - phase_ids
    if missing:
        raise AssertionError(
            f"Missing phases: {sorted(missing)}. Found: {sorted(phase_ids)}"
        )


def assert_workflow_profile_valid(
    profile: dict[str, Any], *, service_type_id: str
) -> None:
    got_service = str(
        profile.get("service_type_id") or profile.get("serviceTypeId") or ""
    ).strip()
    if got_service != service_type_id:
        raise AssertionError(
            f"Profile service_type mismatch: expected={service_type_id}, got={got_service}"
        )
