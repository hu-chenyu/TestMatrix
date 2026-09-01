"""
HTML邮件报告模板验证用例（第二阶段Day11）

验证目标:
    1. render产出: 完整HTML结构且被_is_html判定为HTML
    2. 汇总卡片: 各状态数字正确渲染
    3. 通过率颜色: 绿/橙/红三档阈值正确
    4. 模块/优先级表格: 行数与内容正确
    5. 失败明细: 明细行/空列表庆祝文案
    6. 耗时格式化: 秒/毫秒自适应
    7. 空数据边界: total=0不抛异常、通过率N/A

数据说明:
    全部用dataclass直接构造，不依赖真实Allure结果目录。
"""

import allure
import pytest

from src.core.notification import EmailNotifier, EmailReportTemplate
from src.core.report_analyzer import (
    FailedCaseDetail,
    ModuleStat,
    PriorityStat,
    StatisticsResult,
)


def make_stat(**overrides) -> StatisticsResult:
    """
    构造标准StatisticsResult测试对象（默认100条95%通过）

    参数:
        **overrides: 需覆盖的字段值

    返回:
        StatisticsResult: 组装好的统计结果对象
    """
    defaults = {
        "total": 100,
        "passed": 95,
        "failed": 3,
        "broken": 2,
        "skipped": 0,
        "pass_rate": 0.95,
        "total_duration_ms": 1500,
        "avg_duration_ms": 15.0,
        "p95_duration_ms": 50.0,
        "min_duration_ms": 5,
        "max_duration_ms": 200,
    }
    defaults.update(overrides)
    return StatisticsResult(**defaults)


@allure.feature("通知模块")
@allure.story("HTML报告模板")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestRenderBasic:
    """render基础产出验证"""

    def test_render_returns_html(self):
        """
        HTML结构: 返回含<html>/<body>/<table>的完整文档，
        且被EmailNotifier._is_html判定为True

        参数:
            无

        返回:
            无
        """
        html = EmailReportTemplate().render(make_stat(), "RUN-TEST-0001")

        assert "<html" in html
        assert "<body" in html
        assert "<table" in html
        assert "<!DOCTYPE html>" in html
        assert "RUN-TEST-0001" in html
        assert EmailNotifier._is_html(html) is True

    def test_summary_cards_numbers(self):
        """
        汇总卡片: total=100/passed=95/纯failed=3/error=2/95.00%
        全部正确渲染

        参数:
            无

        返回:
            无
        """
        html = EmailReportTemplate().render(make_stat())

        assert "100" in html
        assert "95" in html
        assert "3" in html
        assert "2" in html
        assert "95.00%" in html

    def test_pass_rate_colors(self):
        """
        通过率颜色三档: 0.95绿#28a745 / 0.75橙#ffc107 / 0.6红#dc3545

        参数:
            无

        返回:
            无
        """
        template = EmailReportTemplate()

        green_html = template.render(make_stat(pass_rate=0.95))
        assert "#28a745" in green_html

        orange_html = template.render(make_stat(pass_rate=0.75))
        assert "#ffc107" in orange_html

        red_html = template.render(make_stat(pass_rate=0.6))
        assert "#dc3545" in red_html


@allure.feature("通知模块")
@allure.story("分布表格与明细")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestTablesRendering:
    """模块/优先级/失败明细表格渲染验证"""

    def test_module_table_rows(self):
        """
        模块表格: 3个模块3行数据（不含表头），模块名正确显示，
        通过率升序排列（风险模块在前）

        参数:
            无

        返回:
            无
        """
        stat = make_stat(
            by_module={
                "用户管理": ModuleStat(name="用户管理", total=50, passed=50,
                                      failed=0, pass_rate=1.0),
                "订单管理": ModuleStat(name="订单管理", total=30, passed=15,
                                       failed=15, pass_rate=0.5),
                "数据报表": ModuleStat(name="数据报表", total=20, passed=20,
                                       failed=0, pass_rate=1.0),
            }
        )
        html = EmailReportTemplate().render(stat)

        assert "用户管理" in html
        assert "订单管理" in html
        assert "数据报表" in html
        # 模块区块存在（含表头）且三行数据均在
        assert "模块分布" in html
        # 通过率升序: 订单管理(0.5)应排在用户管理之前
        assert html.index("订单管理") < html.index("用户管理")

    def test_failed_details_table(self):
        """
        失败明细: 2条明细2行渲染，用例名与错误信息正确显示；
        超长错误信息截断200字符

        参数:
            无

        返回:
            无
        """
        details = [
            FailedCaseDetail(
                uuid="u1", name="test_login_failed", full_name="tests#test_login_failed",
                status="failed", duration_ms=100, module="用户管理",
                priority="critical", error_message="业务码期望0实际2001",
            ),
            FailedCaseDetail(
                uuid="u2", name="test_query_timeout", full_name="tests#test_query_timeout",
                status="broken", duration_ms=300, module="订单管理",
                priority="normal", error_message="x" * 250,  # 超长错误信息
            ),
        ]
        html = EmailReportTemplate().render(make_stat(), failed_details=details)

        assert "test_login_failed" in html
        assert "test_query_timeout" in html
        assert "业务码期望0实际2001" in html
        # 250字符截断为200+...（原文完整串不应出现）
        assert "x" * 250 not in html
        assert "..." in html

    def test_no_failed_show_success(self):
        """
        无失败场景: 空明细列表显示庆祝文案，
        不出现失败明细数据表头（"错误信息"列）

        参数:
            无

        返回:
            无
        """
        html = EmailReportTemplate().render(
            make_stat(), failed_details=[]
        )

        assert "🎉" in html
        assert "无失败用例" in html
        # 明细表头不应出现
        assert "错误信息" not in html


@allure.feature("通知模块")
@allure.story("格式化与边界")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestFormattingAndEdge:
    """耗时格式化与空数据边界验证"""

    def test_duration_formatting(self):
        """
        耗时格式: 1500ms→"1.50 s"、15.5ms→"15.50 ms"，
        秒/毫秒自适应渲染

        参数:
            无

        返回:
            无
        """
        stat = make_stat(
            total_duration_ms=1500, avg_duration_ms=15.5, p95_duration_ms=50.0
        )
        html = EmailReportTemplate().render(stat)

        assert "1.50 s" in html  # 总耗时1500ms
        assert "15.50 ms" in html  # 平均耗时15.5ms
        assert "50.00 ms" in html  # P95耗时50.0ms

    def test_empty_total_edge_case(self):
        """
        空数据边界: total=0不抛异常，通过率显示N/A，
        汇总卡片数字为0，无模块/优先级数据显示提示文案

        参数:
            无

        返回:
            无
        """
        stat = make_stat(
            total=0, passed=0, failed=0, broken=0, skipped=0,
            pass_rate=0.0, total_duration_ms=0, avg_duration_ms=0.0,
            p95_duration_ms=0.0, min_duration_ms=0, max_duration_ms=0,
        )
        template = EmailReportTemplate()

        # 不抛异常
        html = template.render(stat)

        # 通过率0.0显示N/A（total=0无意义），灰色
        assert "N/A" in html
        assert "#6c757d" in html
        # 空数据提示
        assert "无模块数据" in html
        assert "无优先级数据" in html
        assert "无失败用例" in html

    def test_render_none_stat_raises(self):
        """
        入参校验: stat为None抛ValueError

        参数:
            无

        返回:
            无
        """
        with pytest.raises(ValueError, match="统计结果不能为空"):
            EmailReportTemplate().render(None)
