"""
report_analyzer统计结果仓储验证用例（第二阶段Day8）

验证目标:
    1. save_statistics字段映射: failed剔除broken、broken映射error
    2. 唯一约束: 重复execution_id抛IntegrityError
    3. 查询: 按批次号查询/最近N条降序/趋势升序
    4. 趋势数据: get_trend_data字典格式/get_pass_rate_trend浮点列表
    5. remark扩展字段保存
    6. 端到端: 解析→统计→入库→查询全链路数据一致

数据隔离设计:
    每个测试函数独享tmp_path临时SQLite库（monkeypatch环境变量+
    reset+init_db），测试后reset恢复，不污染正式库。
"""

import time
from pathlib import Path

import allure
import pytest
from sqlalchemy.exc import IntegrityError

from src.core.report_analyzer import (
    AllureResult,
    ReportAnalyzer,
    ReportRepository,
    ReportStatistics,
    StatisticsResult,
)
from src.db.db_session import DatabaseSession

# 项目根目录（本文件位于 tests/ 下，向上一级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 真实Allure结果目录
REAL_RESULTS_DIR = PROJECT_ROOT / "output" / "allure_results"


def make_stat(
    total: int = 10,
    passed: int = 5,
    failed: int = 2,
    broken: int = 1,
    skipped: int = 2,
) -> StatisticsResult:
    """
    构造StatisticsResult测试对象（直接填充字段，不经过aggregate）

    参数:
        total/passed/failed/broken/skipped (int): 各状态计数

    返回:
        StatisticsResult: 组装好的统计结果对象
    """
    return StatisticsResult(
        total=total,
        passed=passed,
        failed=failed,  # 注意: 此处failed为failed+broken合计口径
        broken=broken,
        skipped=skipped,
        pass_rate=round(passed / total, 4) if total else 0.0,
    )


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """
    临时SQLite数据库fixture（每个测试独享干净数据库）

    测试前:
        1. monkeypatch设置TM_DB_TYPE=sqlite与临时库路径
        2. DatabaseSession.reset()清除引擎单例（新环境变量生效）
        3. DatabaseSession.init_db()建表
    测试后:
        DatabaseSession.reset()恢复（避免影响其他测试）

    参数:
        tmp_path (Path): pytest临时目录fixture
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture

    返回:
        Path: 临时数据库文件路径
    """
    db_file = tmp_path / "test_report_repo.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_file
    DatabaseSession.reset()


@allure.feature("报告统计仓储")
@allure.story("统计入库")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestSaveStatistics:
    """save_statistics统计结果入库验证"""

    def test_save_statistics_basic(self, temp_db):
        """
        字段映射: 混合状态（5P+2纯F+1broken+2S）入库后
        failed=2纯failed、error=1broken、total=10、pass_rate=0.5

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        stat = make_stat(total=10, passed=5, failed=3, broken=1, skipped=2)
        record = ReportRepository.save_statistics(stat, "RUN-TEST-0001")

        assert record.total_cases == 10
        assert record.passed == 5
        assert record.failed == 2  # 3合计 - 1broken = 2纯failed
        assert record.error == 1  # broken映射error
        assert record.skipped == 2
        assert record.pass_rate == 0.5
        assert record.id is not None

    def test_save_statistics_all_passed(self, temp_db):
        """
        全通过场景: failed=0、error=0、pass_rate=1.0

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        stat = make_stat(total=8, passed=8, failed=0, broken=0, skipped=0)
        record = ReportRepository.save_statistics(stat, "RUN-TEST-0002")

        assert record.total_cases == 8
        assert record.failed == 0
        assert record.error == 0
        assert record.pass_rate == 1.0

    def test_save_statistics_duplicate_execution_id(self, temp_db):
        """
        唯一约束: 相同execution_id二次入库抛IntegrityError

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        stat = make_stat(total=5, passed=5)
        ReportRepository.save_statistics(stat, "RUN-DUP-0001")

        with pytest.raises(IntegrityError):
            ReportRepository.save_statistics(stat, "RUN-DUP-0001")

    def test_remark_field_saved(self, temp_db):
        """
        remark扩展字段: 入库时传入JSON扩展数据文本，查询验证保存正确

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        stat = make_stat(total=4, passed=4)
        remark_text = '{"p95_duration_ms": 320.5, "by_module": {"用户管理": {"total": 4}}}'
        ReportRepository.save_statistics(stat, "RUN-REMARK-0001", remark=remark_text)

        record = ReportRepository.get_by_execution_id("RUN-REMARK-0001")
        assert record.remark == remark_text
        assert "p95_duration_ms" in record.remark


@allure.feature("报告统计仓储")
@allure.story("统计查询")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestQueryStatistics:
    """统计结果查询验证"""

    def test_get_by_execution_id(self, temp_db):
        """
        批次号查询: 已入库批次返回正确对象；
        不存在批次返回None

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        ReportRepository.save_statistics(
            make_stat(total=6, passed=6), "RUN-QUERY-0001"
        )

        record = ReportRepository.get_by_execution_id("RUN-QUERY-0001")
        assert record is not None
        assert record.total_cases == 6
        assert record.pass_rate == 1.0

        assert ReportRepository.get_by_execution_id("RUN-NOT-EXIST") is None

    def test_get_latest_statistics(self, temp_db):
        """
        最近N条查询: 插入5条后limit=3返回3条，
        按created_at降序（最新在前）

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        for index in range(5):
            ReportRepository.save_statistics(
                make_stat(total=10, passed=index + 1),
                f"RUN-LATEST-{index:04d}",
            )
            time.sleep(0.02)  # 保证created_at可区分

        records = ReportRepository.get_latest_statistics(limit=3)

        assert len(records) == 3
        # 最新在前（最后插入的RUN-LATEST-0004排第一）
        assert records[0].execution_id == "RUN-LATEST-0004"
        assert records[2].execution_id == "RUN-LATEST-0002"

    def test_get_trend_data(self, temp_db):
        """
        趋势数据: 插入5条递增通过率（0.5→0.9），
        返回5条时间升序、pass_rate依次递增的字典列表

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        pass_rates = [0.5, 0.6, 0.7, 0.8, 0.9]
        for index, rate in enumerate(pass_rates):
            ReportRepository.save_statistics(
                make_stat(total=10, passed=int(rate * 10)),
                f"RUN-TREND-{index:04d}",
            )
            time.sleep(0.02)

        trend = ReportRepository.get_trend_data(limit=5)

        assert len(trend) == 5
        # 时间升序: 最早插入的在前
        assert trend[0]["execution_id"] == "RUN-TREND-0000"
        assert trend[-1]["execution_id"] == "RUN-TREND-0004"
        # 通过率依次递增
        assert [item["pass_rate"] for item in trend] == pass_rates
        # 字典字段完整性（created_at为字符串可序列化）
        assert all(item["created_at"] for item in trend)
        assert isinstance(trend[0]["created_at"], str)
        assert "total_cases" in trend[0] and "failed" in trend[0]

    def test_get_pass_rate_trend(self, temp_db):
        """
        通过率列表: 插入3条后返回[0.5, 0.7, 0.9]浮点数列表（时间升序）

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        for index, passed in enumerate([5, 7, 9]):
            ReportRepository.save_statistics(
                make_stat(total=10, passed=passed),
                f"RUN-RATE-{index:04d}",
            )
            time.sleep(0.02)

        rate_trend = ReportRepository.get_pass_rate_trend(limit=3)

        assert rate_trend == [0.5, 0.7, 0.9]
        assert all(isinstance(rate, float) for rate in rate_trend)

    def test_get_trend_empty_table(self, temp_db):
        """
        空表容错: get_trend_data与get_pass_rate_trend均返回空列表

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        assert ReportRepository.get_trend_data() == []
        assert ReportRepository.get_pass_rate_trend() == []


@allure.feature("报告统计仓储")
@allure.story("端到端集成")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.api
@pytest.mark.regression
class TestEndToEndIntegration:
    """解析→统计→入库→查询全链路集成验证"""

    def test_end_to_end_parse_aggregate_save(self, temp_db):
        """
        全链路: 真实Allure结果目录（缺失时tmp构造降级）
        parse→aggregate→save→get_by_execution_id数据一致

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        # 1. 解析（真实目录优先，无产物时构造降级）
        results = ReportAnalyzer.parse_results_dir(REAL_RESULTS_DIR)
        if not results:
            results = [
                AllureResult(
                    uuid=f"uuid-{i}", name=f"case_{i}", status="passed",
                    start=100, stop=200,
                    labels={"severity": ["normal"]},
                )
                for i in range(10)
            ]

        # 2. 统计聚合
        stat = ReportStatistics.aggregate(results)

        # 3. 入库（remark携带to_dict扩展数据）
        execution_id = "RUN-E2E-0001"
        ReportRepository.save_statistics(
            stat, execution_id,
            remark=str(ReportStatistics.to_dict(stat)),
        )

        # 4. 查询验证数据一致
        record = ReportRepository.get_by_execution_id(execution_id)
        assert record is not None
        assert record.total_cases == stat.total
        assert record.passed == stat.passed
        assert record.failed == stat.failed - stat.broken
        assert record.error == stat.broken
        assert record.skipped == stat.skipped
        assert record.pass_rate == stat.pass_rate

        # 5. 趋势查询含该批次
        trend = ReportRepository.get_trend_data(limit=10)
        assert any(item["execution_id"] == execution_id for item in trend)
