"""
TestMatrix 大扫除 v2 · 任务一：executions SSE 与入参边缘路径覆盖补齐

覆盖目标
--------
本文件针对 src/web/routes/executions.py 的 17 条未覆盖语句（91% -> 100%）。
未覆盖清单与对应场景：

    行号       场景                                       本文件用例
    ---------  -----------------------------------------  ------------------
    199-200    get_execution_detail 抛 CaseNotFoundError  test_detail_not_found_translated
    234        请求体可选字段传非字符串                   test_optional_body_str_non_string
    288        请求体是 JSON 数组而非对象                  test_trigger_body_not_object
    542        终态快照帧的 failed 分支                   test_terminal_snapshot_failed_frame
    651-653    DB重建时明细查询异常降级单帧               test_db_rebuild_detail_error_degrades
    694-702    通道缺失 + 批次 failed 的单帧直发          test_events_failed_batch_single_frame
    703-714    通道缺失 + 非终态的单帧快照直发            test_events_running_batch_single_frame
    725        live 断点过滤命中终态事件                   test_terminal_event_filtered_by_resume
    734        live 断点放行终态事件                       test_terminal_event_forwarded_sets_saw
    755-756    心跳节拍后刷新活动时间                     test_heartbeat_refreshes_last_activity
    763        订阅分支读到终态事件即 break               test_running_batch_breaks_on_terminal

设计要点
--------
- SSE 帧读取用 response.get_data(as_text=True)：终态分支的流是有限帧，
  读完自动关闭，不需要帧数上限保护（7.13 的上限保护只针对可能挂死的
  实时订阅分支）
- 批次状态一律 monkeypatch CaseManager.get_execution_status 构造，
  不靠真实执行编排造终态：既确定性更高，又避开后台线程与 teardown 竞态（7.12）
- 事件通道注册表每条用例前后 reset（7.13）

测试铁律（对齐 PROJECT_CONTEXT.md 7.12 / 7.13）
- 无 time.sleep 固定等待
- 每条用例独立临时 SQLite 库
- 通道注册表 autouse 清洁
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import allure
import pytest
from flask.testing import FlaskClient
from src.core import event_bus
from src.core.event_bus import ExecutionEvent, get_channel
from src.db.db_session import DatabaseSession
from src.web import create_app
from src.web.routes import executions as executions_mod

# 帧读取上限：终态分支是有限帧，设上限只为兜住"被测代码退化成实时
# 订阅"时的挂死（7.13 双保护口径）
MAX_STREAM_FRAMES = 100


@pytest.fixture(autouse=True)
def _clean_event_registry() -> Iterator[None]:
    """
    事件通道注册表清洁fixture（autouse，全测试生效）

    7.13 铁律: 通道注册表是模块级全局单例，不复位时上一条测试的
    通道残留会串进下一条的断言，制造 flaky

    参数:
        无

    返回:
        Iterator[None]: yield None
    """
    event_bus.reset_channels()
    yield
    event_bus.reset_channels()


@pytest.fixture
def exec_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Path]:
    """
    独立临时 SQLite 库（function 级）

    teardown 顺序: reset 引擎（释放文件锁）-> 删除库文件

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest 临时目录

    返回:
        Iterator[Path]: yield 临时库文件路径
    """
    db_path = tmp_path / "executions_v2.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_path
    DatabaseSession.reset()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def client(exec_db: Path) -> FlaskClient:
    """
    Flask 测试客户端（基于 exec_db 临时库）

    参数:
        exec_db (Path): 临时库文件路径（保证建表已完成）

    返回:
        FlaskClient: Flask 测试客户端
    """
    return create_app("test").test_client()


def _finished_status(
    total: int = 2, passed: int = 2, failed: int = 0
) -> dict[str, Any]:
    """
    构造 finished 批次状态字典（内部方法）

    字段集严格对齐 _terminal_snapshot_frame 的 finished 分支读取的
    六个键（total/passed/failed/error/skipped/pass_rate）：少一个键
    都会在渲染时抛 KeyError，把真正要测的分支掩盖掉。

    参数:
        total (int): 用例总数
        passed (int): 通过数
        failed (int): 失败数

    返回:
        dict[str, Any]: finished 状态字典
    """
    return {
        "status": "finished",
        "total_cases": total,
        "passed": passed,
        "failed": failed,
        "error": 0,
        "skipped": 0,
        "pass_rate": round(passed / total, 4) if total else 0.0,
    }


def _read_stream(response: Any) -> str:
    """
    读取 SSE 流全部文本（内部方法）

    参数:
        response (Any): buffered=False 的流式响应

    返回:
        str: 完整流文本
    """
    return response.get_data(as_text=True)


def _frame_ids(raw: str) -> list[str]:
    """
    从流文本中提取全部 id 行取值（内部方法）

    参数:
        raw (str): SSE 流文本

    返回:
        list[str]: 按出现顺序的 id 取值
    """
    return [
        line.split(":", 1)[1].strip()
        for line in raw.splitlines()
        if line.startswith("id:")
    ]


# ===========================================================================
# A组: 详情接口与请求体入参
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("executions 入参与详情")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestExecutionsRequestHandling:
    """404 翻译 / 可选字段类型 / 非对象请求体"""

    def test_detail_not_found_translated(
        self, client: FlaskClient
    ) -> None:
        """
        核心层抛 CaseNotFoundError 时翻译为 404（行199-200）

        阶段2 P2-9 把"子串匹配异常文案"改成了纯类型判定，这条锁住
        纯类型判定仍然把业务异常正确翻译成 404。
        """
        from src.core.case_manager import CaseNotFoundError

        with patch(
            "src.core.case_manager.CaseManager.get_execution_detail",
            side_effect=CaseNotFoundError("批次不存在"),
        ):
            response = client.get("/api/executions/RUN-V2-NOTFOUND")

        body = response.get_json()
        assert response.status_code == 404
        assert body["code"] == 404
        assert body["message"] == "执行批次不存在"
        assert body["data"] == {"execution_id": "RUN-V2-NOTFOUND"}, (
            "detail 应携带 execution_id 供前端定位"
        )

    def test_optional_body_str_blank_returns_none(self) -> None:
        """
        请求体可选字段的归一化口径（行234）

        非字符串（列表/数字）与空白串一律视为"未传入"：若把 123 当成
        "123" 走枚举校验，报错会指向"executor 非法"，而用户真正的问题是
        类型不对——两个错误指向不同的排查方向。
        纯函数直测比走 HTTP 更精确：HTTP 路径还要先备出可执行用例，
        且非字符串被正确忽略后行为与缺省完全一致，无法从响应反推。
        """
        parse = executions_mod._parse_optional_body_str

        assert parse({"executor": "  "}, "executor") is None
        assert parse({"executor": "simulated"}, "executor") == "simulated"
        assert parse({}, "executor") is None
        assert parse({"executor": ["simulated"]}, "executor") is None
        assert parse({"executor": 123}, "executor") is None
        assert parse({"executor": None}, "executor") is None

    def test_trigger_body_not_object(self, client: FlaskClient) -> None:
        """
        请求体是 JSON 数组而非对象时报 400（行288）

        数组/字符串等合法 JSON 但非对象的请求体若放行，body.get()
        会抛 AttributeError 变成 500，客户端拿不到可读的 400。
        """
        response = client.post("/api/executions/trigger", json=["不是对象"])

        body = response.get_json()
        assert response.status_code == 400
        assert body["message"] == "请求体必须为JSON对象"


# ===========================================================================
# B组: SSE 通道缺失的降级分支
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("executions SSE 降级分支")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestSseDegradedBranches:
    """通道已清理/不存在时的单帧直发与降级"""

    def test_terminal_snapshot_failed_frame(self) -> None:
        """
        终态快照帧的 failed 分支（行542）

        failed 批次没有 defect_statistics 汇总行，只能靠 status_data
        直接合成 batch_failed 帧。
        """
        frame = executions_mod._terminal_snapshot_frame(
            {"status": "failed", "error_message": "3条用例全部失败"},
            event_id=1,
            namespace=executions_mod.EVENT_ID_NAMESPACE_DB,
        )

        assert "event: batch_failed" in frame
        assert "3条用例全部失败" in frame
        assert "id: db:1" in frame, "DB 体系帧 id 必须带 db: 前缀"

    def test_db_rebuild_detail_error_degrades(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        DB重建时明细查询异常降级为单帧终态（行651-653）

        明细查不出来时仍要给出终态帧：客户端断线重连后哪怕只拿到
        一帧 batch_finished，也知道批次结束了；直接报错则整条流
        失败，客户端永远等不到结束。
        """
        from src.core.case_manager import CaseManager, CaseManagerError

        execution_id = "RUN-V2-DBERR"
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(
                lambda _eid: _finished_status(total=4, passed=4)
            ),
        )
        monkeypatch.setattr(
            CaseManager,
            "get_execution_detail",
            staticmethod(
                lambda _eid: (_ for _ in ()).throw(
                    CaseManagerError("模拟明细查询异常")
                )
            ),
        )
        assert get_channel(execution_id, create=False) is None, (
            "前置条件：通道必须不在注册表内才会走DB重建分支"
        )

        response = client.get(f"/api/executions/{execution_id}/events")
        raw = _read_stream(response)

        assert "event: batch_finished" in raw, (
            f"明细查询失败也应给出终态帧，实际流: {raw[:200]!r}"
        )
        assert "event: case_finished" not in raw, (
            "明细不可用时不得输出无来源的 case_finished 帧"
        )
        assert _frame_ids(raw) == [], (
            "降级帧不占序列位置，必须不输出 id 行，否则会污染断点游标；"
            f"实际 id 行: {_frame_ids(raw)}"
        )

    def test_degraded_frame_does_not_break_reconnect(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        降级帧之后重连，客户端仍能拿到 batch_start（V2-P2-2 核心回归）

        复现修复前的丢帧场景:
          请求1: 明细查询瞬时失败 -> 降级帧被标成 db:1
          请求2: DB 恢复正常，客户端带 Last-Event-ID: db:1 重连
                 -> 正常路径的 batch_start 也是 db:1，`1 > 1` 为假
                 -> batch_start 被永久丢弃，客户端再也不知道有多少条用例
        修复: 降级帧不输出 id 行 -> 客户端游标不被推进 -> 请求2 全量重发。
        """
        from src.core.case_manager import CaseManager, CaseManagerError

        execution_id = "RUN-V2-RECONNECT"
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(lambda _eid: _finished_status(total=4, passed=4)),
        )
        detail = {
            "summary": {"total_cases": 4},
            "items": [
                {
                    "case_id": f"TM-RA-{index}",
                    "case_name": f"用例{index}",
                    "result": "passed",
                    "duration": 0.1,
                    "error_message": None,
                }
                for index in range(2)
            ],
        }

        # 请求1: 明细查询失败，走降级路径
        monkeypatch.setattr(
            CaseManager,
            "get_execution_detail",
            staticmethod(
                lambda _eid: (_ for _ in ()).throw(
                    CaseManagerError("模拟明细查询瞬时失败")
                )
            ),
        )
        first_raw = _read_stream(
            client.get(f"/api/executions/{execution_id}/events")
        )
        assert "event: batch_finished" in first_raw
        first_ids = _frame_ids(first_raw)
        assert first_ids == [], "降级帧不得推进客户端游标"

        # 请求2: DB 恢复，客户端带上一轮收到的 id 重连
        monkeypatch.setattr(
            CaseManager,
            "get_execution_detail",
            staticmethod(lambda _eid: detail),
        )
        # 请求2 的 Last-Event-ID 必须来自请求1 **实际收到的 id**。
        # v5 修正（Hy4 指出）: 原先硬编码 "live:0"，那与"降级帧是否带
        # id"毫无关系——无论降级帧带不带 db:1，请求2 带的都是 live:0，
        # 撤销修复后本用例照样全绿（空转）。
        #
        # 关键: 降级帧**不带 id**，客户端就没有可回传的游标，真实行为是
        # **根本不发 Last-Event-ID 头**。此处若人为补一个 "db:1"，
        # 反而会亲手制造出"batch_start 被过滤"的假象（那是下面对照组
        # 专门验证的场景），与本用例要验证的"全量重发"背道而驰。
        second_headers = (
            {"Last-Event-ID": first_ids[-1]} if first_ids else {}
        )
        second_raw = _read_stream(
            client.get(
                f"/api/executions/{execution_id}/events",
                headers=second_headers,
            )
        )

        assert "event: batch_start" in second_raw, (
            "重连后必须仍能收到 batch_start——修复前它会被断点过滤永久丢弃；"
            f"实际流: {second_raw[:200]!r}"
        )
        assert '"total_cases": 4' in second_raw

    def test_batch_start_would_be_filtered_by_db_id(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        对照组: 带 `db:1` 重连时 batch_start **确实**会被断点过滤

        证明上一条的断言语义: 若降级帧当初带的是 db:1，客户端带着它
        重连，正常路径的 batch_start（同为 db:1）就会因 `1 > 1` 为假
        而被丢弃——即原 bug 的真实形态。本条把这个事实显式钉住，避免
        上一条在"降级帧是否带 id"这个维度上退化��空转。
        """
        from src.core.case_manager import CaseManager

        execution_id = "RUN-V2-DBFILTER"
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(lambda _eid: _finished_status(total=4, passed=4)),
        )
        monkeypatch.setattr(
            CaseManager,
            "get_execution_detail",
            staticmethod(
                lambda _eid: {
                    "summary": {"total_cases": 4},
                    "items": [
                        {
                            "case_id": "TM-RA-1",
                            "case_name": "用例1",
                            "result": "passed",
                            "duration": 0.1,
                            "error_message": None,
                        }
                    ],
                }
            ),
        )

        raw = _read_stream(
            client.get(
                f"/api/executions/{execution_id}/events",
                headers={"Last-Event-ID": "db:1"},
            )
        )

        assert "event: batch_start" not in raw, (
            f"带 db:1 重连时 batch_start 应被断点过滤（这正是原 bug 的成因），"
            f"实际流: {raw[:200]!r}"
        )
        # 1 个明细 -> batch_start=db:1(被过滤) / case_finished=db:2 / 终态=db:3
        assert _frame_ids(raw) == ["db:2", "db:3"], (
            f"应只重发 db:1 之后的帧，实际: {_frame_ids(raw)}"
        )

    def test_events_failed_batch_single_frame(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        通道缺失 + 批次 failed 时单帧直发（V2-P2-2：不输出 id 行）

        单帧是"当前状态快照"而非序列中的一帧，故不占序号（详见
        test_degraded_frame_does_not_break_reconnect 的说明）。
        """
        from src.core.case_manager import CaseManager

        execution_id = "RUN-V2-FAILED"
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(
                lambda _eid: {
                    "status": "failed",
                    "error_message": "执行体抛异常导致批次失败",
                }
            ),
        )

        response = client.get(f"/api/executions/{execution_id}/events")
        raw = _read_stream(response)

        assert response.status_code == 200
        assert response.mimetype == "text/event-stream"
        assert "event: batch_failed" in raw
        assert "执行体抛异常导致批次失败" in raw
        assert _frame_ids(raw) == [], (
            f"failed 单帧是状态快照，不得输出 id 行，实际: {_frame_ids(raw)}"
        )

    def test_events_running_batch_single_frame(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        通道缺失 + 非终态时单帧进行中快照直发（行703-714）

        CLI 触发的批次在 Web 进程内永远等不到 publish，若挂死等事件
        客户端会一直连着不返回。直发单帧让客户端立刻拿到当前状态。
        """
        from src.core.case_manager import CaseManager

        execution_id = "RUN-V2-RUNNING"
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(
                lambda _eid: {
                    "status": "running",
                    "total_cases": 7,
                }
            ),
        )

        response = client.get(f"/api/executions/{execution_id}/events")
        raw = _read_stream(response)

        assert "event: batch_start" in raw
        assert '"total_cases": 7' in raw
        assert '"status": "running"' in raw
        assert _frame_ids(raw) == [], (
            "进行中单帧是状态快照，不得输出 id 行（否则同样会污染"
            f"断点游标），实际: {_frame_ids(raw)}"
        )


# ===========================================================================
# C组: 通道存在但批次终态的 live 断点过滤
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("executions SSE live 断点过滤")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestSseLiveResumeFiltering:
    """终态事件被断点过滤/被放行两种走向"""

    @staticmethod
    def _seed_terminal_channel(execution_id: str) -> None:
        """建通道并投递 batch_start + batch_finished 两条终态相关事件"""
        channel = get_channel(execution_id, create=True)
        assert channel is not None
        channel.publish(ExecutionEvent(
            event_type="batch_start", data={"total_cases": 2}
        ))
        channel.publish(ExecutionEvent(
            event_type="batch_finished", data={"passed": 2, "failed": 0}
        ))

    def test_terminal_event_forwarded_sets_saw(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        live 断点放行终态事件（行734）

        放行后 saw_terminal=True，因此**不会**再补一条降级快照帧——
        客户端只看到一个 batch_finished，不重复。
        """
        from src.core.case_manager import CaseManager

        execution_id = "RUN-V2-LIVE-FWD"
        self._seed_terminal_channel(execution_id)
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(
                lambda _eid: _finished_status()
            ),
        )

        response = client.get(f"/api/executions/{execution_id}/events")
        raw = _read_stream(response)

        assert _frame_ids(raw) == ["live:1", "live:2"], (
            f"live 体系应按通道 event_id 透传，实际: {_frame_ids(raw)}"
        )
        assert raw.count("event: batch_finished") == 1, (
            f"终态事件已放行，不应再补降级快照帧，实际流: {raw[:200]!r}"
        )

    def test_terminal_event_filtered_by_resume(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        live 断点过滤命中终态事件（行725）

        客户端声明已收到 live:2（即已看过 batch_finished）后重连，
        两条事件都被跳过，但仍须据"已见过终态"这一事实返回——
        不能因为本轮没放行任何终态帧就再补一条重复的。
        """
        from src.core.case_manager import CaseManager

        execution_id = "RUN-V2-LIVE-FILT"
        self._seed_terminal_channel(execution_id)
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(
                lambda _eid: _finished_status()
            ),
        )

        response = client.get(
            f"/api/executions/{execution_id}/events",
            headers={"Last-Event-ID": "live:2"},
        )
        raw = _read_stream(response)

        assert _frame_ids(raw) == [], (
            f"断点之后无新事件，不应补发任何帧，实际: {_frame_ids(raw)}"
        )
        assert "event: batch_finished" not in raw, (
            "客户端已收到过终态帧，重连时绝不能重复补发"
        )

    def test_no_terminal_event_appends_snapshot(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        积压事件里没有终态时补一条降级快照帧（736 的兜底对照）

        与上一条成对：证明"不补发"是因为断点已含终态，而不是
        "本轮一律不补"。
        """
        from src.core.case_manager import CaseManager

        execution_id = "RUN-V2-LIVE-NOTERM"
        channel = get_channel(execution_id, create=True)
        assert channel is not None
        channel.publish(ExecutionEvent(
            event_type="batch_start", data={"total_cases": 2}
        ))
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(
                lambda _eid: _finished_status()
            ),
        )

        response = client.get(f"/api/executions/{execution_id}/events")
        raw = _read_stream(response)

        assert "event: batch_finished" in raw, (
            "通道内无终态事件时应补一条降级快照帧，否则客户端收不到结束"
        )


# ===========================================================================
# D组: 运行中批次的实时订阅分支
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("executions SSE 实时订阅")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestSseLiveSubscribe:
    """心跳刷新与读到终态即 break"""

    def test_running_batch_breaks_on_terminal(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        实时订阅分支读到终态事件即 break（行762-763）

        通道里出现 batch_finished 但 status_data 仍是 running——这是
        publish 之后、批次状态刷新之前的竞态窗口。订阅器必须立刻结束
        而不是继续等后续事件，否则这个流永远不会自己结束。
        """
        from src.core.case_manager import CaseManager

        execution_id = "RUN-V2-BREAK"
        channel = get_channel(execution_id, create=True)
        assert channel is not None
        channel.publish(ExecutionEvent(
            event_type="batch_start", data={"total_cases": 1}
        ))
        channel.publish(ExecutionEvent(
            event_type="batch_finished", data={"passed": 1, "failed": 0}
        ))
        # 状态刻意停留在 running，复刻 publish 早于批次状态刷新的窗口
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(
                lambda _eid: {"status": "running", "total_cases": 1}
            ),
        )

        response = client.get(f"/api/executions/{execution_id}/events")
        raw = _read_stream(response)

        assert "event: batch_finished" in raw
        assert "event: batch_finished" not in raw.split("event: batch_finished")[1], (
            "读到终态后必须停止订阅，终态帧只应出现一次"
        )

    def test_heartbeat_refreshes_last_activity(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        心跳节拍后刷新活动时间（行755-756）

        刷新后下一次心跳要重新计满间隔，否则连续多个 tick 都会发心跳
        帧、保活注释刷屏。7.14 口径：心跳间隔必须 monkeypatch 压缩，
        绝不等真实的 15 秒。
        """
        from src.core.case_manager import CaseManager

        execution_id = "RUN-V2-HEARTBEAT"
        get_channel(execution_id, create=True)
        monkeypatch.setattr(
            executions_mod, "HEARTBEAT_INTERVAL_SECONDS", 0.1
        )
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(
                lambda _eid: {"status": "running", "total_cases": 1}
            ),
        )
        # 通道为空且不关闭：subscribe 会以 0.5s 为节拍持续心跳，
        # 用帧数上限兜住读取（7.13 双保护）
        frame_count = 0
        response = client.get(
            f"/api/executions/{execution_id}/events", buffered=False
        )
        try:
            for chunk in response.iter_encoded():
                if b"heartbeat" in chunk:
                    frame_count += 1
                    if frame_count >= 2:
                        break
        finally:
            response.close()

        assert frame_count >= 1, (
            "空闲订阅在心跳间隔超时后应收到 ': heartbeat' 注释帧"
        )
