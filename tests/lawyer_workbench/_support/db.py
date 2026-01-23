"""Postgres helpers for E2E assertions (no mocks).

All Java/Python services in docker-compose.java-stack share a single Postgres instance, but use
different databases. This helper lets tests query each DB for black-box assertions.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Iterable


def _pg_host() -> str:
    return os.getenv("E2E_PG_HOST", "localhost").strip() or "localhost"


def _pg_port() -> int:
    raw = os.getenv("E2E_PG_PORT", "5434")
    try:
        return int(raw)
    except Exception:
        return 5434


def _pg_user() -> str:
    return os.getenv("E2E_PG_USER", "postgres").strip() or "postgres"


def _pg_password() -> str:
    return os.getenv("E2E_PG_PASSWORD", "postgres")


@dataclass(frozen=True)
class PgTarget:
    dbname: str
    host: str = _pg_host()
    port: int = _pg_port()
    user: str = _pg_user()
    password: str = _pg_password()


def _connect(target: PgTarget):
    import psycopg2
    import psycopg2.extras

    return psycopg2.connect(
        dbname=target.dbname,
        user=target.user,
        password=target.password,
        host=target.host,
        port=int(target.port),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def _execute_sync(target: PgTarget, sql: str, params: Iterable[Any] | None, *, fetch: str | None):
    sql = str(sql or "").strip()
    if not sql:
        raise ValueError("sql is required")

    with _connect(target) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, list(params or []))
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None


async def fetch_one(target: PgTarget, sql: str, params: Iterable[Any] | None = None) -> dict[str, Any] | None:
    return await asyncio.to_thread(_execute_sync, target, sql, params, fetch="one")


async def fetch_all(target: PgTarget, sql: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    rows = await asyncio.to_thread(_execute_sync, target, sql, params, fetch="all")
    return list(rows or [])


async def fetch_val(target: PgTarget, sql: str, params: Iterable[Any] | None = None) -> Any:
    row = await fetch_one(target, sql, params)
    if not row:
        return None
    # Return the first column's value.
    for _, v in row.items():
        return v
    return None


async def count(target: PgTarget, sql: str, params: Iterable[Any] | None = None) -> int:
    v = await fetch_val(target, sql, params)
    try:
        return int(v or 0)
    except Exception:
        return 0

