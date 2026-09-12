"""
TestMatrix Day23: 报告统计与质量度量API测试

测试覆盖:
    汇总:
        1. test_summary_empty               空库全零降级不报错
        2. test_summary_success             双批次累计计数与加权通过率
    趋势:
        3. test_trend_success               时间升序且字段完整
        4. test_trend_limit                 limit截断最近N批 + limit=0返回400
        5. test_trend_empty                 空库返回空列表
    模块分布:
        6. test_module_distribution         分组计数/通过率/total降序排序
        7. test_module_distribution_orphan  悬空明细归unknown不被丢弃
    失败Top:
        8. test_failed_top_success          榜首计数与最近失败堆栈原样透传
        9. test_failed_top_limit            limit只返回Top N条
        10. test_failed_top_empty           无失败返回空列表
    质量度量:
        11. test_quality_metrics_success    三组口径与手算一致
        12. test_quality_metrics_empty      空库全零且子结构齐全

测试基建:
    临时SQLite文件库（tmp_path + 前后DatabaseSession.reset()防
    Windows文件锁）+ Flask test client端到端验证；批次数据全部经
    核心层create_execution/record_execution/finish_execution真实
    写入（零mock零真实外部依赖）；跨批次时间次序断言用
    time.sleep(1.1)跨秒落库（created_at数据库秒级精度）。
"""

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

import allure
import pytest
from flask.testing import FlaskClient

from src.core.case_manager import CaseManager
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

# 模拟失败堆栈文本（多行结构，验证原样透传不截断）
FAILED_STACK = (
    "AssertionError: expected 200 got 500\n"
    '  File "tests/test_login.py", line 42, in test_login\n'
    "    assert response.status_code == 200"
)

# 榜首最近一次失败的特征堆栈（与FAILED_STACK区分，验证取的是最近一条）
FINAL_STACK = (
    "TimeoutError: request timed out after 30s\n"
    '  File "src/common/http_client.py", line 88, in _send\n'
    "    raise TimeoutError(f'request timed out after {self.timeout}s')"
)


# ===========================================================================
# 造数与夹具
# ===========================================================================
def seed_cases() -> None:
    """
    造用例种子数据并入库（4条）

    数据布局:
        - TM-UC-0001/TM-UC-0002 模块"用户中心" active
        - TM-OD-0001 模块"订单中心" active
        - TM-DIS-0001 模块"停用模块" disabled（验证覆盖率分母只算active）

    参数:
        无

    返回:
        无
    """
    with DatabaseSession.session_scope() as session:
        for case_id, name, module, status in [
            ("TM-UC-0001", "用户登录成功校验", "用户中心", "active"),
            ("TM-UC-0002", "用户登录密码错误校验", "用户中心", "active"),
            ("TM-OD-0001", "订单创建校验", "订单中心", "active"),
            ("TM-DIS-0001", "停用用例校验", "停用模块", "disabled"),
        ]:
            session.add(
                models.TestCase(
                    case_id=case_id,
                    name=name,
                    module=module,
                    priority="P1",
                    case_type="api",
                    status=status,
                    description="报告统计API测试种子用例",
                )
            )


def _create_finished_batch(
    records: list[tuple[str, str, str, Optional[str], float]],
) -> str:
    """
    经核心层造一个已完成执行批次（内部方法）

    造数链路: create_execution生成批次号 -> 逐条record_execution写
    明细（duration按入参写入，供耗时均值断言）-> finish_execution
    聚合生成汇总记录，与生产链路完全一致。

    参数:
        records (list[tuple]): (case_id, case_name, result, error_message,
            duration)元组列表；error_message仅failed/error必填；
            case_id不要求存在于用例表（悬空明细场景直接传幽灵编号）

    返回:
        str: 执行批次号
    """
    execution_id = CaseManager.create_execution(
        executor="tester", environment="dev"
    )
    for case_id, case_name, result, error_message, duration in records:
        start_time = datetime.now()
        end_time = start_time + timedelta(seconds=duration)
        CaseManager.record_execution(
            execution_id=execution_id,
            case_id=case_id,
            case_name=case_name,
            result=result,
            start_time=start_time,
            end_time=end_time,
            duration=duration,
            error_message=error_message,
        )
    CaseManager.finish_execution(execution_id)
    return execution_id


@pytest.fixture
def reports_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
    """
    报告统计API测试客户端fixture（临时SQLite文件库）

    将数据库指向pytest临时目录独立库文件并完成建表+用例种子造数
    （执行批次由各测试方法自行经核心层创建），前后重置
    DatabaseSession引擎单例，防止Windows下SQLite文件句柄残留导致
    tmp_path清理失败，也避免测试间引擎状态污染。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[FlaskClient]: yield测试客户端，前后完成引擎重置
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv(
        "TM_DB_SQLITE_PATH", str(tmp_path / "reports_api.db")
    )
    DatabaseSession.reset()
    DatabaseSession.init_db()
    seed_cases()
    yield create_app("test").test_client()
    DatabaseSession.reset()


# ===========================================================================
# 报告统计与质量度量API测试
# ===========================================================================
@allure.feature("报告统计API")
@allure.story("报告统计与质量度量")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestReportsApi:
    """报告统计与质量度量五接口测试"""

    # ------------------------------------------------------------------
    # 全局汇总
    # ------------------------------------------------------------------
    def test_summary_empty(self, reports_client: FlaskClient) -> None:
        """
        空库汇总: 无任何批次，返回200且total_batches=0、
        overall_pass_rate=0.0、latest_batch=None，不报错
        """
        response = reports_client.get("/api/reports/summary")
        data = response.get_json()

        assert response.status_code == 200, "空库汇总应返回200"
        summary = data["data"]
        assert summary["total_batches"] == 0
        assert summary["total_executed"] == 0
        assert summary["passed"] == 0
        assert summary["failed"] == 0
        assert summary["overall_pass_rate"] == 0.0, "零除场景应为0.0"
        assert summary["latest_batch"] is None, "空库无最新批次"

    def test_summary_success(self, reports_client: FlaskClient) -> None:
        """
        汇总成功: 批次1全过3条 + 批次2为4条里3过1失败，
        累计total_batches=2/total_executed=7/passed=6/failed=1，
        加权通过率=round(6/7,4)=0.8571（非批次pass_rate算术平均），
        latest_batch为最后完成的批次
        """
        _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "passed", None, 0.5),
            ("TM-UC-0002", "用户登录密码错误校验", "passed", None, 0.5),
            ("TM-OD-0001", "订单创建校验", "passed", None, 0.5),
        ])
        # created_at数据库秒级精度，跨秒落库保证latest可判定
        time.sleep(1.1)
        second_id = _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "passed", None, 0.5),
            ("TM-UC-0002", "用户登录密码错误校验", "passed", None, 0.5),
            ("TM-OD-0001", "订单创建校验", "passed", None, 0.5),
            ("TM-DIS-0001", "停用用例校验", "failed", FAILED_STACK, 0.5),
        ])

        response = reports_client.get("/api/reports/summary")
        data = response.get_json()

        assert response.status_code == 200
        summary = data["data"]
        assert summary["total_batches"] == 2, "已完成批次应为2"
        assert summary["total_executed"] == 7, "累计执行应为3+4=7次"
        assert summary["passed"] == 6
        assert summary["failed"] == 1
        assert summary["error"] == 0
        assert summary["skipped"] == 0
        # 加权口径: 6/7=0.8571（算术平均(1.0+0.75)/2=0.875，可区分两种口径）
        assert summary["overall_pass_rate"] == 0.8571, (
            "通过率应为总通过数/总执行数的加权口径"
        )

        latest = summary["latest_batch"]
        assert latest["execution_id"] == second_id, "最新批次应为最后完成的"
        assert latest["pass_rate"] == 0.75, "批次2通过率为3/4"
        assert isinstance(latest["created_at"], str), "created_at应为ISO字符串"

    # ------------------------------------------------------------------
    # 通过率趋势
    # ------------------------------------------------------------------
    def test_trend_success(self, reports_client: FlaskClient) -> None:
        """
        趋势成功: 3批次按时间升序返回（首条最早），长度3，
        每条字段完整
        """
        first_id = _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "passed", None, 0.5),
        ])
        time.sleep(1.1)
        second_id = _create_finished_batch([
            ("TM-UC-0002", "用户登录密码错误校验", "passed", None, 0.5),
        ])
        time.sleep(1.1)
        third_id = _create_finished_batch([
            ("TM-OD-0001", "订单创建校验", "passed", None, 0.5),
        ])

        response = reports_client.get("/api/reports/trend")
        data = response.get_json()

        assert response.status_code == 200
        trend = data["data"]
        assert len(trend) == 3, "3个批次应返回3条趋势"
        assert trend[0]["execution_id"] == first_id, "首条应为最早批次"
        assert trend[1]["execution_id"] == second_id
        assert trend[2]["execution_id"] == third_id, "末条应为最新批次"
        # 每条字段完整（get_trend_data契约）
        assert {
            "execution_id", "pass_rate", "total_cases", "passed",
            "failed", "error", "created_at",
        } == set(trend[0].keys()), "趋势条目应含完整字段"
        assert isinstance(trend[0]["created_at"], str)

    def test_trend_limit(self, reports_client: FlaskClient) -> None:
        """
        趋势截断: 3批次limit=2只返回最近2批（升序末条=最新批次）；
        limit=0返回400
        """
        _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "passed", None, 0.5),
        ])
        time.sleep(1.1)
        _create_finished_batch([
            ("TM-UC-0002", "用户登录密码错误校验", "passed", None, 0.5),
        ])
        time.sleep(1.1)
        third_id = _create_finished_batch([
            ("TM-OD-0001", "订单创建校验", "passed", None, 0.5),
        ])

        response = reports_client.get("/api/reports/trend?limit=2")
        data = response.get_json()

        assert response.status_code == 200
        trend = data["data"]
        assert len(trend) == 2, "limit=2应只返回最近2批"
        assert trend[-1]["execution_id"] == third_id, (
            "升序末条应为最新完成的批次"
        )

        invalid_response = reports_client.get("/api/reports/trend?limit=0")
        assert invalid_response.status_code == 400, "limit=0应返回400"
        assert invalid_response.get_json()["code"] == 400

    def test_trend_empty(self, reports_client: FlaskClient) -> None:
        """
        趋势空库: 无批次时返回200且空列表，不报错
        """
        response = reports_client.get("/api/reports/trend")
        data = response.get_json()

        assert response.status_code == 200
        assert data["data"] == [], "空库趋势应为空列表"

    # ------------------------------------------------------------------
    # 模块执行分布
    # ------------------------------------------------------------------
    def test_module_distribution(
        self, reports_client: FlaskClient
    ) -> None:
        """
        模块分布: 用户中心2过1失败、订单中心1过，两模块分组计数与
        pass_rate正确，排序total降序（用户中心在前）
        """
        _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "passed", None, 0.5),
            ("TM-UC-0002", "用户登录密码错误校验", "passed", None, 0.5),
            ("TM-UC-0001", "用户登录成功校验", "failed", FAILED_STACK, 0.5),
            ("TM-OD-0001", "订单创建校验", "passed", None, 0.5),
        ])

        response = reports_client.get("/api/reports/module-distribution")
        data = response.get_json()

        assert response.status_code == 200
        distribution = data["data"]
        assert len(distribution) == 2, "应只有两个模块分组"

        uc = distribution[0]
        assert uc["module"] == "用户中心"
        assert uc["total"] == 3, "用户中心明细应为3条"
        assert uc["passed"] == 2
        assert uc["failed"] == 1
        assert uc["error"] == 0
        assert uc["skipped"] == 0
        assert uc["pass_rate"] == 0.6667, "round(2/3,4)=0.6667"

        od = distribution[1]
        assert od["module"] == "订单中心", "total降序订单中心(1条)在后"
        assert od["total"] == 1
        assert od["passed"] == 1
        assert od["pass_rate"] == 1.0

    def test_module_distribution_orphan(
        self, reports_client: FlaskClient
    ) -> None:
        """
        悬空明细: case_id在用例表不存在（TM-GHOST-9999）的明细
        归入module="unknown"，不因outerjoin无关联被丢弃
        """
        _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "passed", None, 0.5),
            ("TM-GHOST-9999", "已删除的幽灵用例", "passed", None, 0.5),
        ])

        response = reports_client.get("/api/reports/module-distribution")
        data = response.get_json()

        assert response.status_code == 200
        distribution = data["data"]
        modules = {item["module"] for item in distribution}
        assert "unknown" in modules, "悬空明细应归unknown模块不被丢弃"

        unknown = next(
            item for item in distribution if item["module"] == "unknown"
        )
        assert unknown["total"] == 1, "悬空明细应完整计数"
        assert unknown["passed"] == 1

    # ------------------------------------------------------------------
    # 失败用例Top榜
    # ------------------------------------------------------------------
    def test_failed_top_success(self, reports_client: FlaskClient) -> None:
        """
        失败Top: TM-UC-0001跨3批次各失败1次（共3次，最后一次带特征
        堆栈）、TM-UC-0002失败1次；榜首TM-UC-0001、fail_count=3、
        last_error_message原样等于最后一次写入的堆栈
        """
        _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "failed", FAILED_STACK, 0.5),
            ("TM-UC-0002", "用户登录密码错误校验", "failed", FAILED_STACK, 0.5),
        ])
        time.sleep(1.1)
        _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "failed", FAILED_STACK, 0.5),
        ])
        time.sleep(1.1)
        _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "failed", FINAL_STACK, 0.5),
        ])

        response = reports_client.get("/api/reports/failed-top")
        data = response.get_json()

        assert response.status_code == 200
        top = data["data"]
        assert len(top) == 2, "失败用例应为2个"

        champion = top[0]
        assert champion["case_id"] == "TM-UC-0001", "榜首应为3次失败的用例"
        assert champion["case_name"] == "用户登录成功校验"
        assert champion["fail_count"] == 3, "跨批次失败次数应累计为3"
        assert champion["last_error_message"] == FINAL_STACK, (
            "最近失败堆栈应原样透传不截断"
        )
        assert isinstance(champion["last_failed_at"], str), (
            "last_failed_at应为ISO字符串"
        )

        runner_up = top[1]
        assert runner_up["case_id"] == "TM-UC-0002"
        assert runner_up["fail_count"] == 1, "fail_count降序TM-UC-0002在后"

    def test_failed_top_limit(self, reports_client: FlaskClient) -> None:
        """
        Top截断: 3个不同case各失败1次，limit=2只返回2条
        （fail_count相同时按case_id升序取前2）
        """
        _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "failed", FAILED_STACK, 0.5),
            ("TM-UC-0002", "用户登录密码错误校验", "failed", FAILED_STACK, 0.5),
            ("TM-OD-0001", "订单创建校验", "failed", FAILED_STACK, 0.5),
        ])

        response = reports_client.get("/api/reports/failed-top?limit=2")
        data = response.get_json()

        assert response.status_code == 200
        top = data["data"]
        assert len(top) == 2, "limit=2应只返回2条"
        assert [item["case_id"] for item in top] == [
            "TM-OD-0001", "TM-UC-0001",
        ], "fail_count并列时按case_id ASCII升序（OD的O<U）"

    def test_failed_top_empty(self, reports_client: FlaskClient) -> None:
        """
        Top空榜: 批次全部通过（无failed/error明细）时返回空列表
        """
        _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "passed", None, 0.5),
            ("TM-OD-0001", "订单创建校验", "skipped", None, 0.5),
        ])

        response = reports_client.get("/api/reports/failed-top")
        data = response.get_json()

        assert response.status_code == 200
        assert data["data"] == [], "无失败记录应返回空列表"

    # ------------------------------------------------------------------
    # 质量度量
    # ------------------------------------------------------------------
    def test_quality_metrics_success(
        self, reports_client: FlaskClient
    ) -> None:
        """
        质量度量: 1批次含TM-UC-0001 passed(0.5s)/TM-UC-0002 passed(1.5s)/
        TM-OD-0001 failed(1.0s)，覆盖率=3/3=1.0、缺陷密度=1/3=0.3333、
        问题用例占比=1/3=0.3333、平均耗时=1.0s、P95小样本取最大=1.5s、
        平均每批次=3.0、code_coverage恒为None
        """
        _create_finished_batch([
            ("TM-UC-0001", "用户登录成功校验", "passed", None, 0.5),
            ("TM-UC-0002", "用户登录密码错误校验", "passed", None, 1.5),
            ("TM-OD-0001", "订单创建校验", "failed", FAILED_STACK, 1.0),
        ])

        response = reports_client.get("/api/reports/quality-metrics")
        data = response.get_json()

        assert response.status_code == 200
        metrics = data["data"]
        # 覆盖率: 执行过3个distinct用例 / active共3条（disabled不计入分母）
        assert metrics["case_execution_coverage"] == 1.0
        # 缺陷密度: 1条failed / 3条明细
        assert metrics["defect_density"] == 0.3333, "round(1/3,4)=0.3333"
        # 问题用例占比: 1个出过失败的distinct用例 / 3条active
        assert metrics["problem_case_ratio"] == 0.3333

        efficiency = metrics["execution_efficiency"]
        assert {
            "avg_duration_sec", "p95_duration_sec", "avg_cases_per_batch"
        } == set(efficiency.keys()), "执行效率子结构应齐全"
        assert efficiency["avg_duration_sec"] == 1.0, "round((0.5+1.5+1.0)/3,2)=1.0"
        assert efficiency["p95_duration_sec"] == 1.5, (
            "样本3条<20取最大值近似"
        )
        assert efficiency["avg_cases_per_batch"] == 3.0, "3条明细/1个批次"

        assert metrics["code_coverage"] is None, (
            "code_coverage为预留契约字段恒为null"
        )

    def test_quality_metrics_empty(
        self, reports_client: FlaskClient
    ) -> None:
        """
        质量度量空库: 无执行明细时全部指标0.0、execution_efficiency
        子结构齐全、code_coverage恒为None，不报错
        """
        response = reports_client.get("/api/reports/quality-metrics")
        data = response.get_json()

        assert response.status_code == 200, "空库质量度量应返回200"
        metrics = data["data"]
        assert metrics["case_execution_coverage"] == 0.0
        assert metrics["defect_density"] == 0.0
        assert metrics["problem_case_ratio"] == 0.0

        efficiency = metrics["execution_efficiency"]
        assert efficiency["avg_duration_sec"] == 0.0
        assert efficiency["p95_duration_sec"] == 0.0
        assert efficiency["avg_cases_per_batch"] == 0.0

        assert metrics["code_coverage"] is None
