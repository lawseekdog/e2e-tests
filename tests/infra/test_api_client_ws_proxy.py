from __future__ import annotations

import pytest

from client.api_client import ApiClient
from client.api_client import _resolve_ws_proxy


def test_resolve_ws_proxy_defaults_to_auto(monkeypatch) -> None:
    monkeypatch.delenv("E2E_WS_PROXY", raising=False)
    assert _resolve_ws_proxy() is True


def test_resolve_ws_proxy_can_disable_proxy(monkeypatch) -> None:
    monkeypatch.setenv("E2E_WS_PROXY", "off")
    assert _resolve_ws_proxy() is None


def test_resolve_ws_proxy_accepts_explicit_url(monkeypatch) -> None:
    monkeypatch.setenv("E2E_WS_PROXY", "http://127.0.0.1:7890")
    assert _resolve_ws_proxy() == "http://127.0.0.1:7890"


@pytest.mark.asyncio
async def test_resume_hard_cut_never_sends_pending_card_payload() -> None:
    client = ApiClient("http://example.com/api/v1")
    captured: dict[str, object] = {}

    async def _fake_post_ws(
        ws_path: str,
        msg_type: str,
        data: dict[str, object],
        *,
        max_attempts: int | None = None,
        open_timeout_s: float | None = None,
    ) -> dict[str, object]:
        _ = (max_attempts, open_timeout_s)
        captured["ws_path"] = ws_path
        captured["msg_type"] = msg_type
        captured["data"] = data
        return {"events": [{"event": "end", "data": {"output": "ok"}}], "output": "ok"}

    client._post_ws = _fake_post_ws  # type: ignore[method-assign]

    resp = await client.resume(
        "session-1",
        {"answers": [{"field_key": "profile.background", "value": "继续"}]},
        pending_card={
            "id": "card-1",
            "skill_id": "legal-opinion-intake",
            "questions": [{"field_key": "profile.background", "question": "Q"}],
        },
        max_loops=8,
    )

    assert resp.get("output") == "ok"
    payload = captured.get("data")
    assert isinstance(payload, dict)
    assert payload.get("card_id") == "card-1"
    assert "pending_card" not in payload
