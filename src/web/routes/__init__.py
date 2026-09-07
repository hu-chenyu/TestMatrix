"""
Flask蓝图路由包

设计:
    - base_bp:        基础接口（首页/健康检查/版本信息），URL前缀 /
    - cases_bp:       用例管理API，URL前缀 /api/cases
    - executions_bp:  执行记录API，URL前缀 /api/executions
    - reports_bp:     报告API，URL前缀 /api/reports

蓝图拆分原则:
    按业务领域垂直拆分，每个蓝图独立管理自己的路由，
    避免单一路由文件过大，便于后续团队协作与功能扩展。
"""

from src.web.routes.base import base_bp
from src.web.routes.cases import cases_bp
from src.web.routes.executions import executions_bp
from src.web.routes.reports import reports_bp

__all__ = ["base_bp", "cases_bp", "executions_bp", "reports_bp"]