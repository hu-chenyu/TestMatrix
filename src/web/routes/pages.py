"""
HTML页面路由蓝图（Day35 前端骨架）

提供:
    GET /dashboard   质量看板页面
    GET /cases       用例管理页面
    GET /executions  执行记录页面

设计边界（铁律）:
    1. HTML 页面路由与 JSON 数据接口严格分离：页面路径挂根路径，
       数据接口统一在 /api/* 下，两者不得冲突；
    2. 视图只负责 render_template 渲染模板，不写业务逻辑、不直接
       操作数据库——页面所需数据全部由前端 JS（static/js/api.js）
       异步调用 /api 接口获取，Day36 起逐步接入；
    3. 每个视图注入 active_nav 供基模板导航栏高亮当前项，
       page_title 供子模板/后续组件展示页面名称。
"""

from flask import Blueprint, render_template

pages_bp = Blueprint("pages", __name__)


@pages_bp.route("/dashboard")
def dashboard() -> str:
    """
    渲染质量看板页面

    参数:
        无（纯页面路由，不接收业务查询参数）

    返回:
        str: 渲染后的 HTML 文档（pages/dashboard.html 继承 base.html）
    """
    # active_nav 与基模板导航判断严格对应：dashboard → 质量看板高亮
    return render_template(
        "pages/dashboard.html",
        active_nav="dashboard",
        page_title="质量看板",
    )


@pages_bp.route("/cases")
def cases() -> str:
    """
    渲染用例管理页面

    参数:
        无（列表筛选条件由 Day41+ 前端 JS 通过查询参数异步请求 /api/cases）

    返回:
        str: 渲染后的 HTML 文档（pages/cases.html 继承 base.html）
    """
    # active_nav 与基模板导航判断严格对应：cases → 用例管理高亮
    return render_template(
        "pages/cases.html",
        active_nav="cases",
        page_title="用例管理",
    )


@pages_bp.route("/executions")
def executions() -> str:
    """
    渲染执行记录页面

    参数:
        无（批次列表数据由 Day46+ 前端 JS 异步请求 /api/executions）

    返回:
        str: 渲染后的 HTML 文档（pages/executions.html 继承 base.html）
    """
    # active_nav 与基模板导航判断严格对应：executions → 执行记录高亮
    return render_template(
        "pages/executions.html",
        active_nav="executions",
        page_title="执行记录",
    )
