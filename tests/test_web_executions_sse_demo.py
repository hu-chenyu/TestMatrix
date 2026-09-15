"""
TestMatrix Day25: SSE实时事件推送框架测试
（GET /api/executions/<id>/events + 内存事件总线）

测试覆盖:
    事件总线纯单元测试（不依赖HTTP，确定性优先）:
        1. test_publish_subscribe      publish入队后subscribe迭代
           能读到，event_type/data/timestamp三字段正确
        2. test_channel_isolation      两个execution_id各publish，
           订阅者只收到自己通道的事件（注册表隔离）
        3. test_close_unblocks         close后队列空时subscribe
           迭代器立即结束不挂死；close前publish的残余事件
           close后仍能被读完；snapshot不消费队列
        4. test_get_channel_missing    get_channel(create=False)
           查不存在批次返回None
    HTTP SSE测试:
        5. test_events_not_found       不存在批次events接口: 404
        6. test_events_content_type    已finished批次events接口:
           响应头Content-Type含text/event-stream
        7. test_event_format_and_snapshot  同步跑小批次（桩执行器
           4条全过）后读events，帧逐行校验SSE格式三要素
           （event:/data:/空行），事件序列恰为
           batch_start → case_finished×4 → batch_finished
           （走已终态批次的DB重建快照分支，不依赖线程时序）
        8. test_trigger_real_thread_stream  POST /trigger真线程
           触发后流式读取events（buffered=False逐块迭代），
           收集case_finished帧断言字段齐全，读到batch_finished
           帧立即break防挂死，wait后台线程终态

异步测试原则（防flaky，同Day24口径）:
    - 编排与帧格式验证同步直调_execute_batch_async（桩执行器
      全过，结果确定性），不走真实线程
    - 真线程链路仅test 8一条，触发前轮询等待事件通道建立
      （置running后立即建通道）或批次先行终态（极快完成也没
      关系: 终态批次走DB重建分支同样收到完整事件序列）
    - 流式读取设帧数上限 + 读到终态帧立即break（阻塞迭代无法
      用墙钟超时中断，靠"批次必然发布终态事件"保证收敛）
    - 事件通道注册表是全局单例: autouse fixture每条测试前后
      reset_channels()，防跨测试通道污染（今日最大flaky来源）

测试基建:
    临时SQLite文件库（tmp_path + 前后DatabaseSession.reset()防
    Windows文件锁）+ Flask test client端到端验证；种子4条active
    用例（TM-UC-0001/0002用户中心、TM-OD-0001/0002订单中心，
    末位奇偶各半，模拟执行必有通过有失败便于断言）。
"""

import json
import time
from pathlib import Path
from typing import Iterator, Optional, Tuple

import allure
import pytest
from flask.testing import FlaskClient

from src.core import event_bus
from src.core.case_manager import CaseManager
from src.core.event_bus import (
    EventChannel,
    ExecutionEvent,
    get_channel,
)
from src.core.executors import BaseExecutor, ExecutionResult
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app

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


def _parse_sse_frame(frame_text: str) -> Tuple[str, dict]:
    """
    解析单条SSE帧文本（内部方法）

    帧格式: "event: {类型}\\ndata: {JSON}"（调用方已按空行分帧），
    逐行校验行前缀并提取事件类型与JSON载荷。

    参数:
        frame_text (str): 单帧文本（不含结尾空行）

    返回:
        tuple[str, dict]: (事件类型, 载荷字典)

    异常:
        AssertionError: 行前缀不符合SSE规范或data不是合法JSON
    """
    lines = frame_text.strip("\n").split("\n")
    assert lines[0].startswith("event: "), f"帧首行应为event:前缀 | 实际: {lines[0]!r}"
    assert lines[1].startswith("data: "), f"帧次行应为data:前缀 | 实际: {lines[1]!r}"
    event_type = lines[0][len("event: "):]
    payload = json.loads(lines[1][len("data: "):])
    return event_type, payload


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
        snapshot返回快照但不消费队列
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
        assert len(channel.snapshot()) == 2, "快照不应消费队列"

        channel.close(reason="done")
        assert channel.is_closed is True
        assert channel.close_reason == "done"

        # close后残余事件仍能被读完 + 迭代器随之结束（不挂死）
        drained = list(channel.subscribe())
        assert [e.event_type for e in drained] == [
            "batch_start",
            "case_finished",
        ], "close前的残余事件应全部被读完"
        # 队列已空且closed: 再次订阅立即结束
        assert list(channel.subscribe()) == []

    def test_get_channel_missing(self) -> None:
        """
        注册表查询不存在批次: get_channel(create=False)返回None
        （路由层据此走降级分支/404）
        """
        assert get_channel("RUN-ghost-0000", create=False) is None


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
            event_type, payload = _parse_sse_frame(frame)
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
                    event_type, payload = _parse_sse_frame(frame_text)
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
