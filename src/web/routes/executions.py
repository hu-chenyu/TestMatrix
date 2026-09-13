"""
执行记录API蓝图

功能（第三阶段Day22交付）:
    - GET /api/executions/               执行批次列表（分页，
      最新完成批次在前）
    - GET /api/executions/<execution_id> 批次详情（汇总统计 +
      单用例执行明细，失败用例error_message完整返回不截断）

功能（第三阶段Day24交付）:
    - POST /api/executions/trigger              触发用例异步执行
      （daemon后台线程执行批次，立即返回202与pending状态；
      trigger固定为web，Web触发来源不可由前端伪造）
    - GET /api/executions/<execution_id>/status 批次状态查询
      （pending/running/finished/failed状态机直查批次元信息表）

数据口径:
    - 批次列表只含已完成批次（finish_execution后才有defect_statistics
      汇总记录），未finish的执行中批次不在列表范围；执行中批次的
      状态查询走 /<execution_id>/status 接口
    - 查询接口纯GET无请求体不引入marshmallow；触发接口入参全部
      可选（无请求体即全量回归），走手动校验（简单参数不上Schema）
"""

import threading
from typing import Optional

from flask import Blueprint, request

from src.core.case_manager import MAX_PAGE_SIZE, CaseManager, CaseManagerError
from src.core.executors import VALID_EXECUTORS
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


def _parse_optional_body_str(body: dict, name: str) -> Optional[str]:
    """
    解析请求体中可选字符串字段（内部方法）

    字段缺省、None、空白串或非字符串类型视为未传入（返回None），
    其余返回strip后的值；避免"executor": ""这类空值下发起无意义
    过滤或校验歧义。

    参数:
        body (dict): JSON请求体字典
        name (str): 字段名（module/priority/tags/executor等）

    返回:
        str | None: 解析后的字符串值，未传入时为None

    异常:
        无
    """
    raw_value = body.get(name)
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        return None
    stripped = raw_value.strip()
    return stripped if stripped else None


@executions_bp.route("/trigger", methods=["POST"])
def trigger_execution():
    """
    触发用例异步执行接口

    请求体全部字段可选（无请求体即全量回归）:
        module      模块筛选（str）
        priority    优先级筛选（str或list[str]）
        tags        标签筛选（str或list[str]）
        case_type   用例类型api/chip，默认api
        executor    执行器类型simulated/pytest，默认读TM_EXECUTOR
                    环境变量（兜底simulated）
        environment 执行环境，默认dev
        remark      批次备注

    处理流程:
        1. trigger固定"web"（Web触发来源由服务端裁定，
           不可由前端伪造传入）
        2. executor非法值手动校验转400（简单参数不上marshmallow）
        3. CaseManager.start_execution筛选用例并落pending批次行，
           空用例集转400
        4. daemon后台线程执行批次（_execute_batch_async内部
           自管session与异常兜底），接口立即返回202不阻塞

    参数:
        无（从request.get_json解析可选请求体）

    返回:
        tuple[dict, int]: (统一响应体, 202)，data为:
            {"execution_id": 批次号, "status": "pending",
             "total_cases": 用例数}

    异常:
        ValidationError: executor非法/请求体非JSON对象/
                         无符合条件的用例时抛出（400）
        CaseManagerError: 其他核心层异常原样上抛（兜底500）
    """
    # 请求体可选: 无请求体(None)视为空对象走全量回归
    body = request.get_json(silent=True)
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ValidationError("请求体必须为JSON对象")

    # executor校验: 合法值simulated/pytest，缺省None交由工厂
    # 读TM_EXECUTOR环境变量（兜底simulated）
    executor_kind = _parse_optional_body_str(body, "executor")
    if executor_kind is not None and executor_kind not in VALID_EXECUTORS:
        raise ValidationError(
            f"executor非法: {executor_kind!r}，合法取值: {list(VALID_EXECUTORS)}"
        )

    # 可选筛选与环境参数透传核心层（module/priority/tags/case_type）
    try:
        result = CaseManager.start_execution(
            trigger="web",
            executor_name="web",
            environment=_parse_optional_body_str(body, "environment") or "dev",
            remark=_parse_optional_body_str(body, "remark"),
            module=body.get("module"),
            priority=body.get("priority"),
            tags=body.get("tags"),
            case_type=_parse_optional_body_str(body, "case_type") or "api",
        )
    except CaseManagerError as exc:
        # 关键字分流（口径同6.9/6.10）: 无符合条件的用例转400，
        # 其余（数据库异常等）原样上抛兜底500
        if "无符合条件的用例" not in str(exc):
            raise
        raise ValidationError(str(exc)) from exc

    # daemon后台线程执行批次，接口立即返回不阻塞
    # （_execute_batch_async内部自建session、异常兜底置failed，
    # 绝不向请求线程抛异常）
    threading.Thread(
        target=CaseManager._execute_batch_async,
        args=(result["execution_id"], result["cases"], executor_kind),
        daemon=True,
    ).start()

    return success(
        data={
            "execution_id": result["execution_id"],
            "status": "pending",
            "total_cases": result["total_cases"],
        },
        message="执行批次已受理，后台执行中",
        code=202,
    )


@executions_bp.route("/<execution_id>/status")
def get_execution_status(execution_id: str):
    """
    执行批次状态查询接口

    返回批次状态机当前状态（pending/running/finished/failed）与
    finish后冗余的各结果计数、通过率、起止时间；failed批次携带
    批次级error_message；执行中批次冗余统计字段为初始值0。

    参数:
        execution_id (str): 执行批次号（URL路径参数）

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为批次状态字典:
            {"execution_id", "status", "total_cases", "passed",
             "failed", "error", "skipped", "pass_rate",
             "error_message", "started_at", "finished_at",
             "created_at"}

    异常:
        NotFoundError: 批次不存在（无批次元信息行）时抛出（404）
        CaseManagerError: 核心层数据库异常原样上抛（兜底500）
    """
    # 核心层查询（None转404；数据库异常原样上抛兜底500）
    status_data = CaseManager.get_execution_status(execution_id)
    if status_data is None:
        raise NotFoundError(
            "执行批次不存在", detail={"execution_id": execution_id}
        )
    return success(data=status_data)
