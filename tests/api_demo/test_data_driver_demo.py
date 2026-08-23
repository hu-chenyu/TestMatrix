"""
数据驱动引擎演示用例（第二阶段）

演示 src/core/data_driver.py 数据驱动引擎的完整能力:
    1. YAML数据驱动: load_cases统一入口加载标准字段YAML数据并参数化执行
    2. Excel数据驱动: load_cases加载Excel数据（指定sheet）并参数化执行
    3. 三维筛选验证: filter_cases按module/priority/tags过滤及组合过滤

演示数据文件:
    - testdata/yaml/api_user_query_matrix.yaml  （YAML标准字段格式）
    - testdata/excel/api_user_query_matrix.xlsx （Excel标准字段格式，sheet: query_cases）

业务说明:
    数据复用conftest内置模拟服务的用户查询接口，
    通过token_required字段区分"携带令牌正向查询"与"无令牌反向校验"两类场景。
"""

from pathlib import Path

import allure
import pytest

from src.common.assertion import (
    assert_equal,
    assert_json_value,
    assert_status_code,
)
from src.core.data_driver import DataDriver

# 项目根目录（本文件位于 tests/api_demo/ 下，向上两级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 演示数据文件路径
YAML_DATA_FILE = PROJECT_ROOT / "testdata" / "yaml" / "api_user_query_matrix.yaml"
EXCEL_DATA_FILE = PROJECT_ROOT / "testdata" / "excel" / "api_user_query_matrix.xlsx"

# 模块级加载数据（参数化在用例收集阶段生效，无法走fixture机制）
YAML_CASES = DataDriver.load_cases(YAML_DATA_FILE)
EXCEL_CASES = DataDriver.load_cases(EXCEL_DATA_FILE, sheet_name="query_cases")


def _execute_query_case(case: dict, http_client) -> None:
    """
    按数据用例执行用户查询并断言（YAML/Excel数据驱动的公共执行逻辑）

    根据token_required字段决定请求方式:
        - true:  先登录获取有效Token，携带Authorization头发起查询
        - false: 不携带任何令牌直接查询（预期被401拒绝）

    参数:
        case (dict): 单条数据驱动用例（含查询参数与全部断言预期值）
        http_client (HttpClient): 会话级HTTP统一客户端fixture

    返回:
        无（断言失败时抛出AssertionError，由失败钩子自动记录日志）

    异常:
        HttpClientError: 模拟服务连接失败时由客户端抛出
    """
    # 布尔字段容错: 兼容YAML的true/false与Excel的TRUE/FALSE/1/0
    token_required = str(case["token_required"]).strip().upper() in ("TRUE", "1", "YES")

    headers = {}
    if token_required:
        with allure.step("前置: 登录获取有效令牌"):
            login_resp = http_client.post(
                "/api/login", json={"username": "admin", "password": "123456"}
            )
            token = login_resp.json()["data"]["token"]
            headers["Authorization"] = f"Bearer {token}"

    with allure.step(f'发起用户查询请求 | 目标用户ID: {case["query_user_id"]}'):
        response = http_client.get(f'/api/users/{case["query_user_id"]}', headers=headers)

    with allure.step("校验HTTP状态码与业务码"):
        assert_status_code(response, case["expected_status"])
        assert_json_value(response, "code", case["expected_code"])

    # expected_username字段存在时校验用户信息（不存在用户的场景无该字段）
    if case.get("expected_username"):
        with allure.step("校验返回用户信息"):
            assert_json_value(response, "data.username", case["expected_username"])


@allure.feature("数据驱动引擎")
@allure.story("YAML数据驱动")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestYamlDataDriven:
    """YAML数据驱动演示: load_cases统一入口加载YAML数据并参数化执行"""

    @pytest.mark.parametrize(
        "case",
        YAML_CASES,
        ids=[f'{case["case_id"]}-{case["name"]}' for case in YAML_CASES],
    )
    def test_yaml_driven_query(self, case, http_client):
        """
        YAML数据驱动用户查询参数化测试

        参数:
            case (dict): YAML中单条标准字段用例数据
            http_client (HttpClient): 会话级HTTP统一客户端fixture

        返回:
            无
        """
        allure.dynamic.title(f'[YAML驱动] {case["case_id"]} {case["name"]}')
        _execute_query_case(case, http_client)


@allure.feature("数据驱动引擎")
@allure.story("Excel数据驱动")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestExcelDataDriven:
    """Excel数据驱动演示: load_cases加载Excel数据（指定sheet）并参数化执行"""

    @pytest.mark.parametrize(
        "case",
        EXCEL_CASES,
        ids=[f'{case["case_id"]}-{case["name"]}' for case in EXCEL_CASES],
    )
    def test_excel_driven_query(self, case, http_client):
        """
        Excel数据驱动用户查询参数化测试

        参数:
            case (dict): Excel中单行标准字段用例数据（tags逗号分隔字符串已自动转列表）
            http_client (HttpClient): 会话级HTTP统一客户端fixture

        返回:
            无
        """
        allure.dynamic.title(f'[Excel驱动] {case["case_id"]} {case["name"]}')
        _execute_query_case(case, http_client)


@allure.feature("数据驱动引擎")
@allure.story("用例筛选")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestDataDriverFilter:
    """三维筛选演示: 验证filter_cases按module/priority/tags过滤及组合过滤的准确性"""

    def test_filter_by_priority(self):
        """
        按优先级筛选: YAML数据中P0用例1条、P1用例2条、P2用例1条

        参数:
            无

        返回:
            无
        """
        p0_cases = DataDriver.filter_cases(YAML_CASES, priority="P0")
        assert_equal(len(p0_cases), 1, "P0用例应筛选出1条")
        assert_equal(p0_cases[0]["case_id"], "TM-API-0201")

        high_cases = DataDriver.filter_cases(YAML_CASES, priority=["P0", "P1"])
        assert_equal(len(high_cases), 3, "P0+P1用例应筛选出3条")

    def test_filter_by_tags(self):
        """
        按标签筛选: tags任一命中即保留（smoke 1条、regression 3条）

        参数:
            无

        返回:
            无
        """
        smoke_cases = DataDriver.filter_cases(YAML_CASES, tags=["smoke"])
        assert_equal(len(smoke_cases), 1, "smoke标签用例应筛选出1条")
        assert_equal(smoke_cases[0]["case_id"], "TM-API-0201")

        regression_cases = DataDriver.filter_cases(YAML_CASES, tags="regression")
        assert_equal(len(regression_cases), 3, "regression标签用例应筛选出3条")

    def test_filter_combined_and_cross_source(self):
        """
        组合筛选与跨源筛选: module+priority组合为AND关系；
        Excel源数据同样可筛选（验证引擎格式无关性）

        参数:
            无

        返回:
            无
        """
        combined = DataDriver.filter_cases(
            YAML_CASES, module="用户管理", priority="P0", tags=["smoke"]
        )
        assert_equal(len(combined), 1, "三维组合条件应筛选出1条")
        assert_equal(combined[0]["case_id"], "TM-API-0201")

        excel_smoke = DataDriver.filter_cases(EXCEL_CASES, tags=["smoke"])
        assert_equal(len(excel_smoke), 1, "Excel源smoke标签用例应筛选出1条")
        assert_equal(excel_smoke[0]["case_id"], "TM-API-0301")

        no_match = DataDriver.filter_cases(YAML_CASES, module="不存在的模块")
        assert_equal(len(no_match), 0, "无匹配条件应返回空列表")
