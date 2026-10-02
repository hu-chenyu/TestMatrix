"""
TestMatrix 阶段C-3: 支撑层未覆盖分支的补齐测试（覆盖率安全垫）

背景:
    阶段C-1/C-2 已把三个重点文件推到 100%，全仓覆盖率到达 95%。
    但 95% 只比阈值高 0.04 个百分点，任何后续提交新增未覆盖代码都
    会立刻跌破阈值。本文件把安全垫垫到 ~96.4%，并优先选择**真实
    风险高**而非"顺手能测"的路径。

    A组 web/exceptions.py —— 生产错误响应链路
        1.  UnauthorizedError 401
        2.  ForbiddenError 403
        3.  405 方法不允许
        4.  500 处理器（PROPAGATE_EXCEPTIONS=False 的生产路径）
            TESTING=True  回显原始异常、TESTING=False 只回通用文案
            —— 既有测试只覆盖了 Exception 兜底处理器，Flask 按状态码
            分派的 500 处理器（生产真实走的那条）在全仓零覆盖
    B组 common/env_manager.py
        5.  .env 文件缺失静默降级
        6.  get_int 值非法回落默认值
        7.  get_float 值非法回落默认值
        8.  业务语义属性（base_url/http_timeout/http_retries）与 as_dict
    C组 core/executors.py
        9.  抽象方法契约（run_one 必须显式实现）
        10. pytest 子进程超时映射 error
        11. pytest 子进程启动失败映射 error
            —— 这两条是"测试平台执行器"的兜底路径：子进程起不来时
            若不映射成 error，用例会以"结果缺失"而非"执行失败"呈现
    D组 core/cache.py
        12. fakeredis 缺失降级 no-op
        13. 真实 Redis URL 构建后端
        14. reset_backend 容忍 close 异常
        15. 脏缓存值反序列化失败按未命中回源
        16. 批量失效遇 Redis 异常跳过
    E组 core/event_bus.py
        17. 通道关闭后 publish 被丢弃
        18. 非法事件类型被丢弃
        19. publish 内部故障被吞掉
        20. close 内部故障被吞掉
            —— 19/20 直接对应"日志通道故障绝不影响执行主流程"
            这条铁律，回归后果是后台执行线程被事件总线搞挂

    E组 19/20 需要替换 EventChannel 的私有属性来制造故障。
    这是刻意选择：这两条 except 分支的唯一触发方式就是内部状态
   损坏，没有任何公开 API 能触达，不替换就永远测不到。断言仍只
    校验对外契约（不抛异常、状态符合预期），不锁死内部实现。

测试铁律（对齐 PROJECT_CONTEXT.md 7.12/7.16/7.19）:
    - 事件通道用独立 EventChannel 实例（不入全局注册表），零串扰
    - 缓存用独立 CacheClient 实例 + 独立 fake 后端
    - 子进程调用一律 monkeypatch 拦截，绝不真起 py 进程
    - 全程 loguru，无 print
"""

import subprocess
import sys
from pathlib import Path
from typing import Any

import allure
import pytest
import redis
from loguru import logger as loguru_logger
from src.common.env_manager import EnvManager
from src.core import executors as executors_mod
from src.core.cache import CacheClient
from src.core.event_bus import EventChannel, ExecutionEvent
from src.core.executors import (
    PYTEST_TIMEOUT_SECONDS,
    BaseExecutor,
    PytestRunner,
)
from src.web import ForbiddenError, UnauthorizedError, create_app

# 不参与真实访问的占位地址
DUMMY_BASE_URL = "http://unit.test"


# ===========================================================================
# 内部工具
# ===========================================================================
def _exception_app(config_name: str) -> Any:
    """
    构造只挂一条"必定抛异常"路由的最小应用（用于500链路）

    参数:
        config_name (str): 配置档名，test/prod

    返回:
        Any: Flask 应用实例
    """
    app = create_app(config_name)
    # 生产路径：Flask 把未处理异常包装成 InternalServerError 后按状态码
    # 分派给 500 处理器；TESTING 默认开启异常传播会绕过该处理器，
    # 故两个档位都必须显式关闭传播
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.route("/api/boom")
    def boom() -> Any:
        """必定抛异常的路由"""
        raise RuntimeError("内部实现细节: 数据库分片键缺失")

    @app.route("/api/guarded")
    def guarded() -> Any:
        """未登录访问受保护资源"""
        raise UnauthorizedError("登录态已失效")

    @app.route("/api/forbidden")
    def forbidden() -> Any:
        """已登录但权限不足"""
        raise ForbiddenError("缺少 delete:case 权限")

    return app


class _LogCapture:
    """
    loguru 日志捕获器（context manager，PROJECT_CONTEXT 7.17 标准做法）

    为什么不用 pytest 的 caplog: loguru 不经过 stdlib logging 的 handler
    体系，caplog 抓不到任何记录，会得到一个永远为空的列表并据此误判
    "没有记日志"。
    """

    def __init__(self, level: str = "WARNING") -> None:
        """
        初始化捕获缓冲

        参数:
            level (str): 捕获级别，默认 WARNING
        """
        self.messages: list[str] = []
        self._level = level
        self._sink_id: int | None = None

    def __enter__(self) -> "_LogCapture":
        """注册 loguru sink 开始收集"""
        self._sink_id = loguru_logger.add(
            self.messages.append, level=self._level
        )
        return self

    def __exit__(self, *_exc: Any) -> None:
        """移除 sink（不吞异常）"""
        if self._sink_id is not None:
            loguru_logger.remove(self._sink_id)
            self._sink_id = None


def _after_request_failing_app(config_name: str) -> Any:
    """
    构造 after_request 阶段必定抛异常的应用（触发 500 按码处理器）

    响应finalize阶段的异常绕过 handle_user_exception，是 Flask 2.3
    中按状态码注册的 500 处理器唯一可达的入口。

    参数:
        config_name (str): 配置档名，test/prod

    返回:
        Any: Flask 应用实例
    """
    app = create_app(config_name)
    # 生产路径：异常不向上抛，Flask 包装成 InternalServerError 后
    # 交给 500 处理器；TESTING 默认开启传播会绕过该处理器
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.after_request
    def failing_after_request(response: Any) -> Any:
        """模拟响应头计算失败（生产上真实可达的钩子故障）"""
        raise RuntimeError("响应finalize失败: 响应头计算异常")

    return app


# ===========================================================================
# A组: web/exceptions.py
# ===========================================================================
@allure.feature("阶段C覆盖率补齐")
@allure.story("全局异常处理器")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestErrorHandlerCoverage:
    """401/403 业务异常 + 405 + 生产 500 链路"""

    def test_unauthorized_error_401(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        未授权异常: 抛 UnauthorizedError 时返回 HTTP 401 且
        code/message 与异常一致，detail 进 data 字段。
        """
        monkeypatch.setenv("TM_SECRET_KEY", "test-only-prod-key")
        client = _exception_app("test").test_client()

        response = client.get("/api/guarded")
        body = response.get_json()

        assert response.status_code == 401
        assert body["code"] == 401
        assert body["message"] == "登录态已失效"

    def test_forbidden_error_403(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        无权限异常: 抛 ForbiddenError 时返回 HTTP 403，与 401
        区分开（401=未登录，403=已登录但无权，前端据此决定是否跳登录）。
        """
        monkeypatch.setenv("TM_SECRET_KEY", "test-only-prod-key")
        client = _exception_app("test").test_client()

        response = client.get("/api/forbidden")
        body = response.get_json()

        assert response.status_code == 403
        assert body["code"] == 403
        assert body["message"] == "缺少 delete:case 权限"

    def test_method_not_allowed_405(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        方法不允许: 对只读路由发 POST 时返回统一 JSON 的 405，
        而不是 Flask 默认的 HTML 错误页（否则前端拿不到 code/message）。
        """
        monkeypatch.setenv("TM_SECRET_KEY", "test-only-prod-key")
        client = _exception_app("test").test_client()

        response = client.post("/api/boom")
        body = response.get_json()

        assert response.status_code == 405
        assert body["code"] == 405
        assert body["message"] == "请求方法不被允许"
        assert response.mimetype == "application/json", (
            "错误响应必须是 JSON，不得回落 Flask 默认 HTML 页"
        )

    def test_500_handler_prod_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        生产 500 链路（按状态码分派的处理器）:
            - TESTING=True  回显原始异常消息（联调可定位）
            - TESTING=False 只回"服务器内部错误"，不泄露内部实现细节

        触发方式说明（这是本条最反直觉的部分）:
        路由里直接 raise 异常**不会**走到 500 按码处理器。Flask 2.3 的
        handle_user_exception 对非 HTTPException 只沿 MRO 查类处理器，
        必然先命中已注册的 Exception 兜底；按码处理器仅在异常绕过
        handle_user_exception、直接进入 handle_exception 时才被查——
        实际可达的场景是 after_request 钩子抛错（响应finalize阶段）。
        两条处理器对客户端的契约一致（故断言只看对外行为），
        165-167 行真正执行的证据是覆盖率报告中该段转为已覆盖。
        """
        monkeypatch.setenv("TM_SECRET_KEY", "test-only-prod-key")

        debug_client = _after_request_failing_app("test").test_client()
        debug_response = debug_client.get("/api/version")
        debug_body = debug_response.get_json()

        assert debug_response.status_code == 500
        assert debug_body["code"] == 500
        assert debug_body["message"] == "响应finalize失败: 响应头计算异常", (
            "TESTING 模式应回显原始异常消息便于联调"
        )

        prod_client = _after_request_failing_app("prod").test_client()
        prod_response = prod_client.get("/api/version")
        prod_body = prod_response.get_json()

        assert prod_response.status_code == 500
        assert prod_body["message"] == "服务器内部错误"
        prod_text = prod_response.get_data(as_text=True)
        assert "响应头计算异常" not in prod_text, "生产模式不得泄露异常消息"
        assert "Traceback" not in prod_text, "生产模式不得泄露堆栈"
        assert "RuntimeError" not in prod_text, "生产模式不得泄露异常类名"


# ===========================================================================
# B组: common/env_manager.py
# ===========================================================================
@allure.feature("阶段C覆盖率补齐")
@allure.story("多环境配置管理")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.regression
class TestEnvManagerCoverage:
    """.env 缺失 / 数值转换兜底 / 业务语义属性"""

    def test_missing_env_file_degrades_silently(self, tmp_path: Path) -> None:
        """
        .env 缺失静默降级: 配置文件不存在是 CI/生产常态（全部走系统
        环境变量），不得抛异常，也不得把 _loaded 标成已加载。
        """
        manager = EnvManager(env_file=str(tmp_path / "no-such-file.env"))

        assert manager._loaded is False, "文件不存在时不得标记为已加载"
        assert manager.get("TM_NOT_SET_ANYWHERE") is None, (
            "无配置时应返回 None 交由调用方取默认值"
        )

    @pytest.mark.parametrize(
        ("raw_value", "expected"),
        [("abc", 42), ("3.7.1", 42), ("", 42), ("   ", 42)],
        ids=["not-a-number", "dotted-version", "empty", "blank"],
    )
    def test_get_int_falls_back_on_invalid(
        self, monkeypatch: pytest.MonkeyPatch, raw_value: str, expected: int
    ) -> None:
        """
        get_int 非法值回落: 版本号这类"看起来像数字"的配置被误填时
        必须回落默认值而不是抛异常（启动期配置错误不该搞挂进程）。
        """
        monkeypatch.setenv("TM_TEST_PORT", raw_value)
        manager = EnvManager(env_file="")

        assert manager.get_int("TM_TEST_PORT", 42) == expected

    def test_get_float_falls_back_on_invalid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        get_float 非法值回落: 同 get_int，非法值返回默认值。
        """
        monkeypatch.setenv("TM_TEST_RATIO", "not-a-float")
        manager = EnvManager(env_file="")

        assert manager.get_float("TM_TEST_RATIO", 0.5) == 0.5

    def test_business_properties_and_as_dict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        业务语义属性: 有配置时读环境变量，无配置时读内置默认值；
        as_dict 批量取值键缺失时值为 None（不报错）。
        """
        manager = EnvManager(env_file="")
        for key in (
            "TM_BASE_URL",
            "TM_HTTP_TIMEOUT",
            "TM_HTTP_RETRIES",
            "TM_LOG_LEVEL",
        ):
            monkeypatch.delenv(key, raising=False)

        # 缺省值
        assert manager.base_url == "https://httpbin.org"
        assert manager.http_timeout == 10
        assert manager.http_retries == 2
        assert manager.log_level == "INFO"

        # 环境变量覆盖
        monkeypatch.setenv("TM_BASE_URL", "https://staging.test")
        monkeypatch.setenv("TM_HTTP_TIMEOUT", "45")
        monkeypatch.setenv("TM_HTTP_RETRIES", "5")
        assert manager.base_url == "https://staging.test"
        assert manager.http_timeout == 45
        assert manager.http_retries == 5

        snapshot = manager.as_dict(["TM_BASE_URL", "TM_MISSING_KEY"])
        assert snapshot == {
            "TM_BASE_URL": "https://staging.test",
            "TM_MISSING_KEY": None,
        }, "运行时配置快照缺键时应为 None，便于日志一眼看出未配置项"


# ===========================================================================
# C组: core/executors.py
# ===========================================================================
@allure.feature("阶段C覆盖率补齐")
@allure.story("用例执行器")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.regression
class TestExecutorCoverage:
    """抽象契约 / 子进程超时 / 子进程启动失败"""

    def test_abstract_run_one_must_be_implemented(self) -> None:
        """
        抽象方法契约: 新增执行器若忘记实现 run_one，调用时必须得到
        NotImplementedError 而非静默返回空结果。
        """

        class _ForgettingExecutor(BaseExecutor):
            """忘记覆写 run_one 的执行器"""

            def run_one(self, case: dict) -> Any:
                """委托给抽象父类"""
                return super().run_one(case)

        with pytest.raises(NotImplementedError):
            _ForgettingExecutor().run_one({"case_id": "TM-0001"})

    def test_subprocess_timeout_maps_to_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        子进程超时: 超时必须映射为 result=error 而非抛异常——
        否则整批执行会被一个卡死的用例带崩，且失败原因丢失。
        错误消息含超时阈值与完整命令便于复现。
        """
        command_holder: list[list] = []

        def _raise_timeout(cmd: list, **_kwargs) -> None:
            """记录命令并模拟超时"""
            command_holder.append(cmd)
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=PYTEST_TIMEOUT_SECONDS)

        monkeypatch.setattr(executors_mod.subprocess, "run", _raise_timeout)
        runner = PytestRunner()
        case = {"case_id": "TM-UC-0001", "script_path": "test_demo.py"}

        result = runner.run_one(case)

        assert result.result == "error"
        assert f"执行超时(>{PYTEST_TIMEOUT_SECONDS}s)" in result.error_message
        assert "test_demo.py" in result.error_message, (
            "超时消息应含被执行的脚本路径"
        )
        assert result.duration >= 0
        assert command_holder, "应真正走到 subprocess.run"

    def test_subprocess_launch_failure_maps_to_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        子进程启动失败: 命令不存在（py 不在 PATH）等 OSError 必须映射
        为 result=error，消息保留原始异常以便运维发现"解释器没装"。
        """
        def _raise_os_error(_cmd: list, **_kwargs) -> None:
            """模拟解释器不存在"""
            raise OSError(2, "No such file or directory: 'py'")

        monkeypatch.setattr(executors_mod.subprocess, "run", _raise_os_error)
        runner = PytestRunner()

        result = runner.run_one({"case_id": "TM-UC-0001"})

        assert result.result == "error"
        assert "pytest子进程启动失败" in result.error_message
        assert "No such file or directory" in result.error_message, (
            "启动失败应保留 OS 原始错误文本"
        )


# ===========================================================================
# D组: core/cache.py
# ===========================================================================
@allure.feature("阶段C覆盖率补齐")
@allure.story("Redis 缓存层")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.regression
class TestCacheCoverage:
    """依赖缺失降级 / 真实后端构建 / 脏数据回源 / 失效异常"""

    def test_fakeredis_missing_degrades_to_noop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        fakeredis 缺失降级: 缓存已启用但依赖装不上时按未启用处理，
        绝不因缓存影响业务启动（缓存是旁路能力）。
        """
        monkeypatch.setenv("TM_REDIS_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        monkeypatch.setitem(sys.modules, "fakeredis", None)
        client = CacheClient()

        assert client._get_backend() is None
        assert client.get_json("k") is None
        client.set_json("k", {"v": 1})
        assert client.get_json("k") is None, "降级后读写均为 no-op"

    def test_real_redis_url_builds_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        真实 Redis URL 构建: 非 fake:// 走 redis.from_url，
        from_url 本身不建连故不产生 I/O。
        """
        monkeypatch.setenv("TM_REDIS_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "redis://127.0.0.1:1/0")
        client = CacheClient()

        backend = client._get_backend()

        assert backend is not None, "真实 URL 应构建出后端"
        assert isinstance(backend, redis.Redis)
        client.reset_backend()

    def test_reset_backend_tolerates_close_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        reset_backend 容忍 close 异常: close 抛错仍须把 _backend 置
        None，否则测试间复用坏实例造成跨例污染。
        """

        class _BadCloseBackend:
            """模拟 close 抛异常的残留后端"""

            def close(self) -> None:
                """关闭时抛错"""
                raise RuntimeError("close 失败")

        client = CacheClient()
        client._backend = _BadCloseBackend()
        client.reset_backend()

        assert client._backend is None

    def test_corrupt_cache_value_falls_back_to_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        脏缓存值回源: 缓存里的值不是合法 JSON（如历史版本写入的格式
        变了）时按未命中处理并回源查库，不得让读接口 500。
        """
        monkeypatch.setenv("TM_REDIS_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        client = CacheClient()
        backend = client._get_backend()
        assert backend is not None
        # 绕过 set_json 直接塞脏数据，模拟历史遗留/外部写入
        backend.set("tm:cache:corrupt", "这不是JSON{{{")

        try:
            assert client.get_json("tm:cache:corrupt") is None, (
                "脏数据应按未命中降级回源"
            )
        finally:
            client.reset_backend()

    def test_delete_pattern_tolerates_redis_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        批量失效遇 Redis 异常跳过: 用例写操作成功后的缓存失效是
        旁路动作，失败只记 warning，绝不能让业务请求跟着失败。

        **v3 补断言**（原版只有一句"不抛异常即为通过"，零断言）:
        只断言"不抛"区分不了"异常被正确吞掉"与"压根没走到异常分支"。
        本条补两条可观测证据——①降级确实发生过（warning 里出现该前缀与
        前缀参数）；②后端调用确实被触发了（桩里记录 scan_iter 被调用），
        排除"因为参数拼错/分支没进"导致的空转通过。
        """
        scan_calls: list[str] = []

        class _ScanRaisesBackend:
            """模拟 SCAN 抛连接异常的后端"""

            def scan_iter(self, match: str) -> Any:
                """记录调用并抛连接异常"""
                scan_calls.append(match)
                raise redis.ConnectionError("模拟宕机: scan失败")

        monkeypatch.setenv("TM_REDIS_ENABLED", "true")
        monkeypatch.setenv("TM_REDIS_URL", "fake://0")
        client = CacheClient()
        client._backend = _ScanRaisesBackend()
        capture = _LogCapture(level="WARNING")

        try:
            with capture:
                client.delete_pattern("tm:cache:cases:")
        finally:
            client.reset_backend()

        assert scan_calls == ["tm:cache:cases:*"], (
            f"必须真的走到 SCAN 才能验证异常降级，实际调用: {scan_calls}"
        )
        joined = "\n".join(capture.messages)
        assert "缓存批量失效异常" in joined, (
            f"SCAN 异常必须记 warning 留痕，实际日志: {joined}"
        )
        assert "tm:cache:cases:" in joined, (
            f"warning 应带上前缀参数便于定位，实际日志: {joined}"
        )


# ===========================================================================
# E组: core/event_bus.py
# ===========================================================================
@allure.feature("阶段C覆盖率补齐")
@allure.story("执行事件总线")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestEventBusFaultTolerance:
    """发布丢弃规则 / 内部故障吞掉，绝不影响执行主流程"""

    def test_publish_after_close_is_dropped(self) -> None:
        """
        通道关闭后发布被丢弃: 批次终态后的迟到事件无消费意义，
        publish 必须静默丢弃并保留原 event_id（不覆写、不入历史环）。
        """
        channel = EventChannel()
        channel.close(reason="finished")
        late_event = ExecutionEvent(
            event_type="case_finished", data={"case_id": "TM-UC-0001"}
        )

        channel.publish(late_event)

        assert late_event.event_id is None, (
            "被丢弃的事件不应被分配 event_id"
        )
        assert list(channel.subscribe(last_event_id=None)) == [], (
            "关闭后不应再有可回放事件"
        )

    def test_invalid_event_type_is_dropped(self) -> None:
        """
        非法事件类型被丢弃: 埋点方写错类型名时事件不得进入历史环
        （否则前端 switch 收到未知类型会渲染成空白帧）。
        """
        channel = EventChannel()
        bogus = ExecutionEvent(event_type="batch_retry", data={})

        channel.publish(bogus)

        assert bogus.event_id is None
        # 先关闭通道再回放: subscribe 在通道未关闭且无新事件时会以
        # 0.5s 为节拍无限等待，不关闭则迭代器永不结束
        channel.close(reason="test")
        assert list(channel.subscribe(last_event_id=None)) == [], (
            "非法类型事件不应进入历史环"
        )

    def test_publish_internal_fault_is_swallowed(self) -> None:
        """
        publish 内部故障被吞掉: 日志通道故障绝不能搞挂真实执行主流程
        ——此处用会抛异常的历史环替换内部状态制造故障（该 except 分支
        无公开 API 可触达）。

        **v3 补断言**（原版只有一句"不抛异常即为通过"，零断言）:
        补两条可观测证据——①吞掉确实发生过（error 日志里出现兜底铁律
        那句）；②故障点确实被走到（桩被调用过），排除"因为事件类型非法
        等原因提前 return"导致的空转通过。
        """
        append_calls: list[Any] = []

        class _RaisingHistory:
            """模拟历史环写入失败（内存故障场景）"""

            def append(self, event: Any) -> None:
                """记录调用并抛错"""
                append_calls.append(event)
                raise MemoryError("历史环写入失败")

        channel = EventChannel()
        channel._history = _RaisingHistory()
        capture = _LogCapture(level="ERROR")

        with capture:
            channel.publish(
                ExecutionEvent(
                    event_type="batch_start", data={"total_cases": 1}
                )
            )

        assert len(append_calls) == 1, (
            f"必须真的走到历史环写入才能验证兜底，实际调用 {len(append_calls)} 次"
        )
        joined = "\n".join(capture.messages)
        assert "事件发布异常" in joined, (
            f"发布异常必须被吞掉并记 error，实际日志: {joined}"
        )
        assert "已吞掉，不影响执行主流程" in joined, (
            f"error 日志应体现旁路铁律的处置，实际日志: {joined}"
        )

    def test_close_internal_fault_is_swallowed(self) -> None:
        """
        close 内部故障被吞掉: 批次终态清理不能因日志通道故障失败，
        否则整条批次收尾流程（置终态/通知/缓存失效）会被打断。

        断言"未谎报已关闭"即可：加锁失败时 _closed 根本没被置位，
        若代码谎报 is_closed=True，批次收尾会以为通道已关闭而跳过
        后续事件投递。
        """

        class _RaisingCondition:
            """模拟 Condition 加锁失败"""

            def __enter__(self) -> Any:
                """进入临界区即抛错"""
                raise RuntimeError("条件变量加锁失败")

            def __exit__(self, *_exc: Any) -> None:
                """退出临界区（不会走到）"""
                return None

        channel = EventChannel()
        channel._condition = _RaisingCondition()

        channel.close(reason="failed")  # 不抛异常即为通过
        assert channel.is_closed is False, (
            "加锁失败时不应谎报已关闭"
        )
