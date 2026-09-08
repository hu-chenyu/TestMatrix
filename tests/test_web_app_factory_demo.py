"""
TestMatrix Day17: Flask应用工厂与蓝图架构测试

测试覆盖:
    1. 应用工厂: 默认/测试/生产三环境配置注入
    2. 环境变量: TM_ENV 驱动配置加载
    3. 蓝图注册: 四个蓝图(base/cases/executions/reports)正确注册
    4. 接口响应: 首页/健康检查/用例列表接口JSON格式正确
    5. 错误处理: 404返回JSON而非HTML错误页
    6. 安全响应头: X-Content-Type-Options/X-Frame-Options
"""

import os

import pytest
from flask import Flask

from src.db.db_session import DatabaseSession
from src.web import create_app
from src.web.config import DevelopmentConfig, TestingConfig, ProductionConfig


class TestWebAppFactory:
    """应用工厂函数测试"""

    def test_create_app_default(self) -> None:
        """
        测试默认创建app: 不传参，确认app是Flask实例，配置为DevelopmentConfig
        """
        app = create_app()

        assert isinstance(app, Flask), "create_app() 应返回Flask实例"
        assert app.config["DEBUG"] is True, "默认配置DEBUG应为True"
        assert app.config["TESTING"] is False, "默认配置TESTING应为False"

    def test_create_app_testing(self) -> None:
        """
        测试显式传入config_name="test": 确认TESTING=True，DEBUG=True
        """
        app = create_app("test")

        assert app.config["TESTING"] is True, "测试环境TESTING应为True"
        assert app.config["DEBUG"] is True, "测试环境DEBUG应为True"

    def test_create_app_production(self) -> None:
        """
        测试传入config_name="prod": 确认DEBUG=False，TESTING=False
        """
        app = create_app("prod")

        assert app.config["DEBUG"] is False, "生产环境DEBUG应为False"
        assert app.config["TESTING"] is False, "生产环境TESTING应为False"

    def test_config_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        测试通过TM_ENV环境变量加载配置: monkeypatch设置TM_ENV=test，
        create_app()应加载TestingConfig
        """
        monkeypatch.setenv("TM_ENV", "test")

        app = create_app()

        assert app.config["TESTING"] is True, "TM_ENV=test 应加载TestingConfig"
        assert app.config["DEBUG"] is True, "TM_ENV=test 时DEBUG应为True"

        monkeypatch.delenv("TM_ENV", raising=False)

    def test_blueprints_registered(self) -> None:
        """
        测试蓝图注册: 创建app后，app.blueprints应包含四个蓝图
        """
        app = create_app()

        registered = app.blueprints.keys()
        assert "base" in registered, "base蓝图应已注册"
        assert "cases" in registered, "cases蓝图应已注册"
        assert "executions" in registered, "executions蓝图应已注册"
        assert "reports" in registered, "reports蓝图应已注册"


class TestWebEndpoints:
    """Web接口端点测试"""

    @pytest.fixture
    def client(self):
        """创建测试客户端"""
        app = create_app("test")
        return app.test_client()

    def test_root_endpoint(self, client) -> None:
        """
        测试GET /: 确认status_code=200，JSON含version和status
        """
        response = client.get("/")
        data = response.get_json()

        assert response.status_code == 200, "首页应返回200"
        assert data["message"] == "TestMatrix API", "message应为TestMatrix API"
        assert data["data"]["version"] == "1.0.0", "version应为1.0.0"
        assert data["data"]["status"] == "running", "status应为running"

    def test_health_endpoint(self, client) -> None:
        """
        测试GET /health: 确认status_code=200，JSON含status=healthy和timestamp
        """
        response = client.get("/health")
        data = response.get_json()

        assert response.status_code == 200, "健康检查应返回200"
        assert data["data"]["status"] == "healthy", "health状态应为healthy"
        assert "timestamp" in data["data"], "health应包含timestamp"
        assert "env" in data["data"], "health应包含env"

    def test_cases_list_endpoint(
        self, client, tmp_path, monkeypatch, request
    ) -> None:
        """
        测试GET /api/cases: 确认200，返回统一格式且data含
        items/total分页字段（Day19占位接口替换为真实实现，
        断言同步更新；临时SQLite库隔离，空库查询返回0条）
        """
        monkeypatch.setenv("TM_DB_TYPE", "sqlite")
        monkeypatch.setenv(
            "TM_DB_SQLITE_PATH", str(tmp_path / "cases_list_endpoint.db")
        )
        DatabaseSession.reset()
        DatabaseSession.init_db()
        request.addfinalizer(DatabaseSession.reset)

        response = client.get("/api/cases/")
        data = response.get_json()

        assert response.status_code == 200, "用例列表接口应返回200"
        assert data["code"] == 200, "响应体code应为200"
        assert "items" in data["data"], "data应包含items分页字段"
        assert "total" in data["data"], "data应包含total分页字段"
        assert data["data"]["total"] == 0, "空库默认查询应为0条"

    def test_404_handler(self, client) -> None:
        """
        测试404错误处理: GET /nonexistent返回JSON格式(code=404)，
        非Flask默认HTML错误页
        """
        response = client.get("/nonexistent")
        data = response.get_json()

        assert response.status_code == 404, "不存在的路径应返回404"
        assert data is not None, "404响应应为JSON格式"
        assert data["code"] == 404, "JSON中code应为404"
        assert data["data"] is None, "JSON中data应为null"

    def test_security_headers(self, client) -> None:
        """
        测试安全响应头: GET /health确认响应头含X-Content-Type-Options
        和X-Frame-Options
        """
        response = client.get("/health")

        assert (
            response.headers.get("X-Content-Type-Options") == "nosniff"
        ), "应包含X-Content-Type-Options: nosniff"
        assert (
            response.headers.get("X-Frame-Options") == "SAMEORIGIN"
        ), "应包含X-Frame-Options: SAMEORIGIN"