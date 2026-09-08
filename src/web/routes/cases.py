"""
用例管理API蓝图

功能（第三阶段Day19交付）:
    - GET /api/cases/  用例列表（分页 + module/priority/case_type/
      status/keyword六维筛选，全部可选、可组合使用）

后续Day20实现:
    - POST   /api/cases/          创建用例
    - GET    /api/cases/<id>      用例详情
    - PUT    /api/cases/<id>      更新用例
    - DELETE /api/cases/<id>      删除用例
"""

from typing import Optional

from flask import Blueprint, request

from src.core.case_manager import MAX_PAGE_SIZE, CaseManager
from src.web.exceptions import ValidationError
from src.web.response import success

cases_bp = Blueprint("cases", __name__, url_prefix="/api/cases")

# 用例类型合法值
VALID_CASE_TYPES = ("api", "chip")

# 用例状态查询合法值（all为"查全部"语义，透传核心层时转为None）
VALID_CASE_STATUSES = ("active", "disabled", "all")

# 分页参数默认值
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20


def _parse_int_param(name: str, default: int) -> int:
    """
    解析整型查询参数（内部方法）

    参数缺省或空白串时返回默认值；传入非合法整数时抛ValidationError。

    参数:
        name (str): 查询参数名（page/page_size）
        default (int): 参数缺省时的默认值

    返回:
        int: 解析后的整数值

    异常:
        ValidationError: 参数值不是合法整数时抛出
    """
    raw_value = request.args.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        raise ValidationError("page和page_size必须为正整数")


def _parse_optional_str(name: str) -> Optional[str]:
    """
    解析可选字符串查询参数（内部方法）

    参数缺省或空白串视为未传入（返回None），其余返回strip后的值，
    避免"?keyword="这类空参数下发起无意义的全量过滤。

    参数:
        name (str): 查询参数名

    返回:
        str | None: 解析后的字符串值，未传入时为None

    异常:
        无
    """
    raw_value = request.args.get(name)
    if raw_value is None:
        return None
    stripped = raw_value.strip()
    return stripped if stripped else None


@cases_bp.route("/")
def list_cases():
    """
    用例列表接口

    支持六维筛选（全部可选、可组合）:
        page        页码，默认1，必须>=1
        page_size   每页条数，默认20，1到100
        module      模块筛选（精确匹配）
        priority    优先级筛选（不区分大小写，统一大写匹配）
        case_type   类型筛选（api/chip）
        status      状态筛选（active/disabled/all），默认active
        keyword     关键字模糊搜索（case_id/name/description）

    参数:
        无（从request.args解析查询参数）

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为分页结构:
            {"items": list[dict], "total": int, "page": int,
             "page_size": int, "total_pages": int}

    异常:
        ValidationError: 分页参数非法 / case_type或status取值非法时
                         抛出（全局异常处理器统一转400响应）
    """
    # 1. 分页参数解析与范围校验
    page = _parse_int_param("page", DEFAULT_PAGE)
    page_size = _parse_int_param("page_size", DEFAULT_PAGE_SIZE)
    if page < 1:
        raise ValidationError("page必须为大于等于1的整数")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValidationError(f"page_size必须在1到{MAX_PAGE_SIZE}之间")

    # 2. 筛选参数解析与取值校验
    module = _parse_optional_str("module")
    priority = _parse_optional_str("priority")
    keyword = _parse_optional_str("keyword")

    case_type = _parse_optional_str("case_type")
    if case_type is not None and case_type not in VALID_CASE_TYPES:
        raise ValidationError("case_type只能为api或chip")

    status = _parse_optional_str("status") or "active"
    if status not in VALID_CASE_STATUSES:
        raise ValidationError("status只能为active、disabled或all")

    # 3. 调用核心层分页查询（status=all转None查全部状态）
    result = CaseManager.list_cases_paged(
        module=module,
        priority=priority,
        case_type=case_type,
        status=None if status == "all" else status,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return success(data=result)
