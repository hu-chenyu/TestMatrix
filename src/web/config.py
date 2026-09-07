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
from typing import Type


class Config:
    """
    配置基类

    所有环境共享的通用配置项，子类可按需覆盖。
    配置读取优先级: 环境变量 > 类属性默认值

    属性:
        SECRET_KEY (str): Flask应用密钥，生产环境必须从环境变量读取
        JSON_AS_ASCII (bool): JSON响应中是否转义非ASCII字符，False保留中文可读性
        JSON_SORT_KEYS (bool): JSON响应是否按键排序，False保持插入顺序
        TESTING (bool): 是否测试模式，影响Flask异常处理行为
        DEBUG (bool): 是否调试模式，影响自动重载与错误页面
    """

    # Flask安全密钥: 开发环境随机生成，生产环境必须通过 TM_SECRET_KEY 设置
    SECRET_KEY: str = os.getenv("TM_SECRET_KEY", secrets.token_hex(32))

    # JSON序列化配置: 保留中文可读性，不自动排序key
    JSON_AS_ASCII: bool = False
    JSON_SORT_KEYS: bool = False

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

    适用于pytest测试套件，开启TESTING模式（Flask异常不隐藏），
    数据库使用内存SQLite避免磁盘IO与残留。
    """

    DEBUG: bool = True
    TESTING: bool = True

    # 测试环境使用内存数据库，不污染磁盘
    TM_DB_TYPE: str = "sqlite"
    TM_DB_SQLITE_PATH: str = ":memory:"


class ProductionConfig(Config):
    """
    生产环境配置

    关闭DEBUG，要求SECRET_KEY必须从环境变量设置。
    """

    DEBUG: bool = False
    TESTING: bool = False


# 配置名到配置类的映射表
config_map: dict[str, Type[Config]] = {
    "dev": DevelopmentConfig,
    "test": TestingConfig,
    "prod": ProductionConfig,
}


def get_config(env_name: str | None = None) -> Type[Config]:
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