"""
ORM 模型索引存在性测试（Day44 M3）

测试范围:
    1. DefectStatistic.created_at 新增索引 idx_ds_created_at 存在且列正确
       （批次列表分页与趋势图默认排序键，防数据量增长后全表扫描）；
    2. 其余五张表的既有索引全部保留（加索引改动的回归保护，防误删）；
    3. DefectStatistic.execution_id 唯一约束仍在（唯一业务键不被索引改动波及）。

隔离方式:
    使用独立的内存 SQLite（sqlite:///:memory:）+ Base.metadata.create_all
    建表，不依赖 DatabaseSession 全局配置与 dev.db 文件，无真实数据库连接，
    引擎在用例结束后 dispose 释放。
"""

from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine, Inspector
from src.db.models import Base


@pytest.fixture()
def inspector() -> Generator[Inspector, None, None]:
    """建内存库表并返回 SQLAlchemy Inspector，用例后销毁引擎。

    返回:
        Inspector: 已完成全量建表的内存库结构查看器
    """
    # 每用例独立内存库，create_all 按模型 __table_args__ 建索引
    engine: Engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield inspect(engine)
    engine.dispose()


def test_defect_statistics_created_at索引存在且指向created_at列(
    inspector: Inspector,
) -> None:
    """新增索引 idx_ds_created_at 必须存在，且唯一作用于 created_at 列。"""
    indexes = inspector.get_indexes("defect_statistics")
    target = next(
        (idx for idx in indexes if idx["name"] == "idx_ds_created_at"),
        None,
    )
    # 索引存在
    assert target is not None, "defect_statistics 缺少 idx_ds_created_at 索引"
    # 单列索引且列名精确为 created_at
    assert target["column_names"] == ["created_at"]


@pytest.mark.parametrize(
    ("table_name", "index_name"),
    [
        ("test_cases", "idx_module_status"),
        ("test_executions", "idx_execution_result"),
        ("test_execution_batches", "idx_teb_status"),
        ("notification_dead_letters", "idx_dl_execution"),
        ("notification_history", "idx_nh_channel_status"),
    ],
)
def test_既有表索引全部保留(
    inspector: Inspector, table_name: str, index_name: str
) -> None:
    """五张表的既有索引必须全部保留（新增索引改动的回归保护）。"""
    index_names = {idx["name"] for idx in inspector.get_indexes(table_name)}
    assert index_name in index_names, (
        f"{table_name} 的既有索引 {index_name} 丢失，实际索引：{index_names}"
    )


def test_defect_statistics_execution_id唯一约束保留(
    inspector: Inspector,
) -> None:
    """execution_id 唯一约束必须保留（批次号业务唯一键不受索引改动影响）。"""
    unique_constraints = inspector.get_unique_constraints("defect_statistics")
    constrained_columns = {
        tuple(uc["column_names"]) for uc in unique_constraints
    }
    assert ("execution_id",) in constrained_columns
