"""Small, reusable helpers for E2E tests."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable


def unwrap_api_response(resp: Any) -> Any:
    """Unwrap ApiResponse/PageResponse and return the `data` field when present."""
    if isinstance(resp, dict) and "code" in resp:
        return resp.get("data")
    return resp


async def eventually(
    fn: Callable[[], Any],
    *,
    timeout_s: float = 60.0,
    interval_s: float = 1.0,
    description: str = "condition",
) -> Any:
    """Poll `fn` until it returns a truthy value or timeout.

    `fn` can be sync or async.
    """
    deadline = time.time() + float(timeout_s)
    last: Any = None
    while time.time() < deadline:
        last = fn()
        if asyncio.iscoroutine(last):
            last = await last
        if last:
            return last
        await asyncio.sleep(float(interval_s))
    raise AssertionError(f"Timed out waiting for {description} (timeout={timeout_s}s). Last={last!r}")


def coerce_str(v: Any) -> str:
    return str(v) if v is not None else ""


def trim(v: Any) -> str | None:
    s = str(v or "").strip()
    return s or None

