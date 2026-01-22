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
    async with ApiClient(BASE_URL) as c:
        await c.login(LAWYER_USERNAME, LAWYER_PASSWORD)
        yield c


@pytest.fixture
async def anonymous_client():
    """未登录的 API 客户端"""
    async with ApiClient(BASE_URL) as c:
        yield c
