"""
用户查询接口自动化测试用例（认证链路）

验证点:
    - 登录签发Token -> Bearer Token认证查询的完整业务链路
    - 数据驱动: 查询参数与预期结果来自 testdata/yaml/api_user_data.yaml
    - 认证反向用例: 无Token/伪造Token访问均被拒绝（HTTP 401 + 业务码2001）
"""

import allure
import pytest

from src.common.assertion import (
    assert_json_value,
    assert_status_code,
)
from tests.conftest import load_yaml_data

# 模块级加载YAML数据（参数化在用例收集阶段生效）
USER_QUERY_CASES = load_yaml_data("api_user_data.yaml")["user_query_cases"]


@allure.feature("用户管理模块")
@allure.story("用户查询接口")
@pytest.mark.api
class TestUserQuery:
    """用户查询接口GET /api/users/<user_id>测试集"""

    @pytest.fixture()
    def valid_token(self, http_client):
        """
        用例级前置fixture: 登录获取有效Token

        参数:
            http_client (HttpClient): 会话级HTTP统一客户端fixture

        返回:
            str: 有效Bearer Token字符串
        """
        response = http_client.post(
            "/api/login", json={"username": "admin", "password": "123456"}
        )
        return response.json()["data"]["token"]

    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.parametrize(
        "case",
        USER_QUERY_CASES,
        ids=[f'{case["case_id"]}-{case["name"]}' for case in USER_QUERY_CASES],
    )
    def test_query_user_with_valid_token(self, case, http_client, valid_token):
        """
        合法令牌用户查询参数化测试

        参数:
            case (dict): 单条查询测试数据（目标用户、期望状态码/业务码/用户名）
            http_client (HttpClient): HTTP统一客户端
            valid_token (str): 前置登录获得的有效Token

        返回:
            无（断言失败时抛出AssertionError）
        """
        allure.dynamic.title(f'{case["case_id"]} {case["name"]}')

        with allure.step("携带有效Token发送用户查询请求"):
            response = http_client.get(
                f'/api/users/{case["query_user_id"]}',
                headers={"Authorization": f"Bearer {valid_token}"},
            )

        with allure.step("校验HTTP状态码与业务码"):
            assert_status_code(response, case["expected_status"])
            assert_json_value(response, "code", case["expected_code"])

        # 用户存在时校验返回的用户信息，不存在的场景跳过信息断言
        if case.get("expected_username"):
            with allure.step("校验返回用户信息"):
                assert_json_value(
                    response, "data.username", case["expected_username"]
                )
                assert_json_value(
                    response, "data.user_id", case["query_user_id"]
                )

    @allure.severity(allure.severity_level.NORMAL)
    @allure.title("无Token访问用户查询接口被拒绝")
    def test_query_user_without_token_rejected(self, http_client):
        """
        认证反向用例: 不携带Authorization头应返回401未授权

        参数:
            http_client (HttpClient): HTTP统一客户端

        返回:
            无
        """
        with allure.step("不携带Token发送用户查询请求"):
            response = http_client.get("/api/users/1")

        with allure.step("校验HTTP状态码401与认证业务码"):
            assert_status_code(response, 401)
            assert_json_value(response, "code", 2001)
            assert_json_value(response, "msg", "未授权或令牌无效")

    @allure.severity(allure.severity_level.NORMAL)
    @allure.title("伪造Token访问用户查询接口被拒绝")
    def test_query_user_with_invalid_token_rejected(self, http_client):
        """
        认证反向用例: 携带伪造Token应返回401未授权

        参数:
            http_client (HttpClient): HTTP统一客户端

        返回:
            无
        """
        with allure.step("携带伪造Token发送用户查询请求"):
            response = http_client.get(
                "/api/users/1",
                headers={"Authorization": "Bearer tm-token-fake"},
            )

        with allure.step("校验HTTP状态码401与认证业务码"):
            assert_status_code(response, 401)
            assert_json_value(response, "code", 2001)
