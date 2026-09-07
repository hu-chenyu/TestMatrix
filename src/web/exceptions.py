"""
全局异常处理模块

功能:
    - APIError业务异常基类与五个具体业务异常类（404/400/401/403/409）
    - register_error_handlers将全局异常处理器注册到Flask应用
    - 所有异常响应经response.error统一封装:
      {"code": int, "message": str, "data": any}

异常处理分层（Flask按异常类型精确匹配优先）:
    1. APIError及其子类: 视图中raise的业务异常，返回统一error封装
    2. 404/405/500 HTTP异常: 统一JSON格式（覆盖Flask默认HTML错误页）
    3. Exception兜底: 记录完整traceback到日志，返回500

TESTING模式说明:
    - 测试模式（TESTING=True）下500类响应保留原始异常信息（str(exc)），
      便于联调调试，但仅限异常消息，不含堆栈
    - 生产模式（TESTING=False）下500类响应只返回"服务器内部错误"，
      不向客户端泄露任何内部实现细节

使用示例:
    from src.web.exceptions import NotFoundError, ValidationError

    @bp.route("/cases/<int:case_id>")
    def get_case(case_id):
        case = query_case(case_id)
        if case is None:
            raise NotFoundError(detail={"case_id": case_id})
        return success(data=case)
"""

import traceback
from typing import Any

from flask import Flask

from src.common.logger import LogManager
from src.web.response import error

logger = LogManager.get_logger()


class APIError(Exception):
    """
    业务异常基类

    所有业务异常继承此类；全局异常处理器捕获后以统一JSON格式
    返回给客户端，视图代码通过raise表达业务失败，无需手写响应。

    属性:
        message (str): 错误描述信息（面向客户端可读）
        code (int): HTTP状态码
        detail (Any): 可选错误详情（如校验失败字段列表），放入响应data
    """

    def __init__(
        self, message: str, code: int = 400, detail: Any = None
    ) -> None:
        """
        初始化业务异常

        参数:
            message (str): 错误描述信息
            code (int): HTTP状态码，默认400
            detail (Any): 可选错误详情，默认None

        返回:
            无
        """
        self.message = message
        self.code = code
        self.detail = detail
        super().__init__(message)


class NotFoundError(APIError):
    """资源不存在异常（404），查无数据时抛出"""

    def __init__(
        self, message: str = "资源不存在", code: int = 404, detail: Any = None
    ) -> None:
        super().__init__(message, code, detail)


class ValidationError(APIError):
    """参数校验失败异常（400），请求参数不合法时抛出"""

    def __init__(
        self, message: str = "参数校验失败", code: int = 400, detail: Any = None
    ) -> None:
        super().__init__(message, code, detail)


class UnauthorizedError(APIError):
    """未授权异常（401），未登录或令牌失效时抛出"""

    def __init__(
        self, message: str = "未授权", code: int = 401, detail: Any = None
    ) -> None:
        super().__init__(message, code, detail)


class ForbiddenError(APIError):
    """无权限异常（403），已登录但权限不足时抛出"""

    def __init__(
        self, message: str = "无权限", code: int = 403, detail: Any = None
    ) -> None:
        super().__init__(message, code, detail)


class ConflictError(APIError):
    """资源冲突异常（409），唯一约束冲突/重复创建时抛出"""

    def __init__(
        self, message: str = "资源冲突", code: int = 409, detail: Any = None
    ) -> None:
        super().__init__(message, code, detail)


def register_error_handlers(app: Flask) -> None:
    """
    注册全局异常处理器到Flask应用

    在应用工厂中于蓝图注册之后调用，保证全部路由的异常
    都能被统一捕获并转换为标准JSON响应。

    参数:
        app (Flask): Flask应用实例

    返回:
        无
    """

    @app.errorhandler(APIError)
    def handle_api_error(exc: APIError) -> tuple[dict[str, Any], int]:
        """捕获业务异常: 返回统一error封装（detail作为响应data）"""
        logger.warning(
            f"业务异常 | code={exc.code} | message={exc.message} | "
            f"detail={exc.detail}"
        )
        return error(exc.message, exc.code, exc.detail)

    @app.errorhandler(404)
    def handle_404(exc: Exception) -> tuple[dict[str, Any], int]:
        """捕获404: 请求的资源不存在（覆盖Flask默认HTML错误页）"""
        return error("请求的资源不存在", 404)

    @app.errorhandler(405)
    def handle_405(exc: Exception) -> tuple[dict[str, Any], int]:
        """捕获405: 请求方法不被允许"""
        return error("请求方法不被允许", 405)

    @app.errorhandler(500)
    def handle_500(exc: Exception) -> tuple[dict[str, Any], int]:
        """
        捕获500: 服务器内部错误

        Flask传入的通常是包装后的InternalServerError，
        其original_exception属性为原始未处理异常。
        """
        original = getattr(exc, "original_exception", None)
        logger.error(f"服务器内部错误 | 原始异常: {original or exc}")
        if app.config.get("TESTING") and original is not None:
            return error(str(original), 500)
        return error("服务器内部错误", 500)

    @app.errorhandler(Exception)
    def handle_unexpected_error(
        exc: Exception,
    ) -> tuple[dict[str, Any], int]:
        """
        兜底捕获所有未处理异常

        记录完整traceback到日志（含异常链），
        响应不泄露内部实现细节（堆栈/类名/文件路径）。
        """
        logger.error(
            f"未处理异常被兜底捕获 | 类型: {type(exc).__name__} | "
            f"信息: {exc}\n"
            f"{traceback.format_exception(type(exc), exc, exc.__traceback__)}"
        )
        if app.config.get("TESTING"):
            return error(str(exc), 500)
        return error("服务器内部错误", 500)
