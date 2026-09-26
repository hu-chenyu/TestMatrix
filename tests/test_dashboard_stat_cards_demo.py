"""
TestMatrix Day37: Dashboard 统计卡片渲染测试（summary + cases total 整合）

测试覆盖:
    1. 四个统计卡片标题（用例总数/通过率/执行批次/失败数）
    2. 四个数值位 id（statTotal/statPassRate/statBatches/statFailed）
    3. summary 数据源契约：卡片所需五字段（overall_pass_rate/
       total_batches/failed/error/total_executed）
    4. cases 列表 total 契约：page=1&page_size=1 的 data.total 为非负整数
    5. dashboard.js 含 renderStatCards / loadCaseTotal 两个渲染函数
    6. dashboard.js 含字段映射与百分比格式化（toFixed）
    7. dashboard.js / chart-helper.js 静态资源可访问（200）
    8. 空库安全降级：summary 零批次/零执行/0.0 通过率、cases total=0

说明:
    pytest 不执行浏览器 JS，卡片真实数字的渲染（5 / 78.6% / 3 / 3）
    由浏览器实测兜底；本文件只锁定页面结构、JS 渲染逻辑存在性与
    后端数据源契约。空库类用例统一走临时 SQLite 文件隔离。
"""

import pytest
from flask.testing import FlaskClient

from src.db.db_session import DatabaseSession
from src.web import create_app


class TestDashboardStatCards:
    """Dashboard 统计卡片结构与数据源契约测试"""

    @pytest.fixture
    def client(self) -> FlaskClient:
        """
        创建测试客户端 fixture

        参数:
            无

        返回:
            FlaskClient: 基于 test 环境应用工厂的测试客户端
        """
        # 页面结构/静态资源用例不依赖业务数据
        app = create_app("test")
        return app.test_client()

    @pytest.fixture
    def isolated_db(self, tmp_path, monkeypatch, request):
        """
        临时 SQLite 库隔离 fixture（供数据源契约与空库用例使用）

        参数:
            tmp_path (pathlib.Path): pytest 提供的用例级临时目录
            monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
            request (pytest.FixtureRequest): 用于注册 teardown 收尾

        返回:
            None: 仅完成临时库初始化；用例内经 client 访问接口
        """
        # 数据库指向用例级临时文件，保证空库断言不被其他用例数据污染
        monkeypatch.setenv("TM_DB_TYPE", "sqlite")
        monkeypatch.setenv(
            "TM_DB_SQLITE_PATH", str(tmp_path / "dashboard_stat_cards.db")
        )
        DatabaseSession.reset()
        DatabaseSession.init_db()

        # teardown 先释放连接，防 Windows 下临时库文件被锁
        def _teardown() -> None:
            DatabaseSession.reset()

        request.addfinalizer(_teardown)

    def test_stat_cards_four_titles(self, client: FlaskClient) -> None:
        """
        测试四个卡片标题: 页面应含“用例总数/通过率/执行批次/失败数”
        （Day37 首张卡片标题由“累计执行”修正为“用例总数”）
        """
        # 请求看板页
        response = client.get("/dashboard")
        html = response.data.decode("utf-8")

        # 页面 200 且为 HTML
        assert response.status_code == 200, "GET /dashboard 应返回200"
        assert "text/html" in response.content_type, "应返回HTML文档"

        # 四个卡片标题逐一断言
        expected_titles = ("用例总数", "通过率", "执行批次", "失败数")
        for title in expected_titles:
            assert title in html, f"看板页应包含统计卡片标题：{title}"

    def test_stat_value_ids_present(self, client: FlaskClient) -> None:
        """
        测试四个数值位 id: statTotal/statPassRate/statBatches/
        statFailed 必须存在（Day36 起 id 契约不变，JS 据此渲染）
        """
        # 请求看板页
        response = client.get("/dashboard")
        html = response.data.decode("utf-8")

        # 四个数值位 id 逐一断言
        expected_ids = (
            "statTotal",
            "statPassRate",
            "statBatches",
            "statFailed",
        )
        for element_id in expected_ids:
            assert f'id="{element_id}"' in html, (
                f"看板页应包含卡片数值位 id={element_id}"
            )

    def test_summary_contract_for_cards(
        self, client: FlaskClient, isolated_db
    ) -> None:
        """
        测试 summary 卡片字段契约: GET /api/reports/summary 200，
        data 同时含通过率/批次/失败/异常/累计执行五个字段
        （空库安全降级，只断言字段存在）
        """
        # 请求全局执行汇总
        response = client.get("/api/reports/summary")
        body = response.get_json()

        # 统一响应 200
        assert response.status_code == 200, "summary 接口应返回200"
        data = body["data"]

        # 卡片渲染依赖的五个字段必须齐全
        expected_fields = (
            "overall_pass_rate",
            "total_batches",
            "failed",
            "error",
            "total_executed",
        )
        for field in expected_fields:
            assert field in data, f"summary.data 应包含字段 {field}"

    def test_cases_total_contract(
        self, client: FlaskClient, isolated_db
    ) -> None:
        """
        测试用例总数契约: GET /api/cases/?page=1&page_size=1 200，
        data.total 应为非负整数（用例总数取该字段，不新增后端端点）
        """
        # 只取 1 条，用 total 拿用例资产总数
        response = client.get("/api/cases/?page=1&page_size=1")
        body = response.get_json()

        # 统一响应 200
        assert response.status_code == 200, "cases 列表接口应返回200"
        total = body["data"]["total"]

        # total 必须是整数且非负（布尔是 int 子类，需显式排除）
        assert isinstance(total, int) and not isinstance(total, bool), (
            "data.total 应为整数"
        )
        assert total >= 0, "data.total 应非负"

        # 回显的分页参数与请求一致，证明接口按 page_size=1 生效
        assert body["data"]["page"] == 1, "回显 page 应为1"
        assert body["data"]["page_size"] == 1, "回显 page_size 应为1"

    def test_dashboard_js_contains_render_functions(
        self, client: FlaskClient
    ) -> None:
        """
        测试渲染函数存在性: dashboard.js 应可访问且含
        renderStatCards 与 loadCaseTotal 两个 Day37 新增函数
        """
        # 请求业务脚本
        response = client.get("/static/js/dashboard.js")

        # 静态资源 200
        assert response.status_code == 200, "dashboard.js 应可访问（200）"
        js_text = response.data.decode("utf-8")

        # 两个关键函数名必须存在（函数定义）
        assert "function renderStatCards" in js_text, (
            "dashboard.js 应定义 renderStatCards 函数"
        )
        assert "function loadCaseTotal" in js_text, (
            "dashboard.js 应定义 loadCaseTotal 函数"
        )

    def test_dashboard_js_contains_field_mapping(
        self, client: FlaskClient
    ) -> None:
        """
        测试字段映射与百分比格式化: dashboard.js 应含
        overall_pass_rate/total_batches/failed/error 字段名及
        toFixed（通过率 0~1 小数转 1 位百分比）
        """
        # 请求业务脚本
        response = client.get("/static/js/dashboard.js")
        js_text = response.data.decode("utf-8")

        # 四个 summary 字段映射必须出现
        expected_tokens = (
            "overall_pass_rate",
            "total_batches",
            "failed",
            "error",
            "toFixed",
        )
        for token in expected_tokens:
            assert token in js_text, (
                f"dashboard.js 应包含字段/方法：{token}"
            )

    def test_dashboard_static_assets_accessible(
        self, client: FlaskClient
    ) -> None:
        """
        测试静态资源可访问: dashboard.js 与 chart-helper.js
        经 /static/ 均返回 200（chart-helper Day36 已验收，连带回归）
        """
        # 两个脚本资源路径
        asset_paths = (
            "/static/js/dashboard.js",
            "/static/js/chart-helper.js",
        )
        for asset_path in asset_paths:
            response = client.get(asset_path)
            assert response.status_code == 200, (
                f"静态资源 {asset_path} 应可访问（200）"
            )

    def test_empty_library_cards_safe(
        self, client: FlaskClient, isolated_db
    ) -> None:
        """
        测试空库安全降级: 临时空库下 summary 的 total_batches=0、
        total_executed=0、overall_pass_rate=0.0，cases 列表 total=0；
        前端据此渲染 0 / --（无执行不显示0%）/ 0 / 0，不报错
        （JS 分支由浏览器实测确认，此处锁定数据源空值结构）
        """
        # 请求两个数据源
        summary_response = client.get("/api/reports/summary")
        summary = summary_response.get_json()["data"]
        cases_response = client.get("/api/cases/?page=1&page_size=1")
        cases_data = cases_response.get_json()["data"]

        # summary 空库聚合为零值（后端空表安全降级契约）
        assert summary["total_batches"] == 0, "空库批次数应为0"
        assert summary["total_executed"] == 0, "空库累计执行应为0"
        assert summary["overall_pass_rate"] == 0.0, (
            "空库通过率应为0.0（前端在 total_executed=0 时改显 --）"
        )
        assert summary["failed"] == 0 and summary["error"] == 0, (
            "空库失败数与异常数应为0"
        )

        # cases total 为整数 0
        assert cases_data["total"] == 0, "空库用例总数应为0"
        assert isinstance(cases_data["total"], int), "total 应为整数类型"
