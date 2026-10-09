"""
TestMatrix 阶段C-2: Redis 任务队列未覆盖分支的补齐测试

背景:
    task_queue.py 是 83% 覆盖（全仓最低之一），缺口集中在
    **worker 生命周期的异常路径**——恰恰是阶段2 P1-5（载荷非 dict
    杀死 worker）与 P2-13（join 超时后重复起 worker）修复所在的
    区域。这些分支一旦回归，表现是"worker 静默死亡、任务永久堆积"，
    线上不会有任何报错，只会让批次永远停在 pending。

    本文件补齐的路径:
    A组 客户端后端构建与故障降级
        1.  fakeredis 缺失降级 no-op
        2.  真实 Redis URL 构建懒连接后端（socket_timeout 防挂死）
        3.  reset_backend 容忍 close 异常
        4.  dequeue 跳过无法反序列化的坏消息
        5.  get_status 后端异常返回 None
    B组 worker 存活与终态归类（核心）
        6.  缺 execution_id 的载荷被跳过且 worker 存活
        7.  执行体抛异常时 worker 存活并继续消费
        8.  批次表为 failed 时归类 failed 并带批次级错误
        9.  批次状态查询为空时归类 failed
        10. 批次状态查询抛异常时归类 failed
    C组 启停异常
        11. start_worker 构造失败返回 None 且不残留引用
        12. stop_worker join 抛异常时保留引用（P2-13 不变量）

    B组每条都用"坏任务 + 好任务"成对入队，以好任务跑到终态作为
    worker 存活的判据——单看坏任务被跳过无法区分"跳过"和"线程已死"。

测试铁律（对齐项目既有测试约定：确定性优先+幂等铁律）:
    - 禁 time.sleep(N) 赌时序：一律轮询状态 hash 终态字段
      （finished_at）并设预算上限，worker 由 contextmanager 统一停
    - 每条用例独立 TaskQueueClient 实例 + 独立队列 key，fake 数据零串扰
    - 只替换外部边界（CaseManager 的两个类方法），不碰被测模块内部
    - 全程 loguru，无 print
"""

import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import allure
import pytest
import redis
from src.core import task_queue as tq
from src.core.case_manager import CaseManager
from src.core.task_queue import (
    WORKER_JOIN_TIMEOUT_SECONDS,
    TaskQueueClient,
    TaskWorker,
)

# worker 终态轮询预算: BRPOP 超时已压到 0.05s，10s 预算远超实际耗时
POLL_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL = 0.05


# ===========================================================================
# 夹具与内部工具
# ===========================================================================
@pytest.fixture
def fake_queue(monkeypatch: pytest.MonkeyPatch) -> Iterator[TaskQueueClient]:
    """
    fakeredis 队列客户端夹具（function 级）

    每条用例用独立队列 key + 独立 TaskQueueClient 实例，
    天然隔离 fake 数据，无需依赖模块级单例复位。

    teardown:
        reset_backend 关闭并丢弃 fakeredis 实例

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

    返回:
        Iterator[TaskQueueClient]: yield 队列客户端实例
    """
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
    monkeypatch.setenv("TM_REDIS_URL", "fake://0")
    monkeypatch.setenv("TM_TASK_QUEUE_KEY", f"tm:queue:{uuid.uuid4().hex}")
    # 压到 50ms，让停止信号与任务分发都在极短周期内响应
    monkeypatch.setenv("TM_TASK_BRPOP_TIMEOUT", "0.05")
    client = TaskQueueClient()
    yield client
    client.reset_backend()


@contextmanager
def _running_worker(client: TaskQueueClient) -> Iterator[threading.Thread]:
    """
    在受控 daemon 线程中跑 TaskWorker，退出时保证线程已收尾

    teardown 逻辑:
        置 stop_event 后 join（含额外宽限），线程仍存活则 pytest.fail
        ——静默放过残留线程会污染后续用例的队列状态

    参数:
        client (TaskQueueClient): 队列客户端实例

    返回:
        Iterator[threading.Thread]: yield worker 线程对象
    """
    stop_event = threading.Event()
    worker = TaskWorker(client, stop_event)
    thread = threading.Thread(
        target=worker.run, name="tm-stage-c-worker", daemon=True
    )
    thread.start()
    try:
        yield thread
    finally:
        stop_event.set()
        thread.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS + 2)
        if thread.is_alive():
            pytest.fail("worker 未在停止信号后按时退出，线程残留")


def _wait_task_status(
    client: TaskQueueClient, execution_id: str
) -> dict:
    """
    轮询任务状态 hash 至终态（出现 finished_at 即收尾完成）

    参数:
        client (TaskQueueClient): 队列客户端实例
        execution_id (str): 目标任务批次号

    返回:
        dict: 终态状态字典

    异常:
        AssertionError: 预算内未达终态时 pytest.fail
    """
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    last_seen: dict = {}
    while time.monotonic() < deadline:
        last_seen = client.get_status(execution_id) or {}
        if "finished_at" in last_seen:
            return last_seen
        time.sleep(POLL_INTERVAL)
    pytest.fail(
        f"任务在 {POLL_TIMEOUT_SECONDS}s 预算内未达终态: {execution_id}, "
        f"最后状态: {last_seen or '无记录'}"
    )
    raise AssertionError("不可达: pytest.fail 后不会继续执行")


def _valid_payload(execution_id: str) -> dict:
    """构造合法任务载荷（字段严格对齐 _execute_batch_async 真实签名）"""
    return {
        "execution_id": execution_id,
        "cases": [{"case_id": "TM-UC-0001", "name": "用户登录成功校验"}],
        "executor_kind": "simulated",
    }


# ===========================================================================
# A组: 客户端后端构建与故障降级
# ===========================================================================
@allure.feature("阶段C覆盖率补齐")
@allure.story("任务队列客户端异常路径")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestTaskQueueClientCoverage:
    """后端构建降级 / 懒连接参数 / 坏消息跳过 / 状态读取降级"""

    def test_fakeredis_missing_degrades_to_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        fakeredis 缺失降级: 队列已启用但 fakeredis 装不上时
        _get_backend 返回 None，enqueue 返回 False 交由调用方
        fallback 裸线程，绝不抛 ImportError 把请求打成 500。
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        # sys.modules 置 None 使 import fakeredis 抛 ImportError
        monkeypatch.setitem(sys.modules, "fakeredis", None)
        client = TaskQueueClient()

        assert client._get_backend() is None, "fakeredis 缺失应降级为 None"
        assert client.enqueue("RUN-NOFAKE", _valid_payload("RUN-NOFAKE")) is False
        assert client.dequeue(timeout=0.1) is None

    def test_real_redis_url_builds_lazy_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        真实 Redis URL 构建: 非 fake:// 走 redis.from_url，且必须
        带 socket_timeout=2——没有超时时 Redis 端无响应会让调用
        线程永久挂死（from_url 本身不建连，故此处不产生真实 I/O）。
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "redis://127.0.0.1:1/0")
        client = TaskQueueClient()

        backend = client._get_backend()

        assert isinstance(backend, redis.Redis), "非 fake URL 应构建真实 redis 客户端"
        kwargs = backend.connection_pool.connection_kwargs
        assert kwargs.get("socket_timeout") == 2, (
            "真实后端必须设 socket_timeout 防连接挂死"
        )
        assert kwargs.get("decode_responses") is True, (
            "decode_responses=True 保证全链路 str 读写不混用 bytes"
        )
        client.reset_backend()

    def test_reset_backend_tolerates_close_failure(
        self, fake_queue: TaskQueueClient
    ) -> None:
        """
        reset_backend 容忍 close 异常: 后端 close 抛错时只记日志，
        仍必须把 _backend 置 None——否则测试间会复用坏实例。
        """

        class _BadCloseBackend:
            """模拟 close 抛异常的残留后端"""

            def close(self) -> None:
                """关闭时抛错"""
                raise RuntimeError("close 失败")

        fake_queue._backend = _BadCloseBackend()
        fake_queue.reset_backend()

        assert fake_queue._backend is None, (
            "close 异常不得导致 _backend 残留，下一例会复用坏实例"
        )

    def test_dequeue_skips_unparsable_payload(
        self, fake_queue: TaskQueueClient
    ) -> None:
        """
        坏消息跳过: BRPOP 取到的原始串不是合法 JSON 时 dequeue
        返回 None 且不抛异常，worker 循环得以继续（消息已被移除，
        不重入队避免毒丸消息卡死队列）。
        """
        backend = fake_queue._get_backend()
        assert backend is not None
        backend.lpush(fake_queue.queue_key, "这不是JSON{{{")

        assert fake_queue.dequeue(timeout=0.5) is None, (
            "坏消息应被丢弃并返回 None，不得抛异常杀死 worker"
        )
        # 队列已排空：消息不会回队形成毒丸循环
        assert fake_queue.dequeue(timeout=0.1) is None

    def test_get_status_returns_none_on_backend_error(
        self, fake_queue: TaskQueueClient
    ) -> None:
        """
        状态读取降级: hgetall 抛 RedisError 时 get_status 返回 None
        且不冒泡，供健康探测类调用方判"不可用"而非直接崩掉。
        """

        class _HgetallDownBackend:
            """模拟 hgetall 抛连接异常的后端"""

            def hgetall(self, name: str) -> None:
                """状态读取抛连接异常"""
                raise redis.ConnectionError("模拟宕机: hgetall失败")

        fake_queue._backend = _HgetallDownBackend()

        assert fake_queue.get_status("RUN-DOWN") is None, (
            "状态读取异常应降级为 None"
        )


# ===========================================================================
# B组: worker 存活与终态归类
# ===========================================================================
@allure.feature("阶段C覆盖率补齐")
@allure.story("worker 存活与终态归类")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestTaskWorkerSurvival:
    """坏任务不杀线程 + 终态按批次表权威状态归类"""

    def test_worker_skips_payload_without_execution_id(
        self, fake_queue: TaskQueueClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        缺 execution_id 的载荷被跳过且 worker 存活: 畸形消息先入队
        （FIFO 下先被取出）应被 continue 跳过，随后入队的正常任务
        仍能跑到终态——正常任务完成即证明线程未死。
        """
        executed: list[str] = []
        monkeypatch.setattr(
            CaseManager,
            "_execute_batch_async",
            lambda **kwargs: executed.append(kwargs["execution_id"]),
        )
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            lambda _execution_id: {"status": "finished"},
        )

        good_id = "RUN-SKIP-GOOD"
        # 坏任务缺少 execution_id；LPUSH+BRPOP 保证它先被消费
        assert fake_queue.enqueue(
            "RUN-SKIP-BAD", {"cases": [], "executor_kind": "simulated"}
        ) is True
        assert fake_queue.enqueue(good_id, _valid_payload(good_id)) is True

        with _running_worker(fake_queue):
            status = _wait_task_status(fake_queue, good_id)

        assert status["status"] == "finished"
        assert executed == [good_id], (
            "只有正常任务被执行过：畸形载荷被跳过，worker 未因之死亡"
        )

    def test_worker_survives_execute_exception(
        self, fake_queue: TaskQueueClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        执行体抛异常时 worker 存活: 第一个任务让 _execute_batch_async
        抛 RuntimeError（双保险路径），该任务归类 failed；随后入队
        的第二个任务仍被执行到终态，证明循环未退出。

        错误优先级: 此时批次表也无记录，但源码里
        `terminal_error = task_error or "批次状态查询为空"` 让**执行异常
        原文优先**——"批次状态缺失"只是现象，"执行体抛了什么"才是
        排障所需，故此处断言拿到的是执行异常原文。
        """
        executed: list[str] = []

        def _flaky_execute(**kwargs) -> None:
            """首个任务抛异常模拟执行体兜底失效"""
            executed.append(kwargs["execution_id"])
            if kwargs["execution_id"] == "RUN-BOOM":
                raise RuntimeError("执行体内部未兜底的异常")

        monkeypatch.setattr(CaseManager, "_execute_batch_async", _flaky_execute)
        # 批次表无记录：走"状态查询为空"分支归类 failed
        monkeypatch.setattr(CaseManager, "get_execution_status", lambda _eid: None)

        boom_id = "RUN-BOOM"
        next_id = "RUN-AFTER-BOOM"
        assert fake_queue.enqueue(boom_id, _valid_payload(boom_id)) is True
        assert fake_queue.enqueue(next_id, _valid_payload(next_id)) is True

        with _running_worker(fake_queue):
            boom_status = _wait_task_status(fake_queue, boom_id)
            next_status = _wait_task_status(fake_queue, next_id)

        assert boom_status["status"] == "failed", "执行体抛异常的任务必须归类 failed"
        assert boom_status["error"] == "RuntimeError: 执行体内部未兜底的异常", (
            "执行体已抛错时应优先记录其原文，而非退化为批次状态缺失"
        )
        assert executed == [boom_id, next_id], "两个任务都被消费过"
        assert next_status["status"] == "failed", (
            "第二个任务同样被消费——worker 线程未因首个任务异常而死"
        )

    def test_worker_marks_failed_when_batch_status_missing(
        self, fake_queue: TaskQueueClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        批次状态查询为空时归类 failed: 执行体正常返回但批次表查不到
        记录时，任务不得被误判为 finished（否则调度层显示成功而
        业务侧无数据）。此时无执行异常可记，error 落明确占位文案。
        """
        monkeypatch.setattr(
            CaseManager, "_execute_batch_async", lambda **kwargs: None
        )
        monkeypatch.setattr(CaseManager, "get_execution_status", lambda _eid: None)

        execution_id = "RUN-NO-BATCH"
        assert fake_queue.enqueue(
            execution_id, _valid_payload(execution_id)
        ) is True

        with _running_worker(fake_queue):
            status = _wait_task_status(fake_queue, execution_id)

        assert status["status"] == "failed", (
            "批次表无记录不得判为 finished，否则调度层与业务侧结论相反"
        )
        assert status["error"] == "批次状态查询为空"

    def test_worker_records_batch_level_error(
        self, fake_queue: TaskQueueClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        批次表 failed 时归类 failed 并带批次级错误: 终态以 SQLite
        批次表为权威来源，Redis hash 只是调度层镜像，error 字段应
        取批次表里的 error_message。
        """
        monkeypatch.setattr(
            CaseManager, "_execute_batch_async", lambda **kwargs: None
        )
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            lambda _eid: {
                "status": "failed",
                "error_message": "批次级错误: 3条用例全部执行失败",
            },
        )

        execution_id = "RUN-BATCH-FAILED"
        assert fake_queue.enqueue(
            execution_id, _valid_payload(execution_id)
        ) is True

        with _running_worker(fake_queue):
            status = _wait_task_status(fake_queue, execution_id)

        assert status["status"] == "failed"
        assert status["error"] == "批次级错误: 3条用例全部执行失败", (
            "终态错误应取批次表的 error_message"
        )

    def test_worker_marks_failed_when_status_query_raises(
        self, fake_queue: TaskQueueClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        状态查询抛异常时归类 failed: 查询失败不得阻断收尾，
        worker 仍要落下终态（error 标记为终态查询异常），
        否则任务会永远停在 running。
        """
        monkeypatch.setattr(
            CaseManager, "_execute_batch_async", lambda **kwargs: None
        )

        def _raise_on_query(_execution_id: str) -> None:
            """模拟批次状态查询异常"""
            raise RuntimeError("数据库锁等待超时")

        monkeypatch.setattr(
            CaseManager, "get_execution_status", _raise_on_query
        )

        execution_id = "RUN-QUERY-RAISE"
        assert fake_queue.enqueue(
            execution_id, _valid_payload(execution_id)
        ) is True

        with _running_worker(fake_queue):
            status = _wait_task_status(fake_queue, execution_id)

        assert status["status"] == "failed", (
            "状态查询失败也必须落终态，不得让任务停在 running"
        )
        assert "终态查询异常" in status["error"]
        assert "数据库锁等待超时" in status["error"]


# ===========================================================================
# C组: 启停异常
# ===========================================================================
@allure.feature("阶段C覆盖率补齐")
@allure.story("worker 启停异常")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestWorkerLifecycleEdge:
    """启动构造失败降级 / join 异常保留引用"""

    def test_start_worker_returns_none_when_construction_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        构造失败降级: TaskWorker 构造抛异常时 start_worker 返回
        None 且不残留模块级引用，应用启动不被阻断。
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_TASK_WORKER_ENABLED", "true")
        monkeypatch.setattr(tq, "_worker_thread", None)
        monkeypatch.setattr(tq, "_stop_event", None)

        def _boom_worker(*_args, **_kwargs) -> None:
            """模拟 worker 构造失败"""
            raise RuntimeError("线程对象构造失败")

        monkeypatch.setattr(tq, "TaskWorker", _boom_worker)

        assert tq.start_worker() is None, "构造失败应返回 None 而非抛出"
        assert tq._worker_thread is None, "失败后不得残留 worker 引用"
        assert tq._stop_event is None, "失败后不得残留停止事件引用"

    def test_stop_worker_keeps_reference_when_join_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        join 抛异常时保留引用（P2-13 不变量）: RuntimeError 分支
        必须提前 return 且不清空 _worker_thread——清空会让下次
        start_worker 的存活检查失效、并发起两个 worker 消费同一队列。
        """

        class _JoinRaisesThread:
            """模拟 join 抛异常的线程（如在自己身上调 join）"""

            def is_alive(self) -> bool:
                """始终存活"""
                return True

            def join(self, timeout: Any = None) -> None:
                """join 抛异常"""
                raise RuntimeError("cannot join current thread")

        stuck = _JoinRaisesThread()
        monkeypatch.setattr(tq, "_worker_thread", stuck)
        monkeypatch.setattr(tq, "_stop_event", threading.Event())

        tq.stop_worker()

        assert tq._worker_thread is stuck, (
            "join 异常时必须保留 worker 引用，否则会重复起第二个 worker"
        )
