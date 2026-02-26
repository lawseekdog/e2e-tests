from __future__ import annotations

import sys
import types

from tests.lawyer_workbench._support.db import PgTarget, _connect


def test_connect_fallbacks_to_underscore_db_name(monkeypatch):
    calls: list[str] = []

    class OperationalError(Exception):
        pass

    def fake_connect(**kwargs):
        dbname = str(kwargs.get("dbname") or "")
        calls.append(dbname)
        if dbname == "matter-service":
            raise OperationalError('database "matter-service" does not exist')
        return object()

    fake_extras = types.SimpleNamespace(RealDictCursor=object())
    fake_psycopg2 = types.SimpleNamespace(
        connect=fake_connect,
        OperationalError=OperationalError,
        extras=fake_extras,
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", fake_extras)

    target = PgTarget(dbname="matter-service", host="localhost", port=5432, user="postgres", password="postgres")
    conn = _connect(target)

    assert conn is not None
    assert calls == ["matter-service", "matter_service"]
