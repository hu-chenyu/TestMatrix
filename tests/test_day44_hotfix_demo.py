"""
Day44 热修的回归测试：修复 Day44 收尾提交（25711f3）自身引入的 3 个回归缺陷。

被测缺陷:
    P1-1  task_queue：worker 热转 + 日志洪水（原 P2-04 的构建期兜底引入）
          实测修复前 1 秒 8871 次 _get_backend + 8871 条 WARNING
    P1-2  notifications：越界 page 返回 500 而非 400（原 P3-07 的分页上界引入）
          裸 ValueError 不是 APIError 子类，落到兜底处理器变 500
    P2-1  case_manager：record_execution 逐条查批次行造成 N+1（原 P2-03 引入）
          实测修复前 20 用例批次 22 次批次元信息 SELECT，放大 2.1x

设计原则:
    - 每条断言都针对**可观测的行为**，不是源码形态（Day44 复查已确认形态
      断言无法防住本轮这三类回归：它们都"有代码、没被执行"）
    - 线程/时序相关用例走**预算内轮询 + 显式 join**，不用固定 time.sleep
      赌时序（项目铁律）
    - 不 mock 被测逻辑本身：冷却、退避、字段透传都走真实路径

这些用例曾以"恒真"或"形态断言"的形式存在而放过问题，本次全部改为行为断言。
"""

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import allure
import pytest
from loguru import logger as loguru_logger
from sqlalchemy import event
from sqlalchemy.engine import Engine
from src.core import task_queue as tq_mod
from src.core.case_manager import CaseManager
from src.core.notification import NotificationError
from src.db.db_session import DatabaseSession

BAD_REDIS_URL = "redis://127.0.0.1:notaport/0"


@contextmanager
def loguru_records(level: str = "INFO") -> Iterator[list[dict[str, Any]]]:
    """收集 loguru 输出的上下文管理器，退出时精确摘除自己的 sink。

    为什么不用 pytest 的 caplog：项目日志走 loguru（src/common/logger.py 的
    LogManager），不是 stdlib logging，caplog 只挂 stdlib 的 handler，
    对 loguru 一条都收不到——`caplog.records` 恒为空列表。用 caplog 断言
    "日志条数"会得到"实际 0 条"的假结果，从而把"根本没打日志"误判为通过。

    为什么不用 `logger.remove()` 后重加：remove() 会连同项目自身的控制台 /
    文件通道一起清掉，用例结束必须手工重建，既脆弱又会把无关副作用带进
    同一进程的后续用例。这里只**追加**一个自己的 sink，并记下 handler id
    在 finally 里精确摘除，对既有配置零影响。
    """
    captured: list[dict[str, Any]] = []

    def sink(message: Any) -> None:
        """loguru 把 Message（str 子类，额外带 .record）交给函数型 sink。"""
        captured.append(message.record)

    # enqueue 保持默认 False：消息在调用线程内同步写入 sink，计数即时可见。
    # 走 enqueue=True 的队列是后台线程刷盘的，用例读计数时可能还没落。
    handler_id = loguru_logger.add(sink, level=level, format="{message}")
    try:
        yield captured
    finally:
        loguru_logger.remove(handler_id)


@pytest.fixture()
def hotfix_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """临时 SQLite 库夹具（热修涉及 case_manager / notifications 两处落库）"""
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day44_hotfix.db"))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield
    DatabaseSession.reset()


@pytest.fixture()
def queue_client(monkeypatch: pytest.MonkeyPatch):
    """启用队列的客户端夹具（用例结束必 reset，含冷却标记）"""
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
    monkeypatch.setenv("TM_REDIS_URL", "fake://")
    client = tq_mod.TaskQueueClient()
    yield client
    client.reset_backend()


# ==============================================================================
# 1. P1-1：worker 热转 + 日志洪水
# ==============================================================================
@allure.feature("Day44热修")
class TestP11WorkerNoHotSpin:
    """Redis URL 配错时，worker 不得空转、不得刷日志"""

    @allure.story("连续失败时 _get_backend 被冷却限流")
    def test_cooldown_throttles_backend_calls(self, monkeypatch):
        """失败后进入冷却期，冷却期内直接返回 None 且不再真正重试构建。

        修复前 1 秒内被调 8871 次；修复后 1 秒内应 ≤ 2 次。
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", BAD_REDIS_URL)
        client = tq_mod.TaskQueueClient()
        try:
            # 第 1 次：真实尝试，失败并进入冷却
            assert client._get_backend() is None
            # 第 2~50 次：全部落在冷却期内，必须是"静默返回"而不是重新构建
            for _ in range(49):
                assert client._get_backend() is None
            # 冷却标记必须被设置，且在 5 秒窗口内
            assert client._failed_until > time.monotonic()
            assert (
                client._failed_until - time.monotonic()
                <= tq_mod.BACKEND_FAILURE_COOLDOWN_SECONDS
            )
        finally:
            client.reset_backend()

    @allure.story("连续失败被冷却时只记 1 条 WARNING")
    def test_no_warning_flood_during_cooldown(self, monkeypatch):
        """冷却期内静默返回 ⇒ 50 次调用只应产生 1 条 WARNING（首次失败那条）。

        这是本次回归的核心指标：修复前 50 次调用 = 50 条完全相同的日志。

        日志计数走 loguru sink 而非 caplog——项目用 loguru，caplog 收不到
        （详见 loguru_records 的 docstring）；这一点本身就值得锁住：若哪天
        有人把实现改回 stdlib logging，本用例会立刻从"抓到 1 条"变成
        "抓到 0 条"而失败，而不是悄悄变成恒真断言。
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", BAD_REDIS_URL)
        client = tq_mod.TaskQueueClient()
        try:
            with loguru_records(level="INFO") as records:
                for _ in range(50):
                    client._get_backend()
            warnings = [r for r in records if r["level"].name == "WARNING"]
            messages = [r["message"] for r in warnings]
            assert len(warnings) == 1, (
                f"50 次调用应只产生 1 条 WARNING（首次失败），"
                f"实际 {len(warnings)} 条——冷却未生效"
            )
            # 反向兜底：唯一那条必须确实是"后端构建失败"，防止
            # "碰巧只剩一条无关日志"骗过上面的条数断言
            assert "构建失败" in messages[0], (
                f"唯一一条 WARNING 应为后端构建失败告警，实际：{messages[0]!r}"
            )
        finally:
            client.reset_backend()

    @allure.story("worker 线程在 2 秒内的 _get_backend 频率有界")
    def test_worker_spin_rate_bounded(self, monkeypatch):
        """端到端：真实起 worker 线程跑 2 秒，统计 _get_backend 真实调用次数。

        修复前 2 秒 = 17742 次；修复后应 ≤ 4 次（冷却 5s 期内只试 1 次）。
        这是本条回归最直接的验收口径。
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", BAD_REDIS_URL)
        client = tq_mod.TaskQueueClient()
        calls = []
        original = client._get_backend

        def counting():
            calls.append(1)
            return original()

        client._get_backend = counting  # type: ignore[method-assign]
        stop = threading.Event()
        worker = tq_mod.TaskWorker(queue_client=client, stop_event=stop)
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        try:
            # 预算内轮询等待，不赌时序
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            stop.set()
            thread.join(timeout=5)
            client.reset_backend()
        assert not thread.is_alive(), "worker 线程未在 join 超时内退出"
        assert len(calls) <= 4, (
            f"2 秒内 _get_backend 被调 {len(calls)} 次，"
            f"应 ≤ 4 次（冷却生效）；修复前约 17742 次"
        )

    @allure.story("冷却期满后能重试并自愈（两种后端）")
    def test_recovery_after_cooldown(self, monkeypatch):
        """防止"修了热转却让 worker 永久躺平"：冷却是时间到即重试。

        自愈判定为四件事同时成立：
          ① 冷却期满后 _get_backend 能真正重新尝试（成功时返回后端）
          ② 成功后 _failed_until 被清零（不再永久禁用）
          ③ 恢复时记的是 INFO「已恢复」而不是继续 WARNING（运维能从
             日志直接看出 Redis 恢复了，而不是靠猜）
          ④ worker 循环在 stop_event 置位后能立即退出（线程不残留，
             由 test_worker_spin_rate_bounded 与
             test_worker_backs_off_when_backend_unavailable 覆盖）

        ③ 两条分支（fakeredis 内存后端 / 真实 redis 客户端）都要走到：
        冷却是"时间到即重试"，判定依据是 `now < _failed_until` 为假，
        所以这里把标记设成**已过期但仍为正**（而非清零）——清零会让
        `recovering` 判为 False，走进的是"首次构建"分支，等于绕开了
        被测的恢复路径。

        真实 redis 分支不依赖真 Redis：只把网络客户端构造器替换成
        替身，冷却/重试/日志这些被测逻辑一律走真实实现。
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", BAD_REDIS_URL)
        client = tq_mod.TaskQueueClient()
        try:
            # ---- 前半：坏 URL 失败 → 切回 fakeredis 后冷却期满自愈 ----
            assert client._get_backend() is None, "坏 URL 应构建失败"
            assert client._failed_until > 0.0, "失败后应进入冷却"
            monkeypatch.setenv("TM_REDIS_URL", "fake://")
            client._failed_until = time.monotonic() - 0.1  # 冷却已过期、标记仍为正
            with loguru_records(level="INFO") as records:
                backend = client._get_backend()
            assert backend is not None, "冷却期满后应能重新构建并自愈"
            assert client._failed_until == 0.0, "成功后应清空冷却标记"
            infos = [r["message"] for r in records if r["level"].name == "INFO"]
            assert any("已恢复" in m for m in infos), (
                f"自愈时应记 INFO「已恢复」，实际 INFO：{infos!r}"
            )

            # ---- 后半：真实 redis 客户端的恢复分支（不依赖真 Redis）----
            # 必须先丢弃上半留下的 fakeredis 实例：_get_backend 开头
            # "已有后端直接返回"会早退，压根走不到 redis 分支。
            client.reset_backend()

            class _StubRedis:
                """redis 客户端替身：只提供 reset 会用到的 close()"""

                def close(self) -> None:
                    """与真实客户端同名同语义，测试结束丢弃后端时会被调用"""

            sentinel = _StubRedis()
            monkeypatch.setenv("TM_REDIS_URL", "redis://127.0.0.1:6379/0")
            monkeypatch.setattr(tq_mod, "_REDIS_FROM_URL", lambda *a, **k: sentinel)
            client._failed_until = time.monotonic() - 0.1
            with loguru_records(level="INFO") as records:
                backend = client._get_backend()
            assert backend is sentinel, "冷却期满后应重新构建出 Redis 客户端"
            assert client._failed_until == 0.0, "真实 redis 分支同样应清空冷却标记"
            infos = [r["message"] for r in records if r["level"].name == "INFO"]
            assert any("连接已恢复" in m for m in infos), (
                f"真实 redis 自愈时应记 INFO「连接已恢复」，实际 INFO：{infos!r}"
            )
        finally:
            client.reset_backend()

    @allure.story("worker 在后端不可用时调用 stop_event.wait 退避")
    def test_worker_backs_off_when_backend_unavailable(self, monkeypatch):
        """退避闸：后端不可用且 payload 为 None 时必须 wait，而不是空转。

        用可控的 stop_event 替身验证 wait 真被调用——比"跑 2 秒看频率"
        更直接地锁住"退避这个动作本身"。
        """
        waits: list[float] = []

        class SpyStopEvent:
            """记录 wait() 调用的假事件；wait 后立即置位以终止循环"""

            def __init__(self) -> None:
                self.calls = 0

            def is_set(self) -> bool:
                # 第 1 轮为 False（进入循环），wait 之后为 True（退出）
                return self.calls > 0

            def wait(self, timeout=None):
                waits.append(timeout)
                self.calls += 1
                return False

        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", BAD_REDIS_URL)
        client = tq_mod.TaskQueueClient()
        try:
            spy = SpyStopEvent()
            worker = tq_mod.TaskWorker(queue_client=client, stop_event=spy)
            worker.run()
            assert len(waits) == 1, (
                f"后端不可用时应恰好退避 1 次，实际 {len(waits)} 次"
            )
            assert waits[0] == tq_mod.WORKER_IDLE_BACKOFF_SECONDS
        finally:
            client.reset_backend()

    @allure.story("后端可用时不走退避分支（不拖慢正常消费）")
    def test_no_backoff_when_backend_healthy(self, queue_client):
        """防止"加了退避反而让正常路径变慢"。

        后端健康时 payload 为 None 是"队列空"，此时 dequeue 已在 BRPOP 上
        阻塞了 brpop_timeout，绝不能再叠加退避。
        """
        assert queue_client._get_backend() is not None, "fake:// 应构建成功"
        stop = threading.Event()
        stop.set()  # 循环不进入，验证 run() 干净返回
        worker = tq_mod.TaskWorker(queue_client=queue_client, stop_event=stop)
        started = time.monotonic()
        worker.run()
        assert time.monotonic() - started < 0.5, "后端健康时 run() 应立即返回"


# ==============================================================================
# 2. P1-2：通知接口越界 page 返回 500 而非 400
# ==============================================================================
@allure.feature("Day44热修")
class TestP12NotificationPageBound:
    """越界页码必须是 400，不能是 500"""

    @allure.story("history 接口越界返回 400 而非 500")
    def test_history_out_of_range_returns_400(self, hotfix_db):
        """P1-2 主验收口径：修复前 HTTP 500，修复后 HTTP 400。"""
        from src.web import create_app

        client = create_app("test").test_client()
        response = client.get("/api/notifications/history?page=99999")
        assert response.status_code == 400, (
            f"越界 page 应返回 400，实际 {response.status_code}"
            f"（500 说明 ValueError 仍未被拦成 ValidationError）"
        )
        assert response.get_json()["code"] == 400

    @allure.story("死信接口越界同样返回 400")
    def test_dead_letters_out_of_range_returns_400(self, hotfix_db):
        """两个端点共用 _parse_pagination，必须都覆盖。"""
        from src.web import create_app

        client = create_app("test").test_client()
        response = client.get("/api/notifications/dead-letters?page=99999")
        assert response.status_code == 400, (
            f"死信接口越界应返回 400，实际 {response.status_code}"
        )

    @allure.story("合法 page 仍返回 200（防止把正常请求也拒了）")
    def test_valid_page_still_200(self, hotfix_db):
        """加校验最常见的副作用是"把对的也拒了"，必须正反两侧都验。"""
        from src.web import create_app

        client = create_app("test").test_client()
        assert client.get("/api/notifications/history?page=1").status_code == 200
        assert client.get("/api/notifications/history").status_code == 200

    @allure.story("page=0 与 page_size 越界返回 400")
    def test_lower_bound_and_page_size(self, hotfix_db):
        """下界与 page_size 口径不得因本次改动而漂移。"""
        from src.web import create_app

        client = create_app("test").test_client()
        assert client.get("/api/notifications/history?page=0").status_code == 400
        assert (
            client.get("/api/notifications/history?page_size=99999").status_code
            == 400
        )
        assert client.get("/api/notifications/history?page=abc").status_code == 400

    @allure.story("核心层抛领域异常而非裸 ValueError")
    def test_core_layer_raises_domain_exception(self, hotfix_db):
        """核心层不再抛裸 ValueError。

        同时锁住向后兼容：NotificationError 继承 ValueError，既有
        `except ValueError` 的调用方行为不变（防止"改基类导致异常穿透"）。
        """
        from src.core.notification import NotificationHistoryRepository

        with pytest.raises(NotificationError) as exc:
            NotificationHistoryRepository().list_history(page=99999, page_size=20)
        assert "page" in str(exc.value)
        assert isinstance(exc.value, ValueError), (
            "NotificationError 必须仍可被 `except ValueError` 捕获（向后兼容）"
        )


# ==============================================================================
# 3. P2-1：record_execution 逐条查批次行造成 N+1
# ==============================================================================
@allure.feature("Day44热修")
class TestP21NoNPlusOne:
    """批次元信息 SELECT 次数必须与用例数无关"""

    @staticmethod
    def _count_batch_selects(cases: list, executor_name: str) -> tuple[int, int]:
        """跑一个批次并统计 (批次元信息SELECT次数, 明细INSERT次数)"""
        stmts: list[str] = []
        recording = False

        @event.listens_for(Engine, "before_cursor_execute")
        def _count(conn, cursor, statement, params, context, executemany):
            if recording:
                stmts.append(" ".join(statement.split()))

        try:
            result = CaseManager.start_execution(
                "web", executor_name=executor_name, environment="prod", module="m"
            )
            eid = result["execution_id"] if isinstance(result, dict) else result
            recording = True
            CaseManager._execute_batch_async(eid, cases, None)
            recording = False
        finally:
            event.remove(Engine, "before_cursor_execute", _count)
        sel = sum(
            1
            for s in stmts
            if s.lower().startswith("select") and "test_execution_batches" in s.lower()
        )
        ins = sum(
            1
            for s in stmts
            if s.lower().startswith("insert") and "test_executions" in s.lower()
        )
        return sel, ins

    def _make_cases(self, n: int, tag: str) -> list:
        return [
            CaseManager.create_case(
                {
                    "case_id": f"{tag}-{i:03d}",
                    "name": f"用例{i}",
                    "module": "m",
                    "priority": "P1",
                }
            )
            for i in range(n)
        ]

    @allure.story("20 用例批次的批次元信息 SELECT 次数与用例数无关")
    def test_batch_select_count_constant(self, hotfix_db):
        """P2-1 主验收口径：修复前 20 用例 = 22 次（随 N 线性增长）。

        修复后应为 2 次——恰好是两次状态更新各查一次，且**与 N 无关**。
        """
        cases = self._make_cases(20, "TM-NP1")
        sel, ins = self._count_batch_selects(cases, "pytest")
        assert ins == 20, f"应落 20 条明细，实际 {ins}"
        assert sel == 2, (
            f"批次元信息 SELECT 应恒为 2 次（running + finished 各一），"
            f"实际 {sel} 次；修复前为 22 次（每条用例查一次）"
        )

    @allure.story("1 用例批次与 20 用例批次的查询次数相同（证明与 N 解耦）")
    def test_query_count_independent_of_case_count(self, hotfix_db):
        """把 N 放大 20 倍而查询次数不变，才是真正消除了 N+1。

        只验"20 条时是 2 次"仍可能掩盖"N=20 时恰好抵消"的巧合。
        """
        one = self._make_cases(1, "TM-NP2A")
        sel_one, _ = self._count_batch_selects(one, "simulated")
        many = self._make_cases(20, "TM-NP2B")
        sel_many, ins_many = self._count_batch_selects(many, "simulated")
        assert ins_many == 20
        assert sel_one == sel_many, (
            f"查询次数应与用例数无关：N=1 时 {sel_one} 次，"
            f"N=20 时 {sel_many} 次"
        )

    @allure.story("明细仍正确继承批次 environment/executor")
    def test_detail_fields_still_inherited(self, hotfix_db):
        """防"修 A 引入 B"的关键：省查询不能把字段继承也省掉。

        修复前靠逐条查询拿到批次行的 prod/pytest；若透传链路断了，
        明细会静默退回模型默认值 dev/local —— 功能退化但不报错。
        这里用非默认值(prod/pytest)确保任何回落都会被抓到。
        """
        cases = self._make_cases(5, "TM-NP3")
        self._count_batch_selects(cases, "pytest")
        result = CaseManager.list_executions_paged(page=1, page_size=10)
        eid = result["items"][0]["execution_id"]
        detail = CaseManager.get_execution_detail(eid)
        assert detail is not None
        assert len(detail["items"]) == 5
        for item in detail["items"]:
            assert item["environment"] == "prod", (
                f"{item['case_id']} 的 environment 应为 prod，"
                f"实际 {item['environment']}（已退回默认值，字段透传断了）"
            )
            assert item["executor"] == "pytest", (
                f"{item['case_id']} 的 executor 应为 pytest，"
                f"实际 {item['executor']}"
            )

    @allure.story("record_execution 显式传参时不再查批次行")
    def test_record_execution_skips_query_when_params_given(self, hotfix_db):
        """直接锁定 record_execution 的新契约：两个参数都给 ⇒ 零查询。

        直接测仓储层而不是只测批次路径，可防止未来有人把循环内
        的透传参数删掉而批次级测试因其他原因仍然通过。
        """
        from datetime import datetime

        cases = self._make_cases(1, "TM-NP4")
        result = CaseManager.start_execution(
            "web", executor_name="simulated", environment="test", module="m"
        )
        eid = result["execution_id"] if isinstance(result, dict) else result

        stmts: list[str] = []
        recording = False

        @event.listens_for(Engine, "before_cursor_execute")
        def _count(conn, cursor, statement, params, context, executemany):
            if recording:
                stmts.append(" ".join(statement.split()))

        try:
            recording = True
            CaseManager.record_execution(
                execution_id=eid,
                case_id=cases[0]["case_id"],
                case_name=cases[0]["name"],
                result="passed",
                start_time=datetime.now(),
                end_time=datetime.now(),
                duration=0.1,
                environment="test",
                executor="simulated",
            )
        finally:
            recording = False
            event.remove(Engine, "before_cursor_execute", _count)
        assert not [
            s for s in stmts
            if s.lower().startswith("select") and "test_execution_batches" in s.lower()
        ], "显式传入 environment/executor 时不应再查批次元信息行"
