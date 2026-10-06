"""
source_ref 字段数据库迁移模块（Day46）

功能:
    - get_backup_dir / backup_dir_override: 备份目录定位与临时重定向
    - _should_skip_backup / _backup_database: 迁移前自动备份（含幂等跳过）
    - _read_snapshot / _write_back / _verify_integrity / _restore_from_backup
    - migrate_add_source_ref: 迁移入口（重建库方案）

方案选择（为何是"重建库"而非 ALTER）:
    当前阶段 SQLite 为主、数据量小（种子库仅数条用例），重建库能拿到
    **与 create_all 完全一致的新表结构**，而 ALTER ADD COLUMN 逐个追加
    历史遗留的类型/约束会与 models.py 声明漂移。故 Day46-67 走重建库，
    Day68 切 Alembic 后改用轻量 ALTER 迁移。

数据往返保真的实现口径:
    读写两侧**都用裸值进出**（读走 SQLAlchemy 核心 select、结果取 mappings，
    写走 text() 显式列名 INSERT），不做任何 Python 层类型转换。
    这样"读出的 datetime 字符串"能原样写回同一个 DATETIME 列，
    若读侧做了 str→datetime 转换而写侧未做（或反之），就会引入
    只有部分表触发的隐性类型漂移（与 7.40 同源教训）。
    新增列（source_ref）不在旧库中，由写回时显式补 None。

MySQL 双模式:
    Day68 前不执行 MySQL 重建（重建 = DROP 全部表，生产数据不可承受），
    migrate_add_source_ref() 在 MySQL 模式下仅打印警告并返回，
    待 Alembic 的 ALTER 方案落地后再补。
"""

import shutil
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy import Engine, Table, inspect, text

from src.common.logger import LogManager
from src.db.db_session import PROJECT_ROOT, DatabaseSession
from src.db.models import Base

logger = LogManager.get_logger()

# 备份文件名模板：testmatrix_backup_YYYYmmdd_HHMMSS.db
BACKUP_FILE_PREFIX = "testmatrix_backup_"
BACKUP_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
BACKUP_SUFFIX = ".db"

# 幂等窗口：该窗口内已有备份则跳过备份步骤（仍执行数据校验）
BACKUP_RECENT_WINDOW_SECONDS = 3600

# 迁移结果 status 取值
STATUS_MIGRATED = "migrated"
STATUS_UNSUPPORTED = "unsupported_backend"


class MigrationError(RuntimeError):
    """
    数据迁移失败异常

    迁移过程任何异常（含数据完整性校验不通过）均以本异常类型上抛，
    且上抛前已完成从备份回滚，调用方无需再判断是否需要恢复。
    """


@dataclass
class TableSnapshot:
    """
    单表迁移前的数据快照

    属性:
        name (str): 表名
        columns (list[str]): 旧库该表**实际存在**的列名（决定写回时哪些列可写）
        rows (list[dict[str, Any]]): 旧库全部行（裸值，与写回类型对称）
    """

    name: str
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)


# 备份目录重定向值（None 表示用默认 output/backups/）
_backup_dir_override: Path | None = None


@contextmanager
def backup_dir_override(directory: str | Path) -> Iterator[Path]:
    """
    临时重定向备份目录（测试隔离用，退出时恢复原值）

    测试必须用本上下文把备份写到 tmp_path，否则迁移测试会在真实
    output/backups/ 下留下垃圾文件污染开发库产物目录。

    参数:
        directory (str | Path): 备份目录（不存在时由 get_backup_dir 自动创建）

    返回:
        Iterator[Path]: yield 生效后的备份目录 Path

    异常:
        无（目录创建异常由 get_backup_dir 抛出）
    """
    global _backup_dir_override
    previous = _backup_dir_override
    _backup_dir_override = Path(directory)
    try:
        yield _backup_dir_override
    finally:
        # 还原而非置 None：用例失败也要复位，否则污染同进程后续用例
        _backup_dir_override = previous


def get_backup_dir() -> Path:
    """
    获取备份目录并确保其存在

    参数:
        无

    返回:
        Path: 备份目录路径（已 mkdir），默认 <项目根>/output/backups

    异常:
        OSError: 目录不可创建时抛出（权限不足等）
    """
    directory = _backup_dir_override
    if directory is None:
        directory = PROJECT_ROOT / "output" / "backups"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _current_backend(engine: Engine) -> str:
    """
    判定当前数据库后端类型

    以**引擎实际 URL** 为准而非重新读环境变量：环境变量只是配置的输入，
    引擎 URL 才是真正生效的事实来源（与 6.45 同一"单一事实来源"取向）。

    参数:
        engine (Engine): SQLAlchemy 引擎

    返回:
        str: "sqlite" / "mysql" / 原始 drivername
    """
    drivername = engine.url.drivername
    if drivername.startswith("sqlite"):
        return "sqlite"
    if drivername.startswith("mysql"):
        return "mysql"
    return drivername


def _resolve_db_file(engine: Engine) -> Path | None:
    """
    解析 SQLite 数据库文件路径

    参数:
        engine (Engine): SQLAlchemy 引擎（仅 SQLite 分支调用）

    返回:
        Path | None: 数据库文件路径；内存库或非 SQLite 返回 None
                      （内存库无文件可备份，重建仍在同一连接内完成）
    """
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database)


def _parse_backup_timestamp(file_name: str) -> datetime | None:
    """
    从备份文件名解析时间戳

    参数:
        file_name (str): 形如 testmatrix_backup_20261006_143000.db 的文件名

    返回:
        datetime | None: 解析出的时间戳；文件名不含时间戳段或格式非法返回 None
    """
    if not file_name.startswith(BACKUP_FILE_PREFIX) or not file_name.endswith(BACKUP_SUFFIX):
        return None
    stamp = file_name[len(BACKUP_FILE_PREFIX) : -len(BACKUP_SUFFIX)]
    try:
        return datetime.strptime(stamp, BACKUP_TIMESTAMP_FORMAT)
    except ValueError:
        # 目录里可能有手工放置的其他文件，解析失败按"非本次备份产物"处理
        return None


def _should_skip_backup(
    directory: Path, within_seconds: int = BACKUP_RECENT_WINDOW_SECONDS
) -> bool:
    """
    判定是否应跳过备份（幂等设计）

    判据：备份目录中已存在时间戳落在最近 within_seconds 秒内的备份文件，
    视为刚刚完成过同一次迁移，跳过重复备份（重复执行迁移时避免备份目录
    被同秒文件刷屏）。

    参数:
        directory (Path): 备份目录
        within_seconds (int): 幂等窗口秒数，默认 3600（1 小时）

    返回:
        bool: True=应跳过备份；False=需要备份
    """
    cutoff = datetime.now() - timedelta(seconds=within_seconds)
    for path in directory.glob(f"{BACKUP_FILE_PREFIX}*{BACKUP_SUFFIX}"):
        stamp = _parse_backup_timestamp(path.name)
        if stamp is not None and stamp >= cutoff:
            logger.info(f"备份幂等跳过 | {within_seconds}s 内已有备份: {path.name}")
            return True
    return False


def _is_usable_backup(path: Path) -> bool:
    """
    校验备份文件是否为"可用的"SQLite 备份

    两道判据:
        1. 能以**只读**方式打开并读到文件头（mode=ro，避免校验动作本身改动文件）
        2. 至少含一张表——只满足第 1 条的空库能打开但无表，
           拿它回滚等于把"库被清空"从一个失败现场换成另一个失败现场

    参数:
        path (Path): 备份文件路径

    返回:
        bool: True=可用备份；False=损坏/空库/不可读

    异常:
        无（sqlite3.Error 与 ValueError 全部内部消化为 False）
    """
    try:
        # as_uri() 产出 file:///C:/... 形式，避免 Windows 反斜杠破坏 URI 语法
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            table_count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
            ).fetchone()
        return bool(table_count and table_count[0] > 0)
    except (sqlite3.Error, ValueError, OSError):
        # 损坏文件/无 SQLite 文件头/空库/路径异常：按"不可用"处理，不让恢复流程崩
        return False


def _find_latest_backup(backup_dir: Path) -> Path | None:
    """
    找出备份目录中最新的**可用**备份（迁移失败但本次无备份时的回退恢复源）

    为什么需要它: 幂等窗口内重复迁移会跳过备份（backup_path 为 None），
    此时若写回失败，原实现不回滚 → 整库被清空。这是 P1 数据丢失路径。

    参数:
        backup_dir (Path): 备份目录

    返回:
        Path | None: 最新一份可用备份的路径；无任何可用备份返回 None

    异常:
        无（文件名时间戳解析失败与文件损坏均跳过并记 warning）
    """
    candidates: list[tuple[datetime, Path]] = []
    for path in backup_dir.glob(f"{BACKUP_FILE_PREFIX}*{BACKUP_SUFFIX}"):
        stamp = _parse_backup_timestamp(path.name)
        if stamp is None:
            logger.warning(f"备份文件名时间戳无法解析，跳过 | 文件: {path.name}")
            continue
        candidates.append((stamp, path))

    # 时间戳从新到旧逐个尝试：最新的损坏时自动回退到次新的可用备份
    for _stamp, path in sorted(candidates, key=lambda item: item[0], reverse=True):
        if _is_usable_backup(path):
            return path
        logger.warning(f"备份文件损坏或无有效表，跳过 | 文件: {path.name}")
    return None


def _backup_database(db_file: Path, directory: Path) -> Path:
    """
    备份 SQLite 数据库文件到备份目录

    参数:
        db_file (Path): 待备份的数据库文件
        directory (Path): 备份目录（须已存在）

    返回:
        Path: 备份文件路径

    异常:
        OSError: 复制失败（磁盘满/文件被占用）时抛出
    """
    stamp = datetime.now().strftime(BACKUP_TIMESTAMP_FORMAT)
    target = directory / f"{BACKUP_FILE_PREFIX}{stamp}{BACKUP_SUFFIX}"
    # copy2 保留 mtime/权限，便于事后判断备份来源
    shutil.copy2(db_file, target)
    logger.info(f"数据库已备份 | 源: {db_file.name} | 备份: {target.name}")
    return target


def _read_snapshot(engine: Engine) -> list[TableSnapshot]:
    """
    读取迁移前各表的数据快照

    只读取 Base.metadata 声明过的表；旧库中存在但元数据未声明的表会被
    drop_all 连带删除，故必须显式告警而不是静默丢数据。

    参数:
        engine (Engine): SQLAlchemy 引擎

    返回:
        list[TableSnapshot]: 各表快照（库中不存在的表不会出现在结果里）

    异常:
        SQLAlchemyError: 查询失败时抛出
    """
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    known = set(Base.metadata.tables)

    unknown = sorted(existing - known)
    if unknown:
        # 准确口径：drop_all 只 DROP 元数据声明过的表，元数据外的表**原样保留**
        # （实测 SQLite 下 legacy_scratch 迁移后仍在库里，数据未丢）。
        # 所以这里要说清的是"不受本次迁移管辖"，而不是"数据将丢失"——
        # 日志若承诺一个代码不兑现的后果，排障时会被当成事实依据（7.47）。
        logger.warning(
            f"旧库存在元数据未声明的表，本次迁移不重建也不写回（表与其数据原样保留）"
            f"，且不会随 models.py 演进 | 表: {', '.join(unknown)}"
        )

    snapshots: list[TableSnapshot] = []
    for name in sorted(existing & known):
        # 必须用真正的 SELECT *，不能用 select(table)：
        # 后者按**元数据声明**投影列，迁移前库里根本没有 source_ref 这一列，
        # 会直接报 "no such column: test_cases.source_ref"——迁移读的就是
        # 旧结构，用新结构去读等于迁移自己把自己挡在门外。
        # 表名取自 Base.metadata 的键（项目内常量，非用户输入），故内插安全。
        with engine.connect() as conn:
            rows = [dict(m) for m in conn.execute(text(f"SELECT * FROM {name}")).mappings().all()]
        columns = [str(col["name"]) for col in inspector.get_columns(name)]
        snapshots.append(TableSnapshot(name=name, columns=columns, rows=rows))
    return snapshots


def _build_insert_rows(snapshot: TableSnapshot, table: Table) -> list[dict[str, Any]]:
    """
    把旧库行转换为适配新表结构的待写入行

    列口径:
        - 新表有、旧表也有的列 → 原样保留旧值
        - 新表有、旧表没有的列（如 source_ref）→ 显式补 None
        - 旧表有、新表没有的列 → 丢弃（打 WARNING 说明，避免静默丢字段）

    参数:
        snapshot (TableSnapshot): 旧表快照
        table (Table): 新表定义

    返回:
        list[dict[str, Any]]: 待写入行列表
    """
    old_columns = set(snapshot.columns)
    dropped = sorted(old_columns - {col.name for col in table.columns})
    if dropped:
        logger.warning(
            f"表 {snapshot.name} 存在新结构已移除的列，写回时丢弃 | 列: {', '.join(dropped)}"
        )
    added = [col.name for col in table.columns if col.name not in old_columns]
    if added:
        logger.info(f"表 {snapshot.name} 新增列写入 None | 列: {', '.join(added)}")

    return [
        {col.name: row.get(col.name) for col in table.columns}
        for row in snapshot.rows
    ]


def _write_back(engine: Engine, snapshots: Sequence[TableSnapshot]) -> dict[str, int]:
    """
    把快照数据写回重建后的新库

    参数:
        engine (Engine): SQLAlchemy 引擎（须已建好新表结构）
        snapshots (Sequence[TableSnapshot]): 各表迁移前快照

    返回:
        dict[str, int]: 表名 → 实际写入行数

    异常:
        SQLAlchemyError: 写入失败时抛出（由上层触发回滚）
    """
    written: dict[str, int] = {}
    with engine.begin() as conn:
        for snapshot in snapshots:
            table = Base.metadata.tables[snapshot.name]
            rows = _build_insert_rows(snapshot, table)
            if rows:
                # 列名取自元数据常量（非用户输入），此处内插是安全的；
                # 用 text() 而非 Core insert 是为保持"裸值进出"，见模块 docstring
                column_list = ", ".join(col.name for col in table.columns)
                placeholders = ", ".join(f":{col.name}" for col in table.columns)
                statement = text(
                    f"INSERT INTO {table.name} ({column_list}) VALUES ({placeholders})"
                )
                conn.execute(statement, rows)
            written[snapshot.name] = len(rows)
    return written


def _count_rows(engine: Engine) -> dict[str, int]:
    """
    统计各表当前行数（迁移后校验用）

    参数:
        engine (Engine): SQLAlchemy 引擎

    返回:
        dict[str, int]: 表名 → 行数
    """
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            # 表名同样来自元数据常量，非用户输入
            result = conn.execute(text(f"SELECT COUNT(*) FROM {table.name}"))
            counts[table.name] = int(result.scalar_one())
    return counts


def _verify_integrity(before: dict[str, int], after: dict[str, int]) -> None:
    """
    校验迁移前后各表行数一致

    参数:
        before (dict[str, int]): 迁移前行数
        after (dict[str, int]): 迁移后行数

    返回:
        None

    异常:
        MigrationError: 任一表行数不一致时抛出
    """
    mismatched = {
        name: (before.get(name, 0), after.get(name, 0))
        for name in set(before) | set(after)
        if before.get(name, 0) != after.get(name, 0)
    }
    if mismatched:
        detail = ", ".join(
            f"{name}: {old} -> {new}" for name, (old, new) in sorted(mismatched.items())
        )
        raise MigrationError(f"数据完整性校验未通过，行数不一致 | {detail}")


def _restore_from_backup(backup_path: Path, db_file: Path) -> None:
    """
    从备份恢复数据库文件（迁移失败时回滚）

    必须先释放引擎连接再覆盖文件，否则 Windows 下会因文件锁导致
    恢复失败、把"可回滚"变成"不可回滚"（7.2 同类坑）。

    参数:
        backup_path (Path): 备份文件路径
        db_file (Path): 待恢复的数据库文件路径

    返回:
        None

    异常:
        OSError: 恢复失败时抛出（覆盖原异常，由上层记录）
    """
    DatabaseSession.reset()
    shutil.copy2(backup_path, db_file)
    logger.warning(f"已从备份恢复数据库 | 备份: {backup_path.name}")


def _restore_outcome(backup: Path, db_file: Path, label: str) -> str:
    """
    执行恢复并如实描述结果（恢复本身失败也要说出来）

    参数:
        backup (Path): 恢复源备份文件
        db_file (Path): 待恢复的数据库文件
        label (str): 恢复来源标签（"本次备份"/"最近历史备份"），用于消息措辞

    返回:
        str: 描述真实恢复结果的中文片段

    异常:
        无（恢复失败也转成描述文本，不掩盖原始迁移异常）
    """
    try:
        _restore_from_backup(backup, db_file)
    except OSError as restore_exc:
        # 恢复本身失败：必须说出来，否则上层会以为已回滚而放过一个空库
        return f"从{label}（{backup.name}）恢复失败: {restore_exc}"
    return f"已从{label}（{backup.name}）恢复"


def _recover_after_failure(
    exc: Exception, backup_path: Path | None, db_file: Path | None
) -> str:
    """
    迁移失败后的恢复与消息生成（Day46-fix P1-1）

    恢复源优先级: 本次备份 > 备份目录中最新的可用备份 > 无（需人工恢复）。

    **不再使用"已保持或恢复迁移前状态"这类无法验证的措辞**——
    幂等窗口内跳过备份时 backup_path 为 None，若不显式回退到历史备份，
    库已被 DROP 重建为空，消息却声称"已恢复"，排障会被直接误导（7.47）。

    参数:
        exc (Exception): 触发迁移失败的原始异常
        backup_path (Path | None): 本次迁移生成的备份（跳过备份时为 None）
        db_file (Path | None): 数据库文件路径（内存库为 None）

    返回:
        str: 与实际恢复行为一致的中文消息

    异常:
        无（所有恢复失败均转为描述文本）
    """
    prefix = f"source_ref 字段迁移失败: {exc}"
    if db_file is None:
        return f"{prefix}；当前为 SQLite 内存库，无文件可恢复，需从其它来源重建"
    if backup_path is not None:
        return f"{prefix}；{_restore_outcome(backup_path, db_file, '本次备份')}"
    latest = _find_latest_backup(get_backup_dir())
    if latest is None:
        return (
            f"{prefix}；无可用备份，库结构已重建为空，"
            f"请从 {get_backup_dir()} 手工恢复最近备份"
        )
    return (
        f"{prefix}；{_restore_outcome(latest, db_file, '最近历史备份')}，"
        f"注意：可能丢失该备份之后、迁移之前的增量数据"
    )


def migrate_add_source_ref() -> dict[str, Any]:
    """
    执行 source_ref 字段迁移（重建库方案）

    迁移流程:
        1. 判定后端；MySQL 直接告警返回（Day68 前不做生产重建）
        2. 读取旧库全部数据快照
        3. 备份（空库无数据可备份、幂等窗口内已有备份时跳过）
        4. drop_all + create_all 重建为新表结构
        5. 写回数据（source_ref 等新增列补 None，其余列原样写回）
        6. 校验各表行数；不一致则从备份回滚并抛 MigrationError

    参数:
        无

    返回:
        dict[str, Any]: 迁移结果，含 status / db_type / backup_path /
                        backup_created / tables / rows_before / rows_after /
                        added_columns / elapsed_seconds

    异常:
        MigrationError: 迁移或完整性校验失败。异常消息**如实描述**恢复结果
            （本次备份 / 最近历史备份 / 无可用备份需人工恢复），
            不保证一定已回滚——无任何可用备份时库已被重建为空
        SQLAlchemyError: 数据库不可达等底层异常（同样先走恢复流程）
    """
    started_at = perf_counter()
    engine = DatabaseSession.get_engine()
    backend = _current_backend(engine)

    if backend != "sqlite":
        # MySQL 重建等于 DROP 全部表，生产数据不可承受；
        # Day68 接 Alembic 后改走 ALTER TABLE ADD COLUMN
        logger.warning(
            f"当前为 {backend} 模式，source_ref 迁移暂不执行，"
            f"请等待 Day68 的 Alembic ALTER 迁移方案"
        )
        return {
            "status": STATUS_UNSUPPORTED,
            "db_type": backend,
            "backup_path": None,
            "backup_created": False,
            "tables": {},
            "rows_before": 0,
            "rows_after": 0,
            "added_columns": {},
            "elapsed_seconds": round(perf_counter() - started_at, 4),
        }

    db_file = _resolve_db_file(engine)
    snapshots = _read_snapshot(engine)
    before = {snapshot.name: len(snapshot.rows) for snapshot in snapshots}
    total_rows = sum(before.values())

    backup_path: Path | None = None
    backup_created = False
    if db_file is None:
        logger.warning("SQLite 内存库无文件可备份，重建在当前连接内完成")
    elif total_rows == 0:
        logger.info("旧库无数据，跳过备份步骤")
    elif _should_skip_backup(get_backup_dir()):
        logger.info("命中备份幂等窗口，跳过备份步骤（仍执行数据校验）")
    else:
        backup_path = _backup_database(db_file, get_backup_dir())
        backup_created = True

    # 只收录"确实新增了列"的表：全表都返回空列表会让报告失去可读性，
    # 而这份结果的正用途就是回答"本次迁移到底给哪张表加了哪一列"
    added_columns: dict[str, list[str]] = {}
    for snapshot in snapshots:
        table = Base.metadata.tables[snapshot.name]
        new_columns = [
            col.name for col in table.columns if col.name not in set(snapshot.columns)
        ]
        if new_columns:
            added_columns[snapshot.name] = new_columns

    try:
        # 释放读阶段连接后再 DROP（Windows 文件锁）
        DatabaseSession.reset()
        fresh_engine = DatabaseSession.get_engine()
        Base.metadata.drop_all(fresh_engine)
        Base.metadata.create_all(fresh_engine)
        _write_back(fresh_engine, snapshots)
        after = _count_rows(fresh_engine)
        _verify_integrity(before, after)
    except Exception as exc:
        # Day46-fix P1-1: 回滚源不能只有"本次备份"——幂等窗口内跳过备份时
        # backup_path 为 None，原实现直接不回滚，整库已被 DROP 成空库
        raise MigrationError(_recover_after_failure(exc, backup_path, db_file)) from exc

    elapsed = round(perf_counter() - started_at, 4)
    logger.info(
        f"source_ref 字段迁移完成 | 备份: {backup_path.name if backup_path else '无'} | "
        f"行数: {total_rows} | 耗时: {elapsed}s"
    )
    return {
        "status": STATUS_MIGRATED,
        "db_type": backend,
        "backup_path": str(backup_path) if backup_path else None,
        "backup_created": backup_created,
        "tables": after,
        "rows_before": total_rows,
        "rows_after": sum(after.values()),
        "added_columns": added_columns,
        "elapsed_seconds": elapsed,
    }
