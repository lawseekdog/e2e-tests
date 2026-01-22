#!/usr/bin/env python3
"""冒烟测试脚本"""

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("BASE_URL", "http://localhost:18001")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123456")


async def smoke_test():
    """执行冒烟测试"""
    print(f"=== LawSeekDog 冒烟测试 ===")
    print(f"Target: {BASE_URL}")
    print()

    async with httpx.AsyncClient(timeout=30.0) as client:
        # 1. 登录
        print("1. 测试登录...")
        response = await client.post(
            f"{BASE_URL}/api/v1/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
        )
        assert response.status_code == 200, f"登录失败: {response.text}"
        token = response.json()["data"]["access_token"]
        print("   ✓ 登录成功")

        headers = {"Authorization": f"Bearer {token}"}

        # 2. 获取用户信息
        print("2. 测试获取用户信息...")
        response = await client.get(f"{BASE_URL}/api/v1/auth/me", headers=headers)
        assert response.status_code == 200, f"获取用户信息失败: {response.text}"
        print("   ✓ 获取用户信息成功")

        # 3. 创建咨询会话
        print("3. 测试创建咨询会话...")
        response = await client.post(
            f"{BASE_URL}/api/v1/consultations/sessions",
            headers=headers,
            json={"engagement_mode": "legal_consultation"}
        )
        assert response.status_code == 200, f"创建会话失败: {response.text}"
        session_id = response.json()["data"]["id"]
        print(f"   ✓ 创建会话成功: {session_id}")

        # 4. 发送消息
        print("4. 测试发送消息...")
        response = await client.post(
            f"{BASE_URL}/api/v1/consultations/sessions/{session_id}/chat",
            headers=headers,
            json={"message": "你好"}
        )
        assert response.status_code == 200, f"发送消息失败: {response.text}"
        print("   ✓ 发送消息成功")

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
