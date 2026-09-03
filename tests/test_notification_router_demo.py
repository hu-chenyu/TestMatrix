"""
通知路由器验证用例（第二阶段Day13）

验证目标:
    1. 分级策略: all全通过也通知 / failed_only全通过跳过、有失败通知 / 非法值降级
    2. 负责人收集: 去重保序 / 手机号与userid分流 / @all开关 / 额外手机号合并
    3. notify分发: 双渠道调用与结果字典 / 单渠道失败不影响另一渠道 /
       策略跳过时零调用
    4. 端到端: AllureResult(owner标签)→aggregate→notify→
       企微payload含mentioned字段 / 邮件HTML含负责人列与提示行

Mock策略:
    patch.object(env_manager, "get"/"get_bool") 成对mock且覆盖
    实例化期与发送期全部读取点（Day10教训）；企微mock requests.post，
    邮件mock smtplib.SMTP_SSL；零真实连接。
"""

from unittest.mock import MagicMock, patch

import allure
import pytest

from src.common.env_manager import env_manager
from src.core.notification import (
    BaseNotifier,
    NotificationRouter,
)
from src.core.report_analyzer import (
    AllureResult,
    FailedCaseDetail,
    ReportStatistics,
    StatisticsResult,
)

# 路由器相关env配置基线（无额外@人配置）
ROUTER_ENV = {
    "TM_NOTIFY_STRATEGY": "all",
    "TM_NOTIFY_AT_ALL": "false",
    "TM_NOTIFY_OWNER_MOBILES": "",
}

# 企微完整配置（端到端mock用）
WECHAT_ENV = {
    "TM_WECHAT_ENABLED": "true",
    "TM_WECHAT_WEBHOOK_URL": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key",
}

# 邮件完整配置（端到端mock用）
EMAIL_ENV = {
    "TM_EMAIL_ENABLED": "true",
    "TM_EMAIL_SMTP_HOST": "smtp.test.com",
    "TM_EMAIL_SMTP_PORT": "465",
    "TM_EMAIL_SENDER": "sender@test.com",
    "TM_EMAIL_PASSWORD": "auth_code",
    "TM_EMAIL_RECEIVERS": "a@test.com,b@test.com",
}

FULL_ENV = {**ROUTER_ENV, **WECHAT_ENV, **EMAIL_ENV}


def _mock_get(config: dict):
    """
    构造env_manager.get替身

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
    构造env_manager.get_bool替身

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


def patch_env(config: dict):
    """
    构造双方法mock上下文（实例化期+发送期全程生效）

    参数:
        config (dict): 配置键值表

    返回:
        tuple: (get_patch, get_bool_patch)
    """
    return (
        patch.object(env_manager, "get", side_effect=_mock_get(config)),
        patch.object(env_manager, "get_bool", side_effect=_mock_get_bool(config)),
    )


def make_stat(**overrides) -> StatisticsResult:
    """
    构造StatisticsResult（默认全通过10条）

    参数:
        **overrides: 覆盖字段

    返回:
        StatisticsResult: 统计结果对象
    """
    defaults = {
        "total": 10, "passed": 10, "failed": 0, "broken": 0,
        "skipped": 0, "pass_rate": 1.0,
        "total_duration_ms": 1000, "avg_duration_ms": 100.0,
        "p95_duration_ms": 150.0, "min_duration_ms": 50, "max_duration_ms": 200,
    }
    defaults.update(overrides)
    return StatisticsResult(**defaults)


def make_failed_details(owners: list) -> list:
    """
    按负责人列表构造失败明细

    参数:
        owners (list): 每条明细的owner值（空串表示无负责人）

    返回:
        List[FailedCaseDetail]: 失败明细列表
    """
    return [
        FailedCaseDetail(
            uuid=f"uuid-{index}", name=f"case_{index}",
            full_name=f"tests#case_{index}", status="failed",
            duration_ms=100, module="用户管理", priority="critical",
            error_message=f"断言失败{index}", owner=owner,
        )
        for index, owner in enumerate(owners)
    ]


class MockNotifier(BaseNotifier):
    """可控结果的mock渠道（channel_name可定制）"""

    def __init__(self, channel_name: str = "mock", send_result: bool = True):
        self.channel_name = channel_name
        self._send_result = send_result
        self.sent_notifications = []

    def send(self, notification):
        self.sent_notifications.append(notification)
        return self._send_result


@allure.feature("通知模块")
@allure.story("分级通知策略")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestNotifyStrategy:
    """分级策略（all/failed_only）验证"""

    def test_strategy_all_notifies_on_success(self):
        """
        策略all: 全通过批次should_notify返回True

        参数:
            无

        返回:
            无
        """
        get_patch, bool_patch = patch_env(ROUTER_ENV)
        with get_patch, bool_patch:
            router = NotificationRouter(strategy="all")
        assert router.should_notify(make_stat()) is True

    def test_strategy_failed_only(self):
        """
        策略failed_only: 全通过返回False；
        含failed/broken的stat返回True

        参数:
            无

        返回:
            无
        """
        get_patch, bool_patch = patch_env(ROUTER_ENV)
        with get_patch, bool_patch:
            router = NotificationRouter(strategy="failed_only")

        # 全通过→False
        assert router.should_notify(make_stat()) is False
        # 含失败（failed+broken合计口径）→True
        failed_stat = make_stat(
            total=10, passed=7, failed=3, broken=1, pass_rate=0.7
        )
        assert router.should_notify(failed_stat) is True

    def test_invalid_strategy_fallback_all(self):
        """
        非法策略值: 不抛异常，warning降级为all，
        should_notify全通过也返回True

        参数:
            无

        返回:
            无
        """
        get_patch, bool_patch = patch_env(ROUTER_ENV)
        with get_patch, bool_patch:
            router = NotificationRouter(strategy="xxx")

        assert router.strategy == "all"
        assert router.should_notify(make_stat()) is True


@allure.feature("通知模块")
@allure.story("负责人收集")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestCollectOwners:
    """负责人收集与@名单分流验证"""

    def test_collect_owners_dedup(self):
        """
        去重保序: owner为[张三,李四,张三,""]→
        owner_names=[张三,李四]（去重去空保序）

        参数:
            无

        返回:
            无
        """
        stat = make_stat(
            total=4, passed=1, failed=3, broken=0, pass_rate=0.25,
            failed_details=make_failed_details(["张三", "李四", "张三", ""]),
        )
        get_patch, bool_patch = patch_env(ROUTER_ENV)
        with get_patch, bool_patch:
            router = NotificationRouter(strategy="all")
            owner_names, _, _ = router.collect_owners(stat)

        assert owner_names == ["张三", "李四"]

    def test_mobile_and_userid_split(self):
        """
        手机号/userid分流: 13800000000进mentioned_mobile_list，
        zhangsan进mentioned_list

        参数:
            无

        返回:
            无
        """
        stat = make_stat(
            total=3, passed=1, failed=2, pass_rate=0.33,
            failed_details=make_failed_details(["13800000000", "zhangsan"]),
        )
        get_patch, bool_patch = patch_env(ROUTER_ENV)
        with get_patch, bool_patch:
            router = NotificationRouter(strategy="all")
            _, mentioned_list, mobile_list = router.collect_owners(stat)

        assert mentioned_list == ["zhangsan"]
        assert mobile_list == ["13800000000"]

    def test_at_all_and_extra_mobiles(self):
        """
        @all开关与额外手机号: TM_NOTIFY_AT_ALL=true时mentioned_list
        首位为"@all"；TM_NOTIFY_OWNER_MOBILES合并去重保序

        参数:
            无

        返回:
            无
        """
        config = {
            **ROUTER_ENV,
            "TM_NOTIFY_AT_ALL": "true",
            "TM_NOTIFY_OWNER_MOBILES": "13900000000,13800000000,13700000000",
        }
        stat = make_stat(
            total=2, passed=0, failed=2, pass_rate=0.0,
            failed_details=make_failed_details(["13800000000", "zhangsan"]),
        )
        get_patch, bool_patch = patch_env(config)
        with get_patch, bool_patch:
            router = NotificationRouter(strategy="all")
            owner_names, mentioned_list, mobile_list = router.collect_owners(stat)

        assert mentioned_list[0] == "@all"
        assert "zhangsan" in mentioned_list
        # 额外手机号合并（138重复去重）
        assert mobile_list == ["13800000000", "13900000000", "13700000000"]


@allure.feature("通知模块")
@allure.story("通知分发")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestNotifyDispatch:
    """notify分发与渠道隔离验证"""

    def test_dispatch_calls_both_channels(self):
        """
        双渠道分发: 策略all时两个mock渠道各被调用1次，
        返回{"email":True,"wechat":True}

        参数:
            无

        返回:
            无
        """
        email_notifier = MockNotifier("email", True)
        wechat_notifier = MockNotifier("wechat", True)

        get_patch, bool_patch = patch_env(ROUTER_ENV)
        with get_patch, bool_patch:
            router = NotificationRouter(
                notifiers=[email_notifier, wechat_notifier]
            )
            results = router.notify(make_stat(), "RUN-DISPATCH-001")

        assert results == {"email": True, "wechat": True}
        assert len(email_notifier.sent_notifications) == 1
        assert len(wechat_notifier.sent_notifications) == 1
        # extra注入@名单
        email_extra = email_notifier.sent_notifications[0].extra
        assert "mentioned_list" in email_extra
        assert "owner_names" in email_extra

    def test_single_channel_failure_isolated(self):
        """
        渠道隔离: wechat返回False不影响email成功，
        结果字典如实记录False

        参数:
            无

        返回:
            无
        """
        email_notifier = MockNotifier("email", True)
        wechat_notifier = MockNotifier("wechat", False)

        get_patch, bool_patch = patch_env(ROUTER_ENV)
        with get_patch, bool_patch:
            router = NotificationRouter(
                notifiers=[email_notifier, wechat_notifier]
            )
            results = router.notify(make_stat(), "RUN-ISO-001")

        assert results == {"email": True, "wechat": False}
        assert len(email_notifier.sent_notifications) == 1

    def test_failed_only_skip_all_channels(self):
        """
        策略跳过: failed_only+全通过时两渠道send均未被调用，
        notify返回空dict

        参数:
            无

        返回:
            无
        """
        email_notifier = MockNotifier("email", True)
        wechat_notifier = MockNotifier("wechat", True)

        get_patch, bool_patch = patch_env(ROUTER_ENV)
        with get_patch, bool_patch:
            router = NotificationRouter(
                strategy="failed_only",
                notifiers=[email_notifier, wechat_notifier],
            )
            results = router.notify(make_stat(), "RUN-SKIP-001")

        assert results == {}
        assert email_notifier.sent_notifications == []
        assert wechat_notifier.sent_notifications == []


@allure.feature("通知模块")
@allure.story("端到端集成")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.api
@pytest.mark.regression
class TestRouterEndToEnd:
    """aggregate→notify端到端验证（全mock）"""

    def test_end_to_end_with_owner_labels(self):
        """
        端到端: 带owner标签（userid+手机号）的AllureResult→
        aggregate→notify；企微payload含mentioned_list/
        mentioned_mobile_list；邮件HTML含负责人列与姓名

        参数:
            无

        返回:
            无
        """
        # 1. 构造带owner标签的失败结果
        results = [
            AllureResult(
                uuid="u-pass", name="case_pass", status="passed",
                start=100, stop=200, labels={"owner": ["zhangsan"]},
            ),
            AllureResult(
                uuid="u-fail-1", name="case_fail_mobile", status="failed",
                start=100, stop=300,
                labels={"owner": ["13800000000"], "severity": ["critical"]},
                status_details={"message": "业务码期望0实际2001"},
            ),
            AllureResult(
                uuid="u-fail-2", name="case_fail_userid", status="broken",
                start=100, stop=400,
                labels={"owner": ["zhangsan"]},
                status_details={"message": "连接超时"},
            ),
        ]
        stat = ReportStatistics.aggregate(results)
        assert stat.failed == 2
        assert {detail.owner for detail in stat.failed_details} == {
            "13800000000", "zhangsan"
        }

        # 2. 真实渠道实例（企微payload走mock requests.post，邮件走mock SMTP_SSL）
        get_patch, bool_patch = patch_env(FULL_ENV)
        with get_patch, bool_patch, \
             patch("requests.post") as mock_post, \
             patch("smtplib.SMTP_SSL") as mock_ssl:
            mock_response = MagicMock()
            mock_response.json.return_value = {"errcode": 0, "errmsg": "ok"}
            mock_post.return_value = mock_response
            mock_ssl.return_value = MagicMock()

            router = NotificationRouter(strategy="all")
            results_dict = router.notify(stat, "RUN-E2E-001")

        # 3. 双渠道结果
        assert results_dict == {"email": True, "wechat": True}

        # 4. 企微payload: mentioned字段注入（markdown正文@不生效的平台规则）
        payload = mock_post.call_args.kwargs["json"]
        assert "mentioned_list" in payload
        assert "zhangsan" in payload["mentioned_list"]
        assert "13800000000" in payload["mentioned_mobile_list"]
        assert payload["msgtype"] == "markdown"
        # markdown正文含负责人视觉行与失败摘要
        assert "负责人" in payload["markdown"]["content"]
        assert "case_fail_mobile" in payload["markdown"]["content"]

        # 5. 邮件HTML: 负责人提示行 + 5列表头 + 姓名展示
        # （MIME as_string中文为base64编码，需解析payload后再断言）
        from email.mime.multipart import MIMEMultipart
        from email.parser import Parser

        sent_message = mock_ssl.return_value.sendmail.call_args[0][2]
        parsed = Parser().parsestr(sent_message)
        body = parsed.get_payload()[0].get_payload(decode=True).decode("utf-8")
        assert "请以下负责人关注" in body
        assert "zhangsan" in body
        assert "负责人" in body
        # 失败表5列: 表头含"用例名/模块/优先级/负责人/错误信息"
        for header in ("用例名", "模块", "优先级", "负责人", "错误信息"):
            assert f">{header}</th>" in body
