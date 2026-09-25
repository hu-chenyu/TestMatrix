"""
TestMatrix Day36: Dashboard 框架与统计 API 对接契约测试

测试覆盖:
    1. 看板页分区布局：4 个统计卡片数值位（statTotal/statPassRate/
       statBatches/statFailed）与 statsCardsRow 容器
    2. 三个 ECharts 图表容器（trendChart/modulePieChart/priorityBarChart）
    3. 失败用例 Top 榜表格（failedTopTable）
    4. ECharts5 CDN 标识与 chart-helper.js / dashboard.js 静态资源引用
    5. 两个新增 JS 静态资源可访问（200）
    6. 导航栏健康状态占位 id（healthStatus，供 dashboard.js 定位）
    7. /api/reports/summary 数据契约：空库 200 且含八个约定字段
    8. /health 数据契约：200 且 status=healthy、database=connected

边界说明:
    Day36 为前端框架日，仅校验页面结构与接口契约（字段存在即断言，
    不校验具体数值）；卡片数字/折线/饼柱/Top 榜的渲染由 Day37-39 覆盖。
"""

import pytest
from flask.testing import FlaskClient

from src.db.db_session import DatabaseSession
from src.web import create_app


class TestDashboardFramework:
    """Dashboard 框架布局与对接契约测试"""

    @pytest.fixture
    def client(self) -> FlaskClient:
        """
        创建测试客户端 fixture

        参数:
            无

        返回:
            FlaskClient: 基于 test 环境应用工厂的测试客户端
        """
        # 框架/契约测试无需业务数据，页面渲染不依赖数据库
        app = create_app("test")
        return app.test_client()

    @pytest.fixture
    def isolated_db(self, tmp_path, monkeypatch, request):
        """
        临时 SQLite 库隔离 fixture（供接口契约测试使用）

        参数:
            tmp_path (pathlib.Path): pytest 提供的临时目录
            monkeypatch (pytest.MonkeyPatch): 环境变量补丁
            request (pytest.FixtureRequest): 用于注册 teardown 收尾

        返回:
            None: 仅完成建库与重置；用例内直接经 client 访问接口
        """
        # 将数据库指向用例级临时文件并重置/初始化，避免跨用例串数据
        monkeypatch.setenv("TM_DB_TYPE", "sqlite")
        monkeypatch.setenv(
            "TM_DB_SQLITE_PATH", str(tmp_path / "dashboard_framework.db")
        )
        DatabaseSession.reset()
        DatabaseSession.init_db()

        # teardown 先 reset 释放连接，防 Windows 文件锁
        def _teardown() -> None:
            DatabaseSession.reset()

        request.addfinalizer(_teardown)

    def test_dashboard_stats_cards_layout(self, client: FlaskClient) -> None:
        """
        测试统计卡片区布局: GET /dashboard 应含 statsCardsRow 容器及
        累计执行/通过率/执行批次/失败数四个数值位 id（本日值为 “--”）
        """
        # 请求看板页并解码为文本
        response = client.get("/dashboard")
        html = response.data.decode("utf-8")

        # 页面 200 且为 HTML
        assert response.status_code == 200, "GET /dashboard 应返回200"
        assert "text/html" in response.content_type, "应返回HTML文档"

        # 卡片区容器与四个数值位必须全部存在（Day37 据此渲染数字）
        expected_ids = (
            "statsCardsRow",
            "statTotal",
            "statPassRate",
            "statBatches",
            "statFailed",
        )
        for element_id in expected_ids:
            assert f'id="{element_id}"' in html, (
                f"看板页应包含统计卡片元素 id={element_id}"
            )

    def test_dashboard_chart_containers_layout(
        self, client: FlaskClient
    ) -> None:
        """
        测试图表容器布局: 应含 trendChart/modulePieChart/priorityBarChart
        三个 ECharts 容器 id（本日初始化为空态）
        """
        # 请求看板页
        response = client.get("/dashboard")
        html = response.data.decode("utf-8")

        # 三个图表容器逐一断言
        expected_chart_ids = (
            "trendChart",
            "modulePieChart",
            "priorityBarChart",
        )
        for chart_id in expected_chart_ids:
            assert f'id="{chart_id}"' in html, (
                f"看板页应包含图表容器 id={chart_id}"
            )

    def test_dashboard_failed_top_layout(self, client: FlaskClient) -> None:
        """
        测试失败 Top 榜布局: 页面应含 failedTopTable 表格
        （Day39 才填充真实数据行）
        """
        # 请求看板页
        response = client.get("/dashboard")
        html = response.data.decode("utf-8")

        # 表格 id 存在即证明分区落地
        assert 'id="failedTopTable"' in html, (
            "看板页应包含失败Top榜表格 id=failedTopTable"
        )

    def test_dashboard_echarts_cdn_and_scripts(
        self, client: FlaskClient
    ) -> None:
        """
        测试 ECharts CDN 与子脚本引用: 页面应含 echarts CDN 标识及
        chart-helper.js、dashboard.js 的 url_for 静态路径，
        且 ECharts 仅由看板页 extra_js 引入（不进 base.html 全局）
        """
        # 请求看板页
        response = client.get("/dashboard")
        html = response.data.decode("utf-8")

        # ECharts5 CDN（5.5.1，仅本页 extra_js 加载）
        assert "cdn.jsdelivr.net/npm/echarts@5.5.1" in html, (
            "看板页应经CDN引入 echarts 5.5.1"
        )
        # 两个新增脚本的静态资源路径（url_for 产物）
        assert "/static/js/chart-helper.js" in html, (
            "看板页应引入 chart-helper.js"
        )
        assert "/static/js/dashboard.js" in html, (
            "看板页应引入 dashboard.js"
        )

        # ECharts 不得污染全局基模板：其他页面（用例管理）不应加载它
        other_response = client.get("/cases")
        other_html = other_response.data.decode("utf-8")
        assert "echarts" not in other_html, (
            "ECharts 不应在非看板页面（/cases）加载"
        )

    def test_dashboard_new_static_assets_accessible(
        self, client: FlaskClient
    ) -> None:
        """
        测试新增静态资源可访问: chart-helper.js 与 dashboard.js
        经 /static/ 均返回 200
        """
        # 两个新增 JS 资源路径
        asset_paths = (
            "/static/js/chart-helper.js",
            "/static/js/dashboard.js",
        )
        for asset_path in asset_paths:
            response = client.get(asset_path)
            assert response.status_code == 200, (
                f"静态资源 {asset_path} 应可访问（200）"
            )

    def test_dashboard_health_placeholder_id(
        self, client: FlaskClient
    ) -> None:
        """
        测试健康状态占位 id: 基模板导航栏右侧应含 id="healthStatus"，
        供 dashboard.js 的 loadHealthStatus 定位改写
        """
        # 请求看板页（基模板由所有页面共用）
        response = client.get("/dashboard")
        html = response.data.decode("utf-8")

        # 健康状态占位 id 必须存在
        assert 'id="healthStatus"' in html, (
            "导航栏健康状态占位应含 id=healthStatus"
        )

    def test_summary_contract_for_dashboard(
        self, client: FlaskClient, isolated_db
    ) -> None:
        """
        测试 summary 接口数据契约: 空库 GET /api/reports/summary 返回200，
        data 含八个约定字段（只断言字段存在，不校验数值；空库安全降级）
        """
        # 请求全局执行汇总接口
        response = client.get("/api/reports/summary")
        body = response.get_json()

        # HTTP 200 且统一响应体 code=200
        assert response.status_code == 200, "summary 接口应返回200"
        assert body["code"] == 200, "统一响应体 code 应为200"

        # data 必须包含看板需要的八个字段（字段名是前后端契约）
        data = body["data"]
        expected_fields = (
            "total_batches",
            "total_executed",
            "passed",
            "failed",
            "error",
            "skipped",
            "overall_pass_rate",
            "latest_batch",
        )
        for field in expected_fields:
            assert field in data, f"summary.data 应包含字段 {field}"

    def test_health_contract_for_dashboard(
        self, client: FlaskClient, isolated_db
    ) -> None:
        """
        测试 health 接口数据契约: 空库可连通时 GET /health 返回200，
        data 含 status/database/timestamp/env，且 status=healthy、
        database=connected（dashboard.js 据此渲染绿色“服务正常”）
        """
        # 请求健康检查接口
        response = client.get("/health")
        body = response.get_json()

        # 正常连通为 200
        assert response.status_code == 200, "数据库连通时 /health 应返回200"

        # 四个约定字段齐全
        data = body["data"]
        for field in ("status", "database", "timestamp", "env"):
            assert field in data, f"health.data 应包含字段 {field}"

        # 状态语义严格断言
        assert data["status"] == "healthy", "正常时 status 应为 healthy"
        assert data["database"] == "connected", (
            "正常时 database 应为 connected"
        )
