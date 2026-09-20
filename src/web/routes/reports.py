"""
报告统计与质量度量API蓝图

功能（第三阶段Day23交付）:
    - GET /api/reports/summary              全局汇总（批次数/累计执行/
      各结果计数/加权通过率/最新批次）
    - GET /api/reports/trend                通过率趋势（时间升序，最近N批，
      复用ReportRepository.get_trend_data）
    - GET /api/reports/module-distribution  模块执行分布（前端饼图，
      悬空历史明细归unknown模块）
    - GET /api/reports/failed-top           失败用例Top榜（失败次数聚合
      +最近一次失败堆栈）
    - GET /api/reports/quality-metrics      质量度量（用例执行覆盖率/
      缺陷密度/问题用例占比/执行效率）

数据口径:
    - 汇总通过率为加权口径（总通过数/总执行数），非批次pass_rate
      算术平均
    - 覆盖率类指标分母只算status=active的用例
    - code_coverage为预留契约字段，当前恒为null（真实代码覆盖率
      待Day81 pytest-cov接入后填充，禁止编造数值）
    - 纯GET查询接口，无请求体，不引入marshmallow校验
"""

from flask import Blueprint, request

from src.core.cache import (
    cache_client,
    reports_failed_top_key,
    reports_module_distribution_key,
    reports_quality_metrics_key,
    reports_summary_key,
    reports_trend_key,
)
from src.core.report_analyzer import ReportRepository
from src.web.exceptions import ValidationError
from src.web.response import success

reports_bp = Blueprint("reports", __name__, url_prefix="/api/reports")

# trend接口limit默认值
DEFAULT_TREND_LIMIT = 20

# failed-top接口limit默认值
DEFAULT_FAILED_TOP_LIMIT = 10

# limit参数上限（与ReportRepository.get_failed_top防御口径对齐）
MAX_LIMIT = 100


def _parse_int_param(name: str, default: int) -> int:
    """
    解析整型查询参数（内部方法）

    参数缺省或空白串时返回默认值；传入非合法整数时抛ValidationError。

    参数:
        name (str): 查询参数名（limit）
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
        raise ValidationError("limit必须为正整数")


@reports_bp.route("/summary")
def report_summary():
    """
    全局执行汇总接口

    基于 defect_statistics 全表聚合，空库安全降级
    （total_batches=0、overall_pass_rate=0.0、latest_batch=null）。

    参数:
        无

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为:
            {"total_batches", "total_executed", "passed", "failed",
             "error", "skipped", "overall_pass_rate"（加权口径）,
             "latest_batch"（最近批次或null）}

    缓存（Day31）:
        固定key tm:reports:summary，批次终态后由CaseManager
        主动失效，TM_CACHE_TTL兜底；空库汇总（空字典结构）
        同样缓存防穿透。

    异常:
        无业务异常（数据库异常由全局处理器兜底500）
    """
    # 缓存命中直接返回；未启用/未命中/故障时静默回源
    cache_key = reports_summary_key()
    cached_data = cache_client.get_json(cache_key)
    if cached_data is not None:
        return success(data=cached_data)

    data = ReportRepository.get_overview_summary()
    cache_client.set_json(cache_key, data)
    return success(data=data)


@reports_bp.route("/trend")
def report_trend():
    """
    通过率趋势接口（时间升序）

    查询参数:
        limit (int): 返回最近N个批次，默认20，1到100

    参数:
        无（从request.args解析查询参数）

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为趋势字典列表
            （时间升序，首条最早）: [{"execution_id", "pass_rate",
            "total_cases", "passed", "failed", "error", "created_at"}]；
            空表返回[]

    缓存（Day31）:
        key含limit（tm:reports:trend:{limit}），不同limit互不
        干扰；空列表同样缓存防穿透；批次终态后统一按前缀失效。

    异常:
        ValidationError: limit非法（非整数/越界）时抛出（400）
    """
    limit = _parse_int_param("limit", DEFAULT_TREND_LIMIT)
    if limit < 1 or limit > MAX_LIMIT:
        raise ValidationError(f"limit必须在1到{MAX_LIMIT}之间")

    cache_key = reports_trend_key(limit)
    cached_data = cache_client.get_json(cache_key)
    if cached_data is not None:
        return success(data=cached_data)

    data = ReportRepository.get_trend_data(limit=limit)
    cache_client.set_json(cache_key, data)
    return success(data=data)


@reports_bp.route("/module-distribution")
def report_module_distribution():
    """
    模块执行分布接口（前端饼图数据源）

    明细表outerjoin用例表按模块聚合，悬空历史明细（用例已物理删除）
    归"unknown"模块不丢失；排序total降序 -> module升序。

    参数:
        无

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为模块分布列表:
            [{"module", "total", "passed", "failed", "error",
              "skipped", "pass_rate"}]；空表返回[]

    缓存（Day31）:
        固定key tm:reports:module_distribution，空列表同样
        缓存防穿透；批次终态后统一按前缀失效。

    异常:
        无业务异常（数据库异常由全局处理器兜底500）
    """
    cache_key = reports_module_distribution_key()
    cached_data = cache_client.get_json(cache_key)
    if cached_data is not None:
        return success(data=cached_data)

    data = ReportRepository.get_module_distribution()
    cache_client.set_json(cache_key, data)
    return success(data=data)


@reports_bp.route("/failed-top")
def report_failed_top():
    """
    失败用例Top榜接口

    查询参数:
        limit (int): 返回Top N失败用例，默认10，1到100

    参数:
        无（从request.args解析查询参数）

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为失败用例列表:
            [{"case_id", "case_name", "fail_count",
              "last_failed_at"（ISO字符串）,
              "last_error_message"（最近一次失败堆栈，原样透传）}]；
            排序fail_count降序 -> case_id升序；无失败记录返回[]

    缓存（Day31）:
        key含limit（tm:reports:failed_top:{limit}），不同limit
        互不干扰；空列表同样缓存防穿透；批次终态后统一按前缀失效。

    异常:
        ValidationError: limit非法（非整数/越界）时抛出（400）
    """
    limit = _parse_int_param("limit", DEFAULT_FAILED_TOP_LIMIT)
    if limit < 1 or limit > MAX_LIMIT:
        raise ValidationError(f"limit必须在1到{MAX_LIMIT}之间")

    cache_key = reports_failed_top_key(limit)
    cached_data = cache_client.get_json(cache_key)
    if cached_data is not None:
        return success(data=cached_data)

    # 核心层limit防御（ValueError）转400（路由层已先行拦截，
    # 此处为双保险兜底）
    try:
        data = ReportRepository.get_failed_top(limit=limit)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    cache_client.set_json(cache_key, data)
    return success(data=data)


@reports_bp.route("/quality-metrics")
def report_quality_metrics():
    """
    质量度量接口

    指标口径:
        - case_execution_coverage: 明细distinct case_id数/active用例数
        - defect_density: failed+error明细数/全部明细数
        - problem_case_ratio: 出过failed/error的distinct case_id数/
          active用例数
        - execution_efficiency: 平均耗时/P95耗时（小样本取最大值）/
          平均每批次用例数
        - code_coverage: 预留契约字段恒为null（Day81 pytest-cov
          接入后填充）

    参数:
        无

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为质量度量字典
            （空库全部指标0.0，不报错）

    缓存（Day31）:
        固定key tm:reports:quality_metrics，空库指标同样
        缓存防穿透；批次终态后统一按前缀失效。

    异常:
        无业务异常（数据库异常由全局处理器兜底500）
    """
    cache_key = reports_quality_metrics_key()
    cached_data = cache_client.get_json(cache_key)
    if cached_data is not None:
        return success(data=cached_data)

    data = ReportRepository.get_quality_metrics()
    cache_client.set_json(cache_key, data)
    return success(data=data)
