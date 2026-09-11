"""
执行记录API蓝图

功能（第三阶段Day22交付）:
    - GET /api/executions/               执行批次列表（分页，
      最新完成批次在前）
    - GET /api/executions/<execution_id> 批次详情（汇总统计 +
      单用例执行明细，失败用例error_message完整返回不截断）

数据口径:
    - 批次列表只含已完成批次（finish_execution后才有defect_statistics
      汇总记录），未finish的执行中批次不在列表范围
    - 纯GET查询接口，无请求体，不引入marshmallow校验
"""

from flask import Blueprint, request

from src.core.case_manager import MAX_PAGE_SIZE, CaseManager, CaseManagerError
from src.web.exceptions import NotFoundError, ValidationError
from src.web.response import success

executions_bp = Blueprint("executions", __name__, url_prefix="/api/executions")

# 分页参数默认值（与cases.py口径一致）
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


@executions_bp.route("/")
def list_executions():
    """
    执行批次列表接口（仅已完成批次）

    查询参数:
        page        页码，默认1，必须>=1
        page_size   每页条数，默认20，1到100

    排序: created_at倒序（最新完成批次在前），同秒完成的批次按
    execution_id倒序保证跨页次序稳定

    参数:
        无（从request.args解析查询参数）

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为分页结构:
            {"items": list[批次汇总dict], "total": int, "page": int,
             "page_size": int, "total_pages": int}

    异常:
        ValidationError: 分页参数非法时抛出（全局异常处理器统一转400）
        CaseManagerError: 核心层数据库异常原样上抛（兜底500）
    """
    # 1. 分页参数解析与范围校验（口径与cases.py一致）
    page = _parse_int_param("page", DEFAULT_PAGE)
    page_size = _parse_int_param("page_size", DEFAULT_PAGE_SIZE)
    if page < 1:
        raise ValidationError("page必须为大于等于1的整数")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValidationError(f"page_size必须在1到{MAX_PAGE_SIZE}之间")

    # 2. 调用核心层分页查询（核心层再做防御校验，两层各自兜底）
    result = CaseManager.list_executions_paged(page=page, page_size=page_size)
    return success(data=result)


@executions_bp.route("/<execution_id>")
def get_execution_detail(execution_id: str):
    """
    执行批次详情接口（汇总统计 + 单用例执行明细）

    返回的items按id升序（与record_execution写入顺序一致，即执行
    先后顺序）；失败/错误用例的error_message（异常堆栈）原样完整
    返回不截断，供前端失败详情展示与问题定位。

    参数:
        execution_id (str): 执行批次号（URL路径参数）

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为:
            {"summary": 批次汇总dict,
             "items": list[单用例明细dict]}

    异常:
        NotFoundError: 批次不存在（无汇总记录）时抛出（404）
        CaseManagerError: 其他核心层异常原样上抛（兜底500）
    """
    # 核心层查询（数据库异常原样上抛兜底500）
    try:
        detail = CaseManager.get_execution_detail(execution_id)
    except CaseManagerError as exc:
        if "不存在" not in str(exc):
            raise
        raise NotFoundError(
            "执行批次不存在", detail={"execution_id": execution_id}
        ) from exc

    # 无汇总记录: 批次号不存在或未finish，统一404
    if detail is None:
        raise NotFoundError(
            "执行批次不存在", detail={"execution_id": execution_id}
        )
    return success(data=detail)
