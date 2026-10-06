"""
source_ref 字段模型测试 + 数据库迁移脚本测试（Day46）

测试范围:
    1. 模型字段测试: TestCase.source_ref 的默认值/可空性/赋值读写/
       列定义（String(512)+nullable+comment 语义）/超长值实际行为/__repr__ 截断
    2. 迁移备份测试: 迁移前自动备份、备份非空、幂等窗口内不重复备份、
       空库不产生备份
    3. 迁移数据完整性测试: 存量用例逐字段不变、source_ref 全为 None、
       其他表数据完整保留、重复迁移不丢数据
    4. 迁移异常与后端分支测试: 迁移中途失败触发回滚、MySQL 模式跳过

数据隔离设计:
    - 数据库文件: monkeypatch 把 TM_DB_SQLITE_PATH 指向 tmp_path 下的独立库
    - 备份目录: backup_dir_override() 把备份重定向到 tmp_path/backups，
      绝不写入真实 output/backups/
    - 每个用例独立的 tmp_path，结束后 reset() 释放引擎连接
      （Windows 下不释放会锁住库文件导致删除失败）
    - 零真实网络/服务依赖：MySQL 分支只构造引擎不建连（create_engine 惰性连接）
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect, text

# 别名导入约定：模型类名以 Test 开头，直接 import 会被 pytest 的类名启发式
# 当成测试类收集并抛 PytestCollectionWarning（验收要求 0 warning），
# 故 TestCase 在本文件一律以 CaseModel 别名引入
from src.db import migration
from src.db.db_session import DatabaseSession
from src.db.migration import (
    MigrationError,
    backup_dir_override,
    get_backup_dir,
    migrate_add_source_ref,
)
from src.db.models import TestCase as CaseModel

# ---------------------------------------------------------------------------
# 造数与读数辅助
# ---------------------------------------------------------------------------


def _seed_demo_data(case_count: int = 5) -> None:
    """
    向当前库写入存量用例 + 执行明细 + 批次行

    用例之外的执行明细/批次行一并造数，用于验证迁移不只保住 test_cases 一张表。

    参数:
        case_count (int): 用例条数，默认 5

    返回:
        None
    """
    from src.db.models import TestExecution, TestExecutionBatch

    with DatabaseSession.session_scope() as session:
        for index in range(1, case_count + 1):
            session.add(
                CaseModel(
                    case_id=f"TM-LEGACY-{index:04d}",
                    name=f"存量用例{index}",
                    module="用户中心" if index % 2 else "订单",
                    priority="P0" if index % 2 else "P2",
                    case_type="api",
                    description=f"存量用例{index}的验证点",
                    creator="legacy",
                )
            )
        session.add(
            TestExecutionBatch(
                execution_id="RUN-20260101-100000-legacy",
                trigger="cli",
                status="finished",
                total_cases=case_count,
                passed=case_count,
                pass_rate=1.0,
            )
        )
        for index in range(1, case_count + 1):
            session.add(
                TestExecution(
                    execution_id="RUN-20260101-100000-legacy",
                    case_id=f"TM-LEGACY-{index:04d}",
                    case_name=f"存量用例{index}",
                    result="passed",
                    duration=0.5,
                )
            )


def _drop_source_ref_column(db_path: Path) -> None:
    """
    从已建表的库中删掉 source_ref 列，把库退化为"迁移前"结构

    用 SQLite 原生 ALTER TABLE ... DROP COLUMN（3.35+）而非另写一份建表 DDL，
    保证除 source_ref 外其余列定义与 models.py 完全一致，迁移对比才有效。

    参数:
        db_path (Path): 数据库文件路径

    返回:
        None

    异常:
        sqlite3.Error: 列不存在或 SQLite 版本过低时抛出
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE test_cases DROP COLUMN source_ref")
        conn.commit()


def _dump_table(table_name: str) -> list[dict[str, Any]]:
    """
    用裸 SQL 读出整表全部列（与迁移脚本读路径同口径）

    刻意不经过 ORM：ORM 会按模型声明补齐 source_ref 列，
    那样就看不出"迁移前根本没有这一列"，也无法逐字段比对往返保真度。

    参数:
        table_name (str): 表名（仅测试内部常量）

    返回:
        list[dict[str, Any]]: 按 id/主键升序的全行字典列表
    """
    order = "execution_id" if table_name == "test_execution_batches" else "rowid"
    with DatabaseSession.get_engine().connect() as conn:
        statement = text(f"SELECT * FROM {table_name} ORDER BY {order}")
        rows = conn.execute(statement).mappings().all()
        return [dict(row) for row in rows]


def _list_backups(backup_dir: Path) -> list[Path]:
    """
    列出备份目录下的备份文件

    参数:
        backup_dir (Path): 备份目录

    返回:
        list[Path]: 备份文件列表（按文件名排序）
    """
    return sorted(backup_dir.glob("testmatrix_backup_*.db"))


def _list_tables() -> list[str]:
    """列出当前库的全部表名（用 SQLAlchemy inspect，与迁移脚本同口径）"""
    return list(inspect(DatabaseSession.get_engine()).get_table_names())


def _table_columns(table_name: str) -> list[str]:
    """读出某表当前的列名列表"""
    inspector = inspect(DatabaseSession.get_engine())
    return [str(col["name"]) for col in inspector.get_columns(table_name)]


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def backup_dir(tmp_path: Path) -> Iterator[Path]:
    """迁移备份目录夹具（重定向到 tmp_path，绝不污染真实 output/backups/）"""
    target = tmp_path / "backups"
    target.mkdir(parents=True, exist_ok=True)
    with backup_dir_override(target):
        yield target


@pytest.fixture()
def legacy_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    旧结构数据库夹具（无 source_ref 列 + 存量数据）

    步骤: 建新表 → 造数 → 删掉 source_ref 列，得到与 Day46 改造前一致的库。

    参数:
        tmp_path (Path): pytest 临时目录
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

    返回:
        Iterator[Path]: yield 数据库文件路径
    """
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    _seed_demo_data()
    # 先释放引擎连接再改表结构，避免 Windows 文件锁
    DatabaseSession.reset()
    _drop_source_ref_column(db_path)
    yield db_path
    DatabaseSession.reset()


@pytest.fixture()
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    全新空库夹具（文件存在但表未建、无任何数据）

    用于验证"空库执行迁移不报错、不产生备份"。

    参数:
        tmp_path (Path): pytest 临时目录
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

    返回:
        Iterator[Path]: yield 数据库文件路径
    """
    db_path = tmp_path / "empty.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    DatabaseSession.reset()
    yield db_path
    DatabaseSession.reset()


# ---------------------------------------------------------------------------
# 一、模型字段测试
# ---------------------------------------------------------------------------


def test_source_ref_column_definition() -> None:
    """
    source_ref 列定义符合设计：String(512)/可空/默认 None/含不可执行语义

    列定义是 Day46 的交付本体，用测试锁死可防后续误改成非空或丢掉语义注释。
    """
    column = CaseModel.__table__.columns["source_ref"]
    assert column.type.length == 512
    assert column.nullable is True
    assert column.default is None
    assert "不可被 pytest 执行" in (column.comment or "")


def test_source_ref_defaults_to_none() -> None:
    """实例化 TestCase 时 source_ref 默认为 None（不传即为空）"""
    assert CaseModel(case_id="TM-API-0001", name="登录校验").source_ref is None


def test_source_ref_assign_and_read() -> None:
    """source_ref 可正常赋值与读取，函数级 node id 形态原样保留"""
    case = CaseModel(case_id="TM-API-0002", name="下单校验")
    case.source_ref = "tests/api_demo/test_api_login.py::TestUserLogin::test_user_login"
    assert case.source_ref == (
        "tests/api_demo/test_api_login.py::TestUserLogin::test_user_login"
    )


def test_source_ref_explicit_none_allowed() -> None:
    """source_ref 显式赋 None 不报错（空值即不可被 pytest 执行）"""
    case = CaseModel(case_id="TM-API-0003", name="查询校验", source_ref=None)
    assert case.source_ref is None


def test_source_ref_empty_string_differs_from_none() -> None:
    """
    空串与 None 是两种不同的空值，ORM 层不做归一

    二者业务语义一致（都不可被 pytest 执行），但必须区分断言：
    迁移脚本只补 None、不补空串，若某天把空串也当作"未回填"清洗掉，
    这条用例即变红。
    """
    empty = CaseModel(case_id="TM-API-0004", name="删除校验", source_ref="")
    none_case = CaseModel(case_id="TM-API-0005", name="清空校验", source_ref=None)
    assert empty.source_ref == ""
    assert none_case.source_ref is None
    assert empty.source_ref is not none_case.source_ref


def test_source_ref_persists_over_512_chars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    超 512 字符的值在 SQLite 下**不被拒绝也不被截断**（实测行为）

    SQLite 不实现 VARCHAR 长度约束，SQLAlchemy 的 String(512) 在 SQLite 上
    只是声明；真正会拒绝的是 MySQL（非严格模式截断/严格模式报错）。
    本用例把这一事实钉死，避免有人误以为"超长会被库挡住"而在入库前
    另写一套校验；Day68 切 MySQL 后需按届时实际模式重测。
    """
    db_path = tmp_path / "long_ref.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    try:
        long_ref = "tests/" + ("a" * 600) + ".py"
        with DatabaseSession.session_scope() as session:
            session.add(
                CaseModel(case_id="TM-API-0006", name="超长路径", source_ref=long_ref)
            )
        rows = _dump_table("test_cases")
        assert len(rows) == 1
        assert rows[0]["source_ref"] == long_ref
        assert len(rows[0]["source_ref"]) > 512
    finally:
        DatabaseSession.reset()


def test_repr_truncates_long_source_ref() -> None:
    """
    __repr__ 中 source_ref 超长截断并补省略号，None 原样显示

    调试日志常直接打印模型实例，不截断会把每行日志撑成长路径。
    """
    case = CaseModel(case_id="TM-API-0007", name="截断校验")
    case.source_ref = "tests/" + ("b" * 80) + ".py"
    text_repr = repr(case)
    assert "..." in text_repr
    assert "b" * 80 not in text_repr

    none_case = CaseModel(case_id="TM-API-0008", name="空值校验")
    assert "source_ref=None" in repr(none_case)


# ---------------------------------------------------------------------------
# 二、迁移备份测试
# ---------------------------------------------------------------------------


def test_get_backup_dir_creates_dir(backup_dir: Path) -> None:
    """get_backup_dir 返回已创建的目录，且受重定向控制"""
    resolved = get_backup_dir()
    assert resolved == backup_dir
    assert resolved.is_dir()


def test_migration_creates_non_empty_backup(legacy_db: Path, backup_dir: Path) -> None:
    """迁移前自动生成备份，且备份文件非空"""
    result = migrate_add_source_ref()
    backups = _list_backups(backup_dir)
    assert len(backups) == 1
    assert backups[0].stat().st_size > 0
    assert result["backup_created"] is True
    assert result["backup_path"] == str(backups[0])


def test_migration_skips_duplicate_backup_within_window(
    legacy_db: Path, backup_dir: Path
) -> None:
    """幂等窗口内重复执行迁移，不产生第二份备份"""
    migrate_add_source_ref()
    second = migrate_add_source_ref()
    assert len(_list_backups(backup_dir)) == 1
    assert second["backup_created"] is False
    assert second["backup_path"] is None
    # 跳过备份不等于跳过校验：行数仍需比对通过。
    # rows_before/rows_after 是**全库**总行数口径
    # （5 用例 + 5 执行明细 + 1 批次行），不是单表条数
    assert second["rows_after"] == second["rows_before"] == 11


def test_migration_backups_again_after_window(legacy_db: Path, backup_dir: Path) -> None:
    """窗口外（伪造为 2 小时前）再次执行迁移，重新备份"""
    stale = backup_dir / "testmatrix_backup_20200101_000000.db"
    stale.write_bytes(legacy_db.read_bytes())
    result = migrate_add_source_ref()
    backups = _list_backups(backup_dir)
    assert stale in backups
    assert len(backups) == 2
    assert result["backup_created"] is True


def test_empty_db_migration_creates_no_backup(empty_db: Path, backup_dir: Path) -> None:
    """空库执行迁移不报错、不产生备份（无数据可备份）"""
    result = migrate_add_source_ref()
    assert _list_backups(backup_dir) == []
    assert result["backup_created"] is False
    assert result["rows_before"] == 0
    assert result["rows_after"] == 0
    # 新库表结构应已带上 source_ref 列
    rows = _dump_table("test_cases")
    assert rows == []


def test_skip_backup_ignores_unparsable_filename(backup_dir: Path) -> None:
    """
    备份目录里的无关/非法文件名不被当作"刚备份过"

    口径: 只有形如 testmatrix_backup_<时间戳>.db 的文件才参与幂等判定，
    手工放置的其它文件（如 .gitkeep、临时文件）解析失败按"非备份产物"处理。
    """
    (backup_dir / "testmatrix_backup_不是时间戳.db").write_bytes(b"x")
    (backup_dir / "readme.txt").write_bytes(b"x")
    assert migration._parse_backup_timestamp("testmatrix_backup_不是时间戳.db") is None
    assert migration._parse_backup_timestamp("readme.txt") is None
    assert migration._parse_backup_timestamp("testmatrix_backup_20261006_143000.db") is not None
    assert migration._should_skip_backup(backup_dir, within_seconds=1) is False


# ---------------------------------------------------------------------------
# 三、迁移数据完整性测试
# ---------------------------------------------------------------------------


def test_migration_preserves_every_case_field(legacy_db: Path, backup_dir: Path) -> None:
    """
    迁移后存量用例逐字段不变，且 source_ref 一律补 None

    断言用"迁移前整行 + source_ref=None"与"迁移后整行"直接相等，
    任何一列被改写/丢失/重排都会让这条变红。
    """
    before = _dump_table("test_cases")
    assert len(before) == 5
    assert "source_ref" not in before[0]

    migrate_add_source_ref()
    after = _dump_table("test_cases")

    assert len(after) == len(before)
    for old_row, new_row in zip(before, after, strict=True):
        assert new_row == {**old_row, "source_ref": None}


def test_get_backup_dir_uses_default_project_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    无重定向时备份目录取 <项目根>/output/backups

    这是**生产唯一会走的分支**：其余用例都把目录重定向到 tmp_path，
    若不单独钉住，等于线上真实路径从未被测过。
    用 monkeypatch 改 PROJECT_ROOT 到 tmp_path，避免真的创建 output/backups。
    """
    monkeypatch.setattr(migration, "PROJECT_ROOT", tmp_path)
    assert migration._backup_dir_override is None
    resolved = get_backup_dir()
    assert resolved == tmp_path / "output" / "backups"
    assert resolved.is_dir()


def test_resolve_db_file_returns_none_for_memory_engine() -> None:
    """内存库无文件可备份，_resolve_db_file 返回 None（且不落任何文件）"""
    engine = create_engine("sqlite://")
    try:
        assert migration._resolve_db_file(engine) is None
    finally:
        engine.dispose()


def test_verify_integrity_raises_on_row_count_mismatch() -> None:
    """
    行数不一致必须抛 MigrationError（完整性校验要有牙齿）

    这条是整个迁移的安全网：它若恒真，"数据丢了"就会被记成"迁移成功"。
    故直接单测判据本身，而不是指望某条迁移用例恰好触发它。
    """
    with pytest.raises(MigrationError) as excinfo:
        migration._verify_integrity({"test_cases": 5}, {"test_cases": 4})
    assert "test_cases: 5 -> 4" in str(excinfo.value)


def test_verify_integrity_passes_on_matching_counts() -> None:
    """行数一致时校验通过；新建的空表不算不一致"""
    assert migration._verify_integrity({"test_cases": 5}, {"test_cases": 5}) is None
    # 迁移中新建但无数据的表：before 无该表、after 为 0，不应误报
    assert migration._verify_integrity({}, {"test_cases": 0}) is None


def test_migration_preserves_other_tables(legacy_db: Path, backup_dir: Path) -> None:
    """迁移不只保住 test_cases：执行明细与批次表数据同样完整保留"""
    executions_before = _dump_table("test_executions")
    batches_before = _dump_table("test_execution_batches")
    assert len(executions_before) == 5
    assert len(batches_before) == 1

    result = migrate_add_source_ref()

    assert _dump_table("test_executions") == executions_before
    assert _dump_table("test_execution_batches") == batches_before
    assert result["tables"]["test_executions"] == 5
    assert result["tables"]["test_execution_batches"] == 1
    assert result["added_columns"]["test_cases"] == ["source_ref"]


def test_unknown_table_in_legacy_db_survives(legacy_db: Path, backup_dir: Path) -> None:
    """
    旧库中元数据未声明的表不被重建、也不被删除（原样保留）

    实测结论（与直觉相反，故钉死）：Base.metadata.drop_all() 只 DROP 元数据
    声明过的表，元数据外的表连同其数据在迁移后**依然存在**。
    因此迁移脚本的告警口径是"该表不受本次迁移管辖"，而不是"数据将丢失"——
    这条用例同时守住行为与那句告警不让它说谎。
    """
    DatabaseSession.reset()
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("CREATE TABLE legacy_scratch (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO legacy_scratch (note) VALUES ('临时表')")
        conn.commit()

    result = migrate_add_source_ref()

    # 迁移自身数据完好，且临时表仍在（含其数据）
    assert result["rows_after"] == result["rows_before"] == 11
    assert "legacy_scratch" in _list_tables()
    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("SELECT note FROM legacy_scratch").fetchall() == [("临时表",)]


def test_dropped_column_in_legacy_db_is_discarded(legacy_db: Path, backup_dir: Path) -> None:
    """
    旧库多出的列写回时被丢弃，迁移后表结构仍与新 schema 一致

    与上一条同属"重建库会丢东西"的边界：列方向上保证迁移后
    表结构 == models.py 声明，不残留已废弃的列。
    """
    DatabaseSession.reset()
    with sqlite3.connect(legacy_db) as conn:
        conn.execute("ALTER TABLE test_cases ADD COLUMN legacy_note TEXT")
        conn.commit()

    result = migrate_add_source_ref()

    assert result["rows_after"] == result["rows_before"]
    columns = _table_columns("test_cases")
    assert "legacy_note" not in columns
    assert "source_ref" in columns


def test_migration_reports_new_column_added(legacy_db: Path, backup_dir: Path) -> None:
    """迁移结果显式报告新增列，便于核对当日到底改了哪张表的哪一列"""
    result = migrate_add_source_ref()
    assert result["status"] == migration.STATUS_MIGRATED
    assert result["db_type"] == "sqlite"
    assert result["added_columns"] == {"test_cases": ["source_ref"]}
    assert result["rows_before"] == result["rows_after"]
    assert result["elapsed_seconds"] >= 0


def test_migration_is_idempotent(legacy_db: Path, backup_dir: Path) -> None:
    """连续执行两次迁移，第二次不报错、数据不丢、不重复备份"""
    migrate_add_source_ref()
    snapshot = _dump_table("test_cases")

    migrate_add_source_ref()

    assert _dump_table("test_cases") == snapshot
    assert len(_list_backups(backup_dir)) == 1
    assert len(snapshot) == 5


def test_source_ref_survives_second_migration(legacy_db: Path, backup_dir: Path) -> None:
    """已回填的 source_ref 在再次迁移时不会被清空（否则 Day47 回填会被冲掉）"""
    migrate_add_source_ref()
    # 模拟 Day47 回填结果
    with DatabaseSession.session_scope() as session:
        case = session.query(CaseModel).filter_by(case_id="TM-LEGACY-0001").first()
        assert case is not None
        case.source_ref = "tests/api_demo/test_api_login.py::test_user_login"

    migrate_add_source_ref()

    rows = {row["case_id"]: row["source_ref"] for row in _dump_table("test_cases")}
    assert rows["TM-LEGACY-0001"] == "tests/api_demo/test_api_login.py::test_user_login"
    assert rows["TM-LEGACY-0002"] is None


# ---------------------------------------------------------------------------
# 四、迁移异常与后端分支
# ---------------------------------------------------------------------------


def test_migration_failure_restores_from_backup(
    legacy_db: Path, backup_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    迁移中途失败触发从备份回滚，不留半迁移状态

    故障注入点在"重建后写回"这一步（最容易写出错的那一步）：
    回滚后必须同时满足"旧表结构仍在（无 source_ref）+ 数据条数不变"，
    只满足其中一条都可能是假绿（例如只删了半张表又被重新建空）。
    """
    before = _dump_table("test_cases")

    def _boom(engine: Any, snapshots: Any) -> Any:
        raise RuntimeError("注入的写回故障")

    monkeypatch.setattr(migration, "_write_back", _boom)

    with pytest.raises(MigrationError) as excinfo:
        migrate_add_source_ref()

    assert "注入的写回故障" in str(excinfo.value)
    # 回滚后回到迁移前：结构无 source_ref + 5 条用例逐字段一致
    restored = _dump_table("test_cases")
    assert "source_ref" not in restored[0]
    assert restored == before


def test_mysql_mode_skips_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_dir: Path
) -> None:
    """
    MySQL 模式打印警告并跳过重建（Day68 前不 DROP 生产表）

    create_engine 是惰性连接，此处只构造引擎、不会真连 MySQL。
    """
    monkeypatch.setenv("TM_DB_TYPE", "mysql")
    monkeypatch.setenv("TM_DB_MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("TM_DB_MYSQL_PORT", "3306")
    monkeypatch.setenv("TM_DB_MYSQL_USER", "root")
    monkeypatch.setenv("TM_DB_MYSQL_PASSWORD", "")
    monkeypatch.setenv("TM_DB_MYSQL_DATABASE", "testmatrix")
    DatabaseSession.reset()
    try:
        result = migrate_add_source_ref()
    finally:
        DatabaseSession.reset()

    assert result["status"] == migration.STATUS_UNSUPPORTED
    assert result["db_type"] == "mysql"
    assert result["backup_created"] is False
    assert result["tables"] == {}
    assert _list_backups(backup_dir) == []
