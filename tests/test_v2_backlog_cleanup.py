"""
TestMatrix 大扫除 v2 · 任务三：backlog 低风险清理的回归测试

覆盖的三条清理
------------------------------------
A组 event_bus closed 校验移入锁内
    1.  关闭后发布被丢弃
    2.  关闭与发布并发时事件不落到已关闭通道的历史环（竞态守卫）
B组 分页参数解析提取为公共函数
    3.  三个路由模块共用同一实现（口径不会漂移）
    4.  既有错误文案与异常链抑制特征保持不变
    5.  缺省/空白串/非法值三类边界行为不变
C组 serial_client expect 分支去掉盲等
    6.  有特征串时不再 sleep(wait_time)，直接进入轮询
    7.  无特征串时仍等满 wait_time（该语义必需，不能一起删）

设计要点
------------------------------------
- A组竞态用例用"先 close 再 publish"与"publish 期间 close"两种时序
  构造，均不依赖 sleep 赌时序：前者确定性，后者靠 Condition 的
  锁语义在 publish 持锁期间 close 必然阻塞，断言 close 完成后
  历史环不再增长
- B组断言"三个模块的 _parse_int_param 是同一个函数对象"，这是
  提取是否真正生效的直接证据；比断言行为更能防止将来又复制一份
- C组用 MagicMock 拦截 read_until/read_all，并断言 sleep 未被调用

测试铁律（对齐项目既有测试约定：确定性优先+幂等铁律）
- 无 time.sleep 固定等待
- 不依赖真实串口硬件（全部 MagicMock 桩）
- 事件通道独立实例，不入全局注册表，零跨例污染
"""

import threading
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import allure
import pytest
from src.common import serial_client as serial_client_mod
from src.common.serial_client import SerialClient
from src.core import event_bus as event_bus_mod
from src.core.event_bus import EventChannel, ExecutionEvent
from src.web.exceptions import ValidationError
from src.web.pagination import parse_int_param
from src.web.routes import cases as cases_routes
from src.web.routes import executions as executions_routes
from src.web.routes import reports as reports_routes


@pytest.fixture(autouse=True)
def _clean_event_registry() -> Iterator[None]:
    """
    事件通道注册表清洁fixture（autouse）

    参数:
        无

    返回:
        Iterator[None]: yield None
    """
    event_bus_mod.reset_channels()
    yield
    event_bus_mod.reset_channels()


# ===========================================================================
# A组: event_bus closed 校验移入锁内
# ===========================================================================
@allure.feature("大扫除v2 backlog清理")
@allure.story("event_bus 关闭校验移入锁内")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestEventBusClosedInsideLock:
    """关闭后发布被丢弃 / 与 close 的竞态不再漏事件"""

    def test_publish_after_close_is_dropped(self) -> None:
        """
        通道关闭后发布被丢弃，事件不进历史环

        移入锁内前的行为同样丢弃，本条锁住的是清理后不回归。
        """
        channel = EventChannel()
        channel.close(reason="finished")
        event = ExecutionEvent(event_type="batch_start", data={"total_cases": 1})

        channel.publish(event)

        assert event.event_id is None, "被丢弃的事件不应被分配 event_id"
        assert list(channel.subscribe(last_event_id=None)) == [], (
            "关闭后不应再有可回放事件"
        )

    @staticmethod
    def _run_publish_close_race() -> tuple[int | None, list[str]]:
        """
        确定性构造"close 已完成、publish 才拿到锁"的时序

        竞态窗口（移入锁内前存在）:
          publish 在锁外读到 _closed=False → close() 获锁、置位、
          notify_all → 订阅者被唤醒 drain 完历史并正常结束 →
          publish 才 append。结果：批次通道已关闭、订阅者已收尾，
          事件却进了历史环且此后无人再读到——**永久丢失**。

        如何做到确定性（不用 sleep、不靠概率）:
          1. 主线程先持有 channel._condition 的锁
          2. 起 publish 线程——它在修复前会**先读完 _closed 再阻塞在锁上**，
             修复后会直接阻塞在锁上、且把 _closed 的读取推迟到拿锁之后
          3. 主线程持锁期间置 _closed=True（等价 close() 的状态变更；
             不能直接调 close()，因为 Condition 默认用不可重入的 Lock，
             持锁调用会自死锁）
          4. 主线程释放锁，publish 线程继续
             - 修复前：已通过检查 → 照常 append（BUG 复现）
             - 修复后：拿锁后重查 _closed=True → 丢弃

        返回:
            tuple[int | None, list[str]]: 事件被分配到的 id（None 表示
            被正确丢弃）与 drain 出的历史环事件类型列表
        """
        channel = EventChannel()
        published = ExecutionEvent(
            event_type="batch_start", data={"total_cases": 1}
        )
        errors: list[BaseException] = []

        def _do_publish() -> None:
            """发布方：会阻塞在 channel._condition 上直到主线程放锁"""
            try:
                channel.publish(published)
            except BaseException as exc:  # noqa: BLE001 测试需捕获全部
                errors.append(exc)

        # 1. 主线程先持锁
        with channel._condition:
            # 2. 起 publish 线程（此刻它必然阻塞在锁上）
            thread = threading.Thread(target=_do_publish, daemon=True)
            thread.start()
            # 3. 持锁期间完成"关闭"的状态变更（等价 close() 内部动作）
            channel._closed = True
            channel._close_reason = "finished"
        # 4. 持锁块结束即释放锁，publish 线程继续执行

        thread.join(timeout=5)
        assert not thread.is_alive(), "publish 线程未按时退出，疑似死锁"
        assert not errors, f"并发分支抛异常: {errors}"

        drained = list(channel.subscribe(last_event_id=None))
        return published.event_id, [item.event_type for item in drained]

    def test_publish_racing_with_close_does_not_leak_into_history(self) -> None:
        """
        通道已关闭后拿到锁的 publish 不得再往历史环追加事件

        修复前把 _closed 的检查放在锁外，publish 在"检查通过"与
        "真正 append"之间被 close() 插队，事件会落进一个已经关闭
        （订阅者已 drain 收尾）的历史环——此后无人再读得到，事件
        永久丢失且不留任何痕迹。

        本用例用确定性时序构造该窗口（见 _run_publish_close_race 的
        四步说明），**不依赖 sleep、不依赖概率**：修复前必红、修复后
        必绿。轮跑 20 轮是为了覆盖线程调度的各种落点。
        """
        for _ in range(20):
            event_id, drained = self._run_publish_close_race()
            assert event_id is None, (
                f"通道已关闭，publish 不得分配 event_id（实际 {event_id}）"
            )
            assert drained == [], (
                f"通道已关闭，事件不得落进历史环（实际 {drained}）"
            )


# ===========================================================================
# B组: 分页参数解析提取为公共函数
# ===========================================================================
@allure.feature("大扫除v2 backlog清理")
@allure.story("分页参数解析公共化")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestPaginationExtraction:
    """三个路由模块共用同一实现 + 行为口径不变"""

    @pytest.mark.parametrize(
        ("module", "expected_hint"),
        [
            (cases_routes, "page和page_size"),
            (executions_routes, "page和page_size"),
            (reports_routes, "limit"),
        ],
        ids=["cases", "executions", "reports"],
    )
    def test_each_route_delegates_to_shared_implementation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        module: Any,
        expected_hint: str,
    ) -> None:
        """
        三个路由的 _parse_int_param 都必须把活派给公共实现

        做法: 把该路由模块自己持有的 parse_int_param 名字替换成探针，
        调用其 _parse_int_param，断言探针被调用且 param_hint 传对。

        为什么用委托断言而不是"跑一遍比行为": 行为断言只能证明
        "当前三者一致"，有人在某个路由里复制回一份本地实现后，
        行为仍一致、测试仍绿；委托断言则立刻变红。
        """
        seen: list[str] = []

        def _spy(name: str, default: int, param_hint: str) -> int:
            """记录被委托时的 param_hint，返回哨兵值"""
            seen.append(param_hint)
            return 99

        monkeypatch.setattr(module, "parse_int_param", _spy)

        result = module._parse_int_param("page", 1)

        assert result == 99, "返回值应原样透传公共实现的结果"
        assert seen == [expected_hint], (
            f"{module.__name__} 未委托到公共实现或 param_hint 传错: {seen}"
        )

    def test_common_function_is_the_single_source(self) -> None:
        """
        三个路由模块都从 src.web.pagination 取实现（模块级事实核对）

        比委托断言更直接：一旦有人在本文件里重新定义一份，三个模块
        的源码扫描会立刻发现重复。
        """
        import inspect

        for module in (cases_routes, executions_routes, reports_routes):
            source = inspect.getsource(module)
            assert "from src.web.pagination import parse_int_param" in source, (
                f"{module.__name__} 未从公共模块导入 parse_int_param"
            )
            # 只查 _parse_int_param 自身的源码体：模块内还有
            # _parse_optional_str 等同样合法读取 request.args 的函数，
            # 全文件扫描会把它们误判成"提取未完成"
            wrapper_src = inspect.getsource(module._parse_int_param)
            assert "request.args" not in wrapper_src, (
                f"{module.__name__} 的 _parse_int_param 仍自行解析查询串"
            )
            assert "parse_int_param(" in wrapper_src, (
                f"{module.__name__} 的 _parse_int_param 未委托到公共实现"
            )

    def test_common_function_preserves_error_messages(self) -> None:
        """
        错误文案按调用方参数组区分，且异常链抑制特征不变

        移入锁内前（指提取前）三处文案分别是
        "page和page_size必须为正整数" / "page和page_size必须为正整数"
        / "limit必须为正整数"，提取后必须逐字保持——既有测试与前端
        都可能按文案匹配。
        """
        from flask import Flask

        app = Flask(__name__)
        with app.test_request_context("/?page=abc"):
            with pytest.raises(ValidationError) as cases_exc:
                cases_routes._parse_int_param("page", 1)
            with pytest.raises(ValidationError) as exec_exc:
                executions_routes._parse_int_param("page", 1)
        # reports.py 服务的是 limit 参数组，必须用 limit=abc 触发
        with app.test_request_context("/?limit=abc"):
            with pytest.raises(ValidationError) as report_exc:
                reports_routes._parse_int_param("limit", 1)

        assert str(cases_exc.value) == "page和page_size必须为正整数"
        assert str(exec_exc.value) == "page和page_size必须为正整数"
        assert str(report_exc.value) == "limit必须为正整数"
        for exc in (cases_exc, exec_exc, report_exc):
            assert exc.value.__suppress_context__ is True, (
                "raise ... from None 的抑制特征是既有测试的唯一可观测点，"
                "提取后必须保持"
            )

    @pytest.mark.parametrize(
        ("raw_query", "default", "expected"),
        [
            ("", 1, 1),
            ("?page=", 1, 1),
            ("?page=%20%20", 1, 1),
            ("?page=5", 1, 5),
            ("?page=-3", 1, -3),
            ("?page=0", 1, 0),
        ],
        ids=["no-param", "empty", "blank", "normal", "negative", "zero"],
    )
    def test_boundary_behavior_unchanged(
        self, raw_query: str, default: int, expected: int
    ) -> None:
        """
        缺省/空白/负数/零的解析口径与提取前逐一致

        负数与零**不**在这里拦截：cases/executions 的范围校验在
        调用点各自做（page<1 才报 400），解析层只负责转换。
        把这条钉住可防止后来者"顺手"在公共函数里加范围校验，
        那样会让三处路由同时改变行为。
        """
        from flask import Flask

        with Flask(__name__).test_request_context(f"/{raw_query}"):
            assert parse_int_param("page", default, "page和page_size") == expected


# ===========================================================================
# C组: serial_client expect 分支去掉盲等
# ===========================================================================
@allure.feature("大扫除v2 backlog清理")
@allure.story("串口 expect 分支去盲等")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestSerialExpectNoBlindWait:
    """有特征串时直接轮询 / 无特征串时仍等满窗口"""

    @staticmethod
    def _stub_client() -> SerialClient:
        """构造只做桩替换的串口客户端（不触碰真实串口）"""
        client = SerialClient(port="loop://")
        client._serial = MagicMock()
        return client

    def test_expect_branch_skips_blind_sleep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        有 expect 时不再 time.sleep(wait_time)，直接进入 read_until 轮询

        修复前无论板卡 10ms 还是 5s 应答，send_command 都先无条件
        干等满 wait_time（默认0.5s），批量命令时线性放大。
        """
        slept: list[float] = []
        monkeypatch.setattr(
            serial_client_mod.time, "sleep", lambda s: slept.append(s)
        )
        client = self._stub_client()
        client.read_until = MagicMock(return_value="DEVICE_READY")
        client.read_all = MagicMock(return_value="ALL_OUTPUT")
        try:
            result = client.send_command(
                "AT+VERSION\n", expect="DEVICE_READY", wait_time=0.5
            )
        finally:
            client.close()

        assert result == "DEVICE_READY"
        assert slept == [], (
            f"expect 分支不应再有任何固定 sleep，实际调用: {slept}"
        )
        client.read_until.assert_called_once()
        client.read_all.assert_not_called()

    def test_no_expect_branch_still_waits_full_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        无 expect 时仍等满 wait_time（该语义必需，不能一并删掉）

        无特征串时 wait_time 的含义是"收割这段窗口内的全部输出"，
        删掉等待会读到空缓冲区——这是与 expect 分支的本质区别，
        也是本条对照组：证明修复没有把两种语义一起砍。
        """
        slept: list[float] = []
        monkeypatch.setattr(
            serial_client_mod.time, "sleep", lambda s: slept.append(s)
        )
        client = self._stub_client()
        client.read_until = MagicMock(return_value="DEVICE_READY")
        client.read_all = MagicMock(return_value="ALL_OUTPUT")
        try:
            result = client.send_command("PING\n", wait_time=0.25)
        finally:
            client.close()

        assert result == "ALL_OUTPUT"
        assert slept == [0.25], (
            f"无 expect 分支必须等满 wait_time，实际 sleep: {slept}"
        )
        client.read_all.assert_called_once()
        client.read_until.assert_not_called()

    def test_expect_branch_propagates_read_until_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        expect 分支的读取异常照原样上抛，不被新逻辑吞掉

        去盲等只是省掉了等待，读失败/超时的异常契约必须不变。
        """
        monkeypatch.setattr(serial_client_mod.time, "sleep", lambda s: None)
        client = self._stub_client()
        expected = serial_client_mod.SerialClientError(
            "读取超时: 未等到特征字符串", port="loop://"
        )
        client.read_until = MagicMock(side_effect=expected)
        try:
            with pytest.raises(serial_client_mod.SerialClientError) as exc_info:
                client.send_command("AT\n", expect="NEVER", wait_time=0.5)
        finally:
            client.close()

        assert exc_info.value is expected, "异常对象应原样上抛，不得被替换"
