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
from datetime import datetime, timezone

from flask import Blueprint
from sqlalchemy import text

from src.common.env_manager import env_manager
from src.db.db_session import DatabaseSession
from src.web.response import error, success

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


def _check_database() -> tuple[bool, str]:
    """
    带超时保护的数据库连通性检查

    探测查询在独立线程池中执行，超过HEALTH_DB_PROBE_TIMEOUT秒
    未完成即判定数据库不可用（网络黑洞/连接堆积等场景），
    确保/health接口自身始终可控返回，不随数据库hang死。

    参数:
        无

    返回:
        tuple[bool, str]: (是否连通, 失败原因描述)；
        连通时第二元素为空字符串，失败时为异常摘要（供排查）
    """
    executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="health-db-probe"
    )
    future = executor.submit(_probe_database)
    try:
        future.result(timeout=HEALTH_DB_PROBE_TIMEOUT)
        return True, ""
    except FutureTimeoutError:
        future.cancel()
        return False, f"数据库探测超时（超过{HEALTH_DB_PROBE_TIMEOUT}秒）"
    except Exception as exc:  # noqa: BLE001 健康检查需兜住全部数据库异常
        return False, str(exc)
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

    返回:
        数据库正常: code=200，
            data={"status": "healthy", "database": "connected",
                  "timestamp": ISO时间, "env": 当前环境}
        数据库异常: code=503（服务降级），
            data={"status": "degraded", "database": "disconnected",
                  "error": 异常摘要, "timestamp": ISO时间, "env": 当前环境}
    """
    connected, error_message = _check_database()
    common = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
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
