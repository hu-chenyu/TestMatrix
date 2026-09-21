"""
TestMatrix Day32: Redis任务队列测试
（队列客户端封装 + worker消费集成 + enqueue失败fallback容错）

测试覆盖（10条，三组）:
    A组 队列客户端单测（5条，直接测src.core.task_queue）:
        1. test_enqueue_then_dequeue_roundtrip
           fake后端入队/出队往返，中文与嵌套结构原样还原
        2. test_task_status_pending_running_finished
           任务状态hash的pending→running→finished流转
        3. test_empty_queue_dequeue_timeout
           空队列BRPOP有限超时返回None，不永久阻塞
        4. test_disabled_queue_passthrough
           TM_TASK_QUEUE_ENABLED未设时enqueue False/dequeue None
        5. test_redis_down_enqueue_returns_false
           后端抛ConnectionError时enqueue不抛异常返回False
    B组 队列模式集成（3条，Flask test client + worker）:
        6. test_trigger_via_queue_executes_to_finished
           trigger入队→worker消费→轮询finished→详情4条2过2挂
        7. test_queue_mode_response_unchanged
           队列模式202响应与裸线程模式逐字段一致
        8. test_sse_events_still_work_in_queue_mode
           worker执行时仍publish，finished后/events能收到三类帧
    C组 边界与容错（2条）:
        9. test_enqueue_failure_falls_back_to_thread
           enqueue恒False时trigger仍202且fallback裸线程执行完成
        10. test_worker_stop_then_restart_consumes_pending
            停worker后入队任务积压pending，重启worker后被消费

测试铁律:
    - 全程fake://内存后端（fakeredis），零真实Redis进程依赖
    - 轮询批次终态走GET /api/executions/<id>/status，禁固定sleep
    - worker线程由fixture统一start/stop，任何用例结束不得残留
    - payload严格对齐_execute_batch_async真实签名
      （execution_id/cases/executor_kind）
    - 全程loguru，无print
"""

import time
from pathlib import Path
from typing import Iterator, Optional

import allure
import pytest
import redis
from flask.testing import FlaskClient
from unittest.mock import patch

from src.core import event_bus
from src.core.cache import cache_client
from src.core.task_queue import (
    TaskQueueClient,
    start_worker,
    stop_worker,
    task_queue_client,
)
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

# 轮询参数: 模拟执行约0.01s/条，worker重启后最迟一个BRPOP超时
# 周期（1s）取到任务，预算远超实际耗时防flaky
POLL_MAX_ATTEMPTS = 20
POLL_INTERVAL = 0.3


# ===========================================================================
# 造数与夹具
# ===========================================================================
def seed_cases() -> None:
    """
    造用例种子数据并入库（4条active）

    模块与末位奇偶分布:
        - 用户中心: TM-UC-0001（奇→failed）、TM-UC-0002（偶→passed）
        - 订单中心: TM-OD-0001（奇→failed）、TM-OD-0002（偶→passed）

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
                    description="任务队列测试种子用例",
                )
            )


@pytest.fixture
def queue_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
    """
    队列启用场景fixture（function级，fake队列+worker+临时SQLite库）

    环境准备:
        - TM_TASK_QUEUE_ENABLED=true / TM_TASK_WORKER_ENABLED=true
        - TM_REDIS_URL=fake://0 触发fakeredis内存实例
        - 缓存/队列单例均reset_backend，保证每例全新fake实例
        - 临时SQLite库+init_db+4条种子active用例
        - start_worker启动单worker消费线程（create_app内的
          start_worker幂等返回同一线程）

    teardown（顺序很重要）:
        - stop_worker停消费线程（worker不得跨用例残留）
        - 队列/缓存后端reset丢弃fake数据
        - DatabaseSession.reset + event_bus.reset_channels
        - 临时库文件unlink

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[FlaskClient]: yield Flask测试客户端
    """
    db_path = tmp_path / "task_queue.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    monkeypatch.delenv("TM_EXECUTOR", raising=False)
    # 队列+worker双开关，复用Day31的fake://内存Redis约定
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
    monkeypatch.setenv("TM_TASK_WORKER_ENABLED", "true")
    monkeypatch.setenv("TM_REDIS_URL", "fake://0")
    # 队列与缓存是两个独立单例，全部复位防fake数据跨用例串扰
    task_queue_client.reset_backend()
    cache_client.reset_backend()

    DatabaseSession.reset()
    DatabaseSession.init_db()
    seed_cases()

    # 先启动worker再建app（create_app内start_worker幂等）
    worker_thread = start_worker()
    assert worker_thread is not None and worker_thread.is_alive(), (
        "队列开关开启时worker应成功启动"
    )
    client = create_app("test").test_client()
    yield client

    # teardown: 先停消费线程，再复位后端/引擎/通道，最后删库文件
    stop_worker()
    task_queue_client.reset_backend()
    cache_client.reset_backend()
    DatabaseSession.reset()
    event_bus.reset_channels()
    db_path.unlink(missing_ok=True)


def _wait_batch_terminal(
    client: FlaskClient, execution_id: str
) -> dict:
    """
    轮询批次状态至终态finished/failed（内部方法，走真实HTTP）

    参数:
        client (FlaskClient): Flask测试客户端
        execution_id (str): 执行批次号

    返回:
        dict: 终态状态字典

    异常:
        AssertionError: 轮询预算内未达终态时pytest.fail
    """
    status_data: Optional[dict] = None
    for _ in range(POLL_MAX_ATTEMPTS):
        response = client.get(f"/api/executions/{execution_id}/status")
        assert response.status_code == 200, "轮询期间状态查询应始终200"
        status_data = response.get_json()["data"]
        if status_data["status"] in ("finished", "failed"):
            return status_data
        time.sleep(POLL_INTERVAL)
    pytest.fail(
        f"批次在轮询预算内未达终态: {execution_id}, "
        f"最后状态: {status_data['status'] if status_data else '无响应'}"
    )
    raise AssertionError("不可达: pytest.fail后代码不会执行到此处")


def _trigger(client: FlaskClient) -> str:
    """
    全量触发执行并返回批次号（内部方法）

    参数:
        client (FlaskClient): Flask测试客户端

    返回:
        str: 执行批次号

    异常:
        AssertionError: 触发未返回202时失败
    """
    response = client.post("/api/executions/trigger")
    assert response.status_code == 202, "触发应返回202受理"
    return response.get_json()["data"]["execution_id"]


# ===========================================================================
# A组: 队列客户端单测（直接测src.core.task_queue，不走HTTP）
# ===========================================================================
@allure.feature("Redis任务队列")
@allure.story("队列客户端单元行为")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestTaskQueueClientUnit:
    """入队/出队/状态hash/超时/关闭降级/故障返回False"""

    def test_enqueue_then_dequeue_roundtrip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        入队出队往返: fake后端LPUSH/BRPOP取回原payload，
        中文用例名与嵌套字典列表结构原样还原
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        client = TaskQueueClient()

        payload = {
            "execution_id": "RUN-UNIT-0001",
            "executor_kind": "simulated",
            "cases": [
                {
                    "case_id": "TM-UC-0001",
                    "name": "用户登录成功校验",
                    "module": "用户中心",
                    "meta": {"优先级": "P1", "标签": ["冒烟", "中文"]},
                }
            ],
        }
        assert client.enqueue("RUN-UNIT-0001", payload) is True
        assert client.dequeue(timeout=0.5) == payload, (
            "出队payload应与入队内容完全一致（含中文/嵌套结构）"
        )

    def test_task_status_pending_running_finished(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        状态hash流转: pending(queued_at)→running(started_at)→
        finished(finished_at)，HGETALL各阶段字段齐全且终态覆盖
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        client = TaskQueueClient()
        execution_id = "RUN-UNIT-STATUS"

        # 阶段1: 入队pending
        client.set_status(execution_id, "pending", queued_at="t0")
        pending_status = client.get_status(execution_id)
        assert pending_status["status"] == "pending"
        assert pending_status["queued_at"] == "t0"

        # 阶段2: worker取到置running
        client.set_status(execution_id, "running", started_at="t1")
        running_status = client.get_status(execution_id)
        assert running_status["status"] == "running"
        assert running_status["started_at"] == "t1"

        # 阶段3: 完成置finished（HSET覆盖status，时间字段累积保留）
        client.set_status(execution_id, "finished", finished_at="t2")
        finished_status = client.get_status(execution_id)
        assert finished_status["status"] == "finished"
        assert finished_status["finished_at"] == "t2"

    def test_empty_queue_dequeue_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        空队列超时: 全新fake实例上dequeue(timeout=0.1)在超时内
        返回None，不永久阻塞（worker能借此周期检查stop_event）
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        # 独立队列key，避免任何残留消息干扰空队列语义
        monkeypatch.setenv("TM_TASK_QUEUE_KEY", "tm:queue:empty-only")
        client = TaskQueueClient()

        start = time.monotonic()
        assert client.dequeue(timeout=0.1) is None
        elapsed = time.monotonic() - start
        # fakeredis条件变量唤醒相对设定超时存在毫秒级提前，下界
        # 放宽到0.05s: 断言意图是"经历了超时等待而非立即返回"，
        # 不把第三方调度抖动误判为flaky；上界保证不会长时间阻塞
        assert elapsed >= 0.05, (
            f"应经历超时等待而非立即返回，实际仅{elapsed:.3f}s"
        )
        assert elapsed < 1.0, "超时后应及时返回，不得长时间阻塞"

    def test_disabled_queue_passthrough(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        默认关闭全no-op: 开关未设置时enqueue返回False（调用方
        据此fallback裸线程）、dequeue返回None、状态读写静默
        """
        monkeypatch.delenv("TM_TASK_QUEUE_ENABLED", raising=False)
        monkeypatch.delenv("TM_TASK_WORKER_ENABLED", raising=False)
        client = TaskQueueClient()

        assert client.enabled is False
        assert client.worker_enabled is False, "worker开关缺省应跟随队列关闭"
        assert client.enqueue("RUN-X", {"execution_id": "RUN-X"}) is False
        assert client.dequeue(timeout=0.1) is None
        # 状态hash在未启用时静默no-op，不抛异常
        client.set_status("RUN-X", "pending")
        assert client.get_status("RUN-X") is None

    def test_redis_down_enqueue_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        故障降级: 后端命令全部抛ConnectionError时enqueue不抛异常、
        返回False（trigger据此fallback裸线程，任务不丢、接口不500）
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        client = TaskQueueClient()

        class _DownRedis:
            """模拟Redis不可达: 队列/hash命令全抛ConnectionError"""

            def lpush(self, key: str, *values: str) -> None:
                """入队命令抛连接异常"""
                raise redis.ConnectionError("模拟宕机: lpush失败")

            def brpop(self, key: str, timeout: float = 0):
                """出队命令抛连接异常"""
                raise redis.ConnectionError("模拟宕机: brpop失败")

            def hset(self, name: str, mapping: Optional[dict] = None,
                     **kwargs) -> None:
                """hash写入命令抛连接异常"""
                raise redis.ConnectionError("模拟宕机: hset失败")

            def hgetall(self, name: str) -> None:
                """hash读取命令抛连接异常"""
                raise redis.ConnectionError("模拟宕机: hgetall失败")

            def close(self) -> None:
                """关闭无需释放资源"""
                return None

        client._backend = _DownRedis()
        assert client.enqueue("RUN-DOWN", {"execution_id": "RUN-DOWN"}) is False
        assert client.dequeue(timeout=0.1) is None, "出队异常也应返回None"
        client.set_status("RUN-DOWN", "pending")  # 静默no-op不抛异常


# ===========================================================================
# B组: 队列模式集成（Flask test client + 真实worker消费）
# ===========================================================================
@allure.feature("Redis任务队列")
@allure.story("队列模式端到端集成")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestQueueModeIntegration:
    """trigger入队→worker消费→执行/响应/SSE全链路"""

    def test_trigger_via_queue_executes_to_finished(
        self, queue_env: FlaskClient
    ) -> None:
        """
        队列执行闭环: trigger返回202后任务由worker（非请求线程）
        消费，轮询至finished，详情4条明细且2 passed 2 failed，
        执行编排与裸线程模式结果一致
        """
        execution_id = _trigger(queue_env)
        final_status = _wait_batch_terminal(queue_env, execution_id)

        assert final_status["status"] == "finished", "worker应把批次执行到finished"
        assert final_status["total_cases"] == 4
        assert final_status["passed"] == 2, "末位偶数2条应通过"
        assert final_status["failed"] == 2, "末位奇数2条应失败"

        detail_response = queue_env.get(
            f"/api/executions/{execution_id}"
        )
        assert detail_response.status_code == 200
        items = detail_response.get_json()["data"]["items"]
        assert len(items) == 4, "worker执行应落全部4条明细"
        # 调度层hash也应记录finished终态
        task_status = task_queue_client.get_status(execution_id)
        assert task_status is not None
        assert task_status["status"] == "finished", (
            "worker收尾应把任务hash置为finished"
        )

    def test_queue_mode_response_unchanged(
        self, queue_env: FlaskClient
    ) -> None:
        """
        响应格式不变: 队列模式trigger响应code=202、
        message="执行批次已受理，后台执行中"、data含
        execution_id/status=pending/total_cases=4，
        与裸线程模式契约逐字段一致
        """
        response = queue_env.post("/api/executions/trigger")
        assert response.status_code == 202
        body = response.get_json()

        assert body["code"] == 202
        assert body["message"] == "执行批次已受理，后台执行中"
        data = body["data"]
        assert set(data.keys()) == {"execution_id", "status", "total_cases"}
        assert isinstance(data["execution_id"], str) and data["execution_id"]
        assert data["status"] == "pending"
        assert data["total_cases"] == 4, "种子4条active"

        # 等终态，避免后台批次跨fixture teardown（7.12口径）
        _wait_batch_terminal(queue_env, data["execution_id"])

    def test_sse_events_still_work_in_queue_mode(
        self, queue_env: FlaskClient
    ) -> None:
        """
        SSE零改动: worker执行时仍向event_bus publish；批次finished
        后GET /events收到batch_start/case_finished/batch_finished
        三类帧（finished走DB重建分支，通道已清理也能完整回放）
        """
        execution_id = _trigger(queue_env)
        _wait_batch_terminal(queue_env, execution_id)

        response = queue_env.get(
            f"/api/executions/{execution_id}/events",
            buffered=False,
        )
        assert response.status_code == 200
        assert response.mimetype == "text/event-stream"
        # 终态批次流为有限帧，get_data读完后流自动关闭
        raw_frames = response.get_data(as_text=True)

        assert "event: batch_start" in raw_frames, "应含批次开始帧"
        assert "event: case_finished" in raw_frames, "应含单用例完成帧"
        assert raw_frames.count("event: case_finished") == 4, (
            "4条用例应有4条case_finished帧"
        )
        assert "event: batch_finished" in raw_frames, "应含批次终态帧"


# ===========================================================================
# C组: 边界与容错
# ===========================================================================
@allure.feature("Redis任务队列")
@allure.story("失败回退与worker重启")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestQueueFallbackAndRestart:
    """enqueue失败fallback裸线程 + worker停启消费积压任务"""

    def test_enqueue_failure_falls_back_to_thread(
        self, queue_env: FlaskClient
    ) -> None:
        """
        入队失败不丢任务: monkeypatch让enqueue恒返回False（模拟
        Redis故障），trigger仍202，路由fallback裸线程执行，
        批次照样轮询到finished（fallback路径与Day24行为一致）
        """
        with patch.object(
            task_queue_client, "enqueue", return_value=False
        ) as enqueue_spy:
            response = queue_env.post("/api/executions/trigger")
            assert response.status_code == 202, (
                "enqueue失败时接口必须仍202，绝不允许500"
            )
            execution_id = response.get_json()["data"]["execution_id"]
            assert enqueue_spy.called, "队列开启时应先尝试入队"

        # fallback裸线程执行，终态与数据完整性与正常链路一致
        final_status = _wait_batch_terminal(queue_env, execution_id)
        assert final_status["status"] == "finished"
        assert final_status["total_cases"] == 4

    def test_worker_stop_then_restart_consumes_pending(
        self, queue_env: FlaskClient
    ) -> None:
        """
        停worker后任务积压、重启后被消费:
        stop_worker后trigger（入队成功但无消费者），/status
        确定仍pending；start_worker重启后轮询至finished，
        证明积压任务未丢失、重启worker可恢复消费
        """
        # 1. 停掉消费线程（队列客户端仍可正常入队）
        stop_worker()

        # 2. trigger: enqueue成功，任务积压在队列中
        response = queue_env.post("/api/executions/trigger")
        assert response.status_code == 202
        execution_id = response.get_json()["data"]["execution_id"]

        # 3. 无消费者时批次确定停留在pending（非轮询终态，只查一次）
        pending_response = queue_env.get(
            f"/api/executions/{execution_id}/status"
        )
        assert pending_response.status_code == 200
        assert pending_response.get_json()["data"]["status"] == "pending", (
            "worker停止时任务只入队不执行，批次应仍pending"
        )

        # 4. 重启worker，积压任务被消费至终态
        restarted_thread = start_worker()
        assert restarted_thread is not None and restarted_thread.is_alive()
        final_status = _wait_batch_terminal(queue_env, execution_id)
        assert final_status["status"] == "finished", (
            "worker重启后应消费积压任务并执行到finished"
        )
        assert final_status["passed"] == 2
        assert final_status["failed"] == 2
