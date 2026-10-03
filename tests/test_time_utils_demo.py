"""
全局时区修复测试（Day44 后半段）

回归点: 本项目时间列有**两种来源**但都存成 naive datetime，序列化时统一
.isoformat() 不带时区标识，前端 new Date() 按 ES2015 规范把"无时区字符串"
当本地时间解析，导致显示整体偏移一个时区：
    1. UTC 来源（server_default=func.now()，SQLite CURRENT_TIMESTAMP 返回
       UTC）: TestCase/TestExecution/TestExecutionBatch/DefectStatistic/
       NotificationHistory 的 created_at 与 updated_at
    2. 本地来源（Python datetime.now() 显式写入）: TestExecution 的
       start_time/end_time、TestExecutionBatch 的 started_at/finished_at

**两类来源必须用不同函数**：一律 replace(tzinfo=utc) 会把本地时间谎称成
UTC，前端再减一次偏移，把本来正确的显示改错且错在反方向。测试用
"同一墙钟值经两个函数得到不同绝对时刻"这条性质锁死该区分。

设计原则:
    - 纯函数测试不依赖运行机器的本地时区（断言用绝对时刻/偏移量，
      不用写死 "+00:00" 之外的具体换算结果）
    - 集成测试经核心层真实查库（tmp_path 临时库 + reset），零 mock，
      验证 API 序列化产物带时区标识
"""

from datetime import datetime, timedelta, timezone

import allure
import pytest
from src.common.time_utils import local_to_utc_iso, to_utc_iso
from src.core.case_manager import CaseManager
from src.db.db_session import DatabaseSession
from src.db.models import DefectStatistic

# 别名导入：模型类名以 Test 开头，直接 import 会被 pytest 的类名启发式
# 当成测试类收集并抛 PytestCollectionWarning（验收要求 0 warning）
from src.db.models import TestExecutionBatch as ExecutionBatchModel

# 固定墙钟值（语义为 UTC 的那一类来源，2026-10-03 06:15:35 UTC）
FIXED_NAIVE = datetime(2026, 10, 3, 6, 15, 35)


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """
    临时 SQLite 库 fixture（不污染 output/testmatrix.db 正式库）

    参数:
        tmp_path: pytest 临时目录 fixture
        monkeypatch: 环境变量覆写 fixture

    返回:
        Iterator[None]: 完成建表即 yield，结束后重置引擎单例
    """
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "tz_utils.db"))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield
    DatabaseSession.reset()


# ===========================================================================
# 1. 纯函数行为
# ===========================================================================
@allure.feature("时间序列化UTC标注")
class TestToUtcIso:
    """to_utc_iso: UTC 来源 naive datetime 补时区标识"""

    @allure.story("None 原样返回 None（可空时间列）")
    def test_none_returns_none(self):
        assert to_utc_iso(None) is None

    @allure.story("naive datetime 补 +00:00 且墙钟值不变")
    def test_naive_gets_utc_marker(self):
        result = to_utc_iso(FIXED_NAIVE)
        assert result == "2026-10-03T06:15:35+00:00"
        parsed = datetime.fromisoformat(result)
        assert parsed.utcoffset() == timedelta(0)
        # 补标识不改动墙钟数值（这正是"UTC 来源"的语义）
        assert parsed.replace(tzinfo=None) == FIXED_NAIVE

    @allure.story("带时区的 aware datetime 不被二次改写")
    def test_aware_passthrough(self):
        aware = FIXED_NAIVE.replace(tzinfo=timezone(timedelta(hours=8)))
        result = to_utc_iso(aware)
        assert result == "2026-10-03T06:15:35+08:00", (
            "已是 aware 的 datetime 必须保留其原时区，不能被强行标成 UTC"
        )


@allure.feature("本地时间转UTC")
class TestLocalToUtcIso:
    """local_to_utc_iso: 本地来源 naive datetime 换算成 UTC"""

    @allure.story("None 原样返回 None")
    def test_none_returns_none(self):
        assert local_to_utc_iso(None) is None

    @allure.story("换算后与把原值当本地解释的绝对时刻一致")
    def test_converts_local_to_utc(self):
        result = local_to_utc_iso(FIXED_NAIVE)
        assert result is not None
        parsed = datetime.fromisoformat(result)
        assert parsed.utcoffset() == timedelta(0), "换算结果必须带 UTC 标识"
        # 关键性质：把结果换回本地时，墙钟值应与入参完全相同
        assert parsed.astimezone().replace(tzinfo=None) == FIXED_NAIVE

    @allure.story("两个函数对同一墙钟值给出不同绝对时刻（锁死来源区分）")
    def test_two_helpers_differ(self):
        """
        这条是防回归核心：若有人把 local_to_utc_iso 误写成
        replace(tzinfo=utc)，两者在非 UTC 机器上就完全等价，
        "本地时间被谎称成 UTC"的缺陷会静默回归。
        """
        as_utc = datetime.fromisoformat(to_utc_iso(FIXED_NAIVE))
        as_local = datetime.fromisoformat(local_to_utc_iso(FIXED_NAIVE))
        offset = datetime.now().astimezone().utcoffset() or timedelta(0)
        if offset != timedelta(0):
            assert as_utc != as_local, (
                "本地时区非 UTC 时，两个函数必须给出不同的绝对时刻"
            )
        else:
            # 运行机器恰在 UTC：两者本就等价，但至少都要带 UTC 标识
            assert as_utc.utcoffset() == as_local.utcoffset() == timedelta(0)


# ===========================================================================
# 2. 核心层集成：API 序列化产物必须带时区标识
# ===========================================================================
@allure.feature("时间序列化集成")
class TestApiPayloadTimezone:
    """核心层查询结果中的时间字段必须带 +00:00"""

    @staticmethod
    def _seed() -> str:
        """
        造一个批次，两个时间字段写入**同一墙钟值**但语义不同：
        created_at 显式写 FIXED_NAIVE（代表 UTC 来源，与 server_default
        func.now() 写入的值同语义），started_at/finished_at 显式写同一
        墙钟值（代表 datetime.now() 的本地来源）。
        """
        execution_id = "RUN-TZ-0001"
        with DatabaseSession.session_scope() as session:
            session.add(
                ExecutionBatchModel(
                    execution_id=execution_id,
                    trigger="web",
                    executor="unit-test",
                    environment="dev",
                    status="finished",
                    total_cases=1,
                    passed=1,
                    # 同一墙钟值，两种语义来源
                    created_at=FIXED_NAIVE,
                    started_at=FIXED_NAIVE,
                    finished_at=FIXED_NAIVE,
                )
            )
            session.add(
                DefectStatistic(
                    execution_id=execution_id,
                    total_cases=1,
                    passed=1,
                    failed=0,
                    error=0,
                    skipped=0,
                    pass_rate=1.0,
                    created_at=FIXED_NAIVE,
                )
            )
        return execution_id

    @allure.story("批次列表项 created_at 带 +00:00")
    def test_list_item_created_at_marked(self, temp_db):
        self._seed()
        items = CaseManager.list_executions_paged()["items"]
        assert len(items) == 1
        created_at = items[0]["created_at"]
        assert created_at == "2026-10-03T06:15:35+00:00", (
            f"UTC 来源应只补标识、墙钟值不变，实际 {created_at!r}"
        )

    @allure.story("批次状态 started_at/finished_at 换算后带 +00:00")
    def test_status_times_converted(self, temp_db):
        self._seed()
        status_data = CaseManager.get_execution_status("RUN-TZ-0001")
        assert status_data is not None
        for field in ("started_at", "finished_at", "created_at"):
            value = status_data[field]
            assert value is not None, f"{field} 不应为 None"
            assert value.endswith("+00:00"), (
                f"{field} 必须带 UTC 标识，实际 {value!r}"
            )
        # 本地来源必须被"换算"：换回本地后墙钟值与写入值一致
        started = datetime.fromisoformat(status_data["started_at"])
        assert started.astimezone().replace(tzinfo=None) == FIXED_NAIVE, (
            "本地来源的 started_at 换算成 UTC 后再换回本地应还原原墙钟值"
        )

    @allure.story("两种来源在同一响应里代表不同绝对时刻（防一律标 UTC）")
    def test_mixed_sources_in_one_payload(self, temp_db):
        """
        同一批次里 created_at(UTC 来源) 与 started_at(本地来源) 写入
        **同一墙钟值**时，序列化后必须代表**不同的绝对时刻**。
        若实现对两者一律 replace(tzinfo=utc)，它们会变成同一绝对时刻，
        本地时间字段在前端就会偏一个时区偏移（UTC+8 用户偏 8 小时）。
        """
        self._seed()
        status_data = CaseManager.get_execution_status("RUN-TZ-0001")
        created = datetime.fromisoformat(status_data["created_at"])
        started = datetime.fromisoformat(status_data["started_at"])
        offset = datetime.now().astimezone().utcoffset() or timedelta(0)
        if offset != timedelta(0):
            assert created != started, (
                "本地时区非 UTC 时，同墙钟值的 UTC 来源与本地来源"
                "换算后必须是不同绝对时刻"
            )
            assert started - created == -offset or started - created == offset, (
                "两者的绝对时刻差应恰为一个时区偏移"
            )
