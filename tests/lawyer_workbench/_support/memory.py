"""Memory-service E2E assertions.

We prefer API-level polling (over direct DB reads) to avoid coupling to schema, but provide
entity_key assertions that mirror unit tests.
"""

from __future__ import annotations

import asyncio
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
        f"/internal/memory-service/internal/memory/users/{int(user_id)}/facts",
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

