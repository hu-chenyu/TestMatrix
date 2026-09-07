"""
通知模块边界与异常分支补测（第二阶段Day16 review补漏）

覆盖人工走查定位的未覆盖分支:
    1. 死信fail_reason超1000字符截断（REASON_MAX_LEN）
    2. 非法max_retries（负数/bool）按0处理（bool是int子类的防御分支）
    3. 适配器批次全部用例缺失test_cases→全unknown不报错
    4. 邮件收件人配置为纯逗号/空→必填校验拦截返回False不连接

测试基建: 临时SQLite + env双方法mock（对齐既有通知测试模式）。
"""

from unittest.mock import MagicMock, patch

import allure
import pytest

from src.common.env_manager import env_manager
from src.core.notification import (
    Notification,
    NotificationDeadLetterRepository,
    NotificationRouter,
)
from src.core.case_manager import CaseManager
from src.db import models
from src.db.db_session import DatabaseSession

# 邮件env配置（收件人故意配纯逗号）
BROKEN_RECEIVERS_ENV = {
    "TM_EMAIL_ENABLED": "true",
    "TM_EMAIL_SMTP_HOST": "smtp.test.com",
    "TM_EMAIL_SMTP_PORT": "465",
    "TM_EMAIL_SENDER": "sender@test.com",
    "TM_EMAIL_PASSWORD": "auth_code",
    "TM_EMAIL_RECEIVERS": ",,, ,",
}

# 路由器env基线
ROUTER_ENV = {
    "TM_NOTIFY_STRATEGY": "all",
    "TM_NOTIFY_AT_ALL": "false",
    "TM_NOTIFY_OWNER_MOBILES": "",
}


def _mock_get(config: dict):
    """env_manager.get替身"""
    def _get(key, default=None):
        value = config.get(key)
        return value if value is not None else default
    return _get


def _mock_get_bool(config: dict):
    """env_manager.get_bool替身"""
    def _get_bool(key, default=False):
        value = config.get(key)
        if value is None:
            return default
        return str(value).strip().lower() in ("true", "1", "yes", "on")
    return _get_bool


def patch_env(config: dict):
    """双方法mock上下文（构造期+发送期全程生效）"""
    return (
        patch.object(env_manager, "get", side_effect=_mock_get(config)),
        patch.object(env_manager, "get_bool", side_effect=_mock_get_bool(config)),
    )


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """
    临时SQLite数据库fixture（对齐既有通知测试模式）

    参数:
        tmp_path (Path): 临时目录
        monkeypatch: 环境变量覆写

    返回:
        Path: 临时库文件路径
    """
    db_file = tmp_path / "test_notification_edge.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_file
    DatabaseSession.reset()
    if db_file.exists():
        db_file.unlink()


@allure.feature("通知模块")
@allure.story("边界与异常分支")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestNotificationEdgeCases:
    """通知模块边界分支补测"""

    def test_dead_letter_fail_reason_truncation(self, temp_db):
        """
        死信截断: fail_reason超1000字符截断到REASON_MAX_LEN，
        落库长度恰为1000且前缀一致

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        repo = NotificationDeadLetterRepository()
        long_reason = "X" * 1200

        record_id = repo.save_dead_letter(
            channel="email", execution_id="RUN-TRUNC",
            title="标题", content="内容", level="critical",
            fail_reason=long_reason, attempts=4,
        )
        letters = repo.list_by_execution_id("RUN-TRUNC")

        assert record_id > 0
        assert len(letters[0]["fail_reason"]) == 1000
        assert letters[0]["fail_reason"] == "X" * 1000

    def test_invalid_max_retries_treated_as_zero(self, temp_db):
        """
        非法重试次数: 负数与bool（bool是int子类）均按0处理，
        send仅尝试1次（1+0）且无等待

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        for bad_value in (-1, True):
            sleeps: list = []
            send_calls: list = []

            class Probe:
                """记录型探针渠道"""
                channel_name = "email"

                def is_enabled(self):
                    return True

                def send(self, notification):
                    send_calls.append(1)
                    return False

            get_patch, bool_patch = patch_env(ROUTER_ENV)
            with get_patch, bool_patch:
                router = NotificationRouter(
                    notifiers=[Probe()], sleeper=sleeps.append,
                    max_retries=bad_value,
                )
            assert router.max_retries == 0, f"{bad_value!r}应按0处理"

            stat = type("S", (), {
                "total": 1, "passed": 1, "failed": 0, "broken": 0,
                "skipped": 0, "pass_rate": 1.0, "failed_details": [],
            })()
            with patch.object(
                NotificationRouter, "_build_channel_notifications",
                return_value={"email": Notification(title="t", content="c")},
            ):
                result = router.notify(stat, "RUN-BAD-RETRY")

            assert result == {"email": False}
            assert len(send_calls) == 1  # 1+0次，无重试
            assert sleeps == []  # 无等待

    def test_adapter_batch_all_unknown(self, temp_db):
        """
        适配器全unknown: 批次全部用例不在test_cases中→
        by_module/by_priority全落unknown，不报错

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        with DatabaseSession.session_scope() as session:
            for index in range(3):
                session.add(models.TestExecution(
                    execution_id="RUN-UNK-001",
                    case_id=f"ORPHAN-{index}",
                    case_name=f"孤儿用例{index}",
                    result="passed", start_time=None, end_time=None,
                    duration=0.1, error_message=None,
                ))

        stat = CaseManager.build_notification_statistics("RUN-UNK-001")

        assert stat is not None
        assert stat.total == 3
        assert set(stat.by_module.keys()) == {"unknown"}
        assert set(stat.by_priority.keys()) == {"unknown"}
        assert stat.by_module["unknown"].total == 3

    def test_email_empty_receivers_blocked(self):
        """
        收件人纯逗号: 解析为空列表→必填校验拦截，
        send返回False且不建立SMTP连接

        参数:
            无

        返回:
            无
        """
        get_patch, bool_patch = patch_env(BROKEN_RECEIVERS_ENV)
        with get_patch, bool_patch, \
             patch("smtplib.SMTP_SSL") as mock_ssl:
            from src.core.notification import EmailNotifier
            notifier = EmailNotifier()

            assert notifier.receivers == []
            result = notifier.send(
                Notification(title="t", content="c")
            )

        assert result is False
        mock_ssl.assert_not_called()
