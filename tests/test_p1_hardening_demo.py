"""
TestMatrix 阶段B-1: P1止血修复的回归测试

测试覆盖（本文件 5 组，对应 5 条 P1）:
    A组 P1-2 cache.py 缓存后端构建期异常冒泡
        1. test_malformed_redis_url_degrades_to_noop
           非法URL（端口非数字）时 get_json 返回None不抛
        2. test_malformed_redis_url_write_also_degrades
           非法URL时 set_json 静默no-op不抛
        3. test_backend_build_failure_is_self_healing
           构建失败后 _backend 保持None，配置修好可自愈（无需重启）
    B组 P1-3/P1-4 case_manager.py 缓存失效未隔离
        4. test_finished_batch_not_rewritten_when_cache_fails
           finished分支缓存失效抛异常时，100%通过批次仍为finished
        5. test_failed_batch_still_publishes_terminal_event
           failed分支缓存失效抛异常时，batch_failed事件/通道关闭/通知仍执行
    C组 P1-5 task_queue.py dequeue 返回非dict
        6. test_dequeue_rejects_non_dict_payload
           合法JSON数组载荷被拒返回None（不返回list）
        7. test_dequeue_accepts_dict_payload_unchanged
           正常dict载荷往返不变（不回归）
        8. test_worker_survives_non_dict_payload
           队列混入非dict载荷后 worker 线程存活且后续合法任务被消费

测试铁律（对齐 PROJECT_CONTEXT.md 7.12/7.19 + 验收清单第三条）:
    - 时序/线程用例一律轮询终态，禁止固定 sleep 赌时序
    - worker 线程由 fixture 统一 start/stop，teardown 严格复位，线程绝不跨例
    - 队列与缓存是两个独立单例，setup/teardown 两侧都 reset_backend
    - 全程 fake:// 内存后端，零真实 Redis 进程
    - 全程 loguru，无 print
"""

import json
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from flask.testing import FlaskClient
from src.core import event_bus
from src.core.cache import cache_client
from src.core.case_manager import CaseManager
from src.core.task_queue import (
    start_worker,
    stop_worker,
    task_queue_client,
)
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

# 轮询参数: 模拟执行约0.01s/条，worker 最迟一个BRPOP超时周期取到任务，
# 预算远超实际耗时防flaky（口径同 test_task_queue_demo.py）
POLL_MAX_ATTEMPTS = 20
POLL_INTERVAL = 0.3

# 非 dict 载荷的多种形态（合法JSON但非对象）
NON_DICT_PAYLOADS = ["[1, 2, 3]", "null", '"纯字符串"', "42", "true"]


# ===========================================================================
# 夹具
# ===========================================================================
@pytest.fixture
def isolated_redis(monkeypatch: pytest.MonkeyPatch):
    """
    fake:// 内存 Redis（缓存与队列单测共用），不开 worker 线程

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture

    返回:
        None: yield型fixture
    """
    monkeypatch.setenv("TM_REDIS_ENABLED", "true")
    monkeypatch.setenv("TM_REDIS_URL", "fake://0")
    # 队列开关需开启才能取到后端（dequeue 路径），但不开 worker
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
    monkeypatch.setenv("TM_TASK_WORKER_ENABLED", "false")
    cache_client.reset_backend()
    task_queue_client.reset_backend()
    yield
    cache_client.reset_backend()
    task_queue_client.reset_backend()


@pytest.fixture
def db_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator:
    """
    批次编排测试用客户端（隔离临时SQLite库，不起worker）

    口径同 worker_client 但不开队列/worker——本组用例同步直调
    _execute_batch_async，验证批次状态机与收尾步骤的确定性行为
    （PROJECT_CONTEXT.md 7.12 确定性优先）。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture
        tmp_path (Path): pytest临时目录fixture

    返回:
        Iterator[FlaskClient]: yield Flask测试客户端
    """
    db_path = tmp_path / "p1_hardening.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    monkeypatch.delenv("TM_EXECUTOR", raising=False)
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "false")
    cache_client.reset_backend()
    task_queue_client.reset_backend()

    DatabaseSession.reset()
    DatabaseSession.init_db()
    _seed_cases()

    client = create_app("test").test_client()
    yield client

    DatabaseSession.reset()
    event_bus.reset_channels()
    cache_client.reset_backend()
    task_queue_client.reset_backend()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def worker_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator:
    """
    队列+worker集成测试客户端（隔离临时SQLite库）

    口径严格照抄 tests/test_task_queue_demo.py::queue_client（踩坑7.19）：
    setup 侧 setenv → 两个单例 reset_backend → DB reset/init/seed →
    start_worker 并断言存活 → 建app；
    teardown 侧 stop_worker → reset_backend → DB reset → 事件通道 reset → 删库。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture
        tmp_path (Path): pytest临时目录fixture

    返回:
        Iterator[FlaskClient]: yield Flask测试客户端
    """
    db_path = tmp_path / "queue_hardening.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    monkeypatch.delenv("TM_EXECUTOR", raising=False)
    # 队列+worker双开关，复用既有fake://内存Redis约定
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
    monkeypatch.setenv("TM_TASK_WORKER_ENABLED", "true")
    monkeypatch.setenv("TM_REDIS_URL", "fake://0")
    monkeypatch.setenv("TM_REDIS_ENABLED", "true")
    task_queue_client.reset_backend()
    cache_client.reset_backend()

    DatabaseSession.reset()
    DatabaseSession.init_db()
    _seed_cases()

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


def _seed_cases() -> None:
    """
    造用例种子数据（分两个模块，便于按模块精确控制批次内用例集合）

    模块"队列加固-全通过"下2条编号末位均为偶数 → SimulatedExecutor
    奇偶规则判 passed，供 P1-3 的"100%通过批次"场景使用；
    模块"队列加固-全失败"下2条末位均为奇数 → 判 failed，供 P1-4 使用。

    分模块的原因: start_execution 会在批次行写入 total_cases=筛选命中数，
    finish_execution 的终态计数以批次行为准，若两种用例混在同一模块，
    无法构造"批次内只含全通过用例"或"只含全失败用例"的纯净场景。

    返回:
        None
    """
    seeds = [
        ("TM-QH-0002", "队列加固用例二", "队列加固-全通过"),
        ("TM-QH-0004", "队列加固用例四", "队列加固-全通过"),
        ("TM-QH-0001", "队列加固用例一", "队列加固-全失败"),
        ("TM-QH-0003", "队列加固用例三", "队列加固-全失败"),
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
                )
            )


# ===========================================================================
# A组: P1-2 缓存后端构建期异常冒泡
# ===========================================================================
class TestCacheBackendBuildFailure:
    """P1-2: redis.from_url 裸调用导致配置错误冒泡成 API 500"""

    def test_malformed_redis_url_degrades_to_noop(
        self, isolated_redis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        非法Redis URL时 get_json 静默返回None，不抛ValueError

        回归点: 修复前 _get_backend 的真实Redis分支是裸调用
        redis.from_url，三个公开方法都是"先取后端再进try"，
        构建期的 ValueError 完全在保护范围之外 →
        GET /api/cases 走缓存读直接500。
        """
        # 端口写成非数字: redis.from_url 抛 ValueError
        monkeypatch.setenv("TM_REDIS_URL", "redis://127.0.0.1:abc/0")
        cache_client.reset_backend()

        result = cache_client.get_json("tm:test:key")

        assert result is None, (
            f"非法URL时缓存读应降级为未命中(None)，实际 {result!r}"
        )

    def test_malformed_redis_url_write_also_degrades(
        self, isolated_redis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        非法 Redis URL 时写路径同样静默降级，且**确实没写进去**

        回归点: 修复前 POST /api/cases 已 commit 成功、日志已打"用例已创建"，
        随后失效缓存抛 ValueError → 客户端收500，用户重试得到409
        "用例编号已存在"，数据已写入但用户以为失败。

        **v3 补断言**（原版全函数零断言，只写"不应抛异常即为通过"，
        注释里声称"再读回确认确实未写入"但代码里并没有读回）:
        降级有两种可能——静默 no-op，或写了但读不出来。必须断言
        "写完读回仍是 None"才能证明是前者；只断言"不抛异常"的话，
        退化成"写成功但读失败"同样会通过。
        """
        # scheme非法: redis.from_url 抛 ValueError
        monkeypatch.setenv("TM_REDIS_URL", "http://127.0.0.1:6379/0")
        cache_client.reset_backend()

        cache_client.set_json("tm:test:key", {"a": 1})
        cache_client.delete_pattern("tm:test:*")

        assert cache_client.get_json("tm:test:key") is None, (
            "非法URL时写入应完全降级为 no-op：读回必须仍是 None。"
            "若能读回内容，说明写入其实成功了，本用例的降级前提不成立"
        )
        assert cache_client._backend is None, (
            "构建失败时 _backend 必须保持 None，不得缓存坏实例"
        )

    def test_backend_build_failure_is_self_healing(
        self, isolated_redis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        构建失败后 _backend 保持None，配置修好后可自愈

        回归点: 修复前若把异常对象缓存进 _backend，后续所有请求都会复用
        这个坏实例、永不自愈；降级实现必须不写入 _backend。
        """
        monkeypatch.setenv("TM_REDIS_URL", "redis://127.0.0.1:abc/0")
        cache_client.reset_backend()

        assert cache_client.get_json("tm:heal:key") is None, "坏URL应降级"
        assert cache_client._backend is None, (
            "构建失败时 _backend 必须保持 None，不得缓存坏实例"
        )

        # 配置修好（切回fake://）后应立即恢复可用，无需重启进程
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        cache_client.reset_backend()
        cache_client.set_json("tm:heal:key", {"ok": True})
        assert cache_client.get_json("tm:heal:key") == {"ok": True}, (
            "配置修好后缓存应自愈恢复读写"
        )


# ===========================================================================
# B组: P1-3 / P1-4 缓存失效未隔离
# ===========================================================================
class TestCacheIsolationInBatchFinalization:
    """P1-3/P1-4: 缓存失效异常污染批次状态 / 杀死 daemon 线程"""

    def test_finished_batch_not_rewritten_when_cache_fails(
        self, db_client: FlaskClient
    ) -> None:
        """
        finished分支: 缓存失效抛异常时，100%通过批次仍为finished

        回归点(P1-3): 修复前 case_manager 的 finished 分支中
        cache_client.invalidate_reports() 无 try 包裹，被最外层
        except Exception 接盘后执行 e 分支，把一个 pass_rate=1.0
        的批次改写成 status="failed"，与真实执行结果矛盾。
        """
        # 同步直调编排（口径同 7.12 确定性优先，不起真实线程）
        # 模块"队列加固-全通过"下两条用例编号末位均为偶数，
        # SimulatedExecutor 奇偶规则判 passed → 构造 100% 通过批次
        started = CaseManager.start_execution(
            trigger="cli", module="队列加固-全通过"
        )
        execution_id = started["execution_id"]
        cases = started["cases"]
        assert len(cases) == 2, f"应筛出2条用例，实际 {len(cases)}"

        # 让缓存失效在批次已置finished之后抛异常
        with patch.object(
            cache_client,
            "invalidate_reports",
            side_effect=RuntimeError("模拟缓存故障"),
        ):
            CaseManager._execute_batch_async(
                execution_id=execution_id,
                cases=cases,
                executor_kind="simulated",
            )

        status = CaseManager.get_execution_status(execution_id)
        assert status is not None, "批次应已落库"
        assert status["status"] == "finished", (
            f"缓存故障不得把已 finished 的批次改写成 "
            f"{status['status']!r}（error_message={status['error_message']!r}）"
        )
        assert status["error_message"] is None, (
            f"批次不应带错误信息，实际 {status['error_message']!r}"
        )
        assert status["total_cases"] == 2, "总用例数应保持2"
        assert status["passed"] == 2, "通过数应保持2"

    def test_failed_batch_still_publishes_terminal_event(
        self, db_client: FlaskClient
    ) -> None:
        """
        failed分支: 缓存失效抛异常时，batch_failed事件/通道关闭/通知仍执行

        回归点(P1-4): 修复前该行位于外层 except 块内部，此处已无任何
        try 可接盘，裸抛会一路冒到线程目标函数外杀死 daemon 线程，
        并连带跳过 batch_failed 事件发布、事件通道关闭与失败通知——
        订阅该批次的 SSE 客户端将永远收不到终态。
        本用例同步直调编排（口径同 7.12），断言通知被调用。
        """
        started = CaseManager.start_execution(
            trigger="cli", module="队列加固-全失败"
        )
        execution_id = started["execution_id"]
        failed_cases = started["cases"]
        assert len(failed_cases) == 2, f"应筛出2条用例，实际 {len(failed_cases)}"

        # 故障执行器: 抛异常触发 failed 分支。
        # get_executor 是从 executors 导入到 case_manager 的模块级函数
        # （非 CaseManager 属性），patch 目标必须是 case_manager 的命名空间
        with patch(
            "src.core.case_manager.get_executor", return_value=_BoomExecutor()
        ):
            with patch.object(
                cache_client,
                "invalidate_reports",
                side_effect=RuntimeError("模拟缓存故障"),
            ):
                with patch.object(
                    CaseManager, "notify_execution_result", return_value={}
                ) as mock_notify:
                    CaseManager._execute_batch_async(
                        execution_id=execution_id,
                        cases=failed_cases,
                        executor_kind="simulated",
                    )

        # 核心断言: 缓存抛异常后，收尾三步仍全部执行
        assert mock_notify.called, (
            "failed分支的缓存异常不得跳过失败通知（否则 daemon 线程被杀死）"
        )
        status = CaseManager.get_execution_status(execution_id)
        assert status is not None and status["status"] == "failed", (
            f"批次应置failed，实际 {status}"
        )
        assert "模拟缓存故障" not in (status["error_message"] or ""), (
            "缓存异常不应覆盖批次真实的失败原因"
        )
        assert "模拟执行器故障" in (status["error_message"] or ""), (
            f"批次错误信息应是真实失败原因，实际 {status['error_message']!r}"
        )


class _BoomExecutor:
    """始终抛异常的最小执行器替身（用于触发 failed 分支）"""

    def run_one(self, case: dict) -> None:
        """
        抛出模拟故障

        参数:
            case (dict): 用例字典（不消费）

        异常:
            RuntimeError: 恒定抛出
        """
        raise RuntimeError("模拟执行器故障")


# ===========================================================================
# C组: P1-5 dequeue 返回非dict导致worker线程死亡
# ===========================================================================
class TestDequeuePayloadTypeGuard:
    """P1-5: 合法JSON但非对象的载荷导致worker线程静默死亡"""

    @pytest.mark.parametrize("raw", NON_DICT_PAYLOADS)
    def test_dequeue_rejects_non_dict_payload(
        self, isolated_redis, raw: str
    ) -> None:
        """
        合法JSON数组/标量载荷必须被拒（返回None），不得原样返回

        回归点: 修复前 dequeue 只做 json.loads 不校验结构，
        返回类型标注是 dict|None 但实际可能是 list/str/int/None。
        TaskWorker.run 在 try 块**之前**执行 payload.get("execution_id")，
        非 dict 抛 AttributeError 逃出 while 循环、令 worker 线程死亡。
        """
        backend = task_queue_client._get_backend()
        assert backend is not None, "fake后端应可用"
        backend.lpush(task_queue_client.queue_key, raw)

        payload = task_queue_client.dequeue(timeout=0)

        assert payload is None, (
            f"非对象载荷 {raw!r} 应被拒并返回None，实际 {payload!r}"
        )

    def test_dequeue_accepts_dict_payload_unchanged(
        self, isolated_redis
    ) -> None:
        """
        正常dict载荷往返不变（不回归）

        注意: payload内的中文与嵌套结构必须原样还原。
        """
        payload = {
            "execution_id": "RUN-DICT-0001",
            "cases": [{"case_id": "TM-中文-0001", "tags": ["冒烟", "回归"]}],
            "executor_kind": "simulated",
        }
        assert task_queue_client.enqueue("RUN-DICT-0001", payload) is True

        got = task_queue_client.dequeue(timeout=0)

        assert got == payload, f"dict载荷应原样往返，实际 {got!r}"

    def test_worker_survives_non_dict_payload(
        self, worker_client: FlaskClient
    ) -> None:
        """
        队列混入非dict载荷后，worker线程存活且后续合法任务仍被消费

        回归点: 修复前该场景下 worker 线程抛 AttributeError 后静默死亡，
        队列里后续所有任务永久堆积且无任何错误日志。

        验证方式: 直接往队列 lpush 一条非对象载荷，再通过真实HTTP
        trigger 投递一个正常批次，轮询批次至终态——若worker已死，
        批次会永远停在pending，轮询预算耗尽后失败。
        """
        backend = task_queue_client._get_backend()
        assert backend is not None, "fake后端应可用"

        # 污染: 非对象载荷排在队首（LPUSH + BRPOP = FIFO，故先入队者先出）
        backend.lpush(task_queue_client.queue_key, json.dumps([1, 2, 3]))
        backend.lpush(task_queue_client.queue_key, json.dumps("畸形字符串"))

        # 通过真实接口投递一个正常批次
        response = worker_client.post(
            "/api/executions/trigger", json={"module": "队列加固-全通过"}
        )
        assert response.status_code == 202, (
            f"trigger应返回202受理，实际 {response.status_code}"
        )
        execution_id = response.get_json()["data"]["execution_id"]

        # 轮询至终态（禁固定sleep，口径同 7.12）
        status_data = _wait_batch_terminal(worker_client, execution_id)

        assert status_data["status"] == "finished", (
            f"worker未因畸形载荷死亡，正常批次应完成，实际 {status_data}"
        )
        assert status_data["total_cases"] == 2, (
            f"应执行模块下2条用例，实际 {status_data['total_cases']}"
        )

    def test_worker_thread_still_alive_after_bad_payload(
        self, worker_client: FlaskClient
    ) -> None:
        """
        worker消费畸形载荷后线程仍存活（不依赖HTTP路径的直接证据）

        回归点: 直接断言线程存活，把"worker不会静默死亡"变成显式契约，
        防止将来有人把结构校验去掉后无人察觉。
        """
        from src.core import task_queue as tq

        backend = task_queue_client._get_backend()
        assert backend is not None, "fake后端应可用"
        backend.lpush(task_queue_client.queue_key, json.dumps([1, 2, 3]))

        # 轮询等待畸形载荷被消费（dequeue 把它弹出后队列长度归零）
        queue_len = POLL_MAX_ATTEMPTS
        for _ in range(POLL_MAX_ATTEMPTS):
            queue_len = backend.llen(task_queue_client.queue_key)
            if queue_len == 0:
                break
            time.sleep(POLL_INTERVAL)
        assert queue_len == 0, (
            f"畸形载荷未被消费（队列仍剩 {queue_len} 条），"
            "说明 worker 已停止消费"
        )

        # worker 由 worker_client fixture 启停；此处直接查模块级引用
        thread = tq._worker_thread
        assert thread is not None and thread.is_alive(), (
            "消费畸形载荷后 worker 线程必须存活，否则后续任务永久堆积"
        )


# ===========================================================================
# 轮询辅助
# ===========================================================================
def _wait_batch_terminal(
    client: FlaskClient, execution_id: str
) -> dict:
    """
    轮询批次状态至终态 finished/failed（走真实HTTP，禁固定sleep）

    参数:
        client (FlaskClient): Flask测试客户端
        execution_id (str): 执行批次号

    返回:
        dict: 终态状态字典

    异常:
        AssertionError: 轮询预算内未达终态时 pytest.fail
    """
    status_data: dict | None = None
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
