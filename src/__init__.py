"""
TestMatrix 通用自动化测试效能平台 - 核心源码包

分层结构:
    common/  公共底层封装层（HTTP/串口/Telnet通信、日志、断言、环境配置）
    db/      数据持久层（SQLAlchemy ORM模型与会话管理）
    core/    平台核心逻辑层（数据驱动、用例调度、报告解析）
    web/     Flask Web可视化管理平台（第二阶段实现）
"""

__version__ = "0.1.0"
