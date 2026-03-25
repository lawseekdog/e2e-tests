from __future__ import annotations

import pytest

from client.api_client import ApiClient


class _FakeResponse:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHttpClient:
    def __init__(self) -> None:
        self.post_calls: list[tuple[str, dict, dict]] = []

    async def post(self, url: str, *, headers: dict, data: dict) -> _FakeResponse:
        self.post_calls.append((url, headers, data))
        return _FakeResponse({"data": {"access_token": "token"}})


@pytest.mark.asyncio
async def test_login_uses_gateway_service_prefix_for_auth_route() -> None:
    client = ApiClient("http://example.com/api/v1")
    fake_http = _FakeHttpClient()
    client._client = fake_http

    async def _fake_get_me() -> dict:
        return {
            "data": {
                "user_id": 1,
                "organization_id": 2,
                "is_superuser": False,
            }
        }

    client.get_me = _fake_get_me  # type: ignore[method-assign]

    await client.login("admin", "admin123456")

    assert fake_http.post_calls
    url, headers, data = fake_http.post_calls[0]
    assert url == "http://example.com/api/v1/auth-service/auth/login"
    assert headers == {"Content-Type": "application/x-www-form-urlencoded"}
    assert data == {"username": "admin", "password": "admin123456"}
