"""
通知推送模块（第二阶段实现中）

架构设计:
    - Notification         通知消息统一数据结构（所有渠道共用）
    - BaseNotifier         通知渠道抽象基类（统一send接口+便捷构造）
    - EmailNotifier        邮件通知器（smtplib标准库实现，零第三方依赖）
    - EmailReportTemplate  HTML邮件报告模板（Day11，内联CSS汇总表格）

渠道扩展规划:
    - Day11: HTML邮件报告模板（已完成，内联CSS+模块/优先级分布+失败明细）
    - Day12: 企业微信机器人webhook通知器（继承BaseNotifier）
    - Day13: 失败用例@负责人 + 分级通知策略（全量/仅失败）
    - 后续: 钉钉等渠道按需扩展，均继承BaseNotifier实现send即可

设计原则:
    - 通知失败绝不影响主流程: send()内部捕获全部异常，只返回bool
    - 配置驱动: 渠道开关与连接参数全部走env_manager（TM_EMAIL_*系列）
    - 端口自适应: 465走SMTP_SSL、587走STARTTLS、其余明文（仅本地测试）
    - 邮件HTML全部内联CSS: Outlook/Gmail会过滤<style>标签，
      内联样式是邮件HTML的事实标准
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

        失败行背景色#fff5f5高亮；错误信息截断200字符（超出加...）。

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

        rows = "\n".join(
            f'<tr style="{FAILED_ROW_STYLE}">\n'
            f'<td style="{TD_STYLE}">{detail.name}</td>\n'
            f'<td style="{TD_STYLE}">{detail.module}</td>\n'
            f'<td style="{TD_STYLE}">{detail.priority}</td>\n'
            f'<td style="{TD_STYLE} font-size: 12px;">'
            f"{self._truncate(detail.error_message)}</td>\n"
            f"</tr>"
            for detail in failed_details
        )
        return self._section_with_table(
            "❌ 失败明细", ["用例名", "模块", "优先级", "错误信息"], rows
        )

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
