from __future__ import annotations

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
