"""
系统健康检查接口测试用例（冒烟链路）

验证点:
    - GET /api/ping服务存活校验
    - HTTP状态码、JSON子集、嵌套路径字段、响应头、响应耗时、包含关系六类断言演示
    - 验证框架最小闭环: 客户端请求 -> 断言库校验 -> Allure结果生成
"""

import allure
import pytest

from src.common.assertion import (
    assert_contains,
    assert_header,
    assert_json_contains,
    assert_json_value,
    assert_response_time,
    assert_status_code,
)


@allure.feature("系统监控模块")
@allure.story("健康检查接口")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.api
@pytest.mark.smoke
class TestServiceHealth:
    """健康检查接口GET /api/ping测试集"""

    @allure.title("健康检查接口返回服务存活状态")
    @allure.description(
        "验证模拟服务ping接口的HTTP状态码、业务字段、嵌套路径字段、"
        "自定义版本响应头、JSON内容类型与响应耗时基线"
    )
    def test_ping_service_alive(self, http_client):
        """
        健康检查冒烟用例（核心链路，每次构建必跑）

        参数:
            http_client (HttpClient): 会话级HTTP统一客户端fixture

        返回:
            无（断言失败时抛出AssertionError）

        异常:
            HttpClientError: 模拟服务连接失败时由客户端抛出
        """
        with allure.step("发送GET /api/ping健康检查请求"):
            response = http_client.get("/api/ping")

        with allure.step("校验HTTP状态码为200"):
            assert_status_code(response, 200)

        with allure.step("校验响应业务字段子集"):
            assert_json_contains(response, {"code": 0, "msg": "pong"})

        with allure.step("校验嵌套路径字段（点号路径取值演示）"):
            assert_json_value(response, "data.service", "testmatrix-mock-api")
            assert_json_value(response, "data.version", "1.0.0")

        with allure.step("校验自定义响应头与内容类型"):
            assert_header(response, "X-Service-Version", "1.0.0")
            assert_contains(
                response.headers.get("Content-Type", ""),
                "application/json",
                "健康检查接口必须返回JSON内容类型",
            )

        with allure.step("校验响应耗时低于性能基线（2秒）"):
            assert_response_time(response, 2000)
