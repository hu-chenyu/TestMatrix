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
        ├── 注册全局错误处理器（404/500/400）
        ├── 注册请求钩子（before_request 日志 / after_request 安全头）
        └── 返回 app

使用示例:
    from src.web import create_app

    app = create_app()          # 默认从 TM_ENV 读取环境
    app = create_app("test")    # 显式指定测试环境

    # 开发服务器启动
    # python -c "from src.web import create_app; create_app().run(port=5000)"
"""

from flask import Flask, jsonify, request

from src.common.logger import LogManager
from src.web.config import get_config
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

    # 4. 注册全局错误处理器
    _register_error_handlers(app)

    # 5. 注册请求钩子
    _register_request_hooks(app)

    return app


def _register_error_handlers(app: Flask) -> None:
    """
    注册全局错误处理器，统一返回JSON格式

    参数:
        app (Flask): Flask应用实例

    返回:
        None
    """

    @app.errorhandler(404)
    def handle_404(error) -> tuple:
        """处理404 Not Found"""
        return (
            jsonify({"code": 404, "message": "请求的资源不存在", "data": None}),
            404,
        )

    @app.errorhandler(500)
    def handle_500(error) -> tuple:
        """处理500 Internal Server Error"""
        logger.error(f"服务器内部错误: {error}")
        return (
            jsonify({"code": 500, "message": "服务器内部错误", "data": None}),
            500,
        )

    @app.errorhandler(400)
    def handle_400(error) -> tuple:
        """处理400 Bad Request"""
        return (
            jsonify({"code": 400, "message": "请求参数错误", "data": None}),
            400,
        )


def _register_request_hooks(app: Flask) -> None:
    """
    注册请求前后钩子

    参数:
        app (Flask): Flask应用实例

    返回:
        None
    """

    @app.before_request
    def log_request() -> None:
        """记录每个请求的基本信息（method/path/remote_addr）"""
        logger.info(
            f"请求: {request.method} {request.path} "
            f"from {request.remote_addr}"
        )

    @app.after_request
    def add_security_headers(response):
        """为每个响应添加安全头"""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response