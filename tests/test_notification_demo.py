"""
通知模块基座验证用例（第二阶段Day10）

验证目标:
    1. Notification数据结构: 默认值与全字段构造
    2. BaseNotifier抽象约束: 不可实例化、子类必须实现send
    3. EmailNotifier配置解析: 开关/必填项/收件人解析（去空去重保序）
    4. send发送链路: SSL(465)/STARTTLS(587)双策略成功路径、
       认证失败容错、未启用跳过
    5. HTML内容自动判定

Mock策略:
    全部用unittest.mock.patch拦截smtplib.SMTP_SSL/SMTP与env_manager.get，
    不真实连接任何SMTP服务器、不发真实邮件。
"""

from unittest.mock import MagicMock, patch

import allure
import pytest

from src.common.env_manager import env_manager
from src.core.notification import (
    BaseNotifier,
    EmailNotifier,
    Notification,
)

# 标准完整邮件配置（mock env_manager时使用）
FULL_EMAIL_CONFIG = {
    "TM_EMAIL_ENABLED": "true",
    "TM_EMAIL_SMTP_HOST": "smtp.test.com",
    "TM_EMAIL_SMTP_PORT": "465",
    "TM_EMAIL_SENDER": "sender@test.com",
    "TM_EMAIL_PASSWORD": "auth_code_123",
    "TM_EMAIL_RECEIVERS": "a@test.com,b@test.com",
}


def mock_env_get(config: dict):
    """
    构造env_manager.get的mock侧效果函数

    参数:
        config (dict): 键值配置表（None值表示该项未配置）

    返回:
        function: get(key, default)的替身函数
    """
    def _get(key, default=None):
        value = config.get(key)
        return value if value is not None else default
    return _get


def mock_env_get_bool(config: dict):
    """
    构造env_manager.get_bool的mock侧效果函数

    参数:
        config (dict): 键值配置表

    返回:
        function: get_bool(key, default)的替身函数
    """
    def _get_bool(key, default=False):
        value = config.get(key)
        if value is None:
            return default
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    return _get_bool


def make_notifier(config: dict) -> EmailNotifier:
    """
    按指定配置构造EmailNotifier（mock环境变量）

    参数:
        config (dict): 邮件配置键值表

    返回:
        EmailNotifier: 初始化完成的邮件通知器实例
    """
    with patch.object(env_manager, "get", side_effect=mock_env_get(config)), \
         patch.object(env_manager, "get_bool", side_effect=mock_env_get_bool(config)):
        return EmailNotifier()


def patch_env(config: dict):
    """
    构造env_manager双方法mock的上下文（send链路全程生效）

    参数:
        config (dict): 邮件配置键值表

    返回:
        tuple: (get_patch, get_bool_patch) 已激活的patch对象对
    """
    return (
        patch.object(env_manager, "get", side_effect=mock_env_get(config)),
        patch.object(env_manager, "get_bool", side_effect=mock_env_get_bool(config)),
    )


@allure.feature("通知模块")
@allure.story("通知数据结构")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestNotificationDataclass:
    """Notification数据类验证"""

    def test_notification_defaults(self):
        """
        默认值: level=info、extra空字典、created_at非空

        参数:
            无

        返回:
            无
        """
        notification = Notification(title="测试通知", content="正文内容")

        assert notification.level == "info"
        assert notification.extra == {}
        assert notification.created_at is not None
        assert notification.execution_id is None
        assert notification.pass_rate is None
        assert notification.total_cases is None
        assert notification.failed_cases is None

    def test_notification_full_fields(self):
        """
        全字段构造: 所有字段赋值正确

        参数:
            无

        返回:
            无
        """
        notification = Notification(
            title="批次执行完成",
            content="<table>汇总表格</table>",
            level="critical",
            execution_id="RUN-20260831-100000-abcd",
            pass_rate=0.95,
            total_cases=100,
            failed_cases=5,
            extra={"owner": "admin", "at_users": ["张三"]},
        )

        assert notification.title == "批次执行完成"
        assert notification.level == "critical"
        assert notification.execution_id == "RUN-20260831-100000-abcd"
        assert notification.pass_rate == 0.95
        assert notification.total_cases == 100
        assert notification.failed_cases == 5
        assert notification.extra["owner"] == "admin"


@allure.feature("通知模块")
@allure.story("抽象基类约束")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestBaseNotifierAbstraction:
    """BaseNotifier抽象基类约束验证"""

    def test_base_notifier_is_abstract(self):
        """
        抽象约束: BaseNotifier不可直接实例化（TypeError）；
        未实现send的子类同样不可实例化

        参数:
            无

        返回:
            无
        """
        with pytest.raises(TypeError):
            BaseNotifier()

        class IncompleteNotifier(BaseNotifier):
            """未实现send的残缺子类"""

        with pytest.raises(TypeError):
            IncompleteNotifier()

    def test_base_notifier_build_notification(self):
        """
        便捷构造: build_notification填充标题正文与可选字段

        参数:
            无

        返回:
            无
        """

        class DummyNotifier(BaseNotifier):
            """最小实现子类（仅用于基类方法验证）"""

            def send(self, notification):
                return True

        notification = DummyNotifier.build_notification(
            "标题", "内容", level="warning", pass_rate=0.8
        )
        assert notification.title == "标题"
        assert notification.content == "内容"
        assert notification.level == "warning"
        assert notification.pass_rate == 0.8


@allure.feature("通知模块")
@allure.story("邮件配置解析")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestEmailConfigParsing:
    """EmailNotifier配置读取与解析验证"""

    def test_is_enabled_false_skips_send(self):
        """
        开关关闭: TM_EMAIL_ENABLED=false时is_enabled()为False，
        send()直接返回False且不建立任何SMTP连接

        参数:
            无

        返回:
            无
        """
        config = {**FULL_EMAIL_CONFIG, "TM_EMAIL_ENABLED": "false"}
        notifier = make_notifier(config)

        assert notifier.is_enabled() is False

        with patch("smtplib.SMTP_SSL") as mock_ssl:
            result = notifier.send(Notification(title="t", content="c"))

        assert result is False
        mock_ssl.assert_not_called()

    def test_config_parsing_complete(self):
        """
        完整配置解析: host/port/sender/receivers正确读取，开关为True

        参数:
            无

        返回:
            无
        """
        get_patch, bool_patch = patch_env(FULL_EMAIL_CONFIG)
        with get_patch, bool_patch:
            notifier = EmailNotifier()

            assert notifier.smtp_host == "smtp.test.com"
            assert notifier.smtp_port == 465
            assert notifier.sender == "sender@test.com"
            assert notifier.receivers == ["a@test.com", "b@test.com"]
            assert notifier.is_enabled() is True

    def test_receivers_parsing_dedup(self):
        """
        收件人解析: "a@x.com, b@x.com,,a@x.com"→
        去空去重保序列表["a@x.com", "b@x.com"]

        参数:
            无

        返回:
            无
        """
        config = {**FULL_EMAIL_CONFIG, "TM_EMAIL_RECEIVERS": "a@x.com, b@x.com,,a@x.com"}
        notifier = make_notifier(config)

        assert notifier.receivers == ["a@x.com", "b@x.com"]


@allure.feature("通知模块")
@allure.story("邮件发送链路")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestEmailSendFlow:
    """EmailNotifier发送链路验证（全mock，不真实连接）"""

    def test_send_success_ssl_port_465(self):
        """
        SSL路径: 端口465走SMTP_SSL，login/sendmail/quit均被调用，
        返回True

        参数:
            无

        返回:
            无
        """
        notification = Notification(title="批次报告", content="纯文本正文")

        get_patch, bool_patch = patch_env(FULL_EMAIL_CONFIG)
        with get_patch, bool_patch, patch("smtplib.SMTP_SSL") as mock_ssl_class:
            notifier = EmailNotifier()
            mock_client = MagicMock()
            mock_ssl_class.return_value = mock_client

            result = notifier.send(notification)

        assert result is True
        mock_ssl_class.assert_called_once_with(
            "smtp.test.com", 465, timeout=10
        )
        mock_client.login.assert_called_once_with("sender@test.com", "auth_code_123")
        mock_client.sendmail.assert_called_once()
        mock_client.quit.assert_called_once()

    def test_send_success_tls_port_587(self):
        """
        STARTTLS路径: 端口587走SMTP+starttls()，
        sendmail调用且返回True

        参数:
            无

        返回:
            无
        """
        config = {**FULL_EMAIL_CONFIG, "TM_EMAIL_SMTP_PORT": "587"}

        get_patch, bool_patch = patch_env(config)
        with get_patch, bool_patch, patch("smtplib.SMTP") as mock_smtp_class:
            notifier = EmailNotifier()
            mock_client = MagicMock()
            mock_smtp_class.return_value = mock_client

            result = notifier.send(Notification(title="t", content="c"))

        assert result is True
        mock_client.starttls.assert_called_once()
        mock_client.sendmail.assert_called_once()

    def test_send_failure_auth_error(self):
        """
        失败容错: login抛SMTPAuthenticationError时
        send()返回False、异常不向上抛出、quit仍被调用（连接释放）

        参数:
            无

        返回:
            无
        """
        import smtplib

        get_patch, bool_patch = patch_env(FULL_EMAIL_CONFIG)
        with get_patch, bool_patch, patch("smtplib.SMTP_SSL") as mock_ssl_class:
            notifier = EmailNotifier()
            mock_client = MagicMock()
            mock_client.login.side_effect = smtplib.SMTPAuthenticationError(
                535, b"Authentication failed"
            )
            mock_ssl_class.return_value = mock_client

            result = notifier.send(Notification(title="t", content="c"))

        assert result is False
        mock_client.quit.assert_called_once()


@allure.feature("通知模块")
@allure.story("内容类型判定")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestHtmlDetection:
    """HTML内容自动判定验证"""

    def test_html_detection(self):
        """
        HTML判定: 含<table>/<div>返回True，纯文本返回False

        参数:
            无

        返回:
            无
        """
        assert EmailNotifier._is_html("<table><tr><td>汇总</td></tr></table>") is True
        assert EmailNotifier._is_html("<div>块内容</div>") is True
        assert EmailNotifier._is_html("<html><body>页面</body></html>") is True
        assert EmailNotifier._is_html("纯文本内容，无标签") is False
        assert EmailNotifier._is_html("P0用例全部通过，共100条") is False
        assert EmailNotifier._is_html("") is False
