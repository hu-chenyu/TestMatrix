"""
Loguru日志统一封装模块

功能:
    - 控制台彩色输出 + 文件持久化双通道
    - 日志文件按天自动切割（每日0点），超期自动清理
    - 支持DEBUG/INFO/WARNING/ERROR分级过滤
    - 支持trace_id绑定，实现单用例全链路日志追踪
    - 单例初始化，保证进程内日志配置唯一

使用示例:
    from src.common.logger import LogManager

    LogManager.setup(log_level="INFO", log_dir="output/logs")
    logger = LogManager.get_logger()
    logger.info("服务启动成功")
    logger.bind(trace_id="TM-20260822-0001").info("带追踪ID的日志")
"""

import sys
from pathlib import Path
from typing import Union

from loguru import logger

# 项目根目录: env.py位于 src/common/ 下，向上两级即为项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class LogManager:
    """
    日志管理器（单例模式）

    负责Loguru的全局配置与logger分发。
    通过类方法访问，无需实例化，保证进程内配置只初始化一次。

    属性:
        _initialized (bool): 初始化状态标记，防止重复配置导致日志重复输出
        _log_dir (Path): 当前日志输出目录
        _level (str): 当前生效的日志级别
    """

    _initialized: bool = False
    _log_dir: Path = PROJECT_ROOT / "output" / "logs"
    _level: str = "INFO"

    @classmethod
    def setup(
        cls,
        log_level: str = "INFO",
        log_dir: Union[str, Path] = "output/logs",
        retention_days: int = 30,
        console_output: bool = True,
    ) -> None:
        """
        初始化全局日志配置（重复调用只生效一次）

        参数:
            log_level (str): 日志级别，可选DEBUG/INFO/WARNING/ERROR，默认INFO
            log_dir (str | Path): 日志输出目录，相对路径基于项目根目录，默认output/logs
            retention_days (int): 历史日志保留天数，超期自动清理，默认30天
            console_output (bool): 是否开启控制台输出，CI环境可关闭以减少噪音，默认True

        返回:
            None

        异常:
            PermissionError: 日志目录创建失败或无写权限时抛出，由调用方处理
            ValueError: 日志级别非法时抛出
        """
        if cls._initialized:
            logger.debug("LogManager已初始化，跳过重复配置")
            return

        # 校验日志级别合法性
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR"}
        log_level = log_level.upper()
        if log_level not in valid_levels:
            raise ValueError(f"非法日志级别: {log_level}，可选值: {valid_levels}")

        # 解析日志目录: 相对路径基于项目根，绝对路径直接使用
        log_path = Path(log_dir)
        cls._log_dir = log_path if log_path.is_absolute() else PROJECT_ROOT / log_path
        cls._level = log_level

        # 确保日志目录存在（含多级父目录）
        cls._log_dir.mkdir(parents=True, exist_ok=True)

        # 移除Loguru默认handler，避免默认配置干扰
        logger.remove()

        # 统一日志格式: 时间 | 级别 | 文件:行号 | 追踪ID | 消息
        log_format = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
            "<yellow>[{extra[trace_id]}]</yellow> | "
            "<level>{message}</level>"
        )

        # 控制台输出通道: 按配置级别过滤，彩色输出便于本地调试
        if console_output:
            logger.add(
                sys.stderr,
                level=log_level,
                format=log_format,
                filter=lambda record: record["extra"].setdefault("trace_id", "-") or True,
                enqueue=True,
            )

        # 文件输出通道: DEBUG全量落盘，按天切割，UTF-8编码，多进程写入安全
        logger.add(
            str(cls._log_dir / "testmatrix_{time:YYYY-MM-DD}.log"),
            level="DEBUG",
            format=log_format,
            rotation="00:00",
            retention=f"{retention_days} days",
            encoding="utf-8",
            filter=lambda record: record["extra"].setdefault("trace_id", "-") or True,
            enqueue=True,
            # 生产安全: 关闭变量值展开，防止敏感信息（密码/Token）泄露到日志
            diagnose=False,
            backtrace=False,
        )

        # 错误日志独立通道: 单独存放ERROR级别，便于快速定位故障
        logger.add(
            str(cls._log_dir / "error_{time:YYYY-MM-DD}.log"),
            level="ERROR",
            format=log_format,
            rotation="00:00",
            retention=f"{retention_days} days",
            encoding="utf-8",
            filter=lambda record: record["extra"].setdefault("trace_id", "-") or True,
            enqueue=True,
            diagnose=False,
        )

        cls._initialized = True
        logger.info(
            f"日志系统初始化完成 | 级别: {log_level} | "
            f"目录: {cls._log_dir} | 保留: {retention_days}天"
        )

    @classmethod
    def get_logger(cls):
        """
        获取全局logger对象

        参数:
            无

        返回:
            loguru.logger: 配置完成的全局logger实例，可直接调用info/debug/error等方法

        异常:
            无（若未调用setup，返回的logger仅使用Loguru默认配置）
        """
        return logger

    @classmethod
    def bind_trace_id(cls, trace_id: str):
        """
        绑定链路追踪ID，用于用例级日志追踪

        参数:
            trace_id (str): 追踪ID，建议格式: 用例编号或批次号，如 TM-API-0001

        返回:
            loguru.logger: 绑定trace_id后的logger实例，后续日志自动携带该ID

        异常:
            无
        """
        return logger.bind(trace_id=trace_id)

    @classmethod
    def get_log_dir(cls) -> Path:
        """
        获取当前日志输出目录

        参数:
            无

        返回:
            Path: 日志目录绝对路径对象
        """
        return cls._log_dir
