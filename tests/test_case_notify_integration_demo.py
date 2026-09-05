"""
批次完成自动通知集成验证用例（第二阶段Day15）

验证目标:
    1. 状态映射: DB四态（passed/failed/error/skipped）→Allure四态
       （error→broken），统计口径自洽
    2. 模块/优先级分组: test_cases关联补全；缺失用例落unknown不报错
    3. 耗时换算: duration秒→stop毫秒
    4. 失败明细: failed/error进入failed_details且error_message带入
    5. notify_execution_result分发/异常旁路/run_batch notify触发/CLI透传

测试基建:
    临时SQLite（对齐既有case_manager测试fixture模式）+ FakeRouter
    （记录调用入参/可配置返回/可配置抛异常），零真实邮件/企微/零真实sleep。
"""

from pathlib import Path
from unittest.mock import patch

import allure
import pytest

from src.core.case_manager import CaseManager, main, run_batch
from src.db import models
from src.db.db_session import DatabaseSession

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 演示数据文件（4条: TM-API-0201[P0]...0204[P2]，模块"用户管理"）
DATA_FILE = PROJECT_ROOT / "testdata" / "yaml" / "api_user_query_matrix.yaml"

# 演示数据库文件
DEMO_DB = PROJECT_ROOT / "output" / "test_case_notify_integration.db"


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """
    临时SQLite数据库fixture（对齐既有case_manager测试模式）

    参数:
        tmp_path (Path): 临时目录
        monkeypatch: 环境变量覆写

    返回:
        Path: 临时库文件路径
    """
    db_file = tmp_path / "test_notify_integration.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_file
    DatabaseSession.reset()
    if db_file.exists():
        db_file.unlink()


class FakeRouter:
    """记录型fake路由器（记录入参/可配置返回/可配置抛异常）"""

    def __init__(self, result: dict = None, raise_exception: bool = False):
        """
        初始化fake路由器

        参数:
            result (dict | None): notify返回值（默认双渠道成功）
            raise_exception (bool): True时notify恒抛异常
        """
        self._result = result if result is not None else {
            "email": True, "wechat": True
        }
        self._raise = raise_exception
        self.calls: list = []  # [(stat, execution_id, strategy), ...]

    def notify(self, stat, execution_id, strategy=None):
        self.calls.append((stat, execution_id, strategy))
        if self._raise:
            raise RuntimeError("模拟通知路由失败")
        return self._result


def seed_batch(execution_id: str, records: list, cases: list = None) -> None:
    """
    造批次数据（test_cases可选 + test_executions必造）

    参数:
        execution_id (str): 批次号
        records (list): 执行记录元组 (case_id, case_name, result, duration, error_message)
        cases (list | None): 用例元组 (case_id, module, priority)，默认不造

    返回:
        无
    """
    with DatabaseSession.session_scope() as session:
        for case_id, module, priority in (cases or []):
            session.add(models.TestCase(
                case_id=case_id, name=f"用例{case_id}", module=module,
                priority=priority, case_type="api", status="active",
            ))
        for case_id, case_name, result, duration, error_message in records:
            session.add(models.TestExecution(
                execution_id=execution_id, case_id=case_id,
                case_name=case_name, result=result,
                start_time=None, end_time=None,
                duration=duration, error_message=error_message,
            ))


@allure.feature("用例调度管理")
@allure.story("批次通知集成")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestBuildNotificationStatistics:
    """执行记录→统计模型适配验证"""

    def test_status_mapping(self, temp_db):
        """
        状态映射: passed/failed/error/skipped各1条→
        total=4、passed=1、broken=1、failed=2（含broken）、skipped=1

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        seed_batch("RUN-MAP-001", [
            ("C-001", "通过用例", "passed", 0.1, None),
            ("C-002", "失败用例", "failed", 0.2, "断言失败"),
            ("C-003", "错误用例", "error", 0.3, "连接超时"),
            ("C-004", "跳过用例", "skipped", 0.0, None),
        ])

        stat = CaseManager.build_notification_statistics("RUN-MAP-001")

        assert stat is not None
        assert stat.total == 4
        assert stat.passed == 1
        assert stat.broken == 1  # DB error→Allure broken
        assert stat.failed == 2  # failed+broken合计口径
        assert stat.skipped == 1
        assert stat.pass_rate == 0.25

    def test_module_grouping_with_unknown(self, temp_db):
        """
        模块分组: by_module按test_cases.module分组；
        test_cases中不存在的case_id落unknown且不报错

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        seed_batch(
            "RUN-MOD-001",
            [
                ("C-101", "用户用例", "passed", 0.1, None),
                ("C-102", "订单用例", "passed", 0.1, None),
                ("C-999", "孤儿用例", "failed", 0.1, "失败"),
            ],
            cases=[
                ("C-101", "用户管理", "P0"),
                ("C-102", "订单管理", "P1"),
                # C-999故意不造，验证unknown降级
            ],
        )

        stat = CaseManager.build_notification_statistics("RUN-MOD-001")

        assert set(stat.by_module.keys()) == {"用户管理", "订单管理", "unknown"}
        assert stat.by_module["用户管理"].total == 1
        assert stat.by_module["unknown"].failed == 1

    def test_priority_grouping(self, temp_db):
        """
        优先级分组: by_priority按severity（P0/P1）正确分组计数

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        seed_batch(
            "RUN-PRI-001",
            [
                ("C-201", "p0用例1", "passed", 0.1, None),
                ("C-202", "p0用例2", "failed", 0.1, "失败"),
                ("C-203", "p1用例", "passed", 0.1, None),
            ],
            cases=[
                ("C-201", "用户管理", "P0"),
                ("C-202", "用户管理", "P0"),
                ("C-203", "用户管理", "P1"),
            ],
        )

        stat = CaseManager.build_notification_statistics("RUN-PRI-001")

        assert set(stat.by_priority.keys()) == {"P0", "P1"}
        assert stat.by_priority["P0"].total == 2
        assert stat.by_priority["P0"].failed == 1
        assert stat.by_priority["P1"].total == 1

    def test_duration_conversion(self, temp_db):
        """
        耗时换算: duration=0.5秒→总耗时500ms量级、avg正确

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        seed_batch("RUN-DUR-001", [
            ("C-301", "用例1", "passed", 0.5, None),
            ("C-302", "用例2", "passed", 0.5, None),
        ])

        stat = CaseManager.build_notification_statistics("RUN-DUR-001")

        assert stat.total_duration_ms == 1000  # 2条×500ms
        assert stat.avg_duration_ms == 500.0
        assert stat.max_duration_ms == 500
        assert stat.min_duration_ms == 500

    def test_failed_details_extraction(self, temp_db):
        """
        失败明细: failed/error行进入failed_details，
        error_message正确带入，status分别为failed/broken

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        seed_batch("RUN-FD-001", [
            ("C-401", "通过用例", "passed", 0.1, None),
            ("C-402", "失败用例", "failed", 0.2, "业务码期望0实际2001"),
            ("C-403", "错误用例", "error", 0.3, "ConnectionError: 超时"),
        ])

        stat = CaseManager.build_notification_statistics("RUN-FD-001")

        assert len(stat.failed_details) == 2
        by_case = {detail.name: detail for detail in stat.failed_details}
        assert by_case["失败用例"].status == "failed"
        assert by_case["失败用例"].error_message == "业务码期望0实际2001"
        assert by_case["错误用例"].status == "broken"
        assert "ConnectionError" in by_case["错误用例"].error_message

    def test_empty_batch_returns_none(self, temp_db):
        """
        批次无记录: build_notification_statistics返回None

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        result = CaseManager.build_notification_statistics("RUN-EMPTY-999")
        assert result is None


@allure.feature("用例调度管理")
@allure.story("批次通知集成")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestNotifyExecutionResult:
    """notify_execution_result分发与run_batch集成验证"""

    def test_notify_dispatch_with_fake_router(self, temp_db):
        """
        正常分发: FakeRouter收到的execution_id与stat关键字段正确，
        返回值透传

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        seed_batch("RUN-DISP-001", [
            ("C-501", "通过用例", "passed", 0.1, None),
            ("C-502", "失败用例", "failed", 0.2, "断言失败"),
        ])
        router = FakeRouter()

        result = CaseManager.notify_execution_result(
            "RUN-DISP-001", router=router, strategy="failed_only"
        )

        assert result == {"email": True, "wechat": True}
        assert len(router.calls) == 1
        stat, execution_id, strategy = router.calls[0]
        assert execution_id == "RUN-DISP-001"
        assert strategy == "failed_only"
        assert stat.total == 2
        assert stat.failed == 1

    def test_notify_exception_bypass(self, temp_db):
        """
        通知异常旁路: FakeRouter.notify抛异常→方法返回{}不抛；
        同用例验证run_batch主流程summary仍正常返回

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        router = FakeRouter(raise_exception=True)
        result = CaseManager.notify_execution_result(
            "RUN-EXC-001", router=router
        )
        assert result == {}

        # run_batch主流程不受通知异常影响
        with patch(
            "src.core.case_manager.CaseManager.notify_execution_result",
            side_effect=RuntimeError("通知全链路崩溃"),
        ):
            summary = run_batch(DATA_FILE, notify=True)

        assert summary["total"] == 4
        assert summary["execution_id"]

    def test_run_batch_default_no_notify(self, temp_db):
        """
        默认不触发: run_batch(notify=False)（默认）时
        notify_execution_result零调用

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        with patch(
            "src.core.case_manager.CaseManager.notify_execution_result"
        ) as mock_notify:
            run_batch(DATA_FILE)

        mock_notify.assert_not_called()

    def test_run_batch_notify_and_cli_flag(self, temp_db, monkeypatch):
        """
        显式触发+CLI: run_batch(notify=True)恰好触发1次且批次号一致；
        CLI --notify透传notify=True（不传时为False）

        参数:
            temp_db (Path): 临时数据库fixture
            monkeypatch: sys.argv覆写

        返回:
            无
        """
        # 1. run_batch显式触发
        with patch(
            "src.core.case_manager.CaseManager.notify_execution_result"
        ) as mock_notify:
            summary = run_batch(DATA_FILE, notify=True)

        mock_notify.assert_called_once()
        assert mock_notify.call_args[0][0] == summary["execution_id"]

        # 2. CLI --notify透传
        monkeypatch.setattr(
            "sys.argv",
            ["case_manager", "-f", str(DATA_FILE), "--notify"],
        )
        with patch("src.core.case_manager.run_batch") as mock_run:
            main()
        assert mock_run.call_args.kwargs["notify"] is True

        # 3. CLI不传时默认False
        monkeypatch.setattr(
            "sys.argv", ["case_manager", "-f", str(DATA_FILE)],
        )
        with patch("src.core.case_manager.run_batch") as mock_run:
            main()
        assert mock_run.call_args.kwargs["notify"] is False
