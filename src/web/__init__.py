"""
Flask Web可视化管理平台

    app.py        应用工厂主入口（create_app工厂函数）
    config.py     多环境配置（开发/测试/生产）
    response.py   统一响应封装（success/error/created/no_content）
    exceptions.py 业务异常类与全局异常处理器注册
    routes/       API路由模块（base/cases/executions/reports四个蓝图）
    static/       静态资源（CSS/JS/图片）
    templates/    Jinja2 HTML页面模板

第二阶段实现:
    - Flask应用工厂模式，支持多环境配置注入
    - 蓝图按业务领域拆分（用例/执行/报告）
    - 统一JSON响应格式与安全响应头
    - 全局异常处理（业务异常/HTTP异常/兜底捕获）与请求日志
    - 健康检查接口含数据库连通性探测（降级返回503）
"""

from src.web.app import create_app
from src.web.exceptions import (
    APIError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from src.web.response import created, error, no_content, success

__all__ = [
    "create_app",
    "success",
    "error",
    "created",
    "no_content",
    "APIError",
    "NotFoundError",
    "ValidationError",
    "UnauthorizedError",
    "ForbiddenError",
    "ConflictError",
]
