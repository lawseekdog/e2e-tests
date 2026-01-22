"""认证相关测试"""

import pytest


@pytest.mark.e2e
@pytest.mark.smoke
async def test_login_success(anonymous_client):
    """测试登录成功"""
    result = await anonymous_client.login("admin", "admin123456")
    assert result["code"] == 0
    assert "access_token" in result["data"]
    assert "refresh_token" in result["data"]


@pytest.mark.e2e
async def test_login_wrong_password(anonymous_client):
    """测试错误密码登录"""
    with pytest.raises(Exception):
        await anonymous_client.login("admin", "wrongpassword")


@pytest.mark.e2e
@pytest.mark.smoke
async def test_get_me(client):
    """测试获取当前用户信息"""
    result = await client.get_me()
    assert result["code"] == 0
    assert result["data"]["username"] == "admin"
