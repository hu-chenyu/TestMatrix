"""
TestMatrix Day35: 前端骨架测试（Bootstrap5 + Jinja2模板继承 + 页面路由）

测试覆盖:
    1. 三个HTML页面路由 /dashboard、/cases、/executions 返回200且为text/html
    2. 模板继承生效：子页面经base.html渲染，含Bootstrap5 CDN标识与导航栏
    3. 导航栏含三个页面链接（href由url_for生成：/dashboard、/cases、/executions）
    4. 静态资源 main.css/main.js/api.js 可经 /static/ 正常访问（200）
    5. 根路径GET /零回归：仍返回JSON（message/version），未被改成HTML首页
    6. 三个页面渲染各自占位标题，且当前导航项唯一高亮（active）

说明:
    骨架阶段为纯页面渲染，无需构造业务数据，fixture仅创建测试客户端，
    不修改conftest.py；页面数据对接从Day36起由前端JS异步调/api完成。
"""

import pytest
from flask.testing import FlaskClient

from src.web import create_app


class TestFrontendSkeleton:
    """前端骨架渲染与静态资源测试"""

    @pytest.fixture
    def client(self) -> FlaskClient:
        """
        创建测试客户端fixture

        参数:
            无

        返回:
            FlaskClient: 基于test环境应用工厂的测试客户端，
                        页面渲染/静态资源均不依赖数据库
        """
        # 骨架纯渲染：仅初始化应用与测试客户端，不seed任何业务数据
        app = create_app("test")
        return app.test_client()

    def test_dashboard_page_returns_html(self, client: FlaskClient) -> None:
        """
        测试质量看板页: GET /dashboard 应返回200、text/html，
        且页面经基模板渲染出导航栏
        """
        # 请求质量看板页面
        response = client.get("/dashboard")

        # 状态码与内容类型断言
        assert response.status_code == 200, "GET /dashboard 应返回200"
        assert "text/html" in response.content_type, (
            "/dashboard 应返回HTML文档（text/html）"
        )
        # 导航栏由base.html提供，出现即证明页面走了模板渲染链路
        assert b"navbar" in response.data, "页面应包含导航栏（navbar）"

    def test_cases_page_returns_html(self, client: FlaskClient) -> None:
        """
        测试用例管理页: GET /cases 应返回200且为text/html
        """
        # 请求用例管理页面
        response = client.get("/cases")

        # 状态码与内容类型断言
        assert response.status_code == 200, "GET /cases 应返回200"
        assert "text/html" in response.content_type, (
            "/cases 应返回HTML文档（text/html）"
        )

    def test_executions_page_returns_html(self, client: FlaskClient) -> None:
        """
        测试执行记录页: GET /executions 应返回200且为text/html
        """
        # 请求执行记录页面
        response = client.get("/executions")

        # 状态码与内容类型断言
        assert response.status_code == 200, "GET /executions 应返回200"
        assert "text/html" in response.content_type, (
            "/executions 应返回HTML文档（text/html）"
        )

    def test_pages_inherit_base_with_bootstrap(
        self, client: FlaskClient
    ) -> None:
        """
        测试模板继承: /dashboard 页面应含Bootstrap5 本地静态资源与导航栏，
        证明子页面通过 {% extends "base.html" %} 继承了统一HTML骨架
        """
        # 请求看板页并解码为文本便于做子串断言
        response = client.get("/dashboard")
        html = response.data.decode("utf-8")

        # Bootstrap5 CSS/JS 走本地静态资源（离线可用，不依赖外网CDN）
        assert "/static/css/bootstrap.min.css" in html, (
            "页面应经本地静态资源加载 Bootstrap5 CSS"
        )
        assert "/static/js/bootstrap.bundle.min.js" in html, (
            "页面应经本地静态资源加载 Bootstrap5 JS bundle"
        )
        # Bootstrap Icons 图标字体本地静态资源
        assert "/static/css/bootstrap-icons.min.css" in html, (
            "页面应经本地静态资源加载 Bootstrap Icons"
        )
        # 导航栏与全局样式均由基模板提供
        assert "navbar" in html, "继承base.html后页面应含导航栏"
        assert "css/main.css" in html, "继承base.html后应引入全局main.css"

    def test_navbar_contains_three_links(self, client: FlaskClient) -> None:
        """
        测试导航栏三链接: 页面应包含 /dashboard、/cases、/executions
        三个href（由url_for('pages.xxx')生成，禁止硬编码路径）
        """
        # 三个页面共用同一导航栏，取看板页断言即可
        response = client.get("/dashboard")
        html = response.data.decode("utf-8")

        # 逐个断言三个导航链接的href（url_for产物为相对根路径）
        assert 'href="/dashboard"' in html, "导航应含质量看板链接 /dashboard"
        assert 'href="/cases"' in html, "导航应含用例管理链接 /cases"
        assert 'href="/executions"' in html, "导航应含执行记录链接 /executions"

    def test_static_assets_accessible(self, client: FlaskClient) -> None:
        """
        测试静态资源可访问: main.css/main.js/api.js 经 /static/ 均返回200
        """
        # 待校验的三个静态资源相对路径（与base.html中url_for产物一致）
        asset_paths = (
            "/static/css/main.css",
            "/static/js/main.js",
            "/static/js/api.js",
        )
        for asset_path in asset_paths:
            # 逐个GET静态资源
            response = client.get(asset_path)
            assert response.status_code == 200, (
                f"静态资源 {asset_path} 应可访问（200）"
            )

    def test_root_json_api_unchanged(self, client: FlaskClient) -> None:
        """
        测试根路径零回归: GET / 必须仍返回JSON（message/data.version），
        前端HTML入口是/dashboard而非/，防止页面路由误占根路径
        """
        # 请求根路径
        response = client.get("/")

        # 必须仍是JSON统一响应体而非HTML
        data = response.get_json()
        assert response.status_code == 200, "GET / 应返回200"
        assert data is not None, "GET / 必须返回JSON，不得改为HTML首页"
        assert data["message"] == "TestMatrix API", (
            "根路径message应保持TestMatrix API不变"
        )
        assert data["data"]["version"] == "1.0.0", (
            "根路径data.version应保持1.0.0不变"
        )

    def test_page_placeholders_render(self, client: FlaskClient) -> None:
        """
        测试页面特征内容与导航高亮: 各页面渲染各自特征标题，
        且当前页对应的导航项唯一带active类

        说明: Day36 起 /dashboard 已由占位卡片重写为真实看板框架，
        其特征文案改为框架分区标题（失败用例 Top 榜）；
        /cases、/executions 仍为 Day35 占位页。
        """
        # 页面路径 → 特征标题（与pages子模板当前内容逐一对应）
        page_expectations = (
            ("/dashboard", "失败用例 Top 榜"),
            ("/cases", "用例管理建设中"),
            ("/executions", "执行记录建设中"),
        )
        for path, placeholder_title in page_expectations:
            # 请求页面并解码
            response = client.get(path)
            html = response.data.decode("utf-8")

            # 特征标题必须渲染（区别于导航栏同名链接文案）
            assert placeholder_title in html, (
                f"{path} 页面应渲染特征标题：{placeholder_title}"
            )
            # 当前导航项唯一高亮：恰好一个 nav-link 带 active 类
            assert html.count('nav-link active') == 1, (
                f"{path} 页面当前导航项应唯一高亮（active恰1处）"
            )
