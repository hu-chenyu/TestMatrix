"""
用户登录接口自动化测试用例（YAML数据驱动）

验证点:
    - 数据驱动: 参数化数据来自 testdata/yaml/api_login_data.yaml
    - HTTP状态码与业务码双层校验（HTTP 200不等于业务成功，经典校验点）
    - 登录成功Token签发 / 登录失败不签发任何数据
"""

import allure
import pytest

from src.common.assertion import (
    assert_is_empty,
    assert_is_not_empty,
    assert_json_value,
    assert_status_code,
)
from tests.conftest import load_yaml_data

# 模块级加载YAML数据（参数化在用例收集阶段生效，无法走fixture机制）
LOGIN_CASES = load_yaml_data("api_login_data.yaml")["login_cases"]


@allure.feature("用户认证模块")
@allure.story("登录接口")
@pytest.mark.api
class TestUserLogin:
    """登录接口POST /api/login测试集（YAML数据驱动）"""

    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.parametrize(
        "case",
        LOGIN_CASES,
        ids=[f'{case["case_id"]}-{case["name"]}' for case in LOGIN_CASES],
    )
    def test_user_login(self, case, http_client):
        """
        登录接口参数化测试

        参数:
            case (dict): 单条登录测试数据（账密、期望状态码、业务码、消息、Token预期）
            http_client (HttpClient): 会话级HTTP统一客户端fixture

        返回:
            无（断言失败时抛出AssertionError，由失败钩子自动记录日志）

        异常:
            HttpClientError: 模拟服务连接失败时由客户端抛出
        """
        # 动态标题: Allure报告中按用例编号+名称展示（比静态标题信息量更足）
        allure.dynamic.title(f'{case["case_id"]} {case["name"]}')

        with allure.step("构造登录请求体"):
            payload = {"username": case["username"], "password": case["password"]}

        with allure.step("发送POST /api/login请求"):
            response = http_client.post("/api/login", json=payload)

        with allure.step("校验HTTP状态码"):
            assert_status_code(response, case["expected_status"])

        with allure.step("校验业务码与业务消息"):
            assert_json_value(response, "code", case["expected_code"])
            assert_json_value(response, "msg", case["expected_msg"])

        with allure.step("校验Token签发逻辑"):
            if case["expect_token"]:
                token = response.json()["data"]["token"]
                assert_is_not_empty(token, "登录成功必须签发非空Token")
            else:
                assert_is_empty(
                    response.json().get("data"),
                    "登录失败不应返回任何业务数据",
                )
