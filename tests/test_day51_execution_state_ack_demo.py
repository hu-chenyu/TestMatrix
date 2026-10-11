"""
TestMatrix Day51: 执行状态回写与队列 ACK/requeue 健壮性验收用例（8 条）

Day51 两个子任务在这里逐一落到可执行断言：
    任务一 执行状态回写（状态机 + 批次进度）
        1. pending → running → finished 完整流转并写起止时间
        2. 非法流转守卫（终态是吸收态，不允许回 running）
        3. 批次进度按明细增量累加 + 重跑去重不重复计数
    任务二 队列 ACK/requeue 健壮性
        4. ACK 正常路径：先写终态、再移除在途条目（顺序可证）+ 在途方法的降级契约
        5. ACK 异常路径：业务终态未确认时拒绝 ack、在途条目保留待补偿
        6. 在途记录带 worker_id + 心跳（心跳线程真实刷新）+ 心跳边界
        7. 心跳超时判崩溃并重入队（含"心跳新鲜不被误判"与死信上界对照）
        8. 幂等守卫防双跑（含"旧 worker 没死透"的 claim 拦截验证）

第 8 条是 Day52 kill -9 验收门禁的**前置验证**：崩溃补偿把消息重投回
队列后，若旧 worker 其实没死透（进程冻结、Redis 短暂隔离、GC 长停顿），
新消费者必须被 execution_id 的 SETNX claim 挡住，否则同一批次被执行两
遍——明细双写、通过率翻倍、看板口径不可信。该用例同时带一条**正向对照**
（放开 claim 后确实能跑完），否则"被挡住"也可能是因为编排整体坏了而
假通过（见 7.33：变异存活要先怀疑断言本身没有牙齿）。

时间处理铁律（7.52/7.19 同族）：
    - 禁止 time.sleep 赌时序，一律轮询 + 预算上限；
    - "心跳过期"不靠真等，而是把状态 hash 里的心跳字段**回填成过去的
      时间戳**——被测逻辑读的就是这个字段，回填与真等对它是同一条路径，
      但完全确定、零等待、零 flaky。

数据库与队列隔离：
    - 每条用例独立的临时 SQLite 库（tmp_path）+ 独立 fakeredis 队列 key；
    - "崩溃任务"直接压入在途 list 而不走队列 FIFO，否则前一个场景重投
      回队列的消息会被 BRPOP 先取走，让两个场景互相污染。
"""

import json
import os
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import allure
import pytest
import redis
from src.core import task_queue as tq
from src.core.case_manager import CaseManager, CaseManagerError
from src.core.executors import ExecutionResult, compose_bridge_note
from src.core.task_queue import (
    FIELD_HEARTBEAT_AT,
    FIELD_REQUEUE_COUNT,
    FIELD_WORKER_ID,
    HEARTBEAT_TTL_SECONDS,
    MAX_REQUEUE_ATTEMPTS,
    STATUS_DEAD_LETTER,
    STATUS_RUNNING,
    TaskQueueClient,
    TaskWorker,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 轮询预算：给足余量同时保持"失败即退出"，不做固定等待
POLL_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.02


# ===========================================================================
# 夹具与内部工具
# ===========================================================================
@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    临时 SQLite 库 + 2 条种子用例（start_execution 需要 active 用例）

    参数:
        tmp_path (Path): pytest 提供的用例级临时目录
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

    返回:
        Iterator[None]: yield 后由 teardown 复位数据库引擎
    """
    from src.db.db_session import DatabaseSession

    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day51_state_ack.db"))
    monkeypatch.setenv("TM_REDIS_ENABLED", "false")
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "false")
    DatabaseSession.reset()
    DatabaseSession.init_db()
    for idx in range(2):
        CaseManager.create_case(
            {
                "case_id": f"TM-D51-{idx + 1:04d}",
                "name": f"状态回写用例{idx + 1}",
                "module": "用户中心",
                "priority": "P1",
                "case_type": "api",
            }
        )
    yield
    DatabaseSession.reset()


@pytest.fixture
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> Iterator[TaskQueueClient]:
    """
    fakeredis 队列客户端（function 级，队列 key 每条用例唯一）

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

    返回:
        Iterator[TaskQueueClient]: yield 队列客户端实例
    """
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
    monkeypatch.setenv("TM_REDIS_URL", "fake://0")
    monkeypatch.setenv("TM_TASK_QUEUE_KEY", f"tm:queue:day51-{uuid.uuid4().hex}")
    monkeypatch.setenv("TM_TASK_BRPOP_TIMEOUT", "0.05")
    client = TaskQueueClient()
    yield client
    client.reset_backend()


def _wait_until(
    predicate: Any, message: str, timeout: float = POLL_TIMEOUT_SECONDS
) -> None:
    """
    轮询直到条件成立（禁固定 sleep 赌时序）

    参数:
        predicate (Any): 无参可调用对象，返回 True 表示条件已满足
        message (str): 超时未满足时的失败说明
        timeout (float): 预算秒数，默认 POLL_TIMEOUT_SECONDS

    返回:
        None

    异常:
        AssertionError: 预算内条件始终不成立时抛出
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    pytest.fail(f"{message}（预算 {timeout}s 内未满足）")


def _backdated(seconds: float) -> str:
    """
    构造"过去某秒"的 naive ISO 时间戳串

    参数:
        seconds (float): 距今多少秒之前

    返回:
        str: datetime.now() - timedelta(seconds=...) 的 isoformat 结果
    """
    return (datetime.now() - timedelta(seconds=seconds)).isoformat()


def _payload(execution_id: str) -> dict[str, Any]:
    """
    构造合法任务载荷（字段严格对齐 _execute_batch_async 真实签名）

    参数:
        execution_id (str): 执行批次号

    返回:
        dict[str, Any]: 含 execution_id/cases/executor_kind 的载荷
    """
    return {
        "execution_id": execution_id,
        "cases": [{"case_id": "TM-D51-0001", "name": "状态回写用例1"}],
        "executor_kind": "simulated",
    }


def _processing_items(client: TaskQueueClient) -> list[str]:
    """
    读取在途 list 当前内容（内部测试辅助函数）

    参数:
        client (TaskQueueClient): 队列客户端

    返回:
        list[str]: 在途报文列表
    """
    backend = client._get_backend()
    assert backend is not None
    return list(backend.lrange(client.processing_key, 0, -1))


def _queued_items(client: TaskQueueClient) -> list[str]:
    """
    读取待执行队列当前内容（内部测试辅助函数）

    参数:
        client (TaskQueueClient): 队列客户端

    返回:
        list[str]: 队列报文列表
    """
    backend = client._get_backend()
    assert backend is not None
    return list(backend.lrange(client.queue_key, 0, -1))


def _crashed_in_flight(
    client: TaskQueueClient, execution_id: str, heartbeat_age: float | None = None
) -> None:
    """
    构造一条"worker 已崩溃"的在途任务（内部测试辅助函数）

    直接把报文压入在途 list 并回填心跳，**不经过队列 FIFO**：若走
    enqueue+dequeue，本文件里前一个场景重投回队列的消息会被 BRPOP 先
    取走（这些用例验证的正是"消息被重投回队列"这件事），两个场景会互相
    污染出难以定位的失败。

    参数:
        client (TaskQueueClient): 队列客户端
        execution_id (str): 执行批次号
        heartbeat_age (float | None): 心跳回填为多少秒前；None 表示写
            入新鲜心跳（模拟"跑得慢但 worker 还活着"）

    返回:
        None
    """
    backend = client._get_backend()
    assert backend is not None
    backend.lpush(client.processing_key, json.dumps(_payload(execution_id)))
    backend.hset(
        tq.task_status_key(execution_id),
        mapping={
            "status": STATUS_RUNNING,
            "started_at": _backdated(3600),
            FIELD_WORKER_ID: "ghost-worker",
            FIELD_HEARTBEAT_AT: (
                datetime.now().isoformat()
                if heartbeat_age is None
                else _backdated(heartbeat_age)
            ),
        },
    )


class _StubAliveThread:
    """模拟 join 之后仍然存活的线程（内部测试辅助类）"""

    def is_alive(self) -> bool:
        """恒返回存活"""
        return True

    def join(self, timeout: float | None = None) -> None:
        """空实现：join 后依然"存活"，用于触发停不掉的告警分支"""


class _RaisingBackend:
    """所有写命令都抛连接异常的假后端（内部测试辅助类）"""

    def hset(self, *_args: Any, **_kwargs: Any) -> None:
        """模拟 hset 抛连接异常"""
        raise redis.ConnectionError("模拟宕机")

    def set(self, *_args: Any, **_kwargs: Any) -> None:
        """模拟 set 抛连接异常"""
        raise redis.ConnectionError("模拟宕机")


class _FixedResultExecutor:
    """按预设结果序列返回 ExecutionResult 的执行器桩（内部测试辅助类）"""

    def __init__(self, results: list[str]) -> None:
        """按顺序消费 results 列表，用尽后复用最后一项"""
        self.results = results
        self.seen_cases: list[dict] = []

    def run_one(self, case: dict) -> ExecutionResult:
        """返回下一个预设结果，并记录收到的用例字典"""
        self.seen_cases.append(case)
        index = min(len(self.seen_cases) - 1, len(self.results) - 1)
        result = self.results[index]
        return ExecutionResult(
            result=result,
            error_message=None if result == "passed" else f"预设失败: {result}",
            duration=0.01,
        )


# ===========================================================================
# 任务一：执行状态回写（状态机 + 批次进度）
# ===========================================================================
@allure.feature("Day51 执行状态回写")
class TestBatchStateMachine:
    """批次状态机流转、非法跳转守卫与进度增量回写"""

    @allure.story("pending 经 running 流转到 finished 并写起止时间")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_状态机_批次完整流转到finished并记录起止时间(
        self, temp_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        状态机流转 + 终态时间戳 + 进度快照三者一致

        覆盖 Day51 任务一的主链路：worker 开始执行写 running + started_at，
        全部完成后写 finished + finished_at，且批次行冗余计数与明细一致。
        顺带钉住"真实批次号被并进用例字典"——执行器据此把 Allure 结果桥接
        到真实批次，而不是回落成 pytest-{case_id} 的单条用例批次。
        """
        executor = _FixedResultExecutor(["passed", "failed"])
        monkeypatch.setattr(
            "src.core.case_manager.get_executor", lambda kind=None: executor
        )
        started = CaseManager.start_execution(trigger="web", case_type="api")

        CaseManager._execute_batch_async(started["execution_id"], started["cases"])

        status = CaseManager.get_execution_status(started["execution_id"])
        assert status["status"] == "finished", f"全流程后应为 finished: {status}"
        assert status["started_at"] is not None, "置 running 时必须写开始时间"
        assert status["finished_at"] is not None, "落终态时必须写结束时间"
        assert status["total_cases"] == 2
        assert status["passed"] == 1 and status["failed"] == 1
        assert abs(status["pass_rate"] - 0.5) < 1e-9
        assert all(
            case.get("execution_id") == started["execution_id"]
            for case in executor.seen_cases
        ), "执行器必须收到真实批次号，桥接才不会回落成 pytest-{case_id}"

    @allure.story("终态是吸收态：finished 不允许回 running")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_状态机_终态批次不允许回running且守卫抛错不改状态(
        self, temp_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        非法流转必须被拒绝，且拒绝后批次行保持原终态

        状态查询接口直查批次行、零校验零聚合。若 finished 能被改回
        running，客户端看到的就是"已完成的批次又开始了"，而它的通过数
        还停留在上一次执行的结果上——报表口径与真实结果就此对不上，
        且日志里没有任何线索指向是谁改的状态。

        同时钉住流转表的静态事实（终态吸收、pending/running 可自环重入），
        防止有人"为了让重跑能用"而给终态加上出口。
        """
        assert CaseManager._is_valid_batch_transition("pending", "running") is True
        assert CaseManager._is_valid_batch_transition("running", "finished") is True
        assert CaseManager._is_valid_batch_transition("running", "running") is True, (
            "崩溃补偿重投后会重新执行同一批次，running 自环必须放行"
        )
        assert CaseManager._is_valid_batch_transition("finished", "running") is False
        assert CaseManager._is_valid_batch_transition("failed", "running") is False
        assert CaseManager._is_valid_batch_transition("pending", "finished") is False
        assert CaseManager._is_valid_batch_transition(None, "running") is False, (
            "批次行 status 是库里的自由文本，非字符串一律判非法而不是放行"
        )
        assert CaseManager._is_valid_batch_transition("unknown", "running") is False, (
            "词表外的状态不得放行：放行等于拿未知状态去更新批次行"
        )

        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _FixedResultExecutor(["passed"]),
        )
        started = CaseManager.start_execution(trigger="web", case_type="api")
        execution_id = started["execution_id"]
        CaseManager._execute_batch_async(execution_id, started["cases"])
        assert CaseManager.get_execution_status(execution_id)["status"] == "finished"

        with pytest.raises(CaseManagerError, match="非法流转"):
            CaseManager.transition_batch_state(execution_id, "running")

        assert CaseManager.get_execution_status(execution_id)["status"] == "finished", (
            "守卫拒绝后批次行必须仍是 finished，不得被改成 running"
        )
        with pytest.raises(CaseManagerError, match="目标状态非法"):
            CaseManager.transition_batch_state(execution_id, "done")

    @allure.story("进度按明细增量累加，重复明细不重复计数")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_进度_增量累加且重复明细返回False不重复计数(self, temp_db: None) -> None:
        """
        进度是 SQL 层自增（原子），且以"明细真的落库"为计数依据

        四件事必须同时成立：
            1. 每条用例执行后进度 +1，且通过率随计数派生；
            2. error 同时计入 failed 与 error 两个桶（口径同 6.3，
               否则进度里的失败数与终态汇总对不上）；
            3. skipped 同样计入；
            4. 重复明细返回 False —— 崩溃补偿重投后批次会重跑全部用例，
               编排层据此跳过进度累加，否则进度按执行次数而非明细条数
               虚高（Day52 kill -9 门禁正是这条路径）。
        """
        started = CaseManager.start_execution(trigger="web", case_type="api")
        execution_id = started["execution_id"]
        CaseManager.transition_batch_state(execution_id, "running")

        CaseManager.update_batch_progress(execution_id, "passed")
        mid = CaseManager.update_batch_progress(execution_id, "failed", snapshot=True)
        tail = CaseManager.update_batch_progress(execution_id, "error", snapshot=True)

        assert mid["completed"] == 2 and mid["passed"] == 1, f"进度未累加: {mid}"
        assert tail["completed"] == 3 and tail["failed"] == 2 and tail["error"] == 1, (
            f"error 应同时计入 failed 与 error 两个桶: {tail}"
        )
        assert abs(tail["pass_rate"] - 1 / 3) < 1e-9
        skipped = CaseManager.update_batch_progress(execution_id, "skipped", snapshot=True)
        assert skipped["completed"] == 4 and skipped["skipped"] == 1, (
            f"skipped 未计入进度: {skipped}"
        )
        assert CaseManager.update_batch_progress(execution_id, "passed", delta=0) is None, (
            "delta<=0 是无意义调用，不得改动进度"
        )
        with pytest.raises(CaseManagerError, match="结果值非法"):
            CaseManager.update_batch_progress(execution_id, "unknown-result")

        now = datetime.now()
        first = CaseManager.record_execution(
            execution_id, "TM-D51-0001", "状态回写用例1", "passed", now, now, 0.1
        )
        again = CaseManager.record_execution(
            execution_id, "TM-D51-0001", "状态回写用例1", "passed", now, now, 0.1
        )
        assert first is True, "首次写入应返回 True"
        assert again is False, (
            "重复明细必须返回 False，编排层据此跳过进度累加（Day52 门禁前置）"
        )


# ===========================================================================
# 任务二：队列 ACK/requeue 健壮性
# ===========================================================================
@allure.feature("Day51 队列ACK/requeue健壮性")
class TestQueueAckRobustness:
    """终态与 ack 同调用链、心跳归属、崩溃补偿与幂等守卫"""

    @allure.story("正常路径：先写终态再移除在途条目")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_ACK正常路径_先写终态再ack且在途条目被清除(
        self, fake_queue: TaskQueueClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        complete_task 把"写终态 → ack"封成一条调用链，顺序可证

        ack 一旦完成，任务会同时从在途 list 与 requeue_stale 的视野里
        消失；若此刻业务终态还没写成功，批次就永久停在 running 且无任何
        补偿路径能捞回来。故顺序必须是硬约束，用调用序列表钉死：
        把 complete_task 内部两步调换即会变红。

        后半段是同一批"在途方法"的降级契约：Redis 不可用、批次号非法、
        写命令抛异常时都不得向上抛——归属信息缺失只会让补偿更保守，
        绝不能让执行链路崩掉。
        """
        execution_id = "RUN-DAY51-ACK-OK"
        assert fake_queue.enqueue(execution_id, _payload(execution_id)) is True
        assert fake_queue.dequeue(timeout=0.1) is not None
        assert len(_processing_items(fake_queue)) == 1, "出队后应先落在在途 list"

        calls: list[str] = []
        original_set_status = fake_queue.set_status
        original_ack = fake_queue.ack

        def _spy_set_status(*args: Any, **kwargs: Any) -> None:
            """记录终态写入并转发真实实现"""
            calls.append("set_status")
            original_set_status(*args, **kwargs)

        def _spy_ack(*args: Any, **kwargs: Any) -> bool:
            """记录 ack 并转发真实实现"""
            calls.append("ack")
            return bool(original_ack(*args, **kwargs))

        fake_queue.set_status = _spy_set_status  # type: ignore[method-assign]
        fake_queue.ack = _spy_ack  # type: ignore[method-assign]

        completed = fake_queue.complete_task(execution_id, "finished")

        assert completed is True
        assert calls == ["set_status", "ack"], f"必须先写终态再 ack，实际顺序: {calls}"
        assert _processing_items(fake_queue) == [], "ack 后在途条目必须被清除"
        status = fake_queue.get_status(execution_id) or {}
        assert status["status"] == "finished"
        assert status["finished_at"], "终态必须带结束时间"

        # 桥接失败的可见性（Day50 登记的 P3 遗留项）：失败原因必须能被
        # 编排层落进 error_message，否则用户只看到"报告数据对不上"
        assert compose_bridge_note("") == ""
        assert "Allure结果桥接失败" in compose_bridge_note("解析产物目录失败")

        assert fake_queue.mark_in_flight("", "w1") is False
        assert fake_queue.heartbeat("") is False
        assert fake_queue.acquire_requeue_guard("") is False
        # 队列开关关闭（后端拿不到）时同样必须降级而不是抛错
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "false")
        disabled = TaskQueueClient()
        assert disabled.mark_in_flight(execution_id, "w1") is False
        assert disabled.heartbeat(execution_id) is False
        assert disabled.acquire_requeue_guard(execution_id) is True, (
            "守卫只是防重复的优化，Redis 不可用时不得把崩溃补偿整个变成不可用"
        )
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")

        broken = TaskQueueClient()
        broken._backend = _RaisingBackend()
        assert broken.mark_in_flight(execution_id, "w1") is False
        assert broken.heartbeat(execution_id) is False
        assert broken.acquire_requeue_guard(execution_id) is True
        broken.reset_backend()

    @allure.story("终态未确认时拒绝 ack，在途条目保留待补偿")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_ACK异常路径_终态未确认时拒绝ack且保留在途条目(
        self, fake_queue: TaskQueueClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        "至多一次"降级为"至少一次"：宁可重跑一次，绝不静默丢任务

        第一段直接验 complete_task 的契约：业务终态没确认落库时仍写
        调度层终态（不让任务在状态 hash 上永远停在 running），但**拒绝
        ack**，让在途条目留在 list 里等 requeue_stale 重投。反过来（先
        ack）的失败模式是：任务从所有补偿视野消失、批次永远停在 running。

        第二段是同一条契约的端到端形态：worker 真跑一轮，而批次行停在
        running（状态写入失败或压根没发生）。此时把任务当成功 ack 掉，
        等于让"状态没写成"这一最贵的故障永久不可见。
        """
        execution_id = "RUN-DAY51-NO-ACK"
        assert fake_queue.enqueue(execution_id, _payload(execution_id)) is True
        assert fake_queue.dequeue(timeout=0.1) is not None

        completed = fake_queue.complete_task(
            execution_id,
            "failed",
            error="批次状态查询为空",
            state_confirmed=False,
        )

        assert completed is False, "终态未确认时必须拒绝 ack"
        assert len(_processing_items(fake_queue)) == 1, (
            "在途条目必须保留，否则 requeue_stale 再也扫不到这条任务"
        )
        status = fake_queue.get_status(execution_id) or {}
        assert status["status"] == "failed", "调度层仍须落下终态避免停在 running"
        assert "批次状态查询为空" in status["error"]

        # ---- 端到端：批次行停在 running 时 worker 不得判成功 ----
        stuck_id = "RUN-DAY51-NOT-TERMINAL"
        assert fake_queue.enqueue(stuck_id, _payload(stuck_id)) is True
        monkeypatch.setattr(CaseManager, "_execute_batch_async", lambda **_kwargs: None)
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            lambda _execution_id: {"status": "running", "error_message": None},
        )
        stop_event = threading.Event()
        worker = TaskWorker(fake_queue, stop_event)
        thread = threading.Thread(target=worker.run, name="tm-day51-stuck", daemon=True)
        thread.start()
        try:
            _wait_until(
                lambda: "finished_at" in (fake_queue.get_status(stuck_id) or {}),
                "worker 未在预算内落下终态",
            )
        finally:
            stop_event.set()
            thread.join(timeout=POLL_TIMEOUT_SECONDS)
            if thread.is_alive():
                pytest.fail("worker 未在停止信号后按时退出，线程残留")

        stuck_status = fake_queue.get_status(stuck_id) or {}
        assert stuck_status["status"] == "failed", (
            "批次未达终态时不得判为 finished，否则调度层与业务侧结论相反"
        )
        assert "未达终态" in stuck_status["error"]
        assert len(_processing_items(fake_queue)) == 1, (
            "终态未确认时在途条目必须保留，等待崩溃补偿重投"
        )

    @allure.story("在途记录带 worker_id，心跳由 worker 线程周期刷新")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_在途记录带worker_id且心跳线程真实刷新(
        self, fake_queue: TaskQueueClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        "谁在执行哪条任务"必须是可查事实，且活着就要持续报心跳

        心跳只能由独立线程按周期刷新：worker 主循环在同步执行批次的
        整段时间里拿不到控制权，只在"每处理完一条用例"时刷新的话，
        一批里只要有单条慢用例，心跳就会在它执行期间完全停摆，
        requeue_stale 会在它跑到一半时把任务重投出去——补偿机制自己
        制造了它本该防止的双跑。

        用例用 Event 精确控制执行窗口：先断言归属已写入，再等心跳字段
        被刷新（轮询，不用固定 sleep），最后放行执行并等线程收尾。
        """
        monkeypatch.setenv("TM_TASK_HEARTBEAT_INTERVAL", "0.05")
        release = threading.Event()
        entered = threading.Event()

        def _blocking_execute(**_kwargs: Any) -> None:
            """模拟长耗时执行：进入后阻塞直到用例放行"""
            entered.set()
            release.wait(timeout=POLL_TIMEOUT_SECONDS)

        monkeypatch.setattr(CaseManager, "_execute_batch_async", _blocking_execute)
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            lambda _execution_id: {"status": "finished"},
        )

        execution_id = "RUN-DAY51-HEARTBEAT"
        assert fake_queue.enqueue(execution_id, _payload(execution_id)) is True
        stop_event = threading.Event()
        worker = TaskWorker(fake_queue, stop_event)
        assert worker.worker_id, "worker 必须持有唯一标识"
        assert TaskWorker(fake_queue, stop_event).worker_id != worker.worker_id, (
            "同一进程内重建的 worker 必须有不同标识，否则排障分不清归属"
        )
        # 心跳间隔必须显著小于 TTL，配置写错时按"更勤快"的安全方向兜底
        assert worker.heartbeat_interval < HEARTBEAT_TTL_SECONDS
        monkeypatch.setenv("TM_TASK_HEARTBEAT_INTERVAL", "0")
        assert worker.heartbeat_interval == tq.HEARTBEAT_INTERVAL_SECONDS
        monkeypatch.setenv("TM_TASK_HEARTBEAT_INTERVAL", "9999")
        assert worker.heartbeat_interval == HEARTBEAT_TTL_SECONDS / 2
        monkeypatch.setenv("TM_TASK_HEARTBEAT_INTERVAL", "0.05")
        # 拿不到主机名不该让 worker 起不来，标识退化为 pid-随机串仍可用
        monkeypatch.setattr(
            tq.socket,
            "gethostname",
            lambda: (_ for _ in ()).throw(OSError("无主机名")),
        )
        assert str(os.getpid()) in tq.generate_worker_id()

        thread = threading.Thread(target=worker.run, name="tm-day51-worker", daemon=True)
        thread.start()
        try:
            _wait_until(
                lambda: (fake_queue.get_status(execution_id) or {}).get(FIELD_WORKER_ID)
                == worker.worker_id,
                "在途状态未写入 worker_id 归属",
            )
            first_heartbeat = (fake_queue.get_status(execution_id) or {})[
                FIELD_HEARTBEAT_AT
            ]
            assert (fake_queue.get_status(execution_id) or {})["status"] == STATUS_RUNNING

            entered.wait(timeout=POLL_TIMEOUT_SECONDS)
            _wait_until(
                lambda: (fake_queue.get_status(execution_id) or {}).get(
                    FIELD_HEARTBEAT_AT
                )
                != first_heartbeat,
                "执行期间心跳未被刷新（补偿机制会把长任务误判为崩溃）",
            )
        finally:
            release.set()
            stop_event.set()
            thread.join(timeout=POLL_TIMEOUT_SECONDS)
            if thread.is_alive():
                pytest.fail("worker 未在停止信号后按时退出，线程残留")

        assert thread.is_alive() is False
        # 空闲期没有在途任务：心跳线程必须空转等待，而不是给无关批次写心跳
        worker._current_execution_id = None
        worker._start_heartbeat()
        time.sleep(0.2)
        worker._stop_heartbeat()
        # 心跳刷新自身抛错时线程必须存活续跑，不能因单轮异常静默死亡
        monkeypatch.setattr(
            fake_queue,
            "heartbeat",
            lambda _execution_id: (_ for _ in ()).throw(RuntimeError("心跳故障")),
        )
        worker._current_execution_id = execution_id
        worker._start_heartbeat()
        time.sleep(0.2)
        worker._stop_heartbeat()
        worker._current_execution_id = None
        assert worker._heartbeat_thread is None, "心跳线程必须在预算内退出"
        # 心跳线程赖着不退时只告警不抛（daemon 兜底），否则会杀死消费线程
        worker._heartbeat_thread = _StubAliveThread()  # type: ignore[assignment]
        worker._stop_heartbeat()
        assert worker._heartbeat_thread is None

    @allure.story("心跳超时判崩溃并重入队，心跳新鲜则不误判")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_心跳超时重入队且心跳新鲜的长任务不被误判(
        self, fake_queue: TaskQueueClient
    ) -> None:
        """
        崩溃补偿只认心跳，不认 started_at——"跑得慢"不等于"死了"

        对照组是本条用例的重点：把 started_at 回填成 1 小时前（远超
        Day45 的 300 秒停留阈值）、心跳保持新鲜，requeue_stale 必须
        返回 0。若判据仍是 started_at，一批正常跑十几分钟的真实执行
        会在自己跑到一半时被复制一份并发起双跑。

        后半段钉住另外两道闸：崩溃侧心跳一并回填成 120 秒前（> 60 秒
        TTL）必须重投；上界侧重投次数已达上限的任务转死信而非永动重投；
        计数是脏值时补偿机制本身不能崩——它崩了，崩溃的任务就真没了。
        """
        assert MAX_REQUEUE_ATTEMPTS >= 1, "重投上限必须是正整数，否则等于零补偿"
        assert STATUS_DEAD_LETTER not in (
            "pending",
            "running",
            "finished",
            "failed",
        ), "死信是补偿机制的标记，不得混入任务真实执行状态的四态词表"

        fresh_id = "RUN-DAY51-SLOW-BUT-ALIVE"
        _crashed_in_flight(fake_queue, fresh_id)

        assert fake_queue.requeue_stale(max_age_seconds=300) == 0, (
            "心跳新鲜的慢任务被误判为崩溃，会在跑到一半时被复制一份并发执行"
        )
        assert len(_processing_items(fake_queue)) == 1

        crashed_id = "RUN-DAY51-CRASHED"
        _crashed_in_flight(
            fake_queue, crashed_id, heartbeat_age=HEARTBEAT_TTL_SECONDS + 60
        )

        assert fake_queue.requeue_stale() == 1, "心跳超时任务必须被重新入队"
        assert crashed_id not in "".join(_processing_items(fake_queue)), (
            "重投前必须先移出在途，否则本轮扫描会扫到自己形成自环"
        )
        queued = _queued_items(fake_queue)
        assert any(crashed_id in item for item in queued), "消息必须回到待执行队列"
        crashed_status = fake_queue.get_status(crashed_id) or {}
        assert crashed_status[FIELD_REQUEUE_COUNT] == "1", "重投次数必须记录"
        assert crashed_status["status"] == tq.STATUS_PENDING

        backend = fake_queue._get_backend()
        assert backend is not None
        dead_id = "RUN-DAY51-DEAD-LETTER"
        _crashed_in_flight(
            fake_queue, dead_id, heartbeat_age=HEARTBEAT_TTL_SECONDS + 60
        )
        backend.hset(
            tq.task_status_key(dead_id),
            mapping={FIELD_REQUEUE_COUNT: str(MAX_REQUEUE_ATTEMPTS)},
        )

        assert fake_queue.requeue_stale() == 0, "已达上限的任务不得再被自动重投"
        assert dead_id not in "".join(_processing_items(fake_queue)), (
            "死信任务应被移出在途 list"
        )
        dead_status = fake_queue.get_status(dead_id) or {}
        assert dead_status["status"] == STATUS_DEAD_LETTER
        assert "死信" in dead_status["error"]

        dirty_id = "RUN-DAY51-DIRTY-COUNT"
        _crashed_in_flight(
            fake_queue, dirty_id, heartbeat_age=HEARTBEAT_TTL_SECONDS + 60
        )
        backend.hset(
            tq.task_status_key(dirty_id), mapping={FIELD_REQUEUE_COUNT: "not-a-number"}
        )

        assert fake_queue.requeue_stale() == 1, (
            "计数字段是脏值时崩溃补偿仍必须工作——补偿机制自己崩了，"
            "崩溃的任务就真的永远捞不回来了"
        )
        assert tq._parse_int_field("not-a-number") == 0
        assert tq._parse_int_field(" 3 ") == 3, "带空白的计数应被归一"
        assert tq._parse_int_field(True) == 0, (
            "bool 是 int 子类，isinstance(True, int) 为真但语义不是计数"
        )
        assert tq._parse_int_field(None, default=-1) == -1

    @allure.story("幂等守卫防重复重投，且旧 worker 未死透时 claim 挡住双跑")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_幂等守卫防双跑_重复扫描只重投一次且claim拦住二次执行(
        self, fake_queue: TaskQueueClient, temp_db: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Day52 kill -9 门禁的前置验证：重投本身不能制造第二次执行

        两个层次各挡一次：
            1) 重投守卫：同一条在途任务被重复 LPUSH 两份（两个 worker
               同时扫到）时只重投一次，否则队列里出现两条同批次消息；
            2) 执行期 claim：重投的消息被再次消费时，旧 worker 若还持有
               claim（进程冻结/Redis 短暂隔离，并未真正死透），新消费
               者必须在 _execute_batch_async 入口被挡住——这才是
               "重投成功但旧 worker 没死透导致双跑"的真正防线。

        末尾带**正向对照**：释放 claim 后同一批次确实能跑完，确保上一段
        的"被挡住"不是因为编排整体坏掉而假通过。
        """
        # temp_db 夹具会把队列开关置回 false（两条链路各按需隔离），
        # 这里显式重新打开，避免夹具执行顺序决定本用例的行为
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        # ---- 层次1：重复扫描只重投一次 ----
        duplicate_id = "RUN-DAY51-DUPLICATE"
        _crashed_in_flight(
            fake_queue, duplicate_id, heartbeat_age=HEARTBEAT_TTL_SECONDS + 60
        )
        backend = fake_queue._get_backend()
        assert backend is not None
        # 模拟两个 worker 同时扫到：同一条报文在在途 list 里出现两份
        raw = _processing_items(fake_queue)[0]
        backend.rpush(fake_queue.processing_key, raw)

        assert fake_queue.requeue_stale() == 1, (
            "同一条在途任务只允许重投一次，否则队列里会出现两条同批次消息"
        )
        requeued = [item for item in _queued_items(fake_queue) if duplicate_id in item]
        assert len(requeued) == 1, f"队列中同批次消息应只有一条: {requeued}"

        # ---- 层次2：旧 worker 未死透，claim 挡住二次执行 ----
        monkeypatch.setattr("src.core.case_manager.task_queue_client", fake_queue)
        started = CaseManager.start_execution(trigger="web", case_type="api")
        execution_id = started["execution_id"]
        executed: list[str] = []

        class _SpyExecutor(_FixedResultExecutor):
            """把 run_one 换成带记录的桩，用来证明双跑确实被挡住"""

            def run_one(self, case: dict) -> ExecutionResult:
                executed.append(str(case.get("case_id", "")))
                return super().run_one(case)

        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _SpyExecutor(["passed", "passed"]),
        )
        # 旧 worker 崩溃前已抢占 claim 且未释放（进程冻结 ≠ 已死透）
        assert fake_queue.claim(execution_id) is True

        CaseManager._execute_batch_async(execution_id, started["cases"])

        assert executed == [], (
            f"claim 被持有时不得执行任何用例（双跑防线失效）: {executed}"
        )
        assert CaseManager.get_execution_status(execution_id)["status"] == "pending", (
            "被 claim 挡下时批次应保持 pending，由崩溃补偿重新调度"
        )

        # ---- 正向对照：释放 claim 后同一批次确实能跑完 ----
        fake_queue.release_claim(execution_id)
        CaseManager._execute_batch_async(execution_id, started["cases"])

        assert len(executed) == len(started["cases"]), "放开 claim 后必须正常执行"
        assert CaseManager.get_execution_status(execution_id)["status"] == "finished", (
            "正向对照失败：上面的拦截断言可能只是因为编排整体坏了而假通过"
        )