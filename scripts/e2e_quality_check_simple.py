#!/usr/bin/env python3
"""E2E 质量检查脚本 - 简化版

对 E2E 测试完成后的系统状态进行全面质量检查。

用法:
    python e2e_quality_check_simple.py <scenario_name> <session_id>

示例:
    python e2e_quality_check_simple.py contract_review 1
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client.api_client import ApiClient


async def main():
    if len(sys.argv) < 3:
        print("用法: python e2e_quality_check_simple.py <scenario_name> <session_id>")
        print("示例: python e2e_quality_check_simple.py contract_review 1")
        sys.exit(1)

    scenario_name = sys.argv[1]
    session_id = sys.argv[2]

    print(f"\n{'=' * 80}")
    print(f"E2E 质量检查")
    print(f"场景: {scenario_name}")
    print(f"Session ID: {session_id}")
    print(f"{'=' * 80}\n")

    scenario_dir = Path(__file__).parent.parent / "browser-scenarios" / scenario_name
    readme_path = scenario_dir / "README.md"

    if not readme_path.exists():
        print(f"❌ 场景 README 不存在: {readme_path}")
        sys.exit(1)

    content = readme_path.read_text(encoding="utf-8")
    match = re.search(r"```yaml\n(.*?)\n```", content, re.DOTALL)

    if not match:
        print(f"❌ 未找到 Quality Check Expectations YAML 块")
        sys.exit(1)

    yaml_content = match.group(1)
    expectations = yaml.safe_load(yaml_content)

    print(f"✓ 加载场景预期配置\n")
    print("预期配置:")
    print(yaml.dump(expectations, allow_unicode=True, default_flow_style=False))
    print(f"\n{'=' * 80}\n")

    base_url = (
        os.getenv("E2E_BASE_URL")
        or os.getenv("BASE_URL")
        or "http://localhost:18001/api/v1"
    )
    username = os.getenv("E2E_USERNAME") or os.getenv("ADMIN_USERNAME") or "admin"
    password = os.getenv("E2E_PASSWORD") or os.getenv("ADMIN_PASSWORD") or "admin123456"

    print(f"连接信息:")
    print(f"  Base URL: {base_url}")
    print(f"  Username: {username}")
    print()

    try:
        async with ApiClient(base_url) as client:
            print("正在登录...")
            try:
                await asyncio.wait_for(client.login(username, password), timeout=30.0)
                print(f"✓ 登录成功\n")
            except asyncio.TimeoutError:
                print(f"❌ 登录超时")
                sys.exit(1)
            except Exception as e:
                print(f"❌ 登录失败: {e}")
                sys.exit(1)

            print(f"正在获取 Session {session_id} 信息...")
            try:
                session_resp = await asyncio.wait_for(
                    client.get_session(session_id), timeout=10.0
                )
                print(f"✓ Session 响应: {session_resp}\n")

                session_data = (
                    session_resp.get("data") if isinstance(session_resp, dict) else None
                )
                if not session_data:
                    print(f"❌ Session 数据为空")
                    sys.exit(1)

                matter_id = str(session_data.get("matter_id") or "").strip()
                if not matter_id:
                    print(f"❌ Session 没有关联的 matter_id")
                    print(f"   Session 数据: {session_data}")
                    sys.exit(1)

                print(f"✓ Matter ID: {matter_id}")
                print(f"✓ User ID: {client.user_id}")
                print(f"✓ Organization ID: {client.organization_id}\n")

                print(f"{'=' * 80}")
                print("质量检查完成")
                print(f"{'=' * 80}\n")

                print("注意: 这是简化版本，仅验证基本连接和数据获取。")
                print("完整的质量检查需要数据库连接和更多的验证逻辑。")

            except asyncio.TimeoutError:
                print(f"❌ 获取 Session 超时")
                sys.exit(1)
            except Exception as e:
                print(f"❌ 获取 Session 失败: {e}")
                import traceback

                traceback.print_exc()
                sys.exit(1)

    except Exception as e:
        print(f"❌ 检查失败: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
