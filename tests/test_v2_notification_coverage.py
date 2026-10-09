"""
TestMatrix 大扫除 v2 · 任务一：notification 异常路径覆盖补齐

覆盖目标
--------
本文件针对 src/core/notification.py 的 24 条未覆盖语句（96% -> 100%）。
未覆盖清单与对应场景：

    行号       场景                                        本文件用例
    ---------  ------------------------------------------  --------------------
    339-340    SMTP quit() 抛异常被忽略                   test_smtp_quit_error_ignored
    360/362/364 必填配置缺失逐项检出                       test_missing_configs_itemize
    428       明文端口（非 TLS）连接分支                  test_plaintext_port_connection
    840       _format_duration 收到 None                 test_format_duration_none
    860/863   _format_ms 的 None 与秒级换算               test_format_ms_edges
    905       _truncate 空消息                           test_truncate_empty_message
    1030      构造期 webhook URL 非法告警                 test_invalid_webhook_url_warns_on_init
    1085-1089 发送期 webhook URL 非法中止                test_send_rejects_invalid_webhook_url
    1304      通过率为 None/负数映射为灰色                test_pass_rate_color_gray_edges
    1414-1418 退避基数非数值回落默认                     test_retry_base_delay_non_numeric
    1618-1623 渠道抛异常被隔离                           test_channel_exception_isolated
    1769-1773 通知历史入库失败不影响主流程              test_history_save_failure_ignored
    1870      失败详情错误信息超长截断                   test_failed_detail_message_truncated
    1875      失败明细超10条时追加汇总行                 test_failed_details_overflow_summary

设计要点
--------
- 零真实网络：SMTP 与企微 HTTP 均用桩替身，断言只看"是否隔离、
  是否留痕、是否脱敏"，不触碰真实服务
- **重试等待用 router.sleeper 注入桩**（记调用次数、零耗时），
  绝不真 sleep：既满足禁固定 sleep 铁律，又不拖慢测试
- 关键断言对齐项目设计原则「通知是旁路能力」：任何单渠道故障都不得
  影响其它渠道，也不得让业务主流程失败

测试铁律（对齐项目设计原则：通知旁路+日志捕获规范）
- loguru 日志断言一律用临时 sink，**不得用 caplog**（loguru 不经
  stdlib logging 通道，caplog 返回空列表会让人误判"没记日志"）
- 无 time.sleep 固定等待，无 print
"""

import smtplib
from collections.abc import Iterator
from typing import Any

import allure
import pytest
from loguru import logger as loguru_logger
from src.core import notification as notification_mod
from src.core.notification import (
    EmailNotifier,
    EmailReportTemplate,
    Notification,
    NotificationRouter,
    WeChatNotifier,
)
from src.core.report_analyzer import (
    FailedCaseDetail,
    ReportStatistics,
    StatisticsResult,
)


# ===========================================================================
# 夹具与内部工具
# ===========================================================================
class _LogCapture:
    """loguru 日志捕获器（context manager，7.17 标准做法）"""

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


class _StubSMTP:
    """
    SMTP 桩：可配置 starttls/quit 抛异常，覆盖连接收尾各分支

    quit 必须保留：被测代码在 finally 里会调它，桩缺这个方法会让
    AttributeError 顶替掉我们要验的 SMTPException。
    """

    def __init__(
        self,
        host: str = "",
        port: int = 0,
        timeout: Any = None,
        fail_starttls: bool = False,
        fail_quit: bool = False,
    ) -> None:
        """
        记录连接参数并预设失败开关

        参数:
            host (str): SMTP 主机
            port (int): SMTP 端口
            timeout (Any): 超时秒数
            fail_starttls (bool): starttls 是否抛异常
            fail_quit (bool): quit 是否抛 SMTPException
        """
        self.host = host
        self.port = port
        self.timeout = timeout
        self.fail_starttls = fail_starttls
        self.fail_quit = fail_quit
        self.starttls_called = False
        self.quit_called = False

    def starttls(self) -> None:
        """按开关决定是否抛异常"""
        self.starttls_called = True
        if self.fail_starttls:
            raise smtplib.SMTPException("STARTTLS 握手失败")

    def login(self, *_args, **_kwargs) -> None:
        """登录为 no-op"""
        return None

    def sendmail(self, *_args, **_kwargs) -> None:
        """发送为 no-op"""
        return None

    def quit(self) -> None:
        """按开关决定是否抛异常"""
        self.quit_called = True
        if self.fail_quit:
            raise smtplib.SMTPException("QUIT 阶段连接已断开")


class _CountingSleeper:
    """
    退避等待桩：记录等待序列、零真实耗时

    注入到 NotificationRouter.sleeper 后，指数退避的"等待了多久"
    变成可断言的纯数据，既消除真实 sleep，又让退避计算本身可测。
    """

    def __init__(self) -> None:
        """初始化等待记录"""
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        """记录而不真等

        参数:
            seconds (float): 计划等待秒数
        """
        self.calls.append(seconds)


class _NullRepo:
    """历史/死信仓储桩：吞掉所有写入，零数据库访问"""

    def save_history(self, *_args, **_kwargs) -> None:
        """历史写入为 no-op"""
        return None

    def save_dead_letter(self, *_args, **_kwargs) -> None:
        """死信写入为 no-op"""
        return None


class _FlakyNotifier:
    """恒失败的渠道桩：驱动 _send_with_retry 走完重试与失败分支"""

    # BaseNotifier 约定渠道实现须提供 channel_name，_send_with_retry
    # 会读取它来标记日志与死信来源
    channel_name = "flaky"

    def __init__(self) -> None:
        """初始化调用计数"""
        self.calls = 0

    def is_enabled(self) -> bool:
        """恒启用（False 会被视为配置性跳过、不走重试）"""
        return True

    def send(self, _notification: Any) -> bool:
        """恒返回失败"""
        self.calls += 1
        return False


def _build_detail(name: str, message: str) -> FailedCaseDetail:
    """构造 FailedCaseDetail（内部工具）"""
    return FailedCaseDetail(
        uuid=f"uuid-{name}",
        name=name,
        full_name=f"tests.demo#{name}",
        status="failed",
        duration_ms=100,
        module="用户中心",
        priority="P1",
        error_message=message,
        error_trace="",
        owner="",
    )


def _build_stat(failed_details: list[FailedCaseDetail]) -> StatisticsResult:
    """构造最小 StatisticsResult 供企微摘要渲染（内部工具）"""
    base = ReportStatistics.aggregate([])
    base.total = max(len(failed_details), 1)
    base.failed = len(failed_details)
    base.pass_rate = 0.0
    base.failed_details = failed_details
    return base


@pytest.fixture
def smtp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    邮件通道必填配置齐备的环境夹具

    teardown:
        monkeypatch 自动恢复环境变量

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

    返回:
        Iterator[None]: yield None（仅用于设置环境）
    """
    monkeypatch.setenv("TM_EMAIL_SMTP_HOST", "smtp.v2.test")
    monkeypatch.setenv("TM_EMAIL_SENDER", "sender@v2.test")
    monkeypatch.setenv("TM_EMAIL_PASSWORD", "PLACEHOLDER-NOT-A-REAL-SECRET")
    monkeypatch.setenv("TM_EMAIL_RECEIVERS", "qa@v2.test")
    # 渠道总开关：send() 第一步就查它，不开则直接跳过、后续分支全走不到
    monkeypatch.setenv("TM_EMAIL_ENABLED", "true")
    # 端口口径（_connect 三分支）: 465=SSL / 587=STARTTLS / 其它=明文。
    # 缺省取明文端口，桩只补 SMTP 一处即可覆盖 quit 收尾路径
    monkeypatch.setenv("TM_EMAIL_SMTP_PORT", "1025")
    yield


# ===========================================================================
# A组: SMTP 连接与收尾
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("notification SMTP 连接与收尾")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestSmtpConnectionLifecycle:
    """quit 异常忽略 / 明文端口 / 必填配置逐项检出"""

    def test_smtp_quit_error_ignored(
        self, monkeypatch: pytest.MonkeyPatch, smtp_env: None
    ) -> None:
        """
        quit() 抛 SMTPException 必须被忽略（行339-340）

        quit 在 finally 里执行：此时业务结果已经确定，若异常向上抛
        会把"邮件已发成功"变成"调用方看到异常"，而连接泄漏反而没人管。
        """
        stub = _StubSMTP(fail_quit=True)
        monkeypatch.setattr(
            notification_mod.smtplib, "SMTP", lambda *a, **k: stub
        )
        capture = _LogCapture(level="DEBUG")

        with capture:
            result = EmailNotifier().send(Notification(
                execution_id="RUN-V2-SMTP-0001",
                title="quit异常收尾测试",
                content="正文",
            ))

        assert result is True, "quit 异常不得把已成功的发送变成失败"
        assert stub.quit_called, "连接释放必须尝试"
        assert any(
            "SMTP连接关闭时异常" in text for text in capture.messages
        ), f"应记 debug 说明关闭异常被忽略，实际: {capture.messages}"

    def test_plaintext_port_connection(
        self, monkeypatch: pytest.MonkeyPatch, smtp_env: None
    ) -> None:
        """
        非 TLS 明文端口不调用 starttls（行428）

        本地 MailHog/Mailpit 用 1025 明文端口，此时调 starttls 会直接
        连接失败——分支错了本地联调邮件根本发不出去。
        """
        monkeypatch.setenv("TM_EMAIL_SMTP_PORT", "1025")
        monkeypatch.setenv("TM_EMAIL_SMTP_USE_TLS", "false")
        stub = _StubSMTP()
        monkeypatch.setattr(
            notification_mod.smtplib, "SMTP", lambda *a, **k: stub
        )

        result = EmailNotifier().send(Notification(
            execution_id="RUN-V2-SMTP-0002",
            title="明文端口测试",
            content="正文",
        ))

        assert result is True
        assert stub.starttls_called is False, (
            "明文端口绝不能调 starttls，否则本地邮件测试环境不可用"
        )

    def test_tls_port_calls_starttls(
        self, monkeypatch: pytest.MonkeyPatch, smtp_env: None
    ) -> None:
        """
        587 端口走 SMTP + starttls 升级（正向对照）

        与上一条成对，证明分流依据是端口口径（465 SSL / 587 STARTTLS
        / 其它明文）而不是笼统的"用不用 TLS 开关"。
        """
        monkeypatch.setenv("TM_EMAIL_SMTP_PORT", "587")
        stub = _StubSMTP()
        monkeypatch.setattr(
            notification_mod.smtplib, "SMTP", lambda *a, **k: stub
        )

        assert EmailNotifier().send(Notification(
            execution_id="RUN-V2-SMTP-0003",
            title="STARTTLS端口测试",
            content="正文",
        )) is True
        assert stub.starttls_called is True, "587 端口必须先 starttls 再登录"

    def test_ssl_port_uses_ssl_socket(
        self, monkeypatch: pytest.MonkeyPatch, smtp_env: None
    ) -> None:
        """
        465 端口走 SMTP_SSL 直连，不再叠加 starttls

        SSL 端口上再调 starttls 是协议错误（连接已经是 TLS 了）。
        """
        monkeypatch.setenv("TM_EMAIL_SMTP_PORT", "465")
        ssl_stub = _StubSMTP()
        plain_stub = _StubSMTP()
        monkeypatch.setattr(
            notification_mod.smtplib, "SMTP_SSL", lambda *a, **k: ssl_stub
        )
        monkeypatch.setattr(
            notification_mod.smtplib, "SMTP", lambda *a, **k: plain_stub
        )

        assert EmailNotifier().send(Notification(
            execution_id="RUN-V2-SMTP-0006",
            title="SSL端口测试",
            content="正文",
        )) is True
        assert plain_stub.host == "", "465 端口不应走明文 SMTP 分支"
        assert plain_stub.quit_called is False
        assert ssl_stub.starttls_called is False, (
            "SSL 端口连接本身就是 TLS，不能再 starttls"
        )

    def test_missing_configs_itemize(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        必填配置缺失逐项检出并指名（行360/362/364）

        一次性报出全部缺失项，而不是"发现一个报一个"：运维改完一个
        再跑一次才发现还缺下一个，来回三四趟才能配齐。
        """
        monkeypatch.setenv("TM_EMAIL_SMTP_HOST", "")
        monkeypatch.setenv("TM_EMAIL_SENDER", "")
        monkeypatch.setenv("TM_EMAIL_PASSWORD", "")
        monkeypatch.setenv("TM_EMAIL_RECEIVERS", "")

        missing = EmailNotifier()._missing_configs()

        assert set(missing) == {
            "TM_EMAIL_SMTP_HOST",
            "TM_EMAIL_SENDER",
            "TM_EMAIL_PASSWORD",
            "TM_EMAIL_RECEIVERS",
        }, f"四项必填配置应一次性全部列出，实际: {missing}"

    def test_partial_missing_configs_reported(
        self, monkeypatch: pytest.MonkeyPatch, smtp_env: None
    ) -> None:
        """
        部分配置缺失时只报缺失项（正向对照）

        证明上一条不是因为"恒返回四项"才凑齐。
        """
        monkeypatch.setenv("TM_EMAIL_PASSWORD", "")

        assert EmailNotifier()._missing_configs() == ["TM_EMAIL_PASSWORD"]

    def test_send_rejected_when_config_missing(
        self, monkeypatch: pytest.MonkeyPatch, smtp_env: None
    ) -> None:
        """
        配置缺失时 send 返回 False 且不建连

        静默跳过会让"配错了"看起来像"邮件服务坏了"，排查方向错位。
        """
        monkeypatch.setenv("TM_EMAIL_RECEIVERS", "")
        built: list[Any] = []
        monkeypatch.setattr(
            notification_mod.smtplib,
            "SMTP",
            lambda *a, **k: built.append(a) or _StubSMTP(),
        )
        capture = _LogCapture(level="ERROR")

        with capture:
            result = EmailNotifier().send(Notification(
                execution_id="RUN-V2-SMTP-0004",
                title="缺配置测试",
                content="正文",
            ))

        assert result is False
        assert built == [], "配置校验未过时不得建立任何 SMTP 连接"
        assert any(
            "邮件配置缺失" in text and "TM_EMAIL_RECEIVERS" in text
            for text in capture.messages
        ), f"error 日志须指名缺失的环境变量名，实际: {capture.messages}"

    def test_send_skipped_when_channel_disabled(
        self, monkeypatch: pytest.MonkeyPatch, smtp_env: None
    ) -> None:
        """
        渠道开关关闭时 send 返回 False 且完全不建连

        默认关闭是设计（项目灰度设计原则同款：灰度可随时切回），
        关着时不该产生任何 SMTP 连接。
        """
        monkeypatch.setenv("TM_EMAIL_ENABLED", "false")
        built: list[Any] = []
        monkeypatch.setattr(
            notification_mod.smtplib,
            "SMTP",
            lambda *a, **k: built.append(a) or _StubSMTP(),
        )

        result = EmailNotifier().send(Notification(
            execution_id="RUN-V2-SMTP-0005",
            title="渠道关闭测试",
            content="正文",
        ))

        assert result is False
        assert built == []


# ===========================================================================
# B组: 邮件模板格式化兜底
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("notification 格式化兜底")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.regression
class TestFormattingFallbacks:
    """None 耗时 / 空消息 / 秒级换算"""

    def test_format_duration_none(self) -> None:
        """
        _format_duration 收到 None 返回 "N/A"（行840）

        耗时缺失时格式化会抛 TypeError，邮件渲染整段失败——用户
        收到的是一封渲染报错的残缺邮件，比显示"N/A"糟糕得多。
        """
        assert EmailReportTemplate._format_duration(None) == "N/A"  # type: ignore[arg-type]
        assert EmailReportTemplate._format_duration(500) == "500 ms"
        assert EmailReportTemplate._format_duration(1500) == "1.50 s"

    def test_format_ms_edges(self) -> None:
        """
        _format_ms 的 None 与秒级换算（行860/863）

        浮点指标与整数耗时是两套格式化：avg/p95 是浮点要保留两位小数，
        混用会让"0.1 ms"显示成"0 ms"。
        """
        assert EmailReportTemplate._format_ms(None) == "N/A"  # type: ignore[arg-type]
        assert EmailReportTemplate._format_ms(0.5) == "0.50 ms"
        assert EmailReportTemplate._format_ms(999.99) == "999.99 ms"
        assert EmailReportTemplate._format_ms(1000) == "1.00 s", (
            "1000ms 是边界：应换算为秒而不是显示 1000.00 ms"
        )
        assert EmailReportTemplate._format_ms(2500.5) == "2.50 s"

    def test_truncate_empty_message(self) -> None:
        """
        _truncate 对空消息返回空串（行905）

        None 传入时直接做 len() 会抛 TypeError；空串也不该返回
        "None" 这种占位文本。
        """
        assert EmailReportTemplate._truncate("") == ""
        assert EmailReportTemplate._truncate(None) == ""  # type: ignore[arg-type]
        assert EmailReportTemplate._truncate("短消息", max_length=100) == "短消息"
        truncated = EmailReportTemplate._truncate("A" * 200, max_length=50)
        assert truncated.endswith("..."), "超长消息应截断并以省略号结尾"
        assert len(truncated) == 53, "截断后长度 = 50 + 3个点"


# ===========================================================================
# C组: 企微 webhook URL 校验
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("notification 企微 webhook 校验")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestWeChatWebhookValidation:
    """构造期告警 / 发送期中止 / 空配置静默"""

    def test_invalid_webhook_url_warns_on_init(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        构造期读到非法 URL 记 warning 并置空（行1030）

        脱敏必须生效：webhook URL 里带 key，直接打进日志等于把凭据
        写进日志文件（阶段2 P1 修的就是同类问题，不能在别处复发）。
        """
        monkeypatch.setenv(
            "TM_WECHAT_WEBHOOK_URL",
            "ftp://invalid.example.com/send?key=PLACEHOLDER-NOT-REAL",
        )
        capture = _LogCapture(level="WARNING")

        with capture:
            notifier = WeChatNotifier()

        assert notifier.webhook_url == "", "非法 URL 应被置空（按未配置处理）"
        assert notifier.is_enabled() is False
        assert any(
            "webhook URL格式非法" in text for text in capture.messages
        ), f"应在构造期就告警，实际: {capture.messages}"
        assert not any(
            "PLACEHOLDER-NOT-REAL" in text for text in capture.messages
        ), f"告警日志绝不能含未脱敏的 key: {capture.messages}"

    def test_empty_webhook_url_no_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        未配置 webhook 时不告警（对照）

        未配置是默认状态（默认关闭），每次启动都刷一条 warning
        只会淹没真正需要关注的配置错误。
        """
        monkeypatch.setenv("TM_WECHAT_WEBHOOK_URL", "")
        capture = _LogCapture(level="WARNING")

        with capture:
            notifier = WeChatNotifier()

        assert notifier.webhook_url == ""
        assert not any(
            "webhook URL格式非法" in text for text in capture.messages
        ), "未配置属正常状态，不应告警"

    def test_send_rejects_invalid_webhook_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        发送期 URL 非法必须中止并记 error（行1085-1089）

        这条是构造期校验之后的第二道闸：环境变量可能在 notifier
        构造之后被改坏。放行到发 HTTP 那一步只会拿到一个语义不明的 4xx。
        """
        monkeypatch.setenv("TM_WECHAT_ENABLED", "true")
        monkeypatch.setenv(
            "TM_WECHAT_WEBHOOK_URL",
            "https://qyapi.weixin.qq.com/cgi-bin/send?key=PLACEHOLDER-OK",
        )
        notifier = WeChatNotifier()
        # 绕过构造期校验，直接把 URL 改坏，模拟"构造后被改坏"
        notifier.webhook_url = "not-a-valid-url"
        posted: list[Any] = []
        monkeypatch.setattr(
            notification_mod.requests,
            "post",
            lambda *a, **k: posted.append(a) or None,
        )
        capture = _LogCapture(level="ERROR")

        with capture:
            result = notifier.send(Notification(
                execution_id="RUN-V2-WX-0001",
                title="非法URL发送测试",
                content="正文",
            ))

        assert result is False, "URL 非法时必须返回 False 而非尝试发送"
        assert posted == [], "URL 非法时不得发起任何 HTTP 请求"
        assert any(
            "webhook URL格式非法" in text for text in capture.messages
        ), f"应记 error 中止发送，实际: {capture.messages}"

    def test_send_rejected_when_url_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        未配置 webhook 时 send 返回 False 且不发请求
        """
        monkeypatch.setenv("TM_WECHAT_ENABLED", "true")
        monkeypatch.setenv("TM_WECHAT_WEBHOOK_URL", "")
        notifier = WeChatNotifier()
        posted: list[Any] = []
        monkeypatch.setattr(
            notification_mod.requests,
            "post",
            lambda *a, **k: posted.append(a) or None,
        )
        capture = _LogCapture(level="ERROR")

        with capture:
            result = notifier.send(Notification(
                execution_id="RUN-V2-WX-0002",
                title="未配置发送测试",
                content="正文",
            ))

        assert result is False
        assert posted == []
        assert any(
            "webhook URL未配置" in text for text in capture.messages
        ), f"应记 error 说明 URL 未配置，实际: {capture.messages}"


# ===========================================================================
# D组: 通过率配色与退避基数
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("notification 配色与退避基数")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestColorAndBackoff:
    """通过率灰色档 / 退避基数非法回落"""

    @pytest.mark.parametrize(
        "pass_rate",
        [None, -0.01, -1.0],
        ids=["none", "small-negative", "negative"],
    )
    def test_pass_rate_color_gray_edges(self, pass_rate: Any) -> None:
        """
        通过率为 None/负数映射为灰色（行1304）

        负数通过率在数据异常时可能出现；染成红色会让"数据有问题"
        被误读成"测试失败严重"，染绿更糟。灰色是唯一诚实的表达。
        """
        assert WeChatNotifier._pass_rate_color(pass_rate) == "comment"

    def test_pass_rate_color_normal_ranges(self) -> None:
        """
        正常区间的配色映射（正向对照）

        证明上一条拦的是"越界值"而非配色功能本身。
        """
        assert WeChatNotifier._pass_rate_color(0.95) == "info"
        assert WeChatNotifier._pass_rate_color(0.0) == "warning"
        assert WeChatNotifier._pass_rate_color(0.8999) == "warning"

    def test_retry_base_delay_non_numeric(self) -> None:
        """
        退避基数非数值时回落默认并告警（行1414-1418）

        阶段2 P2-26 已修过一次：负值/NaN 会让 time.sleep 抛
        ValueError，异常逃到 notify 兜底后**不写死信也不写历史**，
        通知静默丢失且无留痕。本条锁住"非数值"这一支的回落行为。

        校验发生在 NotificationRouter 构造期（写回 self.base_delay），
        事后赋值属性会绕过校验——故必须用构造参数触发。
        """
        sleeper = _CountingSleeper()
        capture = _LogCapture(level="WARNING")

        # 告警发生在 NotificationRouter 构造期（校验写回 self.base_delay），
        # 必须在构造外面包 sink，否则只截到后续的重试日志
        with capture:
            router = NotificationRouter(
                base_delay="not-a-number",  # type: ignore[arg-type]
                max_retries=2,
                sleeper=sleeper,
                dead_letter_repo=_NullRepo(),
                history_repo=_NullRepo(),
            )
        notifier = _FlakyNotifier()

        success, attempts, _reason = router._send_with_retry(
            notifier, Notification(
                execution_id="RUN-V2-BACKOFF-0001",
                title="退避基数非数值",
                content="正文",
            )
        )

        assert success is False
        assert attempts == 3, "总尝试次数 = 1 + max_retries = 3"
        assert notifier.calls == 3, "非法退避基数不得让重试次数归零"
        assert router.base_delay == notification_mod.DEFAULT_BASE_DELAY, (
            f"非法退避基数应回落默认值，实际: {router.base_delay!r}"
        )
        assert len(sleeper.calls) == 2, "3次尝试之间应有2次退避等待"
        assert all(delay > 0 for delay in sleeper.calls), (
            f"回落后每次等待都应为正数，实际: {sleeper.calls}"
        )
        assert any(
            "退避基数非法" in text for text in capture.messages
        ), f"应对非数值退避基数告警，实际: {capture.messages}"

    def test_retry_backoff_is_exponential(self) -> None:
        """
        退避序列为 base×2^(k-1) 的指数增长（正向对照）

        锁住退避算法本身：注入 sleeper 后等待序列变成可断言的纯数据。
        """
        sleeper = _CountingSleeper()
        router = NotificationRouter(
            base_delay=1.0,
            max_retries=3,
            sleeper=sleeper,
            dead_letter_repo=_NullRepo(),
            history_repo=_NullRepo(),
        )

        _success, attempts, _reason = router._send_with_retry(
            _FlakyNotifier(),
            Notification(
                execution_id="RUN-V2-BACKOFF-0002",
                title="指数退避",
                content="正文",
            ),
        )

        assert attempts == 4, "1 + max_retries(3) = 4 次尝试"
        assert sleeper.calls == [1.0, 2.0, 4.0], (
            f"退避序列应为 base×2^(k-1)，实际: {sleeper.calls}"
        )


# ===========================================================================
# E组: 路由层异常隔离与历史落库兜底
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("notification 路由异常隔离")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestRouterIsolation:
    """渠道抛异常不影响其它渠道 / 历史入库失败不影响主流程"""

    def test_channel_exception_isolated(self) -> None:
        """
        渠道循环内的异常被隔离、其它渠道不受影响（行1618-1623）

        这是项目设计原则「通知是旁路能力」的核心守卫：任一渠道环节抛异常
        绝不能连带其它渠道失败，更不能让批次收尾流程崩掉。

        **触发方式说明**（本文件最反直觉的一处）: 渠道 try 块内包了
        _send_with_retry / _save_history / _save_dead_letter，三者各自
        已有内层兜底；send 抛出的异常也被 _send_with_retry 内部吃掉并
        计入重试（所以"渠道 send 直接抛异常"命中的是 1600 行的
        "重试耗尽"，不是本条）。要验证这道**外层兜底**，只能用替身
        打破内层守卫：此处把 _save_history 换成会抛异常的版本，
        模拟内层兜底失效、或未来重构移除了内层 try 的情形。
        这正是双层守卫的意义——内层被打破时外层仍能兜住。

        notifiers 是 list（不是 dict），渠道身份由 channel_name 属性标识。
        """

        class _ExplodingChannel:
            """契约被打破的渠道：send 直接抛异常"""

            channel_name = "email"

            def is_enabled(self) -> bool:
                """恒启用"""
                return True

            def send(self, _notification: Any) -> bool:
                """直接抛异常"""
                raise RuntimeError("渠道内部代码 bug")

        class _HealthyChannel:
            """正常渠道：用于验证异常隔离后它仍能跑完"""

            channel_name = "wechat"

            def __init__(self) -> None:
                """初始化调用计数"""
                self.calls = 0

            def is_enabled(self) -> bool:
                """恒启用"""
                return True

            def send(self, _notification: Any) -> bool:
                """正常返回"""
                self.calls += 1
                return True
        healthy = _HealthyChannel()
        router = NotificationRouter(
            notifiers=[_ExplodingChannel(), healthy],
            max_retries=0,
            strategy="all",
            sleeper=_CountingSleeper(),
            dead_letter_repo=_NullRepo(),
            history_repo=_NullRepo(),
        )

        def _raise_on_history(*_args, **_kwargs) -> None:
            """模拟内层守卫失效：历史写入直接抛出"""
            raise RuntimeError("模拟内层兜底被打破")

        router._save_history = _raise_on_history  # type: ignore[method-assign]
        capture = _LogCapture(level="WARNING")

        with capture:
            results = router.notify(
                _build_stat([]), "RUN-V2-ROUTE-0001", strategy="all"
            )

        assert results.get("email") is False, (
            "抛异常的渠道必须记为失败，不能让它把整个路由带崩"
        )
        assert healthy.calls == 1, (
            "健康渠道必须照常收到通知——异常隔离的真正价值所在"
        )
        assert any(
            "渠道通知异常已捕获" in text for text in capture.messages
        ), f"应记 warning 说明渠道异常已隔离，实际: {capture.messages}"

    def test_history_save_failure_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        通知历史入库失败不得影响主流程（行1769-1773）

        历史表只是留痕手段，入库失败（如磁盘满、表被锁）绝不能让
        已成功的通知被回滚成失败，更不能打断批次收尾。
        """
        router = NotificationRouter()

        def _raise_on_save(*_args, **_kwargs) -> None:
            """模拟历史入库失败"""
            raise RuntimeError("模拟数据库不可用")

        monkeypatch.setattr(
            notification_mod.NotificationHistoryRepository,
            "save_history",
            _raise_on_save,
        )
        capture = _LogCapture(level="ERROR")

        with capture:
            # 不应抛出任何异常
            router._save_history(
                Notification(
                    execution_id="RUN-V2-HIST-0001",
                    title="历史入库失败测试",
                    content="正文",
                ),
                channel="email",
                status="success",
                attempts=1,
            )

        assert any(
            "通知历史入库失败" in text for text in capture.messages
        ), f"应记 error 但不抛出，实际: {capture.messages}"


# ===========================================================================
# F组: 企微失败明细摘要的截断与溢出
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("notification 失败明细摘要")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.regression
class TestFailedDetailSummary:
    """错误信息截断 / 超10条汇总行"""

    def test_failed_detail_message_truncated(self) -> None:
        """
        失败详情的错误信息超 50 字符截断（行1870）

        企微 markdown 消息体有长度上限，失败详情不截断会让整条消息
        被企微拒绝，且一条用例的堆栈就能把其它失败明细全挤掉。
        """
        summary = NotificationRouter()._build_wechat_summary(
            _build_stat([_build_detail("用例A", "E" * 120)]),
            "RUN-V2-SUMMARY-0001",
            [],
        )

        assert "E" * 120 not in summary, "超长错误信息必须被截断"
        assert "E" * 50 + "..." in summary, "应保留前 50 字符并追加省略号"

    def test_failed_details_overflow_summary(self) -> None:
        """
        失败明细超 10 条时追加汇总行（行1875）

        摘要最多列 10 条，但**必须告诉用户还剩多少条**——否则用户
        会以为只有 10 条失败，漏判问题规模。
        """
        details = [
            _build_detail(f"用例{i:02d}", f"断言失败 {i}") for i in range(15)
        ]

        summary = NotificationRouter()._build_wechat_summary(
            _build_stat(details), "RUN-V2-SUMMARY-0002", []
        )

        # 明细行形如 "- 用例NN（用户中心）: 断言失败 N"；摘要头部还有
        # "- 用例总数：15" 一行，故不能直接 count("- 用例")，按模块后缀定位
        detail_lines = [
            line for line in summary.splitlines() if "（用户中心）" in line
        ]
        assert len(detail_lines) == 10, f"明细列表最多列 10 条，实际: {detail_lines}"
        assert "等共15条失败用例" in summary, (
            "超出部分必须以汇总行告知总数，否则用户会低估失败规模"
        )

    def test_failed_details_within_limit_no_summary(self) -> None:
        """
        失败明细不超过 10 条时不追加汇总行（对照组）

        证明上一条不是因为"恒追加汇总"才通过。
        """
        details = [_build_detail(f"用例{i:02d}", "断言失败") for i in range(3)]

        summary = NotificationRouter()._build_wechat_summary(
            _build_stat(details), "RUN-V2-SUMMARY-0003", []
        )

        assert "等共" not in summary, "未超限不应出现汇总行"
        detail_lines = [
            line for line in summary.splitlines() if "（用户中心）" in line
        ]
        assert len(detail_lines) == 3, f"3 条明细应全部列出，实际: {detail_lines}"
