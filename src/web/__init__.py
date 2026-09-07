"""
Flask Web可视化管理平台

    app.py      应用工厂主入口（create_app工厂函数）
    config.py   多环境配置（开发/测试/生产）
    routes/     API路由模块（base/cases/executions/reports四个蓝图）
    static/     静态资源（CSS/JS/图片）
    templates/  Jinja2 HTML页面模板

第二阶段实现:
    - Flask应用工厂模式，支持多环境配置注入
    - 蓝图按业务领域拆分（用例/执行/报告）
    - 统一JSON响应格式与安全响应头
    - 全局错误处理与请求日志
"""

from src.web.app import create_app

__all__ = ["create_app"]