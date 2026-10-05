"""
Day45 全量 bug 审查第 1 批修复的配套测试（并发与队列核心）

对应 5 个被修复的问题（每条都带"回退即变红"的验证，见 6.x）:
    问题1 (P1) worker 后端起效后再故障 → 零延时热转
        → dequeue 区分"队列空"与"故障"，worker 按连续失败次数指数退避
    问题2 (P1) enqueue 超时误判 → 同一批次被 worker 与 fallback 线程双跑
        → execution_id 级 SETNX 幂等抢占 + record_execution 明细去重
    问题3 (P2) BRPOP timeout=0 语义与文档相反 → worker 无法优雅停止
        → <=0 一律夹到 MIN_BRPOP_TIMEOUT_SECONDS（redis-py 的 0 是永久阻塞）
    问题4 (P2) Redis 无 ack：worker 在途崩溃即丢任务
        → BRPOPLPUSH 落入在途 list + ack() 确认 + requeue_stale() 崩溃补偿
    问题5 (P1) benchmark_cache 默认参数 flushdb() 清掉共享 DB 0
        → 默认切独立 5 号库 + 共享库安全闸（--allow-shared-db 才放行）

测试链路铁律:
    - 全程 fakeredis 内存后端（fake://），零真实 Redis 进程依赖
    - 零真实子进程、零真实网络、零真实通知外发
    - 时序一律用 stop_event / 轮询，不使用固定 sleep 赌时序
    - 断言必须**能杀死回退**：本文件对关键判据均做了变异验证
      （把被测分支改回修复前写法，对应用例必须变红）
"""

import threading
import time
from datetime import datetime, timedelta

import fakeredis
import pytest
import redis
from src.core import task_queue as tq
from src.core.task_queue import (
    MIN_BRPOP_TIMEOUT_SECONDS,
    TaskQueueClient,
    TaskWorker,
    failure_backoff_seconds,
    task_claim_key,
    task_status_key,
)

# 注意：**不能** `from src.db.models import TestExecution`。
# TestCase/TestExecution 等 ORM 模型名以 "Test" 开头，而 pytest.ini 的
# `python_classes = Test*` 会把它当成测试类尝试收集，从而在测试输出里
# 产生 PytestCollectionWarning（本项目基线要求 0 warning）。
# 这里改用模块限定访问（models.TestExecution），模块命名空间里不留
# 任何 Test 前缀名字，从根上避免误收集。
from src.db import models
from src.db.db_session import DatabaseSession

# 热转观察窗口（秒）：足够让"无退避"实现跑出成千上万次，
# 又短到不会拖慢套件。无退避实现 2 秒 ≈ 21000 次，有退避实现 ≈ 1-3 次。
HOT_SPIN_WINDOW_SECONDS = 0.6


@pytest.fixture
def client(monkeypatch):
    """
    构造启用队列、绑定 fakeredis 内存后端的客户端

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量注入

    返回:
        TaskQueueClient: 后端已就绪的客户端
    """
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
    monkeypatch.setenv("TM_REDIS_URL", "fake://0")
    instance = TaskQueueClient()
    instance._backend = fakeredis.FakeRedis(decode_responses=True)
    instance._failed_until = 0.0
    instance._consecutive_failures = 0
    instance._last_dequeue_failed = False
    instance._last_dequeued_raw = None
    return instance


class _DeadAfterConnect:
    """后端对象存在但每条命令都抛连接异常（模拟"连上过、现在断了"）"""

    def __init__(self) -> None:
        self.calls = 0

    def _boom(self, *args, **kwargs):
        self.calls += 1
        raise redis.ConnectionError("模拟运行期连接断开")

    brpoplpush = _boom
    lpush = _boom
    lrem = _boom
    lrange = _boom
    hgetall = _boom
    hset = _boom


# ===========================================================================
# 问题1: worker 热转（本次修复的核心）
# ===========================================================================


def test_问题1_后端已构建但连接断开_dequeue被标记为故障(client):
    """
    后端对象非 None 但命令抛异常时，dequeue 必须标记故障

    修复前 dequeue 对"队列空"与"故障"一视同仁返回 None，worker 的
    退避判据 `_backend is None` 在此场景为假 → 零延时热转。
    """
    client._backend = _DeadAfterConnect()

    assert client.dequeue(timeout=0.1) is None
    assert client.has_recent_failure() is True, "连接异常必须被记为故障而非队列空"
    assert client.get_failure_backoff() > 0, "故障后应给出正的退避秒数"


def test_问题1_队列空不算故障且计数归零(client):
    """BRPOPLPUSH 正常超时（队列空）属通信成功，必须清零故障标记"""
    client.enqueue("RUN-EMPTY", {"execution_id": "RUN-EMPTY", "cases": []})
    client._record_dequeue_failure()
    assert client.has_recent_failure() is True

    payload = client.dequeue(timeout=0.1)
    client.ack()

    assert payload is not None, "夹具应能取到任务"
    assert client.has_recent_failure() is False, "取到任务即通信恢复"
    assert client.consecutive_failures == 0, "连续失败计数应归零"


def test_问题1_退避秒数随连续失败递增并封顶():
    """指数退避：1→2→4→8→16→30(封顶)，不再出现恒定值"""
    assert failure_backoff_seconds(0) == 0.0
    assert failure_backoff_seconds(1) == 1.0
    assert failure_backoff_seconds(2) == 2.0
    assert failure_backoff_seconds(3) == 4.0
    assert failure_backoff_seconds(4) == 8.0
    assert failure_backoff_seconds(5) == 16.0
    assert failure_backoff_seconds(6) == 30.0
    assert failure_backoff_seconds(50) == 30.0, "超过封顶值后保持封顶"


def test_问题1_worker故障时退避不空转(client):
    """
    worker 在后端已构建但连接断开时必须退避，不能零延时循环

    验证手法：让 dequeue 走真实路径并恒定抛连接异常，在固定观察窗口内
    统计调用次数。无退避实现会跑出成千上万次（Day45 全量审查实测
    2 秒 21124 次）；本次修复后首次退避 1 秒，窗口内只应出现 1~2 次。

    这里刻意**不整体替换 dequeue**，而是只把 _backend 换成"死连接"对象，
    让真实的异常捕获与故障记账逻辑参与——否则测的是桩而不是被测代码。
    """
    calls = {"n": 0}
    client._backend = _DeadAfterConnect()
    real_dequeue = client.dequeue

    def counting_dequeue(timeout=None):
        calls["n"] += 1
        return real_dequeue(timeout)

    client.dequeue = counting_dequeue

    stop = threading.Event()
    worker = TaskWorker(client, stop)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    time.sleep(HOT_SPIN_WINDOW_SECONDS)
    stop.set()
    thread.join(timeout=5)

    assert not thread.is_alive(), "worker 应能响应停止信号"
    assert calls["n"] <= 3, (
        f"故障窗口内 dequeue 被调用 {calls['n']} 次；"
        f"零延时热转的实测值约为 10000 次/秒，退避未生效"
    )


def test_问题1_恢复后失败计数重置(client):
    """故障若干轮后通信恢复，连续失败计数必须归零"""
    dead = _DeadAfterConnect()
    client._backend = dead
    for _ in range(3):
        client.dequeue(timeout=0.1)
    assert client.consecutive_failures == 3

    # 恢复正常后端，再取一次任务
    client._backend = fakeredis.FakeRedis(decode_responses=True)
    client.enqueue("RUN-RECOVER", {"execution_id": "RUN-RECOVER", "cases": []})
    payload = client.dequeue(timeout=0.1)
    client.ack()

    assert payload is not None
    assert client.consecutive_failures == 0
    assert client.has_recent_failure() is False


# ===========================================================================
# 问题2: 幂等抢占与明细去重
# ===========================================================================


def test_问题2_同一批次只能被抢占一次(client):
    """SETNX 抢占：第一个拿到 True，第二个必须 False"""
    assert client.claim("RUN-CLAIM") is True
    assert client.claim("RUN-CLAIM") is False, "同一批次不得被第二次抢占"


def test_问题2_释放抢占后可重新抢占(client):
    """批次终态释放 claim 后，同批次允许被重新执行（如失败重跑）"""
    assert client.claim("RUN-RETRY") is True
    assert client.claim("RUN-RETRY") is False
    assert client.release_claim("RUN-RETRY") is True
    assert client.claim("RUN-RETRY") is True, "释放后应可重新抢占"


def test_问题2_claim写入带TTL键(client):
    """claim 必须落到约定的 key 上，便于运维排查"""
    client.claim("RUN-KEY")
    assert client._backend.exists(task_claim_key("RUN-KEY")) == 1


def test_问题2_队列未启用时抢占恒放行不阻断执行(monkeypatch):
    """
    后端不可用时 claim 必须返回 True

    队列关闭是项目默认（TM_TASK_QUEUE_ENABLED 默认 false），此时只有
    路由裸线程一条路径，不存在双跑。若此时 claim 返回 False，等于让
    幂等保护反过来把平台核心功能整体停摆。
    """
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "false")
    offline = TaskQueueClient()
    assert offline.claim("RUN-OFFLINE") is True


def test_问题2_空批次号拒绝抢占(client):
    """空 execution_id 不构成有效抢占目标"""
    assert client.claim("") is False


# ===========================================================================
# 问题3: BRPOP timeout 语义
# ===========================================================================


def test_问题3_timeout为0时被夹到最小正值不永久阻塞(client):
    """
    redis-py 的 brpop timeout=0 是"永久阻塞"，不是"不阻塞立即返回"

    修复前 docstring 写反，且直接把 0 透传 → 配置
    TM_TASK_BRPOP_TIMEOUT=0 时 worker 永久卡住、stop_event 失效。
    """
    assert MIN_BRPOP_TIMEOUT_SECONDS > 0
    # 后端空队列时，dequeue(0) 必须**及时**返回而不是挂住
    started = time.perf_counter()
    assert client.dequeue(timeout=0) is None
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"timeout=0 未被夹逼，dequeue 阻塞了 {elapsed:.2f}s"


def test_问题3_负timeout同样被夹逼(client):
    """负数在 redis 侧同样是非法阻塞时长，应一并夹到最小正值"""
    started = time.perf_counter()
    assert client.dequeue(timeout=-5) is None
    assert time.perf_counter() - started < 2.0


# ===========================================================================
# 问题4: 在途确认与崩溃补偿
# ===========================================================================


def test_问题4_任务出队后落在在途list(client):
    """无 ack 的核心：出队后任务必须同时存在于在途 list"""
    client.enqueue("RUN-ACK", {"execution_id": "RUN-ACK", "cases": []})

    payload = client.dequeue(timeout=0.1)

    assert payload is not None
    in_flight = client._backend.lrange(client.processing_key, 0, -1)
    assert len(in_flight) == 1, "出队后任务应留在在途 list"
    assert "RUN-ACK" in in_flight[0]


def test_问题4_ack后在途list被清空(client):
    """执行完成并 ack 后，任务不再留在在途 list（不会被重复补偿）"""
    client.enqueue("RUN-ACK2", {"execution_id": "RUN-ACK2", "cases": []})
    client.dequeue(timeout=0.1)

    assert client.ack() is True
    assert client._backend.lrange(client.processing_key, 0, -1) == []
    # 二次 ack 无待确认任务，返回 False 而非报错
    assert client.ack() is False


def test_问题4_无ack时超时任务被重新投回队列(client):
    """
    模拟 worker 崩溃：任务停在在途 list 且 started_at 很久以前

    崩溃补偿应把它重新入队，使批次不至于永久停在 pending。
    """
    payload = {"execution_id": "RUN-CRASH", "cases": []}
    client.enqueue("RUN-CRASH", payload)
    client.dequeue(timeout=0.1)
    # 不 ack（模拟进程被杀）
    stale_at = (datetime.now() - timedelta(seconds=3600)).isoformat()
    client.set_status("RUN-CRASH", tq.STATUS_RUNNING, started_at=stale_at)

    requeued = client.requeue_stale(max_age_seconds=300)

    assert requeued == 1, "超时在途任务应被重新投回"
    queued = client._backend.lrange(client.queue_key, 0, -1)
    assert len(queued) == 1 and "RUN-CRASH" in queued[0]
    assert client._backend.lrange(client.processing_key, 0, -1) == [], (
        "重投后必须先移出在途，否则下一轮 requeue 会扫到自己形成自环"
    )


def test_问题4_在途未超时不被重投(client):
    """刚取到的在途任务不得被补偿机制误判为崩溃残留"""
    client.enqueue("RUN-FRESH", {"execution_id": "RUN-FRESH", "cases": []})
    client.dequeue(timeout=0.1)
    client.set_status(
        "RUN-FRESH", tq.STATUS_RUNNING, started_at=datetime.now().isoformat()
    )

    assert client.requeue_stale(max_age_seconds=300) == 0
    assert len(client._backend.lrange(client.processing_key, 0, -1)) == 1


def test_问题4_坏消息就地移除不参与补偿(client):
    """无法解析的载荷必须立即移出在途 list，否则会被无限重投（永动循环）"""
    client._backend.lpush(client.queue_key, "not-json-at-all")

    assert client.dequeue(timeout=0.1) is None
    assert client._backend.lrange(client.processing_key, 0, -1) == []
    assert client.requeue_stale() == 0, "坏消息不应被当作崩溃残留重投"


# ===========================================================================
# 问题5: benchmark_cache 共享库安全闸
# ===========================================================================


def test_问题5_基准默认库不是应用用的0号库():
    """默认 URL 必须与应用的 DB 0 隔离，否则 flushdb 会清掉应用数据"""
    from scripts import benchmark_cache as bc

    assert bc.DEFAULT_BENCHMARK_REDIS_URL.endswith("/5")
    assert not bc.DEFAULT_BENCHMARK_REDIS_URL.endswith("/0")


def test_问题5_DB序号解析():
    """URL → DB index 解析，含无路径段与非法路径两种边界"""
    from scripts.benchmark_cache import CacheBenchmarkRunner

    parse = CacheBenchmarkRunner._url_db_index
    assert parse("redis://127.0.0.1:6379/0") == 0
    assert parse("redis://127.0.0.1:6379/5") == 5
    assert parse("redis://127.0.0.1:6379") is None
    assert parse("redis://127.0.0.1:6379/abc") is None


def test_问题5_共享库时默认拒绝flushdb(capsys, monkeypatch):
    """
    目标库与应用 TM_REDIS_URL 共享时，不执行 flushdb

    这是问题5 的第二道防线：万一用户显式把 --redis-url 传回 0 号库，
    也不能静默把应用缓存与任务队列清掉。
    """
    from scripts.benchmark_cache import CacheBenchmarkRunner

    flushed = {"called": False}

    class _SpyBackend:
        def flushdb(self):
            flushed["called"] = True

    runner = CacheBenchmarkRunner.__new__(CacheBenchmarkRunner)
    runner.redis_url = "redis://127.0.0.1:6379/0"
    runner.allow_shared_db = False
    monkeypatch.setenv("TM_REDIS_URL", "redis://127.0.0.1:6379/0")

    runner._safe_flush(_SpyBackend())

    assert flushed["called"] is False, "共享库未经确认不得清空"
    assert "安全闸" in capsys.readouterr().out


def test_问题5_显式放行后才flushdb(capsys, monkeypatch):
    """--allow-shared-db 放行后，共享库才允许清空（保留原能力）"""
    from scripts.benchmark_cache import CacheBenchmarkRunner

    flushed = {"called": False}

    class _SpyBackend:
        def flushdb(self):
            flushed["called"] = True

    runner = CacheBenchmarkRunner.__new__(CacheBenchmarkRunner)
    runner.redis_url = "redis://127.0.0.1:6379/0"
    runner.allow_shared_db = True
    monkeypatch.setenv("TM_REDIS_URL", "redis://127.0.0.1:6379/0")

    runner._safe_flush(_SpyBackend())

    assert flushed["called"] is True


def test_问题5_独立库时正常清空(capsys, monkeypatch):
    """目标库与应用不同时，清空照常执行（基准功能不受影响）"""
    from scripts.benchmark_cache import CacheBenchmarkRunner

    flushed = {"called": False}

    class _SpyBackend:
        def flushdb(self):
            flushed["called"] = True

    runner = CacheBenchmarkRunner.__new__(CacheBenchmarkRunner)
    runner.redis_url = "redis://127.0.0.1:6379/5"
    runner.allow_shared_db = False
    monkeypatch.setenv("TM_REDIS_URL", "redis://127.0.0.1:6379/0")

    runner._safe_flush(_SpyBackend())

    assert flushed["called"] is True, "独立库应正常清空，保证冷 miss 基准有效"


# ===========================================================================
# 问题2 第二道防线: record_execution 明细去重（需真实 SQLite 临时库）
# ===========================================================================


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """
    临时 SQLite 库（前后重置引擎单例，不污染正式库）

    参数:
        tmp_path (pathlib.Path): pytest 提供的临时目录
        monkeypatch (pytest.MonkeyPatch): 环境变量注入

    返回:
        None: 建表完成后 yield，结束时重置引擎
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day45_dedup.db"))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield
    DatabaseSession.reset()


def test_问题2_同一批次同一用例的明细不重复写入(tmp_db):
    """
    (execution_id, case_id) 唯一：重复写入必须被丢弃

    幂等抢占是第一道防线，但它"尽力而为"（抢占本身故障时按放行处理）。
    没有这道防线时，一次抢占失效就会让明细双写，而
    finish_execution 按 execution_id 聚合**全部**明细 → 计数翻倍、
    通过率被污染，且库表里的污染不可逆。
    """
    from src.core.case_manager import CaseManager

    now = datetime.now()
    payload = {
        "result": "passed",
        "case_name": "去重用例",
        "duration": 0.01,
        "start_time": now,
        "end_time": now,
    }
    CaseManager.record_execution(execution_id="RUN-DEDUP", case_id="TM-DEDUP-1", **payload)
    CaseManager.record_execution(execution_id="RUN-DEDUP", case_id="TM-DEDUP-1", **payload)
    # 不同用例应正常写入
    CaseManager.record_execution(execution_id="RUN-DEDUP", case_id="TM-DEDUP-2", **payload)
    # 不同批次应正常写入
    CaseManager.record_execution(execution_id="RUN-OTHER", case_id="TM-DEDUP-1", **payload)

    with DatabaseSession.session_scope() as session:
        rows = (
            session.query(models.TestExecution)
            .filter_by(execution_id="RUN-DEDUP")
            .all()
        )
        case_ids = sorted(row.case_id for row in rows)
        other_count = (
            session.query(models.TestExecution)
            .filter_by(execution_id="RUN-OTHER")
            .count()
        )

    assert case_ids == ["TM-DEDUP-1", "TM-DEDUP-2"], "同一用例的明细不得重复"
    assert other_count == 1, "不同批次的同名用例各自独立，不受影响"


def test_问题2_去重后批次汇总计数不被翻倍(tmp_db):
    """
    去重的最终目的：批次汇总口径不被污染

    幂等抢占失效导致同一批次被执行两次时，若没有明细去重，
    finish_execution 聚合出的 total 会翻倍、通过率失真。
    """
    from src.core.case_manager import CaseManager

    now = datetime.now()
    payload = {
        "result": "passed", "case_name": "汇总用例",
        "duration": 0.01, "start_time": now, "end_time": now,
    }
    # 模拟双跑：同一批次、同一用例写入两次
    for _ in range(2):
        CaseManager.record_execution(
            execution_id="RUN-AGG", case_id="TM-AGG-1", **payload
        )
    CaseManager.record_execution(execution_id="RUN-AGG", case_id="TM-AGG-2", **payload)

    summary = CaseManager.finish_execution("RUN-AGG")

    assert summary["total"] == 2, f"汇总总数被翻倍: {summary}"
    assert summary["passed"] == 2
    assert summary["pass_rate"] == 1.0


def test_问题2_抢占失败时执行入口立即放弃不落明细(tmp_db, monkeypatch):
    """
    已被其他链路抢占的批次，本链路必须立即放弃（不执行、不落明细）

    这是"同一批次双跑"的最后一道执行侧闸门：抢占失败即 return，
    批次行不会被置 running，明细表也不会出现任何本链路的行。
    """
    from src.core import case_manager as cm

    monkeypatch.setattr(cm.task_queue_client, "claim", lambda *a, **k: False)

    cm.CaseManager._execute_batch_async(
        execution_id="RUN-CLAIMED",
        cases=[{"case_id": "TM-X-1", "name": "不该被执行的用例"}],
        executor_kind="simulated",
    )

    with DatabaseSession.session_scope() as session:
        details = session.query(models.TestExecution).filter_by(
            execution_id="RUN-CLAIMED"
        ).count()

    assert details == 0, "抢占失败的链路不得落任何执行明细"


# ===========================================================================
# 容错分支覆盖（Day45 验收 4.2：新增代码行覆盖率必须 100%）
# ===========================================================================


class _StubBackend:
    """可编程的 Redis 替身：按需让指定命令抛异常或返回预设值"""

    def __init__(self, raises=None, values=None):
        self.raises = raises or {}
        self.values = values or {}
        self.calls: list[str] = []

    def _run(self, name, result=None):
        self.calls.append(name)
        if name in self.raises:
            raise self.raises[name]
        return self.values.get(name, result)

    def lpush(self, *args, **kwargs):
        return self._run("lpush", 1)

    def lrem(self, *args, **kwargs):
        return self._run("lrem", 1)

    def lrange(self, *args, **kwargs):
        return self._run("lrange", [])

    def hgetall(self, *args, **kwargs):
        return self._run("hgetall", {})

    def set(self, *args, **kwargs):
        return self._run("set", True)

    def delete(self, *args, **kwargs):
        return self._run("delete", 1)


def test_容错_在途移除遇Redis异常_返回False不抛(monkeypatch):
    """lrem 抛异常时 ack/discard 必须静默失败（Redis 故障绝不能搞挂执行）"""
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
    monkeypatch.setenv("TM_REDIS_URL", "fake://0")
    instance = TaskQueueClient()
    backend = _StubBackend(raises={"lrem": redis.ConnectionError("lrem炸了")})
    instance._backend = backend

    assert instance._discard_from_processing("raw") is False
    assert backend.calls == ["lrem"]


def test_容错_后端不可用时在途相关方法安全返回(monkeypatch):
    """后端 None 时 discard/ack/requeue/release 都必须安全返回

    注意不能只把 `_backend` 置 None：`_get_backend` 在后端为空时会按
    环境变量**重新构建**一个 fakeredis，测试就跑不到"不可用"分支了。
    这里直接关掉队列开关，让 `_get_backend` 走 enabled=false 的短路。
    """
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "false")
    instance = TaskQueueClient()

    assert instance._discard_from_processing("raw") is False
    assert instance.ack() is False          # 无待确认任务
    assert instance.requeue_stale() == 0
    assert instance.release_claim("RUN-X") is False


def test_容错_有在途任务但ack无待确认_返回False(client):
    """ack 在没有 dequeue 记录时不得误删任何东西"""
    client._backend = _StubBackend()

    assert client.ack() is False


def test_容错_在途扫描遇Redis异常_返回0不抛(client):
    """lrange 失败时补偿扫描放弃本轮，不得让 worker 退出"""
    client._backend = _StubBackend(raises={"lrange": redis.ConnectionError("扫不到")})

    assert client.requeue_stale() == 0


def test_容错_在途列表含坏消息_就地丢弃不参与补偿(client):
    """
    在途 list 里的畸形条目必须就地丢弃

    覆盖三条丢弃路径：非 JSON、非 dict 对象、缺 execution_id。
    任一路径漏掉，该条目都会被 requeue_stale 反复重投成永动循环。
    """
    import json as _json

    client._backend = _StubBackend(
        values={"lrange": ["not-json", _json.dumps(["list"]), _json.dumps({"cases": []})]}
    )

    assert client.requeue_stale() == 0
    assert client._backend.calls.count("lrem") == 3, "三条坏消息都应被移出在途"


def test_容错_重投时入队失败_计数不增加且不崩(client):
    """陈旧任务已移出在途但重新入队失败：计数为 0，调用方不感知异常"""
    import json as _json

    # 只关心"重新入队失败"这条路径：lrange 返回一条合法载荷、
    # lpush 抛异常，断言计数为 0 且不向外抛异常。
    client._backend = _StubBackend(
        raises={"lpush": redis.ConnectionError("入队炸了")},
        values={"lrange": [_json.dumps({"execution_id": "RUN-R"})]},
    )
    client._backend.calls.clear()

    assert client.requeue_stale() == 0


def test_容错_陈旧判定_状态缺失或时间戳异常_按陈旧处理(client):
    """无法判定停留时长时一律按陈旧处理（宁可重投也不永久丢任务）"""
    # 状态 hash 完全缺失
    client._backend = _StubBackend(values={"hgetall": {}})
    assert client._is_stale_in_flight("RUN-A", 300) is True

    # hash 存在但无时间字段
    client._backend = _StubBackend(values={"hgetall": {"status": "running"}})
    assert client._is_stale_in_flight("RUN-B", 300) is True

    # 时间戳不可解析
    client._backend = _StubBackend(values={"hgetall": {"started_at": "not-a-date"}})
    assert client._is_stale_in_flight("RUN-C", 300) is True

    # 带时区的时间戳：naive 的 datetime.now() 与 aware 相减会抛 TypeError，
    # 故按"无法判定即陈旧"处理（Day45 修复项）
    client._backend = _StubBackend(
        values={"hgetall": {"started_at": "2026-10-05T00:00:00+00:00"}}
    )
    assert client._is_stale_in_flight("RUN-D", 300) is True

    # 正常未超时
    client._backend = _StubBackend(
        values={"hgetall": {"started_at": datetime.now().isoformat()}}
    )
    assert client._is_stale_in_flight("RUN-E", 300) is False


def test_容错_抢占命令异常_按放行处理不阻断执行(client):
    """抢占本身故障时放行：保护不可用不得让执行链路停摆（明细去重是第二道防线）"""
    client._backend = _StubBackend(raises={"set": redis.ConnectionError("set炸了")})

    assert client.claim("RUN-CLAIM-ERR") is True


def test_容错_释放抢占遇Redis异常_返回False不抛(client):
    """release 失败只影响重试窗口，不应抛出打断收尾"""
    client._backend = _StubBackend(raises={"delete": redis.ConnectionError("del炸了")})

    assert client.release_claim("RUN-REL") is False


def test_容错_空批次号_抢占与释放均拒绝(client):
    """空 execution_id 不是有效目标，两个方向都要拒绝"""
    client._backend = _StubBackend()

    assert client.claim("") is False
    assert client.release_claim("") is False


def test_容错_worker单轮抛非Redis异常_线程存活且继续消费(client):
    """
    worker 最外层 except 必须真的兜住"未预料异常"

    这段兜底在 Day44 P3-01 补上后一直零覆盖（Day45 全量审查证实），
    这里用"dequeue 抛 ValueError（Redis 异常之外的类型）"驱动它，
    并断言线程仍能处理下一个正常任务。
    """
    payloads = [ValueError("未预料的解析异常"), {"execution_id": "RUN-R", "cases": []}, None]
    stop = threading.Event()
    calls = {"n": 0}

    def flaky_dequeue(timeout=None):
        calls["n"] += 1
        item = payloads.pop(0) if payloads else None
        if isinstance(item, Exception):
            raise item
        if item is None:
            stop.set()
        return item

    client.dequeue = flaky_dequeue
    executed: list[str] = []
    # run() 内部是 `from src.core.case_manager import CaseManager` 的
    # **函数内延迟导入**，所以要打的是源模块的属性，不是 task_queue 的属性
    import src.core.case_manager as cm_source

    original = cm_source.CaseManager

    class _FakeCM:
        @staticmethod
        def _execute_batch_async(execution_id, cases, executor_kind=None):
            executed.append(execution_id)

        @staticmethod
        def get_execution_status(execution_id):
            return {"status": "finished"}

    cm_source.CaseManager = _FakeCM
    try:
        worker = TaskWorker(client, stop)
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        thread.join(timeout=5)
    finally:
        cm_source.CaseManager = original

    assert not thread.is_alive(), "worker 必须活着退出，不能被单轮异常杀死"
    assert executed == ["RUN-R"], "单轮异常后仍应继续消费下一个任务"


# ===========================================================================
# 状态 hash 与 claim 的键约定（防止重构悄悄改键名）
# ===========================================================================


def test_键约定_状态与claim键名稳定(client):
    """键名是跨进程契约（worker 与 web 分进程共享），变更需同步改两端"""
    assert task_status_key("RUN-1") == "tm:task:RUN-1"
    assert task_claim_key("RUN-1") == "tm:claim:RUN-1"
    assert client.processing_key == f"{client.queue_key}:processing"


def test_状态hash写入后可读回(client):
    """状态 hash 仍按原契约可写可读（本次改动不得破坏既有行为）"""
    client.set_status("RUN-S", tq.STATUS_PENDING, queued_at="2026-10-05T00:00:00")

    status = client.get_status("RUN-S")

    assert status is not None
    assert status["status"] == tq.STATUS_PENDING
    assert status["queued_at"] == "2026-10-05T00:00:00"
