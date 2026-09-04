"""
通知推送模块（第二阶段实现中）

架构设计:
    - Notification         通知消息统一数据结构（所有渠道共用）
    - BaseNotifier         通知渠道抽象基类（统一send接口+便捷构造）
    - EmailNotifier        邮件通知器（smtplib标准库实现，零第三方依赖）
    - EmailReportTemplate  HTML邮件报告模板（Day11，内联CSS汇总表格）

渠道扩展规划:
    - Day11: HTML邮件报告模板（已完成，内联CSS+模块/优先级分布+失败明细）
    - Day12: 企业微信机器人webhook通知器（已完成，markdown消息）
    - Day13: 分级通知策略（全量/仅失败）+失败用例@负责人+NotificationRouter路由器（已完成）
    - Day14: 重试机制（指数退避+死信记录）
    - Day15: 与case_manager集成（批次完成自动推送）
    - 后续: 钉钉等渠道按需扩展，均继承BaseNotifier实现send即可

设计原则:
    - 通知失败绝不影响主流程: send()内部捕获全部异常，只返回bool
    - 配置驱动: 渠道开关与连接参数全部走env_manager（TM_EMAIL_*系列）
    - 端口自适应: 465走SMTP_SSL、587走STARTTLS、其余明文（仅本地测试）
    - 邮件HTML全部内联CSS: Outlook/Gmail会过滤<style>标签，
      内联样式是邮件HTML的事实标准
"""

import random
import re
import smtplib
import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

import requests

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

# ============ HTML邮件模板常量 ============
# 通过率颜色阈值（≥0.9绿 / ≥0.7橙 / <0.7红 / 无数据灰）
COLOR_GREEN = "#28a745"
COLOR_ORANGE = "#ffc107"
COLOR_RED = "#dc3545"
COLOR_GRAY = "#6c757d"

# 通用样式常量（内联CSS，邮件客户端兼容）
FONT_FAMILY = (
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif'
)
TABLE_STYLE = "border-collapse: collapse; width: 100%; margin: 8px 0;"
TH_STYLE = (
    "background-color: #f8f9fa; border: 1px solid #dee2e6; "
    "padding: 8px 12px; text-align: left;"
)
TD_STYLE = "border: 1px solid #dee2e6; padding: 8px 12px;"
CARD_STYLE = (
    "display: inline-block; width: 150px; text-align: center; "
    "border: 1px solid #dee2e6; border-radius: 8px; "
    "padding: 12px; margin: 4px; vertical-align: top;"
)
FAILED_ROW_STYLE = "background-color: #fff5f5;"

# 错误信息截断长度（字符）
ERROR_MESSAGE_MAX_LENGTH = 200

# ============ 通知重试常量（Day14） ============
# 失败后最大重试次数（首次尝试不计，总尝试=1+max_retries）
DEFAULT_MAX_RETRIES = 3

# 指数退避基准秒数（第k次失败后等待 base×2^(k-1)，序列1/2/4/8...）
DEFAULT_BASE_DELAY = 1.0

# 死信fail_reason落库截断长度（字符）
REASON_MAX_LEN = 1000


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

    # 渠道名标识（子类按渠道覆写；NotificationRouter用其作为结果字典的键）
    channel_name = "base"

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

    # 渠道名标识（NotificationRouter结果字典的键）
    channel_name = "email"

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


# ======================================================================
# HTML邮件报告模板（Day11）
# ======================================================================
class EmailReportTemplate:
    """
    HTML邮件报告模板

    根据批次统计数据（StatisticsResult）生成带内联CSS的HTML邮件内容，
    产出字符串可直接作为Notification.content传给EmailNotifier发送。

    设计说明:
        - 纯字符串拼接，零第三方模板引擎（jinja2等），零额外依赖
        - 全部内联CSS（style属性），不用<style>标签——Outlook/Gmail
          等邮件客户端会过滤<style>，内联样式是邮件HTML事实标准
        - 颜色语义化: 通过率≥90%绿 / ≥70%橙 / <70%红 / 无数据灰
        - 模块表格按通过率升序（低的排前，优先暴露风险模块）

    报告结构（6个区块）:
        1. 标题区: 批次号+通过率大字（颜色随通过率）
        2. 汇总指标卡片: 总数/通过/失败/错误/跳过/通过率
        3. 耗时统计区: 总耗时/平均/P95/最快/最慢
        4. 模块分布表格（通过率升序）
        5. 优先级分布表格
        6. 失败用例明细表格（错误信息截断200字符）
    """

    def render(
        self,
        stat: "StatisticsResult",
        execution_id: str = "",
        failed_details: Optional[List["FailedCaseDetail"]] = None,
    ) -> str:
        """
        生成完整HTML邮件报告

        参数:
            stat (StatisticsResult): 批次级统计结果（report_analyzer产出）
            execution_id (str): 执行批次号（标题区展示），默认空串
            failed_details (List[FailedCaseDetail] | None): 失败明细，
                None时使用stat.failed_details，传列表则覆盖（便于测试）

        返回:
            str: 完整HTML字符串（含DOCTYPE/html/head/body，可直接发送）

        异常:
            ValueError: stat为None时抛出
        """
        if stat is None:
            raise ValueError("统计结果不能为空")

        # failed_details覆盖逻辑: 传None用stat自带，传列表用传入值
        details = (
            stat.failed_details if failed_details is None else failed_details
        )

        sections = [
            self._render_header(stat, execution_id),
            self._render_summary_cards(stat),
            self._render_duration(stat),
            self._render_module_table(stat.by_module),
            self._render_priority_table(stat.by_priority),
            self._render_failed_table(details),
        ]
        body = "\n".join(sections)
        return (
            '<!DOCTYPE html>\n<html>\n<head>\n'
            '<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            f"<title>测试报告 {execution_id}</title>\n</head>\n"
            f'<body style="margin: 0; padding: 16px; '
            f"background-color: #f4f5f7; font-family: {FONT_FAMILY};\">\n"
            f'<div style="max-width: 600px; margin: 0 auto; '
            f'background-color: #ffffff; border-radius: 12px; '
            f'padding: 24px;">\n{body}\n</div>\n</body>\n</html>'
        )

    # ------------------------------------------------------------------
    # 区块渲染方法
    # ------------------------------------------------------------------
    def _render_header(self, stat: "StatisticsResult", execution_id: str) -> str:
        """
        渲染标题区（内部方法）

        参数:
            stat (StatisticsResult): 批次级统计结果
            execution_id (str): 执行批次号

        返回:
            str: 标题区HTML片段（批次号+通过率大字，颜色随通过率）

        异常:
            无
        """
        color = self._get_pass_rate_color(stat.pass_rate, stat.total)
        rate_text = self._format_pass_rate(stat.pass_rate, stat.total)
        batch_text = f"批次 {execution_id}" if execution_id else "测试执行"
        return (
            f'<div style="text-align: center; padding: 8px 0 16px 0;">\n'
            f'<div style="color: {COLOR_GRAY}; font-size: 14px;">{batch_text}</div>\n'
            f'<div style="font-size: 24px; font-weight: bold; '
            f'color: {color};">通过率 {rate_text}</div>\n'
            f"</div>"
        )

    def _render_summary_cards(self, stat: "StatisticsResult") -> str:
        """
        渲染汇总指标卡片（内部方法）

        6个卡片横向排列（inline-block，移动端自动换行）:
        总数/通过/失败/错误/跳过/通过率

        参数:
            stat (StatisticsResult): 批次级统计结果

        返回:
            str: 卡片区HTML片段

        异常:
            无
        """
        cards = [
            ("用例总数", str(stat.total)),
            ("通过", str(stat.passed)),
            ("失败", str(stat.failed - stat.broken)),
            ("错误", str(stat.broken)),
            ("跳过", str(stat.skipped)),
            ("通过率", self._format_pass_rate(stat.pass_rate, stat.total)),
        ]
        card_html = "\n".join(
            f'<div style="{CARD_STYLE}">\n'
            f'<div style="font-size: 12px; color: {COLOR_GRAY};">{label}</div>\n'
            f'<div style="font-size: 24px; font-weight: bold;">{value}</div>\n'
            f"</div>"
            for label, value in cards
        )
        return (
            f'<h3 style="margin: 16px 0 8px 0; font-size: 16px;">'
            f"📊 执行汇总</h3>\n{card_html}"
        )

    def _render_duration(self, stat: "StatisticsResult") -> str:
        """
        渲染耗时统计区（内部方法）

        参数:
            stat (StatisticsResult): 批次级统计结果

        返回:
            str: 耗时区HTML片段（总耗时/平均/P95/最快/最慢）

        异常:
            无
        """
        rows = [
            ("总耗时", self._format_duration(stat.total_duration_ms)),
            ("平均耗时", self._format_ms(stat.avg_duration_ms)),
            ("P95耗时", self._format_ms(stat.p95_duration_ms)),
            ("最快", self._format_duration(stat.min_duration_ms)),
            ("最慢", self._format_duration(stat.max_duration_ms)),
        ]
        cells = "\n".join(
            f'<td style="{TD_STYLE} text-align: center;">'
            f'<div style="font-size: 12px; color: {COLOR_GRAY};">{label}</div>'
            f'<div style="font-size: 16px; font-weight: bold;">{value}</div>'
            f"</td>"
            for label, value in rows
        )
        return (
            f'<h3 style="margin: 16px 0 8px 0; font-size: 16px;">'
            f"⏱ 耗时统计</h3>\n"
            f'<table style="{TABLE_STYLE}">\n<tr>\n{cells}\n</tr>\n</table>'
        )

    def _render_module_table(self, by_module: "Dict[str, Any]") -> str:
        """
        渲染模块分布表格（内部方法）

        按通过率升序排列（通过率低的模块排前面，优先暴露风险）。

        参数:
            by_module (Dict[str, ModuleStat]): 模块分组统计

        返回:
            str: 模块分布表格HTML片段；无数据时显示"无模块数据"

        异常:
            无
        """
        if not by_module:
            return self._empty_section("模块分布", "无模块数据")

        # 通过率升序: 风险模块排前
        sorted_modules = sorted(
            by_module.values(), key=lambda m: m.pass_rate
        )
        rows = "\n".join(
            f'<tr>\n'
            f'<td style="{TD_STYLE}">{module.name}</td>\n'
            f'<td style="{TD_STYLE}">{module.total}</td>\n'
            f'<td style="{TD_STYLE}">{module.passed}</td>\n'
            f'<td style="{TD_STYLE}">{module.failed}</td>\n'
            f'<td style="{TD_STYLE} color: '
            f'{self._get_pass_rate_color(module.pass_rate)};">'
            f"{module.pass_rate * 100:.2f}%</td>\n"
            f"</tr>"
            for module in sorted_modules
        )
        return self._section_with_table(
            "📦 模块分布", ["模块", "总数", "通过", "失败", "通过率"], rows
        )

    def _render_priority_table(self, by_priority: "Dict[str, Any]") -> str:
        """
        渲染优先级分布表格（内部方法）

        参数:
            by_priority (Dict[str, PriorityStat]): 优先级分组统计

        返回:
            str: 优先级分布表格HTML片段；无数据时显示"无优先级数据"

        异常:
            无
        """
        if not by_priority:
            return self._empty_section("优先级分布", "无优先级数据")

        rows = "\n".join(
            f'<tr>\n'
            f'<td style="{TD_STYLE}">{priority.name}</td>\n'
            f'<td style="{TD_STYLE}">{priority.total}</td>\n'
            f'<td style="{TD_STYLE}">{priority.passed}</td>\n'
            f'<td style="{TD_STYLE}">{priority.failed}</td>\n'
            f'<td style="{TD_STYLE} color: '
            f'{self._get_pass_rate_color(priority.pass_rate)};">'
            f"{priority.pass_rate * 100:.2f}%</td>\n"
            f"</tr>"
            for priority in by_priority.values()
        )
        return self._section_with_table(
            "🎯 优先级分布", ["优先级", "总数", "通过", "失败", "通过率"], rows
        )

    def _render_failed_table(self, failed_details: List["FailedCaseDetail"]) -> str:
        """
        渲染失败用例明细表格（内部方法）

        失败行背景色#fff5f5高亮；错误信息截断200字符（超出加...）；
        表头含负责人列（owner为空显示"-"）。

        参数:
            failed_details (List[FailedCaseDetail]): 失败明细列表

        返回:
            str: 失败明细表格HTML片段；空列表显示"🎉 本次执行无失败用例"

        异常:
            无
        """
        if not failed_details:
            return (
                f'<h3 style="margin: 16px 0 8px 0; font-size: 16px;">'
                f"❌ 失败明细</h3>\n"
                f'<div style="padding: 16px; text-align: center; '
                f'color: {COLOR_GREEN};">🎉 本次执行无失败用例</div>'
            )

        # 负责人提示行: 存在负责人时在明细表上方输出（邮件无@能力，靠视觉提示）
        owner_names = list(
            dict.fromkeys(
                detail.owner for detail in failed_details if detail.owner
            )
        )
        owner_section = ""
        if owner_names:
            owner_section = (
                f'<div style="padding: 8px 0; color: {COLOR_RED}; '
                f'font-weight: bold;">请以下负责人关注：'
                f"{'、'.join(owner_names)}</div>\n"
            )

        rows = "\n".join(
            f'<tr style="{FAILED_ROW_STYLE}">\n'
            f'<td style="{TD_STYLE}">{detail.name}</td>\n'
            f'<td style="{TD_STYLE}">{detail.module}</td>\n'
            f'<td style="{TD_STYLE}">{detail.priority}</td>\n'
            f'<td style="{TD_STYLE}">{detail.owner or "-"}</td>\n'
            f'<td style="{TD_STYLE} font-size: 12px;">'
            f"{self._truncate(detail.error_message)}</td>\n"
            f"</tr>"
            for detail in failed_details
        )
        table = self._section_with_table(
            "❌ 失败明细",
            ["用例名", "模块", "优先级", "负责人", "错误信息"],
            rows,
        )
        return owner_section + table

    # ------------------------------------------------------------------
    # 格式化与辅助方法
    # ------------------------------------------------------------------
    @staticmethod
    def _get_pass_rate_color(
        pass_rate: Optional[float], total: int = -1
    ) -> str:
        """
        根据通过率返回颜色值（内部方法）

        参数:
            pass_rate (float | None): 通过率（0.0-1.0），None/负数视为无数据
            total (int): 用例总数；0时通过率无意义视为无数据（默认-1不校验）

        返回:
            str: 颜色值（≥0.9绿#28a745 / ≥0.7橙#ffc107 /
                <0.7红#dc3545 / 无数据灰#6c757d）

        异常:
            无
        """
        if pass_rate is None or pass_rate < 0 or total == 0:
            return COLOR_GRAY
        if pass_rate >= 0.9:
            return COLOR_GREEN
        if pass_rate >= 0.7:
            return COLOR_ORANGE
        return COLOR_RED

    @staticmethod
    def _format_pass_rate(
        pass_rate: Optional[float], total: int = -1
    ) -> str:
        """
        格式化通过率为百分比文本（内部方法）

        参数:
            pass_rate (float | None): 通过率；None/负数/total=0场景
            total (int): 用例总数；0时通过率无意义显示N/A（默认-1不校验）

        返回:
            str: "95.00%"格式；无效值返回"N/A"

        异常:
            无
        """
        if pass_rate is None or pass_rate < 0 or total == 0:
            return "N/A"
        return f"{pass_rate * 100:.2f}%"

    @staticmethod
    def _format_duration(ms: int) -> str:
        """
        毫秒转可读格式（内部方法）

        参数:
            ms (int): 毫秒耗时值

        返回:
            str: <1000ms显示"xxx ms"，≥1000ms显示"x.xx s"

        异常:
            无
        """
        if ms is None:
            return "N/A"
        if ms < 1000:
            return f"{ms} ms"
        return f"{ms / 1000:.2f} s"

    @staticmethod
    def _format_ms(ms: float) -> str:
        """
        浮点毫秒转可读格式（内部方法，avg/p95等浮点指标用）

        参数:
            ms (float): 毫秒耗时值（浮点）

        返回:
            str: <1000显示"xx.xx ms"（保留2位小数），≥1000显示"x.xx s"

        异常:
            无
        """
        if ms is None:
            return "N/A"
        if ms < 1000:
            return f"{ms:.2f} ms"
        return f"{ms / 1000:.2f} s"

    @staticmethod
    def _truncate(message: str, max_length: int = ERROR_MESSAGE_MAX_LENGTH) -> str:
        """
        截断超长错误信息（内部方法）

        参数:
            message (str): 原始错误信息
            max_length (int): 最大长度，默认200字符

        返回:
            str: 超长截断并以"..."结尾；未超长原样返回

        异常:
            无
        """
        if not message:
            return ""
        if len(message) <= max_length:
            return message
        return message[:max_length] + "..."

    def _section_with_table(self, title: str, headers: List[str], rows: str) -> str:
        """
        组装"标题+表格"区块（内部方法）

        参数:
            title (str): 区块标题
            headers (List[str]): 表头列表
            rows (str): 已拼好的数据行HTML

        返回:
            str: 完整区块HTML片段

        异常:
            无
        """
        header_cells = "\n".join(
            f'<th style="{TH_STYLE}">{head}</th>' for head in headers
        )
        return (
            f'<h3 style="margin: 16px 0 8px 0; font-size: 16px;">{title}</h3>\n'
            f'<table style="{TABLE_STYLE}">\n'
            f"<thead>\n<tr>\n{header_cells}\n</tr>\n</thead>\n"
            f"<tbody>\n{rows}\n</tbody>\n</table>"
        )

    @staticmethod
    def _empty_section(title: str, empty_text: str) -> str:
        """
        组装空数据区块（内部方法）

        参数:
            title (str): 区块标题
            empty_text (str): 空数据提示文本

        返回:
            str: 标题+灰色提示的HTML片段

        异常:
            无
        """
        return (
            f'<h3 style="margin: 16px 0 8px 0; font-size: 16px;">{title}</h3>\n'
            f'<div style="padding: 12px; text-align: center; '
            f'color: {COLOR_GRAY};">{empty_text}</div>'
        )


# ======================================================================
# 企业微信机器人webhook通知（Day12）
# ======================================================================
class WeChatNotifier(BaseNotifier):
    """
    企业微信机器人webhook通知器

    通过群机器人webhook发送markdown消息，
    继承BaseNotifier统一send接口（调用方无需感知渠道差异）。

    配置项（env_manager读取，对应.env.example的TM_WECHAT_*系列）:
        TM_WECHAT_ENABLED     总开关（默认false）
        TM_WECHAT_WEBHOOK_URL 机器人webhook完整URL（含key参数）

    API约定:
        POST {webhook_url}，body为
        {"msgtype": "markdown", "markdown": {"content": "..."}}
        响应 {"errcode": 0, "errmsg": "ok"} 表示成功。

    安全设计:
        - webhook URL含key属敏感信息，日志只打印前30字符脱敏
        - send()捕获全部异常返回bool，绝不影响主流程
    """

    # 渠道名标识（NotificationRouter结果字典的键）
    channel_name = "wechat"

    # webhook请求超时（秒）
    WEBHOOK_TIMEOUT_SECONDS = 10

    # 通知级别对应的emoji标记
    LEVEL_ICONS = {"critical": "⚠️", "warning": "🔔", "info": "📋"}

    def __init__(self):
        """
        初始化企微通知器（读取并校验webhook配置）

        参数:
            无

        返回:
            无

        异常:
            无（配置缺失/格式非法不抛异常，send时校验返回False）
        """
        self.webhook_url = str(env_manager.get("TM_WECHAT_WEBHOOK_URL", ""))
        if not self._validate_webhook_url(self.webhook_url):
            if self.webhook_url:
                logger.warning(
                    f"企微webhook URL格式非法 | "
                    f"{self._mask_url(self.webhook_url)}"
                )
            self.webhook_url = ""
        logger.debug(
            f"企微通知器初始化 | webhook: {self._mask_url(self.webhook_url) or '-'}"
        )

    def is_enabled(self) -> bool:
        """
        企微渠道开关检查（重写基类方法）

        参数:
            无

        返回:
            bool: TM_WECHAT_ENABLED为true/1/yes时返回True，默认False

        异常:
            无
        """
        return env_manager.get_bool("TM_WECHAT_ENABLED", False)

    def send(self, notification: Notification) -> bool:
        """
        发送企微webhook通知（重写基类方法）

        执行流程:
            1. 渠道开关检查: 未启用debug日志+返回False（不发请求）
            2. webhook URL校验: 缺失或格式非法error日志+返回False
            3. 生成markdown内容并构造payload
            4. requests.post发送（10秒超时）
            5. 解析响应: errcode==0成功；业务失败/响应异常/网络异常
               全部error日志+返回False

        参数:
            notification (Notification): 统一通知消息对象

        返回:
            bool: 发送成功True / 失败False（任何异常都不向上抛出）

        异常:
            无（requests.exceptions.RequestException全部内部捕获）
        """
        # 1. 渠道开关检查
        if not self.is_enabled():
            logger.debug("企微通知未启用，跳过发送")
            return False

        # 2. webhook URL校验
        if not self.webhook_url:
            logger.error("企微webhook URL未配置，发送中止")
            return False
        if not self._validate_webhook_url(self.webhook_url):
            logger.error(
                f"企微webhook URL格式非法 | "
                f"{self._mask_url(self.webhook_url)}"
            )
            return False

        # 3. 构造payload（@负责人: 企微markdown正文写<@xxx>不触发提醒，
        #    必须在payload顶层注入mentioned_list/mentioned_mobile_list）
        markdown_content = self._build_markdown_content(notification)
        payload: Dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {"content": markdown_content},
        }
        mentioned_list = list(notification.extra.get("mentioned_list", []) or [])
        mentioned_mobile_list = list(
            notification.extra.get("mentioned_mobile_list", []) or []
        )
        if mentioned_list:
            payload["mentioned_list"] = mentioned_list
        if mentioned_mobile_list:
            payload["mentioned_mobile_list"] = mentioned_mobile_list

        # 4. 发送请求（异常全捕获）
        start_time = time.perf_counter()
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=self.WEBHOOK_TIMEOUT_SECONDS,
            )
        except requests.exceptions.RequestException as exc:
            # 网络层异常: ConnectionError/Timeout等全部子类
            logger.error(
                f"企微通知网络异常 | {type(exc).__name__}: {exc}"
            )
            return False

        # 5. 解析响应
        try:
            result = response.json()
        except ValueError:
            # 响应非JSON格式
            logger.error(
                f"企微通知响应解析失败 | 状态码: {response.status_code} | "
                f"响应: {response.text[:100]}"
            )
            return False

        if result.get("errcode") != 0:
            logger.error(
                f"企微通知业务失败 | errcode: {result.get('errcode')} | "
                f"errmsg: {result.get('errmsg')}"
            )
            return False

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            f"企微通知发送成功 | 标题: {notification.title} | "
            f"耗时: {elapsed_ms:.0f}ms"
        )
        return True

    # ------------------------------------------------------------------
    # 内容转换方法
    # ------------------------------------------------------------------
    def _build_markdown_content(self, notification: Notification) -> str:
        """
        将Notification转为企微markdown格式内容（内部方法）

        结构:
            1. 标题行: emoji + 二级标题（级别决定emoji）
            2. 空行分隔
            3. 正文（HTML自动转纯文本，纯文本/markdown原样）
            4. 批次号引用行（execution_id非空时）
            5. 通过率引用行（pass_rate非空时，颜色随通过率）

        参数:
            notification (Notification): 通知消息对象

        返回:
            str: 企微兼容的markdown字符串

        异常:
            无
        """
        icon = self.LEVEL_ICONS.get(notification.level, "📋")
        lines = [f"## {icon} {notification.title}", ""]

        body = self._convert_to_markdown(notification.content)
        if body:
            lines.append(body)
            lines.append("")

        if notification.execution_id:
            lines.append(f"> 批次号：{notification.execution_id}")

        if notification.pass_rate is not None:
            color = self._pass_rate_color(notification.pass_rate)
            lines.append(
                f'> 通过率：<font color="{color}">'
                f"{notification.pass_rate:.2%}</font>"
            )

        # 负责人视觉展示行（真正@提醒靠payload的mentioned字段，此行仅人眼可见）
        owner_names = list(notification.extra.get("owner_names", []) or [])
        if owner_names:
            lines.append(f"> 负责人：{'、'.join(owner_names)}")

        return "\n".join(lines).strip()

    @staticmethod
    def _convert_to_markdown(content: str) -> str:
        """
        通知正文转企微兼容文本（内部方法）

        转换规则:
            - 空内容返回空串
            - 含HTML标签: 正则去除全部标签+还原常见HTML实体+
              合并多余空行（连续3+换行压成2个）
            - 不含HTML标签: 原样返回（视为纯文本或markdown）

        参数:
            content (str): 通知正文

        返回:
            str: 企微可展示的文本内容

        异常:
            无
        """
        if not content:
            return ""
        if not any(tag in content.lower() for tag in HTML_TAGS):
            return content

        # HTML转纯文本: 去标签+还原实体+压缩空行
        text = re.sub(r"<[^>]+>", "", content)
        text = text.replace("&nbsp;", " ")
        text = text.replace("&amp;", "&")
        text = text.replace("&lt;", "<")
        text = text.replace("&gt;", ">")
        text = text.replace("&quot;", '"')
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # ------------------------------------------------------------------
    # 校验与辅助方法
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_webhook_url(url: str) -> bool:
        """
        校验webhook URL格式（内部方法）

        参数:
            url (str): webhook完整URL

        返回:
            bool: 非空且以http://或https://开头返回True；
                  仅做基础格式校验，不验证URL真实可用性
                  （发送时自然会失败并被捕获）

        异常:
            无
        """
        return bool(url) and url.startswith(("http://", "https://"))

    @staticmethod
    def _mask_url(url: str) -> str:
        """
        webhook URL脱敏显示（内部方法，日志安全）

        参数:
            url (str): 原始URL（含key，敏感）

        返回:
            str: 前30字符+"..."；URL本身不超30字符时原样返回

        异常:
            无
        """
        if not url:
            return ""
        return url[:30] + "..." if len(url) > 30 else url

    @staticmethod
    def _pass_rate_color(pass_rate: Optional[float]) -> str:
        """
        通过率映射企微font颜色（内部方法）

        企微markdown仅支持info(绿)/comment(灰)/warning(橙)三色，
        无红色——低于0.7用warning（橙色警示）表达需关注。

        参数:
            pass_rate (float | None): 通过率

        返回:
            str: "info"（≥0.9绿）/ "warning"（≥0.7橙或<0.7警示橙）/
                "comment"（无效值灰）

        异常:
            无
        """
        if pass_rate is None or pass_rate < 0:
            return "comment"
        if pass_rate >= 0.9:
            return "info"
        return "warning"


# ======================================================================
# 通知路由器（Day13）
# ======================================================================
class NotificationRouter:
    """
    通知路由器

    输入 StatisticsResult + execution_id，决策"要不要发、发什么、@谁、
    走哪几个渠道"，并汇总各渠道发送结果。是通知体系的统一入口，
    调用方（case_manager等）只与本类交互，不直接触碰具体渠道。

    决策维度:
        - 策略（all/failed_only）: 控制是否发送
        - 负责人收集: failed_details的owner标签 → 企微@名单/邮件提示行
        - 渠道路由: 显式传入notifiers > 按配置默认实例化邮件+企微

    配置项（env_manager）:
        TM_NOTIFY_STRATEGY          通知策略 all/failed_only（默认all）
        TM_NOTIFY_AT_ALL            存在失败时是否@所有人（默认false）
        TM_NOTIFY_OWNER_MOBILES     额外@的手机号列表（逗号分隔，可选）
        TM_NOTIFY_MAX_RETRIES       失败后最大重试次数（默认3，总尝试=1+次数）
        TM_NOTIFY_RETRY_BASE_DELAY  指数退避基准秒数（默认1.0，序列base×2^k）
        TM_NOTIFY_RETRY_JITTER      是否加随机抖动防雪崩（默认false）
    """

    # 合法策略值
    VALID_STRATEGIES = ("all", "failed_only")

    # 手机号正则（1开头11位，用于区分企微userid与手机号@人）
    MOBILE_PATTERN = re.compile(r"^1\d{10}$")

    def __init__(
        self,
        strategy: Optional[str] = None,
        notifiers: Optional[List[BaseNotifier]] = None,
        max_retries: Optional[int] = None,
        base_delay: Optional[float] = None,
        use_jitter: Optional[bool] = None,
        dead_letter_repo=None,
        sleeper=None,
    ):
        """
        初始化通知路由器

        参数:
            strategy (str | None): 通知策略（all/failed_only）；
                None时读env TM_NOTIFY_STRATEGY，默认all，非法值warning降级all
            notifiers (List[BaseNotifier] | None): 通知渠道列表；
                None时默认实例化 [EmailNotifier(), WeChatNotifier()]
                （未启用渠道由send内部跳过，router不重复判断开关）
            max_retries (int | None): 失败后最大重试次数（首次不计）；
                None时读env TM_NOTIFY_MAX_RETRIES，默认3；非法值按0处理并warning
            base_delay (float | None): 指数退避基准秒数；
                None时读env TM_NOTIFY_RETRY_BASE_DELAY，默认1.0
            use_jitter (bool | None): 重试等待是否加随机抖动；
                None时读env TM_NOTIFY_RETRY_JITTER，默认False（保证测试确定性）
            dead_letter_repo: 死信仓储（默认NotificationDeadLetterRepository；
                测试可注入fake）
            sleeper: 等待函数（默认time.sleep；测试注入记录型fake禁止真实等待）

        返回:
            无

        异常:
            无
        """
        # 策略: 显式传参 > env配置 > 默认all；非法值warning降级all
        if strategy is None:
            strategy = str(env_manager.get("TM_NOTIFY_STRATEGY", "all"))
        if strategy not in self.VALID_STRATEGIES:
            logger.warning(
                f"通知策略非法: {strategy!r}，降级为all | "
                f"合法值: {list(self.VALID_STRATEGIES)}"
            )
            strategy = "all"
        self.strategy = strategy

        # 渠道: 显式传入 > 默认按配置实例化
        if notifiers is None:
            notifiers = [EmailNotifier(), WeChatNotifier()]
        self.notifiers = notifiers

        # 重试参数: 显式传参 > env配置 > 默认值
        if max_retries is None:
            max_retries = env_manager.get_int(
                "TM_NOTIFY_MAX_RETRIES", DEFAULT_MAX_RETRIES
            )
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) \
                or max_retries < 0:
            logger.warning(f"重试次数非法: {max_retries!r}，按0处理")
            max_retries = 0
        self.max_retries = max_retries

        if base_delay is None:
            base_delay = env_manager.get_float(
                "TM_NOTIFY_RETRY_BASE_DELAY", DEFAULT_BASE_DELAY
            )
        self.base_delay = float(base_delay)

        if use_jitter is None:
            use_jitter = env_manager.get_bool(
                "TM_NOTIFY_RETRY_JITTER", False
            )
        self.use_jitter = use_jitter

        # 死信仓储与等待函数: 构造注入（测试可替换，禁止测试真实sleep）
        self.dead_letter_repo = (
            dead_letter_repo if dead_letter_repo is not None
            else NotificationDeadLetterRepository()
        )
        self.sleeper = sleeper if sleeper is not None else time.sleep

        logger.debug(
            f"通知路由器初始化 | 策略: {self.strategy} | "
            f"渠道: {[n.channel_name for n in self.notifiers]} | "
            f"重试: max={self.max_retries} base={self.base_delay}s "
            f"jitter={self.use_jitter}"
        )

    def should_notify(self, stat, strategy: Optional[str] = None) -> bool:
        """
        判断是否需要发送通知

        参数:
            stat (StatisticsResult): 批次级统计结果
            strategy (str | None): 覆盖策略（None用实例策略）

        返回:
            bool: all策略总是True；failed_only策略仅当
                  stat.failed>0（failed+broken合计口径）为True

        异常:
            无
        """
        effective = strategy if strategy is not None else self.strategy
        if effective == "failed_only":
            return stat.failed > 0
        return True

    def collect_owners(self, stat) -> Tuple[List[str], List[str], List[str]]:
        """
        收集失败用例负责人并分流@名单（内部含配置合并）

        分流规则:
            - owner_names: 去空/去重/保序的展示名单
            - 匹配手机号正则（1开头11位）→ mentioned_mobile_list（企微手机号@）
            - 其余视为企微userid → mentioned_list
            - TM_NOTIFY_AT_ALL=true时mentioned_list首位插入"@all"
            - TM_NOTIFY_OWNER_MOBILES（逗号分隔）合并进手机号名单（去重保序）

        参数:
            stat (StatisticsResult): 批次级统计结果

        返回:
            Tuple[List[str], List[str], List[str]]:
            (owner_names, mentioned_list, mentioned_mobile_list)

        异常:
            无
        """
        # 展示名单: 去空去重保序
        owner_names = list(
            dict.fromkeys(
                detail.owner
                for detail in stat.failed_details
                if detail.owner
            )
        )

        # 手机号/_userid分流
        mentioned_list: List[str] = []
        mentioned_mobile_list: List[str] = []
        for owner in owner_names:
            if self.MOBILE_PATTERN.match(owner):
                mentioned_mobile_list.append(owner)
            else:
                mentioned_list.append(owner)

        # @所有人开关
        if env_manager.get_bool("TM_NOTIFY_AT_ALL", False):
            mentioned_list.insert(0, "@all")

        # 额外手机号配置合并（去重保序）
        raw_mobiles = str(env_manager.get("TM_NOTIFY_OWNER_MOBILES", ""))
        for mobile in raw_mobiles.split(","):
            mobile = mobile.strip()
            if mobile and mobile not in mentioned_mobile_list:
                mentioned_mobile_list.append(mobile)

        return owner_names, mentioned_list, mentioned_mobile_list

    def notify(self, stat, execution_id: str, strategy: Optional[str] = None) -> Dict[str, bool]:
        """
        通知分发主入口

        执行流程:
            1. should_notify判定: False时返回空dict且不触碰任何渠道
            2. 收集负责人（owner_names/@名单）
            3. 构造各渠道Notification（邮件HTML/企微markdown摘要）
            4. 逐渠道send，汇总 {"渠道名": True/False}
            5. 单渠道异常（契约上不抛，防御性try）不影响其他渠道

        参数:
            stat (StatisticsResult): 批次级统计结果
            execution_id (str): 执行批次号
            strategy (str | None): 覆盖策略（None用实例策略）

        返回:
            Dict[str, bool]: 各渠道发送结果；策略跳过时返回空dict

        异常:
            无（全部内部消化）
        """
        if not self.should_notify(stat, strategy):
            logger.info(
                f"全通过且策略为仅失败，跳过通知 | 批次: {execution_id}"
            )
            return {}

        owner_names, mentioned_list, mentioned_mobile_list = (
            self.collect_owners(stat)
        )
        notifications = self._build_channel_notifications(
            stat, execution_id, owner_names
        )
        # extra统一注入@名单（渠道自行决定消费方式）
        for notification in notifications.values():
            notification.extra["mentioned_list"] = mentioned_list
            notification.extra["mentioned_mobile_list"] = mentioned_mobile_list
            notification.extra["owner_names"] = owner_names

        results: Dict[str, bool] = {}
        for notifier in self.notifiers:
            channel = notifier.channel_name
            try:
                # 重试由路由层统一管理（BaseNotifier.send保持单次尝试语义）
                success, attempts, fail_reason = self._send_with_retry(
                    notifier, notifications[channel]
                )
                results[channel] = success
                if success:
                    logger.debug(
                        f"渠道通知完成 | 渠道: {channel} | 尝试: {attempts}次 | "
                        f"批次: {execution_id}"
                    )
                elif fail_reason != "渠道未启用":
                    # 配置性跳过（未启用）不是发送失败，不写死信
                    logger.warning(
                        f"渠道通知重试耗尽 | 渠道: {channel} | "
                        f"尝试: {attempts}次 | 原因: {fail_reason} | "
                        f"批次: {execution_id}"
                    )
                    self._save_dead_letter(
                        notifications[channel], channel,
                        fail_reason, attempts,
                    )
                else:
                    logger.debug(
                        f"渠道未启用已跳过 | 渠道: {channel} | 批次: {execution_id}"
                    )
            except Exception as exc:  # 防御性捕获: 渠道契约不抛，万一抛也不影响其他渠道
                logger.warning(
                    f"渠道通知异常已捕获 | 渠道: {channel} | "
                    f"{type(exc).__name__}: {exc}"
                )
                results[channel] = False

        logger.info(
            f"通知路由完成 | 批次: {execution_id} | 策略: "
            f"{strategy or self.strategy} | 负责人: {owner_names or '-'} | "
            f"结果: {results}"
        )
        return results

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------
    def _send_with_retry(
        self, notifier: BaseNotifier, notification: Notification
    ) -> Tuple[bool, int, str]:
        """
        带指数退避重试的渠道发送（内部方法）

        重试策略:
            - send返回False或抛异常均视为一次失败并重试
              （异常类型+消息进fail_reason；契约上send不抛，此处防御性兜底）
            - is_enabled()=False为配置性跳过: 不重试、不等待，直接返回
            - 第k次失败后等待 base_delay×2^(k-1)（序列1/2/4/8...）；
              use_jitter为真时再加random.uniform(0, delay×0.25)抖动
              （防多实例同时重试雪崩，默认关闭保证测试确定性）
            - 总尝试次数上限 = 1 + max_retries

        参数:
            notifier (BaseNotifier): 通知渠道实例
            notification (Notification): 通知消息

        返回:
            Tuple[bool, int, str]: (最终是否成功, 总尝试次数, 最后失败原因)

        异常:
            无（全部内部消化）
        """
        # 配置性跳过: 渠道未启用不是发送失败（不重试/不等待/不写死信）
        if not notifier.is_enabled():
            return False, 0, "渠道未启用"

        max_attempts = 1 + self.max_retries
        attempts = 0
        last_reason = ""
        while attempts < max_attempts:
            attempts += 1
            try:
                if notifier.send(notification):
                    return True, attempts, ""
                last_reason = "send返回False"
            except Exception as exc:  # 防御性: 契约不抛，万一是抛也算失败重试
                last_reason = f"{type(exc).__name__}: {exc}"

            # 还有下一次才计算等待（最后一次失败后不再空等）
            if attempts < max_attempts:
                delay = self.base_delay * (2 ** (attempts - 1))
                if self.use_jitter:
                    delay += random.uniform(0, delay * 0.25)
                logger.warning(
                    f"发送失败准备重试 | 渠道: {notifier.channel_name} | "
                    f"第{attempts}次失败 | 原因: {last_reason} | "
                    f"等待: {delay:.2f}s"
                )
                self.sleeper(delay)

        return False, attempts, last_reason

    def _save_dead_letter(
        self,
        notification: Notification,
        channel: str,
        fail_reason: str,
        attempts: int,
    ) -> None:
        """
        重试耗尽后写入死信留痕（内部方法）

        旁路铁律: 写死信本身失败（如DB挂了）只记error日志，
        绝不向上抛——通知已是旁路能力，死信是其旁路的旁路。

        参数:
            notification (Notification): 通知消息（title/content/level/批次号来源）
            channel (str): 渠道名
            fail_reason (str): 最后失败原因
            attempts (int): 总尝试次数

        返回:
            无

        异常:
            无（仓储异常内部消化）
        """
        try:
            self.dead_letter_repo.save_dead_letter(
                channel=channel,
                execution_id=notification.execution_id or "",
                title=notification.title,
                content=notification.content,
                level=notification.level,
                fail_reason=fail_reason,
                attempts=attempts,
            )
            logger.info(
                f"死信已入库 | 渠道: {channel} | 尝试: {attempts}次 | "
                f"批次: {notification.execution_id or '-'}"
            )
        except Exception as exc:
            logger.error(
                f"死信入库失败（已忽略，不影响主流程） | 渠道: {channel} | "
                f"{type(exc).__name__}: {exc}"
            )
    def _build_channel_notifications(
        self, stat, execution_id: str, owner_names: List[str]
    ) -> Dict[str, Notification]:
        """
        构造各渠道通知消息（内部方法）

        邮件: EmailReportTemplate渲染HTML，level按通过率映射
              （<0.7 critical / <0.9 warning / 否则 info）
        企微: 纯markdown文本摘要（总数/通过/失败/错误/跳过/通过率/P95 +
              失败用例逐条"用例名(模块,负责人): 错误信息截断"）

        参数:
            stat (StatisticsResult): 批次级统计结果
            execution_id (str): 执行批次号
            owner_names (List[str]): 负责人展示名单

        返回:
            Dict[str, Notification]: {渠道名: 通知消息}（extra由notify统一注入）

        异常:
            无
        """
        # level按通过率映射
        if stat.pass_rate is None or stat.pass_rate < 0.7:
            level = "critical"
        elif stat.pass_rate < 0.9:
            level = "warning"
        else:
            level = "info"

        common_kwargs = {
            "level": level,
            "execution_id": execution_id,
            "pass_rate": stat.pass_rate,
            "total_cases": stat.total,
            "failed_cases": stat.failed,
        }

        # 邮件: HTML完整报告
        email_content = EmailReportTemplate().render(stat, execution_id)
        email_notification = Notification(
            title=f"测试批次报告 {execution_id}",
            content=email_content,
            **common_kwargs,
        )

        # 企微: 纯markdown文本摘要（不依赖HTML）
        wechat_content = self._build_wechat_summary(
            stat, execution_id, owner_names
        )
        wechat_notification = Notification(
            title=f"测试批次摘要 {execution_id}",
            content=wechat_content,
            **common_kwargs,
        )

        return {"email": email_notification, "wechat": wechat_notification}

    @staticmethod
    def _build_wechat_summary(
        stat, execution_id: str, owner_names: List[str]
    ) -> str:
        """
        构造企微markdown文本摘要（内部方法）

        参数:
            stat (StatisticsResult): 批次级统计结果
            execution_id (str): 执行批次号
            owner_names (List[str]): 负责人展示名单

        返回:
            str: 纯文本markdown摘要（失败用例逐条含负责人）

        异常:
            无
        """
        lines = [
            f"**测试批次执行完成**（{execution_id}）",
            "",
            f"- 用例总数：{stat.total}",
            f"- 通过：{stat.passed} | 失败：{stat.failed - stat.broken} | "
            f"错误：{stat.broken} | 跳过：{stat.skipped}",
            f"- 通过率：{stat.pass_rate:.2%}" if stat.pass_rate is not None
            else "- 通过率：N/A",
            f"- P95耗时：{stat.p95_duration_ms:.2f}ms",
        ]
        if owner_names:
            lines.append(f"- 负责人：{'、'.join(owner_names)}")
        if stat.failed_details:
            lines.append("")
            lines.append("**失败用例：**")
            for detail in stat.failed_details[:10]:  # 摘要最多列10条防超长
                owner_suffix = f",{detail.owner}" if detail.owner else ""
                message = detail.error_message or "无错误信息"
                if len(message) > 50:
                    message = message[:50] + "..."
                lines.append(
                    f"- {detail.name}（{detail.module}{owner_suffix}）: {message}"
                )
            if len(stat.failed_details) > 10:
                lines.append(f"- ...等共{len(stat.failed_details)}条失败用例")
        return "\n".join(lines)


# ======================================================================
# 通知死信仓储（Day14）
# ======================================================================
class NotificationDeadLetterRepository:
    """
    通知死信数据仓储

    负责死信记录的落库与查询（重试耗尽的通知留痕），
    对齐Day8 ReportRepository的函数内延迟导入风格
    （core层不顶层import db，规避循环依赖）。
    """

    @staticmethod
    def save_dead_letter(
        channel: str,
        execution_id: str,
        title: str,
        content: str,
        level: str,
        fail_reason: str,
        attempts: int,
    ) -> int:
        """
        写入一条死信记录

        参数:
            channel (str): 渠道名（email/wechat）
            execution_id (str): 执行批次号
            title (str): 通知标题
            content (str): 完整消息体（HTML/markdown全文）
            level (str): 通知级别（info/warning/critical）
            fail_reason (str): 最后失败原因（超长截断到1000字符）
            attempts (int): 总尝试次数

        返回:
            int: 自增主键id

        异常:
            sqlalchemy.exc.SQLAlchemyError: 数据库异常时向上抛出
                （由Router._save_dead_letter捕获消化，绝不影响主流程）
        """
        # 延迟导入: 规避core与db模块循环依赖
        from src.db.db_session import DatabaseSession
        from src.db.models import NotificationDeadLetter

        record = NotificationDeadLetter(
            channel=channel,
            execution_id=execution_id,
            title=title,
            content=content,
            level=level,
            fail_reason=fail_reason[:REASON_MAX_LEN],
            attempts=attempts,
            status="dead",
        )
        with DatabaseSession.session_scope() as session:
            session.add(record)
            session.flush()
            logger.info(
                f"死信记录已写入 | id: {record.id} | 渠道: {channel} | "
                f"批次: {execution_id or '-'} | 尝试: {attempts}次"
            )
            return record.id

    @staticmethod
    def list_by_execution_id(execution_id: str) -> List[Dict[str, Any]]:
        """
        按批次号查询死信列表

        参数:
            execution_id (str): 执行批次号

        返回:
            List[Dict[str, Any]]: 该批次的死信字典列表（id升序）

        异常:
            无（查询异常由session记录后向上抛出）
        """
        # 延迟导入: 规避core与db模块循环依赖
        from src.db.db_session import DatabaseSession
        from src.db.models import NotificationDeadLetter

        session = DatabaseSession.get_session()
        try:
            records = (
                session.query(NotificationDeadLetter)
                .filter_by(execution_id=execution_id)
                .order_by(NotificationDeadLetter.id)
                .all()
            )
            return [
                NotificationDeadLetterRepository._to_dict(record)
                for record in records
            ]
        finally:
            session.close()

    @staticmethod
    def list_all(limit: int = 100) -> List[Dict[str, Any]]:
        """
        查询全部死信（最近N条）

        参数:
            limit (int): 返回条数上限，默认100

        返回:
            List[Dict[str, Any]]: 死信字典列表（id升序）

        异常:
            无
        """
        # 延迟导入: 规避core与db模块循环依赖
        from src.db.db_session import DatabaseSession
        from src.db.models import NotificationDeadLetter

        session = DatabaseSession.get_session()
        try:
            records = (
                session.query(NotificationDeadLetter)
                .order_by(NotificationDeadLetter.id)
                .limit(limit)
                .all()
            )
            return [
                NotificationDeadLetterRepository._to_dict(record)
                for record in records
            ]
        finally:
            session.close()

    @staticmethod
    def count_all() -> int:
        """
        统计死信总数

        参数:
            无

        返回:
            int: 死信记录总条数

        异常:
            无
        """
        # 延迟导入: 规避core与db模块循环依赖
        from src.db.db_session import DatabaseSession
        from src.db.models import NotificationDeadLetter

        session = DatabaseSession.get_session()
        try:
            return session.query(NotificationDeadLetter).count()
        finally:
            session.close()

    @staticmethod
    def _to_dict(record) -> Dict[str, Any]:
        """
        死信模型行转字典（内部方法）

        参数:
            record (NotificationDeadLetter): 数据库模型实例

        返回:
            Dict[str, Any]: 含全部字段的字典（created_at转ISO字符串）

        异常:
            无
        """
        return {
            "id": record.id,
            "channel": record.channel,
            "execution_id": record.execution_id,
            "title": record.title,
            "content": record.content,
            "level": record.level,
            "fail_reason": record.fail_reason,
            "attempts": record.attempts,
            "status": record.status,
            "created_at": record.created_at.isoformat()
            if record.created_at
            else "",
        }
