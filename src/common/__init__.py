"""
公共底层封装层

提供全项目复用的通用基础能力:
    logger.py        Loguru日志统一封装
    env_manager.py   多环境配置管理
    http_client.py   HTTP请求统一封装
    serial_client.py 串口通信封装（芯片板卡适配）
    telnet_client.py Telnet网口封装（芯片板卡适配）
    assertion.py     通用增强断言库

注意: 为避免模块间循环依赖与环境未装依赖时的导入崩溃,
统一使用完整路径导入, 例如: from src.common.logger import LogManager
"""
