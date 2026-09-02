"""
db_session多线程兼容性验证用例（Day13前置健壮性修复）

验证背景:
    SQLite默认check_same_thread=True，连接只能在创建它的线程内使用；
    Web平台的请求线程/SSE推送线程/异步执行线程会并发访问同一引擎，
    若不关闭该限制会抛
    "sqlite3.ProgrammingError: SQLite objects created in a thread can
    only be used in that same thread"。
    DatabaseSession.get_engine对SQLite注入connect_args=
    {"check_same_thread": False}完成修复，本文件验证修复真实生效。

验证目标:
    1. 多个子线程内通过session_scope读写SQLite不抛跨线程异常
    2. 各线程写入数据完整落库（行数守恒）
    3. 子线程写入后主线程可读到全部数据（连接跨线程复用）
"""

import threading
from pathlib import Path

import allure
import pytest
from sqlalchemy import text

from src.db.db_session import DatabaseSession

# 项目根目录（本文件位于 tests/ 下，向上一级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def temp_thread_db(tmp_path, monkeypatch):
    """
    临时SQLite数据库fixture（每个测试独享干净库）

    测试前monkeypatch临时库路径并reset重建引擎、建表；
    测试后reset恢复，避免污染其他测试。

    参数:
        tmp_path (Path): pytest临时目录fixture
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture

    返回:
        Path: 临时数据库文件路径
    """
    db_file = tmp_path / "test_thread_safety.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    # 线程触达表（子线程并发写入，主线程最终核对行数）
    with DatabaseSession.session_scope() as session:
        session.execute(text("CREATE TABLE thread_touch (id INTEGER)"))
    yield db_file
    DatabaseSession.reset()


@allure.feature("数据库会话管理")
@allure.story("SQLite多线程兼容")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestSqliteThreadSafety:
    """SQLite引擎跨线程访问验证（Web多线程场景前置保障）"""

    def test_concurrent_threads_rw_no_cross_thread_error(self, temp_thread_db):
        """
        多线程并发读写: 4个子线程各自在子线程内通过session_scope
        独立获取连接并写入3行，全程不抛"created in a thread"跨线程异常，
        最终表内12行齐全（写入行数守恒）

        参数:
            temp_thread_db (Path): 临时SQLite库fixture

        返回:
            无
        """
        thread_count = 4
        writes_per_thread = 3
        errors: list = []

        def worker(worker_id: int) -> None:
            """子线程任务: 各自开3次独立session写入数据"""
            try:
                for seq in range(writes_per_thread):
                    with DatabaseSession.session_scope() as session:
                        session.execute(
                            text("INSERT INTO thread_touch VALUES (:value)"),
                            {"value": worker_id * 100 + seq},
                        )
            except Exception as exc:  # noqa: BLE001 子线程异常回传主线程统一断言
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=worker, args=(index,), name=f"db-worker-{index}")
            for index in range(thread_count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive(), "子线程执行超时未结束"

        # 子线程零异常（若check_same_thread未关闭，此处必现ProgrammingError）
        assert errors == [], f"子线程访问SQLite发生异常: {errors}"

        # 主线程另开session读取，验证跨线程写入全部落库
        with DatabaseSession.session_scope() as session:
            total = session.execute(
                text("SELECT COUNT(*) FROM thread_touch")
            ).scalar()
        assert total == thread_count * writes_per_thread
