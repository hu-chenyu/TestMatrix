"""
Flask Web应用配置模块

功能:
    - 多环境配置类（开发/测试/生产），通过 TM_ENV 环境变量切换
    - 所有配置项优先从环境变量读取，提供合理默认值
    - 配置映射表 config_map，供应用工厂按名加载

使用示例:
    from src.web.config import get_config, DevelopmentConfig

    config_class = get_config()           # 默认读取 TM_ENV
    config_class = get_config("test")     # 显式指定测试配置
"""

import os
import secrets

from src.common.logger import LogManager

logger = LogManager.get_logger()

# 上传请求体大小上限（字节）。Werkzeug 在 Content-Length 超过该值时直接
# 拒绝请求，不会把请求体读进内存/落盘。
# 设为 0 表示不限制——但那样任何人都能用 /api/cases/import 打满磁盘，
# 因此默认给一个对 YAML/Excel 用例文件足够宽松的硬上限（32MB）。
MAX_UPLOAD_BYTES: int = 32 * 1024 * 1024


class Config:
    """
    配置基类

    所有环境共享的通用配置项，子类可按覆盖。
    配置读取优先级: 环境变量 > 类属性默认值

    属性:
        SECRET_KEY (str): Flask应用密钥。生产环境必须从环境变量 TM_SECRET_KEY 读取
        MAX_CONTENT_LENGTH (int): 单请求体大小上限（字节）
        JSON_AS_ASCII / JSON_SORT_KEYS:
            已移除——这两个键在 Flask 2.3 被废弃（迁往 app.json.ensure_ascii /
            app.json.sort_keys），设置它们不再产生任何效果。中文响应可读性由
            Flask 2.3+ 的默认行为保证（ensure_ascii 默认 False）。
        TESTING (bool): 是否测试模式，影响Flask异常处理行为
        DEBUG (bool): 是否调试模式，影响自动重载与错误页面
    """

    # Flask安全密钥: 延迟到工厂阶段解析，不在类定义期求值。
    # 原因：类属性在 import 时求值一次，若多 worker(gunicorn/uwsgi) 部署且未
    # 配置 TM_SECRET_KEY，各进程会各自生成不同的随机 key，导致跨进程的
    # session/签名互相无法校验。
    SECRET_KEY: str | None = None

    # 上传大小硬上限：/api/cases/import 的 YAML/Excel 走 multipart 上传，
    # 无上限时任何人可上传超大文件打满磁盘（平台当前无鉴权，风险直接可利用）
    MAX_CONTENT_LENGTH: int = MAX_UPLOAD_BYTES

    # 测试与调试模式: 子类覆盖
    TESTING: bool = False
    DEBUG: bool = False


class DevelopmentConfig(Config):
    """
    开发环境配置

    适用于本地开发调试，开启DEBUG与详细错误页面。
    """

    DEBUG: bool = True
    TESTING: bool = False


class TestingConfig(Config):
    """
    测试环境配置

    适用于pytest测试套件，开启TESTING模式（Flask异常不隐藏）。
    数据库使用内存SQLite避免磁盘IO与残留。

    注意: 原先在此声明的 TM_DB_TYPE / TM_DB_SQLITE_PATH 是**死配置**——
    数据库层（src/db/db_session.py）经 env_manager 读 os.environ，从不读
    Flask app.config。测试实际通过 monkeypatch.setenv 覆盖环境变量生效，
    因此这两个键已删除，避免"测试用内存库"这一错误认知继续传播。
    """

    DEBUG: bool = True
    TESTING: bool = True


class ProductionConfig(Config):
    """
    生产环境配置

    关闭DEBUG，要求SECRET_KEY必须从环境变量设置。
    """

    DEBUG: bool = False
    TESTING: bool = False


# 配置名到配置类的映射表
config_map: dict[str, type[Config]] = {
    "dev": DevelopmentConfig,
    "test": TestingConfig,
    "prod": ProductionConfig,
}


def resolve_secret_key(config_class: type[Config]) -> str:
    """
    在应用工厂阶段解析 Flask SECRET_KEY（不在类定义期求值）

    解析规则:
        1. TM_SECRET_KEY 环境变量存在且非空 -> 直接使用（生产唯一正确姿势）
        2. 生产环境未配置 -> 抛 ValueError **fail-fast 拒绝启动**
        3. dev/test 环境未配置 -> 生成进程级随机 key（本地开发零配置可用）

    为什么延后到工厂阶段: 类属性在 import 时求值，多 worker 部署下各进程
    会得到不同的随机 key，导致跨进程 session/签名互相无法校验。

    为什么生产必须 fail-fast: 随机 key 每次重启都变、且多 worker 各不相同，
    session 在进程间与重启后必然失效；这属于"配置错了却在运行中悄悄降级"的
    最坏形态——服务看着是好的，登录态却随机失效。启动即失败把问题暴露在
    部署时刻，比运行后排查便宜得多。dev/test 保留随机 key 是刻意的：
    本地零配置即可跑通，不应为开发体验强制配置。

    参数:
        config_class (type[Config]): 当前生效的配置类，用于判断是否生产环境

    返回:
        str: 可用的 SECRET_KEY

    异常:
        ValueError: 生产环境未配置 TM_SECRET_KEY 时抛出，阻断启动
    """
    configured = os.getenv("TM_SECRET_KEY", "").strip()
    if configured:
        return configured

    # 正向判定（v3 修复 V2-P2-6）: 原写法是 `config_class is ProductionConfig`
    # ——"生产"由配置类**身份**决定，将来新增 StagingConfig 之类的档位会
    # 静默落到"允许随机密钥"一侧。改为"只要不是明确的开发/测试档就要求
    # 显式配置"，新增档位默认走严格侧，方向上更安全。
    #
    # **残留缺口（未闭合，需产品决策）**: "生产部署漏配 TM_ENV"这条最常见
    # 的事故路径**仍然覆盖不到**——get_config 对缺失 TM_ENV 缺省为 "dev"，
    # 于是拿到 DevelopmentConfig，本判定不触发，退回随机密钥，且
    # DevelopmentConfig.DEBUG=True 还会一并开启 Werkzeug 调试器。要同时
    # 补上这个缺口又保住"本地零配置即可跑通"（本函数下方明确的设计意图），
    # 只能引入 TM_ENV 之外的第二个生产判据（如显式 TM_DEPLOY_TARGET，
    # 或检测到非 loopback 的部署形态），那属于新增配置契约，不在本轮范围。
    if config_class not in (DevelopmentConfig, TestingConfig):
        raise ValueError(
            "非开发/测试环境必须显式配置 TM_SECRET_KEY：缺失时框架会退化为"
            "进程级随机密钥，服务每次重启即失效，且多 worker 部署下各进程"
            "密钥互不相同，session 必然随机失效。请在 .env 中设置该变量后重启。"
            f"（当前生效配置类: {config_class.__name__}）"
        )

    return secrets.token_hex(32)


def get_config(env_name: str | None = None) -> type[Config]:
    """
    根据环境名获取对应的配置类

    参数:
        env_name (str | None): 环境名（dev/test/prod），不传则从 TM_ENV 环境变量读取

    返回:
        Type[Config]: 对应环境的配置类，默认返回 DevelopmentConfig

    异常:
        ValueError: 传入的环境名不在 config_map 中时抛出
    """
    if env_name is None:
        # 缺省 TM_ENV 是 v5 补的告警点（见下方说明）：这条路径正是
        # "生产漏配 TM_ENV"的入口，必须让它**响亮**而不是静默
        raw_env = os.getenv("TM_ENV")
        env_name = raw_env if raw_env else "dev"
        if not raw_env:
            logger.warning(
                "TM_ENV 未显式配置，已按 dev 缺省：DEBUG 将开启且 SECRET_KEY "
                "退化为进程级随机密钥（多 worker 部署下各进程密钥互不相同，"
                "session 会随机失效）。生产部署请显式设置 TM_ENV=prod 并"
                "配置 TM_SECRET_KEY。"
            )

    if env_name not in config_map:
        raise ValueError(
            f"不支持的环境: {env_name}，可选值: {list(config_map.keys())}"
        )

    return config_map[env_name]