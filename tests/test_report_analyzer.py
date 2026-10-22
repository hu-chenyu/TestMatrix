"""
report_analyzer 统计入库 upsert 语义专项验证（Day63-fix）

验证目标（P2 缺陷修复后的新契约）:
  1. 首次入库：同 execution_id 不存在时走 INSERT，字段映射正确
  2. 同批次号二次入库：**累加**而不是抛 IntegrityError（批量桥接的真实形态）
  3. 通过率：累加后必须按 passed / total_cases **重算**，不能累加比率
  4. error / skipped：两个非主状态列同样逐次累加（表内无耗时列，故不测耗时）
  5. 不同 execution_id 互不影响（各自独立成行）
  6. remark：仅本次非空时覆盖，空串保留已有扩展数据

为什么需要这批用例:
  修复前 save_statistics 是纯 INSERT，批量执行时执行器每条用例桥接同一
  批次号，第 2 条起必撞 `UNIQUE constraint failed:
  defect_statistics.execution_id`——批次统计只剩第 1 条用例的数据。原契约
  被两条既有用例（重复必抛 IntegrityError / 桥接二次失败）钉死，本文件
  与那两条断言同步改为"累加"口径，共同覆盖新契约。

数据隔离设计:
  每个测试独享 tmp_path 下的临时 SQLite 库（monkeypatch 环境变量 +
  DatabaseSession.reset() + init_db()），测试后 reset 恢复，不污染正式库。
"""

from pathlib import Path

import allure
import pytest
from src.core.report_analyzer import ReportRepository, StatisticsResult
from src.db import models
from src.db.db_session import DatabaseSession


def make_stat(
  total: int = 1,
  passed: int = 1,
  failed: int = 0,
  broken: int = 0,
  skipped: int = 0,
) -> StatisticsResult:
  """
  构造 StatisticsResult 测试对象（直接填充字段，不经过 aggregate）

  参数:
    total (int): 用例总数
    passed (int): 通过数
    failed (int): 失败数（**failed + broken 合计口径**，与聚合层一致）
    broken (int): 异常数（入库时映射到 error 列）
    skipped (int): 跳过数

  返回:
    StatisticsResult: 组装好的统计结果对象
  """
  return StatisticsResult(
    total=total,
    passed=passed,
    failed=failed,
    broken=broken,
    skipped=skipped,
    pass_rate=round(passed / total, 4) if total else 0.0,
  )


def count_rows() -> int:
  """
  统计 defect_statistics 当前行数

  参数:
    无

  返回:
    int: 表内行数（用于断言"累加不新增行"）
  """
  with DatabaseSession.session_scope() as session:
    return int(session.query(models.DefectStatistic).count())


@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
  """
  临时 SQLite 数据库 fixture（每个测试独享干净库）

  测试前:
    1. monkeypatch 设置 TM_DB_TYPE=sqlite 与临时库路径
    2. DatabaseSession.reset() 清除引擎单例（让新环境变量生效）
    3. DatabaseSession.init_db() 建表
  测试后:
    DatabaseSession.reset() 释放引擎，避免影响其他测试

  参数:
    tmp_path (Path): pytest 临时目录 fixture
    monkeypatch (pytest.MonkeyPatch): 环境变量覆写 fixture

  返回:
    Path: 临时数据库文件路径
  """
  db_file = tmp_path / "test_report_analyzer_upsert.db"
  monkeypatch.setenv("TM_DB_TYPE", "sqlite")
  monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
  DatabaseSession.reset()
  DatabaseSession.init_db()
  yield db_file
  DatabaseSession.reset()


@allure.feature("报告统计仓储")
@allure.story("统计入库upsert累加")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestSaveStatisticsUpsert:
  """save_statistics 先查后更（累加）语义验证"""

  def test_save_statistics_insert_new_execution(self, temp_db: Path):
    """
    首次入库：execution_id 不存在时走 INSERT，字段映射正确且仅 1 行

    参数:
      temp_db (Path): 临时数据库 fixture

    返回:
      无
    """
    stat = make_stat(total=10, passed=5, failed=3, broken=1, skipped=2)
    record = ReportRepository.save_statistics(stat, "RUN-UPSERT-0001")

    assert record.id is not None
    assert record.total_cases == 10
    assert record.passed == 5
    assert record.failed == 2  # 3 合计 - 1 broken = 2 纯 failed
    assert record.error == 1  # broken 映射 error
    assert record.skipped == 2
    assert record.pass_rate == 0.5
    assert count_rows() == 1

  def test_save_statistics_upsert_accumulate_two_passed(self, temp_db: Path):
    """
    同批次号两次全通过：累加为 total=2 / passed=2，通过率重算为 1.0，仍只有 1 行

    参数:
      temp_db (Path): 临时数据库 fixture

    返回:
      无
    """
    ReportRepository.save_statistics(make_stat(total=1, passed=1), "RUN-UPSERT-0002")
    record = ReportRepository.save_statistics(
      make_stat(total=1, passed=1), "RUN-UPSERT-0002"
    )

    assert record.total_cases == 2
    assert record.passed == 2
    assert record.failed == 0
    assert record.pass_rate == pytest.approx(1.0)
    assert count_rows() == 1  # 累加不新增行

  def test_save_statistics_upsert_mixed_results(self, temp_db: Path):
    """
    混合结果：先 1 通过再 1 失败，累加后 total=2 / passed=1 / failed=1 / 通过率 0.5

    参数:
      temp_db (Path): 临时数据库 fixture

    返回:
      无
    """
    ReportRepository.save_statistics(make_stat(total=1, passed=1), "RUN-UPSERT-0003")
    record = ReportRepository.save_statistics(
      make_stat(total=1, passed=0, failed=1), "RUN-UPSERT-0003"
    )

    assert record.total_cases == 2
    assert record.passed == 1
    assert record.failed == 1
    assert record.error == 0
    # 通过率必须重算（1/2），不能把两次的 1.0 与 0.0 相加
    assert record.pass_rate == pytest.approx(0.5)

  def test_save_statistics_upsert_error_skipped_accumulate(self, temp_db: Path):
    """
    error / skipped 两列同样累加：broken 映射 error 后逐次叠加，跳过数叠加

    参数:
      temp_db (Path): 临时数据库 fixture

    返回:
      无

    说明:
      表内无耗时分列（DefectStatistic 未建 duration_seconds），
      故本用例覆盖的是除 total/passed/failed 之外唯二可累加的计数列。
    """
    # 第一次：3 条 = 1 通过 + 1 失败 + 1 异常（failed 参数含 broken），另 1 跳过
    ReportRepository.save_statistics(
      make_stat(total=4, passed=1, failed=2, broken=1, skipped=1),
      "RUN-UPSERT-0004",
    )
    # 第二次：2 条 = 1 失败 + 1 异常
    record = ReportRepository.save_statistics(
      make_stat(total=2, passed=0, failed=2, broken=1, skipped=0),
      "RUN-UPSERT-0004",
    )

    assert record.total_cases == 6
    assert record.passed == 1
    assert record.failed == 2  # 1 + 1
    assert record.error == 2  # 1 + 1（broken 累加）
    assert record.skipped == 1  # 1 + 0
    assert record.pass_rate == pytest.approx(1 / 6, rel=1e-4)

  def test_save_statistics_different_executions_independent(self, temp_db: Path):
    """
    不同 execution_id 各自独立成行，互不累加

    参数:
      temp_db (Path): 临时数据库 fixture

    返回:
      无
    """
    first = ReportRepository.save_statistics(
      make_stat(total=3, passed=3), "RUN-UPSERT-0005A"
    )
    second = ReportRepository.save_statistics(
      make_stat(total=2, passed=1, failed=1), "RUN-UPSERT-0005B"
    )

    assert count_rows() == 2
    assert first.total_cases == 3
    assert first.passed == 3
    assert second.total_cases == 2
    assert second.passed == 1
    assert second.failed == 1

  def test_save_statistics_upsert_remark_update(self, temp_db: Path):
    """
    remark 条件更新：非空覆盖、空串保留已有值

    参数:
      temp_db (Path): 临时数据库 fixture

    返回:
      无
    """
    ReportRepository.save_statistics(
      make_stat(total=1, passed=1), "RUN-UPSERT-0006", remark="first"
    )
    second = ReportRepository.save_statistics(
      make_stat(total=1, passed=1), "RUN-UPSERT-0006", remark="second"
    )
    assert second.remark == "second"

    # 第三次不带备注（空串）：不得抹掉已有备注
    third = ReportRepository.save_statistics(
      make_stat(total=1, passed=1), "RUN-UPSERT-0006", remark=""
    )
    assert third.remark == "second"
    assert third.total_cases == 3
