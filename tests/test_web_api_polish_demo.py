"""
TestMatrix Day29: API打磨回归测试
（请求日志全链路化 + 关键业务埋点 + trigger case_type枚举校验）

测试覆盖（共10条）:
    请求钩子改造:
        1. test_after_request_security_headers_present 安全头回归
        2. test_after_request_logs_response_with_status 响应日志含
           状态码与耗时
        3. test_request_log_pair "请求:"/"响应:"配对日志
    trigger枚举校验:
        4. test_trigger_invalid_case_type_400 非法case_type返回400
        5. test_trigger_valid_case_type_chip_400_no_case chip合法但
           无chip用例，走到筛选阶段返回"无符合条件的用例"
    关键业务埋点:
        6. test_trigger_logs_acceptance 批次受理埋点含execution_id
        7. test_create_case_logs_creation 创建用例埋点
        8. test_create_case_201_response_unchanged 创建响应结构回归
    零回归:
        9. test_polish_does_not_break_crud CRUD最小回归
        10. test_polish_does_not_break_reports_summary 全链路回归

测试基建:
    临时SQLite文件库（tmp_path + 前后DatabaseSession.reset()防
    Windows文件锁）+ Flask test client端到端验证；种子4条active
    用例（TM-UC-0001/0002用户中心、TM-OD-0001/0002订单中心，
    末位奇偶各半，模拟执行结果可手算: 全量=2过2挂）。

日志断言说明:
    loguru不走stdlib logging，pytest的caplog无法捕获；统一用
    loguru.logger.add临时sink到list、用例结束logger.remove的
    标准做法，sink以format="{message}"只取消息文本。
"""

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import allure
import pytest
from flask.testing import FlaskClient

from src.common.logger import LogManager
from src.core import event_bus
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

# 模块级logger: 与应用内日志同一个loguru全局实例（临时sink挂在此处）
logger = LogManager.get_logger()

# 轮询参数: 模拟执行约0.01s/条，预算远超实际耗时防flaky
POLL_MAX_ATTEMPTS = 20
POLL_INTERVAL_SECONDS = 0.3


# ===========================================================================
# 造数、夹具与日志捕获工具
# ===========================================================================
def seed_cases() -> None:
    """
    造用例种子数据并入库（4条active）

    模块与末位奇偶分布:
        - 用户中心: TM-UC-0001（末位奇→failed）、TM-UC-0002（偶→passed）
        - 订单中心: TM-OD-0001（末位奇→failed）、TM-OD-0002（偶→passed）
    全量4条模拟执行结果可手算: 2过2挂。

    参数:
        无

    返回:
        None
    """
    seeds = [
        ("TM-UC-0001", "用户登录成功校验", "用户中心"),
        ("TM-UC-0002", "用户登录密码错误校验", "用户中心"),
        ("TM-OD-0001", "订单创建成功校验", "订单中心"),
        ("TM-OD-0002", "订单状态流转校验", "订单中心"),
    ]
    # 经session_scope事务写入（失败自动回滚，不残留半成品）
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
                    description="API打磨测试种子用例",
                )
            )


@contextmanager
def capture_logs() -> Iterator[list[str]]:
    """
    临时挂载loguru sink收集INFO及以上日志（上下文管理器）

    进入时add一个同步sink（enqueue默认False，消息即时写入list），
    退出时logger.remove精确摘除该sink，保证各用例日志独立不串。

    参数:
        无

    返回:
        Iterator[list[str]]: 收集消息文本的列表（format="{message}"
        只取消息正文，不含时间/级别等前缀）

    异常:
        无（finally保证sink必然被remove）
    """
    captured: list[str] = []
    # str(message)会按该sink自身的format渲染，即纯消息文本
    sink_id = logger.add(
        lambda message: captured.append(str(message)),
        level="INFO",
        format="{message}",
    )
    try:
        yield captured
    finally:
        # 只摘除本次sink，不影响LogManager既有的控制台/文件handler
        logger.remove(sink_id)


def wait_batch_terminal(client: FlaskClient, execution_id: str) -> dict:
    """
    走状态API轮询批次至finished/failed终态（内部方法）

    防teardown竞态（7.12口径）: 触发过后台线程的用例结束前必须
    确认批次终态，否则fixture的DatabaseSession.reset()可能与仍在
    执行的线程竞态；轮询走真实HTTP接口而非直调核心层。

    参数:
        client (FlaskClient): Flask测试客户端
        execution_id (str): 执行批次号

    返回:
        dict: 终态批次状态字典

    异常:
        AssertionError: 超过轮询预算仍未终态时抛出
    """
    for _ in range(POLL_MAX_ATTEMPTS):
        response = client.get(f"/api/executions/{execution_id}/status")
        assert response.status_code == 200, "批次状态查询应返回200"
        status_data = response.get_json()["data"]
        if status_data["status"] in ("finished", "failed"):
            return status_data
        time.sleep(POLL_INTERVAL_SECONDS)
    raise AssertionError(f"批次 {execution_id} 未在预期时间内进入终态")


@pytest.fixture
def polish_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
    """
    API打磨测试客户端fixture（function级临时SQLite文件库）

    将数据库指向pytest临时目录独立库文件并完成建表+4条种子造数；
    清除TM_EXECUTOR保证未显式传executor时确定性走simulated。
    teardown依次复位引擎（防Windows文件锁）、复位事件通道（防
    daemon线程late publish串下条测试）、删除临时库文件。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[FlaskClient]: yield Flask测试客户端
    """
    db_file = tmp_path / "polish_api.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    monkeypatch.delenv("TM_EXECUTOR", raising=False)
    DatabaseSession.reset()
    DatabaseSession.init_db()
    seed_cases()
    yield create_app("test").test_client()
    DatabaseSession.reset()
    event_bus.reset_channels()
    db_file.unlink(missing_ok=True)


def trigger_full_batch(client: FlaskClient) -> dict:
    """
    无请求体触发全量回归并返回受理载荷（内部方法）

    参数:
        client (FlaskClient): Flask测试客户端

    返回:
        dict: 202响应的data字段（execution_id/status/total_cases）

    异常:
        AssertionError: 未返回202时抛出
    """
    response = client.post("/api/executions/trigger")
    assert response.status_code == 202, "全量触发应返回202"
    return response.get_json()["data"]


# ===========================================================================
# Day29 API打磨回归测试
# ===========================================================================
@allure.feature("API打磨")
@allure.story("请求日志/枚举校验/业务埋点")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestWebApiPolish:
    """Day29 请求钩子改造、case_type枚举校验与关键埋点回归测试"""

    # ------------------------------------------------------------------
    # 请求钩子改造
    # ------------------------------------------------------------------
    def test_after_request_security_headers_present(
        self, polish_client: FlaskClient
    ) -> None:
        """安全头回归: 响应携带X-Content-Type-Options与X-Frame-Options"""
        response = polish_client.get("/health")

        assert response.status_code == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"

    def test_after_request_logs_response_with_status(
        self, polish_client: FlaskClient
    ) -> None:
        """响应日志含状态码200与耗时ms（after_request配对日志回归）"""
        with capture_logs() as captured:
            polish_client.get("/health")

        log_text = "\n".join(captured)
        assert "200" in log_text, "响应日志应携带200状态码"
        assert "ms" in log_text, "响应日志应携带毫秒级耗时"
        assert "响应:" in log_text

    def test_request_log_pair(self, polish_client: FlaskClient) -> None:
        """配对日志: 同一请求既有"请求:"入口又有"响应:"出口"""
        with capture_logs() as captured:
            polish_client.get("/health")

        log_text = "\n".join(captured)
        assert "请求:" in log_text, "应记录请求入口日志"
        assert "响应:" in log_text, "应记录响应出口日志"

    # ------------------------------------------------------------------
    # trigger case_type枚举校验
    # ------------------------------------------------------------------
    def test_trigger_invalid_case_type_400(
        self, polish_client: FlaskClient
    ) -> None:
        """非法case_type: 传http返回400且message定位case_type非法"""
        response = polish_client.post(
            "/api/executions/trigger", json={"case_type": "http"}
        )
        data = response.get_json()

        assert response.status_code == 400
        assert data["code"] == 400
        assert "case_type非法" in data["message"]

    def test_trigger_valid_case_type_chip_400_no_case(
        self, polish_client: FlaskClient
    ) -> None:
        """
        chip合法值应通过枚举校验: 种子均为api用例，case_type=chip
        筛选结果为空，返回400"无符合条件的用例"，而非"case_type非法"
        """
        response = polish_client.post(
            "/api/executions/trigger", json={"case_type": "chip"}
        )
        data = response.get_json()

        assert response.status_code == 400
        assert "无符合条件的用例" in data["message"], (
            "枚举校验通过后应走到用例筛选阶段"
        )
        assert "case_type非法" not in data["message"]

    # ------------------------------------------------------------------
    # 关键业务埋点
    # ------------------------------------------------------------------
    def test_trigger_logs_acceptance(
        self, polish_client: FlaskClient
    ) -> None:
        """受理埋点: 全量触发后日志含"批次已受理"与execution_id"""
        with capture_logs() as captured:
            payload = trigger_full_batch(polish_client)
            # 等终态防teardown竞态（日志断言不依赖执行完成）
            wait_batch_terminal(polish_client, payload["execution_id"])

        log_text = "\n".join(captured)
        assert "批次已受理" in log_text
        assert payload["execution_id"] in log_text

    def test_create_case_logs_creation(
        self, polish_client: FlaskClient
    ) -> None:
        """创建埋点: 新建用例后日志含"用例已创建"与业务编号"""
        body = {
            "case_id": "TM-POLISH-0001",
            "name": "API打磨创建埋点校验",
            "module": "平台治理",
            "priority": "P1",
            "case_type": "api",
        }
        with capture_logs() as captured:
            response = polish_client.post("/api/cases/", json=body)

        assert response.status_code == 201
        log_text = "\n".join(captured)
        assert "用例已创建" in log_text
        assert "TM-POLISH-0001" in log_text

    def test_create_case_201_response_unchanged(
        self, polish_client: FlaskClient
    ) -> None:
        """响应结构回归: 创建返回201且data.case_id与请求一致"""
        body = {
            "case_id": "TM-POLISH-0002",
            "name": "创建响应结构回归校验",
            "module": "平台治理",
            "priority": "P2",
        }
        response = polish_client.post("/api/cases/", json=body)
        data = response.get_json()

        assert response.status_code == 201
        assert data["code"] == 201
        assert data["data"]["case_id"] == "TM-POLISH-0002"

    # ------------------------------------------------------------------
    # 零回归
    # ------------------------------------------------------------------
    def test_polish_does_not_break_crud(
        self, polish_client: FlaskClient
    ) -> None:
        """CRUD最小回归: POST→GET→PUT→DELETE全链路状态正确"""
        case_id = "TM-CRUD-POLISH-0001"
        # 创建
        create_response = polish_client.post(
            "/api/cases/",
            json={
                "case_id": case_id,
                "name": "CRUD回归原始名称",
                "module": "平台治理",
                "priority": "P1",
            },
        )
        assert create_response.status_code == 201

        # 查询详情
        get_response = polish_client.get(f"/api/cases/{case_id}")
        assert get_response.status_code == 200
        assert get_response.get_json()["data"]["case_id"] == case_id

        # 更新（只更新name/priority，其余字段不变）
        put_response = polish_client.put(
            f"/api/cases/{case_id}",
            json={"name": "CRUD回归更新名称", "priority": "P0"},
        )
        put_data = put_response.get_json()["data"]
        assert put_response.status_code == 200
        assert put_data["name"] == "CRUD回归更新名称"
        assert put_data["priority"] == "P0"

        # 删除
        delete_response = polish_client.delete(f"/api/cases/{case_id}")
        assert delete_response.status_code == 204

        # 删除后再查应404
        missing_response = polish_client.get(f"/api/cases/{case_id}")
        assert missing_response.status_code == 404

    def test_polish_does_not_break_reports_summary(
        self, polish_client: FlaskClient
    ) -> None:
        """全链路回归: 全量触发终态后summary为4执行/2过/2挂"""
        payload = trigger_full_batch(polish_client)
        final_status = wait_batch_terminal(
            polish_client, payload["execution_id"]
        )
        assert final_status["status"] == "finished"

        summary_response = polish_client.get("/api/reports/summary")
        summary = summary_response.get_json()["data"]

        assert summary_response.status_code == 200
        assert summary["total_executed"] == 4
        assert summary["passed"] == 2, "末位偶数用例应通过"
        assert summary["failed"] == 2, "末位奇数用例应失败"
