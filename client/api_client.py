"""E2E 测试 API 客户端"""

from __future__ import annotations

import asyncio
import json
import os
import httpx
from typing import Any
from pathlib import Path


class ApiClient:
    """API 客户端封装"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None
        # Java services (behind nginx) use these headers as the primary auth/context.
        self.user_id: str | None = None
        self.organization_id: str | None = None
        self.is_superuser: bool = False
        # Internal endpoints (e.g. /internal/*) require an internal API key.
        # When running E2E locally, pass it via env (docker-compose/java-stack uses INTERNAL_API_KEY).
        self.internal_api_key: str | None = os.getenv("INTERNAL_API_KEY")
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        # Chat endpoints are SSE streams and may take longer than typical JSON APIs.
        self._client = httpx.AsyncClient(timeout=600.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.user_id:
            headers["X-User-Id"] = str(self.user_id)
        if self.organization_id:
            headers["X-Organization-Id"] = str(self.organization_id)
        if self.is_superuser:
            headers["X-Is-Superuser"] = "true"
        if self.internal_api_key:
            headers["X-Internal-Api-Key"] = str(self.internal_api_key)
        return headers

    async def _request(
        self, method: str, path: str, **kwargs
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("headers", self.headers)
        # Local docker dev: gateway may transiently return 502/503/504 while a service is being recreated.
        # Use a slightly more patient retry policy for GETs to keep E2E stable.
        # Spring Boot services (esp. matter-service) can take ~50s to start; keep enough headroom.
        # Matter-service may take ~60-90s to restart locally (JIT + Flyway). Keep enough headroom so E2E
        # can ride out transient 502/503/504 from the gateway.
        get_retries = int(os.getenv("E2E_HTTP_GET_RETRIES", "60") or 60)
        max_attempts = get_retries if method.upper() == "GET" else 1
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
                if response.status_code in {502, 503, 504} and attempt < max_attempts:
                    # Gateway hiccups happen when services restart in local docker; retry GETs only.
                    await asyncio.sleep(min(4.0, 0.5 * attempt))
                    continue
                response.raise_for_status()
                return response.json()
            except Exception as e:
                last_exc = e
                if attempt >= max_attempts:
                    raise
                await asyncio.sleep(min(4.0, 0.5 * attempt))
        raise last_exc if last_exc else RuntimeError("request failed")

    async def _post_sse(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """POST an SSE endpoint and collect events until the stream ends."""
        url = f"{self.base_url}{path}"
        headers = dict(self.headers)
        headers["Accept"] = "text/event-stream"
        max_attempts = 5
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            events: list[dict[str, Any]] = []
            current_event: str | None = None
            try:
                async with self._client.stream("POST", url, headers=headers, json=data) as response:
                    if response.status_code in {502, 503, 504} and attempt < max_attempts:
                        await asyncio.sleep(min(4.0, 0.5 * attempt))
                        continue
                    response.raise_for_status()
                    try:
                        async for line in response.aiter_lines():
                            if line.startswith("event: "):
                                current_event = line[7:].strip() or None
                                continue
                            if line.startswith("data: "):
                                raw = line[6:].strip()
                                payload: Any = None
                                if raw:
                                    try:
                                        payload = json.loads(raw)
                                    except Exception:
                                        payload = {"raw": raw}
                                evt = current_event or (payload or {}).get("event") if isinstance(payload, dict) else None
                                events.append({"event": evt, "data": payload})
                                current_event = None
                                # Most endpoints use "end" as the last event; stop early to avoid waiting for
                                # proxy/connection teardown quirks.
                                if evt in {"end", "complete"}:
                                    break
                    except (httpx.RemoteProtocolError, httpx.ReadTimeout) as e:
                        # SSE is long-lived; reverse proxies or upstreams may terminate the stream abruptly.
                        # Return whatever we already collected so tests can continue by polling state.
                        events.append({"event": "error", "data": {"error": str(e), "partial": True}})

                output = ""
                for it in reversed(events):
                    if it.get("event") in {"end", "complete"} and isinstance(it.get("data"), dict):
                        output = str(it["data"].get("output") or "")
                        break
                return {"events": events, "output": output}
            except httpx.HTTPStatusError as e:
                last_exc = e
                code = e.response.status_code if e.response is not None else None
                if code in {502, 503, 504} and attempt < max_attempts:
                    await asyncio.sleep(min(4.0, 0.5 * attempt))
                    continue
                raise
            except Exception as e:
                last_exc = e
                if attempt >= max_attempts:
                    raise
                await asyncio.sleep(min(4.0, 0.5 * attempt))

        raise last_exc if last_exc else RuntimeError("sse request failed")

    async def get(self, path: str, **kwargs) -> dict[str, Any]:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, data: dict | None = None, **kwargs) -> dict[str, Any]:
        return await self._request("POST", path, json=data, **kwargs)

    async def put(self, path: str, data: dict | None = None, **kwargs) -> dict[str, Any]:
        return await self._request("PUT", path, json=data, **kwargs)

    async def patch(self, path: str, data: dict | None = None, **kwargs) -> dict[str, Any]:
        return await self._request("PATCH", path, json=data, **kwargs)

    async def delete(self, path: str, **kwargs) -> dict[str, Any]:
        return await self._request("DELETE", path, **kwargs)

    # ========== Auth ==========

    async def login(self, username: str, password: str) -> dict[str, Any]:
        # NOTE: auth-service exposes a form login endpoint; JSON login may be disabled by server config.
        # Use x-www-form-urlencoded to keep E2E stable across gateway/service implementations.
        url = f"{self.base_url}/api/v1/auth/login"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = await self._client.post(url, headers=headers, data={"username": username, "password": password})
        resp.raise_for_status()
        result = resp.json()
        self.token = result["data"]["access_token"]
        # Populate X-User-Id / X-Organization-Id for downstream services.
        me = await self.get_me()
        self.user_id = str(me["data"]["user_id"])
        org_id = me["data"].get("organization_id")
        self.organization_id = str(org_id) if org_id is not None and str(org_id).strip() else None
        self.is_superuser = bool(me["data"].get("is_superuser"))
        return result

    async def get_me(self) -> dict[str, Any]:
        return await self.get("/api/v1/auth/me")

    # ========== Consultations ==========

    async def create_session(
        self,
        engagement_mode: str = "start_service",
        service_type_id: str | None = None,
        matter_id: str | None = None,
    ) -> dict[str, Any]:
        data = {"engagement_mode": engagement_mode}
        if service_type_id:
            data["service_type_id"] = service_type_id
        if matter_id:
            data["matter_id"] = matter_id
        return await self.post("/api/v1/consultations/sessions", data)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self.get(f"/api/v1/consultations/sessions/{session_id}")

    async def chat(
        self,
        session_id: str,
        user_query: str,
        attachments: list[str] | None = None,
        max_loops: int | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "user_id": int(self.user_id) if self.user_id else None,
            "user_query": user_query,
            "attachments": attachments or [],
        }
        if max_loops is not None:
            data["max_loops"] = max_loops
        return await self._post_sse(f"/api/v1/consultations/sessions/{session_id}/chat", data)

    async def get_pending_card(self, session_id: str) -> dict[str, Any]:
        return await self.get(f"/api/v1/consultations/sessions/{session_id}/pending_card")

    async def resume(
        self,
        session_id: str,
        user_response: dict[str, Any],
    ) -> dict[str, Any]:
        data = {
            "user_id": int(self.user_id) if self.user_id else None,
            "user_response": user_response,
        }
        return await self._post_sse(f"/api/v1/consultations/sessions/{session_id}/resume", data)

    async def switch_service_type(
        self,
        session_id: str,
        *,
        service_type_id: str,
        title: str | None = None,
        cause_of_action_code: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"service_type_id": str(service_type_id)}
        if title is not None:
            payload["title"] = str(title)
        if cause_of_action_code is not None:
            payload["cause_of_action_code"] = str(cause_of_action_code)
        return await self.post(f"/api/v1/consultations/sessions/{session_id}/service-type", payload)

    # ========== Files ==========

    async def upload_file(
        self,
        file_path: str,
        purpose: str = "consultation",
    ) -> dict[str, Any]:
        """Upload a file via files-service (multipart)."""
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(file_path)

        url = f"{self.base_url}/api/v1/files/upload"
        headers = dict(self.headers)
        # Let httpx set multipart boundary.
        headers.pop("Content-Type", None)

        params: dict[str, Any] = {"purpose": purpose}
        if self.user_id:
            params["user_id"] = int(self.user_id)

        with path.open("rb") as f:
            files = {"file": (path.name, f)}
            resp = await self._client.post(url, headers=headers, params=params, files=files)
            resp.raise_for_status()
            return resp.json()

    async def download_file_bytes(self, file_id: str) -> bytes:
        """Download a file's raw bytes via files-service."""
        fid = str(file_id).strip()
        if not fid:
            raise ValueError("file_id is required")
        url = f"{self.base_url}/api/v1/files/{fid}/download"
        headers = dict(self.headers)
        headers.pop("Content-Type", None)
        resp = await self._client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.content

    # ========== Matters ==========

    async def create_matter(
        self,
        service_type_id: str,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        data = {"service_type_id": service_type_id}
        if client_id:
            data["client_id"] = client_id
        return await self.post("/api/v1/matters", data)

    async def get_matter(self, matter_id: str) -> dict[str, Any]:
        return await self.get(f"/api/v1/matters/{matter_id}")

    async def get_matter_tasks(self, matter_id: str) -> dict[str, Any]:
        return await self.get(f"/api/v1/matters/{matter_id}/tasks")

    async def complete_task(
        self, matter_id: str, task_id: str, result: dict
    ) -> dict[str, Any]:
        return await self.post(
            f"/api/v1/matters/{matter_id}/tasks/{task_id}/complete",
            result
        )

    async def get_workflow_snapshot(self, matter_id: str) -> dict[str, Any]:
        return await self.get(f"/api/v1/matters/{matter_id}/workflow")

    async def get_workflow_profile(self, matter_id: str) -> dict[str, Any]:
        return await self.get(f"/api/v1/matters/{matter_id}/workflow/profile")

    async def list_deliverables(self, matter_id: str, output_key: str | None = None) -> dict[str, Any]:
        params = {}
        if output_key:
            params["output_key"] = output_key
        return await self.get(f"/api/v1/matters/{matter_id}/deliverables", params=params)

    async def list_traces(self, matter_id: str, limit: int | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = int(limit)
        return await self.get(f"/api/v1/matters/{matter_id}/traces", params=params)

    # ========== Knowledge ==========

    async def search_knowledge(
        self,
        query: str,
        doc_types: list[str] | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        data = {"query": query, "top_k": top_k}
        if doc_types:
            data["doc_types"] = doc_types
        return await self.post("/api/v1/knowledge/search", data)
