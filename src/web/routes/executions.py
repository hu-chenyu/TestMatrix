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

功能（第三阶段Day25交付）:
    - GET /api/executions/<execution_id>/events 批次SSE实时事件流
      （text/event-stream流式响应，逐帧转发事件通道中的
      batch_start/case_finished/batch_finished/batch_failed事件；
      已终态批次立即快照直发关闭流，不挂死等待）

功能（第三阶段Day26交付）:
    - SSE帧增加id行（event_id透传，Last-Event-ID断线回放依据）
    - Last-Event-ID请求头解析: 客户端断线重连携带已收到的最后
      事件id，服务端只回放该id之后的事件（断点续传）
    - 心跳保活: 运行中批次订阅空闲超过HEARTBEAT_INTERVAL_SECONDS
      时发送": heartbeat"注释帧（SSE注释行以冒号开头，浏览器
      EventSource静默忽略），防中间代理/负载均衡掐断空闲连接

功能（第三阶段Day29交付）:
    - trigger接口case_type枚举校验（api/chip，复用cases.py的
      VALID_CASE_TYPES单一事实来源，非法值400不再静默走默认）
    - 批次受理成功后落业务埋点日志（execution_id/筛选用例数/
      executor/四维筛选条件），与请求级"请求:/响应:"日志分层

数据口径:
    - 批次列表只含已完成批次（finish_execution后才有defect_statistics
      汇总记录），未finish的执行中批次不在列表范围；执行中批次的
      状态查询走 /<execution_id>/status 接口
    - 查询接口纯GET无请求体不引入marshmallow；触发接口入参全部
      可选（无请求体即全量回归），走手动校验（简单参数不上Schema）
    - events接口三分支: 通道存在且运行中→订阅实时转发；
      通道存在但已终态（publish后close前的竞态窗口）→snapshot
      补发积压事件；通道不存在（pending极早期/CLI批次/终态已
      清理）→按批次状态降级直发（finished批次从DB重建完整事件
      序列），全部有限帧后关闭流
    - 帧id与回放口径（Day26）: 运行中订阅与终态补发透传通道
      event_id；DB重建分支的合成id从1起连续分配——两套编号
      相互独立（通道event_id是通道内单调计数，DB合成id是重建
      序列序号），客户端Last-Event-ID只与当次响应的编号体系比较
"""

import json
import threading
import time
from typing import Iterator, Optional

from flask import Blueprint, Response, request, stream_with_context

from src.common.logger import LogManager
from src.core.case_manager import MAX_PAGE_SIZE, CaseManager, CaseManagerError
from src.core.event_bus import get_channel
from src.core.executors import VALID_EXECUTORS
from src.web.exceptions import NotFoundError, ValidationError
from src.web.response import success
# case_type合法枚举复用cases.py常量（单一事实来源，防两处定义漂移）
from src.web.routes.cases import VALID_CASE_TYPES

logger = LogManager.get_logger()

executions_bp = Blueprint("executions", __name__, url_prefix="/api/executions")

# 分页参数默认值（与cases.py口径一致）
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20

# 批次终态事件类型（流式接口收到后立即关闭流的信号）
TERMINAL_EVENT_TYPES = ("batch_finished", "batch_failed")

# 相邻SSE帧之间的短暂让出间隔（秒）: 开发服务器下让出GIL，
# 保证流式响应不被缓冲、生产方线程得以持续publish
FRAME_INTERVAL_SECONDS = 0.05

# 心跳间隔（秒）: 运行中批次订阅空闲超过该时长时发送一条
# ": heartbeat"注释帧保活（防中间代理/负载均衡掐断空闲连接）；
# 注释帧不带id行，不会推进客户端的Last-Event-ID游标
HEARTBEAT_INTERVAL_SECONDS = 15.0


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
        3. case_type枚举校验（api/chip，复用cases.py的
           VALID_CASE_TYPES），非法值转400（Day29新增）
        4. CaseManager.start_execution筛选用例并落pending批次行，
           空用例集转400
        5. daemon后台线程执行批次（_execute_batch_async内部
           自管session与异常兜底），接口立即返回202不阻塞；
           受理成功后落业务埋点日志（Day29新增）

    参数:
        无（从request.get_json解析可选请求体）

    返回:
        tuple[dict, int]: (统一响应体, 202)，data为:
            {"execution_id": 批次号, "status": "pending",
             "total_cases": 用例数}

    异常:
        ValidationError: executor非法/case_type非法/请求体非JSON
                         对象/无符合条件的用例时抛出（400）
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

    # 四维筛选条件先解析到局部变量（供核心层透传与受理埋点共用，
    # 避免日志与实际入参因两次解析发生漂移）
    module_filter = body.get("module")
    priority_filter = body.get("priority")
    tags_filter = body.get("tags")
    # case_type缺省归一化为api（Day29前非法值静默走默认，此处补齐枚举校验）
    case_type = _parse_optional_body_str(body, "case_type") or "api"
    if case_type not in VALID_CASE_TYPES:
        raise ValidationError(
            f"case_type非法: {case_type!r}，合法取值: api/chip"
        )

    # 可选筛选与环境参数透传核心层（module/priority/tags/case_type）
    try:
        result = CaseManager.start_execution(
            trigger="web",
            executor_name="web",
            environment=_parse_optional_body_str(body, "environment") or "dev",
            remark=_parse_optional_body_str(body, "remark"),
            module=module_filter,
            priority=priority_filter,
            tags=tags_filter,
            case_type=case_type,
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

    # 业务受理埋点（在start之后、return之前）: 记录批次号/筛选用例数/
    # executor/四维筛选条件，便于后台批次检索与问题定位；未传入
    # （None）的字段统一打"-"，executor未显式指定同样打"-"（实际
    # 执行器由工厂按TM_EXECUTOR环境变量裁定，默认simulated）
    module_text = module_filter if module_filter is not None else "-"
    priority_text = priority_filter if priority_filter is not None else "-"
    tags_text = tags_filter if tags_filter is not None else "-"
    executor_text = executor_kind if executor_kind is not None else "-"
    logger.info(
        f"批次已受理 | execution_id={result['execution_id']} | "
        f"trigger=web | total_cases={result['total_cases']} | "
        f"executor={executor_text} | "
        f"筛选=module:{module_text}/priority:{priority_text}/"
        f"tags:{tags_text}/case_type:{case_type}"
    )

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


def _format_sse_frame(
    event_type: str, data: dict, event_id: Optional[int] = None
) -> str:
    """
    格式化单条SSE帧（内部方法）

    帧格式按SSE规范（id行可选）:
        event: {事件类型}\\n
        id: {事件序号}\\n（可选，event行后data行前；
             客户端EventSource收到后自动记录为Last-Event-ID，
             断线重连时回传实现断点续传）
        data: {JSON字符串}\\n
        \\n（空行结束一帧）

    参数:
        event_type (str): 事件类型（合法取值见
                          event_bus.VALID_EVENT_TYPES）
        data (dict): 事件载荷，序列化为JSON字符串
                     （ensure_ascii=False，中文原样输出）
        event_id (int | None): 事件序号（Last-Event-ID断点回放
                              依据），None时不输出id行（降级帧
                              向后兼容Day25三要素格式）

    返回:
        str: 单条完整SSE帧文本（以空行结尾）

    异常:
        无
    """
    payload = json.dumps(data, ensure_ascii=False)
    if event_id is None:
        return f"event: {event_type}\ndata: {payload}\n\n"
    return f"event: {event_type}\nid: {event_id}\ndata: {payload}\n\n"


def _parse_last_event_id_header() -> Optional[int]:
    """
    解析Last-Event-ID请求头（内部方法，断线回放锚点）

    SSE客户端（EventSource）断线重连时自动回传最后收到的事件id；
    能转int就转，非法或缺失时静默当None（全量回放，绝不因畸形
    头拒掉重连请求）。

    参数:
        无（从request.headers读取）

    返回:
        int | None: 断点事件id；请求头缺失/非合法整数时返回None

    异常:
        无
    """
    raw_value = request.headers.get("Last-Event-ID")
    if raw_value is None:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _terminal_snapshot_frame(
    status_data: dict, event_id: Optional[int] = None
) -> str:
    """
    构造批次终态快照帧（内部方法，降级补发专用）

    按批次状态机当前状态构造对应的终态SSE帧:
        failed   → batch_failed {error_message}
        finished → batch_finished {total, passed, failed, error,
                                  skipped, pass_rate}
    （载荷字段与case_manager埋点口径完全一致）

    参数:
        status_data (dict): get_execution_status返回的批次状态字典
        event_id (int | None): 帧id（DB重建分支的合成id或固定1），
                              None时不输出id行（降级帧）

    返回:
        str: 终态快照SSE帧文本

    异常:
        无
    """
    if status_data["status"] == "failed":
        return _format_sse_frame(
            "batch_failed",
            {"error_message": status_data.get("error_message")},
            event_id=event_id,
        )
    return _format_sse_frame(
        "batch_finished",
        {
            "total": status_data["total_cases"],
            "passed": status_data["passed"],
            "failed": status_data["failed"],
            "error": status_data["error"],
            "skipped": status_data["skipped"],
            "pass_rate": status_data["pass_rate"],
        },
        event_id=event_id,
    )


@executions_bp.route("/<execution_id>/events")
def stream_execution_events(execution_id: str):
    """
    执行批次SSE实时事件流接口

    text/event-stream流式响应，逐帧转发事件通道中的执行事件
    （batch_start/case_finished/batch_finished/batch_failed），
    帧携带id行（事件序号），收到终态事件后关闭流。

    断线回放（Day26）:
        客户端断线重连时携带Last-Event-ID请求头（EventSource
        自动回传最后收到的事件id），服务端只回放该id之后的事件
        （断点续传）；请求头缺失/非法时静默全量回放。

    心跳保活（Day26）:
        运行中批次订阅空闲超过HEARTBEAT_INTERVAL_SECONDS时发送
        ": heartbeat"注释帧（SSE注释行以冒号开头，EventSource
        静默忽略，不推进Last-Event-ID游标），防空闲连接被中间
        代理掐断。

    分支口径（通道先查再查批次状态，防"状态读到running后通道
    又被终态清理"的竞态）:
        1. 批次不存在: 404（统一错误格式）
        2. 通道不存在（pending极早期 / CLI批次 / 终态已清理）:
           按状态降级直发——finished批次从DB（汇总+明细）重建
           batch_start→case_finished×N→batch_finished完整事件
           序列（合成id从1起连续分配，并按last_event_id过滤）；
           failed/pending批次单帧快照直发（固定event_id=1）；
           全部有限帧后关闭流，不挂死等待
        3. 通道存在且批次已终态（publish后close注册表移除前的
           竞态窗口）: channel.snapshot()补发积压事件（此时积压
           已含终态事件，event_id透传进帧），无终态事件时补一条
           终态快照帧
        4. 通道存在且运行中: subscribe(last_event_id, tick=True)
           阻塞订阅实时转发（历史环回放断点后事件），收到
           batch_finished/batch_failed后关闭流，空闲超时发心跳

    SSE帧格式: event: {类型}\\nid: {序号}\\ndata: {JSON}\\n\\n
    （空行结束一帧，id行可选）

    参数:
        execution_id (str): 执行批次号（URL路径参数）

    返回:
        Response: text/event-stream流式响应（stream_with_context
                 包装的生成器；帧间sleep 0.05s让出GIL防开发
                 服务器缓冲）

    异常:
        NotFoundError: 批次不存在（无批次元信息行）时抛出（404）
        CaseManagerError: 核心层数据库异常原样上抛（兜底500，
                          仅发生在流开始前的状态查询阶段）
    """
    # 1. 先查通道再查批次状态: 本地channel引用在批次终态后仍
    #    有效（注册表移除不影响已获取引用的drain语义），避免
    #    "状态读到running、通道已被close_channel清理"时误走
    #    降级分支丢实时事件
    channel = get_channel(execution_id, create=False)

    # 2. 批次存在性: 不存在统一404
    status_data = CaseManager.get_execution_status(execution_id)
    if status_data is None:
        raise NotFoundError(
            "执行批次不存在", detail={"execution_id": execution_id}
        )

    # 3. 断点回放锚点: Last-Event-ID请求头（能转int就转，
    #    非法或缺失静默当None全量回放）
    last_event_id = _parse_last_event_id_header()

    # 4. 流式生成器（三分支: 降级快照 / 终态补发 / 实时订阅）
    @stream_with_context
    def event_generator() -> Iterator[str]:
        # 分支一: 通道不在注册表（pending极早期/CLI批次/终态已清理）
        if channel is None:
            if status_data["status"] == "finished":
                # 已完成批次: 从DB重建完整事件序列直发
                # （汇总+明细与埋点载荷同口径，客户端离线后补看
                # 全量日志不依赖通道存活）
                detail = None
                try:
                    detail = CaseManager.get_execution_detail(execution_id)
                except CaseManagerError:
                    # 明细查询异常不阻断流: 降级为单条终态快照帧
                    detail = None
                # 合成id从1起连续分配（batch_start=1、case_finished
                # 依次递增、batch_finished=2+N），与通道event_id是
                # 两套独立编号，仅服务本次重建响应的断点过滤；
                # last_event_id为None时归一化为0（id从1起，等同全量）
                resume_after = last_event_id if last_event_id is not None else 0
                next_frame_id = 1
                if detail is not None:
                    if next_frame_id > resume_after:
                        yield _format_sse_frame(
                            "batch_start",
                            {
                                "total_cases": detail["summary"]["total_cases"],
                                "executor_kind": None,
                            },
                            event_id=next_frame_id,
                        )
                    next_frame_id += 1
                    for item in detail["items"]:
                        if next_frame_id > resume_after:
                            yield _format_sse_frame(
                                "case_finished",
                                {
                                    "case_id": item["case_id"],
                                    "case_name": item["case_name"],
                                    "result": item["result"],
                                    "duration": item["duration"],
                                    "error_message": item["error_message"],
                                },
                                event_id=next_frame_id,
                            )
                        next_frame_id += 1
                # 终态帧同样按断点过滤: 客户端已收到过该id则不重复补发
                if next_frame_id > resume_after:
                    yield _terminal_snapshot_frame(
                        status_data, event_id=next_frame_id
                    )
                return
            if status_data["status"] == "failed":
                # 失败批次: 无defect_statistics汇总行，单帧终态直发
                # （固定event_id=1: 单帧即全量，无断点过滤意义）
                yield _terminal_snapshot_frame(status_data, event_id=1)
                return
            # pending极早期/CLI运行中批次: 单帧进行中快照直发
            # （不挂死等待——CLI批次在Web进程内永远等不到publish）
            yield _format_sse_frame(
                "batch_start",
                {
                    "total_cases": status_data["total_cases"],
                    "status": status_data["status"],
                },
                event_id=1,
            )
            return

        # 分支二: 通道存在但批次已终态（publish后close前的竞态窗口）
        if status_data["status"] in ("finished", "failed"):
            saw_terminal = False
            for event in channel.snapshot():
                # event_id透传进帧（通道内真实编号）
                yield _format_sse_frame(
                    event.event_type, event.data, event_id=event.event_id
                )
                if event.event_type in TERMINAL_EVENT_TYPES:
                    saw_terminal = True
            # 积压无终态事件时补一条终态快照帧（防御性兜底，降级帧无id行）
            if not saw_terminal:
                yield _terminal_snapshot_frame(status_data)
            return

        # 分支三: 运行中批次: 订阅通道实时转发（断点回放+心跳）
        # （本线程持有channel引用，即使批次此刻终态且close_channel
        # 从注册表移除，残余事件仍能被读完，不丢终态事件）
        last_activity = time.monotonic()
        for event in channel.subscribe(
            last_event_id=last_event_id, tick=True
        ):
            if event is None:
                # 心跳节拍（订阅0.5s限时等待超时）: 距上次活动
                # 超过心跳间隔才发注释帧（保活但不刷屏）
                if (
                    time.monotonic() - last_activity
                    >= HEARTBEAT_INTERVAL_SECONDS
                ):
                    yield ": heartbeat\n\n"
                    last_activity = time.monotonic()
                continue
            # 真实事件转发（透传通道event_id）并刷新活动时间
            yield _format_sse_frame(
                event.event_type, event.data, event_id=event.event_id
            )
            last_activity = time.monotonic()
            if event.event_type in TERMINAL_EVENT_TYPES:
                break
            # 帧间短暂让出GIL，防开发服务器流式被缓冲
            time.sleep(FRAME_INTERVAL_SECONDS)

    return Response(event_generator(), mimetype="text/event-stream")
