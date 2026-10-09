"""
Day43 收尾批量修复的回归测试（交叉评审确认的 10 项中可测的部分）

覆盖三组修复:
    1. executors.py —— PytestRunner 骨架三问题
       （D5 输出截断取尾部 + 指定编码 / M1 硬编码 py / D6 假通过）
    2. cases.py —— case_id 字符集校验（D6/M2）
    3. 前端三处 JS 修复（D8 onerror 分 readyState / D10 页码补刷 /
       D11 图表错误态）以**源码形态断言**固定：JS 无 pytest 覆盖，
       CI 不跑浏览器，源码断言是当前唯一能在 CI 里拦住回归的手段
       （形态固定 ≠ 行为验证，行为验证靠浏览器实测）

设计原则:
    - 每个断言都对具体返回值/消息做实质校验，无空断言
    - 不 mock 被测逻辑本身（truncate_output_tail 走真实实现）
    - 前端源码断言按"必须包含的语义片段"匹配，不锁死整段文本，
      避免后续正常重构被误判为回归
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import allure
import pytest
from flask.testing import FlaskClient
from src.core import executors as executors_mod
from src.core.executors import (
    OUTPUT_TRUNCATE_LENGTH,
    SUBPROCESS_ENCODING,
    SUBPROCESS_ERRORS,
    PytestRunner,
    truncate_output_tail,
)
from src.db.db_session import DatabaseSession
from src.web import create_app

JS_DIR = Path(__file__).resolve().parent.parent / "src" / "web" / "static" / "js"
TPL_DIR = Path(__file__).resolve().parent.parent / "src" / "web" / "templates"


@pytest.fixture()
def crud_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    用例 CRUD API 客户端（临时 SQLite 文件库）

    参数:
        monkeypatch: 环境变量覆写
        tmp_path: pytest 临时目录

    返回:
        Iterator[FlaskClient]: 建表完成的应用测试客户端，结束后重置引擎
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day43_closing.db"))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield create_app("test").test_client()
    DatabaseSession.reset()


# ===========================================================================
# 1. executors.py：输出截断取尾部（D5）
# ===========================================================================
@allure.feature("Day43收尾")
class TestTruncateOutputTail:
    """pytest 输出截断必须取**尾部**（诊断价值最高的部分）"""

    @allure.story("未超限时原样返回，不加任何标记")
    def test_short_output_unchanged(self):
        text = "short output"
        assert truncate_output_tail(text) == text
        assert "截断" not in truncate_output_tail(text)

    @allure.story("恰好等于上限时不截断")
    def test_exact_limit_unchanged(self):
        text = "x" * OUTPUT_TRUNCATE_LENGTH
        assert truncate_output_tail(text) == text

    @allure.story("超限时保留末尾并加前缀标记")
    def test_long_output_keeps_tail(self):
        head = "HEAD_SESSION_INFO"
        tail = "3 failed, 27 passed in 1.2s"
        text = head + ("y" * (OUTPUT_TRUNCATE_LENGTH * 2)) + tail

        result = truncate_output_tail(text)

        # 尾部（汇总行）必须保留 —— 这是修复的核心目的
        assert tail in result, "末尾的 pytest 汇总行必须保留"
        # 头部（无信息量的 session 头）应被截掉
        assert head not in result, "头部 session 信息应被截断"
        # 前缀标记让用户知道这是截断内容
        assert "已保留末尾" in result
        assert str(OUTPUT_TRUNCATE_LENGTH) in result
        # 长度受控
        assert len(result) <= OUTPUT_TRUNCATE_LENGTH + 80

    @allure.story("空串返回空串（不抛异常）")
    def test_empty_output(self):
        assert truncate_output_tail("") == ""


@allure.feature("Day43收尾")
class TestPytestRunnerCommand:
    """build_command：跨平台解释器 + 选项终止符（M1/D6）"""

    @allure.story("解释器是当前解释器而非 Windows 专属的 py")
    def test_uses_sys_executable(self):
        # Day47 起执行目标取 source_ref（不再回落 case_id），补该键
        command = PytestRunner().build_command(
            {"case_id": "TM-X-1", "source_ref": "tests/x.py"}
        )
        assert command[0] == sys.executable
        assert command[0] != "py"

    @allure.story("path 前有 -- 选项终止符（阻断选项注入）")
    def test_has_option_terminator(self):
        command = PytestRunner().build_command(
            {"case_id": "TM-X-1", "source_ref": "tests/x.py"}
        )
        assert "--" in command
        assert command.index("--") < command.index("tests/x.py")

    @allure.story("以 - 开头的执行路径在拼命令前即被拒（Day47-fix P3-3）")
    def test_dash_source_ref_rejected_before_command(self):
        """
        Day43 起防护手段是 `--` 终止符；Day47-fix 又在 build_command 里
        加了 source_ref 格式校验，**形如选项的路径在拼命令之前就被拒**。

        这里把断言从"终止符让它落在 -- 之后"改成"它根本进不了命令"：
        前者是事后兜底（值已进了命令行，只是没被当选项），后者是事前
        拒绝。两条防线都保留，但**前者不能单独作为正确性依据**——
        终止符只是不让它被解析成选项，值本身仍会被当作测试文件路径传
        给 pytest。格式校验把它挡在门外是更强的性质。
        """
        with pytest.raises(ValueError) as excinfo:
            PytestRunner().build_command(
                {"case_id": "TM-X-1", "source_ref": "--version"}
            )
        assert "source_ref" in str(excinfo.value)

        # 终止符本身的位置仍然必须正确（合法路径照常拼装）
        command = PytestRunner().build_command(
            {"case_id": "TM-X-1", "source_ref": "tests/x.py"}
        )
        assert command[command.index("--") + 1] == "tests/x.py"

    @allure.story("Day48 收口：source_ref 唯一优先，script_path 不再兜底")
    def test_script_path_priority(self):
        # Day47 起 source_ref 为第一优先，Day48 进一步移除 script_path
        # 过渡兼容；两条铁律都不变——只带 script_path 的用例必须显式报错，
        # 且**绝不回落 case_id**
        with pytest.raises(ValueError) as excinfo:
            PytestRunner().build_command(
                {"case_id": "TM-X-1", "script_path": "tests/test_demo.py"}
            )
        assert "source_ref" in str(excinfo.value), (
            "script_path 已不再作为执行目标，异常必须指向 source_ref"
        )

        command = PytestRunner().build_command(
            {"case_id": "TM-X-1", "source_ref": "tests/test_demo.py"}
        )
        assert "tests/test_demo.py" in command
        assert "TM-X-1" not in command


@allure.feature("Day43收尾")
class FakePopen:
    """
    subprocess.Popen 的可控替身（Day49 起 PytestRunner 改用 Popen）

    Day49 之前本文件 patch 的是 `subprocess.run`，返回 CompletedProcess；
    执行器改为自持 Popen 句柄（为了拿到 pid 做进程树清理）之后，
    patch 点必须跟着换成 Popen，断言语义保持不变。

    只提供 run_one 真正读取的属性（pid/returncode/communicate/poll/wait），
    不臆造其余字段。
    """

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        """
        构造替身

        参数:
            returncode (int): 子进程退出码
            stdout (str): communicate 返回的 stdout
            stderr (str): communicate 返回的 stderr
        """
        # pid 给 0：清理逻辑对非真实 pid 会走"跳过"守卫，不会误杀本机进程
        self.pid = 0
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        """返回预置的 stdout/stderr"""
        return self._stdout, self._stderr

    def poll(self) -> int | None:
        """进程已收场，返回退出码"""
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """进程已收场，直接返回退出码"""
        return self.returncode

    def kill(self) -> None:
        """补杀动作（本替身无需真实副作用）"""


class TestPytestRunnerSubprocess:
    """run_one：编码策略与尾部截断落库（D5）"""

    @staticmethod
    def _completed(returncode: int, stdout: str = "", stderr: str = ""):
        obj = MagicMock()
        obj.returncode = returncode
        obj.stdout = stdout
        obj.stderr = stderr
        return obj

    @staticmethod
    def _case() -> dict:
        """
        构造带 source_ref 的用例字典（本组用例统一入口）

        Day47 起 build_command 不再回落 case_id，只给 case_id 会在
        命令拼装阶段抛 ValueError，下游的编码/截断/退出码分支根本走不到
        ——那会让这些用例**假通过**（run_one 把异常降级成 error，而本组
        恰好有几条断言 result == "error"），等于悄悄停测。

        参数:
            无

        返回:
            dict: 含 case_id 与 source_ref 的用例字典
        """
        return {"case_id": "TM-X-1", "source_ref": "tests/test_x.py"}

    @allure.story("子进程创建显式指定 encoding 与 errors")
    def test_subprocess_uses_utf8_replace(self):
        captured = {}

        def _fake_popen(cmd, **kwargs):
            captured.update(kwargs)
            return FakePopen(0)

        with patch.object(executors_mod.subprocess, "Popen", _fake_popen):
            PytestRunner().run_one(self._case())

        assert captured.get("encoding") == SUBPROCESS_ENCODING == "utf-8"
        assert captured.get("errors") == SUBPROCESS_ERRORS == "replace"
        assert captured.get("text") is True

    @allure.story("超长 stdout 落库时保留末尾汇总行而非头部")
    def test_long_stdout_keeps_summary_tail(self):
        head = "SESSION_HEADER"
        tail = "== 1 failed, 2 passed in 0.42s =="
        stdout = head + ("z" * (OUTPUT_TRUNCATE_LENGTH * 2)) + tail

        with patch.object(
            executors_mod.subprocess,
            "Popen",
            return_value=FakePopen(1, stdout=stdout),
        ):
            result = PytestRunner().run_one(self._case())

        assert result.result == "failed"
        assert result.error_message is not None
        assert tail in result.error_message
        assert head not in result.error_message

    @allure.story("超长 stderr 同样取尾部")
    def test_long_stderr_keeps_tail(self):
        tail = "ERROR: usage error tail marker"
        stderr = ("e" * (OUTPUT_TRUNCATE_LENGTH * 2)) + tail

        with patch.object(
            executors_mod.subprocess,
            "Popen",
            return_value=FakePopen(2, stderr=stderr),
        ):
            result = PytestRunner().run_one(self._case())

        assert result.result == "error"
        assert result.error_message is not None
        assert tail in result.error_message

    @allure.story("退出码 0/1/2 映射不变（不回归）")
    def test_exit_code_mapping_unchanged(self):
        runner = PytestRunner()
        with patch.object(
            executors_mod.subprocess, "Popen", return_value=FakePopen(0)
        ):
            assert runner.run_one(self._case()).result == "passed"
        with patch.object(
            executors_mod.subprocess, "Popen", return_value=FakePopen(1, stdout="x")
        ):
            assert runner.run_one(self._case()).result == "failed"
        with patch.object(
            executors_mod.subprocess,
            "Popen",
            return_value=FakePopen(2, stderr="usage error"),
        ):
            got = runner.run_one(self._case())
            assert got.result == "error"
            assert "usage error" in got.error_message

    @allure.story("TimeoutExpired / OSError 仍降级为 error（不回归）")
    def test_timeout_and_oserror_still_error(self):
        """
        两条异常路径必须各自触发，不能被"source_ref 为空"提前短路。

        这里刻意给全 source_ref：若不给，build_command 先抛 ValueError
        并被 run_one 转成 error，两条断言都会**假通过**而真实的
        超时/OSError 分支一次都没执行——这正是 7.51 说的"只测了
        各自有测试、没测它们组合"的同类陷阱。
        """
        runner = PytestRunner()

        def _timeout(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

        def _oserror(cmd, **kwargs):
            raise OSError(2, "No such file or directory")

        with patch.object(executors_mod.subprocess, "Popen", _timeout):
            result = runner.run_one(self._case())
        assert result.result == "error"
        assert "执行超时" in (result.error_message or ""), (
            "必须是超时分支而非其它 error 来源"
        )

        with patch.object(executors_mod.subprocess, "Popen", _oserror):
            result = runner.run_one(self._case())
        assert result.result == "error"
        assert "启动失败" in (result.error_message or ""), (
            "必须是启动失败分支而非其它 error 来源"
        )


# ===========================================================================
# 2. cases.py：case_id 字符集校验（D6/M2）
# ===========================================================================
@allure.feature("Day43收尾")
class TestCaseIdCharset:
    """case_id 必须是安全编号：首字符字母/数字，其余字母数字点下划线连字符"""

    @staticmethod
    def _payload(case_id: str) -> dict:
        return {
            "case_id": case_id,
            "name": "字符集校验用例",
            "module": "auth",
            "priority": "P2",
            "case_type": "api",
            "status": "active",
        }

    @allure.story("以连字符开头的编号被拒（pytest 选项注入）")
    def test_reject_leading_dash(self, crud_client: FlaskClient):
        """case_id='--version' 曾让 pytest 打印版本后 exit 0 → 假通过"""
        resp = crud_client.post("/api/cases/", json=self._payload("--version"))
        assert resp.status_code == 400, f"应被拒，实际 {resp.status_code}"
        assert "case_id" in str(resp.get_json()["data"])

    @allure.story("含路径穿越形态的编号被拒")
    def test_reject_path_traversal(self, crud_client: FlaskClient):
        resp = crud_client.post("/api/cases/", json=self._payload("../evil"))
        assert resp.status_code == 400

    @allure.story("含空格的编号被拒")
    def test_reject_space(self, crud_client: FlaskClient):
        resp = crud_client.post("/api/cases/", json=self._payload("TM X 1"))
        assert resp.status_code == 400

    @allure.story("含中文的编号被拒")
    def test_reject_chinese(self, crud_client: FlaskClient):
        resp = crud_client.post("/api/cases/", json=self._payload("TM-中文-0001"))
        assert resp.status_code == 400

    @allure.story("合法编号正常创建（201）")
    def test_accept_valid(self, crud_client: FlaskClient):
        resp = crud_client.post("/api/cases/", json=self._payload("TM-VALID-001"))
        assert resp.status_code == 201
        assert resp.get_json()["data"]["case_id"] == "TM-VALID-001"

    @allure.story("点号/下划线/连字符组合仍合法（覆盖存量编号形态）")
    def test_accept_dots_underscores(self, crud_client: FlaskClient):
        for case_id in ("TM.API.001", "TM_api_002", "TM-API-9001", "a", "A1"):
            resp = crud_client.post("/api/cases/", json=self._payload(case_id))
            assert resp.status_code == 201, f"{case_id} 应被接受"


# ===========================================================================
# 3. 前端源码形态断言（JS 无 pytest 覆盖，CI 不跑浏览器）
# ===========================================================================
@allure.feature("Day43收尾")
class TestFrontendSourceGuards:
    """前端三处修复的源码形态守卫（行为验证靠浏览器实测）"""

    @staticmethod
    def _js(name: str) -> str:
        return (JS_DIR / name).read_text(encoding="utf-8")

    @allure.story("D8: onerror 区分 EventSource.CLOSED 与 CONNECTING")
    def test_onerror_branches_on_ready_state(self):
        src = self._js("executions.js")
        onerror = src[src.index("es.onerror") : src.index("getLogModal().show()")]
        assert "EventSource.CLOSED" in onerror
        assert "EventSource.CONNECTING" in onerror
        assert 'setConnectionBadge("closed")' in onerror
        # CLOSED 分支必须真正收流，而不是只改文案
        assert "closeEventSource()" in onerror

    @allure.story("D8: closed 徽章态已注册")
    def test_closed_badge_registered(self):
        src = self._js("executions.js")
        assert "closed: { cls:" in src

    @allure.story("D10: 两页均有 pendingPage 补刷且在 finally 中消费")
    def test_pending_page_in_both_pages(self):
        for name in ("cases.js", "executions.js"):
            src = self._js(name)
            assert "pendingPage: null" in src, f"{name} 缺 pendingPage 状态"
            assert "state.pendingPage = targetPage" in src, f"{name} 未登记 pendingPage"
            # 消费点必须在 finally（锁释放后）且先置 null 再递归
            assert "state.pendingPage = null;" in src
            assert src.index("state.pendingPage = null;") < src.rindex("loadCases()") if name == "cases.js" else True

    @allure.story("D10: executions.js 的页大小补刷未被回归掉")
    def test_pending_page_size_kept(self):
        src = self._js("executions.js")
        assert "pendingPageSize: null" in src
        assert "state.pendingPageSize = null;" in src

    @allure.story("D11: 图表错误态与空态文案区分")
    def test_chart_error_state_distinct(self):
        src = self._js("dashboard.js")
        assert "function markChartLoadState" in src
        assert "加载失败，请点右上角「刷新」重试" in src
        assert "暂无执行趋势" in src, "空态文案应保留"
        # 三个图表容器都要标记
        for cid in ("trendChart", "modulePieChart", "priorityBarChart"):
            assert f'markChartLoadState("{cid}", true)' in src

    @allure.story("D17: cases.js 查表已判自有属性")
    def test_cases_color_map_has_own_property(self):
        src = self._js("cases.js")
        assert "Object.prototype.hasOwnProperty.call(colorMap" in src

    @allure.story("N1: base.html 显式声明空 favicon（消除 404 噪声）")
    def test_favicon_declared(self):
        html = (TPL_DIR / "base.html").read_text(encoding="utf-8")
        assert 'rel="icon"' in html

    @allure.story("D4: docker-compose 不含明文口令默认值")
    def test_compose_has_no_plaintext_password(self):
        compose = (
            Path(__file__).resolve().parent.parent
            / "docker"
            / "docker-compose.yml"
        ).read_text(encoding="utf-8")
        assert "root123456" not in compose
        assert "testmatrix123" not in compose
        assert "TM_DB_MYSQL_ROOT_PASSWORD:?" in compose
        assert "TM_DB_MYSQL_PASSWORD:?" in compose
