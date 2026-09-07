"""
TestMatrix Day18: 统一响应封装与全局异常处理测试

测试覆盖:
    1. 响应封装: success/error/created的默认值与自定义参数
    2. 业务异常: NotFoundError/ValidationError触发统一JSON响应
    3. 兜底异常: 未处理RuntimeError返回500且不泄露堆栈（TESTING/生产双模式）
    4. 健康检查: 数据库连通（healthy）与断连降级（degraded+503）
    5. 格式统一: 全部路由响应均含code/message/data三字段
"""

from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest
from flask import Flask
from sqlalchemy.exc import OperationalError

from src.db.db_session import DatabaseSession
from src.web import NotFoundError, ValidationError, create_app
from src.web.response import created, error, success


# ===========================================================================
# 测试夹具
# ===========================================================================
@pytest.fixture
def sqlite_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """
    临时SQLite数据库环境（前后重置引擎单例）

    将数据库指向pytest临时目录，测试后重置DatabaseSession引擎，
    避免Windows下SQLite文件句柄残留导致tmp_path清理失败，
    也避免污染项目默认数据库文件。

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest临时目录

    返回:
        Iterator[None]: yield空值，前后完成引擎重置
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "health_probe.db"))
    DatabaseSession.reset()
    yield
    DatabaseSession.reset()


def _create_exception_app(config_name: str = "test") -> Flask:
    """
    创建带异常触发路由的测试应用

    在标准应用上追加三个直接raise的触发路由，
    用于验证全局异常处理器的捕获与响应封装。

    参数:
        config_name (str): 配置环境名（test/prod），默认test

    返回:
        Flask: 追加了异常触发路由的应用实例
    """
    app = create_app(config_name)

    @app.route("/api/trigger/not-found")
    def trigger_not_found():
        raise NotFoundError()

    @app.route("/api/trigger/validation")
    def trigger_validation():
        raise ValidationError("字段缺失")

    @app.route("/api/trigger/runtime")
    def trigger_runtime():
        raise RuntimeError("意外错误")

    return app


# ===========================================================================
# 响应封装函数测试
# ===========================================================================
class TestResponseHelpers:
    """统一响应封装函数测试（纯函数级，无需应用上下文）"""

    def test_success_response_default(self) -> None:
        """
        测试success()默认参数: 返回code=200, message="ok", data=None
        """
        payload, status = success()

        assert status == 200, "默认HTTP状态码应为200"
        assert payload == {"code": 200, "message": "ok", "data": None}

    def test_success_response_with_data(self) -> None:
        """
        测试success(data=...): 返回正确的data负载
        """
        payload, status = success(data={"key": "value"})

        assert status == 200
        assert payload["code"] == 200
        assert payload["data"] == {"key": "value"}

    def test_error_response(self) -> None:
        """
        测试error("bad request", 400): 返回code=400与正确message
        """
        payload, status = error("bad request", 400)

        assert status == 400, "error应透传HTTP状态码400"
        assert payload["code"] == 400
        assert payload["message"] == "bad request"
        assert payload["data"] is None

    def test_created_response(self) -> None:
        """
        测试created(): 资源创建成功返回code=201
        """
        payload, status = created()

        assert status == 201, "created应返回HTTP 201"
        assert payload["code"] == 201
        assert payload["message"] == "created"
        assert payload["data"] is None


# ===========================================================================
# 全局异常处理器测试
# ===========================================================================
class TestGlobalExceptionHandlers:
    """全局异常处理器测试（经test_client端到端验证）"""

    def test_api_error_handler(self) -> None:
        """
        测试业务异常捕获: 路由raise NotFoundError()，
        客户端收到HTTP 404与统一JSON格式（code/message/data）
        """
        client = _create_exception_app().test_client()

        response = client.get("/api/trigger/not-found")
        data = response.get_json()

        assert response.status_code == 404, "NotFoundError应返回HTTP 404"
        assert data["code"] == 404
        assert data["message"] == "资源不存在"
        assert data["data"] is None
        assert set(data.keys()) == {"code", "message", "data"}

    def test_validation_error_handler(self) -> None:
        """
        测试参数校验异常: raise ValidationError("字段缺失")，
        客户端收到HTTP 400与正确的message
        """
        client = _create_exception_app().test_client()

        response = client.get("/api/trigger/validation")
        data = response.get_json()

        assert response.status_code == 400, "ValidationError应返回HTTP 400"
        assert data["code"] == 400
        assert data["message"] == "字段缺失"

    def test_unhandled_exception_500(self) -> None:
        """
        测试兜底异常捕获: 路由raise RuntimeError("意外错误")，
        客户端收到HTTP 500且响应不泄露堆栈信息；
        生产模式（TESTING=False）下仅返回通用错误消息
        """
        # TESTING模式: 保留原始异常消息（便于调试），但不泄露堆栈
        test_client = _create_exception_app("test").test_client()
        response = test_client.get("/api/trigger/runtime")
        data = response.get_json()

        assert response.status_code == 500, "未处理异常应返回HTTP 500"
        assert data["code"] == 500
        body_text = response.get_data(as_text=True)
        assert "Traceback" not in body_text, "响应不得泄露堆栈信息"
        assert "RuntimeError" not in body_text, "响应不得泄露异常类名"

        # 生产模式: 只返回通用错误消息，不暴露原始异常细节
        prod_client = _create_exception_app("prod").test_client()
        prod_response = prod_client.get("/api/trigger/runtime")
        prod_data = prod_response.get_json()

        assert prod_response.status_code == 500
        assert prod_data["message"] == "服务器内部错误"
        assert "意外错误" not in prod_response.get_data(as_text=True), (
            "生产模式不得泄露原始异常消息"
        )


# ===========================================================================
# 健康检查数据库探测测试
# ===========================================================================
class TestHealthDatabaseProbe:
    """/health接口数据库连通性探测测试"""

    def test_health_database_connected(self, sqlite_env: None) -> None:
        """
        测试数据库连通正常: /health返回database=connected，
        status=healthy，HTTP 200
        """
        client = create_app("test").test_client()

        response = client.get("/health")
        data = response.get_json()

        assert response.status_code == 200, "数据库正常时健康检查应为200"
        assert data["data"]["status"] == "healthy"
        assert data["data"]["database"] == "connected"
        assert "timestamp" in data["data"]
        assert "env" in data["data"]

    def test_health_database_disconnected(self) -> None:
        """
        测试数据库连接失败: mock DatabaseSession.session_scope抛异常，
        /health返回database=disconnected，status=degraded，HTTP 503
        """
        client = create_app("test").test_client()
        db_error = OperationalError(
            "SELECT 1", {}, Exception("数据库连接失败")
        )

        with patch(
            "src.web.routes.base.DatabaseSession.session_scope",
            side_effect=db_error,
        ):
            response = client.get("/health")

        data = response.get_json()

        assert response.status_code == 503, "数据库断连时健康检查应为503"
        assert data["code"] == 503
        assert data["data"]["status"] == "degraded"
        assert data["data"]["database"] == "disconnected"
        assert data["data"]["error"], "降级响应应携带非空错误摘要"


# ===========================================================================
# 全路由统一格式测试
# ===========================================================================
class TestUnifiedResponseFormat:
    """全路由统一响应格式测试"""

    def test_all_routes_use_unified_format(self, sqlite_env: None) -> None:
        """
        测试统一格式: 访问/、/health、/api/version、/api/cases，
        所有响应均包含code/message/data三个字段且code与HTTP状态码一致
        """
        client = create_app("test").test_client()

        for path in ("/", "/health", "/api/version", "/api/cases/"):
            response = client.get(path)
            data = response.get_json()

            assert response.status_code == 200, f"{path} 应返回200"
            assert data is not None, f"{path} 应返回JSON响应"
            assert {"code", "message", "data"} <= set(data.keys()), (
                f"{path} 响应应包含code/message/data三字段"
            )
            assert data["code"] == response.status_code, (
                f"{path} 响应体code应与HTTP状态码一致"
            )
