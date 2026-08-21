"""
多环境配置管理模块

功能:
    - 基于python-dotenv加载.env配置文件，敏感信息与代码分离
    - 支持dev/test/prod多环境一键切换（TM_ENV变量控制）
    - 配置读取优先级: 系统环境变量 > .env文件 > 内置默认值
    - 提供类型安全的取值方法（get/get_int/get_bool/get_float）

使用示例:
    from src.common.env_manager import env_manager

    env_manager.get("TM_BASE_URL")            # 读取字符串配置
    env_manager.get_int("TM_HTTP_TIMEOUT")    # 读取整型配置
    env_manager.base_url                      # 读取被测服务地址
"""

import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

# 项目根目录: 本模块位于 src/common/ 下，向上两级即为项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class EnvManager:
    """
    多环境配置管理器（单例模式）

    统一管理项目全部配置项的读取与类型转换，
    模块级别导出单例 env_manager，全项目共享同一份配置。

    属性:
        _env_file (Path): .env配置文件路径（项目根目录下）
        _loaded (bool): 配置文件加载状态标记
    """

    # 支持的合法环境列表
    VALID_ENVS = ("dev", "test", "prod")

    def __init__(self, env_file: Optional[str] = None):
        """
        初始化配置管理器

        参数:
            env_file (str | None): .env文件路径，默认使用项目根目录下的.env

        返回:
            无

        异常:
            无（.env文件不存在时静默降级，全部走系统环境变量与默认值）
        """
        self._env_file = Path(env_file) if env_file else PROJECT_ROOT / ".env"
        self._loaded = False
        self._load_env_file()

    def _load_env_file(self) -> None:
        """
        加载.env配置文件到进程环境

        参数:
            无

        返回:
            无

        异常:
            无（文件不存在或解析失败仅记录警告，不阻断启动；
                override=False保证系统环境变量优先级高于文件值）
        """
        if self._env_file.exists():
            load_dotenv(self._env_file, override=False)
            self._loaded = True
        else:
            # .env缺失属于正常场景（如CI环境全部用系统变量），静默处理
            pass

    # ------------------------------------------------------------------
    # 通用取值方法
    # ------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        """
        读取字符串配置项

        参数:
            key (str): 配置键名，约定统一使用TM_前缀
            default (Any): 键不存在时的默认返回值，默认None

        返回:
            Any: 配置值（字符串），无值时返回default

        异常:
            无
        """
        value = os.getenv(key)
        return value if value is not None and value != "" else default

    def get_int(self, key: str, default: int = 0) -> int:
        """
        读取整型配置项

        参数:
            key (str): 配置键名
            default (int): 键不存在或值非法时的默认值

        返回:
            int: 转换后的整型配置值

        异常:
            无（值无法转换为int时返回default，不抛出异常）
        """
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        """
        读取浮点型配置项

        参数:
            key (str): 配置键名
            default (float): 键不存在或值非法时的默认值

        返回:
            float: 转换后的浮点型配置值

        异常:
            无（值无法转换为float时返回default，不抛出异常）
        """
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        """
        读取布尔型配置项

        参数:
            key (str): 配置键名
            default (bool): 键不存在时的默认值

        返回:
            bool: 转换后的布尔值（'true'/'1'/'yes'为真，'false'/'0'/'no'为假）

        异常:
            无（值非法时返回default，不抛出异常）
        """
        raw = self.get(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in ("true", "1", "yes", "on")

    # ------------------------------------------------------------------
    # 业务语义配置属性
    # ------------------------------------------------------------------
    @property
    def current_env(self) -> str:
        """
        当前激活环境名

        参数:
            无

        返回:
            str: dev/test/prod，未配置时默认dev
        """
        env = self.get("TM_ENV", "dev")
        return env if env in self.VALID_ENVS else "dev"

    @property
    def base_url(self) -> str:
        """
        被测服务基础地址

        参数:
            无

        返回:
            str: HTTP接口测试目标服务基础URL
        """
        return self.get("TM_BASE_URL", "https://httpbin.org")

    @property
    def http_timeout(self) -> int:
        """
        HTTP请求统一超时时间（秒）

        参数:
            无

        返回:
            int: 超时秒数，默认10秒
        """
        return self.get_int("TM_HTTP_TIMEOUT", 10)

    @property
    def http_retries(self) -> int:
        """
        HTTP请求失败自动重试次数

        参数:
            无

        返回:
            int: 重试次数，默认2次
        """
        return self.get_int("TM_HTTP_RETRIES", 2)

    @property
    def log_level(self) -> str:
        """
        日志输出级别

        参数:
            无

        返回:
            str: DEBUG/INFO/WARNING/ERROR，默认INFO
        """
        return self.get("TM_LOG_LEVEL", "INFO")

    @property
    def log_dir(self) -> str:
        """
        日志输出目录

        参数:
            无

        返回:
            str: 日志目录路径（相对项目根），默认output/logs
        """
        return self.get("TM_LOG_DIR", "output/logs")

    def as_dict(self, keys: list) -> dict:
        """
        批量读取配置项为字典（用于日志打印运行时配置快照）

        参数:
            keys (list): 需要读取的配置键名列表

        返回:
            dict: {键名: 配置值} 结构的字典
        """
        return {key: self.get(key) for key in keys}


# 模块级单例: 全项目统一从此处导入，保证配置状态一致
env_manager = EnvManager()
