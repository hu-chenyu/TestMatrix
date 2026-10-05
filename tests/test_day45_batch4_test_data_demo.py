"""
Day45 全量 bug 审查第 4 批修复的配套测试（测试与数据）

对应 4 项被修复的问题（每条都有"回退即变红"的变异验证）:
    问题1 (P1) 常量断言冒充行为覆盖，SSE 收流与通知预算实为"永不验证"
        → 删不掉常量断言（它们仍是廉价回归网），但补上真实行为测试：
          通知预算耗尽/充足、SSE 存活上限到期后名额归还
    问题2 (P1) 事件总线历史环淘汰后静默丢事件，运行中批次拿不到 DB 重建兜底
        → EventChannel.detect_gap + subscribe 首帧 stream_reset 告知缺失区间
    问题3 (P2) 批次 failed 分支只改状态不聚合 → 批次从列表彻底消失
        → 提取 _aggregate_execution_results，failed 分支同样写 defect_statistics
    问题4 (P3) 批次号 strip 口径不统一（6 个入口各写各的）
        → 提取 _normalize_batch_id，全部落库入口统一调用

问题1 的 SSE 半边说明：Day45 第 3 批 P2-2 已把存活上界判定移到每轮循环，
并配套两条行为测试（事件持续到达 / 空闲流走心跳分支），本批不再重复，
只补一条第 3 批没有的：**存活上限到期后 SSE 名额被归还**。

测试链路铁律:
    - 零 skip / 零 xfail / 不断言 True
    - 不 mock 被测函数本身（只 mock 其外部依赖：执行器、渠道、sleeper）
    - 占位值一律 PLACEHOLDER_*，不构造形如真密钥的串
    - 时序用轮询/上限，不靠固定 sleep 赌时序
"""

import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core import notification as notif  # noqa: E402
from src.core.event_bus import (  # noqa: E402
    HISTORY_RING_MAXLEN,
    STREAM_RESET_EVENT_TYPE,
    EventChannel,
    ExecutionEvent,
)
from src.web.routes import executions as ex  # noqa: E402

PLACEHOLDER_ADDR = "203.0.113.7"


# ===========================================================================
# 问题 1：常量断言冒充行为覆盖 → 真实行为测试
# ===========================================================================


class _AlwaysFailNotifier(notif.BaseNotifier):
    """恒定发送失败的渠道桩（内部测试辅助类）"""

    channel_name = "stub-fail"

    def __init__(self) -> None:
        self.calls = 0

    def is_enabled(self) -> bool:
        return True

    def send(self, notification) -> bool:
        """恒返回 False（契约上 send 返回 True 才算成功）"""
        self.calls += 1
        return False


class _AlwaysOkNotifier(notif.BaseNotifier):
    """恒定发送成功的渠道桩（内部测试辅助类）"""

    channel_name = "stub-ok"

    def __init__(self) -> None:
        self.calls = 0

    def is_enabled(self) -> bool:
        return True

    def send(self, notification) -> bool:
        """恒返回 True"""
        self.calls += 1
        return True


def _make_notification() -> notif.Notification:
    """构造一条占位通知（不构造形似真密钥的串）"""
    return notif.Notification(
        title="PLACEHOLDER 标题",
        content="PLACEHOLDER 正文",
    )


def test_问题1_通知预算耗尽后提前收尾(monkeypatch):
    """
    预算耗尽必须提前收尾，把剩余重试机会让给后续批次

    修复前这里只有 `assert notif.MAX_SEND_BUDGET_SECONDS == 30.0`——把常量
    改成 1e-9（等效关闭）测试照样过。判据必须是行为：base_delay 大于预算
    时，第一次退避被夹到"剩余预算"，睡满预算后下一次判定即触发提前收尾，
    尝试次数**少于** 1+max_retries。
    """
    monkeypatch.setattr(notif, "MAX_SEND_BUDGET_SECONDS", 1.0)
    waits: list[float] = []
    notifier = _AlwaysFailNotifier()
    router = notif.NotificationRouter(
        notifiers=[notifier],
        max_retries=3,
        base_delay=10.0,  # 远超预算 → 第一次退避即被夹到剩余预算
        use_jitter=False,
        sleeper=waits.append,  # 记录型 sleeper，零真实等待
    )

    ok, attempts, reason = router._send_with_retry(notifier, _make_notification())

    assert ok is False, "渠道恒失败却报成功"
    assert attempts == 2, (
        f"预算 1.0s / 基准退避 10s 时应第2次就耗尽预算收尾，"
        f"实得 {attempts} 次尝试（1+max_retries=4）"
    )
    assert "发送预算耗尽提前收尾" in reason, f"未如实记账预算耗尽: {reason!r}"
    assert waits == [1.0], (
        f"单次退避应被夹到剩余预算 1.0s（不得睡满基准 10s），实得 {waits}"
    )
    assert notifier.calls == 2, "提前收尾后不得再发起第三次尝试"


def test_问题1_通知预算充足时按满额重试(monkeypatch):
    """预算充足时不误收尾（不回归）：走满 1+max_retries 次并正常失败返回"""
    monkeypatch.setattr(notif, "MAX_SEND_BUDGET_SECONDS", 30.0)
    waits: list[float] = []
    notifier = _AlwaysFailNotifier()
    router = notif.NotificationRouter(
        notifiers=[notifier],
        max_retries=3,
        base_delay=0.1,
        use_jitter=False,
        sleeper=waits.append,
    )

    ok, attempts, reason = router._send_with_retry(notifier, _make_notification())

    assert ok is False
    assert attempts == 4, f"预算充足时应走满 1+max_retries=4 次，实得 {attempts}"
    assert notifier.calls == 4
    assert "发送预算耗尽" not in reason, f"预算充足却误判耗尽: {reason!r}"
    # 退避序列 base×2^k = 0.1/0.2/0.4，共3次等待
    assert waits == [0.1, 0.2, 0.4], f"退避序列不对: {waits}"


def test_问题1_通知预算充足时成功即收手(monkeypatch):
    """预算是"失败重试"的封顶，不该拖累成功路径：首次成功零等待"""
    monkeypatch.setattr(notif, "MAX_SEND_BUDGET_SECONDS", 30.0)
    waits: list[float] = []
    notifier = _AlwaysOkNotifier()
    router = notif.NotificationRouter(
        notifiers=[notifier],
        max_retries=3,
        base_delay=10.0,  # 基准退避大于预算：若预算逻辑误伤成功路径会暴露
        use_jitter=False,
        sleeper=waits.append,
    )

    ok, attempts, reason = router._send_with_retry(notifier, _make_notification())

    assert ok is True, f"首次成功却报失败: {reason!r}"
    assert attempts == 1
    assert waits == [], f"成功路径不得发生任何等待，实得 {waits}"


def test_问题1_SSE存活上限到期后名额被归还(exec_client, monkeypatch):
    """
    流因存活上限被主动收流时，并发名额必须归还

    这是第 3 批没覆盖的角度：名额归还的三条路径里，生成器 finally 走的
    是"正常收流"，而存活上限到期是 `return` 提前退出——路径相同但很
    容易在后续重构中被改成 break/异常而漏掉归还。
    """
    client, eid = exec_client
    monkeypatch.setattr(ex, "SSE_MAX_LIFETIME_SECONDS", 0.3)
    monkeypatch.setattr(ex, "HEARTBEAT_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(
        ex.CaseManager,
        "get_execution_status",
        lambda _eid: {
            "execution_id": _eid, "status": "running", "total_cases": 2,
            "passed": 0, "failed": 0, "error": 0, "skipped": 0,
            "pass_rate": 0.0, "start_time": None, "end_time": None,
            "duration": 0.0,
        },
    )
    ex._SSE_ACTIVE_CONNECTIONS.clear()

    from src.core.event_bus import get_channel

    get_channel(eid, create=True)  # 空通道，只走心跳节拍
    body = client.get(f"/api/executions/{eid}/events").get_data(as_text=True)

    assert "stream-lifetime-exceeded" in body, "存活上限未触发收流"
    assert ex._SSE_ACTIVE_CONNECTIONS == {}, (
        f"因存活上限收流后名额未归还: {dict(ex._SSE_ACTIVE_CONNECTIONS)}"
    )


# ===========================================================================
# 问题 2：事件总线历史环缺口必须显式告知
# ===========================================================================


def _fill_ring(channel: EventChannel, total: int) -> None:
    """往通道灌入 total 条事件（内部辅助函数）"""
    for seq in range(total):
        channel.publish(
            ExecutionEvent(event_type="case_finished", data={"seq": seq})
        )


def test_问题2_游标早于最旧存活事件时判定有缺口():
    """缺口区间必须精确：游标 2、最旧存活 6 → 缺失 3~5"""
    channel = EventChannel()
    _fill_ring(channel, HISTORY_RING_MAXLEN + 5)

    gap = channel.detect_gap(2)

    oldest_id = HISTORY_RING_MAXLEN + 5 - HISTORY_RING_MAXLEN + 1
    assert oldest_id == 6, f"最旧存活事件id推算有误: {oldest_id}"
    assert gap == (3, 5), f"缺口区间应为(3,5)，实得 {gap}"


def test_问题2_游标恰好接上最旧事件不算缺口():
    """游标 == 最旧id-1 时下一条就是最旧存活事件，不该误报缺口"""
    channel = EventChannel()
    _fill_ring(channel, HISTORY_RING_MAXLEN + 5)
    oldest_id = 6

    assert channel.detect_gap(oldest_id - 1) is None, "游标恰好接上却被判为缺口"
    assert channel.detect_gap(oldest_id) is None, "游标已追平却被判为缺口"
    assert channel.detect_gap(oldest_id + 5) is None, "游标领先却被判为缺口"
    assert channel.detect_gap(None) is None, "全量回放（None）不该判为缺口"


def test_问题2_空通道不判缺口():
    """空通道 + 任意游标：没有历史就没有缺口，不能凭空报错"""
    channel = EventChannel()

    assert channel.detect_gap(1) is None
    assert channel.detect_gap(99999) is None


def test_问题2_首帧有缺口时发出stream_reset():
    """
    极旧游标重连必须先收到 stream_reset 告知缺失区间

    修复前直接从最旧存活事件续传，event_id 连续递增、**丢事件这件事
    没有任何信号**，排障会误判为"用例没被执行"。
    """
    channel = EventChannel()
    _fill_ring(channel, HISTORY_RING_MAXLEN + 5)
    channel.close(reason="test")

    events = list(channel.subscribe(last_event_id=2))

    resets = [e for e in events if e.event_type == STREAM_RESET_EVENT_TYPE]
    assert len(resets) == 1, f"应恰好收到1帧stream_reset，实得 {len(resets)}"
    reset = resets[0]
    assert reset.event_id is None, (
        "stream_reset 不占 event_id（否则会把客户端游标推到不存在的序号）"
    )
    assert reset.data["missing_from"] == 3
    assert reset.data["missing_to"] == 5
    assert reset.data["missing_count"] == 3
    assert reset.data["oldest_available_id"] == 6
    # 缺口帧之后仍正常续传（连接不被关闭，实时事件继续有效）
    replayed = [e for e in events if e.event_type != STREAM_RESET_EVENT_TYPE]
    assert len(replayed) == HISTORY_RING_MAXLEN, (
        f"真实事件回放数应仍为 maxlen，实得 {len(replayed)}"
    )
    assert replayed[0].event_id == 6, "应从最旧存活事件续传"
    assert replayed[-1].event_id == HISTORY_RING_MAXLEN + 5, "应一直读到最新"


def test_问题2_无缺口时不发stream_reset():
    """游标在窗口内：正常续传，不得凭空插入 stream_reset 干扰客户端"""
    channel = EventChannel()
    _fill_ring(channel, 10)
    channel.close(reason="test")

    events = list(channel.subscribe(last_event_id=4))

    assert not [e for e in events if e.event_type == STREAM_RESET_EVENT_TYPE], (
        "窗口内游标不该收到 stream_reset"
    )
    assert [e.event_id for e in events] == [5, 6, 7, 8, 9, 10]


def test_问题2_stream_reset不占合法发布事件类型():
    """
    stream_reset 刻意不进 VALID_EVENT_TYPES

    那个元组是 `publish` 的入参白名单，而 stream_reset 由 subscribe
    合成、不经过 publish；放进去等于允许外部伪造不占 event_id 的伪事件。
    """
    from src.core import event_bus

    assert STREAM_RESET_EVENT_TYPE not in event_bus.VALID_EVENT_TYPES, (
        "stream_reset 混进了 publish 白名单，外部可伪造伪事件"
    )
    channel = EventChannel()
    channel.publish(
        ExecutionEvent(event_type=STREAM_RESET_EVENT_TYPE, data={})
    )
    assert channel.snapshot() == [], (
        "publish(stream_reset) 应被白名单拒绝并丢弃，不入历史环"
    )


def test_问题2_路由层把stream_reset成帧且不推进游标(exec_client, monkeypatch):
    """
    端到端：缺口帧经 SSE 接口发出，且不输出 id 行

    输出 id 行会把客户端 lastEventId 推进到一个不对应任何真实事件的
    序号，下次重连的断点过滤会从这里起算——与本项目既有的"降级帧不
    推进游标"约定（v3 修复 V2-P2-2）是同一条纪律。

    判据里为什么必须包含**运维告警日志**这条：stream_reset 的 event_id
    是 None，通用渲染路径 `_format_sse_frame(event_type, data,
    event_id=None)` 本来就会正确省略 id 行——路由层那个特判分支对
    "帧长什么样"没有任何影响，只负责打一条 WARNING。只断言帧的话，
    把特判整段删掉测试照样绿（变异存活），等于养了一段没人验证的代码。
    """
    from src.common.logger import LogManager

    client, eid = exec_client
    monkeypatch.setattr(
        ex.CaseManager,
        "get_execution_status",
        lambda _eid: {
            "execution_id": _eid, "status": "running", "total_cases": 2,
            "passed": 0, "failed": 0, "error": 0, "skipped": 0,
            "pass_rate": 0.0, "start_time": None, "end_time": None,
            "duration": 0.0,
        },
    )
    monkeypatch.setattr(ex, "SSE_MAX_LIFETIME_SECONDS", 0.3)
    monkeypatch.setattr(ex, "HEARTBEAT_INTERVAL_SECONDS", 0.0)
    ex._SSE_ACTIVE_CONNECTIONS.clear()

    from src.core.event_bus import get_channel

    channel = get_channel(eid, create=True)
    _fill_ring(channel, HISTORY_RING_MAXLEN + 5)
    channel.close(reason="test")

    from src.web.routes import executions as executions_mod

    # 制造极旧游标：Last-Event-ID 指向远早于最旧存活事件的位置
    monkeypatch.setattr(
        executions_mod, "_parse_last_event_id_header",
        lambda: (executions_mod.EVENT_ID_NAMESPACE_LIVE, 2),
    )

    logger = LogManager.get_logger()
    lines: list[str] = []
    sink_id = logger.add(
        lambda message: lines.append(str(message)), level="INFO", format="{message}"
    )
    try:
        body = client.get(f"/api/executions/{eid}/events").get_data(as_text=True)
    finally:
        logger.remove(sink_id)

    assert f"event: {STREAM_RESET_EVENT_TYPE}" in body, (
        f"路由层未把 stream_reset 成帧，body={body[:200]!r}"
    )
    reset_frame = body.split(f"event: {STREAM_RESET_EVENT_TYPE}")[1].split("\n\n")[0]
    assert "id:" not in reset_frame, (
        f"stream_reset 帧输出了 id 行，会推进客户端游标: {reset_frame!r}"
    )
    assert '"missing_from": 3' in reset_frame, f"缺口区间未随帧下发: {reset_frame!r}"
    assert any(
        "SSE订阅存在历史缺口" in line for line in lines
    ), (
        "路由层未就历史缺口打运维告警 —— 缺口只有服务端日志可查，"
        "客户端之外再无任何信号"
    )


# ===========================================================================
# 问题 3：failed 分支必须同样聚合
# ===========================================================================


class _PassThenRaiseExecutor:
    """前 k 条通过、之后抛异常的桩执行器（内部测试辅助类）"""

    def __init__(self, fail_after: int) -> None:
        self.fail_after = fail_after
        self.seen = 0

    def run_one(self, case: dict):
        """按 fail_after 决定返回通过结果还是抛异常"""
        from src.core.executors import ExecutionResult

        self.seen += 1
        if self.seen > self.fail_after:
            raise RuntimeError(f"桩执行器在第 {self.fail_after} 条后崩溃")
        return ExecutionResult(result="passed", error_message=None, duration=0.001)


def test_问题3_failed分支写入defect_statistics(exec_client, monkeypatch):
    """
    第 k 条用例抛异常时，前 k-1 条明细的统计必须落库

    修复前 failed 分支只 `_update_batch_status(status="failed")`，
    defect_statistics 零行；而 list_executions_paged 只读该表，
    批次于是从列表彻底消失。
    """
    from src.core.case_manager import CaseManager
    from src.db import models
    from src.db.db_session import DatabaseSession as DB

    client, _ = exec_client
    result = CaseManager.start_execution(trigger="web", case_type="api")
    eid = result["execution_id"]
    cases = result["cases"]
    assert len(cases) >= 2, "需要至少2条用例才能构造中途崩溃"

    executor = _PassThenRaiseExecutor(fail_after=1)
    monkeypatch.setattr(
        "src.core.case_manager.get_executor", lambda kind=None: executor
    )
    CaseManager._execute_batch_async(eid, cases)

    status = CaseManager.get_execution_status(eid)
    assert status["status"] == "failed", "桩执行器崩溃后批次应为failed"

    DB.reset()
    session = DB.get_session()
    try:
        row = (
            session.query(models.DefectStatistic)
            .filter_by(execution_id=eid)
            .first()
        )
        assert row is not None, (
            "failed 批次没有写 defect_statistics —— "
            "该表是 list_executions_paged 的唯一数据源，批次会从列表消失"
        )
        assert row.total_cases == 1, (
            f"应只统计崩溃前已落库的 1 条明细，实得 {row.total_cases}"
        )
        assert row.passed == 1
    finally:
        DB.reset()


def test_问题3_failed批次出现在列表中且统计正确(exec_client, monkeypatch):
    """端到端：failed 批次在批次列表里可见，计数不是全 0"""
    from src.core.case_manager import CaseManager

    client, _ = exec_client
    result = CaseManager.start_execution(trigger="web", case_type="api")
    eid = result["execution_id"]
    cases = result["cases"]

    monkeypatch.setattr(
        "src.core.case_manager.get_executor",
        lambda kind=None: _PassThenRaiseExecutor(fail_after=1),
    )
    CaseManager._execute_batch_async(eid, cases)

    listed = CaseManager.list_executions_paged(page=1, page_size=50)
    row = next(
        (r for r in listed["items"] if r.get("execution_id") == eid), None
    )
    assert row is not None, (
        f"failed 批次 {eid} 未出现在 list_executions_paged 列表中"
    )
    assert row["passed"] == 1, f"列表中通过数应为 1，实得 {row.get('passed')}"
    assert row["status"] == "failed", "列表中批次状态应为failed"

    # 状态接口直查批次行的冗余统计，不聚合 → 不回写就会一直显示全 0
    status = CaseManager.get_execution_status(eid)
    assert status["passed"] == 1, (
        f"get_execution_status 直查批次行，failed 分支未回写冗余统计: {status}"
    )


def test_问题3_首条即崩溃时不写汇总行也不影响终态(exec_client, monkeypatch):
    """
    异常发生在第一条用例之前：无可聚合明细，属正常降级

    此时不得抛错打断收尾（外层 except 块内裸抛会杀死 daemon 线程，
    连带跳过终态事件与失败通知），批次仍须落到 failed 终态。
    """
    from src.core.case_manager import CaseManager
    from src.db import models
    from src.db.db_session import DatabaseSession as DB

    client, _ = exec_client
    result = CaseManager.start_execution(trigger="web", case_type="api")
    eid = result["execution_id"]
    cases = result["cases"]

    monkeypatch.setattr(
        "src.core.case_manager.get_executor",
        lambda kind=None: _PassThenRaiseExecutor(fail_after=0),
    )
    CaseManager._execute_batch_async(eid, cases)

    status = CaseManager.get_execution_status(eid)
    assert status["status"] == "failed", "首条崩溃后批次仍须落到failed终态"

    DB.reset()
    session = DB.get_session()
    try:
        assert (
            session.query(models.DefectStatistic)
            .filter_by(execution_id=eid)
            .first()
        ) is None, "无明细的批次不该凭空造出汇总行"
    finally:
        DB.reset()


def test_问题3_正常完成分支聚合不受影响(exec_client, monkeypatch):
    """finished 分支仍走同一条聚合逻辑且结果正确（不回归）"""
    from src.core.case_manager import CaseManager

    client, _ = exec_client
    result = CaseManager.start_execution(trigger="web", case_type="api")
    eid = result["execution_id"]

    monkeypatch.setattr(
        "src.core.case_manager.get_executor",
        lambda kind=None: _PassThenRaiseExecutor(fail_after=99),
    )
    CaseManager._execute_batch_async(eid, result["cases"])

    status = CaseManager.get_execution_status(eid)
    assert status["status"] == "finished", "全通过批次应为finished"
    assert status["total_cases"] == len(result["cases"])
    assert status["passed"] == len(result["cases"])
    assert status["pass_rate"] == 1.0


# ===========================================================================
# 问题 4：批次号归一口径统一
# ===========================================================================


def test_问题4_带空白批次号可完成整条链路(temp_db, monkeypatch):
    """
    `" RUN-X "` 必须与 `"RUN-X"` 完全等价

    修复前只有 record_execution 做 strip：明细按干净号落库，而
    finish/_update_batch_status 按带空格号查询 → 查不到、warning 返回
    None 静默丢状态更新，最后 finish_execution 抛"批次不存在"、整批判
    failed。这条用例把"带空白号查询的结果与干净号逐一相同"钉死。
    """
    from src.core.case_manager import CaseManager

    result = CaseManager.start_execution(trigger="web", case_type="api")
    eid = result["execution_id"]
    cases = result["cases"]
    padded = f"  {eid}  "

    monkeypatch.setattr(
        "src.core.case_manager.get_executor",
        lambda kind=None: _PassThenRaiseExecutor(fail_after=99),
    )
    # 批次按干净号创建；下面一律用带空白号去查，两端口径必须一致
    CaseManager._execute_batch_async(eid, cases)

    status_padded = CaseManager.get_execution_status(padded)
    status_clean = CaseManager.get_execution_status(eid)
    assert status_padded == status_clean, (
        f"带空白批次号查到的状态与干净号不一致: {status_padded} != {status_clean}"
    )
    assert status_padded["status"] == "finished", (
        f"带空白批次号未走到正常收尾: {status_padded}"
    )
    detail_padded = CaseManager.get_execution_detail(padded)
    assert detail_padded is not None, "带空白批次号查不到明细"
    assert detail_padded["summary"]["total_cases"] == len(cases), (
        f"明细未按归一后的批次号落库: {detail_padded['summary']}"
    )
    summary = CaseManager.finish_execution(padded)
    assert summary["execution_id"] == eid, (
        f"汇总行批次号应归一到干净号，实得 {summary['execution_id']!r}"
    )


def test_问题4_各入口归一结果一致(temp_db):
    """6 个落库入口共用同一归一函数（口径单一的机器可验证形式）"""
    from src.core.case_manager import CaseManager

    padded, clean = "  RUN-NORM-0001  ", "RUN-NORM-0001"
    assert CaseManager._normalize_batch_id(padded, "x") == clean

    # 空值口径：全部入口对"空/全空白"都应拒绝，且带上自己的 operation
    for operation in (
        "record_execution", "finish_execution", "get_execution_detail",
        "get_execution_status",
    ):
        with pytest.raises(Exception) as exc:
            CaseManager._normalize_batch_id("   ", operation)
        assert "执行批次号不能为空" in str(exc.value)
        assert exc.value.context["operation"] == operation


def test_问题4_带空白批次号的状态更新能查到行(temp_db):
    """
    `_update_batch_status` 未 strip 时只会 warning 返回 None（静默丢失）

    修复前这里是一条 warning + None：调用方拿到 None 也不报错，状态
    更新就此消失，而明细已按干净号落库，两边分叉。
    """
    from src.core.case_manager import CaseManager

    result = CaseManager.start_execution(trigger="web", case_type="api")
    eid = result["execution_id"]

    meta = CaseManager._update_batch_status(
        f"  {eid}  ", status="running"
    )

    assert meta is not None, (
        "带空白批次号查不到批次行 —— 状态更新被静默丢弃"
    )
    assert CaseManager.get_execution_status(f"  {eid}  ")["status"] == "running"


def test_问题4_带空白批次号不抛批次不存在(temp_db):
    """`finish_execution` 此前只校验非空却仍用带空格原值查询 → 抛批次不存在"""
    from datetime import datetime

    from src.core.case_manager import CaseManager

    result = CaseManager.start_execution(trigger="web", case_type="api")
    eid = result["execution_id"]
    now = datetime.now()

    CaseManager.record_execution(
        execution_id=f"  {eid}  ",
        case_id=result["cases"][0]["case_id"],
        case_name="归一测试用例",
        result="passed",
        start_time=now,
        end_time=now,
        duration=0.001,
    )

    summary = CaseManager.finish_execution(f"  {eid}  ")

    assert summary["execution_id"] == eid, (
        f"汇总行的批次号应与明细一致（均归一到干净号），实得 "
        f"{summary['execution_id']!r}"
    )
    assert summary["total"] == 1
    assert summary["passed"] == 1
    # 明细按干净号落库，用干净号也必须能查到（两端口径一致）
    assert CaseManager.get_execution_detail(eid)["summary"]["total_cases"] == 1


# ===========================================================================
# SSE / DB 夹具（与第 3 批同构，供问题 1~4 的用例共用）
# ===========================================================================


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """临时 SQLite 库 + 2 条种子用例（start_execution 需要 active 用例）"""
    from src.core.case_manager import CaseManager
    from src.db.db_session import DatabaseSession

    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day45_batch4_core.db"))
    monkeypatch.setenv("TM_REDIS_ENABLED", "false")
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "false")
    DatabaseSession.reset()
    DatabaseSession.init_db()
    for idx, (cid, module) in enumerate(
        [("TM-B4-C0001", "用户中心"), ("TM-B4-C0002", "订单中心")]
    ):
        CaseManager.create_case(
            {"case_id": cid, "name": f"归一测试用例{idx + 1}", "module": module,
             "priority": "P1", "case_type": "api"}
        )
    yield
    DatabaseSession.reset()


@pytest.fixture
def exec_client(tmp_path, monkeypatch):
    """
    执行 API 测试客户端（临时 SQLite 库 + 2 条种子用例 + 真实触发一次）

    返回:
        tuple[FlaskClient, str]: (测试客户端, 已完成的批次号)
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day45_batch4.db"))
    monkeypatch.setenv("TM_REDIS_ENABLED", "false")
    monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "false")
    from src.db.db_session import DatabaseSession

    DatabaseSession.reset()
    DatabaseSession.init_db()

    from src.core.case_manager import CaseManager
    from src.web import create_app

    CaseManager.create_case(
        {"case_id": "TM-B4-0001", "name": "用户登录", "module": "用户中心",
         "priority": "P0", "case_type": "api", "description": "标签: smoke"}
    )
    CaseManager.create_case(
        {"case_id": "TM-B4-0002", "name": "订单创建", "module": "订单中心",
         "priority": "P2", "case_type": "api", "description": "标签: chip"}
    )
    client = create_app("test").test_client()
    resp = client.post("/api/executions/trigger", json={"executor": "simulated"})
    execution_id = resp.get_json()["data"]["execution_id"]
    for _ in range(200):  # 轮询至终态（禁固定 sleep 赌时序）
        status = client.get(f"/api/executions/{execution_id}/status").get_json()
        if status["data"]["status"] in ("finished", "failed"):
            break
        time.sleep(0.05)
    yield client, execution_id
    DatabaseSession.reset()
