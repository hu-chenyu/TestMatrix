"""
通知推送模块（第二阶段实现中）

架构设计:
    - Notification      通知消息统一数据结构（所有渠道共用）
    - BaseNotifier      通知渠道抽象基类（统一send接口+便捷构造）
    - EmailNotifier     邮件通知器（smtplib标准库实现，零第三方依赖）

渠道扩展规划:
    - Day12: 企业微信机器人webhook通知器（继承BaseNotifier）
    - Day13: 失败用例@负责人 + 分级通知策略（全量/仅失败）
    - 后续: 钉钉等渠道按需扩展，均继承BaseNotifier实现send即可

设计原则:
    - 通知失败绝不影响主流程: send()内部捕获全部异常，只返回bool
    - 配置驱动: 渠道开关与连接参数全部走env_manager（TM_EMAIL_*系列）
    - 端口自适应: 465走SMTP_SSL、587走STARTTLS、其余明文（仅本地测试）
"""

import smtplib
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from src.common.env_manager import env_manager
from src.common.logger import LogManager

logger = LogManager.get_logger()

# SMTP连接超时（秒）: 防止网络不通时主流程卡死
SMTP_TIMEOUT_SECONDS = 10

# SSL加密端口（SMTP_SSL直接加密连接）
SSL_PORT = 465

# STARTTLS端口（明文连接后升级TLS）
STARTTLS_PORT = 587

# HTML内容判定标签（内容包含任一即视为HTML）
HTML_TAGS = ("<html", "<body", "<table", "<div", "<p>", "<span", "<h1", "<h2")


@dataclass
class Notification:
    """
    通知消息统一数据结构

    所有通知渠道（邮件/企微/钉钉...）共用同一消息结构，
    由各渠道的send实现自行决定渲染方式。

    字段说明:
        title         通知标题（如"测试批次执行完成"）
        content       通知正文（纯文本或HTML字符串）
        level         通知级别: info / warning / critical，默认info
        execution_id  关联的执行批次号（可选）
        pass_rate     通过率（可选，供邮件模板渲染）
        total_cases   用例总数（可选）
        failed_cases  失败数（可选）
        extra         扩展字段（预留后续渠道使用，如企微@人列表）
        created_at    创建时间（默认当前时间）
    """

    title: str
    content: str
    level: str = "info"
    execution_id: Optional[str] = None
    pass_rate: Optional[float] = None
    total_cases: Optional[int] = None
    failed_cases: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)


class BaseNotifier(ABC):
    """
    通知渠道抽象基类

    定义所有通知渠道的统一接口:
        - send          发送通知（子类必须实现；失败内部捕获返回False，不抛异常）
        - is_enabled    渠道开关检查（子类按需重写，读取对应TM_*_ENABLED配置）
        - build_notification 便捷构造Notification对象

    扩展约定:
        新增渠道（企微/钉钉/...）只需继承本类并实现send方法，
        调用方通过统一接口推送，无需感知具体渠道差异。
    """

    @abstractmethod
    def send(self, notification: Notification) -> bool:
        """
        发送通知（抽象方法，子类必须实现）

        契约:
            - 发送成功返回True
            - 任何失败（配置缺失/连接异常/认证失败）内部捕获，
              记录error日志，返回False，绝不向上抛出异常
              （通知是旁路能力，失败不应影响主流程）

        参数:
            notification (Notification): 统一通知消息对象

        返回:
            bool: 发送成功True / 失败False

        异常:
            无（子类实现必须吞掉全部异常）
        """

    def is_enabled(self) -> bool:
        """
        检查通知渠道是否启用

        基类默认返回True；子类按需重写，
        通常读取对应配置开关（如TM_EMAIL_ENABLED）。

        参数:
            无

        返回:
            bool: 渠道启用返回True

        异常:
            无
        """
        return True

    @staticmethod
    def build_notification(title: str, content: str, **kwargs) -> Notification:
        """
        便捷构造Notification对象

        参数:
            title (str): 通知标题
            content (str): 通知正文
            **kwargs: 其余可选字段（level/execution_id/pass_rate/
                      total_cases/failed_cases/extra）

        返回:
            Notification: 组装好的通知消息对象

        异常:
            无
        """
        return Notification(title=title, content=content, **kwargs)


class EmailNotifier(BaseNotifier):
    """
    邮件通知器（smtplib标准库实现）

    配置项（env_manager读取，对应.env.example的TM_EMAIL_*系列）:
        TM_EMAIL_ENABLED    总开关（默认false）
        TM_EMAIL_SMTP_HOST  SMTP服务器地址
        TM_EMAIL_SMTP_PORT  端口（默认465；465=SSL / 587=STARTTLS / 其他=明文）
        TM_EMAIL_SENDER     发件人邮箱
        TM_EMAIL_PASSWORD   发件人授权码/密码
        TM_EMAIL_RECEIVERS  收件人列表（逗号分隔）

    连接策略（按端口自适应）:
        - 465: smtplib.SMTP_SSL（SSL加密直连）
        - 587: smtplib.SMTP + starttls()（明文连接后升级TLS）
        - 其他: smtplib.SMTP（明文，仅本地测试环境使用）
    """

    def __init__(self):
        """
        初始化邮件通知器（读取并解析全部邮件配置）

        参数:
            无

        返回:
            无

        异常:
            无（配置缺失不抛异常，send时校验并返回False）
        """
        self.smtp_host = str(env_manager.get("TM_EMAIL_SMTP_HOST", ""))
        self.smtp_port = env_manager.get_int("TM_EMAIL_SMTP_PORT", SSL_PORT)
        self.sender = str(env_manager.get("TM_EMAIL_SENDER", ""))
        self.password = str(env_manager.get("TM_EMAIL_PASSWORD", ""))
        self.receivers = self._get_receivers()
        logger.debug(
            f"邮件通知器初始化 | host: {self.smtp_host or '-'} | "
            f"port: {self.smtp_port} | sender: {self.sender or '-'} | "
            f"收件人: {len(self.receivers)}个"
        )

    def is_enabled(self) -> bool:
        """
        邮件渠道开关检查（重写基类方法）

        参数:
            无

        返回:
            bool: TM_EMAIL_ENABLED为true/1/yes时返回True，默认False

        异常:
            无
        """
        return env_manager.get_bool("TM_EMAIL_ENABLED", False)

    def send(self, notification: Notification) -> bool:
        """
        发送邮件通知（重写基类方法）

        执行流程:
            1. 渠道开关检查: 未启用debug日志+返回False（不连接）
            2. 必填配置校验: host/sender/password/receivers缺失则
               error日志+返回False
            3. 构造MIMEMultipart邮件（Subject/From/To/正文）
            4. 按端口自适应连接（SSL/STARTTLS/明文）+ login + sendmail
            5. 成功info日志（标题/收件人数/耗时），失败error日志，
               全部异常捕获返回False；finally确保quit关闭连接

        参数:
            notification (Notification): 统一通知消息对象

        返回:
            bool: 发送成功True / 失败False（任何异常都不向上抛出）

        异常:
            无（smtplib.SMTPException / socket.error / OSError全部内部捕获）
        """
        # 1. 渠道开关检查
        if not self.is_enabled():
            logger.debug("邮件通知未启用，跳过发送")
            return False

        # 2. 必填配置校验
        missing = self._missing_configs()
        if missing:
            logger.error(
                f"邮件配置缺失，发送中止 | 缺失项: {missing}"
            )
            return False

        # 3. 构造邮件
        message = self._build_message(notification)

        # 4. 连接发送（异常全捕获）
        import time

        start_time = time.perf_counter()
        client = None
        try:
            client = self._connect()
            client.login(self.sender, self.password)
            client.sendmail(self.sender, self.receivers, message.as_string())
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.info(
                f"邮件通知发送成功 | 标题: {notification.title} | "
                f"收件人: {len(self.receivers)}个 | 耗时: {elapsed_ms:.0f}ms"
            )
            return True
        except (smtplib.SMTPException, socket.error, OSError) as exc:
            # 认证类异常只记录sender不记录密码（敏感信息保护）
            logger.error(
                f"邮件通知发送失败 | host: {self.smtp_host}:{self.smtp_port} | "
                f"sender: {self.sender} | "
                f"异常: {type(exc).__name__}: {exc}"
            )
            return False
        finally:
            if client is not None:
                try:
                    client.quit()  # 确保连接释放，防止连接泄漏
                except smtplib.SMTPException:
                    logger.debug("SMTP连接关闭时异常（已忽略）")

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    def _missing_configs(self) -> List[str]:
        """
        校验必填配置完整性（内部方法）

        参数:
            无

        返回:
            List[str]: 缺失配置项名列表（全部齐备返回空列表）

        异常:
            无
        """
        missing = []
        if not self.smtp_host:
            missing.append("TM_EMAIL_SMTP_HOST")
        if not self.sender:
            missing.append("TM_EMAIL_SENDER")
        if not self.password:
            missing.append("TM_EMAIL_PASSWORD")
        if not self.receivers:
            missing.append("TM_EMAIL_RECEIVERS")
        return missing

    def _build_message(self, notification: Notification) -> MIMEMultipart:
        """
        构造MIME邮件对象（内部方法）

        正文content_type根据内容自动判断:
            含HTML标签（html/body/table/div等）→ html，否则plain；
            charset统一utf-8（中文无乱码）。

        参数:
            notification (Notification): 通知消息对象

        返回:
            MIMEMultipart: 组装完成的邮件对象

        异常:
            无
        """
        message = MIMEMultipart()
        message["Subject"] = notification.title
        message["From"] = self.sender
        message["To"] = ", ".join(self.receivers)

        content_type = "html" if self._is_html(notification.content) else "plain"
        message.attach(
            MIMEText(notification.content, content_type, "utf-8")
        )
        return message

    def _connect(self):
        """
        按端口自适应建立SMTP连接（内部方法）

        连接策略:
            - 465: SMTP_SSL（SSL加密直连）
            - 587: SMTP + starttls()（明文连接后升级TLS）
            - 其他: SMTP（明文，仅本地测试用）
            全部带10秒超时，防止网络不通时主流程卡死。

        参数:
            无

        返回:
            smtplib.SMTP | smtplib.SMTP_SSL: 已建立的SMTP连接

        异常:
            smtplib.SMTPException / socket.error / OSError:
            连接或TLS升级失败时向上抛出（由send统一捕获）
        """
        if self.smtp_port == SSL_PORT:
            client = smtplib.SMTP_SSL(
                self.smtp_host, self.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
            )
        elif self.smtp_port == STARTTLS_PORT:
            client = smtplib.SMTP(
                self.smtp_host, self.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
            )
            client.starttls()
        else:
            # 明文端口: 仅本地测试环境使用（如MailHog/Mailpit）
            client = smtplib.SMTP(
                self.smtp_host, self.smtp_port, timeout=SMTP_TIMEOUT_SECONDS
            )
        return client

    def _get_receivers(self) -> List[str]:
        """
        解析收件人列表（内部方法）

        参数:
            无

        返回:
            List[str]: 收件人邮箱列表（逗号分隔解析，strip去空、
                       去重保序；配置为空返回空列表）

        异常:
            无
        """
        raw = str(env_manager.get("TM_EMAIL_RECEIVERS", ""))
        receivers = [
            item.strip() for item in raw.split(",") if item.strip()
        ]
        # 去重保序（重复配置不重复投递）
        return list(dict.fromkeys(receivers))

    @staticmethod
    def _is_html(content: str) -> bool:
        """
        判断内容是否为HTML（内部方法）

        参数:
            content (str): 通知正文内容

        返回:
            bool: 内容包含HTML标签（html/body/table/div/p等）返回True

        异常:
            无
        """
        if not content:
            return False
        lowered = content.lower()
        return any(tag in lowered for tag in HTML_TAGS)
