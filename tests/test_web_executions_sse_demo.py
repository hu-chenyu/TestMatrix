"""
TestMatrix Day25: SSE实时事件推送框架测试
（GET /api/executions/<id>/events + 内存事件总线）

TestMatrix Day26: SSE增强测试——多订阅者广播 + Last-Event-ID
断线回放 + 心跳保活（历史环非消费式重构后追加8条用例）

测试覆盖:
    事件总线纯单元测试（不依赖HTTP，确定性优先）:
        1. test_publish_subscribe      publish入队后subscribe迭代
           能读到，event_type/data/timestamp三字段正确
        2. test_channel_isolation      两个execution_id各publish，
           订阅者只收到自己通道的事件（注册表隔离）
        3. test_close_unblocks         close后队列空时subscribe
           迭代器立即结束不挂死；close前publish的残余事件
           close后仍能被读完；历史环非消费式——第二次subscribe
           回放的事件序列与第一次一致（多订阅者广播基石）
        4. test_get_channel_missing    get_channel(create=False)
           查不存在批次返回None
        5. test_event_id_monotonic_no_gap   publish在锁内分配的
           event_id从1起单调递增无跳号（Last-Event-ID唯一依据）
        6. test_subscribe_replay_after_last_event_id
           subscribe(last_event_id=2)只回放id>2的断点后事件
        7. test_multi_subscriber_independent_cursor
           两个订阅者先后订阅同一通道各自收全量不被分流
           （历史环非消费式 + 独立游标）
        8. test_history_ring_eviction_replay_from_oldest
           历史环超容量淘汰后从极旧id回放不报错、长度=maxlen
    HTTP SSE测试:
        9.  test_events_not_found       不存在批次events接口: 404
        10. test_events_content_type    已finished批次events接口:
           响应头Content-Type含text/event-stream
        11. test_event_format_and_snapshot  同步跑小批次（桩执行器
           4条全过）后读events，帧逐行校验SSE格式（event:/id:/
           data:三要素），事件序列恰为
           batch_start → case_finished×4 → batch_finished
           （走已终态批次的DB重建快照分支，不依赖线程时序）
        12. test_trigger_real_thread_stream  POST /trigger真线程
           触发后流式读取events（buffered=False逐块迭代），
           收集case_finished帧断言字段齐全，读到batch_finished
           帧立即break防挂死，wait后台线程终态
        13. test_format_sse_frame_id_line_optional
           _format_sse_frame带event_id输出id行（event行后
           data行前）、不带时不输出（降级帧向后兼容）
        14. test_events_last_event_id_resume_after_breakpoint
           同步跑完批次带Last-Event-ID=2请求events，只回
           断点后帧（DB重建合成id过滤）
        15. test_events_heartbeat_comment_frame
           空闲批次monkeypatch心跳间隔到0.1s能收到
           ": heartbeat"注释帧（订阅tick节拍驱动）
        16. test_events_concurrent_subscribers_consistent_ids
           两个线程并发订阅同一运行中批次，各自帧的id序列
           一致且不丢（多订阅者广播端到端）

异步测试原则（防flaky，同Day24口径）:
    - 编排与帧格式验证同步直调_execute_batch_async（桩执行器
      全过，结果确定性），不走真实线程
    - 真线程链路仅test 12/16两条，触发前轮询等待事件通道建立
      （置running后立即建通道）或批次先行终态（极快完成也没
      关系: 终态批次走DB重建分支同样收到完整事件序列）
    - 流式读取设帧数上限 + 读到终态帧立即break（阻塞迭代无法
      用墙钟超时中断，靠"批次必然发布终态事件"保证收敛）
    - 事件通道注册表是全局单例: autouse fixture每条测试前后
      reset_channels()，防跨测试通道污染（今日最大flaky来源）
    - 心跳测试必须monkeypatch心跳间隔（默认15s，真等会拖死
      测试；订阅tick节拍0.5s + 间隔0.1s ⇒ 首个tick即触发）

测试基建:
    临时SQLite文件库（tmp_path + 前后DatabaseSession.reset()防
    Windows文件锁）+ Flask test client端到端验证；种子4条active
    用例（TM-UC-0001/0002用户中心、TM-OD-0001/0002订单中心，
    末位奇偶各半，模拟执行必有通过有失败便于断言）。
"""

import json
import threading
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import allure
import pytest
from flask.testing import FlaskClient

from src.core import event_bus
from src.core.case_manager import CaseManager
from src.core.event_bus import (
    HISTORY_RING_MAXLEN,
    EventChannel,
    ExecutionEvent,
    get_channel,
)
from src.core.executors import BaseExecutor, ExecutionResult
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app
from src.web.routes.executions import _format_sse_frame

# 轮询参数: 批次模拟执行约0.01s/条，预算远超实际耗时防flaky
POLL_MAX_ATTEMPTS = 20
POLL_INTERVAL_SECONDS = 0.3

# 流式读取保护: 单次响应最多读取帧数上限（防缺陷代码下无限流）
MAX_STREAM_FRAMES = 100

# 流式读取保护: 等待事件通道建立/批次终态的轮询截止秒数
CHANNEL_WAIT_TIMEOUT_SECONDS = 5.0


# ===========================================================================
# 造数与夹具
# ===========================================================================
def seed_cases() -> None:
    """
    造用例种子数据并入库（4条active）

    模块与末位奇偶分布:
        - 用户中心: TM-UC-0001（奇→failed）、TM-UC-0002（偶→passed）
        - 订单中心: TM-OD-0001（奇→failed）、TM-OD-0002（偶→passed）
    模拟执行结果可手算: 全量4条=2过2挂；test 7/6用桩执行器
    全过（4条passed，帧序列确定性）。

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
                    description="SSE事件流API测试种子用例",
                )
            )


@pytest.fixture(autouse=True)
def _clean_event_registry() -> Iterator[None]:
    """
    事件通道注册表清洁fixture（autouse，全测试生效）

    事件注册表是模块级全局单例，残留通道会跨测试串号（同名
    批次读到上个测试的旧通道对象）——每条测试前后各清空一次，
    这是今日SSE测试防flaky的关键保障。

    参数:
        无

    返回:
        Iterator[None]: yield无数据，前后各清空注册表一次
    """
    event_bus.reset_channels()
    yield
    event_bus.reset_channels()


@pytest.fixture
def sse_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """
    SSE事件流测试数据库fixture（临时SQLite文件库）

    将数据库指向pytest临时目录独立库文件并完成建表+用例种子
    造数，前后重置DatabaseSession引擎单例（防Windows文件句柄
    残留与测试间引擎状态污染）；同时清除TM_EXECUTOR环境变量，
    保证未显式传executor的触发请求确定性走simulated执行器。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[None]: yield无数据，前后完成引擎重置
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv(
        "TM_DB_SQLITE_PATH", str(tmp_path / "sse_api.db")
    )
    monkeypatch.delenv("TM_EXECUTOR", raising=False)
    DatabaseSession.reset()
    DatabaseSession.init_db()
    seed_cases()
    yield
    DatabaseSession.reset()


@pytest.fixture
def sse_client(sse_db: None) -> FlaskClient:
    """
    SSE事件流API测试客户端fixture（基于sse_db临时库）

    参数:
        sse_db (None): 依赖的数据库fixture（保证建库+种子+重置）

    返回:
        FlaskClient: Flask测试客户端（teardown时数据库
                     由sse_db统一重置）
    """
    return create_app("test").test_client()


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


class _AllPassExecutor(BaseExecutor):
    """
    全部通过的桩执行器（内部测试辅助类）

    run_one固定返回passed，保证同步批次结果确定性（4条全过），
    用于SSE帧格式与快照分支的稳定断言，不依赖真实线程时序。
    """

    def run_one(self, case: dict) -> ExecutionResult:
        """
        桩执行: 固定返回passed

        参数:
            case (dict): 待执行用例字典

        返回:
            ExecutionResult: result=passed，error_message=None，
                             duration=0.001

        异常:
            无
        """
        return ExecutionResult(
            result="passed", error_message=None, duration=0.001
        )


class _SlowAllPassExecutor(BaseExecutor):
    """
    慢速全部通过的桩执行器（内部测试辅助类，Day26并发订阅专用）

    run_one固定sleep后返回passed，拉长批次运行窗口（4条×0.08s
    ≈0.32s+），保证并发订阅线程能落在"通道存在且运行中"的实时
    订阅分支，而不是批次先行终态后的DB重建分支。

    参数说明:
        CASE_DELAY_SECONDS 每条用例的固定耗时（秒）
    """

    # 单条用例固定耗时: 拉长运行窗口又不拖慢测试总时长
    CASE_DELAY_SECONDS = 0.08

    def run_one(self, case: dict) -> ExecutionResult:
        """
        慢速桩执行: sleep后固定返回passed

        参数:
            case (dict): 待执行用例字典

        返回:
            ExecutionResult: result=passed，error_message=None，
                             duration=0.001

        异常:
            无
        """
        time.sleep(self.CASE_DELAY_SECONDS)
        return ExecutionResult(
            result="passed", error_message=None, duration=0.001
        )


def _parse_sse_frame(frame_text: str) -> Tuple[str, dict, Optional[int]]:
    """
    解析单条SSE帧文本（内部方法）

    帧格式: "event: {类型}\\nid: {序号}\\ndata: {JSON}"
    （id行可选，event行后data行前；调用方已按空行分帧），
    逐行校验行前缀并提取事件类型、事件id与JSON载荷。

    参数:
        frame_text (str): 单帧文本（不含结尾空行）

    返回:
        tuple[str, dict, int | None]: (事件类型, 载荷字典, 事件id)，
        帧未携带id行时事件id为None（降级帧）

    异常:
        AssertionError: 行前缀不符合SSE规范或data不是合法JSON
    """
    lines = frame_text.strip("\n").split("\n")
    assert lines[0].startswith("event: "), f"帧首行应为event:前缀 | 实际: {lines[0]!r}"
    event_type = lines[0][len("event: "):]
    # id行可选: 次行以"id: "开头则提取事件id，data行顺延一位
    event_id: Optional[int] = None
    data_line_index = 1
    if lines[1].startswith("id: "):
        event_id = int(lines[1][len("id: "):])
        data_line_index = 2
    assert lines[data_line_index].startswith("data: "), (
        f"帧data行应为data:前缀 | 实际: {lines[data_line_index]!r}"
    )
    payload = json.loads(lines[data_line_index][len("data: "):])
    return event_type, payload, event_id


# ===========================================================================
# 事件总线纯单元测试（不依赖HTTP，确定性优先）
# ===========================================================================
@allure.feature("事件总线")
@allure.story("内存事件通道基础设施")
@allure.severity(allure.severity_level.CRITICAL)
class TestEventBusUnit:
    """EventChannel与通道注册表纯单元测试"""

    def test_publish_subscribe(self) -> None:
        """
        发布订阅基本链路: publish一条batch_start后subscribe迭代器
        能读到，event_type/data/timestamp三字段正确
        """
        channel = EventChannel()
        channel.publish(
            ExecutionEvent(
                event_type="batch_start",
                data={"total_cases": 4, "executor_kind": "simulated"},
            )
        )
        # close后残余事件仍可读完（drain语义），迭代器随之结束
        channel.close(reason="test")
        received = list(channel.subscribe())

        assert len(received) == 1, "应恰好收到1条事件"
        first = received[0]
        assert first.event_type == "batch_start"
        assert first.data == {"total_cases": 4, "executor_kind": "simulated"}
        assert isinstance(first.timestamp, float), "timestamp应为浮点秒级时间戳"

    def test_channel_isolation(self) -> None:
        """
        通道隔离: 两个execution_id各publish不同事件，订阅者只
        收到自己通道的事件（注册表按批次号隔离）
        """
        chan_a = get_channel("RUN-unit-aaaa", create=True)
        chan_b = get_channel("RUN-unit-bbbb", create=True)
        assert chan_a is not None and chan_b is not None
        assert chan_a is not chan_b, "不同批次应拿到不同通道对象"

        chan_a.publish(ExecutionEvent(event_type="batch_start", data={"seq": 1}))
        chan_b.publish(
            ExecutionEvent(event_type="case_finished", data={"seq": 2})
        )
        # close_channel关闭并从注册表移除（drain语义不影响已发布事件）
        event_bus.close_channel("RUN-unit-aaaa", reason="test")
        event_bus.close_channel("RUN-unit-bbbb", reason="test")

        events_a = list(chan_a.subscribe())
        events_b = list(chan_b.subscribe())
        assert [e.event_type for e in events_a] == ["batch_start"], (
            "通道A订阅者只应收到通道A的事件"
        )
        assert [e.event_type for e in events_b] == ["case_finished"], (
            "通道B订阅者只应收到通道B的事件"
        )
        assert events_a[0].data == {"seq": 1}
        assert events_b[0].data == {"seq": 2}

    def test_close_unblocks(self) -> None:
        """
        close解除阻塞: close后队列空时subscribe迭代器立即正常
        结束不挂死；close前publish的残余事件close后仍能被读完；
        snapshot返回快照但不消费历史环；历史环非消费式——第二次
        subscribe回放的事件序列与第一次一致（多订阅者广播基石，
        Day26行为变更点）
        """
        channel = EventChannel()
        channel.publish(ExecutionEvent(event_type="batch_start", data={}))
        channel.publish(
            ExecutionEvent(event_type="case_finished", data={"case_id": "X"})
        )
        assert channel.is_closed is False

        # snapshot不消费: 连续两次快照一致，订阅后事件仍在
        snapshots = channel.snapshot()
        assert len(snapshots) == 2, "快照应含全部2条积压事件"
        assert len(channel.snapshot()) == 2, "快照不应消费历史环"

        channel.close(reason="done")
        assert channel.is_closed is True
        assert channel.close_reason == "done"

        # close后残余事件仍能被读完 + 迭代器随之结束（不挂死）
        drained = list(channel.subscribe())
        assert [e.event_type for e in drained] == [
            "batch_start",
            "case_finished",
        ], "close前的残余事件应全部被读完"
        # 历史环非消费式: 第二次subscribe仍回放全部事件，
        # 收到的序列与第一次完全一致（Day26有意行为变更）
        replayed = list(channel.subscribe())
        assert [e.event_type for e in replayed] == [
            e.event_type for e in drained
        ], "第二次订阅的事件序列应与第一次一致"
        assert [e.event_id for e in replayed] == [
            e.event_id for e in drained
        ], "两次订阅的事件id序列应一致（含event_id字段）"

    def test_get_channel_missing(self) -> None:
        """
        注册表查询不存在批次: get_channel(create=False)返回None
        （路由层据此走降级分支/404）
        """
        assert get_channel("RUN-ghost-0000", create=False) is None

    def test_event_id_monotonic_no_gap(self) -> None:
        """
        event_id单调递增无跳号: 连续publish五条事件，锁内分配的
        event_id恰为1到5严格递增（Last-Event-ID断点回放的唯一
        依据，跳号会导致客户端误判丢帧）
        """
        channel = EventChannel()
        for seq in range(5):
            channel.publish(
                ExecutionEvent(
                    event_type="case_finished", data={"seq": seq}
                )
            )
        channel.close(reason="test")

        received = list(channel.subscribe())
        assert [e.event_id for e in received] == [1, 2, 3, 4, 5], (
            "event_id应从1起单调递增无跳号"
        )
        # 手工构造时event_id缺省None，publish在锁内统一覆写分配
        assert received[0].event_id == 1, "首条事件id应从1起"

    def test_subscribe_replay_after_last_event_id(self) -> None:
        """
        断点回放: publish四条事件后subscribe(last_event_id=2)，
        只回放id>2的事件（恰为id 3、4两条），断点前事件被跳过
        （Last-Event-ID断线重连的核心语义）
        """
        channel = EventChannel()
        for seq in range(4):
            channel.publish(
                ExecutionEvent(
                    event_type="case_finished", data={"seq": seq}
                )
            )
        channel.close(reason="test")

        received = list(channel.subscribe(last_event_id=2))
        assert [e.event_id for e in received] == [3, 4], (
            "last_event_id=2应只回放id>2的事件"
        )
        assert all(e.data["seq"] >= 2 for e in received), (
            "回放事件应与id序号对应"
        )

    def test_multi_subscriber_independent_cursor(self) -> None:
        """
        多订阅者独立游标: 订阅者A读到一半时订阅者B接入，B仍收
        全量三条事件（不分流不残缺），A继续读完剩余事件——历史环
        非消费式 + 各自独立游标，多订阅者广播的核心保证
        """
        channel = EventChannel()
        for seq in range(3):
            channel.publish(
                ExecutionEvent(
                    event_type="case_finished", data={"seq": seq}
                )
            )
        channel.close(reason="test")

        # 订阅者A: 手动逐条消费前两条（部分消费）
        subscriber_a = channel.subscribe()
        first = next(subscriber_a)
        second = next(subscriber_a)
        assert first.event_id == 1 and second.event_id == 2, (
            "订阅者A应先读到前两条事件"
        )

        # 订阅者B: 在A消费到一半时接入，仍收全量三条不被分流
        received_b = list(channel.subscribe())
        assert [e.event_id for e in received_b] == [1, 2, 3], (
            "订阅者B应收到全部三条事件（独立游标互不干扰）"
        )

        # 订阅者A: 继续读完剩余事件后迭代正常结束（drain语义）
        third = next(subscriber_a)
        assert third.event_id == 3, "订阅者A应读完最后一条事件"
        with pytest.raises(StopIteration):
            next(subscriber_a)

    def test_history_ring_eviction_replay_from_oldest(self) -> None:
        """
        历史环超容量淘汰: publish超过maxlen条事件后，从全量游标
        与极旧游标回放都只收到maxlen条（最旧事件已被淘汰），
        从最旧存活事件开始回放不报错（best-effort回放窗口边界）
        """
        channel = EventChannel()
        total = HISTORY_RING_MAXLEN + 5
        for seq in range(total):
            channel.publish(
                ExecutionEvent(
                    event_type="case_finished", data={"seq": seq}
                )
            )
        channel.close(reason="test")

        # 全量游标（None）: 只剩maxlen条，从最旧存活事件起
        received = list(channel.subscribe())
        assert len(received) == HISTORY_RING_MAXLEN, (
            "超容量后历史环应只剩maxlen条"
        )
        assert received[0].event_id == total - HISTORY_RING_MAXLEN + 1, (
            "最旧存活事件id应为总数-maxlen+1"
        )
        assert received[-1].event_id == total, "最新事件id应为总数"

        # 极旧游标（早于最旧存活事件）: 不报错，同样从最旧开始
        received_old = list(channel.subscribe(last_event_id=2))
        assert len(received_old) == HISTORY_RING_MAXLEN, (
            "极旧断点回放长度应为maxlen（从最旧存活开始）"
        )
        assert received_old[0].event_id == received[0].event_id, (
            "极旧断点应从最旧存活事件开始回放"
        )


# ===========================================================================
# HTTP SSE接口测试
# ===========================================================================
@allure.feature("执行SSE流式接口")
@allure.story("批次实时事件流")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestExecutionSseApi:
    """GET /api/executions/<id>/events 流式接口测试"""

    def test_events_not_found(self, sse_client: FlaskClient) -> None:
        """
        不存在批次订阅事件流: 返回404统一错误格式
        """
        response = sse_client.get(
            "/api/executions/RUN-20990101-000000-none0/events"
        )
        data = response.get_json()

        assert response.status_code == 404, "不存在批次应返回404"
        assert data["code"] == 404
        assert data["data"] == {
            "execution_id": "RUN-20990101-000000-none0"
        }

    def test_events_content_type(
        self,
        sse_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        SSE响应头: 已finished批次的events接口返回200且
        Content-Type含text/event-stream（流式响应MIME约定）
        """
        result = CaseManager.start_execution(
            trigger="web", executor_name="web", case_type="api"
        )
        execution_id = result["execution_id"]
        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _AllPassExecutor(),
        )
        CaseManager._execute_batch_async(execution_id, result["cases"], None)

        response = sse_client.get(f"/api/executions/{execution_id}/events")
        # 消费完整响应体（有限帧，防请求上下文悬挂）
        response.get_data(as_text=True)

        assert response.status_code == 200, "事件流接口应返回200"
        content_type = response.headers.get("Content-Type", "")
        assert "text/event-stream" in content_type, (
            f"Content-Type应为text/event-stream | 实际: {content_type}"
        )

    def test_event_format_and_snapshot(
        self,
        sse_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        帧格式与终态快照: 同步跑一个小批次（桩执行器4条全过，
        不走真实线程）后读events，逐行校验SSE帧三要素
        （event:前缀/data:后合法JSON/帧间空行分隔），事件序列
        恰为 batch_start → case_finished×4 → batch_finished
        （终态批次通道已清理，走DB重建快照分支，不挂死等待）
        """
        result = CaseManager.start_execution(
            trigger="web", executor_name="web", case_type="api"
        )
        execution_id = result["execution_id"]
        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _AllPassExecutor(),
        )
        # 同步执行（同步返回即执行完成，批次终态且通道已关闭清理）
        CaseManager._execute_batch_async(execution_id, result["cases"], None)
        # 通道已从注册表移除: events走已终态批次的DB重建分支
        assert get_channel(execution_id, create=False) is None

        response = sse_client.get(f"/api/executions/{execution_id}/events")
        assert response.status_code == 200
        body = response.get_data(as_text=True)

        # 按空行分帧（结尾空行产生的空段过滤）
        frames = [f for f in body.split("\n\n") if f.strip()]
        assert len(frames) == 6, f"4条用例批次应产出6帧 | 实际: {len(frames)}"

        event_types: list = []
        payloads: dict = {}
        for frame in frames:
            event_type, payload, _frame_id = _parse_sse_frame(frame)
            event_types.append(event_type)
            payloads.setdefault(event_type, []).append(payload)

        # 事件序列: batch_start → case_finished×4 → batch_finished
        assert event_types == ["batch_start"] + ["case_finished"] * 4 + [
            "batch_finished"
        ], f"事件序列不符 | 实际: {event_types}"

        # batch_start载荷: 用例数正确
        assert payloads["batch_start"][0]["total_cases"] == 4

        # case_finished载荷: 字段齐全、4条全过、case_id为种子4条
        case_payloads = payloads["case_finished"]
        assert {p["case_id"] for p in case_payloads} == {
            "TM-UC-0001",
            "TM-UC-0002",
            "TM-OD-0001",
            "TM-OD-0002",
        }
        for payload in case_payloads:
            assert set(payload) == {
                "case_id",
                "case_name",
                "result",
                "duration",
                "error_message",
            }, f"case_finished字段应与埋点口径一致 | 实际: {sorted(payload)}"
            assert payload["result"] == "passed", "桩执行器应4条全过"
            assert payload["error_message"] is None
            assert isinstance(payload["duration"], float)

        # batch_finished载荷: 汇总统计与4条全过一致
        final_payload = payloads["batch_finished"][0]
        assert final_payload["total"] == 4
        assert final_payload["passed"] == 4
        assert final_payload["failed"] == 0
        assert final_payload["error"] == 0
        assert final_payload["skipped"] == 0
        assert final_payload["pass_rate"] == 1.0

    def test_trigger_real_thread_stream(
        self, sse_client: FlaskClient
    ) -> None:
        """
        真线程流式链路: POST /trigger真线程触发后，流式读取
        events（buffered=False逐块迭代），逐帧解析，收集到
        case_finished帧断言字段齐全，读到batch_finished帧立即
        break（不等流自然结束防挂死），wait后台线程终态后校验
        批次统计与种子奇偶规则一致（4条=2过2挂）
        """
        trigger_response = sse_client.post("/api/executions/trigger")
        trigger_data = trigger_response.get_json()
        assert trigger_response.status_code == 202
        execution_id = trigger_data["data"]["execution_id"]

        # 等待事件通道建立（置running后立即创建）或批次先行终态
        # （极快完成也没关系: 终态批次走DB重建分支同样收到
        # 完整事件序列，两种路径均能收敛）
        deadline = time.time() + CHANNEL_WAIT_TIMEOUT_SECONDS
        while time.time() < deadline:
            if get_channel(execution_id, create=False) is not None:
                break
            probe = CaseManager.get_execution_status(execution_id)
            if probe is not None and probe["status"] in (
                "finished",
                "failed",
            ):
                break
            time.sleep(0.02)

        # 流式读取: buffered=False + iter_encoded逐块惰性迭代
        # （werkzeug 3.x的TestResponse已移除__iter__，直接
        # for-in响应对象会TypeError，须走iter_encoded迭代器）
        response = sse_client.get(
            f"/api/executions/{execution_id}/events", buffered=False
        )
        assert response.status_code == 200

        case_payloads: list = []
        terminal_type: Optional[str] = None
        frame_count = 0
        buffer = ""
        try:
            for chunk in response.iter_encoded():
                buffer += (
                    chunk.decode("utf-8")
                    if isinstance(chunk, bytes)
                    else chunk
                )
                stop = False
                while "\n\n" in buffer:
                    frame_text, buffer = buffer.split("\n\n", 1)
                    if not frame_text.strip():
                        continue
                    event_type, payload, _event_id = _parse_sse_frame(
                        frame_text
                    )
                    frame_count += 1
                    if event_type == "case_finished":
                        case_payloads.append(payload)
                    if event_type in ("batch_finished", "batch_failed"):
                        terminal_type = event_type
                        stop = True
                        break
                    # 帧数上限保护: 防缺陷代码下无限流挂死测试
                    if frame_count >= MAX_STREAM_FRAMES:
                        stop = True
                        break
                if stop:
                    break
        finally:
            # 主动关闭响应迭代器，触发生成器GeneratorExit干净收尾
            response.close()

        # 读到终态帧（batch_finished/batch_failed）才允许通过
        assert terminal_type in ("batch_finished", "batch_failed"), (
            "应读到终态事件帧后主动break"
        )
        assert len(case_payloads) >= 1, "应至少收到1条case_finished事件"

        # case_finished帧字段齐全（与埋点载荷口径一致）
        sample = case_payloads[0]
        assert set(sample) == {
            "case_id",
            "case_name",
            "result",
            "duration",
            "error_message",
        }
        assert sample["result"] in ("passed", "failed")
        assert isinstance(sample["duration"], float)

        # wait后台线程终态（防teardown与执行线程竞态）
        final = _wait_batch_terminal(execution_id)
        assert final is not None and final["status"] == "finished", (
            "真实线程批次应正常完成"
        )
        assert final["passed"] == 2, "末位偶数用例应通过"
        assert final["failed"] == 2, "末位奇数用例应失败"
        assert final["pass_rate"] == 0.5

    def test_format_sse_frame_id_line_optional(self) -> None:
        """
        帧格式id行可选: _format_sse_frame带event_id时输出
        "id: {n}"行且位于event行后data行前；不带时不输出id行
        （降级帧向后兼容Day25三要素格式）
        """
        # 带event_id: 三行结构 event → id → data
        frame_with_id = _format_sse_frame(
            "case_finished", {"case_id": "TM-UC-0001"}, event_id=7
        )
        assert frame_with_id == (
            'event: case_finished\nid: 7\ndata: {"case_id": "TM-UC-0001"}\n\n'
        ), "带id帧应为event→id→data三行结构"
        lines = frame_with_id.strip("\n").split("\n")
        assert lines.index("event: case_finished") < lines.index("id: 7") < (
            lines.index('data: {"case_id": "TM-UC-0001"}')
        ), "id行应位于event行后data行前"

        # 不带event_id: 降级为Day25两行结构（无id行）
        frame_without_id = _format_sse_frame(
            "case_finished", {"case_id": "TM-UC-0001"}
        )
        assert frame_without_id == (
            'event: case_finished\ndata: {"case_id": "TM-UC-0001"}\n\n'
        ), "不带id帧不应输出id行（降级帧向后兼容）"

    def test_events_last_event_id_resume_after_breakpoint(
        self,
        sse_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Last-Event-ID断点续传: 同步跑完批次（桩执行器4条全过，
        通道已清理走DB重建分支）后带Last-Event-ID=2请求events，
        只回断点后帧——合成id 3/4/5/6的case_finished×3 +
        batch_finished，id≤2的帧（batch_start与首条case_finished）
        被过滤不重发
        """
        result = CaseManager.start_execution(
            trigger="web", executor_name="web", case_type="api"
        )
        execution_id = result["execution_id"]
        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _AllPassExecutor(),
        )
        # 同步执行完成（通道随之关闭清理，events走DB重建分支）
        CaseManager._execute_batch_async(execution_id, result["cases"], None)
        assert get_channel(execution_id, create=False) is None

        response = sse_client.get(
            f"/api/executions/{execution_id}/events",
            headers={"Last-Event-ID": "2"},
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)

        frames = [f for f in body.split("\n\n") if f.strip()]
        assert len(frames) == 4, (
            f"断点2之后应只剩4帧 | 实际: {len(frames)}"
        )
        parsed = [_parse_sse_frame(f) for f in frames]
        frame_ids = [p[2] for p in parsed]
        event_types = [p[0] for p in parsed]

        # 合成id过滤: 只回id>2的帧（3/4/5/6连续不重号）
        assert frame_ids == [3, 4, 5, 6], (
            f"应只回合成id>2的帧 | 实际: {frame_ids}"
        )
        # 事件序列: 断点后的case_finished×3 + batch_finished
        assert event_types == ["case_finished"] * 3 + ["batch_finished"], (
            f"断点后事件序列不符 | 实际: {event_types}"
        )

    def test_events_heartbeat_comment_frame(
        self,
        sse_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        心跳保活: pending批次预建事件通道（模拟运行中空闲窗口:
        通道在、无新事件、批次未终态），monkeypatch心跳间隔到
        0.1s后流式读取events，订阅tick节拍（0.5s限时等待超时）
        驱动发出": heartbeat"注释帧（SSE注释行以冒号开头）
        """
        result = CaseManager.start_execution(
            trigger="web", executor_name="web", case_type="api"
        )
        execution_id = result["execution_id"]
        # 手工预建通道: 批次pending且通道存在 ⇒ 走分支三实时订阅，
        # 无新事件且未关闭 ⇒ tick节拍超时驱动心跳判定
        assert get_channel(execution_id, create=True) is not None
        monkeypatch.setattr(
            "src.web.routes.executions.HEARTBEAT_INTERVAL_SECONDS", 0.1
        )

        response = sse_client.get(
            f"/api/executions/{execution_id}/events", buffered=False
        )
        assert response.status_code == 200

        # 流式读取: 收到首条心跳注释帧即break（批次永不变终态，
        # 流不会自然结束，靠break + finally close干净收尾）
        buffer = ""
        got_heartbeat = False
        frame_count = 0
        try:
            for chunk in response.iter_encoded():
                buffer += (
                    chunk.decode("utf-8")
                    if isinstance(chunk, bytes)
                    else chunk
                )
                stop = False
                while "\n\n" in buffer:
                    frame_text, buffer = buffer.split("\n\n", 1)
                    if not frame_text.strip():
                        continue
                    frame_count += 1
                    if frame_text.strip() == ": heartbeat":
                        got_heartbeat = True
                        stop = True
                        break
                    # 帧数上限保护: 防缺陷代码下无限流挂死测试
                    if frame_count >= MAX_STREAM_FRAMES:
                        stop = True
                        break
                if stop:
                    break
        finally:
            response.close()

        assert got_heartbeat, (
            "空闲订阅应在心跳间隔超时后收到': heartbeat'注释帧"
        )

    def test_events_concurrent_subscribers_consistent_ids(
        self,
        sse_client: FlaskClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        多订阅者并发广播端到端: 慢速桩执行器拉长运行窗口，两个
        线程并发订阅同一运行中批次（各自独立Flask test client
        流式连接），各自的帧id序列一致且不丢——恰为完整
        1..6（batch_start→case_finished×4→batch_finished）
        """
        monkeypatch.setattr(
            "src.core.case_manager.get_executor",
            lambda kind=None: _SlowAllPassExecutor(),
        )
        trigger_response = sse_client.post("/api/executions/trigger")
        assert trigger_response.status_code == 202
        execution_id = trigger_response.get_json()["data"]["execution_id"]

        # 等通道建立（置running后batch_start埋点建通道）或批次
        # 先行终态（两条路径均能收敛到完整id序列）
        deadline = time.time() + CHANNEL_WAIT_TIMEOUT_SECONDS
        while time.time() < deadline:
            if get_channel(execution_id, create=False) is not None:
                break
            probe = CaseManager.get_execution_status(execution_id)
            if probe is not None and probe["status"] in (
                "finished",
                "failed",
            ):
                break
            time.sleep(0.02)

        # 第二个独立client（同app实例，避免共享cookie jar的线程
        # 安全疑虑），两个线程并发订阅同一批次
        client_b = sse_client.application.test_client()
        ids_a: List[int] = []
        ids_b: List[int] = []

        def _read_stream(client: FlaskClient, ids: List[int]) -> None:
            """
            单订阅者流式读取（内部方法）: buffer按\n\n拼半帧，
            逐帧解析收集事件id，读到终态帧break防挂死

            参数:
                client (FlaskClient): 流式请求所用测试客户端
                ids (list[int]): 帧id收集列表（输出参数）

            返回:
                无
            """
            response = client.get(
                f"/api/executions/{execution_id}/events", buffered=False
            )
            buffer = ""
            try:
                for chunk in response.iter_encoded():
                    buffer += (
                        chunk.decode("utf-8")
                        if isinstance(chunk, bytes)
                        else chunk
                    )
                    stop = False
                    while "\n\n" in buffer:
                        frame_text, buffer = buffer.split("\n\n", 1)
                        if not frame_text.strip():
                            continue
                        event_type, _payload, event_id = _parse_sse_frame(
                            frame_text
                        )
                        if event_id is not None:
                            ids.append(event_id)
                        if event_type in ("batch_finished", "batch_failed"):
                            stop = True
                            break
                        # 帧数上限保护: 防缺陷代码下无限流挂死测试
                        if len(ids) >= MAX_STREAM_FRAMES:
                            stop = True
                            break
                    if stop:
                        break
            finally:
                response.close()

        thread_a = threading.Thread(
            target=_read_stream, args=(sse_client, ids_a)
        )
        thread_b = threading.Thread(
            target=_read_stream, args=(client_b, ids_b)
        )
        thread_a.start()
        thread_b.start()
        thread_a.join(timeout=CHANNEL_WAIT_TIMEOUT_SECONDS * 2)
        thread_b.join(timeout=CHANNEL_WAIT_TIMEOUT_SECONDS * 2)
        assert not thread_a.is_alive(), "订阅者A应在超时前读完事件流"
        assert not thread_b.is_alive(), "订阅者B应在超时前读完事件流"

        # 两订阅者各自帧id序列一致且不丢: 恰为完整1..6
        expected_ids = [1, 2, 3, 4, 5, 6]
        assert ids_a == expected_ids, (
            f"订阅者A应收到完整id序列1..6 | 实际: {ids_a}"
        )
        assert ids_b == expected_ids, (
            f"订阅者B应收到完整id序列1..6 | 实际: {ids_b}"
        )

        # wait后台线程终态（防teardown与执行线程竞态）
        final = _wait_batch_terminal(execution_id)
        assert final is not None and final["status"] == "finished", (
            "慢速桩批次应正常完成"
        )
        assert final["passed"] == 4, "慢速桩执行器应4条全过"
