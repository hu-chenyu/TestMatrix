"""
时间序列化工具（UTC 标注 + 本地时间转换）

解决的问题:
    本项目的时间列有**两种来源**，但都存成 naive datetime，序列化时
    统一 .isoformat() 不带时区标识，前端 new Date() 按 ES2015 规范把
    "无时区字符串"当**本地时间**解析，导致显示时间整体偏移。

两种来源（务必区分，选错方向会让时间往反方向偏）:
    1. UTC 来源 —— server_default=func.now() / onupdate=func.now()。
       SQLite 的 CURRENT_TIMESTAMP 明确返回 **UTC**（见 SQLite 文档）。
       涉及字段: TestCase.created_at / updated_at、TestExecution.created_at、
       TestExecutionBatch.created_at、DefectStatistic.created_at、
       NotificationHistory.created_at、NotificationDeadLetter.created_at。
       → 用 to_utc_iso：直接**补** +00:00 标识，不动数值。
    2. 本地来源 —— Python 侧 datetime.now() 显式写入。
       涉及字段: TestExecution.start_time / end_time、
       TestExecutionBatch.started_at / finished_at。
       → 用 local_to_utc_iso：把本地时间**换算**成 UTC（数值会减 8 小时）。

    实测佐证（dev.db 同一批次）: created_at=06:03:53（UTC）、
    started_at=14:03:53（本地），恰好差 8 小时。

为什么不用"一律 replace(tzinfo=utc)":
    那会把本地来源的时间谎称成 UTC，前端再减 8 小时——**把本来就正确
    的显示改错，且错在反方向**。两种来源必须用两个函数。

不做的事:
    - 不改数据库 schema: SQLite 不存 timezone-aware datetime
    - 不改写入侧: func.now() 与 datetime.now() 的写入方式保持原样，
      本模块只管输出侧的时区标注
    - 不改前端: 拿到带 +00:00 的 ISO 串后 new Date() 会正确解析为 UTC，
      再用 getHours() 等本地化方法呈现，无需前端配合
"""

from datetime import UTC, datetime


def to_utc_iso(dt: datetime | None) -> str | None:
    """
    UTC 来源的 naive datetime 序列化为带 UTC 标识的 ISO 字符串

    背景: SQLite CURRENT_TIMESTAMP(func.now()) 返回 UTC，存入模型后是
    naive datetime。直接 .isoformat() 得到不带时区的字符串，前端
    new Date() 会误判为本地时间，显示时间比实际少一个时区偏移
    （UTC+8 用户少 8 小时）。

    参数:
        dt (datetime | None): 数据库读出的 naive datetime（语义为 UTC）；
            None（如尚未写入的可空时间列）原样返回 None

    返回:
        str | None: 带 +00:00 时区标识的 ISO 字符串；入参 None 时返回 None

    异常:
        无
    """
    if dt is None:
        return None
    # 已是 aware datetime 时 replace 会保留其原时区，不做转换（防御）
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC).isoformat()
    return dt.isoformat()


def local_to_utc_iso(dt: datetime | None) -> str | None:
    """
    本地来源的 naive datetime 换算为 UTC 后序列化为带标识的 ISO 字符串

    背景: 执行链路的 started_at / finished_at / start_time / end_time
    由 Python 的 datetime.now() 写入，存的是**本地时间**。它们与
    server_default 写入的 UTC 时间混在同一批接口响应里，若一律按 UTC
    标注，前端会把本地时间当 UTC 再减一次偏移，误差方向相反。

    参数:
        dt (datetime | None): 数据库读出的 naive datetime（语义为本地时间）；
            None 原样返回 None

    返回:
        str | None: 换算为 UTC 的 ISO 字符串（数值比原值少一个时区偏移）；
            入参 None 时返回 None

    异常:
        无
    """
    if dt is None:
        return None
    # naive datetime 传 astimezone 时按"本地时区"解释，等价于正确换算
    return dt.astimezone(UTC).isoformat()
