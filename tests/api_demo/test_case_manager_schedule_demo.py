"""
case_manager执行调度模块演示与验证用例（第二阶段Day2）

验证目标:
    1. create_execution: 批次号格式正则 + 各合法trigger可用 + 非法trigger抛异常
    2. select_cases_for_execution: 默认返回active用例、priority筛选、
       tags交集筛选（description"标签:"格式解析）、disabled用例不返回
    3. record_execution: 4种合法result正常写入、非法result抛异常、
       failed/error时error_message必填校验
    4. finish_execution: 统计数字与pass_rate核对、defect_statistics落表、
        不存在批次抛异常
    5. 完整链路: create_execution -> select_cases -> 逐条record -> finish
       端到端数据一致性验证

数据隔离设计:
    复用Day1的demo_db fixture模式: monkeypatch独立库 + 测试后reset释放连接并删库，
    不污染默认库、不留测试垃圾。
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

import allure
import pytest

from src.core.case_manager import CaseManager, CaseManagerError
from src.db import models
from src.db.db_session import DatabaseSession

# 项目根目录（本文件位于 tests/api_demo/ 下，向上两级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 同步用演示数据文件（4条用例: TM-API-0201[P0,smoke/api] / 0202[P1,api/regression]
# / 0203[P1,regression/security] / 0204[P2,regression]，模块均为"用户管理"）
DATA_FILE = PROJECT_ROOT / "testdata" / "yaml" / "api_user_query_matrix.yaml"

# 演示数据库文件路径（与fixture中环境变量指向的路径一致）
DEMO_DB = PROJECT_ROOT / "output" / "test_case_manager_schedule_demo.db"

# 批次号格式: RUN-YYYYMMDD-HHMMSS-xxxx（xxxx为4位hex小写）
EXECUTION_ID_PATTERN = re.compile(r"^RUN-\d{8}-\d{6}-[0-9a-f]{4}$")


@pytest.fixture()
def demo_db(monkeypatch):
    """
    独立演示数据库fixture（每个测试独享干净数据库）

    测试前:
        1. monkeypatch覆写TM_DB_SQLITE_PATH指向独立库文件
        2. DatabaseSession.reset()清除引擎单例（保证新环境变量生效）
        3. DatabaseSession.init_db()在独立库中建表
    测试后:
        1. reset释放数据库连接（Windows下必须先释放才能删除库文件）
        2. 删除演示库文件

    参数:
        monkeypatch (pytest.MonkeyPatch): pytest内置monkeypatch fixture

    返回:
        Generator[Path]: yield演示库文件路径
    """
    monkeypatch.setenv(
        "TM_DB_SQLITE_PATH", "output/test_case_manager_schedule_demo.db"
    )
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield DEMO_DB
    DatabaseSession.reset()
    if DEMO_DB.exists():
        DEMO_DB.unlink()


def _record_case(execution_id: str, case: dict, result: str,
                 error_message: str = None) -> None:
    """
    按统一模板写入单条执行结果（模块内工具函数，简化测试组装）

    参数:
        execution_id (str): 执行批次号
        case (dict): 用例字典（含case_id/name）
        result (str): 执行结果
        error_message (str | None): 失败/错误时的异常信息

    返回:
        无
    """
    start_time = datetime(2026, 8, 25, 10, 0, 0)
    end_time = start_time + timedelta(seconds=0.5)
    CaseManager.record_execution(
        execution_id=execution_id,
        case_id=case["case_id"],
        case_name=case["name"],
        result=result,
        start_time=start_time,
        end_time=end_time,
        duration=0.5,
        error_message=error_message,
    )


@allure.feature("用例调度管理")
@allure.story("创建执行批次")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestCreateExecution:
    """create_execution批次创建验证"""

    def test_create_execution_returns_valid_id(self):
        """
        批次创建: 返回批次号匹配RUN-YYYYMMDD-HHMMSS-xxxx格式，
        四种合法trigger（manual/cli/web/ci）均可创建

        参数:
            无

        返回:
            无
        """
        for trigger in ("manual", "cli", "web", "ci"):
            execution_id = CaseManager.create_execution(trigger=trigger)
            assert EXECUTION_ID_PATTERN.match(execution_id), (
                f"trigger={trigger}生成的批次号格式非法: {execution_id}"
            )

    def test_create_execution_invalid_trigger_raises(self):
        """
        异常路径: 非法trigger（cron）抛CaseManagerError，
        context携带operation与入参值

        参数:
            无

        返回:
            无
        """
        with pytest.raises(CaseManagerError) as exc_info:
            CaseManager.create_execution(trigger="cron")
        assert exc_info.value.context.get("trigger") == "cron"
        assert "cron" in str(exc_info.value)


@allure.feature("用例调度管理")
@allure.story("筛选待执行用例")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestSelectCasesForExecution:
    """select_cases_for_execution待执行用例筛选验证"""

    @pytest.fixture(autouse=True)
    def setup_cases(self, demo_db):
        """
        类内共用前置: 每个测试先同步4条标准用例入库

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        CaseManager.sync_cases_from_file(DATA_FILE)

    def test_select_default_returns_all_active_sorted(self):
        """
        默认筛选: 返回4条active的api用例，
        按 priority升序（P0→P1→P1→P2）再case_id升序排列

        参数:
            无

        返回:
            无
        """
        cases = CaseManager.select_cases_for_execution()

        assert len(cases) == 4
        assert [case["case_id"] for case in cases] == [
            "TM-API-0201", "TM-API-0202", "TM-API-0203", "TM-API-0204",
        ]
        assert [case["priority"] for case in cases] == ["P0", "P1", "P1", "P2"]

    def test_select_by_priority(self):
        """
        优先级筛选: P0命中1条；P0+P2列表命中2条且保持P0在前

        参数:
            无

        返回:
            无
        """
        p0_cases = CaseManager.select_cases_for_execution(priority="P0")
        assert [case["case_id"] for case in p0_cases] == ["TM-API-0201"]

        p0_p2_cases = CaseManager.select_cases_for_execution(priority=["P0", "P2"])
        assert [case["case_id"] for case in p0_p2_cases] == [
            "TM-API-0201", "TM-API-0204",
        ]

    def test_select_by_tags_intersection(self):
        """
        标签交集筛选: smoke命中1条 / regression命中3条 /
        security+smoke双标签命中2条（0201与0203任一命中）

        参数:
            无

        返回:
            无
        """
        smoke_cases = CaseManager.select_cases_for_execution(tags=["smoke"])
        assert [case["case_id"] for case in smoke_cases] == ["TM-API-0201"]

        regression_cases = CaseManager.select_cases_for_execution(tags="regression")
        assert len(regression_cases) == 3

        mixed_cases = CaseManager.select_cases_for_execution(
            tags=["security", "smoke"]
        )
        assert [case["case_id"] for case in mixed_cases] == [
            "TM-API-0201", "TM-API-0203",
        ]

    def test_select_excludes_disabled_cases(self):
        """
        状态过滤: 手动停用TM-API-0204后，默认筛选不返回该用例（仅3条）

        参数:
            无

        返回:
            无
        """
        with DatabaseSession.session_scope() as session:
            session.query(models.TestCase).filter_by(
                case_id="TM-API-0204"
            ).update({"status": "disabled"})

        cases = CaseManager.select_cases_for_execution()
        assert len(cases) == 3
        assert "TM-API-0204" not in [case["case_id"] for case in cases]


@allure.feature("用例调度管理")
@allure.story("记录执行结果")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestRecordExecution:
    """record_execution单条执行结果入库验证"""

    def test_record_four_valid_results(self, demo_db):
        """
        正常写入: passed/failed/error/skipped四种结果各写一条，
        库内记录数与关键字段核对

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        execution_id = CaseManager.create_execution(trigger="manual")
        _record_case(execution_id, {"case_id": "TM-0001", "name": "通过用例"}, "passed")
        _record_case(
            execution_id, {"case_id": "TM-0002", "name": "失败用例"}, "failed",
            error_message="断言失败: 期望200实际500",
        )
        _record_case(
            execution_id, {"case_id": "TM-0003", "name": "错误用例"}, "error",
            error_message="连接超时: ConnectionTimeout",
        )
        _record_case(execution_id, {"case_id": "TM-0004", "name": "跳过用例"}, "skipped")

        with DatabaseSession.session_scope() as session:
            records = (
                session.query(models.TestExecution)
                .filter_by(execution_id=execution_id)
                .all()
            )
            assert len(records) == 4
            results = {record.case_id: record.result for record in records}
            assert results == {
                "TM-0001": "passed",
                "TM-0002": "failed",
                "TM-0003": "error",
                "TM-0004": "skipped",
            }
            # failed记录的error_message已落库
            failed_record = next(
                record for record in records if record.case_id == "TM-0002"
            )
            assert "断言失败" in failed_record.error_message

    def test_record_invalid_result_raises(self, demo_db):
        """
        异常路径: 非法result（crashed）抛CaseManagerError，
        context携带result入参值

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        with pytest.raises(CaseManagerError) as exc_info:
            _record_case(
                "RUN-20260825-100000-abcd",
                {"case_id": "TM-0001", "name": "非法结果"}, "crashed",
            )
        assert exc_info.value.context.get("result") == "crashed"

    @pytest.mark.parametrize("invalid_result", ["failed", "error"])
    def test_record_requires_error_message(self, demo_db, invalid_result):
        """
        必填校验: failed/error结果未传error_message时抛CaseManagerError

        参数:
            demo_db (Path): 独立演示数据库fixture
            invalid_result (str): 需要error_message的结果类型

        返回:
            无
        """
        with pytest.raises(CaseManagerError, match="error_message必填"):
            _record_case(
                "RUN-20260825-100000-abcd",
                {"case_id": "TM-0001", "name": "缺失错误信息"}, invalid_result,
            )


@allure.feature("用例调度管理")
@allure.story("批次汇总统计")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestFinishExecution:
    """finish_execution批次汇总统计验证"""

    def test_finish_execution_summary_and_statistics(self, demo_db):
        """
        汇总统计: 3passed+1failed批次，统计数字核对、pass_rate=0.75、
        defect_statistics表落库记录核对

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        execution_id = CaseManager.create_execution(trigger="cli", executor="jenkins")
        _record_case(execution_id, {"case_id": "TM-0001", "name": "用例1"}, "passed")
        _record_case(execution_id, {"case_id": "TM-0002", "name": "用例2"}, "passed")
        _record_case(execution_id, {"case_id": "TM-0003", "name": "用例3"}, "passed")
        _record_case(
            execution_id, {"case_id": "TM-0004", "name": "用例4"}, "failed",
            error_message="断言失败",
        )

        summary = CaseManager.finish_execution(execution_id)

        assert summary == {
            "execution_id": execution_id,
            "total": 4,
            "passed": 3,
            "failed": 1,
            "error": 0,
            "skipped": 0,
            "pass_rate": 0.75,
        }

        # defect_statistics表落库核对
        with DatabaseSession.session_scope() as session:
            statistic = (
                session.query(models.DefectStatistic)
                .filter_by(execution_id=execution_id)
                .one()
            )
            assert statistic.total_cases == 4
            assert statistic.passed == 3
            assert statistic.failed == 1
            assert statistic.pass_rate == 0.75

    def test_finish_execution_upsert_idempotent(self, demo_db):
        """
        幂等刷新: 同一批次重复finish不产生重复统计记录，指标原地更新

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        execution_id = CaseManager.create_execution(trigger="manual")
        _record_case(execution_id, {"case_id": "TM-0001", "name": "用例1"}, "passed")

        CaseManager.finish_execution(execution_id)
        # 追加一条失败记录后重复finish，统计应刷新且记录仍唯一
        _record_case(
            execution_id, {"case_id": "TM-0002", "name": "用例2"}, "failed",
            error_message="断言失败",
        )
        summary = CaseManager.finish_execution(execution_id)

        assert summary["total"] == 2
        assert summary["pass_rate"] == 0.5
        with DatabaseSession.session_scope() as session:
            count = (
                session.query(models.DefectStatistic)
                .filter_by(execution_id=execution_id)
                .count()
            )
            assert count == 1, "重复finish不应产生重复统计记录"

    def test_finish_execution_unknown_batch_raises(self, demo_db):
        """
        异常路径: 不存在的批次（无执行记录）抛CaseManagerError

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        with pytest.raises(CaseManagerError, match="不存在"):
            CaseManager.finish_execution("RUN-20260825-999999-xxxx")


@allure.feature("用例调度管理")
@allure.story("完整执行链路")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.api
@pytest.mark.regression
class TestExecutionLifecycle:
    """create->select->record->finish端到端完整链路验证"""

    def test_full_execution_lifecycle(self, demo_db):
        """
        端到端链路: 创建批次→筛选P0+P1用例（3条）→逐条记录结果→
        汇总统计，验证三张表数据全链路一致

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        # 1. 用例入库并创建批次
        CaseManager.sync_cases_from_file(DATA_FILE)
        execution_id = CaseManager.create_execution(
            trigger="ci", executor="jenkins", environment="test",
            remark="冒烟+核心回归",
        )
        assert EXECUTION_ID_PATTERN.match(execution_id)

        # 2. 筛选P0+P1待执行用例（3条，P0在前）
        selected = CaseManager.select_cases_for_execution(priority=["P0", "P1"])
        assert len(selected) == 3

        # 3. 逐条记录执行结果（前2条通过，第3条失败）
        for index, case in enumerate(selected):
            result = "failed" if index == len(selected) - 1 else "passed"
            _record_case(
                execution_id, case, result,
                error_message="断言失败: 业务码期望0实际2002" if result == "failed" else None,
            )

        # 4. 完成批次汇总
        summary = CaseManager.finish_execution(execution_id)
        assert summary["total"] == 3
        assert summary["passed"] == 2
        assert summary["failed"] == 1
        assert summary["pass_rate"] == round(2 / 3, 4)

        # 5. 端到端数据一致性: 执行明细与汇总统计对齐
        with DatabaseSession.session_scope() as session:
            executions = (
                session.query(models.TestExecution)
                .filter_by(execution_id=execution_id)
                .order_by(models.TestExecution.id)
                .all()
            )
            assert len(executions) == 3
            # 明细case_id与筛选结果一致
            assert [record.case_id for record in executions] == [
                case["case_id"] for case in selected
            ]

            statistic = (
                session.query(models.DefectStatistic)
                .filter_by(execution_id=execution_id)
                .one()
            )
            assert statistic.total_cases == summary["total"]
            assert statistic.passed == summary["passed"]
            assert statistic.pass_rate == summary["pass_rate"]
