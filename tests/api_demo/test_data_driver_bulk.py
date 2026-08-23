"""
数据驱动引擎大数据量验证用例（50条混合数据集）

验证目标:
    1. 数据加载性能: 25条YAML（request/expected嵌套结构）+ 25条Excel（含3个空行）
       批量加载耗时统计
    2. 数据完整性: 50条用例编号唯一、双源数量准确、空行正确跳过
    3. 三维筛选性能与准确性: 50条用例组合筛选耗时及结果核对（与朴素实现交叉验证）
    4. 参数化执行: 50条混合数据全量驱动真实业务断言
    5. 性能基线断言: 加载/筛选耗时低于宽松阈值（防性能退化，非精确压测）

性能指标采集方式:
    模块级（用例收集阶段）对load_cases/filter_cases包裹perf_counter计时，
    指标汇总后通过日志输出并附加Allure附件，供效能分析留档。
"""

import time
from pathlib import Path

import allure
import pytest

from src.common.assertion import (
    assert_equal,
    assert_json_value,
    assert_less,
    assert_status_code,
)
from src.common.logger import LogManager
from src.core.data_driver import DataDriver

logger = LogManager.get_logger()

# 项目根目录（本文件位于 tests/api_demo/ 下，向上两级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 批量数据文件路径
YAML_BULK_FILE = PROJECT_ROOT / "testdata" / "yaml" / "api_bulk_query_data.yaml"
EXCEL_BULK_FILE = PROJECT_ROOT / "testdata" / "excel" / "api_bulk_query_data.xlsx"

# 性能基线阈值（毫秒）: 宽松上限防退化，实际值远低于阈值
YAML_LOAD_THRESHOLD_MS = 2000
EXCEL_LOAD_THRESHOLD_MS = 3000
FILTER_THRESHOLD_MS = 500


# ===========================================================================
# 模块级数据加载与性能计时（用例收集阶段执行一次）
# ===========================================================================
_start = time.perf_counter()
YAML_BULK_CASES = DataDriver.load_cases(YAML_BULK_FILE)
YAML_LOAD_MS = (time.perf_counter() - _start) * 1000

_start = time.perf_counter()
EXCEL_BULK_CASES = DataDriver.load_cases(EXCEL_BULK_FILE, sheet_name="bulk_query_cases")
EXCEL_LOAD_MS = (time.perf_counter() - _start) * 1000

# 合并50条混合数据集
COMBINED_CASES = YAML_BULK_CASES + EXCEL_BULK_CASES

# 三维筛选性能计时（连续两次筛选取总耗时）
_start = time.perf_counter()
SMOKE_FILTERED = DataDriver.filter_cases(COMBINED_CASES, tags=["smoke"])
P0_FILTERED = DataDriver.filter_cases(COMBINED_CASES, priority="P0")
FILTER_TOTAL_MS = (time.perf_counter() - _start) * 1000

TOTAL_PREP_MS = YAML_LOAD_MS + EXCEL_LOAD_MS + FILTER_TOTAL_MS


def _get_request_param(case: dict, key: str):
    """
    兼容嵌套与扁平两种数据组织格式，提取请求参数（模块内部工具）

    参数:
        case (dict): 单条用例数据（YAML嵌套格式或Excel扁平格式）
        key (str): 请求参数名（token_required / query_user_id）

    返回:
        Any: 请求参数值

    异常:
        KeyError: 参数不存在时抛出
    """
    if "request" in case:
        return case["request"][key]
    return case[key]


def _get_expected_param(case: dict, key: str):
    """
    兼容嵌套与扁平两种数据组织格式，提取断言预期值（模块内部工具）

    参数:
        case (dict): 单条用例数据
        key (str): 预期值名称（status / code / username）

    返回:
        Any: 预期值；不存在时返回None

    异常:
        无
    """
    if "expected" in case:
        return case["expected"].get(key)
    return case.get(f"expected_{key}")


def _execute_bulk_query_case(case: dict, http_client) -> None:
    """
    按数据用例执行用户查询并断言（YAML嵌套/Excel扁平通用执行逻辑）

    参数:
        case (dict): 单条批量数据用例（含请求参数与全部断言预期值）
        http_client (HttpClient): 会话级HTTP统一客户端fixture

    返回:
        无（断言失败时抛出AssertionError，由失败钩子自动记录日志）

    异常:
        HttpClientError: 模拟服务连接失败时由客户端抛出
    """
    token_required = str(_get_request_param(case, "token_required")).strip().upper() in (
        "TRUE", "1", "YES",
    )

    headers = {}
    if token_required:
        with allure.step("前置: 登录获取有效令牌"):
            login_resp = http_client.post(
                "/api/login", json={"username": "admin", "password": "123456"}
            )
            headers["Authorization"] = f"Bearer {login_resp.json()['data']['token']}"

    with allure.step(f'发起用户查询请求 | 目标用户ID: {_get_request_param(case, "query_user_id")}'):
        response = http_client.get(
            f'/api/users/{_get_request_param(case, "query_user_id")}', headers=headers
        )

    with allure.step("校验HTTP状态码与业务码"):
        assert_status_code(response, _get_expected_param(case, "status"))
        assert_json_value(response, "code", _get_expected_param(case, "code"))

    expected_username = _get_expected_param(case, "username")
    if expected_username:
        with allure.step("校验返回用户信息"):
            assert_json_value(response, "data.username", expected_username)


@allure.feature("数据驱动引擎")
@allure.story("大数据量性能验证")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestBulkYamlDriven:
    """YAML批量数据驱动: 25条嵌套结构用例参数化执行"""

    @pytest.mark.parametrize(
        "case",
        YAML_BULK_CASES,
        ids=[f'{case["case_id"]}-{case["name"]}' for case in YAML_BULK_CASES],
    )
    def test_bulk_yaml_query(self, case, http_client):
        """
        YAML嵌套结构数据驱动用户查询参数化测试

        参数:
            case (dict): YAML中单条嵌套结构用例（request/expected分离）
            http_client (HttpClient): 会话级HTTP统一客户端fixture

        返回:
            无
        """
        allure.dynamic.title(f'[批量YAML] {case["case_id"]} {case["name"]}')
        _execute_bulk_query_case(case, http_client)


@allure.feature("数据驱动引擎")
@allure.story("大数据量性能验证")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestBulkExcelDriven:
    """Excel批量数据驱动: 25条扁平结构用例参数化执行（数据源含3个空行已自动跳过）"""

    @pytest.mark.parametrize(
        "case",
        EXCEL_BULK_CASES,
        ids=[f'{case["case_id"]}-{case["name"]}' for case in EXCEL_BULK_CASES],
    )
    def test_bulk_excel_query(self, case, http_client):
        """
        Excel扁平结构数据驱动用户查询参数化测试

        参数:
            case (dict): Excel中单行扁平结构用例（tags逗号分隔字符串已归一化为列表）
            http_client (HttpClient): 会话级HTTP统一客户端fixture

        返回:
            无
        """
        allure.dynamic.title(f'[批量Excel] {case["case_id"]} {case["name"]}')
        _execute_bulk_query_case(case, http_client)


@allure.feature("数据驱动引擎")
@allure.story("大数据量性能验证")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestDataDriverBulkPerformance:
    """大数据量下数据完整性与引擎性能基线验证"""

    def test_data_integrity_at_volume(self):
        """
        数据完整性校验: 双源数量准确、50条编号唯一、Excel空行正确跳过

        参数:
            无

        返回:
            无
        """
        assert_equal(len(YAML_BULK_CASES), 25, "YAML批量数据应为25条")
        assert_equal(len(EXCEL_BULK_CASES), 25, "Excel批量数据应为25条（空行不计入）")
        assert_equal(len(COMBINED_CASES), 50, "混合数据集总量应为50条")

        all_ids = [case["case_id"] for case in COMBINED_CASES]
        assert_equal(len(set(all_ids)), 50, "50条用例编号必须全部唯一")

        # 嵌套与扁平双格式的tags均已归一化为列表
        assert all(isinstance(case["tags"], list) for case in COMBINED_CASES), \
            "全部用例tags应已归一化为列表"

    def test_filter_accuracy_at_volume(self):
        """
        筛选准确性交叉验证: filter_cases结果与朴素列表推导实现逐条核对

        参数:
            无

        返回:
            无
        """
        # 朴素实现独立计算期望结果（避免用被测逻辑验证自身）
        expected_smoke_ids = {
            case["case_id"] for case in COMBINED_CASES if "smoke" in case["tags"]
        }
        assert_equal(
            {case["case_id"] for case in SMOKE_FILTERED},
            expected_smoke_ids,
            "smoke标签筛选结果应与朴素实现一致",
        )

        expected_p0_ids = {
            case["case_id"] for case in COMBINED_CASES if case["priority"] == "P0"
        }
        assert_equal(
            {case["case_id"] for case in P0_FILTERED},
            expected_p0_ids,
            "P0优先级筛选结果应与朴素实现一致",
        )

        # 组合筛选维度AND关系验证
        combined = DataDriver.filter_cases(
            COMBINED_CASES, module="用户管理", priority="P0", tags=["smoke"]
        )
        expected_combined_ids = {
            case["case_id"]
            for case in COMBINED_CASES
            if case["module"] == "用户管理"
            and case["priority"] == "P0"
            and "smoke" in case["tags"]
        }
        assert_equal(
            {case["case_id"] for case in combined},
            expected_combined_ids,
            "三维组合筛选结果应与朴素实现一致",
        )

    def test_load_and_filter_performance(self):
        """
        性能基线校验: 加载与筛选耗时低于阈值，指标汇总输出并附加Allure留档

        参数:
            无

        返回:
            无
        """
        avg_load_ms = (YAML_LOAD_MS + EXCEL_LOAD_MS) / len(COMBINED_CASES)
        metrics_text = (
            "===== 数据驱动引擎大数据量性能指标 =====\n"
            f"YAML加载（25条嵌套）  : {YAML_LOAD_MS:.2f} ms\n"
            f"Excel加载（25条含空行）: {EXCEL_LOAD_MS:.2f} ms\n"
            f"单条平均加载耗时      : {avg_load_ms:.3f} ms/条\n"
            f"筛选耗时（2次/50条）  : {FILTER_TOTAL_MS:.2f} ms\n"
            f"数据准备总耗时        : {TOTAL_PREP_MS:.2f} ms\n"
            "========================================"
        )
        logger.info(f"\n{metrics_text}")
        allure.attach(
            body=metrics_text,
            name="数据驱动引擎性能指标",
            attachment_type=allure.attachment_type.TEXT,
        )

        assert_less(YAML_LOAD_MS, YAML_LOAD_THRESHOLD_MS, "YAML批量加载耗时应低于基线阈值")
        assert_less(EXCEL_LOAD_MS, EXCEL_LOAD_THRESHOLD_MS, "Excel批量加载耗时应低于基线阈值")
        assert_less(FILTER_TOTAL_MS, FILTER_THRESHOLD_MS, "批量筛选耗时应低于基线阈值")
