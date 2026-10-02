"""
TestMatrix 阶段B-3: P2 竞态/串口/通知/状态机修复的回归测试

测试覆盖（本文件 6 组，对应本阶段修复的 P2）:
    A组 P2-10 / P2-11 SSE 编号体系标识与断点过滤
        1.  test_sse_frame_id_carries_namespace
        2.  test_parse_last_event_id_both_formats
        3.  test_cross_namespace_resume_does_not_swallow_events
        4.  test_terminal_snapshot_branch_filters_by_last_event_id
    B组 P2-13 stop_worker join 超时后不得起第二个 worker
        5.  test_stop_worker_keeps_reference_when_join_timeout
        6.  test_start_worker_does_not_double_start_after_timeout
    C组 P2-18 / P2-19 / P2-20 串口客户端
        7.  test_serial_open_sets_write_timeout
        8.  test_read_until_zero_timeout_is_respected
        9.  test_open_does_not_precheck_comports
        10. test_open_unknown_protocol_url_raises_client_error
    D组 P2-21 / P2-22 / P2-24 用例类型推断与批次号去重集合
        11. test_infer_case_type_only_matches_file_name
        12. test_execution_id_set_is_bounded
    E组 P2-26 / P2-29 通知重试基数与死信
        13. test_base_delay_rejects_negative_and_nan
        14. test_dead_letter_list_returns_newest
        15. test_dead_letter_content_is_truncated
    F组 P2-39 http_client 重试方法白名单
        16. test_retry_only_for_idempotent_methods

测试铁律（对齐 PROJECT_CONTEXT.md 7.12/7.19 + 验收清单第三条）:
    - 无固定 sleep 赌时序
    - worker 线程由 fixture 统一启停，teardown 严格复位，线程绝不跨例
    - 每条用例独立临时 SQLite 库
    - 全程 loguru，无 print
"""

import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import serial
from flask import Flask
from src.common import serial_client as serial_client_mod
from src.common.http_client import IDEMPOTENT_METHODS, HttpClient
from src.core import event_bus
from src.core import task_queue as tq
from src.core.case_manager import (
    MAX_TRACKED_EXECUTION_IDS,
    CaseManager,
    generate_execution_id,
)
from src.core.event_bus import ExecutionEvent
from src.core.notification import (
    DEAD_LETTER_CONTENT_PREVIEW,
    NotificationDeadLetterRepository,
    NotificationRouter,
)
from src.db.db_session import DatabaseSession
from src.web import create_app
from src.web.routes import executions as executions_mod


# ===========================================================================
# 夹具
# ===========================================================================
@pytest.fixture
def db_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    隔离临时 SQLite 库环境

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture
        tmp_path (Path): pytest临时目录fixture

    返回:
        Path: 本条用例的库文件路径
    """
    db_file = tmp_path / "p2_b3_hardening.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_file
    DatabaseSession.reset()
    event_bus.reset_channels()
    db_file.unlink(missing_ok=True)


# ===========================================================================
# A组: SSE 编号体系标识（P2-10 / P2-11）
# ===========================================================================
class TestSseEventIdNamespace:
    """P2-10/P2-11: 帧id带编号体系前缀，重连按前缀路由"""

    def test_sse_frame_id_carries_namespace(self) -> None:
        """
        帧id必须带 live:/db: 前缀，两套体系可区分
        """
        live_frame = executions_mod._format_sse_frame(
            "batch_start", {"total_cases": 1}, event_id=5
        )
        db_frame = executions_mod._format_sse_frame(
            "batch_start", {"total_cases": 1}, event_id=5,
            namespace=executions_mod.EVENT_ID_NAMESPACE_DB,
        )
        assert "id: live:5" in live_frame, "实时流帧id应为live:前缀"
        assert "id: db:5" in db_frame, "DB重建帧id应为db:前缀"

        # 无 event_id 时降级为无id行（向后兼容）
        plain = executions_mod._format_sse_frame("batch_start", {})
        assert "\nid:" not in plain, "无event_id时不应输出id行"

    def test_parse_last_event_id_both_formats(self) -> None:
        """
        Last-Event-ID 支持新格式与旧格式裸整数

        回归点: 旧格式裸整数按 live 体系解释，保证升级不破坏在途连接
        """
        ctx_app = Flask(__name__)
        with ctx_app.test_request_context(
            "/", headers={"Last-Event-ID": "db:7"}
        ):
            assert executions_mod._parse_last_event_id_header() == ("db", 7)

        with ctx_app.test_request_context(
            "/", headers={"Last-Event-ID": "live:42"}
        ):
            assert executions_mod._parse_last_event_id_header() == ("live", 42)

        # 旧格式裸整数 -> live
        with ctx_app.test_request_context(
            "/", headers={"Last-Event-ID": "13"}
        ):
            assert executions_mod._parse_last_event_id_header() == ("live", 13)

        # 缺失 / 畸形 / 未知命名空间 -> None（全量重放）
        for raw in [None, "", "   ", "abc", "unknown:5", "db:abc"]:
            headers = {} if raw is None else {"Last-Event-ID": raw}
            with ctx_app.test_request_context("/", headers=headers):
                assert executions_mod._parse_last_event_id_header() is None, (
                    f"{raw!r} 应解析为None（全量重放）"
                )

    def test_cross_namespace_resume_does_not_swallow_events(
        self, db_path_env: Path
    ) -> None:
        """
        跨体系断点不得吞掉事件（本次修复的核心场景）

        回归点: 修复前两套编号都是裸整数。客户端从实时流收到 id=50 后重连，
        此时通道已清理 → 走DB重建分支 → 服务端拿 50 去和合成序号 1..6 比较，
        结果**一帧不发**，终态事件永久丢失、EventSource 无限重连。
        修复后 live:50 到达 db 分支时体系不匹配 → 按全量重发处理。
        """
        from flask.testing import FlaskClient
        from src.db import models

        # 造2条用例，否则 start_execution 筛不到任何用例
        with DatabaseSession.session_scope() as session:
            for case_id, name in [
                ("TM-B3-0002", "跨体系用例二"),
                ("TM-B3-0004", "跨体系用例四"),
            ]:
                session.add(
                    models.TestCase(
                        case_id=case_id,
                        name=name,
                        module="跨体系",
                        priority="P1",
                        case_type="api",
                        status="active",
                    )
                )

        client: FlaskClient = create_app("test").test_client()

        result = CaseManager.start_execution(
            trigger="web", executor_name="web", case_type="api"
        )
        execution_id = result["execution_id"]
        # 同步跑完批次，通道随之关闭清理 → events 走DB重建分支
        CaseManager._execute_batch_async(execution_id, result["cases"], None)
        assert event_bus.get_channel(execution_id, create=False) is None

        # 模拟"客户端此前在实时流收到 live:50 后重连"
        response = client.get(
            f"/api/executions/{execution_id}/events",
            headers={"Last-Event-ID": "live:50"},
        )
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        frames = [f for f in body.split("\n\n") if f.strip()]

        assert len(frames) > 0, (
            "跨体系断点必须全量重发；修复前此处为0帧，终态事件永久丢失"
        )
        assert any("batch_finished" in f for f in frames), (
            f"全量重发必须包含终态帧 | 实际帧数: {len(frames)}"
        )

    def test_terminal_snapshot_branch_filters_by_last_event_id(
        self, db_path_env: Path
    ) -> None:
        """
        终态补发分支必须按 Last-Event-ID 过滤（P2-11）

        回归点: 修复前该分支完全不过滤，客户端重连会收到全部积压事件，
        case_finished 被重复追加。
        """
        from src.db import models

        # 造1条用例，使 start_execution 能建出批次行
        with DatabaseSession.session_scope() as session:
            session.add(
                models.TestCase(
                    case_id="TM-B3-SEED-0002",
                    name="终态补发用例",
                    module="终态补发",
                    priority="P1",
                    case_type="api",
                    status="active",
                )
            )

        client = create_app("test").test_client()

        # 造一条已 finished 的批次（走分支二需要状态已是终态且批次元信息行存在）
        started = CaseManager.start_execution(
            trigger="web", executor_name="web", case_type="api"
        )
        execution_id = started["execution_id"]
        CaseManager._update_batch_status(
            execution_id,
            status="finished",
            passed=2,
            finished_at=datetime.now(),
        )

        # 建通道并发布3条事件后保留（不close），使 GET 走分支二
        channel = event_bus.get_channel(execution_id, create=True)
        for event_type, payload in [
            ("batch_start", {"total_cases": 2, "executor_kind": None}),
            (
                "case_finished",
                {"case_id": "A", "case_name": "A", "result": "passed",
                 "duration": 0.1, "error_message": None},
            ),
            (
                "case_finished",
                {"case_id": "B", "case_name": "B", "result": "passed",
                 "duration": 0.1, "error_message": None},
            ),
        ]:
            channel.publish(
                ExecutionEvent(
                    event_type=event_type, data=payload, timestamp=0.0
                )
            )

        # 断点=live:2 → 只应收到第3条
        response = client.get(
            f"/api/executions/{execution_id}/events",
            headers={"Last-Event-ID": "live:2"},
        )
        body = response.get_data(as_text=True)
        frames = [f for f in body.split("\n\n") if f.strip()]

        # 断点过滤生效: 断点之前的两条（live:1/live:2）不得重复补发
        assert not any('"case_id": "A"' in f for f in frames), (
            f"断点之前的帧不得重复补发 | 实际帧: {[f[:60] for f in frames]}"
        )
        assert any('"case_id": "B"' in f for f in frames), (
            f"断点之后的那条应补发 | 实际帧: {[f[:60] for f in frames]}"
        )
        # 发布的3条里无终态事件，分支会补一条防御性终态快照帧（无id行），
        # 因此总帧数为 1(断点后) + 1(终态兜底) = 2
        assert len(frames) == 2, (
            f"应补发1条断点后帧 + 1条终态兜底帧 | 实际: {len(frames)}条"
        )
        assert "batch_finished" in frames[-1], (
            f"末帧应为防御性补发的终态快照 | 实际: {frames[-1][:80]}"
        )


# ===========================================================================
# B组: worker 重复启动（P2-13）
# ===========================================================================
class TestStopWorkerKeepsReference:
    """P2-13: join 超时后不得清空引用，否则会起第二个 worker"""

    def test_stop_worker_keeps_reference_when_join_timeout(self) -> None:
        """
        join 超时后必须保留模块级引用

        回归点: 修复前无论 join 是否超时都把 _worker_thread 置 None，
        下次 start_worker 的"存活检查"失效 → 再起一个 worker，两个
        worker 并发消费同一队列，破坏"单worker串行"不变量。
        """

        # 构造一个"永远 join 不完"的活线程替身
        class _StuckThread:
            """join 超时的线程替身（is_alive 恒为 True）"""

            def __init__(self) -> None:
                self.joined = False

            def is_alive(self) -> bool:
                return True

            def join(self, timeout=None) -> None:
                self.joined = True

        stuck = _StuckThread()
        tq._worker_thread = stuck  # type: ignore[assignment]
        tq._stop_event = threading.Event()

        tq.stop_worker()

        assert stuck.joined, "stop_worker 应尝试 join"
        assert tq._worker_thread is stuck, (
            "join 超时后必须保留模块级引用，否则 start_worker 会重复起 worker"
        )

        # 复原全局状态，避免污染后续用例
        tq._worker_thread = None
        tq._stop_event = None

    def test_start_worker_does_not_double_start_after_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        join 超时后再调 start_worker 不得新建线程（幂等仍成立）

        回归点: 这是上一条的直接后果——引用保留后，start_worker 的
        is_alive() 检查才能正确识别"worker 还在跑"并返回原线程。
        修复前引用被置空，此处会新建第二个 worker，两个 worker 并发消费
        同一队列，破坏"单worker串行"不变量。
        """
        monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
        monkeypatch.setenv("TM_TASK_WORKER_ENABLED", "true")

        class _AliveThread:
            """存活的线程替身（start_worker 应识别并复用，不得重建）"""

            def is_alive(self) -> bool:
                return True

            def start(self) -> None:
                raise AssertionError("不应重新 start 已存活的worker")

        alive = _AliveThread()
        tq._worker_thread = alive  # type: ignore[assignment]
        tq._stop_event = threading.Event()
        try:
            returned = tq.start_worker()
            assert returned is alive, (
                "存活worker存在时 start_worker 必须幂等返回原线程，"
                f"实际 {type(returned).__name__}"
            )
        finally:
            tq._worker_thread = None
            tq._stop_event = None


# ===========================================================================
# C组: 串口客户端（P2-18 / P2-19 / P2-20）
# ===========================================================================
class TestSerialClientHardening:
    """P2-18/P2-19/P2-20: 写超时、零超时语义、comports 前置拦截"""

    def test_serial_open_sets_write_timeout(self) -> None:
        """
        构造物理串口时必须设置 write_timeout

        回归点: pyserial 的 timeout 只作用于**读**，写路径默认永久阻塞。
        板卡复位死循环/USB转串口桥固件卡死时 write() 无限挂死，且阻塞发生在
        with 块内 __exit__ 永不执行、串口句柄不释放。
        注: 用假 Serial 捕获构造参数（loop:// 会走 serial_for_url 分支，
        观测不到物理串口的构造参数）
        """
        captured: dict = {}

        class _FakeSerial:
            """捕获构造参数的串口替身"""

            def __init__(self, **kwargs) -> None:
                captured.update(kwargs)
                self.is_open = True

            def close(self) -> None:
                self.is_open = False

        client = serial_client_mod.SerialClient(port="COM1")
        with patch.object(serial_client_mod.serial, "Serial", _FakeSerial):
            client.open()
        try:
            assert "write_timeout" in captured, (
                f"构造物理串口必须传 write_timeout | 实际参数: {sorted(captured)}"
            )
            assert captured["write_timeout"] == client.timeout, (
                "write_timeout 应复用读超时配置"
            )
        finally:
            client.close()

    def test_read_until_zero_timeout_is_respected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        显式传 timeout=0 必须原样生效（x or default 反模式）

        回归点: 修复前是 `timeout or self.timeout`，0 是 falsy 会被静默
        改写成默认3.0s。批量探测端口可用性的轮询逻辑每轮多等3秒。
        """
        client = serial_client_mod.SerialClient(port="loop://")
        client.open()
        try:
            start = time.perf_counter()
            with pytest.raises(serial_client_mod.SerialClientError):
                client.read_until(expect="##NEVER_APPEAR##", timeout=0)
            elapsed = time.perf_counter() - start
            assert elapsed < 1.0, (
                f"显式0超时未生效，实际耗时 {elapsed:.2f}s（疑似用了默认超时）"
            )
        finally:
            client.close()

    def test_open_does_not_precheck_comports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        open 不得再用 comports 前置硬拦截

        回归点: macOS 的 pyserial comports() 不枚举 /dev/cu.*，而
        serial.Serial 本身能打开——修复前在 macOS 上板卡测试完全不可用。
        本用例让 comports 返回空列表，open 仍应成功走到实际打开逻辑。
        """
        monkeypatch.setattr(serial.tools.list_ports, "comports", lambda: [])

        client = serial_client_mod.SerialClient(port="loop://")
        # loop:// 不在 comports 列表里（列表已被清空），修复前会被直接拒绝
        client.open()
        try:
            assert client.is_open, "comports 为空时仍应能打开 loop:// 伪串口"
        finally:
            client.close()

    def test_open_unknown_protocol_url_raises_client_error(self) -> None:
        """
        未知协议URL必须抛 SerialClientError 而非裸 ValueError

        回归点: serial_for_url 对未知协议抛 ValueError（不是 SerialException
        子类），修复前会裸逃出 open()，调用方按文档写的
        `except SerialClientError` 接不住。
        """
        client = serial_client_mod.SerialClient(port="nosuchproto://x")
        with pytest.raises(serial_client_mod.SerialClientError):
            client.open()
        assert client._serial is None, "打开失败后不得残留半开实例"


# ===========================================================================
# D组: 用例类型推断与批次号去重（P2-21 / P2-22 / P2-24）
# ===========================================================================
class TestCaseTypeInference:
    """P2-21: 用例类型只按文件名推断，不受路径中的账户名干扰"""

    def test_infer_case_type_only_matches_file_name(self) -> None:
        """
        账户名/父目录含关键词不得影响推断结果

        回归点: 修复前对整条绝对路径做子串匹配。部署机账户名含 chip/serial/
        telnet（如 telnet_lab）时，该用户上传的所有文件都被误判为 chip，
        连纯 API 用例也会被标成板卡类型。
        """
        infer = CaseManager._infer_case_type

        # 账户名含关键词 + 纯API文件名 -> 应判 api
        assert infer("/home/telnet_lab/api_login.yaml") == "api", (
            "父目录含 telnet 不得把纯API文件判成chip"
        )
        assert (
            infer("C:/Users/serial.wang/Temp/api_user_query.xlsx") == "api"
        ), "账户名含 serial 不得把纯API文件判成chip"
        assert (
            infer("/opt/chipboard/ordinary_data.yaml") == "api"
        ), "父目录含 chip 不得把纯API文件判成chip"

        # 文件名含关键词 -> 仍应判 chip
        assert infer("board_chip.yaml") == "chip"
        assert infer("data/serial_test.xlsx") == "chip"
        assert infer("telnet_cmds.yaml") == "chip"

        # 反斜杠路径同样只看文件名
        assert infer("C:\\work\\serial\\api_cases.yaml") == "api"


class TestExecutionIdSetBounded:
    """P2-24: 批次号去重集合必须有界"""

    def test_execution_id_set_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        集合超上限必须被重置，不得单调增长

        回归点: 修复前集合只增不减，长跑进程内存单调增长（每天1000批次
        一年36万条）。
        """
        from src.core import case_manager as cm

        # 把上限临时压到极小值，便于观察重置行为
        monkeypatch.setattr(cm, "MAX_TRACKED_EXECUTION_IDS", 5)
        original = cm._generated_execution_ids
        cm._generated_execution_ids = set()
        try:
            sizes = []
            for _ in range(20):
                generate_execution_id()
                sizes.append(len(cm._generated_execution_ids))

            assert max(sizes) <= 6, (
                f"去重集合不得单调增长，超上限应被重置 | 观测到最大 {max(sizes)}"
            )
            assert len(cm._generated_execution_ids) < 20, (
                "20次生成后集合仍应被限制在很小的规模内，"
                f"实际 {len(cm._generated_execution_ids)}"
            )
        finally:
            cm._generated_execution_ids = original

    def test_default_cap_is_documented_value(self) -> None:
        """默认上限为文档约定的1万（回归点：防止被误改成过小值）"""
        assert MAX_TRACKED_EXECUTION_IDS == 10_000


# ===========================================================================
# E组: 通知重试与死信（P2-26 / P2-29）
# ===========================================================================
class TestNotifyRetryBaseDelay:
    """P2-26: 退避基数下限校验，避免通知静默丢失"""

    @pytest.mark.parametrize("bad_value", [-1.0, float("nan"), float("inf")])
    def test_base_delay_rejects_negative_and_nan(self, bad_value: float) -> None:
        """
        负值/NaN/Inf 必须回落到默认值

        回归点: 修复前只有 float() 强转。负值让 time.sleep(负数) 抛
        ValueError，异常从 _send_with_retry 逃到 notify 兜底 → 渠道记失败
        但**不写死信也不写历史**，通知静默丢失且无任何留痕。
        """
        router = NotificationRouter(base_delay=bad_value, max_retries=0)
        assert router.base_delay > 0, (
            f"退避基数必须为正数，实际 {router.base_delay}（输入 {bad_value!r}）"
        )

    def test_base_delay_valid_value_kept(self) -> None:
        """合法退避基数原样保留（不回归）"""
        router = NotificationRouter(base_delay=2.5, max_retries=0)
        assert router.base_delay == 2.5


class TestDeadLetterList:
    """P2-29: 死信列表取最新N条且正文截断"""

    def test_dead_letter_list_returns_newest(self, db_path_env: Path) -> None:
        """
        list_all 必须返回**最新**N条而非最旧N条

        回归点: 修复前 order_by(id).limit(N) 取的是最旧N条
        （docstring 写"最近N条"但行为相反）。死信持续累积，总量超上限后
        新死信将永远取不到——而新死信恰恰是排查时最需要看的。
        """
        from src.db import models

        with DatabaseSession.session_scope() as session:
            for index in range(1, 6):
                session.add(
                    models.NotificationDeadLetter(
                        channel="email",
                        execution_id=f"RUN-{index:04d}",
                        title=f"标题{index}",
                        content="正文",
                        level="warning",
                        fail_reason="原因",
                        attempts=1,
                    )
                )

        rows = NotificationDeadLetterRepository.list_all(limit=2)
        assert len(rows) == 2, f"应返回2条，实际 {len(rows)}"
        titles = [row["title"] for row in rows]
        assert titles == ["标题4", "标题5"], (
            f"应返回最新的两条且保持id升序 | 实际: {titles}"
        )

    def test_dead_letter_content_is_truncated(self, db_path_env: Path) -> None:
        """
        死信正文超长必须截断并标记

        回归点: 死信正文是整封含全部失败明细的HTML报告，单条可达百KB级。
        列表接口一次取最多1万条整体返回，不截断会把上万份完整报告读进内存。
        """
        from src.db import models

        long_content = "X" * (DEAD_LETTER_CONTENT_PREVIEW + 5000)
        with DatabaseSession.session_scope() as session:
            session.add(
                models.NotificationDeadLetter(
                    channel="email",
                    execution_id="RUN-LONG",
                    title="长正文",
                    content=long_content,
                    level="warning",
                    fail_reason="原因",
                    attempts=1,
                )
            )

        row = NotificationDeadLetterRepository.list_all(limit=1)[0]

        assert row["content_truncated"] is True, "超长正文必须标记为已截断"
        assert len(row["content"]) < len(long_content), "列表返回的正文应被截断"
        assert row["content"].endswith("...[已截断]"), "截断标记应可见"
        assert "X" * 100 in row["content"], "截断后仍应保留正文开头内容"

    def test_short_content_not_marked_truncated(self, db_path_env: Path) -> None:
        """未超长的正文不标记截断（不回归）"""
        from src.db import models

        with DatabaseSession.session_scope() as session:
            session.add(
                models.NotificationDeadLetter(
                    channel="email",
                    execution_id="RUN-SHORT",
                    title="短正文",
                    content="短正文内容",
                    level="warning",
                    fail_reason="原因",
                    attempts=1,
                )
            )

        row = NotificationDeadLetterRepository.list_all(limit=1)[0]
        assert row["content_truncated"] is False
        assert row["content"] == "短正文内容", "未超长时正文应原样返回"


# ===========================================================================
# F组: http_client 重试方法白名单（P2-39）
# ===========================================================================
class TestHttpClientRetryScope:
    """P2-39: 只对幂等只读方法自动重试"""

    def test_retry_only_for_idempotent_methods(self) -> None:
        """
        POST/PUT/PATCH/DELETE 不得进入自动重试白名单

        回归点: 修复前 allowed_methods=None，所有方法都重试。本项目是
        **测试平台**，重试打到被测系统的写请求会重复建资源，测试结论失真。
        """
        assert IDEMPOTENT_METHODS == frozenset({"GET", "HEAD", "OPTIONS"}), (
            f"重试白名单应收敛为只读幂等方法，实际 {sorted(IDEMPOTENT_METHODS)}"
        )
        for unsafe in ("POST", "PUT", "PATCH", "DELETE"):
            assert unsafe not in IDEMPOTENT_METHODS, (
                f"{unsafe} 非幂等副作用方法不得自动重试"
            )

    def test_client_retry_policy_applied(self) -> None:
        """HttpClient 实际挂载的 adapter 采用了该白名单（不回归：能正常构造）"""
        client = HttpClient(base_url="https://api.example.com")
        try:
            adapter = client.session.get_adapter("https://api.example.com")
            assert adapter.max_retries.allowed_methods == IDEMPOTENT_METHODS, (
                f"实际生效的重试白名单不符: {adapter.max_retries.allowed_methods}"
            )
        finally:
            client.close()


# ===========================================================================
# 共享常量
# ===========================================================================
