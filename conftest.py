"""全局 pytest fixtures"""

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from client.api_client import ApiClient

# Prefer repo-root .env (docker-compose/dev defaults) then allow e2e-tests/.env to add
# BASE_URL/user creds without overwriting INTERNAL_API_KEY.
repo_root_env = Path(__file__).resolve().parent.parent / ".env"
if repo_root_env.exists():
    load_dotenv(repo_root_env, override=False)
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

BASE_URL = os.getenv("BASE_URL", "http://localhost:18001")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123456")
LAWYER_USERNAME = os.getenv("LAWYER_USERNAME", "lawyer1")
LAWYER_PASSWORD = os.getenv("LAWYER_PASSWORD", "lawyer123456")


@pytest.fixture
async def client():
    """已登录的 API 客户端"""
    async with ApiClient(BASE_URL) as c:
        await c.login(ADMIN_USERNAME, ADMIN_PASSWORD)
        yield c


@pytest.fixture
async def lawyer_client():
    """已登录（律师身份）的 API 客户端，用于事项/待办/阶段推进链路。"""
    # E2E local docker env may only seed the super admin by default. Ensure a lawyer user exists
    # (idempotent) so tests don't depend on manual DB prep.
    async with ApiClient(BASE_URL) as admin:
        await admin.login(ADMIN_USERNAME, ADMIN_PASSWORD)

        # Check if lawyer user exists.
        resp = await admin.get(f"/api/v1/admin/users?page=1&size=5&q={LAWYER_USERNAME}")
        existing = None
        if isinstance(resp, dict):
            for it in resp.get("data") if isinstance(resp.get("data"), list) else []:
                if isinstance(it, dict) and str(it.get("username") or "").strip() == LAWYER_USERNAME:
                    existing = it
                    break

        lawyer_user_id = None
        if existing is None:
            created = await admin.post(
                "/api/v1/admin/users",
                {
                    "username": LAWYER_USERNAME,
                    "initial_password": LAWYER_PASSWORD,
                    "full_name": "Lawyer One (E2E)",
                    "email": "lawyer1@example.com",
                },
            )
            # Admin endpoints return ApiResponse; unwrap to find id.
            created_data = created.get("data") if isinstance(created, dict) else None
            if isinstance(created_data, dict):
                lawyer_user_id = created_data.get("id")
            if lawyer_user_id is None:
                raise RuntimeError(f"failed to create lawyer user: {created}")

            # Mark as lawyer.
            await admin.put(
                f"/api/v1/admin/users/{lawyer_user_id}/user-type",
                {"user_type": "lawyer"},
            )
        else:
            lawyer_user_id = existing.get("id")

        if lawyer_user_id is None:
            raise RuntimeError(f"failed to resolve lawyer user id: {resp}")

        # Ensure there is at least one organization and bind the lawyer user to it so downstream services
        # receive X-Organization-Id and can auto-kickoff matters.
        org_list = await admin.get("/api/v1/admin/organizations?page=1&size=5")
        org_id = None
        if isinstance(org_list, dict):
            items = org_list.get("data") if isinstance(org_list.get("data"), list) else []
            if items:
                first = items[0] if isinstance(items[0], dict) else {}
                org_id = first.get("id")

        if org_id is None:
            created_org = await admin.post(
                "/api/v1/admin/organizations",
                {
                    "name": "E2E Law Firm",
                    "practice_area": "civil",
                    "law_firm_license": "E2E-001",
                    "owner_user_id": int(lawyer_user_id),
                },
            )
            org_data = created_org.get("data") if isinstance(created_org, dict) else None
            if isinstance(org_data, dict):
                org_id = org_data.get("id")
        if org_id is None:
            raise RuntimeError(f"failed to ensure organization: {org_list}")

        await admin.patch(
            f"/internal/user-service/internal/users/{lawyer_user_id}/organization",
            {"organization_id": int(org_id)},
        )

    async with ApiClient(BASE_URL) as c:
        await c.login(LAWYER_USERNAME, LAWYER_PASSWORD)
        yield c


@pytest.fixture
async def anonymous_client():
    """未登录的 API 客户端"""
    async with ApiClient(BASE_URL) as c:
        yield c
