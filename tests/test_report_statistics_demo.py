"""
report_analyzer统计聚合引擎验证用例（第二阶段Day7）

验证目标:
    1. aggregate状态计数: 空列表全零/全通过/混合状态/通过率舍入
    2. 耗时分布: 总/平均/最大/最小/P95（大样本百分位/小样本最大值近似）
    3. 双维度分组: by_module/by_priority分组统计准确性
    4. 标签提取: 模块名四级fallback链/优先级降级unknown
    5. 失败明细: failed+broken明细字段（error_message/trace提取）
    6. to_dict序列化: 转字典后可json.dumps
    7. 端到端: 真实output/allure_results/数据 parse→aggregate全链路

数据说明:
    单元验证直接构造AllureResult对象（不落盘）；
    端到端验证使用真实Allure结果目录。
"""

import json
from pathlib import Path

import allure
import pytest

from src.core.report_analyzer import (
    AllureResult,
    FailedCaseDetail,
    ModuleStat,
    PriorityStat,
    ReportAnalyzer,
    ReportStatistics,
    StatisticsResult,
)

# 项目根目录（本文件位于 tests/ 下，向上一级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 真实Allure结果目录
REAL_RESULTS_DIR = PROJECT_ROOT / "output" / "allure_results"


def make_result(
    name: str,
    status: str = "passed",
    start: int = 1000,
    stop: int = 2000,
    feature: str = None,
    severity: str = None,
    suite: str = None,
    parent_suite: str = None,
    full_name: str = "",
    status_details: dict = None,
) -> AllureResult:
    """
    构造AllureResult测试对象的工厂函数

    参数:
        name (str): 用例名
        status (str): 执行状态，默认"passed"
        start/stop (int): 起止毫秒时间戳（duration_ms=stop-start），默认耗时1000ms
        feature (str | None): feature标签值（模块名），默认不设
        severity (str | None): severity标签值（优先级），默认不设
        suite/parent_suite (str | None): suite/parentSuite标签值，默认不设
        full_name (str): 全限定名（fallback提取用），默认空串
        status_details (dict | None): 失败详情，默认None

    返回:
        AllureResult: 组装好的用例级结果对象
    """
    labels = {}
    if feature:
        labels["feature"] = [feature]
    if severity:
        labels["severity"] = [severity]
    if suite:
        labels["suite"] = [suite]
    if parent_suite:
        labels["parentSuite"] = [parent_suite]
    return AllureResult(
        uuid=f"uuid-{name}",
        name=name,
        full_name=full_name,
        status=status,
        start=start,
        stop=stop,
        labels=labels,
        status_details=status_details,
    )


@allure.feature("报告统计聚合")
@allure.story("状态计数")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestAggregateStatus:
    """aggregate状态计数与通过率验证"""

    def test_aggregate_empty_list_returns_zeros(self):
        """
        空列表容错: 返回全零StatisticsResult，pass_rate=0.0，不抛异常

        参数:
            无

        返回:
            无
        """
        stat = ReportStatistics.aggregate([])

        assert isinstance(stat, StatisticsResult)
        assert stat.total == 0
        assert stat.passed == 0
        assert stat.failed == 0
        assert stat.broken == 0
        assert stat.skipped == 0
        assert stat.pass_rate == 0.0
        assert stat.total_duration_ms == 0
        assert stat.p95_duration_ms == 0.0
        assert stat.by_module == {}
        assert stat.by_priority == {}
        assert stat.failed_details == []

    def test_aggregate_all_passed(self):
        """
        全通过场景: 10条passed→total=10, passed=10, failed=0, pass_rate=1.0

        参数:
            无

        返回:
            无
        """
        results = [make_result(f"case_{i}") for i in range(10)]
        stat = ReportStatistics.aggregate(results)

        assert stat.total == 10
        assert stat.passed == 10
        assert stat.failed == 0
        assert stat.skipped == 0
        assert stat.pass_rate == 1.0

    def test_aggregate_mixed_status(self):
        """
        混合状态: 5passed+2failed+1broken+2skipped→total=10,
        passed=5, failed=3(failed+broken), broken=1, skipped=2, pass_rate=0.5

        参数:
            无

        返回:
            无
        """
        results = (
            [make_result(f"pass_{i}") for i in range(5)]
            + [make_result(f"fail_{i}", status="failed") for i in range(2)]
            + [make_result("broken_0", status="broken")]
            + [make_result(f"skip_{i}", status="skipped") for i in range(2)]
        )
        stat = ReportStatistics.aggregate(results)

        assert stat.total == 10
        assert stat.passed == 5
        assert stat.failed == 3  # 2 failed + 1 broken
        assert stat.broken == 1
        assert stat.skipped == 2
        assert stat.pass_rate == 0.5

    def test_pass_rate_rounding(self):
        """
        通过率舍入: 3passed+1failed→pass_rate=0.75（4位小数精度）

        参数:
            无

        返回:
            无
        """
        results = (
            [make_result(f"pass_{i}") for i in range(3)]
            + [make_result("fail_0", status="failed")]
        )
        stat = ReportStatistics.aggregate(results)

        assert stat.pass_rate == 0.75


@allure.feature("报告统计聚合")
@allure.story("耗时分布")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestDurationStats:
    """耗时分布统计验证（总/均/极值/P95）"""

    def test_duration_stats_basic(self):
        """
        基础耗时: 构造已知耗时[100,200,300,400]→
        total=1000, avg=250.0, max=400, min=100

        参数:
            无

        返回:
            无
        """
        durations = [100, 200, 300, 400]
        results = [
            make_result(f"case_{i}", start=0, stop=duration)
            for i, duration in enumerate(durations)
        ]
        stat = ReportStatistics.aggregate(results)

        assert stat.total_duration_ms == 1000
        assert stat.avg_duration_ms == 250.0
        assert stat.max_duration_ms == 400
        assert stat.min_duration_ms == 100
        # 4条<20条，P95取最大值近似
        assert stat.p95_duration_ms == 400.0

    def test_p95_duration_large_sample(self):
        """
        P95大样本: 100条耗时1-100ms→P95=第95条值=95ms
        （索引=ceil(0.95*100)-1=94，0-based对应第95个）

        参数:
            无

        返回:
            无
        """
        results = [
            make_result(f"case_{i}", start=0, stop=i + 1) for i in range(100)
        ]
        stat = ReportStatistics.aggregate(results)

        assert stat.p95_duration_ms == 95.0

    def test_p95_small_sample_uses_max(self):
        """
        P95小样本: 5条（<20条阈值）→P95直接取最大值近似

        参数:
            无

        返回:
            无
        """
        durations = [10, 20, 30, 40, 50]
        results = [
            make_result(f"case_{i}", start=0, stop=duration)
            for i, duration in enumerate(durations)
        ]
        stat = ReportStatistics.aggregate(results)

        assert stat.p95_duration_ms == 50.0


@allure.feature("报告统计聚合")
@allure.story("分组统计")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestGroupStatistics:
    """模块/优先级双维度分组统计验证"""

    def test_group_by_module(self):
        """
        模块分组: 3个模块（用户管理3条2通过/订单管理2条1通过/
        数据报表1条1失败）→分组key与各项统计核对

        参数:
            无

        返回:
            无
        """
        results = (
            [
                make_result("um_pass_1", feature="用户管理"),
                make_result("um_pass_2", feature="用户管理"),
                make_result("um_fail_1", feature="用户管理", status="failed"),
            ]
            + [
                make_result("om_pass_1", feature="订单管理"),
                make_result("om_fail_1", feature="订单管理", status="broken"),
            ]
            + [make_result("dr_pass_1", feature="数据报表")]
        )
        stat = ReportStatistics.aggregate(results)

        assert set(stat.by_module.keys()) == {"用户管理", "订单管理", "数据报表"}

        user_module = stat.by_module["用户管理"]
        assert isinstance(user_module, ModuleStat)
        assert user_module.total == 3
        assert user_module.passed == 2
        assert user_module.failed == 1
        assert user_module.pass_rate == round(2 / 3, 4)

        order_module = stat.by_module["订单管理"]
        assert order_module.total == 2
        assert order_module.failed == 1  # broken计入failed
        assert order_module.pass_rate == 0.5

        report_module = stat.by_module["数据报表"]
        assert report_module.total == 1
        assert report_module.pass_rate == 1.0

    def test_group_by_priority(self):
        """
        优先级分组: critical(2条1通过)/normal(3条2通过1失败)/
        minor(1条1通过)→分组统计核对

        参数:
            无

        返回:
            无
        """
        results = (
            [
                make_result("crit_pass", severity="critical"),
                make_result("crit_fail", severity="critical", status="failed"),
            ]
            + [
                make_result("norm_pass_1", severity="normal"),
                make_result("norm_pass_2", severity="normal"),
                make_result("norm_fail", severity="normal", status="broken"),
            ]
            + [make_result("minor_pass", severity="minor")]
        )
        stat = ReportStatistics.aggregate(results)

        assert set(stat.by_priority.keys()) == {"critical", "normal", "minor"}

        critical_stat = stat.by_priority["critical"]
        assert isinstance(critical_stat, PriorityStat)
        assert critical_stat.total == 2
        assert critical_stat.passed == 1
        assert critical_stat.failed == 1
        assert critical_stat.pass_rate == 0.5

        normal_stat = stat.by_priority["normal"]
        assert normal_stat.total == 3
        assert normal_stat.passed == 2
        assert normal_stat.failed == 1

        minor_stat = stat.by_priority["minor"]
        assert minor_stat.total == 1
        assert minor_stat.pass_rate == 1.0


@allure.feature("报告统计聚合")
@allure.story("标签提取")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestLabelExtraction:
    """模块名/优先级提取fallback链验证"""

    def test_extract_module_fallback_chain(self):
        """
        模块名提取fallback链: feature最优先→suite→parentSuite→
        full_name倒数第二段→unknown，逐级验证

        参数:
            无

        返回:
            无
        """
        # 1. feature最优先（同时存在suite时取feature）
        result = make_result(
            "case_feature", feature="功能模块", suite="套件模块"
        )
        assert ReportStatistics._extract_module(result) == "功能模块"

        # 2. 无feature时取suite
        result = make_result("case_suite", suite="套件模块")
        assert ReportStatistics._extract_module(result) == "套件模块"

        # 3. 无feature/suite时取parentSuite
        result = make_result("case_parent", parent_suite="父套件模块")
        assert ReportStatistics._extract_module(result) == "父套件模块"

        # 4. 标签全无时从full_name提取倒数第二段
        result = make_result(
            "case_fullname",
            full_name="tests.api_demo.test_login.TestLogin#test_success",
        )
        assert ReportStatistics._extract_module(result) == "test_login"

        # 5. 全都没有→unknown
        result = make_result("case_unknown")
        assert ReportStatistics._extract_module(result) == "unknown"

        # 优先级: severity缺失→unknown
        assert ReportStatistics._extract_priority(make_result("no_sev")) == "unknown"
        sev_result = make_result("has_sev", severity="blocker")
        assert ReportStatistics._extract_priority(sev_result) == "blocker"


@allure.feature("报告统计聚合")
@allure.story("失败明细")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestFailedDetails:
    """失败用例明细提取验证"""

    def test_failed_details_extraction(self):
        """
        失败明细: 2failed+1broken（含statusDetails）→明细3条，
        error_message/error_trace正确提取，module/priority正确标注

        参数:
            无

        返回:
            无
        """
        results = [
            make_result("pass_1", feature="用户管理"),
            make_result(
                "fail_1", status="failed", feature="用户管理", severity="critical",
                start=100, stop=350,
                status_details={
                    "message": "业务码期望0实际2001",
                    "trace": "AssertionError: ...",
                },
            ),
            make_result(
                "fail_2", status="failed", feature="订单管理",
                status_details={"message": "响应超时"},
            ),
            make_result(
                "broken_1", status="broken", feature="订单管理",
                status_details={"trace": "ConnectionError: ..."},
            ),
        ]
        stat = ReportStatistics.aggregate(results)

        assert len(stat.failed_details) == 3
        assert all(isinstance(d, FailedCaseDetail) for d in stat.failed_details)

        first = stat.failed_details[0]
        assert first.name == "fail_1"
        assert first.status == "failed"
        assert first.duration_ms == 250
        assert first.module == "用户管理"
        assert first.priority == "critical"
        assert first.error_message == "业务码期望0实际2001"
        assert first.error_trace == "AssertionError: ..."

        # message缺失时空串兜底
        second = stat.failed_details[1]
        assert second.error_message == "响应超时"
        assert second.error_trace == ""

        # broken明细同样提取
        third = stat.failed_details[2]
        assert third.status == "broken"
        assert third.error_trace == "ConnectionError: ..."


@allure.feature("报告统计聚合")
@allure.story("序列化与端到端")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestSerializationAndE2E:
    """to_dict序列化与真实数据端到端验证"""

    def test_to_dict_serialization(self):
        """
        序列化: aggregate后to_dict→字典可json.dumps，
        关键字段值与原对象一致

        参数:
            无

        返回:
            无
        """
        results = [
            make_result("pass_1", feature="用户管理", severity="critical"),
            make_result(
                "fail_1", status="failed", feature="用户管理",
                status_details={"message": "断言失败"},
            ),
        ]
        stat = ReportStatistics.aggregate(results)
        stat_dict = ReportStatistics.to_dict(stat)

        # 可JSON序列化（dataclass已全转基本类型）
        serialized = json.dumps(stat_dict, ensure_ascii=False)
        assert "用户管理" in serialized

        # 关键字段一致
        assert stat_dict["total"] == 2
        assert stat_dict["passed"] == 1
        assert stat_dict["failed"] == 1
        assert stat_dict["pass_rate"] == 0.5
        assert "用户管理" in stat_dict["by_module"]
        assert stat_dict["by_module"]["用户管理"]["total"] == 2
        assert len(stat_dict["failed_details"]) == 1
        assert stat_dict["failed_details"][0]["error_message"] == "断言失败"

    def test_real_data_end_to_end(self):
        """
        端到端: 真实output/allure_results/数据
        parse_results_dir→aggregate→统计与解析结果一致

        参数:
            无

        返回:
            无
        """
        results = ReportAnalyzer.parse_results_dir(REAL_RESULTS_DIR)
        # 真实目录数据不足时降级构造（CI环境无Allure产物兜底）
        if not results:
            results = [make_result(f"case_{i}") for i in range(10)]

        stat = ReportStatistics.aggregate(results)

        assert stat.total == len(results)
        # 通过+失败+跳过+unknown <= total（unknown不计入通过/失败/跳过）
        counted = stat.passed + stat.failed + stat.skipped
        assert counted <= stat.total
        # 分组总量守恒: 各模块用例数之和=总数
        assert sum(m.total for m in stat.by_module.values()) == stat.total
        assert sum(p.total for p in stat.by_priority.values()) == stat.total
        # 失败明细数与failed计数一致
        assert len(stat.failed_details) == stat.failed
        # 真实数据全passed场景验证（当前基线107条全通过）
        if all(r.status == "passed" for r in results):
            assert stat.pass_rate == 1.0
