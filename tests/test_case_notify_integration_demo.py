"""
批次完成自动通知集成验证用例（第二阶段Day15）

验证目标:
    1. 状态映射: DB四态（passed/failed/error/skipped）→Allure四态
       （error→broken），统计口径自洽
    2. 模块/优先级分组: test_cases关联补全；缺失用例落unknown不报错
    3. 耗时换算: duration秒→stop毫秒
    4. 失败明细: failed/error进入failed_details且error_message带入
    5. notify_execution_result分发/异常旁路/run_batch notify触发/CLI透传

TestMatrix Day27: _execute_batch_async 通知接入验证（追加8条用例，
TestExecuteBatchAsyncNotification）——Web触发执行完成后finished/failed
双分支在通道关闭后旁路调用notify_execution_result，通知异常双层兜底
不影响批次状态，notification_router可选参数供测试注入。

测试基建:
    临时SQLite（对齐既有case_manager测试fixture模式）+ FakeRouter
    （记录调用入参/可配置返回/可配置抛异常），零真实邮件/企微/零真实sleep；
    真线程测试（test 7）双通道轮询（先批次终态、后通知计数），
    禁止固定sleep赌时序；conftest的autouse fixture全局禁用真实
    通知渠道（TM_EMAIL_ENABLED/TM_WECHAT_ENABLED=false）兜底。
"""

import time
from pathlib import Path
from typing import Iterator, Optional, Tuple
from unittest.mock import patch

import allure
import pytest

from src.core import event_bus
from src.core.case_manager import CaseManager, main, run_batch
from src.core.executors import BaseExecutor, ExecutionResult
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

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


# ===========================================================================
# Day27: _execute_batch_async 通知接入测试
# ===========================================================================
# 轮询参数（口径同SSE测试: 模拟执行约0.01s/条，预算远超实际耗时防flaky）
NOTIFY_POLL_MAX_ATTEMPTS = 20
NOTIFY_POLL_INTERVAL_SECONDS = 0.3

# 种子用例数（seed_active_cases固定4条，断言统计模型的基准值）
SEEDED_CASE_COUNT = 4


def seed_active_cases() -> None:
    """
    造active用例种子数据并入库（4条，供start_execution筛选）

    用例编号固定TM-NOTIFY-0001~0004，模块用户中心/订单中心各2条，
    全部active状态保证start_execution(case_type="api")全量选中。

    参数:
        无

    返回:
        无
    """
    seeds = [
        ("TM-NOTIFY-0001", "通知接入用例1", "用户中心"),
        ("TM-NOTIFY-0002", "通知接入用例2", "用户中心"),
        ("TM-NOTIFY-0003", "通知接入用例3", "订单中心"),
        ("TM-NOTIFY-0004", "通知接入用例4", "订单中心"),
    ]
    with DatabaseSession.session_scope() as session:
        for case_id, name, module in seeds:
            session.add(
                models.TestCase(
                    case_id=case_id,
                    name=name,
                    module=module,
                    priority="P1",
                    case_type="api",
                    status="active",
                    description="通知接入测试种子用例",
                )
            )


def start_seeded_batch() -> Tuple[str, list]:
    """
    造种子用例并启动执行批次（内部辅助方法）

    参数:
        无

    返回:
        tuple[str, list]: (执行批次号, 待执行用例字典列表)
    """
    seed_active_cases()
    result = CaseManager.start_execution(
        trigger="web", executor_name="web", case_type="api"
    )
    return result["execution_id"], result["cases"]


class _AllPassStubExecutor(BaseExecutor):
    """
    全部通过的桩执行器（内部测试辅助类）

    run_one固定返回passed，保证同步批次结果确定性（4条全过），
    验证finished分支的通知调用点。
    """

    def run_one(self, case: dict) -> ExecutionResult:
        """
        桩执行: 固定返回passed

        参数:
            case (dict): 待执行用例字典

        返回:
            ExecutionResult: result=passed，error_message=None，duration=0.001
        """
        return ExecutionResult(
            result="passed", error_message=None, duration=0.001
        )


class _FailResultStubExecutor(BaseExecutor):
    """
    全部失败的桩执行器（内部测试辅助类，run_one返回失败结果不抛异常）

    4条用例全部落failed明细（record_execution正常执行），批次本身
    正常finish——用于验证通知统计模型携带失败上下文（stat.failed）。
    """

    def run_one(self, case: dict) -> ExecutionResult:
        """
        桩执行: 固定返回failed结果（带错误信息）

        参数:
            case (dict): 待执行用例字典

        返回:
            ExecutionResult: result=failed，error_message="断言失败"，
            duration=0.001
        """
        return ExecutionResult(
            result="failed", error_message="断言失败", duration=0.001
        )


class _RaisingStubExecutor(BaseExecutor):
    """
    run_one抛异常的桩执行器（内部测试辅助类）

    首条用例即抛RuntimeError，_execute_batch_async顶层except兜底
    置批次failed（record_execution尚未执行，明细表零记录）。
    """

    def run_one(self, case: dict) -> ExecutionResult:
        """
        桩执行: 抛异常模拟执行器崩溃

        参数:
            case (dict): 待执行用例字典

        返回:
            ExecutionResult: 永不返回（直接抛RuntimeError）

        异常:
            RuntimeError: 恒抛出（模拟执行器崩溃）
        """
        raise RuntimeError("模拟执行器崩溃")


@allure.feature("用例调度管理")
@allure.story("批次通知集成")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestExecuteBatchAsyncNotification:
    """_execute_batch_async 终态通知接入验证（Day27）"""

    @pytest.fixture(autouse=True)
    def _clean_event_registry(self) -> Iterator[None]:
        """
        事件通道注册表清洁fixture（autouse，本类全测试生效）

        _execute_batch_async会创建事件通道（batch_start埋点），
        patch关闭方法的测试会残留通道——每条测试前后清空注册表，
        口径同SSE测试（坑7.13: 全局可变状态跨测试必须复位）。

        参数:
            无

        返回:
            Iterator[None]: yield无数据，前后各清空注册表一次
        """
        event_bus.reset_channels()
        yield
        event_bus.reset_channels()

    def test_finished_batch_invokes_notify_execution_result(
        self, temp_db, monkeypatch
    ) -> None:
        """
        finished分支调用点: 全过桩执行器同步跑完批次，
        notify_execution_result被调用1次且首参为批次号

        参数:
            temp_db (Path): 临时数据库fixture
            monkeypatch (pytest.MonkeyPatch): 补丁工具

        返回:
            无
        """
        execution_id, cases = start_seeded_batch()
        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _AllPassStubExecutor(),
        )
        with patch(
            "src.core.case_manager.CaseManager.notify_execution_result"
        ) as mock_notify:
            CaseManager._execute_batch_async(execution_id, cases, None)

        mock_notify.assert_called_once()
        assert mock_notify.call_args[0][0] == execution_id, (
            "通知调用首参应为执行批次号"
        )

    def test_failed_batch_invokes_notify_execution_result(
        self, temp_db, monkeypatch
    ) -> None:
        """
        failed分支调用点: 执行器run_one抛异常造failed批次，
        notify_execution_result仍被调用1次

        说明: 异常批次run_one抛异常时record_execution尚未执行，
        明细表零记录，notify_execution_result内部统计模型为None
        时跳过Router.notify——本测只验证except分支调用点存在，
        不断言FakeRouter收到调用。

        参数:
            temp_db (Path): 临时数据库fixture
            monkeypatch (pytest.MonkeyPatch): 补丁工具

        返回:
            无
        """
        execution_id, cases = start_seeded_batch()
        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _RaisingStubExecutor(),
        )
        with patch(
            "src.core.case_manager.CaseManager.notify_execution_result"
        ) as mock_notify:
            CaseManager._execute_batch_async(execution_id, cases, None)

        status = CaseManager.get_execution_status(execution_id)
        assert status is not None and status["status"] == "failed", (
            "run_one抛异常批次应置failed"
        )
        mock_notify.assert_called_once()
        assert mock_notify.call_args[0][0] == execution_id, (
            "failed分支同样应以批次号为首参调用通知"
        )

    def test_notify_exception_does_not_corrupt_batch_status(
        self, temp_db, monkeypatch
    ) -> None:
        """
        通知异常兜底: notify_execution_result抛RuntimeError时
        批次状态仍为finished（外层try接盘，通知异常不影响主流程，
        通知旁路铁律的接入层保障）

        参数:
            temp_db (Path): 临时数据库fixture
            monkeypatch (pytest.MonkeyPatch): 补丁工具

        返回:
            无
        """
        execution_id, cases = start_seeded_batch()
        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _AllPassStubExecutor(),
        )
        with patch(
            "src.core.case_manager.CaseManager.notify_execution_result",
            side_effect=RuntimeError("通知链路崩溃"),
        ):
            CaseManager._execute_batch_async(execution_id, cases, None)

        status = CaseManager.get_execution_status(execution_id)
        assert status is not None and status["status"] == "finished", (
            "通知异常不应把批次状态搞挂（旁路铁律）"
        )
        assert status["passed"] == SEEDED_CASE_COUNT

    def test_notify_called_after_channel_closed(
        self, temp_db, monkeypatch
    ) -> None:
        """
        调用顺序: 通知必须在_close_execution_channel之后调用
        （SSE终态事件先于通知送达客户端；通知含指数退避重试
        可能耗时数秒，不能阻塞订阅方收终态事件）

        参数:
            temp_db (Path): 临时数据库fixture
            monkeypatch (pytest.MonkeyPatch): 补丁工具

        返回:
            无
        """
        execution_id, cases = start_seeded_batch()
        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _AllPassStubExecutor(),
        )
        order_list: list = []

        def _fake_close(
            batch_id: str, reason: Optional[str] = None
        ) -> None:
            """记录型通道关闭替身（记录调用顺序）"""
            order_list.append("close")

        def _fake_notify(*args, **kwargs) -> dict:
            """记录型通知替身（记录调用顺序）"""
            order_list.append("notify")
            return {}

        monkeypatch.setattr(
            CaseManager, "_close_execution_channel", _fake_close
        )
        monkeypatch.setattr(
            CaseManager, "notify_execution_result", _fake_notify
        )
        CaseManager._execute_batch_async(execution_id, cases, None)

        assert order_list == ["close", "notify"], (
            "通知必须在通道关闭之后调用（SSE终态事件优先）"
        )

    def test_notification_router_injection_propagates(
        self, temp_db, monkeypatch
    ) -> None:
        """
        router透传: notification_router参数注入FakeRouter实例，
        批次完成后FakeRouter.calls收到(stat, execution_id, strategy)
        三元组——统计模型覆盖全部种子用例、批次号一致

        参数:
            temp_db (Path): 临时数据库fixture
            monkeypatch (pytest.MonkeyPatch): 补丁工具

        返回:
            无
        """
        execution_id, cases = start_seeded_batch()
        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _AllPassStubExecutor(),
        )
        router = FakeRouter()
        CaseManager._execute_batch_async(
            execution_id, cases, None, notification_router=router
        )

        assert len(router.calls) == 1, "注入的router应恰好收到1次notify"
        stat, called_execution_id, strategy = router.calls[0]
        assert stat.total == SEEDED_CASE_COUNT, (
            "统计模型应覆盖全部种子用例"
        )
        assert called_execution_id == execution_id
        assert strategy is None, "未显式传策略时应为None（Router用自身策略）"

    def test_default_router_instantiated_when_none(
        self, temp_db, monkeypatch
    ) -> None:
        """
        默认实例化: 不注入router时notify_execution_result内部
        实例化NotificationRouter恰1次并调用其notify（Web触发
        路径不传router，走此默认链路，策略由env控制）

        参数:
            temp_db (Path): 临时数据库fixture
            monkeypatch (pytest.MonkeyPatch): 补丁工具

        返回:
            无
        """
        execution_id, cases = start_seeded_batch()
        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _AllPassStubExecutor(),
        )
        created: list = []
        notify_calls: list = []

        class _RecordingRouter:
            """记录型router替身类（记录实例化与notify调用）"""

            def __init__(self) -> None:
                created.append(self)

            def notify(self, stat, batch_id, strategy=None) -> dict:
                """
                记录型notify（记录入参三元组）

                参数:
                    stat (StatisticsResult): 批次统计模型
                    batch_id (str): 执行批次号
                    strategy (str | None): 通知策略

                返回:
                    dict: 空结果字典
                """
                notify_calls.append((stat, batch_id, strategy))
                return {}

        monkeypatch.setattr(
            "src.core.case_manager.NotificationRouter", _RecordingRouter
        )
        CaseManager._execute_batch_async(execution_id, cases, None)

        assert len(created) == 1, "NotificationRouter应被默认实例化恰1次"
        assert len(notify_calls) == 1, "默认router的notify应被调用1次"
        assert notify_calls[0][1] == execution_id

    def test_trigger_real_thread_notifies_on_completion(
        self, temp_db, monkeypatch
    ) -> None:
        """
        Web触发真线程端到端: POST /trigger后台线程执行完成后
        自动调用notify_execution_result

        防flaky双通道收敛（禁止固定sleep）:
            通道一: 轮询批次终态（防teardown与执行线程竞态）
            通道二: 终态后继续轮询通知调用计数——finished状态
            置位先于通知调用，终态不等于通知已完成

        参数:
            temp_db (Path): 临时数据库fixture
            monkeypatch (pytest.MonkeyPatch): 补丁工具

        返回:
            无
        """
        seed_active_cases()
        # 清除TM_EXECUTOR保证真线程确定性走simulated执行器
        monkeypatch.delenv("TM_EXECUTOR", raising=False)
        # 线程安全recording fake: list.append在GIL下原子，
        # 后台线程与测试主线程无锁并发安全
        notify_calls: list = []

        def _fake_notify(*args, **kwargs) -> dict:
            """线程安全记录型通知替身（记录调用入参）"""
            notify_calls.append(args)
            return {}

        monkeypatch.setattr(
            CaseManager, "notify_execution_result", _fake_notify
        )
        client = create_app("test").test_client()
        trigger_response = client.post("/api/executions/trigger")
        assert trigger_response.status_code == 202
        execution_id = trigger_response.get_json()["data"]["execution_id"]

        # 通道一: 轮询批次终态（finished/failed）
        status_data = None
        for _ in range(NOTIFY_POLL_MAX_ATTEMPTS):
            status_data = CaseManager.get_execution_status(execution_id)
            if (
                status_data is not None
                and status_data["status"] in ("finished", "failed")
            ):
                break
            time.sleep(NOTIFY_POLL_INTERVAL_SECONDS)
        assert status_data is not None, "批次状态查询不应为空"
        assert status_data["status"] == "finished", (
            f"真线程批次应正常完成 | 实际: {status_data['status']}"
        )

        # 通道二: 终态后继续轮询通知调用计数（状态置位先于通知调用）
        for _ in range(NOTIFY_POLL_MAX_ATTEMPTS):
            if len(notify_calls) >= 1:
                break
            time.sleep(NOTIFY_POLL_INTERVAL_SECONDS)

        assert len(notify_calls) == 1, (
            f"批次完成后应恰好触发1次通知 | 实际: {len(notify_calls)}次"
        )
        assert notify_calls[0][0] == execution_id

    def test_failed_batch_notification_carries_failure_stat(
        self, temp_db, monkeypatch
    ) -> None:
        """
        失败统计透传: 失败结果桩执行器（run_one返回failed结果
        不抛异常），4条用例全部落failed明细且批次正常finish，
        通知统计模型携带失败上下文（stat.failed==4、total==4）

        参数:
            temp_db (Path): 临时数据库fixture
            monkeypatch (pytest.MonkeyPatch): 补丁工具

        返回:
            无
        """
        execution_id, cases = start_seeded_batch()
        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _FailResultStubExecutor(),
        )
        router = FakeRouter()
        CaseManager._execute_batch_async(
            execution_id, cases, None, notification_router=router
        )

        status = CaseManager.get_execution_status(execution_id)
        assert status is not None and status["status"] == "finished", (
            "失败结果不抛异常，批次本身应正常完成"
        )
        assert len(router.calls) == 1, "应恰好触发1次通知"
        stat = router.calls[0][0]
        assert stat.failed == SEEDED_CASE_COUNT, (
            "统计模型应携带全部4条失败明细"
        )
        assert stat.total == SEEDED_CASE_COUNT
