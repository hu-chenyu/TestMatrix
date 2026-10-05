"""
Day45 全量 bug 审查第 2 批修复的配套测试（安全与脱敏）

对应 6 个被修复的问题（每条都有"回退即变红"的变异验证）:
    问题1 (P1) 生产环境日志配置从未生效
        → get_logger() 惰性触发 setup()，由 _initialized 守卫使初始化只做一次
    问题2 (P1) 嵌套凭据明文进日志（dict 分支不递归）
        → mask_data 对 dict/list 同等递归，带深度上限与循环引用保护
    问题3 (P1) 响应体完全不过脱敏
        → _safe_body 对 JSON 响应体走同一套递归脱敏，非 JSON 原样返回
    问题4 (P2) TESTING 模式回显 SQL 参数值
        → mask_sql_parameters 只抹 [parameters: ...]，保留 [SQL: ...]
    问题5 (P2) Redis URL 内嵌口令原样进日志
        → 复用 mask_url 的 userinfo 脱敏
    问题6 (P3) 断言失败信息直出敏感 URL 与全部响应头
        → 复用 mask_url / mask_headers / SENSITIVE_HEADERS

测试链路铁律:
    - 占位凭据一律用 PLACEHOLDER_* 字面量，**不构造任何形似真密钥的值**
    - 不发起真实网络请求：断言层与 http_client 层用响应替身
    - 零 skip / 零 xfail / 不断言 True
"""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.common import security  # noqa: E402
from src.common.assertion import assert_header, assert_status_code  # noqa: E402
from src.common.http_client import HttpClient  # noqa: E402
from src.common.logger import LogManager  # noqa: E402
from src.common.security import (  # noqa: E402
    CIRCULAR_MARK,
    DEPTH_EXCEEDED_MARK,
    MASK,
    mask_data,
    mask_headers,
    mask_sql_parameters,
    mask_url,
)

# 占位凭据：非真实密钥，仅用于断言脱敏行为
PLACEHOLDER_PW = "PLACEHOLDER-NOT-A-REAL-PASSWORD"
PLACEHOLDER_TOKEN = "PLACEHOLDER-NOT-A-REAL-TOKEN"


class _FakeResponse:
    """requests.Response 的最小替身（断言层与 http_client 层共用）"""

    def __init__(self, status_code=200, text="", headers=None, url=""):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url


# ===========================================================================
# 问题2: 嵌套凭据递归脱敏（dict 与 list 必须同等递归）
# ===========================================================================


def test_问题2_嵌套dict凭据被脱敏():
    """修复前 dict 分支只看顶层键、value 原样返回，嵌套凭据明文泄露"""
    masked = mask_data({"user": {"password": PLACEHOLDER_PW}})

    assert masked == {"user": {"password": MASK}}


def test_问题2_list内嵌dict凭据被脱敏():
    """`{"items":[{"access_token":...}]}` 是 dict 里套 list，同样要脱敏"""
    masked = mask_data({"items": [{"access_token": PLACEHOLDER_TOKEN}]})

    assert masked == {"items": [{"access_token": MASK}]}


def test_问题2_深层嵌套五层被脱敏():
    """被测系统可构造任意深结构，递归必须贯穿到底"""
    deep = {
        "l1": {"l2": {"l3": {"l4": {"l5": {"password": PLACEHOLDER_PW}}}}}
    }

    masked = mask_data(deep)

    dumped = json.dumps(masked, ensure_ascii=False)
    assert MASK in dumped, "五层嵌套内的 password 未被打码"
    assert PLACEHOLDER_PW not in dumped


def test_问题2_超过深度上限标记而非无限下钻():
    """超过 MAX_MASK_DEPTH 的部分标记 [嵌套过深]，不栈溢出"""
    node: dict = {"password": PLACEHOLDER_PW}
    for _ in range(security.MAX_MASK_DEPTH + 3):
        node = {"n": node}

    masked = mask_data(node)

    assert DEPTH_EXCEEDED_MARK in json.dumps(masked, ensure_ascii=False)


def test_问题2_循环引用不导致无限递归():
    """dict 自引用必须被识别为环并标记，否则朴素递归会挂死"""
    cyclic: dict = {"name": "root"}
    cyclic["self"] = cyclic

    masked = mask_data(cyclic)

    assert masked["self"] == CIRCULAR_MARK, "循环引用未被识别"
    assert masked["name"] == "root", "非敏感字段值不应被改动"


def test_问题2_共享子对象不被误判为循环():
    """同一个 dict 被引用两次不是环；用 id 判环容易在这里误伤"""
    shared = {"k": "v"}
    payload = {"first": shared, "second": shared}

    masked = mask_data(payload)

    assert masked["first"] == {"k": "v"}
    assert masked["second"] == {"k": "v"}
    assert CIRCULAR_MARK not in json.dumps(masked)


def test_问题2_非敏感键的嵌套值不被误脱敏():
    """脱敏不能把业务数据一并打掉，否则排障能力被削弱"""
    masked = mask_data(
        {"order": {"id": "SO-2026-0001", "items": [{"sku": "A-1", "qty": 2}]}}
    )

    assert masked == {"order": {"id": "SO-2026-0001", "items": [{"sku": "A-1", "qty": 2}]}}


def test_问题2_按语义位置选用不同字段表():
    """查询串口径含裸 key（回调凭据），请求体口径不含（业务数据）"""
    assert "key" not in security.SENSITIVE_BODY_FIELDS
    assert "key" in security.SENSITIVE_QUERY_FIELDS
    # 同样的 {"key": ...}，在 body 位置保持原样、在 query 位置被打码
    assert mask_data({"key": "order-1"})["key"] == "order-1"
    assert mask_data({"key": "cb-1"}, security.SENSITIVE_QUERY_FIELDS)["key"] == MASK


def test_问题2_空值与标量不被破坏():
    """脱敏是日志增强，不能改变非容器类型的语义"""
    assert mask_data(None) == "-"
    assert mask_data("plain") == "plain"
    assert mask_data(42) == 42


# ===========================================================================
# 问题3: 响应体脱敏
# ===========================================================================


def test_问题3_JSON响应体中的嵌套凭据被脱敏():
    """API 返回的凭据都在响应里——请求侧打码而响应侧不打码是最大漏检面"""
    body = json.dumps(
        {"code": 0, "data": {"access_token": PLACEHOLDER_TOKEN, "pwd": PLACEHOLDER_PW}}
    )
    response = _FakeResponse(
        text=body, headers={"Content-Type": "application/json"}, url="https://x/y"
    )

    safe = HttpClient._safe_body(response)

    assert PLACEHOLDER_TOKEN not in safe, "响应体里的 access_token 明文进日志"
    assert MASK in safe


def test_问题3_非JSON响应体不被误解析():
    """HTML/纯文本走原样返回：正则脱敏误伤与漏伤并存，结构化解析才可靠"""
    html = "<html><body>token=" + PLACEHOLDER_TOKEN + "</body></html>"
    response = _FakeResponse(text=html, headers={"Content-Type": "text/html"})

    safe = HttpClient._safe_body(response)

    assert safe == html, "非 JSON 响应体必须原样返回，不得强行解析"


def test_问题3_声称JSON但解析失败时原样返回():
    """网关截断的 body 会让 json.loads 失败，脱敏不得因此抛错"""
    broken = '{"data": {"access_token": "' + PLACEHOLDER_TOKEN
    response = _FakeResponse(text=broken, headers={"Content-Type": "application/json"})

    safe = HttpClient._safe_body(response)

    assert safe == broken


def test_问题3_无ContentType但以花括号开头也按JSON处理():
    """部分服务端不发 Content-Type，形态明确时仍应脱敏"""
    response = _FakeResponse(text='{"password":"' + PLACEHOLDER_PW + '"}', headers={})

    safe = HttpClient._safe_body(response)

    assert PLACEHOLDER_PW not in safe


def test_问题3_读取异常时沿用占位文案():
    """二进制/编码异常不得让日志链路崩"""
    class _Boom:
        headers = {}

        @property
        def text(self):
            raise UnicodeDecodeError("utf-8", b"\x00", 0, 1, "bad")

    safe = HttpClient._safe_body(_Boom())

    assert "响应体读取失败" in safe


# ===========================================================================
# 问题5: Redis URL userinfo 脱敏
# ===========================================================================


def test_问题5_含口令的RedisURL被脱敏():
    """口令在 userinfo 段，query 脱敏覆盖不到"""
    safe = mask_url("redis://admin:" + PLACEHOLDER_PW + "@10.0.0.5:6379/0")

    assert PLACEHOLDER_PW not in safe
    assert MASK in safe


def test_问题5_脱敏后host与port与db仍可见():
    """打码只针对凭据，排障需要的连接目标必须保留"""
    safe = mask_url("redis://admin:" + PLACEHOLDER_PW + "@10.0.0.5:6379/3")

    assert "10.0.0.5" in safe and "6379" in safe and safe.endswith("/3")


def test_问题5_不含口令的URL不受影响():
    """无凭据时不应产生多余改动（否则日志对比会变噪声）"""
    for url in ("redis://127.0.0.1:6379/0", "redis://user@host:6379/2"):
        assert mask_url(url) == url


def test_问题5_cache层确实使用了脱敏():
    """源码形态守卫：cache.py 的两处日志必须经 mask_url

    这条用源码文本断言而非运行行为——构建失败分支需要构造一个
    非法 Redis URL 才能触发，而 RedisHealthChecker 的健康闸会在
    之前就短路。源码形态断言能锁住"是否调用了 mask_url"这个
    真正容易回退的点。
    """
    source = (PROJECT_ROOT / "src" / "core" / "cache.py").read_text(encoding="utf-8")
    assert "mask_url(url)" in source
    # 原样拼接 url 的写法必须已消失
    assert "URL: {url} |" not in source
    assert "URL: {url}\"" not in source


# ===========================================================================
# 问题6: 断言层脱敏
# ===========================================================================


def test_问题6_状态码断言失败信息中的URL被脱敏():
    """断言失败信息会进 pytest stdout 与 Allure 报告，泄露寿命远长于日志"""
    response = _FakeResponse(
        status_code=500,
        text="oops",
        url="https://api.test/v1/login?token=" + PLACEHOLDER_TOKEN,
    )

    with pytest.raises(AssertionError) as exc:
        assert_status_code(response, 200)

    assert PLACEHOLDER_TOKEN not in str(exc.value), "断言信息里 URL 的 token 未脱敏"
    assert "api.test" in str(exc.value), "脱敏后仍应保留可定位的 host"


def test_问题6_响应头不存在时dump的敏感头被脱敏():
    """原实现 dump 全部响应头，Set-Cookie/Authorization 直接进失败信息"""
    response = _FakeResponse(
        status_code=200,
        text="{}",
        headers={"Set-Cookie": "sid=" + PLACEHOLDER_PW, "X-Trace": "t-1"},
    )

    with pytest.raises(AssertionError) as exc:
        assert_header(response, "X-Missing")

    message = str(exc.value)
    assert PLACEHOLDER_PW not in message, "Set-Cookie 值未被脱敏"
    assert "X-Trace" in message, "非敏感头应保持可见，否则排障信息丢失"


def test_问题6_敏感头值不匹配时实际值与期望值都被脱敏():
    """期望值同样可能写着真凭据，两侧都要打码"""
    response = _FakeResponse(
        status_code=200,
        text="{}",
        headers={"Authorization": "Bearer " + PLACEHOLDER_TOKEN},
    )

    with pytest.raises(AssertionError) as exc:
        assert_header(response, "Authorization", expected="Bearer other-token")

    message = str(exc.value)
    assert PLACEHOLDER_TOKEN not in message
    assert "other-token" not in message, "期望值里的凭据也必须打码"
    assert "Authorization" in message, "头名应保留，便于定位"


def test_问题6_非敏感头值不匹配时值照常显示():
    """脱敏不能把普通头也打掉"""
    response = _FakeResponse(200, "{}", {"Content-Type": "text/html"})

    with pytest.raises(AssertionError) as exc:
        assert_header(response, "Content-Type", expected="application/json")

    assert "text/html" in str(exc.value)


def test_问题6_断言的判定逻辑本身不受影响():
    """脱敏只改消息，不改通过/失败的判定"""
    ok = _FakeResponse(200, "{}", {"X-A": "1", "Content-Type": "text/html"})
    # 匹配 -> 不抛
    assert_status_code(ok, 200)
    assert_header(ok, "X-A", expected="1")
    assert_header(ok, "Content-Type")  # expected 省略时只校验存在
    # 不匹配 -> 抛
    with pytest.raises(AssertionError):
        assert_status_code(ok, 404)
    with pytest.raises(AssertionError):
        assert_header(ok, "X-A", expected="2")


def test_mask_headers_敏感头打码非敏感头保留():
    """公共原语本身的契约"""
    masked = mask_headers({"Authorization": "Bearer x", "X-Trace": "t-1", "Cookie": "c=1"})

    assert masked["Authorization"] == MASK
    assert masked["Cookie"] == MASK
    assert masked["X-Trace"] == "t-1"


# ===========================================================================
# 问题4: TESTING 模式不回显 SQL 参数值
# ===========================================================================


def test_问题4_SQL参数值被抹除而语句保留():
    """[parameters: ...] 是值、[SQL: ...] 是骨架：抹值留骨架"""
    raw = (
        "(sqlite3.OperationalError) no such table: t\n"
        "[SQL: SELECT * FROM test_cases WHERE case_id = ?]\n"
        "[parameters: ('TM-SECRET-0001', 1, 0)]"
    )

    masked = mask_sql_parameters(raw)

    assert "TM-SECRET-0001" not in masked, "SQL 参数值未被抹除"
    assert "[SQL: SELECT * FROM test_cases WHERE case_id = ?]" in masked, "SQL 骨架应保留"
    assert "no such table: t" in masked


def test_问题4_无parameters段时原样返回():
    """非 SQL 异常不该被无谓改写"""
    raw = "用例查询数据库异常: something went wrong"

    assert mask_sql_parameters(raw) == raw


def test_问题4_TESTING档位响应体不含参数值(tmp_path, monkeypatch):
    """端到端：DB 异常在 TESTING 下进 HTTP 响应体时，参数值不得出现"""
    from src.core import case_manager as cm
    from src.db.db_session import DatabaseSession
    from src.web import create_app

    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "sqlmask.db"))
    DatabaseSession.reset()
    DatabaseSession.init_db()

    leaked = (
        "用例查询数据库异常: (sqlite3.OperationalError) no such table: t\n"
        "[SQL: SELECT * FROM test_cases WHERE case_id = ?]\n"
        "[parameters: ('TM-SECRET-0001', 1, 0)]"
    )

    original = cm.CaseManager.list_cases_paged
    cm.CaseManager.list_cases_paged = classmethod(
        lambda cls, *a, **k: (_ for _ in ()).throw(cm.CaseManagerError(leaked))
    )
    try:
        client = create_app("test").test_client()
        response = client.get("/api/cases/")
    finally:
        cm.CaseManager.list_cases_paged = original
        DatabaseSession.reset()

    body = response.get_data(as_text=True)
    assert response.status_code == 500
    assert "TM-SECRET-0001" not in body, "TESTING 响应体泄露了 SQL 参数值"
    assert "[SQL: SELECT * FROM test_cases WHERE case_id = ?]" in body, (
        "SQL 骨架应保留供排障"
    )


def test_问题4_handle500分支在TESTING下也不回显参数值(monkeypatch):
    """`@app.errorhandler(500)` 这条分支端到端走不到，需直接测其契约

    实测 Flask 的处理器查找顺序：未处理异常先按**异常类**回退，
    命中我们注册的 `Exception` 处理器（handle_unexpected_error），
    `handle_500` 只在真正抛出 InternalServerError 时才被调用。
    因此本分支必须从 error_handler_spec 里取出处理器直接调用，
    否则它就是一条始终得不到验证的脱敏代码。
    """
    from src.web import create_app
    from werkzeug.exceptions import InternalServerError

    monkeypatch.setenv("TM_SECRET_KEY", "PLACEHOLDER-NOT-A-REAL-SECRET-KEY")
    app = create_app("test")
    handler = app.error_handler_spec[None][500][InternalServerError]

    leaked = (
        "(sqlite3.OperationalError) no such table: t\n"
        "[SQL: SELECT * FROM test_cases WHERE case_id = ?]\n"
        "[parameters: ('TM-SECRET-0001', 1, 0)]"
    )
    original = RuntimeError(leaked)
    body, code = handler(InternalServerError(original_exception=original))
    message = body["message"]

    assert code == 500
    assert "TM-SECRET-0001" not in message, "handle_500 在 TESTING 下泄露了 SQL 参数值"
    assert "[SQL: SELECT * FROM test_cases WHERE case_id = ?]" in message


def test_问题4_handle500在生产档位回显固定文案(monkeypatch):
    """生产档位不得回显任何内部细节（修复前后都应如此）"""
    from src.web import create_app
    from werkzeug.exceptions import InternalServerError

    monkeypatch.setenv("TM_SECRET_KEY", "PLACEHOLDER-NOT-A-REAL-SECRET-KEY")
    app = create_app("prod")
    handler = app.error_handler_spec[None][500][InternalServerError]

    body, code = handler(
        InternalServerError(
            original_exception=RuntimeError(
                "[SQL: SELECT 1]\n[parameters: ('TM-SECRET-0001',)]"
            )
        )
    )

    assert code == 500
    assert "TM-SECRET-0001" not in body["message"]
    assert "[SQL:" not in body["message"]


def test_问题4_生产档位行为不变(monkeypatch):
    """生产本来就不回显细节，修复不得改变这一点"""
    from src.web import create_app

    # 生产档位对 SECRET_KEY 是 fail-closed 的（Day38 加固），需显式提供
    monkeypatch.setenv("TM_SECRET_KEY", "PLACEHOLDER-NOT-A-REAL-SECRET-KEY")
    app = create_app("prod")
    assert app.config.get("TESTING") is False, "生产档位不应处于 TESTING"


def test_问题4_生产档位不回显异常细节(tmp_path, monkeypatch):
    """生产档位下 500 响应体必须是固定文案，不含 SQL 也不含参数值"""
    from src.core import case_manager as cm
    from src.db.db_session import DatabaseSession
    from src.web import create_app

    monkeypatch.setenv("TM_SECRET_KEY", "PLACEHOLDER-NOT-A-REAL-SECRET-KEY")
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "prodmask.db"))
    DatabaseSession.reset()
    DatabaseSession.init_db()

    leaked = (
        "用例查询数据库异常: (sqlite3.OperationalError) no such table: t\n"
        "[SQL: SELECT * FROM test_cases WHERE case_id = ?]\n"
        "[parameters: ('TM-SECRET-0001', 1, 0)]"
    )
    original = cm.CaseManager.list_cases_paged
    cm.CaseManager.list_cases_paged = classmethod(
        lambda cls, *a, **k: (_ for _ in ()).throw(cm.CaseManagerError(leaked))
    )
    try:
        app = create_app("prod")
        app.config["TESTING"] = False
        response = app.test_client().get("/api/cases/")
    finally:
        cm.CaseManager.list_cases_paged = original
        DatabaseSession.reset()

    body = response.get_data(as_text=True)
    assert response.status_code == 500
    assert "TM-SECRET-0001" not in body
    assert "[SQL:" not in body, "生产档位不得回显 SQL 语句"


# ===========================================================================
# 问题1: get_logger 惰性初始化
# ===========================================================================


def test_问题1_首次get_logger触发setup(monkeypatch):
    """修复前 get_logger 直接 return logger，src/ 内零 setup 调用点，
    生产实际跑 loguru 默认 handler（不产生任何日志文件）"""
    calls: list[dict] = []

    def fake_setup(**kwargs):
        calls.append(kwargs)
        LogManager._initialized = True

    monkeypatch.setattr(LogManager, "setup", staticmethod(fake_setup))
    monkeypatch.setattr(LogManager, "_initialized", False)

    LogManager.get_logger()

    assert len(calls) == 1, "首次 get_logger 未触发 setup"
    assert "log_level" in calls[0] and "log_dir" in calls[0], (
        "setup 参数应取自 env_manager"
    )


def test_问题1_多次get_logger只初始化一次(monkeypatch):
    """11 处模块级 `logger = LogManager.get_logger()` 不得重复配置"""
    calls: list[dict] = []

    def fake_setup(**kwargs):
        calls.append(kwargs)
        LogManager._initialized = True

    monkeypatch.setattr(LogManager, "setup", staticmethod(fake_setup))
    monkeypatch.setattr(LogManager, "_initialized", False)

    for _ in range(5):
        LogManager.get_logger()

    assert len(calls) == 1, f"setup 被调用 {len(calls)} 次，应只有 1 次"


def test_问题1_外部已setup时不再重复初始化(monkeypatch):
    """conftest 的显式 setup() 必须仍然生效，不被惰性分支覆盖或冲突"""
    calls: list[dict] = []

    def fake_setup(**kwargs):
        calls.append(kwargs)
        LogManager._initialized = True

    monkeypatch.setattr(LogManager, "setup", staticmethod(fake_setup))
    monkeypatch.setattr(LogManager, "_initialized", True)

    LogManager.get_logger()

    assert calls == [], "已初始化时不应再次调用 setup"


def test_问题1_setup失败时降级不抛(monkeypatch):
    """日志是旁路能力：初始化失败不能让业务链路崩"""
    def boom(**kwargs):
        raise PermissionError("日志目录不可写")

    monkeypatch.setattr(LogManager, "setup", staticmethod(boom))
    monkeypatch.setattr(LogManager, "_initialized", False)

    logger = LogManager.get_logger()  # 不应抛出

    assert logger is not None


def test_问题1_惰性初始化后日志文件确实生成(tmp_path, monkeypatch):
    """端到端：真实 setup() 走一遍，验证日志文件落盘（生产曾一个都不产生）"""
    monkeypatch.setenv("TM_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("TM_LOG_LEVEL", "INFO")
    monkeypatch.setattr(LogManager, "_initialized", False)
    LogManager._log_dir = tmp_path

    try:
        LogManager.get_logger().info("lazy-init-file-probe")
        files = {p.name for p in tmp_path.glob("*.log")}
        assert any(name.startswith("testmatrix") for name in files), (
            f"未生成主日志文件，实际生成: {files}"
        )
    finally:
        LogManager._log_dir = PROJECT_ROOT / "output" / "logs"
