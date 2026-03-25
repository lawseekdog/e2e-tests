from __future__ import annotations

import pytest

from client.api_client import ApiClient


class _Response:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _AsyncClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def post(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(("POST", url, kwargs))
        return _Response({"code": 0, "message": "OK", "data": {"access_token": "token-1"}})

    async def request(self, method: str, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((method, url, kwargs))
        return _Response(
            {
                "code": 0,
                "message": "OK",
                "data": {
                    "user_id": 1,
                    "organization_id": 1,
                    "is_superuser": True,
                },
            }
        )


@pytest.mark.asyncio
async def test_login_uses_auth_service_gateway_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2E_HTTP_LOGIN_RETRIES", "1")
    monkeypatch.setenv("E2E_HTTP_GET_RETRIES", "1")
    client = ApiClient("http://127.0.0.1:18080/api/v1")
    fake = _AsyncClient()
    client._client = fake  # type: ignore[assignment]

    await client.login("admin", "admin123456")

    assert fake.calls[0][1] == "http://127.0.0.1:18080/api/v1/auth-service/auth/login"
