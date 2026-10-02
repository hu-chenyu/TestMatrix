"""
基础接口蓝图

提供:
    GET /              平台首页（版本信息+运行状态）
    GET /health        健康检查接口（含数据库连通性探测，降级返回503）
    GET /api/version   版本信息接口

所有接口统一使用response模块封装响应，禁止直接return jsonify(...)。
"""

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime

from flask import Blueprint
from sqlalchemy import text

from src.common.env_manager import env_manager
from src.common.logger import LogManager
from src.db.db_session import DatabaseSession
from src.web.response import error, success

logger = LogManager.get_logger()

base_bp = Blueprint("base", __name__)

# 数据库探测超时时间（秒）: 探测查询在独立线程执行，超时即判定数据库
# 不可用并立即返回降级响应，保证/health接口不会被慢库/网络黑洞拖死
HEALTH_DB_PROBE_TIMEOUT: float = 3.0


def _probe_database() -> None:
    """
    数据库连通性探测（在独立线程中执行）

    通过DatabaseSession会话执行轻量查询SELECT 1，验证数据库
    引擎建连与查询通路可用；探测查询本身异常向上抛出，
    由_check_database统一捕获归类。

    参数:
        无

    返回:
        无

    异常:
        SQLAlchemyError/ValueError等数据库异常向上抛出（连接失败/
        配置非法等场景）
    """
    with DatabaseSession.session_scope() as session:
        session.execute(text("SELECT 1"))


def _check_database() -> tuple[bool, str, str]:
    """
    带超时保护的数据库连通性检查

    探测查询在独立线程池中执行，超过HEALTH_DB_PROBE_TIMEOUT秒
    未完成即判定数据库不可用（网络黑洞/连接堆积等场景），
    确保/health接口自身始终可控返回，不随数据库hang死。

    返回值刻意拆成「对外摘要」与「内部详情」两段：摘要不含连接串与SQL，
    详情仅供 TESTING 模式回显与日志使用（口径同 6.7 TESTING/生产双模式）。

    参数:
        无

    返回:
        tuple[bool, str, str]: (是否连通, 对外摘要, 内部详情)
        - 连通: (True, "", "")
        - 超时: (False, 超时描述, 超时描述)
        - 探测异常: (False, 异常类型名摘要, 完整异常文本)
          完整文本含 SQLAlchemy 连接URL与SQL片段，**禁止**在非 TESTING
          环境回显（/health 无鉴权且常被监控高频轮询）
    """
    executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="health-db-probe"
    )
    future = executor.submit(_probe_database)
    try:
        future.result(timeout=HEALTH_DB_PROBE_TIMEOUT)
        return True, "", ""
    except FutureTimeoutError:
        future.cancel()
        detail = f"数据库探测超时（超过{HEALTH_DB_PROBE_TIMEOUT}秒）"
        return False, detail, detail
    except Exception as exc:  # noqa: BLE001 健康检查需兜住全部数据库异常
        # 完整上下文交由日志保留，供运维排查
        logger.warning(
            f"数据库健康探测失败 | 类型: {type(exc).__name__} | {exc}"
        )
        return False, f"数据库不可用（{type(exc).__name__}）", f"{type(exc).__name__}: {exc}"
    finally:
        # wait=False: 超时场景下不等待探测线程结束，立即释放主流程
        executor.shutdown(wait=False)


@base_bp.route("/")
def index():
    """
    平台首页接口

    返回:
        统一响应: code=200，data含版本与运行状态
    """
    return success(
        data={"version": "1.0.0", "status": "running"},
        message="TestMatrix API",
    )


@base_bp.route("/health")
def health():
    """
    健康检查接口（含数据库连通性探测）

    探测逻辑: 执行SELECT 1轻量查询（带超时保护），
    数据库异常不影响进程存活，仅将服务标记为降级状态。

    错误信息双模式（口径同 6.7 与 exceptions.py 全局处理器）:
        TESTING=True  -> 回显完整异常详情，便于测试与本地联调定位
        TESTING=False -> 只回显不含连接串/SQL的异常类型名摘要。
                         /health 无鉴权且常被监控高频轮询，SQLAlchemy 异常
                         文本含连接URL（MySQL 模式下含用户名/库名）与SQL片段，
                         生产环境回显等于主动泄露内部实现细节

    返回:
        数据库正常: code=200，
            data={"status": "healthy", "database": "connected",
                  "timestamp": ISO时间, "env": 当前环境}
        数据库异常: code=503（服务降级），
            data={"status": "degraded", "database": "disconnected",
                  "error": 异常摘要或详情（按TESTING门控）,
                  "timestamp": ISO时间, "env": 当前环境}
    """
    connected, public_summary, internal_detail = _check_database()
    # 仅TESTING模式回显详情；生产环境一律用脱敏摘要
    from flask import current_app

    is_testing = bool(current_app.config.get("TESTING"))
    error_message = internal_detail if is_testing else public_summary
    common = {
        "timestamp": datetime.now(UTC).isoformat(),
        "env": env_manager.current_env,
    }
    if connected:
        return success(
            data={"status": "healthy", "database": "connected", **common}
        )
    return error(
        "服务降级：数据库连接失败",
        503,
        data={
            "status": "degraded",
            "database": "disconnected",
            "error": error_message,
            **common,
        },
    )


@base_bp.route("/api/version")
def api_version():
    """
    版本信息接口

    返回:
        统一响应: code=200，data含平台版本与API版本号
    """
    return success(
        data={"version": "1.0.0", "api_version": "v1"},
        message="version info",
    )
