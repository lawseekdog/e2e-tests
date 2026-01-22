"""咨询会话测试"""

import pytest


@pytest.mark.e2e
@pytest.mark.smoke
async def test_create_consultation_session(client):
    """测试创建咨询会话"""
    result = await client.create_session()
    assert result["code"] == 0
    assert "id" in result["data"]
    assert result["data"]["engagement_mode"] == "start_service"


@pytest.mark.e2e
async def test_chat_in_session(client):
    """测试在会话中发送消息"""
    # 创建会话
    session_result = await client.create_session()
    session_id = session_result["data"]["id"]

    # 发送消息
    chat_result = await client.chat(session_id, "你好，我想咨询一个法律问题")
    assert isinstance(chat_result, dict)
    assert isinstance(chat_result.get("events"), list)
    assert chat_result["events"], "SSE 应该返回事件流"
    events = [e.get("event") for e in chat_result["events"] if isinstance(e, dict)]
    # 允许两种情况：
    # - 直接返回 end/output
    # - 触发卡片（后续需要 resume）
    assert ("interrupt" in events) or ("card" in events) or bool(str(chat_result.get("output") or "").strip())


@pytest.mark.e2e
async def test_upgrade_to_service(client):
    """测试咨询升级为事项"""
    # 创建咨询会话
    session_result = await client.create_session()
    session_id = session_result["data"]["id"]

    # 进行一些对话
    await client.chat(session_id, "我想起诉房东不退押金")

    # 升级为事项（通过创建带 service_type_id 的新会话或调用升级接口）
    # 具体实现取决于 API 设计
    pass
