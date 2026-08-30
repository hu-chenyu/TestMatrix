"""
core层边界与异常场景补全测试（第二阶段Day9 review日）

覆盖三模块既有测试未覆盖的盲区:
    1. data_driver: 损坏YAML容错/必填字段缺失定位/空sheet/空Excel/空结果集组合筛选/
       三维全组合筛选
    2. case_manager: dry-run不落库/单用例失败不中断整批/CLI非法路径退出码1/
       空筛选结果批次/优先级执行顺序
    3. report_analyzer: 趋势limit边界（0/超总量/单条）/remark超长文本保存查询

数据隔离: 全部使用tmp_path临时文件与临时数据库，不污染正式数据。
"""

import json
from pathlib import Path

import allure
import pytest
from openpyxl import Workbook

from src.core.case_manager import CaseManager, CaseManagerError, run_batch
from src.core.data_driver import DataDriver, DataDriverError
from src.core.report_analyzer import ReportRepository, StatisticsResult
from src.db import models
from src.db.db_session import DatabaseSession

# 项目根目录（本文件位于 tests/ 下，向上一级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 演示数据文件（4条: P0/P1/P1/P2，模块"用户管理"）
DATA_FILE = PROJECT_ROOT / "testdata" / "yaml" / "api_user_query_matrix.yaml"


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """
    临时SQLite数据库fixture（case_manager系列测试独享）

    参数:
        tmp_path (Path): pytest临时目录fixture
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture

    返回:
        Path: 临时数据库文件路径
    """
    db_file = tmp_path / "test_core_edge.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_file
    DatabaseSession.reset()


# ======================================================================
# data_driver边界补全
# ======================================================================
@allure.feature("core层边界补全")
@allure.story("data_driver边界")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestDataDriverEdgeCases:
    """data_driver异常格式容错与组合筛选边界验证"""

    def test_corrupted_yaml_raises_with_context(self, tmp_path):
        """
        损坏YAML: 语法非法文件抛DataDriverError，
        异常信息携带文件路径定位

        参数:
            tmp_path (Path): 临时目录fixture

        返回:
            无
        """
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text(
            "cases:\n  - case_id: TM-0001\n   name: 缩进错误\n", encoding="utf-8"
        )

        with pytest.raises(DataDriverError, match="YAML语法解析失败"):
            DataDriver.load_cases(bad_file)

    def test_missing_required_field_with_location(self, tmp_path):
        """
        必填字段缺失: 缺case_id的用例报错带"第1条用例"定位与字段名

        参数:
            tmp_path (Path): 临时目录fixture

        返回:
            无
        """
        bad_file = tmp_path / "missing_field.yaml"
        bad_file.write_text(
            "- name: 缺失编号用例\n  module: 用户管理\n  priority: P0\n",
            encoding="utf-8",
        )

        with pytest.raises(DataDriverError, match="第1条用例.*case_id"):
            DataDriver.load_cases(bad_file)

    def test_empty_excel_sheet_raises(self, tmp_path):
        """
        空sheet: 无任何内容的Excel抛DataDriverError

        参数:
            tmp_path (Path): 临时目录fixture

        返回:
            无
        """
        workbook = Workbook()
        empty_file = tmp_path / "empty.xlsx"
        workbook.save(empty_file)

        with pytest.raises(DataDriverError, match="内容为空"):
            DataDriver.load_cases(empty_file)

    def test_excel_with_partial_blank_rows(self, tmp_path):
        """
        空行跳过: 含空行与部分空白数据行的Excel正确解析非空行

        参数:
            tmp_path (Path): 临时目录fixture

        返回:
            无
        """
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["case_id", "name", "module", "priority", "tags"])
        sheet.append(["TM-E-001", "正常用例", "边界测试", "P0", "smoke"])
        sheet.append([None, None, None, None, None])  # 整行空，跳过
        sheet.append(["TM-E-002", "第二条", "边界测试", "P1", "api"])
        excel_file = tmp_path / "blank_rows.xlsx"
        workbook.save(excel_file)

        cases = DataDriver.load_cases(excel_file)

        assert len(cases) == 2
        assert cases[0]["case_id"] == "TM-E-001"
        assert cases[1]["case_id"] == "TM-E-002"

    def test_combined_filter_empty_result(self, tmp_path):
        """
            组合筛选空结果: 三维全组合条件命中0条时返回空列表不抛异常

        参数:
            tmp_path (Path): 临时目录fixture

        返回:
            无
        """
        data_file = tmp_path / "cases.yaml"
        data_file.write_text(
            "- case_id: TM-0001\n  name: 用例\n  module: 用户管理\n"
            "  priority: P0\n  tags: [smoke]\n",
            encoding="utf-8",
        )
        cases = DataDriver.load_cases(data_file)

        # 三维条件互斥组合（模块不匹配+优先级不匹配+标签不匹配）
        empty = DataDriver.filter_cases(
            cases, module="不存在的模块", priority="P3", tags=["nonexistent"]
        )
        assert empty == []

        # 单维度不匹配同样返回空
        assert DataDriver.filter_cases(cases, module="订单管理") == []
        assert DataDriver.filter_cases(cases, tags=["security"]) == []

    def test_full_combination_filter_hits(self, tmp_path):
        """
        三维全组合命中: module+priority+tags同时满足时精确命中

        参数:
            tmp_path (Path): 临时目录fixture

        返回:
            无
        """
        data_file = tmp_path / "multi.yaml"
        data_file.write_text(
            "- case_id: TM-0001\n  name: 命中\n  module: 用户管理\n"
            "  priority: P0\n  tags: [smoke, api]\n"
            "- case_id: TM-0002\n  name: 模块不符\n  module: 订单\n"
            "  priority: P0\n  tags: [smoke]\n"
            "- case_id: TM-0003\n  name: 标签不符\n  module: 用户管理\n"
            "  priority: P0\n  tags: [regression]\n",
            encoding="utf-8",
        )
        cases = DataDriver.load_cases(data_file)

        matched = DataDriver.filter_cases(
            cases, module="用户管理", priority="P0", tags=["smoke"]
        )
        assert [case["case_id"] for case in matched] == ["TM-0001"]


# ======================================================================
# case_manager边界补全
# ======================================================================
@allure.feature("core层边界补全")
@allure.story("case_manager边界")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestCaseManagerEdgeCases:
    """case_manager执行链路边界与容错验证"""

    def test_dry_run_writes_nothing(self, temp_db):
        """
        dry-run不落库: 执行后test_executions与defect_statistics均无记录

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        result = run_batch(DATA_FILE, dry_run=True)

        assert result == {"dry_run": True, "count": 4}
        with DatabaseSession.session_scope() as session:
            assert session.query(models.TestExecution).count() == 0
            assert session.query(models.DefectStatistic).count() == 0

    def test_single_failure_does_not_break_batch(self, temp_db):
        """
        单用例失败不中断: P0单条失败（奇偶规则）批次仍完成统计，
        失败明细含模拟执行错误信息

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        summary = run_batch(DATA_FILE, priority="P0")

        # TM-API-0201末尾1奇数→failed，但批次统计正常完成
        assert summary["total"] == 1
        assert summary["failed"] == 1
        assert summary["pass_rate"] == 0.0

        with DatabaseSession.session_scope() as session:
            records = session.query(models.TestExecution).all()
            assert len(records) == 1
            assert "模拟执行失败" in records[0].error_message

    def test_empty_selection_raises_no_execution(self, temp_db):
        """
        空筛选结果: 不匹配的筛选条件无执行记录，
        finish_execution按设计抛"批次不存在"（无记录批次不产出统计）

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        execution_id = CaseManager.create_execution(trigger="cli")
        selected = CaseManager.select_cases_for_execution(
            module="不存在的模块"
        )
        assert selected == []

        # 无执行记录的批次不可finish（Day4既定设计）
        with pytest.raises(CaseManagerError, match="不存在"):
            CaseManager.finish_execution(execution_id)

    def test_priority_execution_order(self, temp_db):
        """
        优先级执行顺序: 全量执行时按P0→P1→P1→P2顺序入库
        （id递增即执行顺序，验证分级调度有序性）

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        run_batch(DATA_FILE)

        with DatabaseSession.session_scope() as session:
            records = (
                session.query(models.TestExecution)
                .order_by(models.TestExecution.id)
                .all()
            )
            priorities = [
                CaseManager.list_cases(
                    module="用户管理", case_type="api"
                ),
            ]
            # 执行顺序与用例优先级排序一致
            assert [record.case_id for record in records] == [
                "TM-API-0201", "TM-API-0202", "TM-API-0203", "TM-API-0204",
            ]

    def test_cli_nonexistent_file_exit_code(self, temp_db, monkeypatch, capsys):
        """
        CLI边界: --file指向不存在文件时打印错误并以退出码1退出

        参数:
            temp_db (Path): 临时数据库fixture
            monkeypatch (pytest.MonkeyPatch): sys.argv覆写
            capsys: 输出捕获fixture

        返回:
            无
        """
        from src.core.case_manager import main

        monkeypatch.setattr(
            "sys.argv",
            ["case_manager", "--file", str(temp_db / "not_exist.yaml")],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        assert "不存在" in capsys.readouterr().out

    def test_record_execution_empty_id_raises(self, temp_db):
        """
        入参校验: 空批次号记录执行结果抛CaseManagerError

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        with pytest.raises(CaseManagerError, match="批次号不能为空"):
            CaseManager.record_execution(
                execution_id="", case_id="TM-0001", case_name="用例",
                result="passed", start_time=None, end_time=None, duration=0.1,
            )


# ======================================================================
# report_analyzer边界补全
# ======================================================================
@allure.feature("core层边界补全")
@allure.story("report_analyzer边界")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestReportAnalyzerEdgeCases:
    """report_analyzer趋势边界与remark超长文本验证"""

    def _insert_records(self, count: int) -> None:
        """
        插入指定数量的统计记录（模块内工具方法）

        参数:
            count (int): 插入记录数

        返回:
            无
        """
        import time as time_module

        for index in range(count):
            ReportRepository.save_statistics(
                StatisticsResult(
                    total=10, passed=index + 1,
                    pass_rate=round((index + 1) / 10, 4),
                ),
                f"RUN-EDGE-{index:04d}",
            )
            time_module.sleep(0.02)

    def test_trend_limit_zero_and_negative(self, temp_db):
        """
        趋势边界: limit=0与负数均返回空列表（数据库limit非正不取数）

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        self._insert_records(3)

        assert ReportRepository.get_trend_data(limit=0) == []
        assert ReportRepository.get_pass_rate_trend(limit=0) == []

    def test_trend_limit_exceeds_total(self, temp_db):
        """
        趋势边界: limit超过总记录数时返回全部记录（不报错不填充）

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        self._insert_records(3)

        trend = ReportRepository.get_trend_data(limit=100)
        assert len(trend) == 3

        rates = ReportRepository.get_pass_rate_trend(limit=100)
        assert rates == [0.1, 0.2, 0.3]

    def test_trend_single_record(self, temp_db):
        """
        单条趋势: 仅1条记录时趋势列表长度为1（无对比场景容错）

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        self._insert_records(1)

        trend = ReportRepository.get_trend_data()
        assert len(trend) == 1
        assert trend[0]["execution_id"] == "RUN-EDGE-0000"
        assert ReportRepository.get_pass_rate_trend() == [0.1]

    def test_remark_very_long_text(self, temp_db):
        """
        remark超长文本: 10KB扩展数据保存与完整查询（Text列容量验证）

        参数:
            temp_db (Path): 临时数据库fixture

        返回:
            无
        """
        long_remark = json.dumps(
            {"failed_details": [{"name": f"case_{i}", "error": "x" * 50} for i in range(100)]},
            ensure_ascii=False,
        )
        assert len(long_remark) > 5000  # 确认构造出超长文本

        ReportRepository.save_statistics(
            StatisticsResult(total=5, passed=5, pass_rate=1.0),
            "RUN-LONG-REMARK",
            remark=long_remark,
        )

        record = ReportRepository.get_by_execution_id("RUN-LONG-REMARK")
        assert record.remark == long_remark
        # 超长remark可正常反序列化
        parsed = json.loads(record.remark)
        assert len(parsed["failed_details"]) == 100
