"""E2E 测试 API 客户端"""

from __future__ import annotations

import asyncio
import json
import os
import httpx
import websockets
from typing import Any
from pathlib import Path
from urllib.parse import urlparse


# External gateway is standardized to: /api/v1/<service>/**
# So within the gateway prefix (/api/v1), service routes must NOT include another /api/v1.
AUTH = "/auth-service"
USER = "/user-service"
ORG = "/organization-service"
CONSULTATIONS = "/consultations-service"
FILES = "/files-service"
MATTERS = "/matter-service"
KNOWLEDGE = "/knowledge-service"

_WS_DEBUG = str(os.getenv("E2E_WS_DEBUG", "") or "").strip().lower() in {"1", "true", "yes"}


class ApiClient:
    """API 客户端封装"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None
        # Java services (behind nginx) use these headers as the primary auth/context.
        self.user_id: str | None = None
        self.organization_id: str | None = None
        self.is_superuser: bool = False
        # Internal endpoints (e.g. /{service}/api/v1/internal/*) require an internal API key.
        # When running E2E locally, pass it via env (docker-compose/java-stack uses INTERNAL_API_KEY).
        self.internal_api_key: str | None = os.getenv("INTERNAL_API_KEY")
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ApiClient":
        # Chat endpoints are SSE streams and may take longer than typical JSON APIs.
        timeout_s = float(os.getenv("E2E_HTTP_TIMEOUT_S", "1800") or 1800)
        self._client = httpx.AsyncClient(timeout=timeout_s)
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

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("headers", self.headers)
        client = self._client
        if client is None:
            raise RuntimeError(
                "ApiClient is not initialized; use 'async with ApiClient(...)'"
            )
        # Local docker dev: gateway may transiently return 502/503/504 while a service is being recreated.
        # Use a slightly more patient retry policy for GETs to keep E2E stable.
        # Spring Boot services (esp. matter-service) can take ~50s to start; keep enough headroom.
        # Matter-service may take ~60-90s to restart locally (JIT + Flyway). Keep enough headroom so E2E
        # can ride out transient 502/503/504 from the gateway.
        # In local docker, Spring services may restart (Flyway, JIT warmup) and nginx returns 502/503/504
        # for several minutes. Keep this tolerant by default; override via E2E_HTTP_GET_RETRIES in CI.
        get_retries = int(os.getenv("E2E_HTTP_GET_RETRIES", "180") or 180)
        max_attempts = get_retries if method.upper() == "GET" else 1
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.request(method, url, **kwargs)
            except httpx.RequestError as e:
                # Network/connect/read timeouts can happen during local service restarts.
                last_exc = e
                if attempt >= max_attempts:
                    raise
                await asyncio.sleep(min(4.0, 0.5 * attempt))
                continue

            if response.status_code in {502, 503, 504} and attempt < max_attempts:
                # Gateway hiccups happen when services restart in local docker; retry GETs only.
                await asyncio.sleep(min(4.0, 0.5 * attempt))
                continue

            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                # Do not retry on expected 4xx (e.g. probing legacy endpoints). Only retry transient 5xx.
                code = e.response.status_code if e.response is not None else None
                if code in {502, 503, 504} and attempt < max_attempts:
                    last_exc = e
                    await asyncio.sleep(min(4.0, 0.5 * attempt))
                    continue
                raise

            return response.json()
        raise last_exc if last_exc else RuntimeError("request failed")

    async def _post_ws(
        self,
        ws_path: str,
        msg_type: str,
        data: dict[str, Any],
        *,
        max_attempts: int | None = None,
        open_timeout_s: float | None = None,
    ) -> dict[str, Any]:
        """Connect to WebSocket, authenticate, send message, and collect events until 'end'."""
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        # base_url may include a path prefix (e.g. /api/v1 behind APISIX).
        # WebSocket URLs must include the same prefix, otherwise APISIX will 404 the handshake.
        base_path = (parsed.path or "").rstrip("/")
        ws_url = f"{scheme}://{parsed.netloc}{base_path}{ws_path}"

        if max_attempts is None:
            max_attempts = int(os.getenv("E2E_HTTP_WS_RETRIES", "180") or 180)
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            events: list[dict[str, Any]] = []
            try:
                async with websockets.connect(
                    ws_url,
                    close_timeout=10,
                    open_timeout=open_timeout_s,
                    # Consultations-service speaks "ping"/"pong" as app-level JSON events; some
                    # proxies don't reliably forward WebSocket protocol pings in local dev.
                    # Disable protocol-level keepalive to avoid spurious 1011 ping timeouts.
                    ping_interval=None,
                ) as ws:
                    # Send auth message first
                    auth_msg = {
                        "type": "auth",
                        "user_id": int(self.user_id) if self.user_id else None,
                        "organization_id": self.organization_id,
                    }
                    await ws.send(json.dumps(auth_msg))

                    # Wait for auth_success
                    auth_response = await asyncio.wait_for(ws.recv(), timeout=30)
                    auth_data = json.loads(auth_response)
                    if auth_data.get("event") != "auth_success":
                        raise RuntimeError(f"WebSocket auth failed: {auth_data}")

                    # Send the actual message
                    msg = {"type": msg_type, **data}
                    await ws.send(json.dumps(msg))

                    # Collect events until 'end'
                    while True:
                        try:
                            ws_event_timeout_s = float(os.getenv("E2E_WS_EVENT_TIMEOUT_S", "120") or 120)
                            raw = await asyncio.wait_for(ws.recv(), timeout=ws_event_timeout_s)
                            payload = json.loads(raw)
                            evt = payload.get("event")
                            evt_data = payload.get("data", payload)

                            # Handle ping
                            if evt == "ping":
                                # consultations-service heartbeats are one-way app-level events.
                                # Do not reply with an unsupported websocket message type.
                                continue

                            if _WS_DEBUG and evt and evt not in {"delta", "token"}:
                                # Keep debug logs small; avoid dumping cards/deltas.
                                summary = ""
                                if isinstance(evt_data, dict):
                                    if evt in {"task_start", "task_end"}:
                                        summary = str(evt_data.get("node") or evt_data.get("name") or "")
                                    elif evt == "progress":
                                        summary = str(evt_data.get("phase") or evt_data.get("message") or "")
                                    elif evt == "card":
                                        summary = str(evt_data.get("skill_id") or evt_data.get("title") or "")
                                    elif evt in {"error", "end"}:
                                        summary = str(evt_data.get("message") or evt_data.get("output") or "")
                                print(f"[ws] {evt} {summary}".strip(), flush=True)

                            events.append({"event": evt, "data": evt_data})

                            if evt in {"end", "complete"}:
                                break
                        except asyncio.TimeoutError:
                            events.append(
                                {
                                    "event": "error",
                                    "data": {"error": "timeout", "partial": True},
                                }
                            )
                            break

                output = ""
                for it in reversed(events):
                    if it.get("event") in {"end", "complete"} and isinstance(
                        it.get("data"), dict
                    ):
                        output = str(it["data"].get("output") or "")
                        break
                return {"events": events, "output": output}

            except Exception as e:
                last_exc = e
                if attempt >= max_attempts:
                    raise
                await asyncio.sleep(min(4.0, 0.5 * attempt))

        raise last_exc if last_exc else RuntimeError("websocket request failed")

    # Hard-cut: consultations-service streaming is WebSocket-only (SSE endpoints removed).

    async def get(self, path: str, **kwargs) -> dict[str, Any]:
        return await self._request("GET", path, **kwargs)

    async def post(
        self, path: str, data: dict[str, Any] | None = None, **kwargs
    ) -> dict[str, Any]:
        return await self._request("POST", path, json=data, **kwargs)

    async def put(
        self, path: str, data: dict[str, Any] | None = None, **kwargs
    ) -> dict[str, Any]:
        return await self._request("PUT", path, json=data, **kwargs)

    async def patch(
        self, path: str, data: dict[str, Any] | None = None, **kwargs
    ) -> dict[str, Any]:
        return await self._request("PATCH", path, json=data, **kwargs)

    async def delete(self, path: str, **kwargs) -> dict[str, Any]:
        return await self._request("DELETE", path, **kwargs)

    # ========== Auth ==========

    async def login(self, username: str, password: str) -> dict[str, Any]:
        # NOTE: auth-service exposes a form login endpoint; JSON login may be disabled by server config.
        # Use x-www-form-urlencoded to keep E2E stable across gateway/service implementations.
        url = f"{self.base_url}{AUTH}/auth/login"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        max_attempts = int(os.getenv("E2E_HTTP_LOGIN_RETRIES", "180") or 180)
        transient = {500, 502, 503, 504}
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                client = self._client
                if client is None:
                    raise RuntimeError(
                        "ApiClient is not initialized; use 'async with ApiClient(...)'"
                    )
                resp = await client.post(
                    url,
                    headers=headers,
                    data={"username": username, "password": password},
                )
                if resp.status_code in transient and attempt < max_attempts:
                    # Local docker dev: user-service/auth dependencies may restart and briefly cause 5xx.
                    await asyncio.sleep(min(4.0, 0.5 * attempt))
                    continue
                resp.raise_for_status()
                result = resp.json()
                self.token = result["data"]["access_token"]

                # Populate X-User-Id / X-Organization-Id for downstream services.
                me = None
                for j in range(1, max_attempts + 1):
                    try:
                        me = await self.get_me()
                        break
                    except httpx.HTTPStatusError as e:
                        code = (
                            e.response.status_code if e.response is not None else None
                        )
                        if code in transient and j < max_attempts:
                            await asyncio.sleep(min(4.0, 0.5 * j))
                            continue
                        raise
                    except Exception as e:
                        if j >= max_attempts:
                            raise
                        await asyncio.sleep(min(4.0, 0.5 * j))

                if not isinstance(me, dict) or "data" not in me:
                    raise RuntimeError(f"unexpected /auth/me payload: {me}")

                self.user_id = str(me["data"]["user_id"])
                # Downstream Java services require X-Organization-Id. For E2E/local dev we allow forcing
                # an org id via env, otherwise use /auth/me (and fall back to "0" if missing).
                forced_org = os.getenv("E2E_ORGANIZATION_ID") or os.getenv(
                    "DEFAULT_ORGANIZATION_ID"
                )
                if forced_org and str(forced_org).strip():
                    org_id = forced_org
                else:
                    org_id = (
                        me["data"].get("organization_id")
                        or me["data"].get("organizationId")
                        or "0"
                    )
                self.organization_id = (
                    str(org_id) if org_id is not None and str(org_id).strip() else None
                )
                self.is_superuser = bool(me["data"].get("is_superuser"))
                return result
            except Exception as e:
                last_exc = e
                if attempt >= max_attempts:
                    raise
                await asyncio.sleep(min(4.0, 0.5 * attempt))

        raise last_exc if last_exc else RuntimeError("login failed")

    async def get_me(self) -> dict[str, Any]:
        return await self.get(f"{AUTH}/auth/me")

    # ========== Consultations ==========

    async def create_session(
        self,
        title: str | None = None,
        service_type_id: str | None = None,
        matter_id: str | None = None,
        client_role: str | None = None,
    ) -> dict[str, Any]:
        """Create a consultation session (hard-cut: sessions are matter-backed).

        consultations-service hard-cuts create-session payload to: {title?, matter_id?}.
        To keep E2E tests expressive, allow callers to pass service_type_id/client_role and
        transparently create a matter first.
        """
        mid = str(matter_id or "").strip() or None
        st = str(service_type_id or "").strip() or None
        # client_role is now inferred from service_type_id in workbench-mode; keep it as a no-op hint.
        _ = client_role

        payload: dict[str, Any] = {}
        t = str(title or "").strip()
        if t:
            payload["title"] = t

        if mid is None and st:
            created = await self.create_matter(service_type_id=st, title=t or None)
            mid = str(((created.get("data") or {}) if isinstance(created, dict) else {}).get("id") or "").strip() or None

        if mid:
            payload["matter_id"] = mid

        return await self.post(f"{CONSULTATIONS}/consultations/sessions", payload)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self.get(f"{CONSULTATIONS}/consultations/sessions/{session_id}")

    async def chat(
        self,
        session_id: str,
        user_query: str,
        attachments: list[str] | None = None,
        max_loops: int | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "user_query": user_query,
            "attachments": attachments or [],
        }
        if max_loops is not None:
            data["max_loops"] = max_loops
        ws_path = f"{CONSULTATIONS}/consultations/sessions/{session_id}/ws"
        return await self._post_ws(ws_path, "chat", data)

    async def get_pending_card(self, session_id: str) -> dict[str, Any]:
        return await self.get(
            f"{CONSULTATIONS}/consultations/sessions/{session_id}/pending_card"
        )

    async def upload_session_attachment(
        self, session_id: str, file_path: str
    ) -> dict[str, Any]:
        """Upload an attachment bound to a consultation session (so canvas.evidence_list can show it)."""
        path = Path(file_path)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(file_path)

        sid = str(session_id).strip()
        if not sid:
            raise ValueError("session_id is required")

        url = f"{self.base_url}{CONSULTATIONS}/consultations/sessions/{sid}/attachments"
        headers = dict(self.headers)
        headers.pop("Content-Type", None)  # Let httpx set multipart boundary.

        max_attempts = int(os.getenv("E2E_HTTP_UPLOAD_RETRIES", "60") or 60)
        transient = {500, 502, 503, 504}
        last_exc: Exception | None = None

        client = self._client
        if client is None:
            raise RuntimeError(
                "ApiClient is not initialized; use 'async with ApiClient(...)'"
            )
        for attempt in range(1, max_attempts + 1):
            try:
                with path.open("rb") as f:
                    files = {"file": (path.name, f)}
                    resp = await client.post(url, headers=headers, files=files)
                if resp.status_code in transient and attempt < max_attempts:
                    await asyncio.sleep(min(4.0, 0.5 * attempt))
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_exc = e
                if attempt >= max_attempts:
                    raise
                await asyncio.sleep(min(4.0, 0.5 * attempt))

        raise last_exc if last_exc else RuntimeError("upload session attachment failed")

    async def get_session_canvas(self, session_id: str) -> dict[str, Any]:
        return await self.get(
            f"{CONSULTATIONS}/consultations/sessions/{session_id}/canvas"
        )

    async def get_session_timeline(
        self, session_id: str, limit: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = int(limit)
        return await self.get(
            f"{CONSULTATIONS}/consultations/sessions/{session_id}/timeline",
            params=params,
        )

    async def list_session_traces(
        self, session_id: str, limit: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = int(limit)
        return await self.get(
            f"{CONSULTATIONS}/consultations/sessions/{session_id}/traces",
            params=params,
        )

    async def get_session_trace_detail(
        self, session_id: str, trace_id: str
    ) -> dict[str, Any]:
        tid = str(trace_id).strip()
        if not tid:
            raise ValueError("trace_id is required")
        return await self.get(
            f"{CONSULTATIONS}/consultations/sessions/{session_id}/traces/{tid}"
        )

    async def resume(
        self,
        session_id: str,
        user_response: dict[str, Any],
        pending_card: dict[str, Any] | None = None,
        max_loops: int | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "user_response": user_response,
        }
        if pending_card:
            data["pending_card"] = pending_card
        if max_loops is not None:
            data["max_loops"] = int(max_loops)
        ws_path = f"{CONSULTATIONS}/consultations/sessions/{session_id}/ws"
        return await self._post_ws(ws_path, "resume", data)

    async def workflow_action(
        self,
        session_id: str,
        *,
        workflow_action: str,
        workflow_action_params: dict[str, Any] | None = None,
        attachments: list[str] | None = None,
        max_loops: int | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "workflow_action": workflow_action,
            "workflow_action_params": workflow_action_params or {},
            "attachments": attachments or [],
        }
        if max_loops is not None:
            data["max_loops"] = int(max_loops)
        ws_path = f"{CONSULTATIONS}/consultations/sessions/{session_id}/ws"
        return await self._post_ws(ws_path, "actions", data)

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
        return await self.post(
            f"{CONSULTATIONS}/consultations/sessions/{session_id}/service-type",
            payload,
        )

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

        url = f"{self.base_url}{FILES}/files/upload"
        headers = dict(self.headers)
        # Let httpx set multipart boundary.
        headers.pop("Content-Type", None)

        params: dict[str, Any] = {"purpose": purpose}
        if self.user_id:
            params["user_id"] = int(self.user_id)

        max_attempts = int(os.getenv("E2E_HTTP_UPLOAD_RETRIES", "60") or 60)
        transient = {500, 502, 503, 504}
        last_exc: Exception | None = None

        client = self._client
        if client is None:
            raise RuntimeError(
                "ApiClient is not initialized; use 'async with ApiClient(...)'"
            )
        for attempt in range(1, max_attempts + 1):
            try:
                with path.open("rb") as f:
                    files = {"file": (path.name, f)}
                    resp = await client.post(
                        url, headers=headers, params=params, files=files
                    )
                if resp.status_code in transient and attempt < max_attempts:
                    await asyncio.sleep(min(4.0, 0.5 * attempt))
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last_exc = e
                if attempt >= max_attempts:
                    raise
                await asyncio.sleep(min(4.0, 0.5 * attempt))

        raise last_exc if last_exc else RuntimeError("upload file failed")

    async def download_file_bytes(self, file_id: str) -> bytes:
        """Download a file's raw bytes via files-service."""
        fid = str(file_id).strip()
        if not fid:
            raise ValueError("file_id is required")
        url = f"{self.base_url}{FILES}/files/{fid}/download"
        headers = dict(self.headers)
        headers.pop("Content-Type", None)
        client = self._client
        if client is None:
            raise RuntimeError(
                "ApiClient is not initialized; use 'async with ApiClient(...)'"
            )
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.content

    # ========== Matters ==========

    async def create_matter(
        self,
        service_type_id: str,
        title: str | None = None,
        file_ids: list[str] | None = None,
        cause_of_action_code: str | None = None,
        matter_category: str | None = None,
    ) -> dict[str, Any]:
        st = str(service_type_id or "").strip()
        if not st:
            raise ValueError("service_type_id is required")

        t = str(title or "").strip() or f"E2E matter ({st})"
        data: dict[str, Any] = {"title": t, "service_type_id": st}
        if file_ids:
            data["file_ids"] = [str(x).strip() for x in file_ids if str(x).strip()]
        if cause_of_action_code:
            data["cause_of_action_code"] = str(cause_of_action_code).strip()
        if matter_category:
            data["matter_category"] = str(matter_category).strip()
        return await self.post(f"{MATTERS}/lawyer/matters", data)

    async def get_matter(self, matter_id: str) -> dict[str, Any]:
        return await self.get(f"{MATTERS}/lawyer/matters/{matter_id}")

    async def get_matter_tasks(self, matter_id: str) -> dict[str, Any]:
        return await self.get(f"{MATTERS}/matters/{matter_id}/tasks")

    async def complete_task(
        self, matter_id: str, task_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        return await self.post(
            f"{MATTERS}/matters/{matter_id}/tasks/{task_id}/complete", result
        )

    async def get_workflow_snapshot(self, matter_id: str) -> dict[str, Any]:
        return await self.get(f"{MATTERS}/matters/{matter_id}/workflow")

    async def get_workflow_profile(self, matter_id: str) -> dict[str, Any]:
        return await self.get(f"{MATTERS}/matters/{matter_id}/workflow/profile")

    async def list_deliverables(
        self, matter_id: str, output_key: str | None = None
    ) -> dict[str, Any]:
        params = {}
        if output_key:
            params["output_key"] = output_key
        return await self.get(
            f"{MATTERS}/lawyer/matters/{matter_id}/deliverables", params=params
        )

    async def list_traces(
        self, matter_id: str, limit: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = int(limit)
        try:
            return await self.get(f"{MATTERS}/lawyer/matters/{matter_id}/traces", params=params)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                return await self.get(f"{MATTERS}/matters/{matter_id}/traces", params=params)
            raise

    async def get_matter_timeline(
        self, matter_id: str, limit: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = int(limit)
        try:
            return await self.get(f"{MATTERS}/lawyer/matters/{matter_id}/timeline", params=params)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                return await self.get(f"{MATTERS}/matters/{matter_id}/timeline", params=params)
            raise

    async def get_matter_phase_timeline(self, matter_id: str) -> dict[str, Any]:
        try:
            return await self.get(f"{MATTERS}/lawyer/matters/{matter_id}/phase-timeline")
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                return await self.get(f"{MATTERS}/matters/{matter_id}/phase-timeline")
            raise

    # ========== Knowledge ==========

    async def search_knowledge(
        self,
        query: str,
        doc_types: list[str] | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"query": query, "top_k": top_k}
        if doc_types:
            data["doc_types"] = doc_types
        return await self.post(f"{KNOWLEDGE}/knowledge/search", data)
