#!/usr/bin/env python3
"""冒烟测试脚本（对齐当前接口协议）。

说明：
- 咨询会话服务使用 X-User-Id / X-Organization-Id 等头作为主上下文（网关不解析 JWT）。
- chat/resume 为 SSE；用 e2e-tests/client/ApiClient 统一处理，避免脚本过时。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Allow `from client.*` when running as a script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from client.api_client import ApiClient

load_dotenv(ROOT / ".env")

BASE_URL = os.getenv("BASE_URL", "http://localhost:18001")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123456")


async def smoke_test():
    """执行冒烟测试"""
    print("=== LawSeekDog 冒烟测试 ===")
    print(f"Target: {BASE_URL}")
    print()

    async with ApiClient(BASE_URL) as c:
        # 1. 登录（并自动填充 X-User-Id / X-Organization-Id / X-Is-Superuser）
        print("1. 测试登录...")
        await c.login(ADMIN_USERNAME, ADMIN_PASSWORD)
        print(f"   ✓ 登录成功 user_id={c.user_id} superuser={c.is_superuser}")

        # 2. 创建咨询会话（默认 service_type_id=legal_consultation）
        print("2. 测试创建咨询会话...")
        sess = await c.create_session(service_type_id="legal_consultation")
        session_id = str((sess.get("data") or {}).get("id") or "").strip()
        assert session_id, f"创建会话失败: {sess}"
        print(f"   ✓ 创建会话成功: {session_id}")

        # 3. 发送消息（SSE）
        print("3. 测试发送消息（SSE）...")
        out = await c.chat(session_id, "你好，我想咨询一个法律问题。", attachments=[], max_loops=2)
        events = out.get("events") if isinstance(out, dict) else None
        assert isinstance(events, list) and events, f"chat SSE 返回异常: {out}"
        print(f"   ✓ 发送消息成功 events={len(events)}")

        print()
        print("=== 所有冒烟测试通过! ===")


def main():
    try:
        asyncio.run(smoke_test())
        sys.exit(0)
    except AssertionError as e:
        print(f"\n✗ 测试失败: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ 发生错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
