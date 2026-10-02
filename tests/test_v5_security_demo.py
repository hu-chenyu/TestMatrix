"""
TestMatrix 大扫除 v5 · Commit A：安全类修复的回归测试

覆盖本 commit 修复的三条安全类问题
------------------------------------
A组 用例创建的完整性违例不回显 SQL（V3-P3-6）
    1.  非唯一约束的 IntegrityError 不得进异常 message
    2.  HTTP 500 响应体不含 INSERT 语句与绑定列值
    3.  完整异常仍进日志（诊断价值不丢）
B组 路径脱敏的 v5 修复回归（V3-P1-1 + V3-P2-3）
    4.  同消息两条 Windows 路径都脱敏（v3 能力回退的修复）
    5.  多空格目录名收敛（账户名不泄露）
    6.  Unix 多空格目录名收敛
    7.  放宽后散文断开能力不回退
    8.  引号/括号/换行处的终止边界

为什么 500 响应体必须不含 SQL
------------------------------------
`create_case` 的泛型 CaseManagerError 不会被路由翻译，原样上抛到全局
`Exception` 处理器；而该处理器在 TESTING 下执行 `return error(str(exc), 500)`。
而 SQLAlchemy 的 IntegrityError 文本含完整 INSERT 语句与全部绑定列值
（其中可能有创建人、描述等业务数据）。生产模式（TESTING=False）返回
"服务器内部错误"不受影响，但测试环境与任何误开 TESTING 的部署都会泄露。

测试铁律
------------------------------------
- 每条用例独立临时 SQLite 库，teardown 严格 reset
- 异常注入只在 flush 处（外部边界），不改动被测模块内部逻辑
- 无 time.sleep 固定等待，无 print
"""

from collections.abc import Iterator
from pathlib import Path

import allure
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from src.core.case_manager import CaseManager, CaseManagerError
from src.db.db_session import DatabaseSession
from src.web import create_app
from src.web.routes.cases import _sanitize_error_message

# 非唯一约束的 IntegrityError 文本形态（模拟 CHECK 违例 / 未来新增约束）
CHECK_VIOLATION_MSG = "CHECK constraint failed: chk_case_priority_format"


def _make_check_violation() -> IntegrityError:
    """构造一条非唯一约束的完整性违例（含完整 INSERT 语句）"""
    return IntegrityError(
        "INSERT INTO test_cases (case_id, name, module, priority, creator) "
        "VALUES (?, ?, ?, ?, ?)",
        {"case_id": "TM-V5-0001", "creator": "qa-bot"},
        Exception(CHECK_VIOLATION_MSG),
    )


@pytest.fixture
def api_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator:
    """
    创建接口测试客户端（临时 SQLite 空库，TESTING 模式）

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写工具
        tmp_path (Path): pytest 临时目录

    返回:
        Iterator: yield Flask 测试客户端，前后重置引擎单例
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "v5_security.db"))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield create_app("test").test_client()
    DatabaseSession.reset()


# ===========================================================================
# A组: 500 响应体不回显 SQL
# ===========================================================================
@allure.feature("大扫除v5安全修复")
@allure.story("完整性违例不回显 SQL")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestIntegrityErrorNoSqlEcho:
    """非唯一约束的 IntegrityError 不得带出 SQL 与列值"""

    def test_exception_message_contains_no_sql(
        self, api_client: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        核心层异常 message 不含 INSERT 语句与列名/列值

        修复前 message 为 f"...: {exc}"，而 {exc} 含完整 SQL。
        """
        def _raise_violation(_self: Session) -> None:
            """在 flush 处注入 CHECK 违例"""
            raise _make_check_violation()

        monkeypatch.setattr(Session, "flush", _raise_violation)

        with pytest.raises(CaseManagerError) as excinfo:
            CaseManager.create_case({
                "case_id": "TM-V5-0001",
                "name": "触发完整性违例",
                "module": "用户中心",
                "priority": "P1",
            })

        message = str(excinfo.value)
        assert "INSERT INTO" not in message, f"异常 message 回显了 SQL: {message}"
        assert "test_cases" not in message, f"异常 message 回显了表名列值: {message}"
        assert "qa-bot" not in message, f"异常 message 回显了绑定列值: {message}"
        assert "完整性违例" in message, "异常 message 仍应说明问题类型"

    def test_http_500_body_contains_no_sql(
        self, api_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        HTTP 500 响应体不含 SQL（端到端走全局异常处理器）

        这是泄露的实际出口：TESTING 下处理器 `return error(str(exc), 500)`
        会把异常 message 原样放进响应体。只测核心层不足以覆盖该路径。
        """
        def _raise_violation(_self: Session) -> None:
            """在 flush 处注入 CHECK 违例"""
            raise _make_check_violation()

        monkeypatch.setattr(Session, "flush", _raise_violation)

        response = api_client.post(
            "/api/cases/",
            json={
                "case_id": "TM-V5-0002",
                "name": "触发完整性违例",
                "module": "用户中心",
                "priority": "P1",
            },
        )
        body = response.get_data(as_text=True)

        assert response.status_code == 500, f"应走 500 兜底，实际 {response.status_code}"
        assert "INSERT INTO" not in body, f"500 响应体回显了 SQL: {body}"
        assert "qa-bot" not in body, f"500 响应体回显了绑定列值: {body}"

    def test_full_exception_still_logged(
        self, api_client: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        完整异常仍进日志（脱敏不得以丢失诊断价值为代价）

        修复把 SQL 从 message 挪到日志，运维仍需能查到完整语句。
        """
        from loguru import logger as loguru_logger

        captured: list[str] = []
        sink_id = loguru_logger.add(captured.append, level="ERROR")

        def _raise_violation(_self: Session) -> None:
            """在 flush 处注入 CHECK 违例"""
            raise _make_check_violation()

        monkeypatch.setattr(Session, "flush", _raise_violation)
        try:
            with pytest.raises(CaseManagerError):
                CaseManager.create_case({
                    "case_id": "TM-V5-0003",
                    "name": "触发完整性违例",
                    "module": "用户中心",
                    "priority": "P1",
                })
        finally:
            loguru_logger.remove(sink_id)

        joined = "\n".join(captured)
        assert "完整性违例" in joined, f"完整异常应进日志供排障: {joined!r}"
        assert "CHECK constraint failed" in joined, (
            f"日志应保留原始约束违例文案: {joined!r}"
        )


# ===========================================================================
# B组: 路径脱敏回归
# ===========================================================================
@allure.feature("大扫除v5安全修复")
@allure.story("路径脱敏 v5 修复回归")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestPathSanitizeV5:
    """两条 v3 缺陷的回归守卫"""

    def test_two_windows_paths_both_redacted(self) -> None:
        """
        同消息两条 Windows 路径都脱敏（V3-P1-1 能力回退的修复）

        v3 给 _PATH_CHAR 加了空格排除却漏改两个 name 类：basename 组
        吞下 `a.yaml in D:`（连同下一个盘符前缀），非重叠扫描从 `D:`
        之后继续，剩余 `\\y\\b.ini` 不再以 `[A-Za-z]:\\` 开头 → 零匹配。
        既有"多处路径"用例全用 POSIX，恰好绕开这个洞。
        """
        raw = "see C:" + "\\x\\a.yaml in D:" + "\\y\\b.ini"
        out = _sanitize_error_message(raw)

        assert "D:" not in out, f"第二个盘符前缀泄露: {out}"
        assert "a.yaml" in out and "b.ini" in out, f"两个文件名都应保留: {out}"

    def test_multi_space_windows_dir(self) -> None:
        """多空格 Windows 目录名收敛，账户名不泄露（V3-P2-3）"""
        raw = "open C:" + "\\Users" + "\\John  Smith" + "\\secret.yaml failed"
        out = _sanitize_error_message(raw)

        assert "John" not in out, f"多空格账户名泄露: {out}"
        assert "secret.yaml" in out

    def test_multi_space_posix_dir(self) -> None:
        """多空格 Unix 目录名收敛（V3-P2-3 的 POSIX 侧）"""
        raw = "open /home/bob/My  Docs/cases.yaml failed"
        out = _sanitize_error_message(raw)

        assert "My  Docs" not in out, f"多空格目录泄露: {out}"
        assert "cases.yaml" in out

    def test_prose_break_still_works(self) -> None:
        """
        放宽为 {1,} 后散文断开能力不回退

        放宽的代价检查：若改成无限制的空格放行，`a.yaml then /tmp/...`
        会被整体匹配，第二条路径的目录结构又被吞回去。
        """
        raw = (
            "first /tmp/tm_case_import_1/a.yaml then "
            "/tmp/tm_case_import_2/b.yaml failed"
        )
        out = _sanitize_error_message(raw)

        assert "tm_case_import_1" not in out, f"第一条目录泄露: {out}"
        assert "tm_case_import_2" not in out, f"第二条目录泄露: {out}"
        assert "a.yaml" in out and "b.yaml" in out

    def test_terminators_still_bound_the_match(self) -> None:
        """
        引号/括号/换行仍是终止边界（防止路径吞掉诊断信息）

        冒号与换行是 v5 新增正则最容易回归的边界：字符类一旦写错，
        路径会吞掉后面的行列号诊断信息。
        """
        quoted = '  in "C:' + "\\Users\\John\\t\\a.yaml" + '", line 1, column 3'
        out = _sanitize_error_message(quoted)
        assert "a.yaml" in out and "line 1, column 3" in out, (
            f"引号内应终止且保留行列号: {out}"
        )
        assert "John" not in out

        newline = "bad C:" + "\\Users\\ci\\a.yaml\n  line 2, column 9"
        out_nl = _sanitize_error_message(newline)
        assert "a.yaml" in out_nl and "line 2, column 9" in out_nl, (
            f"换行处应终止且保留下一行诊断信息: {out_nl!r}"
        )
