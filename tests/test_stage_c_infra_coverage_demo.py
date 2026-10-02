"""
TestMatrix 阶段C-1: 基础设施层未覆盖分支的补齐测试

背景:
    阶段1代码审查清零 P0/P1/P2 后进入阶段C（覆盖率 93% -> ≥95%）。
    本文件补齐 db_session.py 与 http_client.py 两处低覆盖模块的
    未覆盖分支。这些分支**不是**为凑数字而挑的边角料，而是两处
    真实的、此前完全没有被测到的运行路径：

    A组 db_session.py
        1.  MySQL 连接串构建（TM_DB_TYPE=mysql 分支）
            —— 全仓零测试。项目默认 sqlite，MySQL 分支一旦回归
            （密码未编码 / 端口读成字符串 / charset 丢失）没有任何
            信号能发现，属于最危险的一类"看起来没坏"的代码。
        2.  非法 TM_DB_TYPE 拒绝
        3.  health_check 正常连通
        4.  health_check 配置非法降级 False
        5.  health_check 连接不可达降级 False
            —— health_check 目前是死代码（已被 base.py 的
            _check_database 带超时探测取代，见阶段D清单）。本组
            测的是它作为公开 API 的契约：连得通 True、任何异常
            吞掉返回 False、绝不向上抛。

    B组 http_client.py
        6.  base_url 非法拒绝
        7.  put/delete/patch 三个快捷方法透传到统一入口
        8.  空 method 拒绝
        9.  Timeout 包装为 HttpClientError
        10. 非 Timeout/ConnectionError 的 RequestException 包装
        11. 绝对 URL 直通，不与 base_url 拼接
        12. 请求体脱敏对 list 递归
        13. 响应体读取失败降级为占位文本
        14. 上下文管理器进入/退出时关闭会话

    刻意不重复的部分（阶段2 Commit A 已有测试，不在此重写）:
        - _safe_url 查询串凭据打码
        - ConnectionError 不得把 URL 带进异常消息
        - 重试方法白名单（P2-39）

测试铁律（对齐 PROJECT_CONTEXT.md 7.12/7.16/7.19）:
    - 每条涉及数据库的用例独立临时 SQLite 库，teardown 严格 reset
    - 不 monkeypatch 任何被测模块的内部实现细节，只替换外部边界
      （网络连接、环境变量），保证测的是真实代码路径
    - 全程 loguru，无 print
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import allure
import pytest
import requests
from src.common.http_client import HttpClient, HttpClientError
from src.db.db_session import DatabaseSession

# 不参与网络访问的占位域名：所有用例都在真正发起请求前就短路返回
DUMMY_BASE_URL = "http://unit.test"


# ===========================================================================
# 夹具
# ===========================================================================
@pytest.fixture
def temp_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Path]:
    """
    独立临时 SQLite 库夹具（function 级）

    环境准备:
        - TM_DB_TYPE=sqlite + TM_DB_SQLITE_PATH 指向 pytest 临时目录
        - reset 掉进程级引擎单例后 init_db 建表

    teardown:
        - reset 释放引擎并断开连接（SQLite 文件句柄不释放则 unlink 失败）
        - 删除临时库文件

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest 临时目录

    返回:
        Iterator[Path]: yield 临时库文件路径
    """
    db_path = tmp_path / "stage_c_infra.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_path
    DatabaseSession.reset()
    db_path.unlink(missing_ok=True)


# ===========================================================================
# A组: db_session.py
# ===========================================================================
@allure.feature("阶段C覆盖率补齐")
@allure.story("数据库会话管理")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestDatabaseSessionCoverage:
    """MySQL连接串构建 / 非法类型拒绝 / health_check三态"""

    def test_mysql_url_built_with_encoded_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        MySQL 连接串构建: 密码含 @ / 空格 / 斜杠 / # 时必须 URL 编码。

        不编码的后果: p@ss 里的 @ 会让 SQLAlchemy 把 host 段解析成
        "p"，连接直接连到错误主机且报错信息极具误导性。本条断言
        锁死 quote_plus 的编码结果，防止有人"简化"这行拼接。
        """
        monkeypatch.setenv("TM_DB_TYPE", "mysql")
        monkeypatch.setenv("TM_DB_MYSQL_HOST", "db.internal")
        monkeypatch.setenv("TM_DB_MYSQL_PORT", "3307")
        monkeypatch.setenv("TM_DB_MYSQL_USER", "tm_user")
        monkeypatch.setenv("TM_DB_MYSQL_PASSWORD", "p@ss w/rd#1")
        monkeypatch.setenv("TM_DB_MYSQL_DATABASE", "tm_db")

        url = DatabaseSession._build_db_url()

        assert url == (
            "mysql+pymysql://tm_user:p%40ss+w%2Frd%231@db.internal:3307/"
            "tm_db?charset=utf8mb4"
        ), "密码中的 @ 空格 / # 必须 URL 编码，端口须为整数、charset 须保留"

    def test_mysql_url_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        MySQL 配置缺省: 只设 TM_DB_TYPE=mysql 时其余项走内置默认值，
        连接串仍应合法可解析（而不是拼出 None 字面量）。
        """
        monkeypatch.setenv("TM_DB_TYPE", "mysql")
        for key in (
            "TM_DB_MYSQL_HOST",
            "TM_DB_MYSQL_PORT",
            "TM_DB_MYSQL_USER",
            "TM_DB_MYSQL_PASSWORD",
            "TM_DB_MYSQL_DATABASE",
        ):
            monkeypatch.delenv(key, raising=False)

        url = DatabaseSession._build_db_url()

        assert url == (
            "mysql+pymysql://root:@127.0.0.1:3306/testmatrix?charset=utf8mb4"
        ), "MySQL 分支缺省值应为 127.0.0.1:3306/root/testmatrix"

    def test_invalid_db_type_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        非法数据库类型: TM_DB_TYPE=postgresql 必须显式抛 ValueError，
        不得静默回落 sqlite——静默回落会让运维以为连了 MySQL，
        实际数据全写进了本地文件库。
        """
        monkeypatch.setenv("TM_DB_TYPE", "postgresql")

        with pytest.raises(ValueError, match="非法数据库类型"):
            DatabaseSession._build_db_url()

    def test_health_check_true_when_connected(self, temp_db: Path) -> None:
        """
        health_check 连通态: 真实临时库上执行 SELECT 1 成功返回 True，
        且不向上抛任何异常。
        """
        assert DatabaseSession.health_check() is True, (
            "临时 SQLite 库可连通，health_check 应返回 True"
        )

    def test_health_check_false_on_invalid_config(
        self, monkeypatch: pytest.MonkeyPatch, temp_db: Path
    ) -> None:
        """
        health_check 配置非法降级: 引擎构建抛 ValueError 时被内部
        吞掉返回 False，调用方拿得到布尔值而不是异常。
        """
        monkeypatch.setenv("TM_DB_TYPE", "not-a-database")
        DatabaseSession.reset()

        assert DatabaseSession.health_check() is False, (
            "配置非法应降级为 False 而不是向上抛异常"
        )

    def test_health_check_false_on_connection_error(
        self, monkeypatch: pytest.MonkeyPatch, temp_db: Path
    ) -> None:
        """
        health_check 数据库不可达降级: 引擎抛 OperationalError（如
        MySQL 服务未启动）时同样返回 False，异常不得逃逸。
        """
        from sqlalchemy.exc import OperationalError

        # get_engine 是 classmethod，经类访问取到的是未绑定的普通函数，
        # 因此替身签名不收 cls
        def _raise_operational_error() -> Any:
            """模拟 MySQL 不可达：引擎构建阶段抛 OperationalError"""
            raise OperationalError("SELECT 1", {}, Exception("MySQL 拒绝连接"))

        monkeypatch.setattr(DatabaseSession, "get_engine", _raise_operational_error)

        assert DatabaseSession.health_check() is False, (
            "数据库不可达应降级为 False 而不是向上抛异常"
        )


# ===========================================================================
# B组: http_client.py
# ===========================================================================
@allure.feature("阶段C覆盖率补齐")
@allure.story("HTTP客户端封装")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestHttpClientCoverage:
    """入参校验 / 快捷方法透传 / 异常包装 / 脱敏与截断 / 上下文管理"""

    @pytest.mark.parametrize(
        "bad_url",
        ["", "   ", None, "ftp://api.test", "api.test", "://api.test"],
        ids=["empty", "blank", "none", "wrong-scheme", "no-scheme", "bad-scheme"],
    )
    def test_invalid_base_url_rejected(self, bad_url: Any) -> None:
        """
        base_url 非法拒绝: 空串/纯空白/None/非 http(s) 协议一律抛
        ValueError。带协议头校验是为了防止把凭据拼进 ftp:// 之类的
        非常规 URL 后被静默接受。
        """
        with pytest.raises(ValueError, match="非法的base_url"):
            HttpClient(base_url=bad_url)

    def test_shortcut_methods_delegate_to_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        put/delete/patch 三个快捷方法透传到统一入口 request()，
        方法名大写、json/data 载荷原样传递，不丢参不篡改。
        """
        client = HttpClient(base_url=DUMMY_BASE_URL)
        recorded: list[tuple] = []
        sentinel = object()

        def _fake_request(method: str, path: str, **kwargs) -> Any:
            """记录调用参数并返回哨兵对象（不触网）"""
            recorded.append((method, path, kwargs))
            return sentinel

        monkeypatch.setattr(client, "request", _fake_request)

        assert client.put("/u/1", json={"a": 1}) is sentinel
        assert client.delete("/u/1") is sentinel
        assert client.patch("/u/1", data={"b": 2}) is sentinel

        # put/patch 的未使用载荷会以 None 一并透传（保持签名对称），
        # 断言的是"参数完整不丢"，不是"字典里不该有 None"
        assert recorded[0] == ("PUT", "/u/1", {"json": {"a": 1}, "data": None})
        assert recorded[1] == ("DELETE", "/u/1", {})
        assert recorded[2] == ("PATCH", "/u/1", {"json": None, "data": {"b": 2}})

    @pytest.mark.parametrize(
        "bad_method", ["", "   ", None], ids=["empty", "blank", "none"]
    )
    def test_empty_method_rejected(
        self, monkeypatch: pytest.MonkeyPatch, bad_method: Any
    ) -> None:
        """
        空 method 拒绝: 传空/空白 method 必须在发请求前抛 ValueError。
        空方法名会一路传到 requests 层报出难懂的底层错误，
        提前拦截才能让调用方一眼看出是自己漏传了参数。
        """
        client = HttpClient(base_url=DUMMY_BASE_URL)
        monkeypatch.setattr(
            client.session,
            "request",
            lambda *a, **kw: pytest.fail("非法 method 不应触达网络层"),
        )

        with pytest.raises(ValueError, match="非法的HTTP方法"):
            client.request(bad_method, "/x")

    def test_timeout_wrapped_with_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        超时包装: requests.exceptions.Timeout 转为 HttpClientError，
        异常消息带上方法与超时秒数（排障时最需要的两个信息），
        request_info 标记 type=timeout 供上层分流。
        """
        client = HttpClient(base_url=DUMMY_BASE_URL, timeout=7)

        def _raise_timeout(*args, **kwargs) -> None:
            """模拟读取超时"""
            raise requests.exceptions.Timeout("Read timed out")

        monkeypatch.setattr(client.session, "request", _raise_timeout)

        with pytest.raises(HttpClientError) as excinfo:
            client.get("/slow")

        assert "请求超时" in str(excinfo.value)
        assert "GET" in str(excinfo.value)
        assert "7" in str(excinfo.value), "超时秒数应写进异常消息便于排障"
        assert excinfo.value.request_info["type"] == "timeout"
        assert excinfo.value.request_info["method"] == "GET"

    def test_generic_request_exception_wrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        其余 RequestException 包装: 既非 Timeout 也非 ConnectionError
        的异常（如重定向过多）走通用分支，消息含异常类型名，
        request_info 标记 type=request_error。
        """
        client = HttpClient(base_url=DUMMY_BASE_URL)

        def _raise_too_many_redirects(*args, **kwargs) -> None:
            """模拟重定向死循环"""
            raise requests.exceptions.TooManyRedirects("Exceeded 30 redirects")

        monkeypatch.setattr(client.session, "request", _raise_too_many_redirects)

        with pytest.raises(HttpClientError) as excinfo:
            client.post("/redirect")

        assert "请求异常" in str(excinfo.value)
        assert "TooManyRedirects" in str(excinfo.value), (
            "通用异常分支应保留异常类型名供定位"
        )
        assert excinfo.value.request_info["type"] == "request_error"

    def test_error_response_status_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        4xx/5xx 属业务判定不是网络异常: 网络层成功的错误状态码必须
        原样返回 Response 交给断言层判成败，不得在客户端层抛异常。
        """
        client = HttpClient(base_url=DUMMY_BASE_URL)
        response = requests.Response()
        response.status_code = 503
        monkeypatch.setattr(client.session, "request", lambda *a, **kw: response)

        result = client.get("/unavailable")

        assert result.status_code == 503, "错误状态码应由断言层处理，客户端不抛"

    def test_build_url_absolute_passthrough(self) -> None:
        """
        URL 拼接: 以 http(s) 开头的 path 视为完整地址原样使用，
        否则与 base_url 拼接（base_url 尾部斜杠不产生双斜杠）。
        """
        client = HttpClient(base_url="https://api.example.com/")

        assert client._build_url("https://other.test/v1/x") == (
            "https://other.test/v1/x"
        ), "绝对地址不得再与 base_url 拼接"
        assert client._build_url("  https://other.test/v1/x  ") == (
            "https://other.test/v1/x"
        ), "完整地址同样应先 strip 空白"
        assert client._build_url("/v1/users") == "https://api.example.com/v1/users"
        assert client._build_url("v1/users") == "https://api.example.com/v1/users"

    def test_mask_data_recurses_into_lists(self) -> None:
        """
        请求体脱敏递归: list 内的 dict 同样要打码，嵌套 list 继续
        递归。漏掉 list 分支时，批量接口的 [{"password": ...}] 载荷
        会把凭据原样打进日志。

        字段口径按 SENSITIVE_BODY_FIELDS 现状断言（password/secret/
        token/access_key）。注意该元组**不含** access_token —— 与
        SENSITIVE_QUERY_FIELDS 已收录 access_token 不一致，属阶段C
        发现的遗留口径缺口，已单独上报待定，本轮不改行为。
        """
        masked = HttpClient._mask_data(
            [
                {"password": "p1", "name": "张三"},
                [{"access_key": "k1", "token": "t1"}, "纯文本"],
                "裸字符串",
            ]
        )

        assert masked == [
            {"password": "***", "name": "张三"},
            [{"access_key": "***", "token": "***"}, "纯文本"],
            "裸字符串",
        ], "list 内的敏感字段必须逐层打码，非 dict/list 原样返回"

    def test_safe_body_handles_unreadable_response(self) -> None:
        """
        响应体读取兜底: 二进制/编码异常导致 response.text 抛错时
        返回占位描述，而不是让日志逻辑连带崩掉。
        """

        class _UnreadableResponse:
            """模拟非文本响应：访问 .text 抛解码异常"""

            @property
            def text(self) -> str:
                """触发 UnicodeDecodeError"""
                raise UnicodeDecodeError(
                    "utf-8", b"\xff\xfe", 0, 2, "invalid start byte"
                )

        result = HttpClient._safe_body(_UnreadableResponse())

        assert result.startswith("<响应体读取失败"), (
            f"读取失败应降级为占位文本，实际: {result}"
        )

    def test_context_manager_closes_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        上下文管理器: with 块退出时自动关闭底层 Session 释放连接池；
        块内抛异常也必须关闭且不吞异常。
        """
        client = HttpClient(base_url=DUMMY_BASE_URL)
        closed: list[bool] = []
        monkeypatch.setattr(
            client.session, "close", lambda: closed.append(True)
        )

        with client as entered:
            assert entered is client, "__enter__ 应返回客户端自身"
        assert closed == [True], "正常退出 with 必须关闭会话"

        closed.clear()
        with pytest.raises(RuntimeError, match="块内异常"):
            with client:
                raise RuntimeError("块内异常")
        assert closed == [True], "异常退出 with 同样必须关闭会话"
