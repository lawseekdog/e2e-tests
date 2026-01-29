"""Memory-service E2E assertions.

We prefer API-level polling (over direct DB reads) to avoid coupling to schema, but provide
entity_key assertions that mirror unit tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Iterable

from .utils import unwrap_api_response


async def list_case_facts(
    client,
    *,
    user_id: int,
    case_id: str,
    limit: int = 200,
) -> list[dict[str, Any]]:
    resp = await client.get(
        f"/api/v1/internal/memory-service/memory/users/{int(user_id)}/facts",
        params={"scope": "case", "case_id": str(case_id), "limit": int(limit)},
    )
    data = unwrap_api_response(resp)
    # memory-service returns a plain list (no ApiResponse) on this internal route.
    if isinstance(data, list):
        return [it for it in data if isinstance(it, dict)]
    if isinstance(resp, list):
        return [it for it in resp if isinstance(it, dict)]
    return []


def entity_keys(facts: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for it in facts or []:
        if not isinstance(it, dict):
            continue
        k = str(it.get("entity_key") or "").strip()
        if k:
            out.add(k)
    return out


def find_fact(facts: list[dict[str, Any]], *, entity_key: str) -> dict[str, Any] | None:
    """Find a fact by exact entity_key."""
    want = str(entity_key or "").strip()
    if not want:
        raise ValueError("entity_key is required")
    for it in facts or []:
        if not isinstance(it, dict):
            continue
        k = str(it.get("entity_key") or "").strip()
        if k == want:
            return it
    return None


def assert_fact_content_contains(
    facts: list[dict[str, Any]],
    *,
    entity_key: str,
    must_include: Iterable[str],
) -> None:
    """Assert a specific fact exists and its content contains required fragments."""
    f = find_fact(facts, entity_key=entity_key)
    if not f:
        raise AssertionError(f"missing memory fact: entity_key={entity_key!r}. Have={sorted(entity_keys(facts))[:50]}")

    content = str(f.get("content") or "")
    missing: list[str] = []
    for needle in must_include:
        s = str(needle or "").strip()
        if not s:
            continue
        if s not in content:
            missing.append(s)
    if missing:
        raise AssertionError(
            f"memory fact content missing fragments: entity_key={entity_key!r} missing={missing}. "
            f"content={content!r}"
        )


def assert_any_fact_content_contains(
    facts: list[dict[str, Any]],
    *,
    candidate_entity_keys: Iterable[str],
    must_include: Iterable[str],
) -> str:
    """Assert any one of the given entity_keys exists and matches content; return the matched key."""
    keys = [str(x).strip() for x in (candidate_entity_keys or []) if str(x).strip()]
    if not keys:
        raise ValueError("candidate_entity_keys is required")
    last_err: AssertionError | None = None
    for k in keys:
        try:
            assert_fact_content_contains(facts, entity_key=k, must_include=must_include)
            return k
        except AssertionError as e:
            last_err = e
            continue
    raise AssertionError(
        f"none of the candidate memory facts matched: keys={keys}. Have={sorted(entity_keys(facts))[:50]}. "
        f"Last={last_err}"
    )


async def wait_for_entity_keys(
    client,
    *,
    user_id: int,
    case_id: str,
    must_include: Iterable[str],
    timeout_s: float = 90.0,
    interval_s: float = 2.0,
) -> list[dict[str, Any]]:
    want = {str(x).strip() for x in must_include if str(x).strip()}
    deadline = time.time() + float(timeout_s)
    last_keys: set[str] = set()
    last_facts: list[dict[str, Any]] = []

    while time.time() < deadline:
        last_facts = await list_case_facts(client, user_id=user_id, case_id=case_id, limit=300)
        last_keys = entity_keys(last_facts)
        if want.issubset(last_keys):
            return last_facts
        await asyncio.sleep(float(interval_s))

    missing = sorted(want - last_keys)
    raise AssertionError(f"Timed out waiting for memory entity_keys. Missing={missing}. Got={sorted(last_keys)[:50]}")


def stable_token(value: str) -> str:
    """Match ai-engine memory materializer stable token (md5[:12])."""
    s = str(value or "")
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


async def wait_for_memory_facts(
    client,
    *,
    user_id: int,
    case_id: str,
    must_include_entity_keys: Iterable[str] | None = None,
    must_include_content: Iterable[str] | None = None,
    timeout_s: float = 90.0,
    interval_s: float = 2.0,
) -> list[dict[str, Any]]:
    """Wait until memory facts satisfy both key subset + content fragments (best-effort)."""
    want_keys = {str(x).strip() for x in (must_include_entity_keys or []) if str(x).strip()}
    want_fragments = [str(x).strip() for x in (must_include_content or []) if str(x).strip()]

    deadline = time.time() + float(timeout_s)
    last_keys: set[str] = set()
    last_facts: list[dict[str, Any]] = []

    while time.time() < deadline:
        last_facts = await list_case_facts(client, user_id=user_id, case_id=case_id, limit=300)
        last_keys = entity_keys(last_facts)

        ok_keys = want_keys.issubset(last_keys) if want_keys else True

        hay = "\n".join([str(it.get("content") or "") for it in last_facts if isinstance(it, dict)])
        ok_content = all(frag in hay for frag in want_fragments) if want_fragments else True

        if ok_keys and ok_content:
            return last_facts
        await asyncio.sleep(float(interval_s))

    missing_keys = sorted(want_keys - last_keys)
    missing_fragments = []
    if want_fragments:
        hay = "\n".join([str(it.get("content") or "") for it in last_facts if isinstance(it, dict)])
        missing_fragments = [x for x in want_fragments if x not in hay]

    raise AssertionError(
        "Timed out waiting for memory facts. "
        f"MissingKeys={missing_keys} MissingFragments={missing_fragments} GotKeys={sorted(last_keys)[:50]}"
    )
