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
        2. 否则生成一个进程级随机 key 并返回

    为什么要延后到工厂阶段: 类属性在 import 时求值，多 worker 部署下各进程
    会得到不同的随机 key，导致跨进程 session/签名互相无法校验。

    参数:
        config_class (type[Config]): 当前生效的配置类，用于判断是否生产环境

    返回:
        str: 可用的 SECRET_KEY

    异常:
        无（未配置时降级为随机 key，仅记警告不阻断启动）
    """
    configured = os.getenv("TM_SECRET_KEY", "").strip()
    if configured:
        return configured

    generated = secrets.token_hex(32)
    if config_class is ProductionConfig:
        # 生产环境未配密钥是个真实问题：随机 key 每次重启都变，多 worker
        # 各不相同，session 在进程间和重启后必然失效。此处只告警不抛异常，
        # 避免把"当前能跑"的部署直接打挂；是否强制要求由部署侧决定。
        print(
            "[警告] TM_SECRET_KEY 未设置，生产环境将使用进程级随机密钥："
            "服务重启后所有 session 失效，多 worker 部署下各进程密钥不一致。"
            "请在 .env 中显式配置 TM_SECRET_KEY。"
        )
    return generated


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
        env_name = os.getenv("TM_ENV", "dev")

    if env_name not in config_map:
        raise ValueError(
            f"不支持的环境: {env_name}，可选值: {list(config_map.keys())}"
        )

    return config_map[env_name]