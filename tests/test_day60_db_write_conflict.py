"""
写库冲突修复与 (execution_id, case_id) 幂等对账用例（Day60）

验证背景（PROJECT_CONTEXT 6.66 排查结论）:
  `test_executions` 原先只有普通复合索引 `idx_execution_result`，
  `(execution_id, case_id)` **没有唯一约束**；`record_execution` 的防重是
  "先 SELECT 再 INSERT"两步，两者之间存在 TOCTOU 窗口。同一批次被两个
  进程/worker 同时执行时，两个写入方都能通过 SELECT 检查 → 双写，
  且 MySQL 侧连 IntegrityError 都不会抛（无唯一键即无冲突）。
  `finish_execution` 按 execution_id 聚合**全部**明细，双写会让计数翻倍、
  通过率被污染，且污染在库表里不可逆。

本文件验证四件事:
  1. 约束真的落在库表上（schema 级 + 写入级双重证据）；
  2. `record_execution` 的去重语义在两条路径上一致（写前查重 / 写时撞约束）；
  3. 存量库迁移"先去重、后建键"是可重入的；
  4. SQLite 写锁等待上限（busy_timeout）可按环境变量配置，默认值不变。

隔离方式:
  每个用例独立临时 SQLite 文件库 + DatabaseSession.reset()，
  会话级 conftest 已把默认库钉在 tmp 目录，本文件不触碰真实开发库。
"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import allure
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from src.core.case_manager import CaseManager, CaseManagerError
from src.db import migration
from src.db.db_session import (
  SQLITE_BUSY_TIMEOUT_MS_DEFAULT,
  DatabaseSession,
  _apply_sqlite_pragmas,
  sqlite_busy_timeout_ms,
  sqlite_journal_mode,
)
from src.db.models import EXECUTION_UNIQUE_KEY_NAME
from src.db.models import TestExecution as ExecutionModel

# 用例内固定的时间基准（record_execution 只存不校验先后关系）
START_TIME = datetime(2026, 10, 20, 10, 0, 0)
END_TIME = START_TIME + timedelta(seconds=1)


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
  """
  临时 SQLite 文件库 fixture（每用例独享）

  参数:
    tmp_path (Path): pytest 临时目录
    monkeypatch (pytest.MonkeyPatch): 环境变量覆写工具

  返回:
    Path: 临时数据库文件路径
  """
  db_file = tmp_path / "day60_write_conflict.db"
  monkeypatch.setenv("TM_DB_TYPE", "sqlite")
  monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
  DatabaseSession.reset()
  DatabaseSession.init_db()
  yield db_file
  DatabaseSession.reset()


def _record(execution_id: str, case_id: str, result: str = "passed") -> bool:
  """
  调用 record_execution 的简写（统一时间参数，减少用例噪音）

  参数:
    execution_id (str): 执行批次号
    case_id (str): 业务用例编号
    result (str): 执行结果

  返回:
    bool: True=已落库；False=按 (execution_id, case_id) 去重跳过
  """
  return CaseManager.record_execution(
    execution_id=execution_id,
    case_id=case_id,
    case_name=f"用例{case_id}",
    result=result,
    start_time=START_TIME,
    end_time=END_TIME,
    duration=1.0,
  )


def _count_rows(execution_id: str, case_id: str) -> int:
  """
  统计某 (execution_id, case_id) 的明细行数

  参数:
    execution_id (str): 执行批次号
    case_id (str): 业务用例编号

  返回:
    int: 行数
  """
  with DatabaseSession.session_scope() as session:
    return (
      session.query(ExecutionModel)
      .filter_by(execution_id=execution_id, case_id=case_id)
      .count()
    )


def _count_rows_via_engine(execution_id: str, case_id: str) -> int:
  """
  绕过 session_scope 统计明细行数（用例替换了 session_scope 时使用）

  参数:
    execution_id (str): 执行批次号
    case_id (str): 业务用例编号

  返回:
    int: 行数
  """
  with DatabaseSession.get_engine().connect() as conn:
    return int(
      conn.execute(
        text(
          "SELECT COUNT(*) FROM test_executions "
          "WHERE execution_id = :execution_id AND case_id = :case_id"
        ),
        {"execution_id": execution_id, "case_id": case_id},
      ).scalar_one()
    )


def _raw_journal_mode(db_file: Path) -> str:
  """
  用裸 sqlite3 连接读取库自身的 journal 模式（不经过 SQLAlchemy 事件链）

  对照基准: 裸连接上没有任何 PRAGMA 监听器，它读到的就是"库自己的设置"。
  引擎侧若读到同一个值，即证明我们没有干预。

  参数:
    db_file (Path): 数据库文件路径

  返回:
    str: 小写的模式名（delete / wal / ...）
  """
  with sqlite3.connect(db_file) as conn:
    row = conn.execute("PRAGMA journal_mode").fetchone()
  return str(row[0]).lower()


def _create_legacy_engine(db_file: Path) -> Engine:
  """
  构造"修复前结构"的库：test_executions 无 (execution_id, case_id) 唯一约束

  刻意不用 Base.metadata.create_all（那会带上本次新加的唯一约束），
  而是手写旧结构的 CREATE TABLE，才能复现存量库的迁移场景。

  参数:
    db_file (Path): 目标数据库文件路径

  返回:
    Engine: 指向该文件的引擎
  """
  engine = create_engine(f"sqlite:///{db_file.as_posix()}", future=True)
  with engine.begin() as conn:
    conn.execute(
      text(
        "CREATE TABLE test_executions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "execution_id VARCHAR(64) NOT NULL, "
        "case_id VARCHAR(64) NOT NULL, "
        "case_name VARCHAR(200) NOT NULL DEFAULT '', "
        "result VARCHAR(16) NOT NULL, "
        "start_time DATETIME, end_time DATETIME, "
        "duration FLOAT NOT NULL DEFAULT 0.0, "
        "error_message TEXT, "
        "environment VARCHAR(16) NOT NULL DEFAULT 'dev', "
        "executor VARCHAR(64) NOT NULL DEFAULT 'local', "
        "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
      )
    )
    conn.execute(text("CREATE INDEX idx_execution_result ON test_executions (execution_id, result)"))
  return engine


@allure.feature("数据库写库冲突")
@allure.story("(execution_id, case_id) 唯一约束")
@pytest.mark.api
@pytest.mark.regression
class TestExecutionUniqueKey:
  """唯一约束落在库表上的双重证据（schema 级 + 写入级）"""

  def test_unique_constraint_declared_on_model(self, temp_db):
    """
    schema 级证据: create_all 建出的库上，(execution_id, case_id) 唯一性
    已生效（唯一约束或唯一索引任一，判据统一由 has_execution_unique_key 回答）

    参数:
      temp_db (Path): 临时库 fixture

    返回:
      无
    """
    assert migration.has_execution_unique_key(inspect(DatabaseSession.get_engine())) is True

  def test_duplicate_insert_rejected_by_database(self, temp_db):
    """
    写入级证据: 绕过 ORM 应用层判重，两条同键裸 INSERT 里第二条必须被库拒绝

    这是本日的核心断言——若约束只存在于应用层判断，并发下必然双写。

    参数:
      temp_db (Path): 临时库 fixture

    返回:
      无
    """
    # 裸 SQL 必须自带全部 NOT NULL 列：模型上的 default= 是 Python 侧默认值，
    # 绕过 ORM 就不会被填充（只写三列会先撞 case_name 的 NOT NULL）
    insert_sql = text(
      "INSERT INTO test_executions "
      "(execution_id, case_id, case_name, result, duration, environment, executor) "
      "VALUES (:execution_id, :case_id, '裸SQL用例', 'passed', 1.0, 'dev', 'local')"
    )
    with DatabaseSession.session_scope() as session:
      session.execute(insert_sql, {"execution_id": "RUN-DAY60-DUP", "case_id": "TM-0001"})

    with pytest.raises(IntegrityError):
      with DatabaseSession.session_scope() as session:
        session.execute(insert_sql, {"execution_id": "RUN-DAY60-DUP", "case_id": "TM-0001"})

    # 冲突后仍是 1 行（拒绝的是整条语句，不是"插一半"）
    assert _count_rows("RUN-DAY60-DUP", "TM-0001") == 1

  def test_same_case_in_different_batches_allowed(self, temp_db):
    """
    约束粒度是 (批次, 用例)，不是 case_id 单列：
    同一用例在不同批次里必须都能落库（否则统计口径会被压扁成一条）

    参数:
      temp_db (Path): 临时库 fixture

    返回:
      无
    """
    assert _record("RUN-DAY60-A", "TM-0001") is True
    assert _record("RUN-DAY60-B", "TM-0001") is True
    assert _count_rows("RUN-DAY60-A", "TM-0001") == 1
    assert _count_rows("RUN-DAY60-B", "TM-0001") == 1


@allure.feature("数据库写库冲突")
@allure.story("record_execution 幂等")
@pytest.mark.api
@pytest.mark.regression
class TestRecordExecutionIdempotency:
  """重复写入的两条路径（写前查重 / 写时撞约束）返回同一语义"""

  def test_duplicate_record_execution_is_idempotent(self, temp_db):
    """
    同一 (execution_id, case_id) 连写两次: 第一次 True、第二次 False，
    表内仍只有 1 行（写前查重路径，Day45 起的既有语义）

    参数:
      temp_db (Path): 临时库 fixture

    返回:
      无
    """
    assert _record("RUN-DAY60-IDEM", "TM-0001") is True
    assert _record("RUN-DAY60-IDEM", "TM-0001") is False
    assert _count_rows("RUN-DAY60-IDEM", "TM-0001") == 1

  def test_integrity_error_with_existing_row_absorbed(self, temp_db, monkeypatch):
    """
    写时撞唯一约束路径: IntegrityError + 回查确认该键已有行 → 吸收为 False

    为什么要把异常路径做成可注入的用例：真实并发撞约束的时序无法在单测里
    稳定复现（SQLite 下更可能先撞锁超时），而这条分支的正确性又直接决定
    "并发双写会不会被当成故障"。故在"DB 会在 commit 时抛 IntegrityError"
    这个边界上注入，验证分支语义本身。

    参数:
      temp_db (Path): 临时库 fixture
      monkeypatch (pytest.MonkeyPatch): 补丁工具

    返回:
      无
    """
    assert _record("RUN-DAY60-RACE", "TM-0001") is True
    monkeypatch.setattr(
      DatabaseSession,
      "session_scope",
      _scope_raising_integrity_error,
    )
    monkeypatch.setattr(
      CaseManager, "_execution_detail_exists", classmethod(lambda cls, e, c: True)
    )

    assert _record("RUN-DAY60-RACE", "TM-0001") is False
    assert _count_rows_via_engine("RUN-DAY60-RACE", "TM-0001") == 1

  def test_integrity_error_without_existing_row_raised(self, temp_db, monkeypatch):
    """
    写时撞约束但回查无行 → 不是重复键，必须原样上抛 CaseManagerError

    这是"不许一律吸收"的反向对照：若把全部 IntegrityError 都当重复，
    NOT NULL / CHECK 违例会被伪装成"已存在，跳过"，数据缺陷静默消失。

    参数:
      temp_db (Path): 临时库 fixture
      monkeypatch (pytest.MonkeyPatch): 补丁工具

    返回:
      无
    """
    monkeypatch.setattr(
      DatabaseSession,
      "session_scope",
      _scope_raising_integrity_error,
    )
    monkeypatch.setattr(
      CaseManager, "_execution_detail_exists", classmethod(lambda cls, e, c: False)
    )

    with pytest.raises(CaseManagerError) as exc_info:
      _record("RUN-DAY60-OTHER", "TM-0002")
    assert exc_info.value.context.get("operation") == "record_execution"

  def test_detail_exists_query_failure_treated_as_not_duplicate(
    self, temp_db, monkeypatch
  ):
    """
    回查本身失败时返回 False（让调用方按真实错误上抛，不把故障吸收成"跳过"）

    参数:
      temp_db (Path): 临时库 fixture
      monkeypatch (pytest.MonkeyPatch): 补丁工具

    返回:
      无
    """

    @contextmanager
    def _broken_scope():
      """替身 session_scope：进入即抛数据库异常"""
      raise OperationalError("SELECT 1", {}, Exception("connection reset"))
      yield  # pragma: no cover  只为把本函数标记为上下文管理器生成器

    monkeypatch.setattr(DatabaseSession, "session_scope", _broken_scope)
    assert CaseManager._execution_detail_exists("RUN-DAY60-Q", "TM-0003") is False


@allure.feature("数据库写库冲突")
@allure.story("并发写入")
@pytest.mark.api
@pytest.mark.regression
class TestConcurrentWrite:
  """多线程同时写同一键：最终只有 1 行，且只有一个写入方"成功" """

  def test_concurrent_same_key_ends_with_single_row(self, temp_db):
    """
    4 个线程同时写同一 (execution_id, case_id)，最终表内恰好 1 行

    断言口径刻意只落在"最终状态"上（行数 = 1、成功方 = 1）而不落在
    "谁成功"或"用了哪条路径"上——并发下写入方可能被写前查重挡住，
    也可能撞唯一约束被吸收，两条路径都是正确结果，不该按实现细节断言。

    参数:
      temp_db (Path): 临时库 fixture

    返回:
      无
    """
    thread_count = 4
    barrier = threading.Barrier(thread_count)
    outcomes: list[bool] = []
    errors: list[str] = []

    def worker() -> None:
      """子线程任务: 等到全部线程就绪后同时写入同一键"""
      try:
        barrier.wait(timeout=10)
        outcomes.append(_record("RUN-DAY60-CONCURRENT", "TM-0009"))
      except Exception as exc:  # noqa: BLE001 子线程异常回传主线程统一断言
        errors.append(f"{type(exc).__name__}: {exc}")

    threads = [
      threading.Thread(target=worker, name=f"writer-{index}")
      for index in range(thread_count)
    ]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join(timeout=20)
      assert not thread.is_alive(), "并发写入线程超时未结束"

    assert errors == [], f"并发写入抛出未预期异常: {errors}"
    assert len(outcomes) == thread_count
    # 恰好 1 个线程"真正写入"，其余被去重吸收
    assert outcomes.count(True) == 1
    assert _count_rows("RUN-DAY60-CONCURRENT", "TM-0009") == 1


@allure.feature("数据库写库冲突")
@allure.story("SQLite 写锁等待")
@pytest.mark.api
@pytest.mark.regression
class TestSqlitePragmas:
  """busy_timeout / journal_mode 的配置口径"""

  def test_busy_timeout_applied_from_env(self, temp_db, monkeypatch):
    """
    TM_SQLITE_BUSY_TIMEOUT_MS=30000 时连接上的 busy_timeout 必须是 30000

    参数:
      temp_db (Path): 临时库 fixture
      monkeypatch (pytest.MonkeyPatch): 补丁工具

    返回:
      无
    """
    monkeypatch.setenv("TM_SQLITE_BUSY_TIMEOUT_MS", "30000")
    DatabaseSession.reset()
    with DatabaseSession.get_engine().connect() as conn:
      assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 30000

  def test_busy_timeout_default_keeps_library_default(self, temp_db, monkeypatch):
    """
    未配置时取 5000ms（pysqlite 内建默认）——生产默认值不被本次改动抬升

    参数:
      temp_db (Path): 临时库 fixture
      monkeypatch (pytest.MonkeyPatch): 补丁工具

    返回:
      无
    """
    monkeypatch.delenv("TM_SQLITE_BUSY_TIMEOUT_MS", raising=False)
    monkeypatch.setenv("TM_SQLITE_BUSY_TIMEOUT_MS", "")
    assert sqlite_busy_timeout_ms() == SQLITE_BUSY_TIMEOUT_MS_DEFAULT == 5000
    DatabaseSession.reset()
    with DatabaseSession.get_engine().connect() as conn:
      assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000

  def test_journal_mode_default_is_untouched(self, temp_db, monkeypatch):
    """
    默认不干预 journal_mode：引擎侧看到的模式与"裸连接看到的库自身模式"一致

    断言刻意**不写死 delete**：本用例要证明的是"我们没改它"，而不是"它是
    delete"。写死 delete 会让开发者在 .env 里开了 WAL 时看到一条与本次
    改动无关的红——把环境配置差异误报成回归。

    参数:
      temp_db (Path): 临时库 fixture
      monkeypatch (pytest.MonkeyPatch): 补丁工具

    返回:
      无
    """
    # 显式清掉配置，才是在验证"未配置"这条语义（开发机 .env 可能已开 WAL）
    monkeypatch.delenv("TM_SQLITE_JOURNAL_MODE", raising=False)
    DatabaseSession.reset()
    assert sqlite_journal_mode() is None
    with DatabaseSession.get_engine().connect() as conn:
      engine_mode = str(conn.execute(text("PRAGMA journal_mode")).scalar()).lower()
    assert engine_mode == _raw_journal_mode(temp_db)

  def test_journal_mode_wal_applied_when_configured(self, temp_db, monkeypatch):
    """
    TM_SQLITE_JOURNAL_MODE=WAL 时连接实际进入 wal 模式（实验开关可用）

    参数:
      temp_db (Path): 临时库 fixture
      monkeypatch (pytest.MonkeyPatch): 补丁工具

    返回:
      无
    """
    monkeypatch.setenv("TM_SQLITE_JOURNAL_MODE", "wal")
    DatabaseSession.reset()
    with DatabaseSession.get_engine().connect() as conn:
      assert conn.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"

  def test_pragma_failure_does_not_break_connection(self):
    """
    PRAGMA 下发失败时只记 warning，不让连接建立失败

    这是纵深防御：设 PRAGMA 是运维增强，不是连接的前置条件；把它做成
    "设不上就建不了连接"会让一个可选配置变成启动阻断。

    返回:
      无
    """

    class _BrokenCursor:
      """替身游标：任何执行都失败"""

      def execute(self, *args, **kwargs):
        """恒抛 sqlite3 异常"""
        raise sqlite3.OperationalError("database is locked")

      def close(self):
        """空实现（与真实游标接口对齐）"""

    class _BrokenConnection:
      """替身 DBAPI 连接：只提供 cursor()"""

      def cursor(self):
        """返回恒失败的替身游标"""
        return _BrokenCursor()

    # 不抛异常即通过（异常会被 _apply_sqlite_pragmas 内部消化为 warning）
    assert _apply_sqlite_pragmas(_BrokenConnection(), None) is None

  def test_journal_mode_invalid_falls_back_to_untouched(self, temp_db, monkeypatch):
    """
    非法 journal_mode 取值按"不干预"处理（白名单外不得拼进 PRAGMA）

    参数:
      temp_db (Path): 临时库 fixture
      monkeypatch (pytest.MonkeyPatch): 补丁工具

    返回:
      无
    """
    monkeypatch.setenv("TM_SQLITE_JOURNAL_MODE", "WAL; DROP TABLE test_cases")
    assert sqlite_journal_mode() is None
    DatabaseSession.reset()
    with DatabaseSession.get_engine().connect() as conn:
      engine_mode = str(conn.execute(text("PRAGMA journal_mode")).scalar()).lower()
    # 未下发 PRAGMA，故与裸连接看到的库自身模式一致（不写死具体模式）
    assert engine_mode == _raw_journal_mode(temp_db)


@allure.feature("数据库写库冲突")
@allure.story("存量库迁移")
@pytest.mark.api
@pytest.mark.regression
class TestUniqueKeyMigration:
  """存量库"先清重复行、后建唯一键"的可重入迁移"""

  def test_dedupe_keeps_latest_row(self, temp_db):
    """
    去重保留 MAX(id)（同键多行里最后落库的那一行），并如实返回删除行数

    参数:
      temp_db (Path): 临时库 fixture

    返回:
      无
    """
    engine = _create_legacy_engine(temp_db.parent / "legacy.db")
    try:
      with engine.begin() as conn:
        for index, result in enumerate(("passed", "failed", "error"), start=1):
          conn.execute(
            text(
              "INSERT INTO test_executions (id, execution_id, case_id, result) "
              "VALUES (:id, 'RUN-LEGACY', 'TM-0001', :result)"
            ),
            {"id": index, "result": result},
          )
        conn.execute(
          text(
            "INSERT INTO test_executions (id, execution_id, case_id, result) "
            "VALUES (4, 'RUN-LEGACY', 'TM-0002', 'passed')"
          )
        )

      assert migration.dedupe_duplicate_executions(engine) == 2

      with engine.connect() as conn:
        rows = conn.execute(
          text(
            "SELECT id, case_id, result FROM test_executions ORDER BY case_id"
          )
        ).all()
      # TM-0001 保留 id=3（MAX），TM-0002 只有一行不受影响
      assert [(row.id, row.case_id, row.result) for row in rows] == [
        (3, "TM-0001", "error"),
        (4, "TM-0002", "passed"),
      ]
    finally:
      engine.dispose()

  def test_dedupe_on_missing_table_returns_zero(self, tmp_path):
    """
    表不存在时去重返回 0 而不是抛 no such table（空库/全新 clone 是正常态）

    参数:
      tmp_path (Path): pytest 临时目录

    返回:
      无
    """
    engine = create_engine(f"sqlite:///{(tmp_path / 'empty.db').as_posix()}", future=True)
    try:
      assert migration.dedupe_duplicate_executions(engine) == 0
    finally:
      engine.dispose()

  def test_ensure_unique_key_is_rerunnable(self, tmp_path):
    """
    迁移可重入: 首次 dedupe+建键（created=True、报告删除行数），
    第二次 created=False、deduped=0，且数据不重复

    参数:
      tmp_path (Path): pytest 临时目录

    返回:
      无
    """
    engine = _create_legacy_engine(tmp_path / "legacy.db")
    try:
      with engine.begin() as conn:
        for index in range(1, 4):
          conn.execute(
            text(
              "INSERT INTO test_executions (id, execution_id, case_id, result) "
              "VALUES (:id, 'RUN-LEGACY', 'TM-0001', 'passed')"
            ),
            {"id": index},
          )

      first = migration.ensure_execution_unique_key(engine)
      assert first["created"] is True
      assert first["deduped_rows"] == 2
      assert first["key_name"] == EXECUTION_UNIQUE_KEY_NAME

      second = migration.ensure_execution_unique_key(engine)
      assert second["created"] is False
      assert second["deduped_rows"] == 0

      with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM test_executions")).scalar_one() == 1
      # 建键之后重复写入必须被库拒绝
      with pytest.raises(IntegrityError):
        with engine.begin() as conn:
          conn.execute(
            text(
              "INSERT INTO test_executions (execution_id, case_id, result) "
              "VALUES ('RUN-LEGACY', 'TM-0001', 'passed')"
            )
          )
    finally:
      engine.dispose()

  def test_has_unique_key_false_when_table_missing(self, tmp_path):
    """
    表不存在时判据返回 False（而不是抛 no such table）

    参数:
      tmp_path (Path): pytest 临时目录

    返回:
      无
    """
    engine = create_engine(f"sqlite:///{(tmp_path / 'bare.db').as_posix()}", future=True)
    try:
      assert migration.has_execution_unique_key(inspect(engine)) is False
    finally:
      engine.dispose()

  def test_init_db_backfills_legacy_unique_key(self, tmp_path, monkeypatch):
    """
    init_db 对存量库自动补齐唯一键（升级代码即生效，不依赖手工跑迁移）

    参数:
      tmp_path (Path): pytest 临时目录
      monkeypatch (pytest.MonkeyPatch): 补丁工具

    返回:
      无
    """
    db_file = tmp_path / "legacy_init.db"
    legacy_engine = _create_legacy_engine(db_file)
    try:
      with legacy_engine.begin() as conn:
        for index in range(1, 3):
          conn.execute(
            text(
              "INSERT INTO test_executions (id, execution_id, case_id, result) "
              "VALUES (:id, 'RUN-LEGACY-INIT', 'TM-0001', 'passed')"
            ),
            {"id": index},
          )
    finally:
      legacy_engine.dispose()

    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    DatabaseSession.reset()
    DatabaseSession.init_db()

    assert migration.has_execution_unique_key(inspect(DatabaseSession.get_engine())) is True
    assert _count_rows("RUN-LEGACY-INIT", "TM-0001") == 1

  def test_source_ref_migration_dedupes_before_rebuild(self, tmp_path, monkeypatch):
    """
    source_ref 重建库迁移在**读快照前**先去重，迁移后不残留重复行

    为什么必须在这个位置: 重建库的写回走裸 INSERT，新表带唯一约束——
    快照里留着重复行就会在写回阶段撞约束并整库回滚，把"加列迁移"
    变成"迁移失败"。

    参数:
      tmp_path (Path): pytest 临时目录
      monkeypatch (pytest.MonkeyPatch): 补丁工具

    返回:
      无
    """
    db_file = tmp_path / "legacy_dup.db"
    legacy_engine = _create_legacy_engine(db_file)
    try:
      with legacy_engine.begin() as conn:
        for index in range(1, 4):
          conn.execute(
            text(
              "INSERT INTO test_executions (id, execution_id, case_id, result) "
              "VALUES (:id, 'RUN-LEGACY-SRC', 'TM-0001', 'passed')"
            ),
            {"id": index},
          )
        conn.execute(
          text(
            "INSERT INTO test_executions (id, execution_id, case_id, result) "
            "VALUES (9, 'RUN-LEGACY-SRC', 'TM-0002', 'passed')"
          )
        )
    finally:
      legacy_engine.dispose()

    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    DatabaseSession.reset()
    with migration.backup_dir_override(tmp_path / "backups"):
      result = migration.migrate_add_source_ref()

    assert result["status"] == migration.STATUS_MIGRATED
    assert result["deduped_rows"] == 2
    assert result["rows_before"] == result["rows_after"] == 2
    assert _count_rows("RUN-LEGACY-SRC", "TM-0001") == 1

  def test_ensure_unique_key_skips_missing_table(self, tmp_path):
    """
    表不存在时跳过建键（不抛错，把建表职责留给 create_all）

    参数:
      tmp_path (Path): pytest 临时目录

    返回:
      无
    """
    engine = create_engine(f"sqlite:///{(tmp_path / 'no_table.db').as_posix()}", future=True)
    try:
      outcome = migration.ensure_execution_unique_key(engine)
      assert outcome["created"] is False
      assert outcome["deduped_rows"] == 0
    finally:
      engine.dispose()

  def test_ensure_unique_key_unsupported_backend_skipped(self, temp_db, monkeypatch):
    """
    非 sqlite/mysql 后端跳过建键（不构造 DDL、不改库）

    参数:
      temp_db (Path): 临时库 fixture
      monkeypatch (pytest.MonkeyPatch): 补丁工具

    返回:
      无
    """
    monkeypatch.setattr(migration, "_current_backend", lambda engine: "postgresql")
    outcome = migration.ensure_execution_unique_key(DatabaseSession.get_engine())
    assert outcome["created"] is False
    assert outcome["db_type"] == "postgresql"

  def test_build_ddl_for_mysql(self):
    """
    MySQL 方言的建键语句必须是 ALTER TABLE ADD UNIQUE KEY（无 IF NOT EXISTS）

    返回:
      无
    """
    ddl = migration.build_execution_unique_key_ddl("mysql")
    assert ddl.startswith("ALTER TABLE test_executions ADD UNIQUE KEY")
    assert EXECUTION_UNIQUE_KEY_NAME in ddl
    assert "(execution_id, case_id)" in ddl

  def test_build_ddl_for_unknown_backend_raises(self):
    """
    未知后端无法构造 DDL，必须显式报错而不是返回一条错的 SQL

    返回:
      无
    """
    with pytest.raises(migration.MigrationError):
      migration.build_execution_unique_key_ddl("postgresql")


@allure.feature("数据库写库冲突")
@allure.story("批次明细对账")
@pytest.mark.api
@pytest.mark.regression
class TestBatchReconciliation:
  """批次聚合与明细行的一一对账（无重复、无遗漏、计数一致）"""

  def test_repeated_writes_do_not_inflate_batch_totals(self, temp_db):
    """
    同一批用例被重复写入（模拟崩溃补偿重投 / 双 worker 同跑）后，
    finish_execution 的 total 必须等于**去重后的用例数**，不是写入次数

    参数:
      temp_db (Path): 临时库 fixture

    返回:
      无
    """
    execution_id = "RUN-DAY60-RECON"
    case_ids = [f"TM-RECON-{index:04d}" for index in range(1, 6)]
    for case_id in case_ids:
      assert _record(execution_id, case_id) is True
    # 整批重跑一遍：全部被去重吸收
    for case_id in case_ids:
      assert _record(execution_id, case_id) is False

    stats = CaseManager.finish_execution(execution_id)
    assert stats["total"] == len(case_ids)
    assert stats["passed"] == len(case_ids)
    assert stats["pass_rate"] == 1.0

    with DatabaseSession.session_scope() as session:
      rows = (
        session.query(ExecutionModel.case_id)
        .filter_by(execution_id=execution_id)
        .all()
      )
    assert sorted(row.case_id for row in rows) == case_ids

  def test_progress_update_on_missing_batch_returns_none(self, temp_db):
    """
    批次行不存在时进度累加返回 None（不抛错、不静默造行）

    原路径只有依赖后台线程时序的用例偶然覆盖，导致覆盖率在同一提交上
    波动（±0.02%）。这里给出一条确定性路径：进度回写是热路径，任何
    "读改写时批次已被删除"的场景都靠这个 None 降级。

    参数:
      temp_db (Path): 临时库 fixture

    返回:
      无
    """
    assert CaseManager.update_batch_progress("RUN-NO-SUCH-BATCH", "passed") is None

  def test_detail_keys_are_unique_per_batch(self, temp_db):
    """
    (execution_id, case_id) 在库内唯一：distinct 对数 = 总行数

    参数:
      temp_db (Path): 临时库 fixture

    返回:
      无
    """
    execution_id = "RUN-DAY60-DISTINCT"
    for case_id in ("TM-D-0001", "TM-D-0002", "TM-D-0001"):
      _record(execution_id, case_id)

    with DatabaseSession.session_scope() as session:
      total = session.query(ExecutionModel).filter_by(execution_id=execution_id).count()
      distinct_pairs = (
        session.query(ExecutionModel.execution_id, ExecutionModel.case_id)
        .filter_by(execution_id=execution_id)
        .distinct()
        .count()
      )
    assert total == distinct_pairs == 2


@contextmanager
def _scope_raising_integrity_error():
  """
  替身 session_scope：进入即抛 IntegrityError（模拟 commit 阶段撞唯一约束）

  做成上下文管理器而非普通函数，是为了让异常在 `with` 语句的 __enter__
  阶段抛出——与真实实现"commit 时抛"对调用方可见的形态一致（都在 with
  块外被 record_execution 的 except IntegrityError 捕获）。

  返回:
    Iterator[Session]: 不产出有效 session（进入即抛）

  异常:
    IntegrityError: 恒抛出
  """
  raise IntegrityError(
    "INSERT INTO test_executions", {}, Exception("UNIQUE constraint failed")
  )
  yield  # pragma: no cover  只为把本函数标记为上下文管理器生成器
