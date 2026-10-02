"""
TestMatrix 国庆大扫除 · 阶段2 Commit A：安全与凭据类修复的回归测试

本文件为每一条**安全修复**提供"漏洞已封堵"的证明（提示词 2.13），覆盖：

    P0  dashboard.js 饼图 tooltip 存储型 XSS —— module 未转义
    P1  notification.py 企微 webhook key 随 requests 异常原文写入日志
    P2  base.py /health 无条件回传 str(exc)，泄露连接URL与SQL
    P2  app.py 缺失 Referrer-Policy / HSTS 安全头
    P2  base.html 缺失 CSP
    P2  config.py JSON_AS_ASCII/JSON_SORT_KEYS 为 Flask 2.3 已移除的死配置
    P2  config.py TestingConfig 的 TM_DB_* 为无人读取的死配置
    P2  config.py SECRET_KEY 类定义期求值（多worker各进程不一致）
    P2  config.py 缺 MAX_CONTENT_LENGTH（上传无限制）
    P2  cases.py 日志直接写用户可控文件名（日志注入）
    P2  cases.py secure_filename 吃掉中文名导致中文xlsx无法导入
    P2  http_client.py URL 查询串从不脱敏
    P2  notification.py 邮件报告 HTML 未转义
    P2  notification.py 企微响应非dict时 AttributeError 逃出 send()
    P2  logger.py 控制台通道缺 diagnose=False / 主日志文件硬编码 DEBUG
    P2  exceptions.py 通用 Exception 兜底吞掉 HTTPException（413 被转成 500）

关于前端断言的口径说明:
    本项目无 JS 测试运行器，且当前环境无 node，无法对 escapeHtml 做真实
    行为验证。前端修复采用**源码形态断言**：锁定 formatter 内的转义调用
    存在、裸拼接已消失。这类断言能挡住"有人把 escapeHtml(...) 改回
    row.module"这一回归，真实浏览器行为验证列为 backlog。

数据说明:
    全部用 dataclass / monkeypatch 构造，不依赖真实网络与真实 Redis。
"""

import contextlib
import io
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
from flask.testing import FlaskClient
from src.common.env_manager import env_manager
from src.common.http_client import (
    SENSITIVE_BODY_FIELDS,
    SENSITIVE_QUERY_FIELDS,
    HttpClient,
)
from src.common.logger import LogManager
from src.core.notification import (
    EmailReportTemplate,
    Notification,
    WeChatNotifier,
    _mask_webhook_url,
)
from src.core.report_analyzer import FailedCaseDetail, ModuleStat
from src.db.db_session import DatabaseSession
from src.web import create_app
from src.web.config import (
    MAX_UPLOAD_BYTES,
    Config,
    DevelopmentConfig,
    ProductionConfig,
    TestingConfig,
    resolve_secret_key,
)

# 项目根目录（tests/ 的上一级），用于定位前端静态资源源码
PROJECT_ROOT = Path(__file__).resolve().parent.parent
JS_DIR = PROJECT_ROOT / "src" / "web" / "static" / "js"
TEMPLATE_DIR = PROJECT_ROOT / "src" / "web" / "templates"

# 用于验证 XSS 封堵的 payload（模拟真实攻击者写入的 module 值）
XSS_PAYLOAD = '<img src=x onerror=fetch("/api/cases/XSS-1",{method:"DELETE"})>'

# 企微 webhook 固定测试地址（key 会在各用例中按需替换）
FAKE_WECHAT_BASE = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


# ======================================================================
# P0: dashboard.js 饼图 tooltip 存储型 XSS
# ======================================================================
class TestDashboardTooltipXSS:
    """P0: ECharts tooltip formatter 必须转义后端 module 字段"""

    @pytest.fixture
    def dashboard_js(self) -> str:
        """
        读取 dashboard.js 源码

        返回:
            str: dashboard.js 全文
        """
        return (JS_DIR / "dashboard.js").read_text(encoding="utf-8")

    @pytest.fixture
    def main_js(self) -> str:
        """
        读取 main.js 源码

        返回:
            str: main.js 全文
        """
        return (JS_DIR / "main.js").read_text(encoding="utf-8")

    def test_module_distribution_tooltip_escapes_module(
        self, dashboard_js: str
    ) -> None:
        """
        模块分布饼图 tooltip 必须对 row.module 调用 escapeHtml

        回归点: 修复前是 `row.module + "<br>总数："`，module 来自
        POST /api/cases/（后端仅校验长度不校验字符集），ECharts 5 tooltip
        默认 renderMode:"html"，自定义 formatter 返回值不转义 -> 存储型 XSS。
        """
        formatter_match = re.search(
            r"formatter:\s*function\s*\(param\)\s*\{(.*?)\n\s{16}\},",
            dashboard_js,
            re.DOTALL,
        )
        assert formatter_match is not None, (
            "dashboard.js 应仍存在 trigger:'item' 的饼图 formatter，"
            "若被重构请同步更新本回归测试"
        )
        formatter_body = formatter_match.group(1)

        assert "escapeHtml(row.module)" in formatter_body, (
            "饼图 tooltip 必须转义 row.module，否则构成存储型 XSS"
        )
        # 修复前的形态：裸拼接，必须已消失（负向断言，防止"加了转义但漏了某处"）
        assert not re.search(
            r"(?<!escapeHtml\()\brow\.module\s*\+", formatter_body
        ), "饼图 tooltip 中不得再出现 row.module 的裸字符串拼接"

    def test_trend_tooltip_escapes_execution_id(self, dashboard_js: str) -> None:
        """
        趋势折线 tooltip 必须对 row.execution_id 调用 escapeHtml

        回归点: 与 P0 同类遗漏。当前 execution_id 由后端生成不可控，
        但一旦改为可由用户指定即等价 XSS。
        """
        assert "escapeHtml(row.execution_id)" in dashboard_js, (
            "趋势图 tooltip 必须转义 row.execution_id"
        )
        assert not re.search(r"批次：\"\s*\+\s*row\.execution_id", dashboard_js), (
            "趋势图 tooltip 中不得再出现 row.execution_id 的裸拼接"
        )

    def test_escape_html_is_globally_exposed_by_main_js(self, main_js: str) -> None:
        """
        escapeHtml 必须由 main.js 挂到 window

        回归点: dashboard.html 不加载 cases.js，escapeHtml 若只挂在
        window.casesPage 上则 dashboard 完全拿不到转义能力。
        """
        assert "window.escapeHtml = escapeHtml;" in main_js, (
            "escapeHtml 必须挂到 window（main.js 是全站唯一公共脚本）"
        )

    def test_escape_html_covers_all_five_html_metacharacters(
        self, main_js: str
    ) -> None:
        """
        escapeHtml 必须覆盖全部五个 HTML 元字符

        锁定两个语义锚点：替换正则的字符类列出全部五个元字符；实体映射表
        含全部五个实体。断言刻意不绑定 JS 的引号书写风格（'&' 与 "&"
        两种写法都合法），避免因无意义的格式改动误报。
        """
        assert "[&<>\"']/g" in main_js, (
            "escapeHtml 的替换正则字符类必须覆盖 & < > \" ' 五个元字符"
        )
        entities = {
            "&amp;": "&",
            "&lt;": "<",
            "&gt;": ">",
            "&quot;": '"',
            "&#39;": "'",
        }
        for entity, char in entities.items():
            assert entity in main_js, f"escapeHtml 缺少实体映射 {entity}（{char}）"

    def test_showtoast_supports_warning_type(self, main_js: str) -> None:
        """
        showToast 必须支持 warning/info 类型

        回归点: 修复前 type 被归一化为 danger/success 两值，
        dashboard.js 4 处传 "warning" 全被渲染成绿色成功样式配"加载失败"文案。
        """
        assert '["success", "danger", "warning", "info"]' in main_js, (
            "showToast 的类型白名单必须含 warning 与 info"
        )
        assert 'type === "danger" ? "danger" : "success"' not in main_js, (
            "showToast 不得再使用 danger/success 二值归一化"
        )


# ======================================================================
# P1: 企微 webhook key 凭据泄露
# ======================================================================
class TestWechatWebhookKeyLeak:
    """P1: webhook key 绝不出现在日志或异常消息中"""

    @pytest.fixture(autouse=True)
    def enable_wechat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        打开企微总开关

        WeChatNotifier.send 首行即检查 TM_WECHAT_ENABLED，关闭时直接返回
        False 且不发请求——不打开开关则后续断言全部为永真。
        """
        monkeypatch.setenv("TM_WECHAT_ENABLED", "true")
        monkeypatch.setenv("TM_WECHAT_WEBHOOK_URL", FAKE_WECHAT_BASE + "?key=x")

    def test_network_exception_does_not_log_webhook_key(self) -> None:
        """
        网络异常分支不得把 str(exc) 写入日志

        回归点: urllib3 的 MaxRetryError 文本自带完整 URL
        （"Max retries exceeded with url: /cgi-bin/webhook/send?key=XXX"），
        修复前 `f"...: {exc}"` 直接把 webhook key 明文落盘。
        """
        secret = "SUPERSECRET-key-abc123"
        notifier = WeChatNotifier()
        notifier.webhook_url = FAKE_WECHAT_BASE + "?key=" + secret

        # 构造一个与真实 urllib3 文本同构的异常（含完整URL与key）
        leaked_exception = requests.exceptions.ConnectionError(
            "Max retries exceeded with url: "
            f"/cgi-bin/webhook/send?key={secret} "
            "(Caused by NameResolutionError(...))"
        )

        with patch.object(requests, "post", side_effect=leaked_exception):
            with caplog_at_error() as captured:
                result = notifier.send(make_notification())

        assert result is False, "网络异常时 send() 必须返回 False 而非抛出"
        assert captured, "网络异常分支应记录 error 级日志"
        log_text = "\n".join(record["message"] for record in captured)
        assert secret not in log_text, f"webhook key 泄露到日志: {log_text}"
        assert "qyapi.weixin.qq.com" in log_text, (
            "脱敏后应保留主机名等排障信息"
        )

    def test_mask_webhook_url_strips_query(self) -> None:
        """
        _mask_webhook_url 必须剥离 query 与 fragment
        """
        masked = _mask_webhook_url(
            FAKE_WECHAT_BASE + "?key=SECRET123#frag"
        )
        assert "SECRET123" not in masked, "query 中的 key 必须被剥离"
        assert "frag" not in masked, "fragment 必须被剥离"
        assert masked == FAKE_WECHAT_BASE

    def test_mask_webhook_url_handles_malformed_input(self) -> None:
        """
        _mask_webhook_url 对畸形输入不得抛异常

        回归点: 该函数在**异常处理路径**中被调用，一旦自身抛异常会掩盖
        原始故障并让 send() 契约失效。
        """
        assert _mask_webhook_url("") == "<webhook地址非法>"
        assert _mask_webhook_url("not a url") == "<webhook地址非法>"
        # 不可解析的 IPv6 形式会触发 urlsplit 的 ValueError 分支
        assert _mask_webhook_url("http://[::1").startswith("<webhook")


# ======================================================================
# P2: /health 信息泄露 + 安全头 + CSP
# ======================================================================
class TestHealthEndpointLeak:
    """P2: 未鉴权健康检查接口不得回传数据库内部细节"""

    @pytest.fixture
    def prod_client(self) -> FlaskClient:
        """
        **生产模式**测试客户端（TESTING=False）

        /health 的脱敏门控按 app.config["TESTING"] 判定，验证"生产不泄露"
        必须在非测试模式下进行，否则测的是 TESTING 分支的详情回显。

        返回:
            FlaskClient: 生产模式 Flask 测试客户端
        """
        app = create_app("dev")
        app.config["TESTING"] = False
        return app.test_client()

    @pytest.fixture
    def client(self) -> FlaskClient:
        """
        创建测试客户端（TESTING=True）

        返回:
            FlaskClient: test 环境客户端
        """
        return create_app("test").test_client()

    def test_health_error_does_not_expose_credentials(
        self, prod_client: FlaskClient
    ) -> None:
        """
        生产模式下数据库异常不得回显连接URL与SQL

        回归点: 修复前 /health 无条件回显 str(exc)，SQLAlchemy 异常文本含
        完整连接URL（MySQL 模式下含用户名/库名）与SQL片段，而 /health
        无鉴权且常被监控高频轮询。

        注意: 断言必须在 **生产模式** 下做——修复后按项目 6.7 的
        TESTING/生产双模式约定，TESTING 模式会回显详情供联调定位
        （与 exceptions.py 全局处理器同口径），只有非 TESTING 才脱敏。
        """
        leaked = (
            "(sqlite3.OperationalError) unable to open database file\n"
            "[SQL: SELECT 1]\n"
            "[parameters: {}]\n"
            "mysql+pymysql://root:Sup3rSecret@10.0.0.5:3306/testmatrix"
        )
        with patch(
            "src.web.routes.base._probe_database",
            side_effect=RuntimeError(leaked),
        ):
            response = prod_client.get("/health")

        assert response.status_code == 503, "数据库不可用时 /health 应返回503"
        body = response.get_data(as_text=True)
        assert "degraded" in body, "降级响应体应仍标识 degraded 状态"
        # 敏感内容不得出现
        assert "Sup3rSecret" not in body, "数据库密码泄露到 /health 响应"
        assert "mysql+pymysql://" not in body, "数据库连接URL泄露到 /health 响应"
        assert "SELECT 1" not in body, "SQL 语句泄露到 /health 响应"
        # 异常类型名保留供排障
        assert "RuntimeError" in body, "应回传异常类型名而非原文"

    def test_health_error_detail_only_in_testing_mode(self) -> None:
        """
        TESTING 模式才回显完整详情（6.7 双模式约定，不回归）

        修复后 /health 拆成「对外摘要」与「内部详情」两段，TESTING=True
        时回显详情供联调定位，非 TESTING 时只回显脱敏摘要。
        """
        leaked = "诊断细节: mysql+pymysql://root:Sup3rSecret@host/db"
        client = create_app("test").test_client()
        with patch(
            "src.web.routes.base._probe_database",
            side_effect=RuntimeError(leaked),
        ):
            response = client.get("/health")

        body = response.get_data(as_text=True)
        assert response.status_code == 503
        assert "Sup3rSecret" in body, (
            "TESTING 模式应回显完整详情供联调定位（口径同 exceptions.py）"
        )

    def test_health_probe_failure_is_logged_with_full_detail(
        self, client: FlaskClient
    ) -> None:
        """
        脱敏后完整上下文仍须进日志（不能为了不泄露就丢掉排障能力）
        """
        with caplog_at_warning() as captured:
            with patch(
                "src.web.routes.base._probe_database",
                side_effect=RuntimeError("db detail for log only"),
            ):
                client.get("/health")

        log_text = "\n".join(record["message"] for record in captured)
        assert "db detail for log only" in log_text, (
            "完整异常上下文应保留在日志中供运维排查"
        )


class TestSecurityHeadersAndCSP:
    """P2: 安全响应头与 CSP 纵深防御"""

    @pytest.fixture
    def client(self) -> FlaskClient:
        """
        创建测试客户端

        返回:
            FlaskClient: test 环境客户端
        """
        return create_app("test").test_client()

    def test_response_carries_all_security_headers(
        self, client: FlaskClient
    ) -> None:
        """
        响应必须携带完整安全头集合

        回归点: 修复前只有 X-Content-Type-Options 与 X-Frame-Options 两行，
        缺 Referrer-Policy（URL 含 execution_id/case_id 等业务标识）与 HSTS。
        """
        response = client.get("/api/version")
        assert response.status_code == 200

        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
        assert response.headers["Referrer-Policy"] == "same-origin", (
            "缺 Referrer-Policy，跨站跳转将泄露完整业务URL"
        )
        assert response.headers["Strict-Transport-Security"] == (
            "max-age=31536000"
        ), "缺 HSTS 头"

    def test_hsts_does_not_force_subdomains(self, client: FlaskClient) -> None:
        """
        HSTS 不得带 includeSubDomains

        回归点: 本项目是自建工具，同域可能存在其他 HTTP 服务，
        强制子域升级 HTTPS 会造成非预期中断。
        """
        hsts = client.get("/api/version").headers["Strict-Transport-Security"]
        assert "includeSubDomains" not in hsts

    def test_base_template_declares_csp(self, client: FlaskClient) -> None:
        """
        base.html 必须声明 CSP

        回归点: 修复前无任何 CSP，XSS 无纵深防御。取值需兼容现状：
        无外链 CDN、无内联 <script>，但 dashboard.html 存在行内 style 属性
        （Bootstrap 亦以 CSSOM 设置样式），故 style-src 必须保留
        'unsafe-inline'，否则会把页面样式全部打失效。
        """
        html = client.get("/dashboard").get_data(as_text=True)

        assert "Content-Security-Policy" in html, "base.html 缺 CSP 声明"
        csp_match = re.search(
            r'http-equiv="Content-Security-Policy"\s*\n?\s*content="([^"]+)"',
            html,
        )
        assert csp_match is not None, "CSP meta 的 content 属性无法解析"
        csp = csp_match.group(1)

        assert "script-src 'self'" in csp, "script-src 应限制为同源"
        assert "object-src 'none'" in csp, "object-src 应为 none"
        assert "connect-src 'self'" in csp, "connect-src 应限制为同源"
        assert "style-src 'self' 'unsafe-inline'" in csp, (
            "style-src 必须保留 'unsafe-inline'，否则 dashboard 行内样式失效"
        )
        assert "frame-ancestors" not in csp, (
            "frame-ancestors 在 meta 中无效，点击劫持防护走 X-Frame-Options 响应头"
        )

    def test_csp_does_not_break_page_rendering(self, client: FlaskClient) -> None:
        """
        加 CSP 后页面关键资源仍走同源相对路径

        回归点: 若误加 CDN 域名，脚本会被 CSP 拦截导致页面全白。
        """
        html = client.get("/dashboard").get_data(as_text=True)
        assert "https://cdn" not in html, "不得引用外部 CDN"
        assert "http://" not in html.split("<body")[0], (
            "head 中不得有外链 http 资源，否则 script-src 'self' 会拦截"
        )
        assert "/static/js/dashboard.js" in html, "业务脚本仍应被引入"


# ======================================================================
# P2: exceptions.py 通用兜底吞掉 HTTPException
# ======================================================================
class TestHttpExceptionNotSwallowed:
    """P2: werkzeug HTTP 异常必须保留原始状态码，不得被兜底转成 500"""

    @pytest.fixture
    def client(self) -> FlaskClient:
        """
        创建测试客户端

        返回:
            FlaskClient: test 环境客户端
        """
        return create_app("test").test_client()

    def test_413_keeps_original_status_code(self) -> None:
        """
        上传超限必须返回 413 而非 500

        回归点: 本文件注册了 `Exception` 兜底处理器，而 werkzeug 的
        RequestEntityTooLarge 是 HTTPException（Exception 子类）。Flask 2.3
        的处理器查找按 状态码 -> 类MRO 回退，未注册 413 时会命中 Exception
        兜底，把"客户端上传超限"错误地报成 500 "服务器内部错误"。后果是
        监控无法区分正常拦截与服务端真故障。

        构造方式: 用**结构完整**的 multipart 请求体（而非畸形 body），
        确保唯一触发 413 的原因是体积超限，不掺杂 multipart 解析差异——
        畸形 body 的解析行为在不同平台/werkzeug 版本间可能不同，会让本
        测试变成平台相关的不稳定用例。
        """
        app = create_app("test")
        # 上限临时调到 1KB（生产为 32MB，单测不宜真传 32MB）
        app.config["MAX_CONTENT_LENGTH"] = 1024
        client = app.test_client()

        # 构造一个 >1KB 的合法 xlsx 上传
        oversized = _build_minimal_xlsx() + b"\x00" * 4096
        response = client.post(
            "/api/cases/import",
            data={"file": (io.BytesIO(oversized), "big.xlsx")},
            content_type="multipart/form-data",
        )
        assert response.status_code == 413, (
            f"超限上传应返回413，实际 {response.status_code}"
            "（被 Exception 兜底吞成了 500？）"
        )
        payload = response.get_json()
        assert payload["code"] == 413, "响应体业务码应同步为 413"

    def test_404_still_uses_its_own_handler(self) -> None:
        """
        新增的 HTTPException 处理器不得抢走 404 自己的处理器

        回归点: 404/405/500 是按**状态码**注册的，Flask 查找时状态码优先，
        新处理器按类注册于回退层，行为应保持不变。

        注意: 响应体是 JSON，Flask 2.3 默认 ensure_ascii=True 会把中文
        转义成 \\uXXXX，故必须解析 JSON 后比对 message，不能对原始字节做
        中文子串匹配。
        """
        client = create_app("test").test_client()
        response = client.get("/no-such-endpoint-xyz")

        assert response.status_code == 404
        payload = response.get_json()
        assert payload["message"] == "请求的资源不存在", (
            "404 应仍走中文业务文案，未被通用 HTTPException 处理器改写"
        )
        assert payload["code"] == 404


# ======================================================================
# P2: config.py 死配置与 SECRET_KEY 时序
# ======================================================================
class TestConfigHardening:
    """P2: Flask 2.3 已移除配置项清理 + SECRET_KEY 解析时机 + 上传上限"""

    def test_removed_flask_json_keys_are_gone(self) -> None:
        """
        JSON_AS_ASCII / JSON_SORT_KEYS 必须已删除

        回归点: 这两个键在 Flask 2.3 被移除（迁往 app.json.ensure_ascii /
        app.json.sort_keys），设置它们不再有任何效果，却让维护者误以为
        已在控制 JSON 序列化行为。
        """
        assert not hasattr(Config, "JSON_AS_ASCII"), (
            "JSON_AS_ASCII 在 Flask 2.3 已移除，是死配置，必须删除"
        )
        assert not hasattr(Config, "JSON_SORT_KEYS"), (
            "JSON_SORT_KEYS 在 Flask 2.3 已移除，是死配置，必须删除"
        )
        response_doc = (TEMPLATE_DIR.parent / "response.py").read_text(
            encoding="utf-8"
        )
        assert "JSON_AS_ASCII等应用配置" not in response_doc, (
            "response.py 仍在声称 JSON_AS_ASCII 等已废弃配置影响序列化行为"
        )

    def test_testing_config_has_no_dead_db_keys(self) -> None:
        """
        TestingConfig 不得再声明 TM_DB_* 死配置

        回归点: 数据库层经 env_manager 读 os.environ，从不读 Flask
        app.config（全仓引用点均在 tests/ 的 monkeypatch.setenv）。
        这两个键会让人误以为"测试用内存 SQLite"，实际测试全部落磁盘库文件。
        """
        assert not hasattr(TestingConfig, "TM_DB_TYPE"), (
            "TM_DB_TYPE 是死配置（DB 层只读 os.environ），必须删除"
        )
        assert not hasattr(TestingConfig, "TM_DB_SQLITE_PATH"), (
            "TM_DB_SQLITE_PATH 是死配置（DB 层只读 os.environ），必须删除"
        )

    def test_secret_key_is_not_resolved_at_class_definition_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        SECRET_KEY 必须在工厂阶段解析，而非类定义期

        回归点: 修复前 `SECRET_KEY = os.getenv(..., secrets.token_hex(32))`
        在 import 期求值一次。多 worker(gunicorn) 部署且未配置
        TM_SECRET_KEY 时，各进程得到不同随机 key，跨进程 session 互相失效。
        """
        assert Config.SECRET_KEY is None, (
            "SECRET_KEY 不得在类定义期求值，应推迟到应用工厂阶段"
        )

        monkeypatch.setenv("TM_SECRET_KEY", "explicit-key-from-env")
        assert resolve_secret_key(DevelopmentConfig) == "explicit-key-from-env"

        monkeypatch.delenv("TM_SECRET_KEY", raising=False)
        first = resolve_secret_key(DevelopmentConfig)
        second = resolve_secret_key(DevelopmentConfig)
        assert first != second, "未配置时应每次生成新的随机密钥"
        assert len(first) == 64, "随机密钥应为 32 字节 hex"

    def test_app_actually_loads_resolved_secret_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        应用工厂必须把解析出的密钥真正写入 app.config
        """
        monkeypatch.setenv("TM_SECRET_KEY", "factory-resolved-key")
        app = create_app("test")
        assert app.config["SECRET_KEY"] == "factory-resolved-key", (
            "应用必须使用工厂阶段解析出的密钥"
        )

    def test_production_without_secret_key_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        生产环境缺 TM_SECRET_KEY 必须 fail-fast 拒绝启动

        回归点: 修复前只 print 警告并降级为进程级随机密钥。那是"配置错了
        却在运行中悄悄降级"的最坏形态——服务看着是好的，登录态却随机失效
        （每次重启即变 + 多 worker 各不相同）。启动即失败把问题暴露在部署时刻。

        v3 变更: 判定口径由"配置类身份是 ProductionConfig"改为"不在
        （开发, 测试）白名单内即要求显式配置"，错误文案随之改为
        "非开发/测试环境"。生产档位的行为不变（仍 fail-fast），
        变的是新增档位默认走严格侧。
        """
        monkeypatch.delenv("TM_SECRET_KEY", raising=False)

        with pytest.raises(ValueError, match="TM_SECRET_KEY") as exc_info:
            resolve_secret_key(ProductionConfig)

        message = str(exc_info.value)
        assert "非开发/测试环境" in message, (
            f"异常信息应说明这是非开发/测试环境的强制要求: {message}"
        )
        assert "重启" in message, "异常信息应说明随机密钥的实际后果"
        assert "ProductionConfig" in message, (
            "异常信息应指出当前生效的配置类，便于判断是不是环境配错了"
        )

    def test_production_with_secret_key_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        生产环境配置了 TM_SECRET_KEY 时正常返回（不回归）
        """
        monkeypatch.setenv("TM_SECRET_KEY", "prod-explicit-key")
        assert resolve_secret_key(ProductionConfig) == "prod-explicit-key"

    def test_dev_without_secret_key_uses_random(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        dev/test 环境缺密钥仍降级为随机 key（本地零配置体验不回归）
        """
        monkeypatch.delenv("TM_SECRET_KEY", raising=False)
        generated = resolve_secret_key(DevelopmentConfig)
        assert len(generated) == 64, "随机密钥应为 32 字节 hex"

    def test_max_content_length_is_enforced(self) -> None:
        """
        必须设置 MAX_CONTENT_LENGTH（上传大小硬上限）

        回归点: 全仓此前无该配置，/api/cases/import 可上传任意大文件打满
        磁盘，而平台当前无鉴权，风险可直接被利用。
        """
        assert Config.MAX_CONTENT_LENGTH == MAX_UPLOAD_BYTES
        assert MAX_UPLOAD_BYTES == 32 * 1024 * 1024, "上限应为 32MB"

        app = create_app("test")
        assert app.config["MAX_CONTENT_LENGTH"] == MAX_UPLOAD_BYTES, (
            "应用必须实际加载该上限配置"
        )


# ======================================================================
# P2: cases.py 日志注入 + 中文文件名
# ======================================================================
class TestCasesImportHardening:
    """P2: 日志注入防护 + secure_filename 中文名兼容"""

    @pytest.fixture
    def import_client(self, monkeypatch, tmp_path):
        """
        导入接口测试客户端（隔离的临时 SQLite 库，用例前后重置引擎单例）

        为什么必须显式建表: /api/cases/import 会真实写库，而 DatabaseSession
        的引擎是进程级单例，不会自动建表。若不显式 init_db()，用例实际依赖
        环境里"恰好已存在的表"——本地 output/testmatrix.db 有历史残留表能侥幸
        通过，CI 全新 checkout 是空库则必然报
        `(sqlite3.OperationalError) no such table: test_cases`。
        这正是 CI 红灯而本地全绿的真实成因。

        口径与项目既有的 tests/test_cases_import_sanitize.py::import_client 一致:
        monkeypatch 隔离库路径 -> reset() 清引擎单例 -> init_db() 建表
        -> teardown 再 reset()，避免污染其他用例的引擎状态。

        参数:
            monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture
            tmp_path (Path): pytest临时目录fixture

        返回:
            Iterator[FlaskClient]: yield Flask测试客户端
        """
        monkeypatch.setenv("TM_DB_TYPE", "sqlite")
        monkeypatch.setenv(
            "TM_DB_SQLITE_PATH", str(tmp_path / "security_hardening_api.db")
        )
        DatabaseSession.reset()
        DatabaseSession.init_db()
        yield create_app("test").test_client()
        DatabaseSession.reset()

    def test_sanitize_log_field_folds_control_chars(self) -> None:
        """
        _sanitize_log_field 必须折叠换行等控制字符

        回归点: original_name 完全由客户端控制，修复前直接 f-string 进
        logger.info，其中的换行会终止当前日志行、后续内容被解析为独立
        日志行——攻击者可借此伪造审计记录（日志注入）。
        """
        from src.web.routes.cases import _sanitize_log_field

        injected = "a.yaml\n2026-10-02 | INFO | 伪造的审计记录 | 结果: 全部通过"
        cleaned = _sanitize_log_field(injected)
        assert "\n" not in cleaned, "换行必须被折叠，否则可伪造日志行"
        assert "\r" not in cleaned, "回车必须被折叠"
        assert "伪造的审计记录" in cleaned, "折叠后内容应保留（不丢信息）"

        for char in ["\t", "\v", "\f"]:
            assert char not in _sanitize_log_field(f"a{char}b"), (
                f"控制字符 {char!r} 必须被折叠"
            )

        cleaned_long = _sanitize_log_field("x" * 500)
        assert len(cleaned_long) < 500, "超长字段必须被截断"
        assert "已截断" in cleaned_long

        assert _sanitize_log_field("api_user_query.yaml") == (
            "api_user_query.yaml"
        ), "普通文件名应原样保留"

    def test_chinese_filename_import_preserves_extension(
        self, import_client: FlaskClient
    ) -> None:
        """
        中文名 xlsx 上传必须保留扩展名

        回归点: werkzeug 的 secure_filename 剥掉全部非 ASCII 字符，
        实测 secure_filename('用例数据.xlsx') == 'xlsx'（连分隔点一起丢失），
        落盘文件无后缀 -> data_driver 判成"格式不支持"。中文名是中文测试
        团队的高频场景，错误提示与事实完全矛盾。
        """
        from werkzeug.utils import secure_filename as sf

        # 先锁定"问题确实存在"这一前提，避免将来 werkzeug 行为变化导致
        # 本测试变成永真断言而失去意义
        assert sf("用例数据.xlsx") == "xlsx", (
            "前提失效：werkzeug 版本已能保留中文名，本测试需重新评估"
        )

        workbook_bytes = _build_minimal_xlsx()
        response = import_client.post(
            "/api/cases/import",
            data={"file": (io.BytesIO(workbook_bytes), "用例数据.xlsx")},
            content_type="multipart/form-data",
        )

        # 关键: 响应体是 JSON，Flask 2.3 默认把中文转义为 \uXXXX。
        # 必须解析 JSON 后再比对 message，否则中文子串匹配会**假通过**
        # （原始字节里根本不含中文，等于断言失效）。
        payload = response.get_json()
        assert payload["code"] == 200, (
            f"中文名 xlsx 导入应成功，实际: {payload}"
        )
        message = payload["message"]
        assert "不支持的文件格式" not in message, (
            f"中文名 xlsx 仍被判为格式不支持: {message}"
        )
        assert payload["data"]["total"] == 1, "应解析出 1 条用例"
        assert payload["data"]["file_name"] == "用例数据.xlsx", (
            "file_name 仍应回显原始上传名"
        )

    def test_ascii_filename_still_works(self, import_client: FlaskClient) -> None:
        """
        修复不得破坏 ASCII 文件名的既有行为（不回归）

        接口只返回导入统计（total/inserted/updated）不回传用例明细，
        因此断言口径是统计值而非用例编号。
        """
        workbook_bytes = _build_minimal_xlsx()
        response = import_client.post(
            "/api/cases/import",
            data={
                "file": (
                    io.BytesIO(workbook_bytes),
                    "api_user_query_matrix.xlsx",
                )
            },
            content_type="multipart/form-data",
        )
        assert response.status_code == 200, (
            f"ASCII 文件名导入应成功: {response.get_data(as_text=True)[:300]}"
        )
        payload = response.get_json()["data"]
        assert payload["total"] == 1, "应解析出 1 条用例"
        assert payload["file_name"] == "api_user_query_matrix.xlsx", (
            "响应中的 file_name 仍应是原始上传名（业务契约）"
        )
        # inserted+updated 应等于总数（upsert 语义，幂等）
        assert (
            payload["inserted"] + payload["updated"] == payload["total"]
        ), "导入统计自相矛盾"

    def test_path_traversal_still_blocked(self, import_client: FlaskClient) -> None:
        """
        修复不得削弱路径遍历防护（不回归）

        回归点: 新逻辑拆成 stem + suffix 两段分别清洗，必须确认
        "../../secret.yaml" 仍无法逃出临时目录。
        """
        workbook_bytes = _build_minimal_xlsx()
        response = import_client.post(
            "/api/cases/import",
            data={
                "file": (
                    io.BytesIO(workbook_bytes),
                    "../../../../evil.xlsx",
                )
            },
            content_type="multipart/form-data",
        )
        payload = response.get_json()
        # 不应报"文件不存在"（说明文件被写到了预期临时目录内）
        assert "不存在" not in payload["message"], (
            f"路径遍历文件名处理异常: {payload}"
        )
        # 也不应在项目根目录留下 evil.xlsx
        assert not (PROJECT_ROOT / "evil.xlsx").exists(), (
            "路径遍历文件名导致文件被写到项目根目录"
        )


# ======================================================================
# P2: http_client URL 查询串脱敏
# ======================================================================
class TestHttpClientUrlMasking:
    """P2: URL 查询串中的凭据必须脱敏"""

    def test_sensitive_query_params_are_masked(self) -> None:
        """
        _safe_url 必须打码 token/key/password 等查询参数
        """
        secret = "SUPERSECRET-token-xyz"
        url = (
            "https://api.example.com/v1/login"
            f"?token={secret}&user=alice&password=Sup3rPass&key=webhookkey"
        )
        safe = HttpClient._safe_url(url)

        assert secret not in safe, f"token 泄露: {safe}"
        assert "Sup3rPass" not in safe, f"password 泄露: {safe}"
        assert "webhookkey" not in safe, f"key 泄露: {safe}"
        assert "user=alice" in safe, "非敏感查询参数应保留"
        assert "login" in safe, "路径应保留"

    def test_url_without_query_is_preserved(self) -> None:
        """
        无敏感查询串的 URL 应原样（截断后）保留
        """
        url = "https://api.example.com/v1/users?page=1"
        assert HttpClient._safe_url(url) == url
        assert HttpClient._safe_url("https://api.example.com/v1/users") == (
            "https://api.example.com/v1/users"
        )

    def test_fragment_is_stripped(self) -> None:
        """
        fragment 不应出现在日志 URL 中
        """
        safe = HttpClient._safe_url("https://a.com/x?y=1#secretfrag")
        assert "secretfrag" not in safe

    def test_malformed_url_does_not_raise(self) -> None:
        """
        畸形 URL 不得因脱敏而抛异常

        回归点: 该方法在请求日志与异常消息构造路径中被调用，
        自身抛异常会掩盖原始故障。
        """
        result = HttpClient._safe_url("http://[::1")
        assert isinstance(result, str)
        assert result, "畸形 URL 应有占位内容"

    def test_long_url_is_truncated(self) -> None:
        """
        超长 URL 必须被截断，防止长签名串刷爆日志
        """
        long_url = "https://a.com/?" + "&".join(
            f"p{i}=v{i}" for i in range(500)
        )
        safe = HttpClient._safe_url(long_url)
        assert len(safe) < len(long_url), "超长 URL 必须截断"
        assert "已截断" in safe

    def test_request_exception_message_does_not_leak_query(self) -> None:
        """
        连接异常时抛出的 HttpClientError 不得含查询串凭据

        回归点: 修复前异常消息为 f"连接失败: {method} {url}"，url 原样拼接；
        且 ConnectionError 分支还打印 str(exc)——urllib3 的异常文本自带
        完整 URL，会二次绕过脱敏。
        """
        secret = "SUPERSECRET-leak-me"
        client = HttpClient(base_url="https://api.example.com")

        with patch.object(
            client.session,
            "request",
            side_effect=requests.exceptions.ConnectionError(
                f"Max retries exceeded with url: /v1/x?token={secret}"
            ),
        ):
            with pytest.raises(Exception) as exc_info:
                client.get(f"/v1/x?token={secret}")

        message = str(exc_info.value)
        assert secret not in message, f"异常消息泄露查询串凭据: {message}"
        assert "api.example.com" in message, "脱敏后应保留主机名供排障"


# ======================================================================
# 阶段D: http_client 请求体凭据字段脱敏口径对齐
# ======================================================================
class TestHttpClientBodyFieldMasking:
    """请求体侧凭据字段必须与查询串侧同口径脱敏"""

    def test_access_token_in_body_is_masked(self) -> None:
        """
        access_token 放在 JSON body 里必须打码

        回归点: SENSITIVE_BODY_FIELDS 历史上只有 access_key 而没有
        access_token，而 SENSITIVE_QUERY_FIELDS 早已收录。同一份凭据
        放 query 里安全、放 body 里原样进日志——OAuth 风格接口把
        access_token 放 body 是标准做法，等于给这条路径开了个口子。
        """
        secret = "SUPERSECRET-access-token-xyz"
        masked = HttpClient._mask_data(
            {"grant_type": "client_credentials", "access_token": secret}
        )

        assert secret not in str(masked), f"access_token 泄露: {masked}"
        assert masked["access_token"] == "***"
        assert masked["grant_type"] == "client_credentials", (
            "非敏感字段必须原样保留，否则日志失去排障价值"
        )

    @pytest.mark.parametrize(
        "field_name",
        [
            "password",
            "passwd",
            "secret",
            "token",
            "access_token",
            "api_key",
            "apikey",
            "access_key",
            "ACCESS_TOKEN",
            "Api_Key",
        ],
        ids=lambda n: n,
    )
    def test_every_credential_body_field_is_masked(self, field_name: str) -> None:
        """
        凭据字段一律打码且大小写不敏感（内部统一 lower() 比对）
        """
        masked = HttpClient._mask_data({field_name: "SENSITIVE-VALUE"})

        assert masked[field_name] == "***", f"{field_name} 未打码"

    def test_business_key_field_is_not_masked(self) -> None:
        """
        裸 key 字段**不**打码（刻意排除，锁定该决策）

        排除理由: 查询串侧的 key 是为 ?key=xxx 这类回调凭据而设；
        请求体里名为 key 的字段通常是业务数据（字典键、分片键），
        打码会显著削弱排障能力。api_key/apikey 已覆盖真正的
        API 凭据命名。
        """
        masked = HttpClient._mask_data({"key": "order-20260916-0001"})

        assert masked["key"] == "order-20260916-0001", (
            "业务 key 字段不应被打码，否则日志无法定位问题"
        )

    def test_body_credential_set_covers_query_credential_set(self) -> None:
        """
        结构不变量: 查询串侧的凭据字段必须全部被请求体侧覆盖

        这是本轮缺口的根因守卫——两侧字段表是各自独立维护的字面量，
        历史上新增 access_token 时只改了查询串侧。凡是凭据类字段
        （裸 key 除外），body 侧漏一个就是一次凭据进日志的缺口，
        本断言让它在 CI 上立刻变红而不是等到被人发现。
        """
        query_credentials = set(SENSITIVE_QUERY_FIELDS) - {"key"}

        assert query_credentials <= set(SENSITIVE_BODY_FIELDS), (
            "请求体脱敏字段未覆盖查询串侧凭据: "
            f"{sorted(query_credentials - set(SENSITIVE_BODY_FIELDS))}"
        )



# ======================================================================
# P2: 邮件报告 HTML 转义
# ======================================================================
class TestEmailTemplateEscaping:
    """P2: 报告中的用户可控字段必须 HTML 转义"""

    def test_esc_escapes_all_metacharacters(self) -> None:
        """
        _esc 必须转义 & < > " ' 五个字符
        """
        esc = EmailReportTemplate._esc
        assert esc("<script>") == "&lt;script&gt;"
        assert esc("a & b") == "a &amp; b"
        assert esc('say "hi"') == "say &quot;hi&quot;"
        assert esc("it's") in ("it&#x27;s", "it&#39;s")
        assert esc(None) == ""
        assert esc(123) == "123"

    def test_module_name_html_injection_is_escaped(self) -> None:
        """
        模块名中的 HTML 标签必须被转义（模块名可经 /api/cases 写入）
        """
        from tests.test_email_template_demo import make_stat

        stat = make_stat(
            by_module={
                XSS_PAYLOAD: ModuleStat(
                    name=XSS_PAYLOAD,
                    total=10,
                    passed=5,
                    failed=5,
                    pass_rate=0.5,
                )
            }
        )
        html = EmailReportTemplate().render(stat)

        assert XSS_PAYLOAD not in html, "模块名中的原始标签未被转义"
        assert "&lt;img" in html, "模块名应被转义为实体后展示"
        assert "<table" in html, "转义不得破坏模板结构"

    def test_failed_detail_fields_are_escaped(self) -> None:
        """
        失败明细的用例名/模块名/负责人/错误信息均须转义
        """
        from tests.test_email_template_demo import make_stat

        payload = '<a href="http://evil.example/reset">点我重置密码</a>'
        details = [
            FailedCaseDetail(
                uuid="u1",
                name=payload,
                full_name="tests#test_x",
                status="failed",
                duration_ms=100,
                module=payload,
                priority=payload,
                error_message=payload,
            )
        ]
        html = EmailReportTemplate().render(
            make_stat(), failed_details=details
        )

        assert payload not in html, "失败明细中的 HTML 注入未被转义"
        assert "<a href=" not in html, "不应出现可点击的注入外链"
        assert "&lt;a href=" in html, "应转义为实体文本"

    def test_execution_id_is_escaped_in_title_and_header(self) -> None:
        """
        execution_id 在 title 与标题区也须转义（纵深防御）
        """
        from tests.test_email_template_demo import make_stat

        payload = "</title><script>alert(1)</script>"
        html = EmailReportTemplate().render(make_stat(), payload)

        assert payload not in html, "execution_id 未被转义"
        assert "&lt;/title&gt;" in html

    def test_normal_chinese_content_still_renders(self) -> None:
        """
        正常中文内容不受转义影响（防止过度转义破坏可读性）
        """
        from tests.test_email_template_demo import make_stat

        stat = make_stat(
            by_module={
                "用户管理": ModuleStat(
                    name="用户管理",
                    total=50,
                    passed=50,
                    failed=0,
                    pass_rate=1.0,
                )
            }
        )
        html = EmailReportTemplate().render(stat)
        assert "用户管理" in html, "正常中文不应被转义破坏"


# ======================================================================
# P2: 企微响应结构异常
# ======================================================================
class TestWechatResponseStructure:
    """P2: 非 dict 响应不得让 AttributeError 逃出 send()"""

    @pytest.fixture(autouse=True)
    def enable_wechat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        打开企微总开关（send 首行即检查该开关，关闭时不发请求）
        """
        monkeypatch.setenv("TM_WECHAT_ENABLED", "true")
        monkeypatch.setenv("TM_WECHAT_WEBHOOK_URL", FAKE_WECHAT_BASE + "?key=x")

    @pytest.mark.parametrize(
        "payload",
        [None, [1, 2, 3], "plain text", 42, True],
        ids=["null", "array", "string", "number", "bool"],
    )
    def test_non_dict_response_returns_false(self, payload: object) -> None:
        """
        响应是合法 JSON 但非对象时，send() 必须返回 False 而不抛异常

        回归点: 修复前只挡 ValueError，result.get 在非 dict 上抛
        AttributeError 逃出 send()，违反 BaseNotifier "绝不向上抛"契约，
        还会触发 4 次无意义重试 + 7 秒退避，并把死信原因替换成
        AttributeError，真实业务原因彻底丢失。
        """
        notifier = WeChatNotifier()
        notifier.webhook_url = FAKE_WECHAT_BASE + "?key=test"

        fake_response = _FakeResponse(status_code=200, json_payload=payload)
        with patch.object(requests, "post", return_value=fake_response):
            result = notifier.send(make_notification())

        assert result is False, (
            f"非 dict 响应（{type(payload).__name__}）应返回 False，实际 {result}"
        )

    def test_non_json_response_still_returns_false(self) -> None:
        """
        非 JSON 响应仍走既有 ValueError 分支返回 False（不回归）
        """
        notifier = WeChatNotifier()
        notifier.webhook_url = FAKE_WECHAT_BASE + "?key=test"

        fake_response = _FakeResponse(
            status_code=200,
            json_payload="<html>not json</html>",
            raise_value=True,
        )
        with patch.object(requests, "post", return_value=fake_response):
            assert notifier.send(make_notification()) is False

    def test_normal_dict_response_still_succeeds(self) -> None:
        """
        正常 {"errcode": 0} 响应仍判定成功（不回归）
        """
        notifier = WeChatNotifier()
        notifier.webhook_url = FAKE_WECHAT_BASE + "?key=test"

        fake_response = _FakeResponse(
            status_code=200, json_payload={"errcode": 0, "errmsg": "ok"}
        )
        with patch.object(requests, "post", return_value=fake_response):
            assert notifier.send(make_notification()) is True

    def test_business_error_response_still_fails(self) -> None:
        """
        errcode 非 0 的 dict 响应仍判定失败（不回归）
        """
        notifier = WeChatNotifier()
        notifier.webhook_url = FAKE_WECHAT_BASE + "?key=test"

        fake_response = _FakeResponse(
            status_code=200,
            json_payload={"errcode": 93000, "errmsg": "invalid webhook key"},
        )
        with patch.object(requests, "post", return_value=fake_response):
            assert notifier.send(make_notification()) is False


# ======================================================================
# P2: logger.py 通道配置
# ======================================================================
class TestLoggerChannelHardening:
    """P2: 控制台通道诊断信息关闭 + 主日志文件级别跟随配置"""

    @staticmethod
    def _drain_loguru_queue() -> None:
        """
        等待 loguru 异步 sink 队列落盘

        LogManager.setup 的三个 sink 均以 enqueue=True 添加（多进程写入
        安全），写入发生在后台线程。直接读文件会读到尚未刷盘的内容。
        """
        from loguru import logger as loguru_logger

        for handler in list(loguru_logger._core.handlers.values()):
            handler.complete_queue()

    @staticmethod
    def _read_log_files(log_dir: Path) -> str:
        """
        读取日志目录下所有日志文件的合并内容

        参数:
            log_dir (Path): 日志目录

        返回:
            str: 全部 .log 文件的文本拼接
        """
        chunks = [
            path.read_text(encoding="utf-8", errors="replace")
            for path in log_dir.glob("*.log")
        ]
        return "\n".join(chunks)

    @pytest.fixture
    def isolated_loguru(self):
        """
        测试结束后把全局日志配置还原为会话初始状态

        为什么必须还原: conftest.py 的 pytest_configure 已通过
        LogManager.setup() 安装了三个 sink；本类的用例会再次调用
        LogManager.setup()（它的内部先 logger.remove() 摘除全部 handler），
        若不还原，本文件之后执行的所有用例都失去日志通道。

        为什么不能用 loguru 的 configure(handlers=...):
        该 API 内部执行 self.add(**params)，只接受**参数字典**，
        传入 Handler 对象会抛
        `TypeError: Logger.add() argument after ** must be a mapping, not Handler`，
        表现为 teardown 阶段 ERROR、被 pytest 判为用例失败。
        这里改为调用与 conftest 同一个公开入口 LogManager.setup()，
        用相同参数重新构建，既不依赖 loguru 私有属性，也不产生上述异常。

        参数:
            无

        返回:
            None（yield 型 fixture）
        """
        yield
        # 用 conftest 的同一入口、同一参数还原会话初始日志配置
        LogManager._initialized = False
        LogManager.setup(
            log_level=env_manager.log_level, log_dir=env_manager.log_dir
        )

    def test_local_variables_not_leaked_into_console(
        self, tmp_path: Path, isolated_loguru, monkeypatch
    ) -> None:
        """
        局部变量值不得出现在控制台输出（v2 审查 V2-P0-1 重写）

        旧版是**假通过**: 它传 console_output=False，而 logger.py 的控制台
        sink 被 `if console_output:` 守卫——该 sink 根本没被创建；断言又只读
        落盘日志文件，而文件 sink 在修复前就已经是 diagnose=False。于是把
        控制台 sink 的 diagnose 改回 True，测试照样全绿。

        现改为 console_output=True + 捕获 sys.stderr，走真实的
        "控制台输出 → 局部变量值" 泄露路径。

        阳性对照: 同时断言异常消息本身出现在捕获流里。若控制台 sink 压根
        没把内容写进来，"敏感值不出现" 会因为捕获到空串而空转通过——
        这正是旧版假通过的同类风险，故必须显式排除。

        **实测口径（v5 定向复现更正）**: loguru 0.7.2 的 `diagnose=True` 会
        对**异常发生那一行源码上的表达式求值**并渲染 `|   -> 值`。
        敏感变量只要作为**实参出现在该行**，其值就会被打进日志:
            connect(db_password)  ->  |       -> 'Sup3rSecretDbPass'
        变量赋值与 raise **分行**时则不泄露（该行无引用可求值）。
        因此本用例刻意采用"变量作为异常行实参"的形态——这是让
        `diagnose=False -> True` 变异能真正变红的唯一形态; 用分行形态
        写出来的测试对 diagnose 变异完全不敏感，是空转。
        """
        import io

        from loguru import logger as loguru_logger
        from src.common.logger import LogManager

        captured = io.StringIO()
        monkeypatch.setattr("sys.stderr", captured)

        LogManager._initialized = False
        LogManager.setup(
            log_level="INFO", log_dir=tmp_path, console_output=True
        )

        def _connect(password: str) -> None:
            """模拟连接函数（参数名刻意不同于变量名，确保求值的是实参值）"""
            raise ConnectionError("连接失败")

        def _raise_with_secret() -> None:
            """敏感变量作为异常发生行上的**函数实参**出现"""
            db_password = "Sup3rSecretDbPass"
            _connect(db_password)

        try:
            _raise_with_secret()
        except ConnectionError:
            loguru_logger.exception("捕获到数据库异常")

        self._drain_loguru_queue()
        console_text = captured.getvalue()

        # 阳性对照: 控制台 sink 确实捕获到了内容
        assert "捕获到数据库异常" in console_text, (
            "控制台 sink 未捕获到任何内容，本用例的脱敏断言会空转通过:\n"
            f"{console_text!r}"
        )
        assert "连接失败" in console_text, (
            f"异常消息本身应出现在控制台输出（业务需要）:\n{console_text!r}"
        )
        # 真正的断言: 敏感变量值不得出现在控制台输出
        assert "Sup3rSecretDbPass" not in console_text, (
            "局部变量值泄露到控制台输出：diagnose 未正确关闭"
        )

    def test_log_format_renders_exception(self, tmp_path: Path) -> None:
        """
        统一日志格式**确实渲染 {exception}**——traceback 一直在日志里

        **v5 审查更正（v3 本用例的前提是错的）**:
        v3 版本断言"格式不含 {exception}"，理由是"loguru 必须格式串引用
        {exception} 才渲染 traceback"。实测 handler._decolorized_format 为
        `'{time:...} | {message}\\n{exception}'` —— **含 {exception}**，
        traceback 一直在渲染。且 v3 那条断言读的是
        `handler._precolorized_formats`，该属性在 loguru 0.7.2 中是
        **空 dict**（仅在有 ANSI 输出时才被填充），join 出空串 →
        `"{exception}" not in ""` **恒真**，是条空测。

        本用例改为断言**实际行为**（格式含 {exception}），并用
        `_decolorized_format`（非空、Python 侧可达）作为观测点。

        为什么这仍然是有效守卫:
            - {exception} 存在 ⇒ traceback（文件路径+行号+异常消息）进日志，
              这是**既有暴露面**，由 diagnose 控制不了；
            - 真正挡住敏感变量值的是 diagnose=False（见
              test_local_variables_not_leaked_into_console）；
            - 有人误以为"格式不含 {exception} 所以安全"而删除它时，
              本用例会红——因为断言的是"含"，删除即破坏该事实。
        """
        from loguru import logger as loguru_logger
        from src.common.logger import LogManager

        LogManager._initialized = False
        LogManager.setup(
            log_level="INFO", log_dir=tmp_path, console_output=False
        )
        try:
            formats = [
                getattr(handler, "_decolorized_format", None)
                for handler in loguru_logger._core.handlers.values()
            ]
            # 观测点自检: 若 _decolorized_format 也不可达，本用例会空转
            assert any(formats), (
                f"loguru Handler 未暴露 _decolorized_format，本用例会空转通过: {formats}"
            )
            for fmt in formats:
                assert fmt is not None and "{exception}" in fmt, (
                    "统一日志格式应含 {exception}（traceback 一直在渲染，"
                    f"这是既有暴露面）: {fmt!r}"
                )
        finally:
            LogManager._initialized = False

    def test_console_sink_declares_diagnose_disabled(self) -> None:
        """
        控制台 sink 源码显式声明 diagnose=False（源码级守卫）

        为什么用源码断言而不是运行时反射: loguru 0.7.2 的 Handler 不暴露
        diagnose 属性（诊断渲染在 C 层 writer 内，Python 侧不可达），
        实测 `handler.diagnose` 与 `handler._exception_formatter.diagnose`
        均为 None。反射断言只能得到一个恒真的 None，无法承担守卫职责。

        它守的是"控制台 sink 这道诊断屏障没有被删掉或反向打开"。
        行为层由 test_local_variables_not_leaked_into_console 守（该用例
        已改为对 diagnose 变异真正敏感的形态），本条守配置，两条分工
        不互相冒充。
        """
        import inspect

        from src.common.logger import LogManager

        source = inspect.getsource(LogManager.setup)
        # 用注释锚点切出控制台 sink 段：不能用 split(")")，filter lambda
        # 里的 setdefault("trace_id", "-") 会把区间提前截断
        console_block = source.split("if console_output:")[1].split(
            "# 文件输出通道"
        )[0]
        # 断言前剥离注释行：否则本文件（及 logger.py）里任何提到这两个
        # 开关的说明性注释都会让断言读到散文而非真实配置
        code_lines = [
            line
            for line in console_block.splitlines()
            if not line.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)

        assert "diagnose=False" in code, (
            "控制台 sink 必须显式设置 diagnose=False——"
            "格式一旦加入 {exception}，诊断渲染会带出源码行与表达式求值"
        )
        assert "diagnose=True" not in code

    def test_file_sink_level_follows_configured_level(
        self, tmp_path: Path, isolated_loguru
    ) -> None:
        """
        主日志文件级别必须跟随 log_level，不得硬编码 DEBUG

        回归点: 修复前主日志文件固定 level="DEBUG"，导致 log_level 参数
        对它完全无效——生产设 ERROR 仍会全量落盘，放大敏感信息泄露面。

        验证方式: 按 WARNING 级别配置后分别打 INFO 与 WARNING 两条日志，
        断言 INFO 不落盘、WARNING 落盘（即配置真正生效）。
        """
        from loguru import logger as loguru_logger
        from src.common.logger import LogManager

        LogManager._initialized = False
        LogManager.setup(
            log_level="WARNING", log_dir=tmp_path, console_output=False
        )

        loguru_logger.info("INFO_SHOULD_BE_FILTERED_OUT")
        loguru_logger.warning("WARNING_SHOULD_BE_LOGGED")
        self._drain_loguru_queue()

        content = self._read_log_files(tmp_path)
        assert "WARNING_SHOULD_BE_LOGGED" in content, "WARNING 应落盘"
        assert "INFO_SHOULD_BE_FILTERED_OUT" not in content, (
            "INFO 日志仍被写入主日志文件——主日志通道硬编码了 DEBUG，"
            "log_level 配置对其完全失效"
        )


# ======================================================================
# 测试辅助
# ======================================================================
class _FakeResponse:
    """requests.Response 的最小替身，仅供本文件单测使用"""

    def __init__(
        self,
        status_code: int,
        json_payload: object,
        raise_value: bool = False,
    ) -> None:
        """
        构造替身响应

        参数:
            status_code (int): HTTP 状态码
            json_payload (object): json() 的返回值；raise_value 为True时
                                  作为响应文本并使 json() 抛 ValueError
            raise_value (bool): json() 是否抛 ValueError
        """
        self.status_code = status_code
        self._json_payload = json_payload
        self._raise_value = raise_value
        self.text = str(json_payload)

    def json(self) -> object:
        """
        模拟 Response.json()

        异常:
            ValueError: raise_value 为 True 时抛出（模拟非 JSON 响应体）
        """
        if self._raise_value:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_payload


def make_notification() -> Notification:
    """
    构造一个最小 Notification 实例

    返回:
        Notification: 可直接投递给各 Notifier.send 的通知对象
    """
    return Notification(
        title="测试报告",
        content="报告正文",
        level="info",
        execution_id="RUN-TEST-0001",
    )


def build_minimal_xlsx() -> bytes:
    """
    构造一个内容合法的最小 xlsx（表头+1行数据）

    返回:
        bytes: xlsx 文件字节流
    """
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["case_id", "name", "module", "priority", "tags"])
    sheet.append(["TM-XLSX-0001", "中文名用例", "用户管理", "P1", "smoke"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _build_minimal_xlsx() -> bytes:
    """
    build_minimal_xlsx 的模块级别名（保持测试内调用点可读）

    返回:
        bytes: xlsx 文件字节流
    """
    return build_minimal_xlsx()


def caplog_at_error():
    """
    构造一个捕获 loguru ERROR 级日志的上下文管理器

    loguru 不走标准 logging 体系，需自行挂接 sink。

    返回:
        contextlib.AbstractContextManager: 退出后为 list[record]，
        每条 record 为 loguru 的 dict 记录
    """
    return _caplog_at("ERROR")


def caplog_at_warning():
    """
    构造一个捕获 loguru WARNING 级日志的上下文管理器

    返回:
        contextlib.AbstractContextManager: 退出后为 list[record]
    """
    return _caplog_at("WARNING")


def _caplog_at(level_name: str):
    """
    构造指定级别的 loguru 日志捕获器

    参数:
        level_name (str): loguru 级别名（ERROR/WARNING/...）

    返回:
        contextlib.AbstractContextManager: 退出后为 list[record]
    """
    from loguru import logger as loguru_logger

    @contextlib.contextmanager
    def _capture():
        records: list = []

        def sink(message) -> None:
            record = message.record
            if record["level"].name == level_name:
                records.append(record)

        handler_id = loguru_logger.add(
            sink, level=level_name, format="{message}"
        )
        try:
            yield records
        finally:
            loguru_logger.remove(handler_id)

    return _capture()
