"""全局 pytest fixtures"""

import os
from pathlib import Path

import httpx
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
INTERNAL_API_KEY = os.getenv("INTERNAL_API_KEY")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123456")
LAWYER_USERNAME = os.getenv("LAWYER_USERNAME", "lawyer1")
LAWYER_PASSWORD = os.getenv("LAWYER_PASSWORD", "lawyer123456")


async def _ensure_seed_packages() -> None:
    """Bootstrap system resources on a fresh DB (no mocks).

    The stack relies on collector-service seed_packages to populate:
    - matters.service_types + playbooks (platform-service)
    - structured knowledge seeds (knowledge-service)
    - templates (templates-service)

    In local docker, these may still be running in the background when E2E starts; make E2E resilient by
    proactively applying required packages when matter-service reports missing config.
    """
    if str(os.getenv("E2E_SKIP_SEED", "") or "").strip().lower() in {"1", "true", "yes"}:
        return
    if not INTERNAL_API_KEY:
        raise RuntimeError("INTERNAL_API_KEY is required for E2E (set repo-root .env or e2e-tests/.env)")

    async with httpx.AsyncClient(timeout=120.0) as c:
        # 1) Fast check: if matter-service can list service types, platform config is ready.
        try:
            resp = await c.get(
                f"{BASE_URL}/api/v1/internal/matter-service/matters/service-types",
                headers={"X-Internal-Api-Key": INTERNAL_API_KEY},
                params={"category": "litigation"},
            )
            if resp.status_code == 200:
                body = resp.json()
                if body.get("code") == 0 and isinstance(body.get("data"), list) and body.get("data"):
                    return
        except Exception:
            # Fall through to seeding (best effort).
            pass

        # 2) Apply the minimum required packages first (must succeed for litigation flows).
        base_payload = {"dry_run": False, "force": False}
        must_packages = ["matters_system_resources", "knowledge_structured_seeds"]
        resp = await c.post(
            f"{BASE_URL}/api/v1/seed-packages/apply-internal",
            headers={"X-Internal-Api-Key": INTERNAL_API_KEY},
            json={**base_payload, "package_ids": must_packages},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"seed_packages apply-internal failed: {body}")
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or data.get("success") is not True:
            raise RuntimeError(f"seed_packages required packages failed: {body}")

        # 3) Templates are required for document-generation. Some optional items (sync templates to sys_templates KB)
        # can fail without breaking the workflow; treat as best-effort but require curated_templates_import success.
        resp = await c.post(
            f"{BASE_URL}/api/v1/seed-packages/apply-internal",
            headers={"X-Internal-Api-Key": INTERNAL_API_KEY},
            json={**base_payload, "package_ids": ["templates_system_resources"]},
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"seed_packages templates_system_resources failed: {body}")

        results = ((body.get("data") or {}) if isinstance(body, dict) else {}).get("results") or []
        ok = False
        for pkg in results if isinstance(results, list) else []:
            if not isinstance(pkg, dict) or str(pkg.get("package_id") or "") != "templates_system_resources":
                continue
            for it in pkg.get("items") if isinstance(pkg.get("items"), list) else []:
                if isinstance(it, dict) and str(it.get("item_id") or "") == "curated_templates_import":
                    res = it.get("result") if isinstance(it.get("result"), dict) else {}
                    if str(res.get("status") or "") == "completed":
                        ok = True
                        break
        if not ok:
            raise RuntimeError(f"seed_packages templates_system_resources did not import templates: {body}")


@pytest.fixture(scope="session", autouse=True)
async def seed_system_resources():
    await _ensure_seed_packages()


@pytest.fixture
async def client():
    """已登录的 API 客户端"""
    async with ApiClient(BASE_URL) as c:
        await c.login(ADMIN_USERNAME, ADMIN_PASSWORD)
        # Fail-fast tenant isolation: internal services require X-Organization-Id.
        # Super admins may not have a default org; pick the first org as the active context for tests.
        if not c.organization_id:
            org_list = await c.get("/api/v1/admin/organizations?page=1&size=5")
            org_id = None
            if isinstance(org_list, dict):
                items = org_list.get("data") if isinstance(org_list.get("data"), list) else []
                if items:
                    first = items[0] if isinstance(items[0], dict) else {}
                    org_id = first.get("id")
            if org_id is not None and str(org_id).strip():
                c.organization_id = str(org_id).strip()
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
            f"/api/v1/internal/user-service/users/{lawyer_user_id}/organization",
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
