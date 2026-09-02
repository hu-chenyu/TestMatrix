"""
企微webhook通知器验证用例（第二阶段Day12）

验证目标:
    1. 继承关系与接口约束: WeChatNotifier是BaseNotifier子类
    2. 配置解析: 开关读取、URL格式校验
    3. 发送链路: 成功/业务失败/未启用跳过/URL缺失/网络异常容错
    4. payload格式: msgtype=markdown且content含标题
    5. markdown渲染: 标题/正文/批次号/通过率着色
    6. HTML转换: 标签去除与纯文本透传

Mock策略:
    全程mock requests.post与env_manager.get/get_bool，
    不真实发送任何HTTP请求、不连接企微服务器。
"""

from unittest.mock import MagicMock, patch

import allure
import pytest
import requests

from src.common.env_manager import env_manager
from src.core.notification import BaseNotifier, Notification, WeChatNotifier

# 标准企微配置（mock时使用）
WECHAT_CONFIG = {
    "TM_WECHAT_ENABLED": "true",
    "TM_WECHAT_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key-123456",
}


def _mock_get(config: dict):
    """
    构造env_manager.get替身函数

    参数:
        config (dict): 配置键值表

    返回:
        function: get(key, default)替身
    """
    def _get(key, default=None):
        value = config.get(key)
        return value if value is not None else default
    return _get


def _mock_get_bool(config: dict):
    """
    构造env_manager.get_bool替身函数

    参数:
        config (dict): 配置键值表

    返回:
        function: get_bool(key, default)替身
    """
    def _get_bool(key, default=False):
        value = config.get(key)
        if value is None:
            return default
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    return _get_bool


def patch_wechat_env(config: dict):
    """
    构造企微配置双方法mock上下文（构造期+发送期全程生效）

    参数:
        config (dict): 企微配置键值表

    返回:
        tuple: (get_patch, get_bool_patch)已激活patch对
    """
    return (
        patch.object(env_manager, "get", side_effect=_mock_get(config)),
        patch.object(env_manager, "get_bool", side_effect=_mock_get_bool(config)),
    )


def make_wechat_notifier(config: dict) -> WeChatNotifier:
    """
    按指定配置构造WeChatNotifier（mock环境变量，仅构造期）

    参数:
        config (dict): 企微配置键值表

    返回:
        WeChatNotifier: 初始化完成的企微通知器实例
    """
    get_patch, bool_patch = patch_wechat_env(config)
    with get_patch, bool_patch:
        return WeChatNotifier()


@allure.feature("通知模块")
@allure.story("企微webhook通知")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestWeChatNotifierBasics:
    """继承关系与配置解析验证"""

    def test_wechat_notifier_inherits_base(self):
        """
        继承约束: WeChatNotifier是BaseNotifier子类且具备send方法

        参数:
            无

        返回:
            无
        """
        notifier = make_wechat_notifier(WECHAT_CONFIG)
        assert isinstance(notifier, BaseNotifier)
        assert callable(notifier.send)
        assert callable(notifier.is_enabled)

    def test_is_enabled_reads_config(self):
        """
        开关读取: TM_WECHAT_ENABLED=true返回True，
        false时返回False

        参数:
            无

        返回:
            无
        """
        enabled_notifier = make_wechat_notifier(WECHAT_CONFIG)
        get_patch, bool_patch = patch_wechat_env(WECHAT_CONFIG)
        with get_patch, bool_patch:
            assert enabled_notifier.is_enabled() is True

        disabled_config = {**WECHAT_CONFIG, "TM_WECHAT_ENABLED": "false"}
        disabled_notifier = make_wechat_notifier(disabled_config)
        get_patch, bool_patch = patch_wechat_env(disabled_config)
        with get_patch, bool_patch:
            assert disabled_notifier.is_enabled() is False


@allure.feature("通知模块")
@allure.story("企微webhook通知")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestWeChatSendFlow:
    """企微发送链路验证（全mock，不真实发送）"""

    def test_send_success(self):
        """
        成功路径: mock响应{"errcode":0}，send()返回True，
        requests.post被调用一次

        参数:
            无

        返回:
            无
        """
        notification = Notification(title="测试报告", content="执行完成")

        get_patch, bool_patch = patch_wechat_env(WECHAT_CONFIG)
        with get_patch, bool_patch, patch("requests.post") as mock_post:
            notifier = WeChatNotifier()
            mock_response = MagicMock()
            mock_response.json.return_value = {"errcode": 0, "errmsg": "ok"}
            mock_post.return_value = mock_response

            result = notifier.send(notification)

        assert result is True
        assert mock_post.call_count == 1

    def test_send_business_failure(self):
        """
        业务失败: errcode=40058时send()返回False

        参数:
            无

        返回:
            无
        """
        get_patch, bool_patch = patch_wechat_env(WECHAT_CONFIG)
        with get_patch, bool_patch, patch("requests.post") as mock_post:
            notifier = WeChatNotifier()
            mock_response = MagicMock()
            mock_response.json.return_value = {
                "errcode": 40058, "errmsg": "invalid webhook url"
            }
            mock_post.return_value = mock_response

            result = notifier.send(Notification(title="t", content="c"))

        assert result is False

    def test_send_skipped_when_disabled(self):
        """
        未启用跳过: 开关false时send()返回False，
        requests.post未被调用

        参数:
            无

        返回:
            无
        """
        disabled_config = {**WECHAT_CONFIG, "TM_WECHAT_ENABLED": "false"}

        get_patch, bool_patch = patch_wechat_env(disabled_config)
        with get_patch, bool_patch, patch("requests.post") as mock_post:
            notifier = WeChatNotifier()
            result = notifier.send(Notification(title="t", content="c"))

        assert result is False
        mock_post.assert_not_called()

    def test_send_missing_webhook_url(self):
        """
        URL缺失: TM_WECHAT_WEBHOOK_URL为空时send()返回False，
        不发请求

        参数:
            无

        返回:
            无
        """
        no_url_config = {**WECHAT_CONFIG, "TM_WECHAT_WEBHOOK_URL": ""}

        get_patch, bool_patch = patch_wechat_env(no_url_config)
        with get_patch, bool_patch, patch("requests.post") as mock_post:
            notifier = WeChatNotifier()
            result = notifier.send(Notification(title="t", content="c"))

        assert result is False
        mock_post.assert_not_called()

    def test_send_network_exception(self):
        """
        网络异常容错: requests.post抛ConnectionError时
        send()返回False，异常不向上抛出

        参数:
            无

        返回:
            无
        """
        get_patch, bool_patch = patch_wechat_env(WECHAT_CONFIG)
        with get_patch, bool_patch, patch("requests.post") as mock_post:
            notifier = WeChatNotifier()
            mock_post.side_effect = requests.exceptions.ConnectionError(
                "Connection refused"
            )

            # 不抛异常即通过（pytest.raises未包裹）
            result = notifier.send(Notification(title="t", content="c"))

        assert result is False

    def test_payload_format_correct(self):
        """
        payload格式: 请求json参数含msgtype=markdown，
        markdown.content包含通知标题

        参数:
            无

        返回:
            无
        """
        notification = Notification(title="批次执行完成", content="全部通过")

        get_patch, bool_patch = patch_wechat_env(WECHAT_CONFIG)
        with get_patch, bool_patch, patch("requests.post") as mock_post:
            notifier = WeChatNotifier()
            mock_response = MagicMock()
            mock_response.json.return_value = {"errcode": 0, "errmsg": "ok"}
            mock_post.return_value = mock_response

            notifier.send(notification)

        # 校验payload结构
        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json")
        assert payload is not None
        assert payload["msgtype"] == "markdown"
        assert "批次执行完成" in payload["markdown"]["content"]
        # 超时参数
        assert call_kwargs.kwargs.get("timeout") == 10


@allure.feature("通知模块")
@allure.story("企微webhook通知")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestMarkdownRendering:
    """markdown内容渲染与HTML转换验证"""

    def test_markdown_content_rendering(self):
        """
        markdown渲染: 标题二级/正文/批次号引用/通过率着色
        全部正确输出

        参数:
            无

        返回:
            无
        """
        notifier = make_wechat_notifier(WECHAT_CONFIG)
        notification = Notification(
            title="测试报告", content="用例全部通过",
            execution_id="RUN-001", pass_rate=1.0,
        )

        markdown = notifier._build_markdown_content(notification)

        assert "## " in markdown
        assert "测试报告" in markdown
        assert "用例全部通过" in markdown
        assert "RUN-001" in markdown
        assert "100.00%" in markdown
        # 100%通过率用info绿色
        assert '<font color="info">' in markdown

    def test_html_content_conversion(self):
        """
        HTML转换: 含标签内容去标签为纯文本；
        纯文本原样返回

        参数:
            无

        返回:
            无
        """
        converted = WeChatNotifier._convert_to_markdown(
            "<p>Hello <b>World</b></p>"
        )
        assert converted == "Hello World"

        # 纯文本透传
        assert WeChatNotifier._convert_to_markdown("普通内容") == "普通内容"
        # 空内容
        assert WeChatNotifier._convert_to_markdown("") == ""

    def test_level_icons_and_url_mask(self):
        """
        级别图标与URL脱敏: critical加⚠️、warning加🔔、info加📋；
        webhook URL日志脱敏为前30字符

        参数:
            无

        返回:
            无
        """
        notifier = make_wechat_notifier(WECHAT_CONFIG)

        critical_md = notifier._build_markdown_content(
            Notification(title="严重告警", content="c", level="critical")
        )
        assert "⚠️" in critical_md

        warning_md = notifier._build_markdown_content(
            Notification(title="警告", content="c", level="warning")
        )
        assert "🔔" in warning_md

        info_md = notifier._build_markdown_content(
            Notification(title="常规", content="c", level="info")
        )
        assert "📋" in info_md

        # URL脱敏: 长URL只保留前30字符
        masked = WeChatNotifier._mask_url(notifier.webhook_url)
        assert masked.startswith("https://qyapi.weixin.qq.com/cg")
        assert "..." in masked
        assert "test-key-123456" not in masked
