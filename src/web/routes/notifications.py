"""
通知查询 API 蓝图（Day41 前40天遗漏修复）

提供通知可观测性的两个只读查询接口，补齐"死信只能直连数据库、
成功发送无历史"的缺口：
    - GET /api/notifications/history       通知发送历史（成功/进死信），分页+筛选
    - GET /api/notifications/dead-letters  死信列表（完整消息留痕），分页+渠道筛选

设计:
    - 只读 GET，不提供重放/删除（重放为后续扩展，避免误触发重复通知）
    - 分页口径与 cases/executions 列表一致（page/page_size，上限100）
    - 仓储层返回 (items, total)，路由层只做参数解析与统一响应封装
"""

from typing import Any

from flask import Blueprint, request

from src.core.case_manager import MAX_PAGE
from src.core.notification import (
    NotificationDeadLetterRepository,
    NotificationHistoryRepository,
)
from src.web.exceptions import ValidationError
from src.web.response import success

notifications_bp = Blueprint("notifications", __name__)

DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
VALID_CHANNELS = ("email", "wechat")
VALID_HISTORY_STATUS = ("success", "dead_letter")


def _parse_pagination() -> tuple[int, int]:
    """
    从查询串解析分页参数（非法值抛 ValidationError → 400）

    page 上界与 cases/executions 统一收口到 MAX_PAGE（Day44 热修 P1-2）：
    修复前此处**只校验 page<1、没有上界**，而核心层 list_history 在 Day44
    补了 MAX_PAGE 校验却抛的是裸 `ValueError`——`ValueError` 不是
    `APIError` 子类，会落到 `@app.errorhandler(Exception)` 变成 **HTTP 500**。
    即"修复深 offset"反而把一个本该 400 的用户输入错误变成了服务端故障，
    与 P2-07 刚立下的铁律直接矛盾。本函数补上界后，越界在路由层就被拦成
    400，核心层的领域异常只是第二道防线、不再是唯一防线。

    返回:
        tuple[int, int]: (page, page_size)，1≤page≤MAX_PAGE、1≤page_size≤100

    异常:
        ValidationError: page/page_size 非正整数或超限
    """
    raw_page = request.args.get("page", str(DEFAULT_PAGE))
    raw_size = request.args.get("page_size", str(DEFAULT_PAGE_SIZE))
    try:
        page = int(raw_page)
        page_size = int(raw_size)
    except (TypeError, ValueError):
        raise ValidationError("page和page_size必须为正整数") from None
    if page < 1 or page > MAX_PAGE:
        raise ValidationError(
            f"page必须在 1 与 {MAX_PAGE} 之间"
        )
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValidationError(f"page_size须在1-{MAX_PAGE_SIZE}之间")
    return page, page_size


def _parse_optional_str(name: str, allowed: tuple[str, ...]) -> str | None:
    """
    解析可选筛选参数，空串视为不筛选；非法枚举值抛 400

    参数:
        name (str): 查询参数名
        allowed (tuple[str, ...]): 允许的取值集合

    返回:
        str | None: 合法值或 None（不筛选）

    异常:
        ValidationError: 取值不在允许集合内
    """
    value = (request.args.get(name) or "").strip()
    if not value:
        return None
    if value not in allowed:
        raise ValidationError(
            f"{name} 非法：{value!r}，允许值 {list(allowed)}"
        )
    return value


@notifications_bp.route("/history", methods=["GET"])
def list_notification_history() -> tuple[dict[str, Any], int]:
    """
    通知发送历史列表（GET /api/notifications/history）

    查询参数:
        page (int): 页码，默认1
        page_size (int): 每页条数，默认20，最大100
        channel (str): 可选，email/wechat
        status (str): 可选，success/dead_letter
        execution_id (str): 可选，按执行批次号精确筛选

    返回:
        tuple: 统一响应体 200，data 为 {items,total,page,page_size,total_pages}
    """
    page, page_size = _parse_pagination()
    channel = _parse_optional_str("channel", VALID_CHANNELS)
    status = _parse_optional_str("status", VALID_HISTORY_STATUS)
    execution_id = (request.args.get("execution_id") or "").strip() or None

    items, total = NotificationHistoryRepository().list_history(
        page=page,
        page_size=page_size,
        channel=channel,
        status=status,
        execution_id=execution_id,
    )
    total_pages = (total + page_size - 1) // page_size
    return success(
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
    )


@notifications_bp.route("/dead-letters", methods=["GET"])
def list_dead_letters() -> tuple[dict[str, Any], int]:
    """
    死信列表（GET /api/notifications/dead-letters）

    查询参数:
        page (int): 页码，默认1
        page_size (int): 每页条数，默认20，最大100
        channel (str): 可选，email/wechat

    返回:
        tuple: 统一响应体 200，data 为 {items,total,page,page_size,total_pages}；
               死信仓储当前仅提供"最近N条"能力，本接口在内存内分页（死信量小，
               量级增大后应下沉为 SQL 分页）
    """
    page, page_size = _parse_pagination()
    channel = _parse_optional_str("channel", VALID_CHANNELS)

    # 死信仓储现有 list_all(limit) 按 id 升序返回最近 N 条；
    # 取足够大窗口后按 id 倒序（最新在前），再做渠道过滤与内存分页
    window = NotificationDeadLetterRepository.list_all(limit=10000)
    rows = list(reversed(window))
    if channel:
        rows = [row for row in rows if row.get("channel") == channel]
    total = len(rows)
    start = (page - 1) * page_size
    items = rows[start : start + page_size]
    total_pages = (total + page_size - 1) // page_size
    return success(
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
    )
