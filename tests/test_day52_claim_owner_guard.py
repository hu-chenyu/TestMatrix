"""
TestMatrix Day52: claim 属主校验与释放防护回归用例（6 条）

Day52 开工前必处理的 P2（deepseek + evolving 在 Day51 复核中共同发现）：

    `release_claim` 是**无条件 DEL**，而 worker 主循环每轮收尾都会调用它
    ——**包括"入口被别的 worker 的 claim 挡下、零用例执行"的那一轮**。
    于是被挡的消费者会 DEL 掉自己并不持有的旧 worker claim，claim 层对
    "进程冻结但未死透"的防护只剩一个重投周期。

    但它也不能被简单删掉：真 kill -9 场景下旧 worker 带着未释放的 claim
    消失，claim TTL 是 3600s；若无条件释放被移除，重投回来的任务会被这道
    闸挡住、3 次重投全部落空先转死信——崩溃补偿等于没有。

六条用例分别钉住统一方案的三个动作、降级口径与存活属主保护：

    1. 属主比对释放：worker B 释放 worker A 的 claim 必须被拒绝，
       而 A 释放自己的必须成功（否则"永远拒绝"也能让本条变绿）
    2. compare-and-del 原子性：WATCH 与 EXEC 之间 claim 被第三方抢占时，
       本轮释放必须失败**且新属主的 claim 存活**；附朴素 GET+DEL 的
       反向对照，证明这条断言真的有牙齿
    3. requeue_stale 显式接管：判定属主死亡后必须先接管那条无人认领的
       claim，重投的任务才抢得到；不接管则被 3600s 的旧 claim 挡死
    4. 被挡轮次不得释放非自有 claim（worker 级）：跑**真实消费循环**，
       A 在执行中被 B 撞上，B 整轮收尾后 A 的 claim 必须原样存活
    5. 存活属主保护：心跳新鲜的批次根本进不了接管段；属主失配时按
       fail-open 接管（Day52-fix P2-2，见 `_takeover_stale_claim` docstring），
       但**心跳新鲜**这条"活人不被接管"的前置闸必须仍然拦住
    6. 降级路径：读不到属主时 `release_claim` 拒绝释放（fail-closed）、
       `_takeover_stale_claim` 继续重投（fail-open），两个相反方向都要钉住

时间处理铁律（沿用 Day51）：
    - 禁 time.sleep 赌时序，一律轮询 + 预算上限；
    - "心跳过期"靠**回填过去的时间戳**表达，不真等 60 秒。

并发处理铁律：
    - 需要两个 worker 共享同一份队列数据时，显式构造共享 FakeServer，
      绝不依赖"两个 FakeRedis 实例恰好共享数据"这种未声明的巧合；
    - 竞态注入用**管线代理**在 GET 与 EXEC 之间精确插桩，不用真线程，
      因此本文件可在 CI 上稳定连跑。
"""

import json
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import allure
import fakeredis
import pytest
import redis
from src.core.case_manager import CaseManager
from src.core.task_queue import (
    FIELD_HEARTBEAT_AT,
    FIELD_WORKER_ID,
    STATUS_RUNNING,
    TaskQueueClient,
    TaskWorker,
    task_claim_key,
)

# 轮询预算：给足余量同时保持"失败即退出"，不做固定等待
POLL_TIMEOUT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.02


# ===========================================================================
# 夹具与内部工具
# ===========================================================================
@pytest.fixture
def shared_redis() -> Any:
    """
    跨客户端共享的 fakeredis 后端（function 级）

    为什么必须显式共享 FakeServer：`TaskQueueClient._get_backend` 每次
    都 `fakeredis.FakeRedis(...)` 新建实例，两个客户端默认**互不可见**。
    而本文件的核心命题就是"worker A 的 claim 能否被 worker B 看见并
    误删"，数据不可见时断言会假通过。

    参数:
        无

    返回:
        Any: decode_responses=True 的共享 FakeStrictRedis 实例
    """
    return fakeredis.FakeStrictRedis(
        server=fakeredis.FakeServer(), decode_responses=True
    )


@pytest.fixture
def pair_clients(
    monkeypatch: pytest.MonkeyPatch, shared_redis: Any
) -> Iterator[tuple[TaskQueueClient, TaskQueueClient]]:
    """
    两个共享同一后端的队列客户端（worker A / worker B）

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        shared_redis (Any): 共享 fakeredis 后端

    返回:
        Iterator[tuple]: yield (client_a, client_b)
    """
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
    monkeypatch.setenv("TM_REDIS_URL", "fake://0")
    monkeypatch.setenv("TM_TASK_QUEUE_KEY", f"tm:queue:day52-{uuid.uuid4().hex}")
    client_a = TaskQueueClient()
    client_b = TaskQueueClient()
    # 直接注入后端：绕过各自的懒构建，强制两者落到同一份数据上
    client_a._backend = shared_redis
    client_b._backend = shared_redis
    client_a.worker_id = "worker-a"
    client_b.worker_id = "worker-b"
    yield client_a, client_b
    client_a.reset_backend()
    client_b.reset_backend()


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    临时 SQLite 库 + 2 条种子用例（第 4 条 worker 级用例需要真实批次行）

    参数:
        tmp_path (Path): pytest 提供的用例级临时目录
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

    返回:
        Iterator[None]: yield 后由 teardown 复位数据库引擎
    """
    from src.db.db_session import DatabaseSession

    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day52_claim_owner.db"))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    for idx in range(2):
        CaseManager.create_case(
            {
                "case_id": f"TM-D52-{idx + 1:04d}",
                "name": f"属主校验用例{idx + 1}",
                "module": "用户中心",
                "priority": "P1",
                "case_type": "api",
            }
        )
    yield
    DatabaseSession.reset()


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
    构造"过去某秒"的 naive ISO 时间戳串（表达心跳陈旧，不真等）

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
        "cases": [{"case_id": "TM-D52-0001", "name": "属主校验用例1"}],
        "executor_kind": "simulated",
    }


class _StealingPipeline:
    """
    在 CAS 的 GET 与 EXEC 之间精确注入"第三方抢占"的管线代理

    为什么用代理而不是真线程：真线程必须靠 sleep 赌窗口，而窗口宽度
    只有几条指令，CI 上必flaky。代理在 `multi()` 之后、`execute()` 之前
    同步完成抢占，与真实竞态**同一条命令序列**，且完全确定。

    被测代码（`_compare_and_del_claim`）本身不被改动，跑的是真实实现。
    """

    def __init__(self, pipe: Any, real: Any, key: str, thief: str) -> None:
        """记录真实管线、真实后端、被抢 key 与新属主"""
        self._pipe = pipe
        self._real = real
        self._key = key
        self._thief = thief

    def __getattr__(self, name: str) -> Any:
        """其余方法（watch/get/unwatch/execute）原样转发给真实管线"""
        return getattr(self._pipe, name)

    def __enter__(self) -> "_StealingPipeline":
        """进入真实管线上下文并返回代理自身"""
        self._pipe.__enter__()
        return self

    def __exit__(self, *exc_info: Any) -> Any:
        """退出时把异常处理原样交还真实管线"""
        return self._pipe.__exit__(*exc_info)

    def multi(self) -> None:
        """排入 MULTI 后立刻模拟第三方抢占：删旧属主、写入新属主"""
        self._pipe.multi()
        self._real.delete(self._key)
        self._real.set(self._key, self._thief, ex=3600)


class _StealingBackend:
    """pipeline() 返回带抢占注入的管线代理的后端替身"""

    def __init__(self, real: Any, key: str, thief: str) -> None:
        """保存真实后端与抢占参数"""
        self._real = real
        self._key = key
        self._thief = thief

    def __getattr__(self, name: str) -> Any:
        """其余方法（get/set/delete 等）原样转发给真实后端"""
        return getattr(self._real, name)

    def pipeline(self, *args: Any, **kwargs: Any) -> Any:
        """返回会在 EXEC 前完成抢占的管线代理"""
        return _StealingPipeline(
            self._real.pipeline(*args, **kwargs), self._real, self._key, self._thief
        )


class _FaultyBackend:
    """真实后端 + 指定命令抛 Redis 异常的注入层

    为什么不用纯替身：降级路径真正要验证的是"失败之后 claim 还在不在"，
    纯替身没有真实数据可查，只能靠推断；包一层真实后端则可以直接读回。
    """

    def __init__(self, real: Any, raises: dict[str, Exception]) -> None:
        """保存真实后端与"命令名 -> 要抛的异常"映射"""
        self._real = real
        self.raises = raises

    def __getattr__(self, name: str) -> Any:
        """未列入 raises 的命令原样转发；列入的改为抛对应异常"""
        if name in self.raises:
            def _boom(*args: Any, **kwargs: Any) -> Any:
                """按注入配置抛出指定异常"""
                raise self.raises[name]

            return _boom
        return getattr(self._real, name)


class _RecordingCaseManager:
    """
    转发到真实 CaseManager 的消费侧替身（记录本轮是否真的跑过执行体）

    worker 的 `_consume_loop` 把 CaseManager 当参数接收，这里借该接缝
    注入记录能力，同时保持 `_execute_batch_async` / `get_execution_status`
    走**真实实现**——被挡判定必须由生产代码自己做出，不能由测试替身代劳。
    """

    def __init__(self) -> None:
        """初始化执行记录列表"""
        self.executed: list[str] = []

    def _execute_batch_async(self, **kwargs: Any) -> None:
        """记录本轮进入执行体，再转发真实实现"""
        self.executed.append(str(kwargs.get("execution_id")))
        CaseManager._execute_batch_async(**kwargs)

    def get_execution_status(self, execution_id: str) -> dict | None:
        """转发真实批次状态查询"""
        return CaseManager.get_execution_status(execution_id)


# ===========================================================================
# claim 属主校验（Day52 P2 修复）
# ===========================================================================
@allure.feature("Day52 claim属主校验")
class TestClaimOwnerGuard:
    """属主比对释放、CAS 原子性、崩溃接管与被挡轮次防护"""

    @allure.story("worker B 释放 worker A 的 claim 必须被拒绝")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.regression
    def test_属主比对_非自有claim释放被拒且自有claim释放成功(
        self, pair_clients: tuple[TaskQueueClient, TaskQueueClient]
    ) -> None:
        """
        属主比对释放的核心命题：只删自己持有的 claim

        三段断言缺一不可：
            1. B 的 claim 被挡下（SET NX 语义仍然成立）；
            2. B 释放 A 的 claim 返回 False，**且 claim 原样存活**；
            3. A 释放自己的 claim 返回 True，claim 消失。
        第 3 段是第 2 段的牙齿：若实现把"一律拒绝"当修复，本条会红。
        """
        client_a, client_b = pair_clients
        execution_id = "RUN-D52-OWNER"

        assert client_a.claim(execution_id) is True, "A 应抢到 claim"
        assert client_b.claim(execution_id) is False, "同一批次不得被二次抢占"
        assert client_b._backend.get(task_claim_key(execution_id)) == "worker-a", (
            "claim 的 value 必须存属主标识，否则属主校验无从判断"
        )

        assert client_b.release_claim(execution_id) is False, (
            "B 释放非自有 claim 必须被拒绝"
        )
        assert client_b._backend.get(task_claim_key(execution_id)) == "worker-a", (
            "被拒绝的释放不得改动 claim：修复前这里会被 DEL 成 None"
        )

        assert client_a.release_claim(execution_id) is True, "A 应能释放自己的 claim"
        assert client_a._backend.get(task_claim_key(execution_id)) is None

    @allure.story("GET 与 EXEC 之间被抢占时释放失败且新属主存活")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.regression
    def test_compare_and_del原子性_期间被抢占则释放失败且新属主存活(
        self, pair_clients: tuple[TaskQueueClient, TaskQueueClient]
    ) -> None:
        """
        WATCH 的意义：属主比对与删除之间不允许存在窗口

        朴素实现"先 GET 比对、再 DEL"在这个窗口里会把**新属主**的 claim
        删掉——刚由崩溃补偿建立起来的闸被当场拆掉，双跑防线反而更弱。
        这里用管线代理在 MULTI 之后精确注入抢占，并断言两件事：
            1. 释放返回 False（事务被抢占，本轮放弃）；
            2. 新属主的 claim 仍存活（这是真正要防的后果）。

        末尾的朴素实现对照用于证明本断言有牙齿：同样的抢占窗口下，
        GET+DEL 会把新属主的 claim 删掉——若它也"安全"，说明本条测的
        不是原子性而是别的东西。
        """
        client_a, client_b = pair_clients
        execution_id = "RUN-D52-CAS"
        key = task_claim_key(execution_id)

        assert client_a.claim(execution_id) is True
        stealing_backend = _StealingBackend(client_a._backend, key, "worker-thief")
        original_backend = client_a._backend
        client_a._backend = stealing_backend
        try:
            assert client_a.release_claim(execution_id) is False, (
                "属主在 CAS 期间被抢占时本轮释放必须失败"
            )
        finally:
            client_a._backend = original_backend

        assert client_b._backend.get(key) == "worker-thief", (
            "新属主的 claim 必须存活；被删即说明删除没有与比对绑定在一次事务里"
        )

        # 反向对照：朴素 GET+DEL 在同一窗口下确实会误删新属主的 claim
        control_id = "RUN-D52-CAS-CTRL"
        control_key = task_claim_key(control_id)
        assert client_a.claim(control_id) is True
        naive_owner = client_b._backend.get(control_key)
        assert naive_owner == "worker-a"
        client_b._backend.delete(control_key)  # 模拟窗口内被接管
        client_b._backend.set(control_key, "worker-thief", ex=3600)
        if naive_owner == "worker-a":
            client_b._backend.delete(control_key)  # 迟到的 DEL 拆掉了别人的保护
        assert client_b._backend.get(control_key) is None, (
            "朴素实现的对照未复现误删，说明本条的 CAS 断言可能测不到真实风险"
        )

    @allure.story("死亡属主的 claim 被显式接管后重投任务可重新抢占")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.regression
    def test_requeue_stale_显式接管死亡属主claim使重投任务可重新抢占(
        self, pair_clients: tuple[TaskQueueClient, TaskQueueClient]
    ) -> None:
        """
        崩溃补偿要能在 claim 的 3600s TTL 内真正恢复，接管是必要条件

        若只重投不接管：旧 worker 带着未释放的 claim 消失，重投回来的
        任务在 `claim()` 处被当场挡下，3 次重投全落空、先转死信。
        因此本条同时钉住前后两态：
            1. 前态：死亡属主的 claim 确实挡着重投任务（接管是承重的）；
            2. 后态：requeue_stale 接管后 claim 消失、重投任务抢得到。
        """
        client_a, client_b = pair_clients
        execution_id = "RUN-D52-STALE"

        # 构造"worker 已崩溃"的在途任务：心跳回填为 3600 秒前
        client_a._backend.lpush(
            client_a.processing_key, json.dumps(_payload(execution_id))
        )
        client_a.set_status(
            execution_id,
            STATUS_RUNNING,
            started_at=_backdated(3600),
            **{FIELD_WORKER_ID: "worker-a", FIELD_HEARTBEAT_AT: _backdated(3600)},
        )
        assert client_a.claim(execution_id) is True, "崩溃 worker 已持有 claim"
        assert client_b.claim(execution_id) is False, (
            "接管前：旧 claim 仍挡着重投任务，这正是接管要解决的阻塞"
        )

        assert client_b.requeue_stale() == 1, "心跳超时的在途任务应被重新投回"

        assert client_a._backend.get(task_claim_key(execution_id)) is None, (
            "requeue_stale 必须显式接管死亡属主的 claim，否则重投任务永远抢不到"
        )
        assert client_b.claim(execution_id) is True, (
            "接管后重投任务必须能重新抢占 claim（崩溃恢复的关键前提）"
        )
        queued = list(client_b._backend.lrange(client_b.queue_key, 0, -1))
        assert len([item for item in queued if execution_id in item]) == 1

    @allure.story("被挡轮次整轮收尾后不得释放他人 claim")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.regression
    def test_被挡轮次_跑完整消费循环也不得释放workerA的claim(
        self,
        pair_clients: tuple[TaskQueueClient, TaskQueueClient],
        temp_db: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        worker 级回归：把 P2 的真实触发路径整条跑一遍

        Day52 P2 的触发点不是"调用 release_claim"，而是**worker 主循环
        收尾那一行**——那一轮零用例执行，却照样走到 release。所以这里
        驱动真实的 TaskWorker 消费循环，让它完成"出队 → 被挡 → 收尾"
        整轮，再检查 A 的 claim 是否还在。

        用"release_claim 调用完毕后才记痕"的间谍消除竞态：记痕出现在
        列表里即代表那一行的删除动作已经发生，断言不会与它并行。
        """
        client_a, client_b = pair_clients
        # 生产代码里的 claim 调用点在 case_manager，指向模块级单例；
        # 换成共享客户端，worker B 才看得到 A 已经持有的 claim
        monkeypatch.setattr("src.core.case_manager.task_queue_client", client_b)

        started = CaseManager.start_execution(trigger="web", case_type="api")
        execution_id = started["execution_id"]
        assert client_a.claim(execution_id) is True, "worker A 已抢占并正在执行"
        client_a.mark_in_flight(execution_id, "worker-a")
        assert client_a.enqueue(execution_id, _payload(execution_id)) is True

        release_calls: list[tuple[str, str | None, bool]] = []
        original_release = client_b.release_claim

        def _spy_release(target_id: str, worker_id: str | None = None) -> bool:
            """真实释放完成后再记痕，消除"记痕早于删除"的竞态"""
            outcome = original_release(target_id, worker_id)
            release_calls.append((target_id, worker_id, outcome))
            return outcome

        monkeypatch.setattr(client_b, "release_claim", _spy_release)

        recorder = _RecordingCaseManager()
        stop_event = threading.Event()
        worker_b = TaskWorker(client_b, stop_event, worker_id="worker-b")
        thread = threading.Thread(
            target=worker_b._consume_loop,
            args=(recorder,),
            name="day52-worker-b",
            daemon=True,
        )
        thread.start()
        try:
            _wait_until(
                lambda: bool(release_calls),
                "worker B 未在预算内走到收尾的 release_claim",
            )
        finally:
            stop_event.set()
            thread.join(timeout=10.0)

        assert recorder.executed == [execution_id], (
            f"本轮必须真的进过执行体（否则断言可能因消息根本没被消费而假通过）: "
            f"{recorder.executed}"
        )
        assert CaseManager.get_execution_status(execution_id)["status"] == "pending", (
            "被 claim 挡下时批次应保持 pending，一个用例都不该执行"
        )
        assert release_calls[0][1] == "worker-b", "收尾必须带上本 worker 的属主身份"
        assert release_calls[0][2] is False, "被挡轮次释放非自有 claim 必须失败"
        assert client_a._backend.get(task_claim_key(execution_id)) == "worker-a", (
            "P2 核心断言：A 的 claim 必须原样存活，修复前此处会被 DEL 成 None"
        )

    @allure.story("心跳新鲜的存活属主不被接管；属主失配时按心跳超时 fail-open 接管")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.regression
    def test_存活属主的claim不被requeue_stale释放(
        self, pair_clients: tuple[TaskQueueClient, TaskQueueClient]
    ) -> None:
        """
        fail-open 之所以安全，全靠"心跳新鲜"这道前置闸

        两条路径必须一起看，单独看任一条都会得出相反的错误结论：

            1. 心跳新鲜 → 根本不进接管段（在途条目与 claim 原样保留）。
               **这条才是"活人不被接管"的真正保障**：只要 worker 还活着，
               它的心跳线程就会持续刷新 heartbeat_at，requeue_stale 永远不会
               走到接管这一步。
            2. 心跳已陈旧、但 claim 属主与状态 hash 属主失配 → **fail-open
               继续接管**（Day52-fix P2-2）。失配的主因是被挡轮次覆写：
               `mark_in_flight` 在 claim 之前执行，被挡下的 worker 早已把
               状态 hash 的 worker_id 写成自己，于是"状态 hash 属主"根本不是
               死亡者，claim 的实际属主才是。此时若跳过，崩溃恢复就从 60s
               退化到 claim TTL 3600s，3 次重投全被挡、先转死信。

        第 2 条与第 1 条构成一组对照：判据是**心跳**，不是"属主字段等不等"。
        """
        client_a, client_b = pair_clients

        # ---- 路径1：心跳新鲜，A 仍在执行 ----
        fresh_id = "RUN-D52-ALIVE"
        client_a._backend.lpush(
            client_a.processing_key, json.dumps(_payload(fresh_id))
        )
        client_a.set_status(
            fresh_id,
            STATUS_RUNNING,
            started_at=_backdated(3600),
            **{
                FIELD_WORKER_ID: "worker-a",
                FIELD_HEARTBEAT_AT: datetime.now().isoformat(),
            },
        )
        assert client_a.claim(fresh_id) is True

        assert client_b.requeue_stale() == 0, "心跳新鲜的批次不得被判定为死亡"
        assert client_a._backend.get(task_claim_key(fresh_id)) == "worker-a", (
            "worker A 仍在执行，其 claim 不得被任何 worker 释放"
        )
        assert (
            len(client_a._backend.lrange(client_a.processing_key, 0, -1)) == 1
        ), "未被重投的任务必须原样留在在途 list"

        # ---- 路径2：心跳陈旧 + 属主失配（被挡轮次覆写）→ fail-open 接管 ----
        stale_id = "RUN-D52-MISMATCH"
        client_a._backend.lpush(
            client_a.processing_key, json.dumps(_payload(stale_id))
        )
        client_a.set_status(
            stale_id,
            STATUS_RUNNING,
            started_at=_backdated(3600),
            **{FIELD_WORKER_ID: "worker-a", FIELD_HEARTBEAT_AT: _backdated(3600)},
        )
        # 状态 hash 记的是 worker-a（被挡轮次覆写的假象），claim 实际属主是
        # worker-b（真正死掉的那个）。心跳已陈旧，接管必须照常进行。
        assert client_b.claim(stale_id) is True
        assert client_b._backend.get(task_claim_key(stale_id)) == "worker-b"

        assert client_b.requeue_stale() == 1, (
            "心跳已陈旧时即使属主失配也必须接管并重投："
            "跳过会让崩溃恢复退化到 claim TTL 3600s"
        )
        assert client_b._backend.get(task_claim_key(stale_id)) is None, (
            "接管后 claim 必须消失，重投任务才抢得到"
        )
        assert client_b.claim(stale_id) is True, (
            "接管后重投任务必须能重新抢占（崩溃恢复的前提）"
        )

    @allure.story("读不到属主时释放拒绝、接管不阻断重投")
    @allure.severity(allure.severity_level.NORMAL)
    @pytest.mark.regression
    def test_降级路径_读不到属主时释放拒绝且接管不阻断重投(
        self,
        pair_clients: tuple[TaskQueueClient, TaskQueueClient],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        两个方向相反的降级口径，各半条都钉死

        读不到属主时两条路径的处置**故意相反**，因为对手方不同：
            - release_claim（worker 清理自己的东西）→ 拒绝释放。误删的
              代价是当场拆掉别人的双跑防线，等不起；
            - _takeover_stale_claim（替系统回收无主资源）→ 继续重投。
              此时不动就等于把唯一能救回崩溃任务的路径掐断，而认错的
              代价由重投次数上界与明细分桶去重兜住。

        用真实后端 + 异常注入层（而非纯替身）驱动，这样"失败后 claim
        到底还在不在"可以直接查到，不用靠推断。
        """
        client_a, _client_b = pair_clients

        # ---- release_claim：事务管线异常 → 拒绝释放，claim 原样存活 ----
        broken_pipe_id = "RUN-D52-DEGRADE-PIPE"
        assert client_a.claim(broken_pipe_id) is True
        original = client_a._backend
        client_a._backend = _FaultyBackend(
            original, {"pipeline": redis.ConnectionError("管线炸了")}
        )
        try:
            assert client_a.release_claim(broken_pipe_id) is False, (
                "读不到属主时必须拒绝释放（fail-closed）"
            )
        finally:
            client_a._backend = original
        assert original.get(task_claim_key(broken_pipe_id)) == "worker-a", (
            "拒绝释放不得改动 claim"
        )

        # ---- _takeover_stale_claim：读属主异常 → 继续重投，claim 存活 ----
        unread_id = "RUN-D52-DEGRADE-GET"
        assert client_a.claim(unread_id) is True
        client_a._backend = _FaultyBackend(
            original, {"get": redis.ConnectionError("读炸了")}
        )
        try:
            assert client_a._takeover_stale_claim(unread_id, "worker-a") is True, (
                "读不到属主时接管应放行：不动就等于掐断崩溃恢复路径"
            )
        finally:
            client_a._backend = original
        assert original.get(task_claim_key(unread_id)) == "worker-a"

        # ---- _takeover_stale_claim：删除异常 → 继续重投，claim 存活 ----
        undeletable_id = "RUN-D52-DEGRADE-DEL"
        assert client_a.claim(undeletable_id) is True
        client_a._backend = _FaultyBackend(
            original, {"delete": redis.ConnectionError("删炸了")}
        )
        try:
            assert client_a._takeover_stale_claim(undeletable_id, "worker-a") is True
        finally:
            client_a._backend = original
        assert original.get(task_claim_key(undeletable_id)) == "worker-a"

        # ---- _takeover_stale_claim：后端整体不可用 → 直接放行 ----
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "false")
        offline = TaskQueueClient()
        assert offline._takeover_stale_claim("RUN-D52-DEGRADE-OFFLINE", "worker-a") is True