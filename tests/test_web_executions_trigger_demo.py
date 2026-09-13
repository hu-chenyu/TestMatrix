"""
TestMatrix Day24: 用例执行触发API测试
（POST /api/executions/trigger + GET /api/executions/<id>/status）

测试覆盖:
    触发:
        1. test_trigger_success          无请求体全量回归: 202 +
           execution_id匹配RUN-前缀 + status=pending + total_cases=种子数
        2. test_trigger_with_filter      module筛选: 只执行该模块用例，
           total_cases正确且最终通过/失败计数与奇偶规则一致
        3. test_trigger_no_cases         无匹配用例: 400 +
           message含"无符合条件的用例"
        4. test_trigger_invalid_executor executor非法值: 400
        5. test_status_not_found         状态查询不存在批次: 404
    异步链路:
        6. test_async_runs_to_finish     trigger立即202返回，轮询批次
           至finished，通过/失败计数与种子奇偶规则手算一致，
           defect_statistics有该批次记录
        7. test_execute_batch_sync       同步直调_execute_batch_async:
           批次pending→running→finished状态转换正确，
           明细表行数=用例数
        8. test_batch_failed_status     注入必抛异常的假执行器: 批次
           落failed且error_message非空，不崩进程
        9. test_executor_factory         工厂返回正确类型 +
           非法值ValueError + TM_EXECUTOR环境变量默认链
        10. test_pytest_runner_exit_code mock子进程退出码:
            0→passed / 1→failed / 2→error且error_message非空

异步测试原则（防flaky）:
    - 编排逻辑验证（状态机/异常兜底）同步直调_execute_batch_async，
      不走真实线程（确定性优先）
    - 仅test_async_runs_to_finish走真实后台线程，轮询status至终态
      （模拟执行0.01s/条必然完成，10次×0.3s预算远超执行耗时）
    - 所有触发过线程的测试结束前轮询至终态，防teardown与
      后台线程竞态（DatabaseSession.reset()期间线程重建引擎
      可能指向库外文件，见PROJECT_CONTEXT 7.12）

测试基建:
    临时SQLite文件库（tmp_path + 前后DatabaseSession.reset()防
    Windows文件锁）+ Flask test client端到端验证；种子4条active
    用例（TM-UC-0001/0002用户中心、TM-OD-0001/0002订单中心，
    末位奇偶各半，模拟执行必有通过有失败便于断言）。
"""

import re
import time
from pathlib import Path
from typing import Iterator, Optional
from unittest.mock import MagicMock, patch

import allure
import pytest
from flask.testing import FlaskClient

from src.core.case_manager import CaseManager
from src.core.executors import (
    BaseExecutor,
    ExecutionResult,
    PytestRunner,
    SimulatedExecutor,
    get_executor,
)
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

# 轮询参数: 批次模拟执行约0.01s/条，预算远超实际耗时防flaky
POLL_MAX_ATTEMPTS = 20
POLL_INTERVAL_SECONDS = 0.3


# ===========================================================================
# 造数与夹具
# ===========================================================================
def seed_cases() -> None:
    """
    造用例种子数据并入库（4条active）

    模块与末位奇偶分布:
        - 用户中心: TM-UC-0001（奇→failed）、TM-UC-0002（偶→passed）
        - 订单中心: TM-OD-0001（奇→failed）、TM-OD-0002（偶→passed）
    模拟执行结果可手算: 全量4条=2过2挂，用户中心2条=1过1挂。

    参数:
        无

    返回:
        无
    """
    seeds = [
        ("TM-UC-0001", "用户登录成功校验", "用户中心"),
        ("TM-UC-0002", "用户登录密码错误校验", "用户中心"),
        ("TM-OD-0001", "订单创建成功校验", "订单中心"),
        ("TM-OD-0002", "订单状态流转校验", "订单中心"),
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
                    description="执行触发API测试种子用例",
                )
            )


def _wait_batch_terminal(execution_id: str) -> Optional[dict]:
    """
    轮询批次状态至终态finished/failed（内部方法）

    防teardown竞态: 触发过后台线程的测试结束前必须等待批次进入
    终态，否则fixture的DatabaseSession.reset()可能与执行中的
    线程竞态（线程重建引擎后可能指向环境变量恢复后的库外文件）。

    参数:
        execution_id (str): 执行批次号

    返回:
        dict | None: 终态状态字典；超时仍未终态时返回最后一次
                     查询结果（由调用方决定是否断言）
    """
    status_data: Optional[dict] = None
    for _ in range(POLL_MAX_ATTEMPTS):
        status_data = CaseManager.get_execution_status(execution_id)
        if (
            status_data is not None
            and status_data["status"] in ("finished", "failed")
        ):
            return status_data
        time.sleep(POLL_INTERVAL_SECONDS)
    return status_data


@pytest.fixture
def trigger_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """
    执行触发测试数据库fixture（临时SQLite文件库）

    将数据库指向pytest临时目录独立库文件并完成建表+用例种子造数，
    前后重置DatabaseSession引擎单例（防Windows文件句柄残留与
    测试间引擎状态污染）；同时清除TM_EXECUTOR环境变量，保证
    未显式传executor的触发请求确定性走simulated执行器。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[None]: yield无数据，前后完成引擎重置
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv(
        "TM_DB_SQLITE_PATH", str(tmp_path / "trigger_api.db")
    )
    monkeypatch.delenv("TM_EXECUTOR", raising=False)
    DatabaseSession.reset()
    DatabaseSession.init_db()
    seed_cases()
    yield
    DatabaseSession.reset()


@pytest.fixture
def trigger_client(trigger_db: None) -> FlaskClient:
    """
    执行触发API测试客户端fixture（基于trigger_db临时库）

    参数:
        trigger_db (None): 依赖的数据库fixture（保证建库+种子+重置）

    返回:
        FlaskClient: Flask测试客户端（ teardown 时数据库
                     由trigger_db统一重置）
    """
    return create_app("test").test_client()


# ===========================================================================
# 执行触发API测试
# ===========================================================================
@allure.feature("执行触发API")
@allure.story("用例异步触发与批次状态查询")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestExecutionsTriggerApi:
    """POST /api/executions/trigger 与 GET /api/executions/<id>/status 测试"""

    # ------------------------------------------------------------------
    # 触发接口
    # ------------------------------------------------------------------
    def test_trigger_success(self, trigger_client: FlaskClient) -> None:
        """
        触发成功: 无请求体即全量回归，返回202、execution_id匹配
        RUN-前缀、status=pending、total_cases=种子4条（接口立即
        返回不等待执行完成）
        """
        response = trigger_client.post("/api/executions/trigger")
        data = response.get_json()

        assert response.status_code == 202, "触发受理应返回202 Accepted"
        payload = data["data"]
        assert re.match(r"^RUN-", payload["execution_id"]), (
            "批次号应为RUN-前缀格式"
        )
        assert payload["status"] == "pending", "初始状态应为pending"
        assert payload["total_cases"] == 4, "全量回归应命中全部4条种子用例"
        assert data["code"] == 202

        # 防teardown竞态: 等待后台线程跑完（不断言终态，链路断言在test 6）
        _wait_batch_terminal(payload["execution_id"])

    def test_trigger_with_filter(self, trigger_client: FlaskClient) -> None:
        """
        模块筛选触发: module=用户中心只执行该模块2条用例，
        total_cases正确，最终通过/失败计数与奇偶规则一致
        （TM-UC-0002偶数通过、TM-UC-0001奇数失败）
        """
        response = trigger_client.post(
            "/api/executions/trigger", json={"module": "用户中心"}
        )
        data = response.get_json()

        assert response.status_code == 202
        payload = data["data"]
        assert payload["total_cases"] == 2, "模块筛选应只命中用户中心2条"

        final = _wait_batch_terminal(payload["execution_id"])
        assert final is not None and final["status"] == "finished", (
            "筛选批次应正常完成"
        )
        assert final["passed"] == 1, "TM-UC-0002末位偶数应通过"
        assert final["failed"] == 1, "TM-UC-0001末位奇数应失败"

    def test_trigger_no_cases(self, trigger_client: FlaskClient) -> None:
        """
        无匹配用例: module=不存在模块筛选结果为空，返回400且
        message含"无符合条件的用例"
        """
        response = trigger_client.post(
            "/api/executions/trigger", json={"module": "不存在模块"}
        )
        data = response.get_json()

        assert response.status_code == 400, "空用例集应返回400"
        assert "无符合条件的用例" in data["message"]
        assert data["code"] == 400

    def test_trigger_invalid_executor(
        self, trigger_client: FlaskClient
    ) -> None:
        """
        非法执行器: executor=ghost不在合法值集合(simulated/pytest)，
        返回400且message含executor定位信息
        """
        response = trigger_client.post(
            "/api/executions/trigger", json={"executor": "ghost"}
        )
        data = response.get_json()

        assert response.status_code == 400, "非法执行器应返回400"
        assert "executor" in data["message"]

    def test_status_not_found(self, trigger_client: FlaskClient) -> None:
        """
        状态查询不存在批次: 返回404统一错误格式
        """
        response = trigger_client.get(
            "/api/executions/RUN-20990101-000000-none0/status"
        )
        data = response.get_json()

        assert response.status_code == 404, "不存在批次应返回404"
        assert data["code"] == 404
        assert data["data"] == {"execution_id": "RUN-20990101-000000-none0"}

    # ------------------------------------------------------------------
    # 异步执行链路
    # ------------------------------------------------------------------
    def test_async_runs_to_finish(self, trigger_client: FlaskClient) -> None:
        """
        真实后台线程链路: trigger立即202返回，轮询批次状态至
        finished（最多10次×0.3s），通过/失败计数与种子奇偶规则
        手算一致（4条=2过2挂），defect_statistics有该批次记录
        """
        response = trigger_client.post("/api/executions/trigger")
        data = response.get_json()
        assert response.status_code == 202
        execution_id = data["data"]["execution_id"]

        # 轮询至终态（模拟执行0.01s/条必然完成，预算充裕防flaky）
        final: Optional[dict] = None
        for _ in range(10):
            time.sleep(POLL_INTERVAL_SECONDS)
            status_response = trigger_client.get(
                f"/api/executions/{execution_id}/status"
            )
            assert status_response.status_code == 200
            final = status_response.get_json()["data"]
            if final["status"] in ("finished", "failed"):
                break

        assert final is not None, "状态查询应有返回"
        assert final["status"] == "finished", "批次最终应为finished"
        assert final["total_cases"] == 4
        assert final["passed"] == 2, "末位偶数用例(TM-UC-0002/TM-OD-0002)应通过"
        assert final["failed"] == 2, "末位奇数用例(TM-UC-0001/TM-OD-0001)应失败"
        assert final["error"] == 0
        assert final["skipped"] == 0
        assert final["pass_rate"] == 0.5
        assert final["started_at"] is not None, "running后started_at应写入"
        assert final["finished_at"] is not None, "finished后finished_at应写入"

        # 汇总表落库校验: finish_execution已聚合写入defect_statistics
        with DatabaseSession.session_scope() as session:
            statistic = (
                session.query(models.DefectStatistic)
                .filter_by(execution_id=execution_id)
                .first()
            )
        assert statistic is not None, "defect_statistics应有该批次记录"
        assert statistic.passed == 2
        assert statistic.failed == 2

    def test_execute_batch_sync(
        self, trigger_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        同步编排验证: 直调_execute_batch_async不走真实线程，
        批次状态机pending→running→finished转换正确（run_one执行
        期间批次状态为running），明细表行数=用例数，冗余统计正确
        """
        result = CaseManager.start_execution(
            trigger="web", executor_name="web", case_type="api"
        )
        execution_id = result["execution_id"]

        # start后批次行为pending且状态可查
        assert result["status"] == "pending"
        pending_data = CaseManager.get_execution_status(execution_id)
        assert pending_data is not None and pending_data["status"] == "pending"
        assert pending_data["total_cases"] == 4

        # 探针执行器: run_one执行期间记录批次状态（验证running态）
        observed_statuses: list = []

        class _ProbeExecutor(BaseExecutor):
            """探针执行器: 观测执行期间批次状态，固定返回通过"""

            def run_one(self, case: dict) -> ExecutionResult:
                status_data = CaseManager.get_execution_status(execution_id)
                observed_statuses.append(
                    status_data["status"] if status_data else "missing"
                )
                return ExecutionResult(
                    result="passed", error_message=None, duration=0.001
                )

        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _ProbeExecutor(),
        )
        # 同步执行（同步返回即执行完成，不启动线程）
        CaseManager._execute_batch_async(execution_id, result["cases"], None)

        # 状态机: pending(start时) → running(用例执行期间) → finished(完成后)
        final = CaseManager.get_execution_status(execution_id)
        assert final is not None and final["status"] == "finished"
        assert observed_statuses == ["running"] * 4, (
            "run_one执行期间批次状态应全部为running"
        )

        # 明细表行数=用例数 + 批次行冗余统计
        with DatabaseSession.session_scope() as session:
            detail_count = (
                session.query(models.TestExecution)
                .filter_by(execution_id=execution_id)
                .count()
            )
        assert detail_count == 4, "明细表行数应等于用例数"
        assert final["passed"] == 4
        assert final["pass_rate"] == 1.0

    def test_batch_failed_status(
        self, trigger_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        执行器异常兜底: 注入run_one必抛异常的假执行器同步跑，
        批次最终status=failed且error_message非空（含异常信息），
        调用不抛异常（线程内绝不向调用方抛出）
        """
        result = CaseManager.start_execution(
            trigger="web", executor_name="web", case_type="api"
        )
        execution_id = result["execution_id"]

        class _BoomExecutor(BaseExecutor):
            """必抛异常的假执行器: 验证批次级异常兜底"""

            def run_one(self, case: dict) -> ExecutionResult:
                raise RuntimeError("执行器内部崩溃")

        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _BoomExecutor(),
        )
        # 不抛异常即通过（线程内异常已内部消化）
        CaseManager._execute_batch_async(execution_id, result["cases"], None)

        final = CaseManager.get_execution_status(execution_id)
        assert final is not None, "failed批次行应存在"
        assert final["status"] == "failed", "异常批次最终应为failed"
        assert final["error_message"], "failed批次error_message应非空"
        assert "执行器内部崩溃" in final["error_message"]

    # ------------------------------------------------------------------
    # 执行器抽象层
    # ------------------------------------------------------------------
    def test_executor_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        执行器工厂: 显式kind返回正确类型（均为BaseExecutor子类），
        非法值抛ValueError；未传kind时读TM_EXECUTOR环境变量，
        环境变量未配置默认simulated
        """
        # 显式kind
        simulated = get_executor("simulated")
        assert isinstance(simulated, SimulatedExecutor)
        assert isinstance(simulated, BaseExecutor)
        pytest_runner = get_executor("pytest")
        assert isinstance(pytest_runner, PytestRunner)
        assert isinstance(pytest_runner, BaseExecutor)

        # 非法kind
        with pytest.raises(ValueError, match="非法执行器类型"):
            get_executor("xxx")

        # 环境变量默认链: 未配置→simulated，配置pytest→PytestRunner
        monkeypatch.delenv("TM_EXECUTOR", raising=False)
        assert isinstance(get_executor(), SimulatedExecutor)
        monkeypatch.setenv("TM_EXECUTOR", "pytest")
        assert isinstance(get_executor(), PytestRunner)

    def test_pytest_runner_exit_code(self) -> None:
        """
        pytest执行器退出码解析: mock子进程returncode
        0→passed（error_message为None）/ 1→failed（error_message非空）/
        2→error（error_message非空且含stderr内容）；
        附命令拼装结构断言（py -m pytest path -q --tb=short）
        """
        runner = PytestRunner()
        case = {"case_id": "TM-UC-0002"}

        # 命令拼装结构（骨架契约）
        command = runner.build_command(case)
        assert command == [
            "py", "-m", "pytest", "TM-UC-0002", "-q", "--tb=short",
        ]

        def _mock_completed(returncode: int, stderr: str = "") -> MagicMock:
            """构造mock子进程完成对象（returncode/stdout/stderr）"""
            completed = MagicMock()
            completed.returncode = returncode
            completed.stdout = ""
            completed.stderr = stderr
            return completed

        # 退出码0 → passed
        with patch(
            "src.core.executors.subprocess.run",
            return_value=_mock_completed(0),
        ):
            exec_result = runner.run_one(case)
        assert exec_result.result == "passed"
        assert exec_result.error_message is None

        # 退出码1 → failed（输出为空时兜底退出码文案，保证非空）
        with patch(
            "src.core.executors.subprocess.run",
            return_value=_mock_completed(1),
        ):
            exec_result = runner.run_one(case)
        assert exec_result.result == "failed"
        assert exec_result.error_message

        # 退出码2 → error（stderr截断2000字存入error_message）
        with patch(
            "src.core.executors.subprocess.run",
            return_value=_mock_completed(2, stderr="usage error: bad option"),
        ):
            exec_result = runner.run_one(case)
        assert exec_result.result == "error"
        assert exec_result.error_message
        assert "usage error" in exec_result.error_message
