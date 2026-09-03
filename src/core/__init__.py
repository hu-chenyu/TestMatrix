"""
平台核心逻辑层

    data_driver.py     YAML/Excel数据驱动引擎（第二阶段实现）
    case_manager.py    用例调度与管理（第二阶段实现）
    report_analyzer.py 测试报告解析与统计（第二阶段实现）
    notification.py    通知推送（第二阶段实现中: 基座+邮件+HTML模板+企微）
"""

from src.core.notification import (
    BaseNotifier,
    EmailNotifier,
    EmailReportTemplate,
    Notification,
    NotificationRouter,
    WeChatNotifier,
)

__all__ = [
    "BaseNotifier",
    "EmailNotifier",
    "EmailReportTemplate",
    "Notification",
    "NotificationRouter",
    "WeChatNotifier",
]
