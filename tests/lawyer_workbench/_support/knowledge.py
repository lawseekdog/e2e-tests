"""knowledge-service helpers for deterministic E2E retrieval assertions."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .utils import unwrap_api_response


async def ingest_doc(
    client,
    *,
    kb_id: str,
    file_id: str,
    content: str,
    doc_type: str = "case",
    metadata: dict[str, Any] | None = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    payload = {
        "kb_id": str(kb_id),
        "file_id": str(file_id),
        "content": str(content),
        "doc_type": str(doc_type),
        "metadata": metadata or {},
        "overwrite": bool(overwrite),
    }
    resp = await client.post("/knowledge-service/api/v1/internal/knowledge/ingest", payload)
    data = unwrap_api_response(resp)
    if not isinstance(data, dict):
        raise AssertionError(f"knowledge ingest returned unexpected payload: {resp}")
    return data


async def search(
    client,
    *,
    query: str,
    kb_ids: list[str],
    top_k: int = 5,
    include_content: bool = True,
    include_metadata: bool = True,
) -> dict[str, Any]:
    payload = {
        "query": str(query),
        "kb_ids": [str(x) for x in kb_ids],
        "top_k": int(top_k),
        "include_content": bool(include_content),
        "include_metadata": bool(include_metadata),
    }
    resp = await client.post("/knowledge-service/api/v1/internal/knowledge/search", payload)
    data = unwrap_api_response(resp)
    if not isinstance(data, dict):
        raise AssertionError(f"knowledge search returned unexpected payload: {resp}")
    return data


async def wait_for_search_hit(
    client,
    *,
    query: str,
    kb_ids: list[str],
    must_file_id: str,
    timeout_s: float = 60.0,
    interval_s: float = 2.0,
) -> dict[str, Any]:
    want = str(must_file_id).strip()
    deadline = time.time() + float(timeout_s)
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        last = await search(client, query=query, kb_ids=kb_ids, top_k=10, include_content=False, include_metadata=True)
        results = last.get("results") if isinstance(last.get("results"), list) else []
        for it in results:
            if isinstance(it, dict) and str(it.get("file_id") or "").strip() == want:
                return last
        await asyncio.sleep(float(interval_s))
    raise AssertionError(f"Timed out waiting for knowledge search hit: file_id={want}. Last={last}")
