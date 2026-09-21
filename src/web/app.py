"""
Flask Web应用工厂（第二阶段实现）

应用工厂模式（Application Factory）好处:
    1. 可测试性: 通过 config_name 参数注入不同配置，测试环境与生产环境隔离
    2. 多环境支持: 同一套代码，通过 TM_ENV 切换 dev/test/prod 三套配置
    3. 可扩展性: create_app 是延迟初始化，注册蓝图/扩展在工厂内完成，
       避免循环导入，新增蓝图只需一行注册代码

架构:
    create_app(config_name) -> Flask 实例
        ├── 加载配置（Config/DevelopmentConfig/TestingConfig/ProductionConfig）
        ├── 注册蓝图（base / cases / executions / reports）
        ├── 注册全局异常处理器（APIError/404/405/500/Exception兜底，
        │   由exceptions.register_error_handlers统一提供）
        ├── 注册请求钩子（before_request 记请求日志与开始时间 /
        │   after_request 安全头 + 响应状态码与耗时配对日志）
        └── 返回 app

使用示例:
    from src.web import create_app

    app = create_app()          # 默认从 TM_ENV 读取环境
    app = create_app("test")    # 显式指定测试环境

    # 开发服务器启动
    # python -c "from src.web import create_app; create_app().run(port=5000)"
"""

import atexit
import time

from flask import Flask, g, request

from src.common.logger import LogManager
from src.core.task_queue import start_worker, stop_worker
from src.web.config import get_config
from src.web.exceptions import register_error_handlers
from src.web.routes import base_bp, cases_bp, executions_bp, reports_bp

logger = LogManager.get_logger()


def create_app(config_name: str | None = None) -> Flask:
    """
    应用工厂函数: 创建并配置Flask应用实例

    参数:
        config_name (str | None): 配置环境名（dev/test/prod），
                                 不传则从 TM_ENV 环境变量读取，默认dev

    返回:
        Flask: 配置完成的Flask应用实例，蓝图与错误处理器已注册

    异常:
        ValueError: 配置名非法时由 get_config 抛出
    """
    # 1. 初始化Flask实例
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    # 2. 加载配置
    config_class = get_config(config_name)
    app.config.from_object(config_class)

    logger.info(
        f"Flask应用工厂初始化完成 | 环境: {config_name or 'TM_ENV'} | "
        f"DEBUG={app.config['DEBUG']} | TESTING={app.config['TESTING']}"
    )

    # 3. 注册蓝图
    app.register_blueprint(base_bp)              # URL前缀: /
    app.register_blueprint(cases_bp)             # URL前缀: /api/cases
    app.register_blueprint(executions_bp)        # URL前缀: /api/executions
    app.register_blueprint(reports_bp)           # URL前缀: /api/reports

    logger.debug(
        f"已注册蓝图: {', '.join(app.blueprints.keys())}"
    )

    # 4. 注册全局异常处理器（APIError/404/405/500/Exception兜底，
    #    必须在蓝图注册之后，保证全部路由异常统一走JSON响应）
    register_error_handlers(app)

    # 5. 注册请求钩子
    _register_request_hooks(app)

    # 6. 任务队列worker启停（Day32）: 仅TM_TASK_WORKER_ENABLED=true
    #    时start_worker内部实际创建daemon消费线程，默认关闭时
    #    返回None零开销；atexit兜底保证进程退出时通知worker停止
    #    （幂等，测试中反复create_app/stop_worker也安全）
    start_worker()
    atexit.register(stop_worker)

    return app


def _register_request_hooks(app: Flask) -> None:
    """
    注册请求前后钩子（Day29改造: 请求/响应日志配对 + 安全头合并）

    钩子职责:
        - before_request: 记"请求:"入口日志（method/path/remote_addr），
          同时用flask.g存高精度开始时间，供after_request计算耗时
        - after_request: 一个函数内完成两件事——补两个安全响应头；
          记"响应:"配对日志（状态码/耗时/客户端地址），与入口日志
          按method+path一一对应，形成单请求全链路日志

    参数:
        app (Flask): Flask应用实例

    返回:
        None
    """

    @app.before_request
    def log_request() -> None:
        """
        请求前置钩子: 记录入口日志并保存开始时间

        入参:
            无（从flask.request与flask.g读取/写入请求上下文）

        返回:
            None

        说明:
            g.request_start使用time.perf_counter()高精度单调时钟，
            仅用于同请求内的耗时差值计算，不携带墙钟语义
        """
        # 入口日志保持既有格式不变（历史日志检索口径不被破坏）
        logger.info(
            f"请求: {request.method} {request.path} "
            f"from {request.remote_addr}"
        )
        # 开始时间挂到g上: g为单请求生命周期对象，after_request可直接读取
        g.request_start = time.perf_counter()

    @app.after_request
    def process_response(response):
        """
        响应后置钩子: 添加安全头并记录响应配对日志（安全头+日志合一）

        入参:
            response (flask.Response): 视图函数或错误处理器产出的
                                       响应对象（可原地修改响应头）

        返回:
            flask.Response: 补齐安全头后的原响应对象

        说明:
            - 耗时 = 当前perf_counter - g.request_start，转毫秒保留1位；
              g.request_start可能不存在（异常发生在before_request之前
              等极端场景），用getattr兜底为None，此时耗时字段打"-"
            - SSE流式接口（/api/executions/<id>/events）的after_request
              在流生成器完整跑完后才触发，耗时长属正常现象，不做过滤
        """
        # 1. 安全响应头（保持既有两行原样，浏览器侧基础防护）
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"

        # 2. 取请求开始时间（不存在时None兜底，绝不因日志缺计时搞挂响应）
        request_start = getattr(g, "request_start", None)
        if request_start is None:
            # before_request未正常执行: 耗时不可计算，打"-"占位保持日志结构
            duration_text = "-"
        else:
            # perf_counter差值单位为秒，乘1000转毫秒并保留1位小数
            duration_ms = (time.perf_counter() - request_start) * 1000
            duration_text = f"{duration_ms:.1f}ms"

        # 3. 响应配对日志: 与"请求:"日志同method/path，额外携带状态码与耗时
        logger.info(
            f"响应: {request.method} {request.path} "
            f"-> {response.status_code}, 耗时 {duration_text} "
            f"from {request.remote_addr}"
        )
        return response