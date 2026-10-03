"""
TestMatrix 大扫除 v5 · Commit C：健壮性修复回归套件

修复来源
--------
本文件覆盖 v4 双 AI 交叉审查（MiniMax 20 条 + Hy4-Preview 8 条）中
归属 Commit C 的 4 项修复，每项都对应一处**可复现的行为变化**，
而非注释或风格调整：

    1. _bracket_ipv6_host 按冒号数量区分 IPv6 与 host:port 误配
       （src/db/db_session.py，Hy4-P2）
    2. get_config 在 TM_ENV 缺省时告警（src/web/config.py，补
       V2-P2-6 残留缺口中最响亮的一环）
    3. SSE 终态事件豁免断点过滤，重连不再零帧（src/web/routes/
       executions.py，V2-P3-5）
    4. cases.js 收敛为复用 window.escapeHtml（前端，V2-P3-4）

为什么单独建文件而不是并入既有套件
----------------------------------
上述 1/2/4 三项此前没有任何测试覆盖（v3 引入时只改了源码），一旦
回归不会有任何用例变红。本文件按"每条修复至少一条能变红的断言 +
阳性对照"的标准补齐。

测试口径
--------
- 第 3 项（SSE）走真实 HTTP 流：断言"连续两次重连各自都返回 1 帧"，
  而不只是"非空"。缺陷的实质是浏览器每 3s 自动重连形成**热循环**，
  单次请求非空并不能证明循环会终止。
- 第 4 项无 JS 测试运行器（环境亦无 node），采用**源码形态断言**：
  锁定"必须存在什么"（单一实现来源、加载序、幂等导出），不绑定
  缩进与排版。这类断言能挡住"有人把本地实现加回来"。
- 无 time.sleep 固定等待；SSE 终态分支是有限帧，读完自动关闭。
- 事件通道注册表为模块级全局单例，用 autouse fixture 前后复位。
"""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import allure
import pytest
from flask.testing import FlaskClient
from loguru import logger as loguru_logger
from src.core import event_bus
from src.core.event_bus import ExecutionEvent, get_channel
from src.db.db_session import DatabaseSession, _bracket_ipv6_host
from src.web import create_app
from src.web.config import get_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
JS_DIR = PROJECT_ROOT / "src" / "web" / "static" / "js"
TEMPLATE_DIR = PROJECT_ROOT / "src" / "web" / "templates"


# ===========================================================================
# 公共 fixture
# ===========================================================================
@pytest.fixture(autouse=True)
def _clean_event_registry() -> Iterator[None]:
    """
    事件通道注册表清洁 fixture（autouse，全文件生效）

    通道注册表是模块级全局单例，不复位时上一条用例残留的通道会串进
    下一条的断言，制造 flaky

    参数:
        无

    返回:
        Iterator[None]: yield None
    """
    event_bus.reset_channels()
    yield
    event_bus.reset_channels()


@pytest.fixture
def exec_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Path]:
    """
    独立临时 SQLite 库（function 级）

    teardown 顺序: reset 引擎（释放文件锁）-> 删除库文件

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest 临时目录

    返回:
        Iterator[Path]: yield 临时库文件路径
    """
    db_path = tmp_path / "v5_robustness.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_path
    DatabaseSession.reset()
    db_path.unlink(missing_ok=True)


@pytest.fixture
def client(exec_db: Path) -> FlaskClient:
    """
    Flask 测试客户端（基于 exec_db 临时库）

    参数:
        exec_db (Path): 临时库文件路径（保证建表已完成）

    返回:
        FlaskClient: Flask 测试客户端
    """
    return create_app("test").test_client()


# ===========================================================================
# A 组: _bracket_ipv6_host 按冒号数量区分
# ===========================================================================
@allure.feature("大扫除v5健壮性修复")
@allure.story("MySQL host 误配诊断")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestBracketIpv6Host:
    """host:port 误配必须响亮地失败，而不是被静默当成 IPv6"""

    def test_host_port_misconfig_is_rejected(self) -> None:
        """
        单冒号的 host:port 误配必须抛 ValueError

        回归点: v3 的判定是"含冒号且不以 [ 开头即 IPv6"，会把
        `127.0.0.1:3307` 补成 `[127.0.0.1:3307]`。这个形态对
        SQLAlchemy 是合法的，于是把一条**响亮的配置错误**（原本解析
        即 ValueError）变成静默连不上，排查成本从"读报错"升级为
        "抓包看连接在哪一层断掉"。端口另有 TM_DB_MYSQL_PORT 配置项。
        """
        with pytest.raises(ValueError) as excinfo:
            _bracket_ipv6_host("127.0.0.1:3307")

        message = str(excinfo.value)
        assert "TM_DB_MYSQL_PORT" in message, (
            f"报错须直接指明正确配置项，否则用户仍不知该改哪里: {message!r}"
        )
        assert "TM_DB_MYSQL_HOST" in message, (
            f"报错须点明出错的配置项: {message!r}"
        )

    def test_ipv6_literal_is_bracketed(self) -> None:
        """
        多冒号的 IPv6 字面量必须补方括号（阳性对照）

        区分两种形态靠的是冒号数量，故必须同时钉住"多冒号仍走补
        方括号"这一侧——否则一个把判定写成"单冒号即报错"的过度
        修复也能让上一条变绿。
        """
        assert _bracket_ipv6_host("::1") == "[::1]"
        assert _bracket_ipv6_host("fe80::1") == "[fe80::1]"

    def test_plain_host_passes_through(self) -> None:
        """不含冒号的主机名/IP 原样返回（阳性对照）"""
        assert _bracket_ipv6_host("127.0.0.1") == "127.0.0.1"
        assert _bracket_ipv6_host("db.internal") == "db.internal"

    def test_already_bracketed_ipv6_is_unchanged(self) -> None:
        """
        已带方括号的 IPv6 不得重复包裹（阳性对照）

        修复把判断拆成"先看方括号、再数冒号"两段，若顺序写反或
        方括号分支漏掉，`[::1]` 会变成 `[[::1]]`。
        """
        assert _bracket_ipv6_host("[::1]") == "[::1]"
        assert _bracket_ipv6_host("[fe80::1]") == "[fe80::1]"


# ===========================================================================
# B 组: TM_ENV 缺省告警
# ===========================================================================
@allure.feature("大扫除v5健壮性修复")
@allure.story("配置缺省告警")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestTmEnvDefaultWarning:
    """生产漏配 TM_ENV 是最常见事故路径，必须让它响亮"""

    def test_missing_tm_env_logs_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        TM_ENV 未配置时必须记 warning 并说明后果

        背景: v3 修 SECRET_KEY 时留下的残留缺口是——get_config 对
        缺失 TM_ENV 缺省为 "dev"，于是拿到 DevelopmentConfig，
        fail-fast 判定不触发，退回随机密钥，且 DEBUG 一并开启。
        本轮不引入第二个生产判据（那是新增配置契约），改为让这条
        路径**响亮**：至少运维在启动日志里看得见。

        断言覆盖三个要素：告警存在、点名了 TM_ENV、说清了后果
        （DEBUG 开启 / SECRET_KEY 随机），避免"只记了句空话"。
        """
        monkeypatch.delenv("TM_ENV", raising=False)

        captured: list[str] = []
        sink_id = loguru_logger.add(captured.append, level="WARNING")
        try:
            config_class = get_config()
        finally:
            loguru_logger.remove(sink_id)

        assert config_class.__name__ == "DevelopmentConfig", (
            f"TM_ENV 缺省仍应回落到 dev（保留本地零配置体验），"
            f"实际: {config_class.__name__}"
        )

        joined = "\n".join(captured)
        assert "TM_ENV" in joined, (
            f"告警须点名 TM_ENV 这个变量，实际日志: {joined!r}"
        )
        assert "DEBUG" in joined, (
            f"告警须说明 DEBUG 会被开启这一后果，实际日志: {joined!r}"
        )
        assert "TM_SECRET_KEY" in joined, (
            f"告警须给出后续动作（配置 SECRET_KEY），实际日志: {joined!r}"
        )

    def test_explicit_tm_env_does_not_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        TM_ENV 显式配置时不得告警（阳性对照）

        防止把告警写成无条件触发——那会让每次本地启动都刷一条
        WARNING，久了必然被无视，告警也就失去意义。
        """
        monkeypatch.setenv("TM_ENV", "test")

        captured: list[str] = []
        sink_id = loguru_logger.add(captured.append, level="WARNING")
        try:
            config_class = get_config()
        finally:
            loguru_logger.remove(sink_id)

        assert config_class.__name__ == "TestingConfig", (
            f"TM_ENV=test 应命中 TestingConfig，实际: {config_class.__name__}"
        )
        assert "TM_ENV" not in "\n".join(captured), (
            f"TM_ENV 已显式配置时不应再告警，实际日志: {captured!r}"
        )

    def test_explicit_env_name_argument_skips_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        显式传 env_name 的调用路径不受影响（阳性对照）

        get_config("test") 不读环境变量，告警只针对"从环境变量取值"
        这条路径。加这条防止修复被写成"函数入口无条件告警"。
        """
        monkeypatch.delenv("TM_ENV", raising=False)

        captured: list[str] = []
        sink_id = loguru_logger.add(captured.append, level="WARNING")
        try:
            config_class = get_config("test")
        finally:
            loguru_logger.remove(sink_id)

        assert config_class.__name__ == "TestingConfig"
        assert "TM_ENV" not in "\n".join(captured), (
            f"显式传参路径不应告警，实际日志: {captured!r}"
        )


# ===========================================================================
# C 组: SSE 终态事件豁免断点过滤
# ===========================================================================
def _finished_status() -> dict[str, Any]:
    """
    构造 finished 批次状态字典（内部方法）

    字段集严格对齐 _terminal_snapshot_frame 的 finished 分支读取的
    六个键：少一个键都会在渲染时抛 KeyError，把真正要测的分支掩盖掉。

    参数:
        无

    返回:
        dict[str, Any]: finished 状态字典
    """
    return {
        "status": "finished",
        "total_cases": 2,
        "passed": 2,
        "failed": 0,
        "error": 0,
        "skipped": 0,
        "pass_rate": 1.0,
    }


def _read_stream(response: Any) -> str:
    """
    读取 SSE 流全部文本（内部方法）

    终态分支的流是有限帧，读完自动关闭，不需要帧数上限保护。

    参数:
        response (Any): 流式响应

    返回:
        str: 完整流文本
    """
    return response.get_data(as_text=True)


def _frame_ids(raw: str) -> list[str]:
    """
    提取 SSE 流中的 id 行序列（内部方法）

    参数:
        raw (str): 完整流文本

    返回:
        list[str]: 按出现顺序排列的 id 值列表
    """
    return re.findall(r"^id:\s*(\S+)\s*$", raw, re.MULTILINE)


@allure.feature("大扫除v5健壮性修复")
@allure.story("SSE 重连零帧热循环")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestSseTerminalReplay:
    """终态帧豁免断点过滤，保证重连循环自然终止"""

    def test_repeated_reconnects_each_return_a_frame(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        连续两次重连都必须各返回 1 帧（缺陷是热循环，不是单次空响应）

        回归点: 修复前终态事件与普通事件一样被断点过滤，客户端带
        Last-Event-ID 重连时服务端**一帧不发**即断流；浏览器按 SSE
        规范约 3s 自动重连并携带同一游标再来一轮，无限循环，且客户
        端永远收不到"批次已结束"的确认。

        为什么连做两次: 缺陷的实质是循环。只断言"单次非空"挡不住
        "第一次补帧、第二次又空"这种半吊子实现；两轮都稳定返回同
        一帧才证明循环会终止。终态帧幂等，重复送达不改变客户端状态。
        """
        from src.core.case_manager import CaseManager

        execution_id = "RUN-V5-SSE-REPLAY"
        channel = get_channel(execution_id, create=True)
        assert channel is not None
        channel.publish(ExecutionEvent(
            event_type="batch_start", data={"total_cases": 2}
        ))
        channel.publish(ExecutionEvent(
            event_type="batch_finished", data={"passed": 2, "failed": 0}
        ))
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(lambda _eid: _finished_status()),
        )

        for attempt in (1, 2):
            raw = _read_stream(
                client.get(
                    f"/api/executions/{execution_id}/events",
                    headers={"Last-Event-ID": "live:2"},
                )
            )
            assert _frame_ids(raw) == ["live:2"], (
                f"第 {attempt} 次重连必须仍返回 1 帧（零帧即热循环），"
                f"实际: {_frame_ids(raw)}"
            )
            assert raw.count("event: batch_finished") == 1, (
                f"第 {attempt} 次重连不得叠加降级快照帧，实际流: {raw[:200]!r}"
            )

    def test_non_terminal_events_still_filtered(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        豁免范围仅限终态帧，普通事件仍按断点过滤

        防止"关掉断点过滤"这种能同样让上一条变绿的错误修法：那样
        每次重连都会把整段积压事件重发一遍，正是 v2 修掉的问题。
        """
        from src.core.case_manager import CaseManager

        execution_id = "RUN-V5-SSE-SCOPE"
        channel = get_channel(execution_id, create=True)
        assert channel is not None
        channel.publish(ExecutionEvent(
            event_type="batch_start", data={"total_cases": 2}
        ))
        channel.publish(ExecutionEvent(
            event_type="batch_finished", data={"passed": 2, "failed": 0}
        ))
        monkeypatch.setattr(
            CaseManager,
            "get_execution_status",
            staticmethod(lambda _eid: _finished_status()),
        )

        raw = _read_stream(
            client.get(
                f"/api/executions/{execution_id}/events",
                headers={"Last-Event-ID": "live:2"},
            )
        )

        assert "event: batch_start" not in raw, (
            f"非终态事件必须照常被断点过滤（豁免仅限终态帧），实际流: {raw[:200]!r}"
        )


# ===========================================================================
# D 组: cases.js 收敛为复用 window.escapeHtml
# ===========================================================================
@allure.feature("大扫除v5健壮性修复")
@allure.story("escapeHtml 单一实现来源")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestEscapeHtmlConvergence:
    """两份实现是同一安全基元的两个事实来源，必须收敛为一处"""

    def test_cases_js_has_no_local_implementation(self) -> None:
        """
        cases.js 不得再自带 escapeHtml 实现

        回归点: 原先本文件有一份与 main.js:47 等价的实现。两份定义
        构成两个事实来源，将来只改一处即产生分叉；更隐蔽的是本文件
        的顶层函数声明会**覆盖** main.js 挂到 window 上的那份（加载
        序 main.js 先于 cases.js），使"改 main.js"实际不生效。
        """
        cases_js = (JS_DIR / "cases.js").read_text(encoding="utf-8")

        assert re.search(r"function\s+escapeHtml\s*\(", cases_js) is None, (
            "cases.js 不得再声明本地 escapeHtml 实现，应复用 window.escapeHtml"
        )

    def test_cases_js_binds_to_window_implementation(self) -> None:
        """
        cases.js 必须显式绑定 window.escapeHtml，且**不得同名声明**（正向锁定修复形态）

        回归点（v5 遗留 bug）: 原断言只要求出现
        `const escapeHtml = window.escapeHtml;`。但 main.js 的 escapeHtml
        是顶层 function 声明，在浏览器里绑定成**不可配置**的全局对象
        属性；cases.js 再用同名 const 声明，按
        GlobalDeclarationInstantiation 规则直接抛 SyntaxError，
        **整个 cases.js 不实例化**——用例页完全失效（列表永远"加载中"）。
        该缺陷此前被 v5 自身的"无本地实现"断言漏过（它只查 function
        形式，查不出 const 形式）。

        现在正向锁定两件事：①绑定存在且本地名是 escHtml；
        ②顶层不再有 escapeHtml 的词法声明（SyntaxError 触发条件）。
        """
        cases_js = (JS_DIR / "cases.js").read_text(encoding="utf-8")

        assert "const escHtml = window.escapeHtml;" in cases_js, (
            "cases.js 应以 const escHtml = window.escapeHtml 复用唯一实现"
        )
        # 阴性对照：同名词法声明会与 main.js 的全局 function 冲突，
        # 导致整脚本 SyntaxError。必须钉住这个名字不再出现。
        assert re.search(
            r"^(const|let|class|var|function)\s+escapeHtml\b", cases_js, re.M
        ) is None, (
            "cases.js 顶层不得再声明 escapeHtml（与 main.js 全局 function "
            "同名会触发 SyntaxError，整个脚本不执行）"
        )

    def test_main_js_remains_the_single_source(self) -> None:
        """
        main.js 仍须定义并导出该实现（阳性对照）

        收敛的前提是 main.js 那边是完整的：它必须真的挂到 window，
        且覆盖全部五个 HTML 元字符。否则 cases.js 绑过去的是一个
        残缺实现，XSS 防护比收敛前更弱。
        """
        main_js = (JS_DIR / "main.js").read_text(encoding="utf-8")

        assert "window.escapeHtml = escapeHtml;" in main_js, (
            "escapeHtml 必须由 main.js 挂到 window（cases.js 现在依赖它）"
        )
        assert "[&<>\"']/g" in main_js, (
            "main.js 的转义字符类必须覆盖 & < > \" ' 五个元字符"
        )

    def test_main_js_loads_before_cases_js(self) -> None:
        """
        加载序必须保证 main.js 先于 cases.js 执行

        `const escapeHtml = window.escapeHtml` 是**加载期求值**：若
        cases.js 先跑，绑定到 undefined，此后所有 escapeHtml(...) 调用
        都会抛 TypeError——而这依赖 base.html 的脚本排布，属于易被
        无意改动的隐式契约，必须显式钉住。
        """
        base_html = (TEMPLATE_DIR / "base.html").read_text(encoding="utf-8")
        cases_html = (TEMPLATE_DIR / "pages" / "cases.html").read_text(
            encoding="utf-8"
        )

        main_in_base = base_html.find("js/main.js")
        assert main_in_base != -1, "base.html 应加载 main.js"
        assert "extra_js" in base_html, "base.html 应提供 extra_js 扩展点"
        assert "js/cases.js" in cases_html, "cases.html 应在 extra_js 加载 cases.js"
        assert cases_html.find("js/cases.js") > cases_html.find("extra_js"), (
            "cases.js 必须挂在 base.html 的 extra_js 块里，才能排在 main.js 之后"
        )

    def test_cases_js_keeps_idempotent_export(self) -> None:
        """
        幂等导出必须保留（回归防护，避免顺手"清理"掉公开面）

        收敛后 escapeHtml 仍挂在 window.casesPage 上，但此时它已指向
        window.escapeHtml，该赋值是幂等的。删掉它不改变本模块行为，
        却会静默改变 window.casesPage 的公开面。

        键名口径（v5 遗留修复）: 本地常量改名 escHtml 后，导出的**键**
        仍是 escapeHtml（6.33 决策——收敛的是实现唯一性，不是模块 API
        面），只有值改指 escHtml。键名一并改掉会破坏既有控制台联调代码。
        """
        cases_js = (JS_DIR / "cases.js").read_text(encoding="utf-8")

        assert re.search(r"escapeHtml:\s*escHtml\s*,", cases_js) is not None, (
            "window.casesPage 仍应导出 escapeHtml 键（值为幂等的 escHtml 别名）"
        )
        # 公开面防护：键名不得被顺手改掉（值可以变，键不能变）
        assert re.search(r"\bescapeHtml\s*:", cases_js) is not None, (
            "window.casesPage 的 escapeHtml 导出键名必须保留"
        )
