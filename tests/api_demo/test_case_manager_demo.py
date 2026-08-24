"""
case_manager模块演示与验证用例（第二阶段Day1）

验证目标:
    1. generate_execution_id: 批次号格式正则匹配 + 100次生成全部不重复
    2. sync_cases_from_file: 首次同步inserted=4 / 二次同步updated=4 /
       不存在的文件抛CaseManagerError（携带上下文）
    3. list_cases: module/priority/case_type多维度筛选、命中数量与
       priority升序（P0→P3）再case_id升序的排序核对

数据隔离设计:
    通过monkeypatch将TM_DB_SQLITE_PATH指向独立库文件
    output/test_case_manager_demo.db，每个测试独享干净数据库；
    测试结束先reset释放引擎连接（Windows下必须先释放才能删除库文件），
    再删除演示库文件，不留测试垃圾、不污染默认库。
"""

import re
from pathlib import Path

import allure
import pytest

from src.core.case_manager import (
    CaseManager,
    CaseManagerError,
    generate_execution_id,
)
from src.db import models
from src.db.db_session import DatabaseSession

# 项目根目录（本文件位于 tests/api_demo/ 下，向上两级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 同步用演示数据文件（4条用例: P0/P1/P1/P2，模块均为"用户管理"）
DATA_FILE = PROJECT_ROOT / "testdata" / "yaml" / "api_user_query_matrix.yaml"

# 演示数据库文件路径（与fixture中环境变量指向的路径一致）
DEMO_DB = PROJECT_ROOT / "output" / "test_case_manager_demo.db"

# 批次号格式: RUN-YYYYMMDD-HHMMSS-xxxx（xxxx为4位hex小写）
EXECUTION_ID_PATTERN = re.compile(r"^RUN-\d{8}-\d{6}-[0-9a-f]{4}$")


@pytest.fixture()
def demo_db(monkeypatch):
    """
    独立演示数据库fixture（每个测试独享干净数据库）

    测试前:
        1. monkeypatch覆写TM_DB_SQLITE_PATH指向独立库文件
        2. DatabaseSession.reset()清除引擎单例（保证新环境变量生效）
        3. DatabaseSession.init_db()在独立库中建表
    测试后:
        1. reset释放数据库连接（Windows下必须先释放才能删除库文件）
        2. 删除演示库文件

    参数:
        monkeypatch (pytest.MonkeyPatch): pytest内置monkeypatch fixture

    返回:
        Generator[Path]: yield演示库文件路径
    """
    monkeypatch.setenv("TM_DB_SQLITE_PATH", "output/test_case_manager_demo.db")
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield DEMO_DB
    DatabaseSession.reset()
    if DEMO_DB.exists():
        DEMO_DB.unlink()


@allure.feature("用例调度管理")
@allure.story("批次号生成")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestGenerateExecutionId:
    """generate_execution_id批次号生成验证"""

    def test_execution_id_format(self):
        """
        批次号格式校验: 匹配RUN-YYYYMMDD-HHMMSS-xxxx（4位hex小写）

        参数:
            无

        返回:
            无
        """
        execution_id = generate_execution_id()
        assert EXECUTION_ID_PATTERN.match(execution_id), (
            f"批次号格式非法: {execution_id}，"
            f"期望匹配 ^RUN-\\d{{8}}-\\d{{6}}-[0-9a-f]{{4}}$"
        )

    def test_execution_id_unique_within_100(self):
        """
        唯一性校验: 连续生成100个批次号全部不重复

        参数:
            无

        返回:
            无
        """
        execution_ids = [generate_execution_id() for _ in range(100)]
        assert len(set(execution_ids)) == 100, (
            f"100个批次号存在重复，唯一数量: {len(set(execution_ids))}"
        )


@allure.feature("用例调度管理")
@allure.story("用例加载入库")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestSyncCasesFromFile:
    """sync_cases_from_file用例upsert入库验证"""

    def test_first_sync_inserts_four_cases(self, demo_db):
        """
        首次同步: 4条用例全部新增（inserted=4），库内数据与
        case_type推断（api）、priority规范化核对

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        result = CaseManager.sync_cases_from_file(DATA_FILE)

        assert result == {"total": 4, "inserted": 4, "updated": 0}

        # 库内逐条核对（按case_id排序后比对关键字段）
        with DatabaseSession.session_scope() as session:
            rows = (
                session.query(models.TestCase)
                .order_by(models.TestCase.case_id)
                .all()
            )
            assert len(rows) == 4
            assert rows[0].case_id == "TM-API-0201"
            assert rows[0].priority == "P0"
            assert rows[0].case_type == "api"
            assert rows[0].creator == "admin"
            assert rows[0].status == "active"
            assert all(row.case_type == "api" for row in rows)

    def test_second_sync_updates_four_cases(self, demo_db):
        """
        二次同步: 相同数据case_id已存在，4条全部走更新（updated=4）

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        CaseManager.sync_cases_from_file(DATA_FILE)
        result = CaseManager.sync_cases_from_file(DATA_FILE)

        assert result == {"total": 4, "inserted": 0, "updated": 4}

        # 更新不产生重复记录
        with DatabaseSession.session_scope() as session:
            assert session.query(models.TestCase).count() == 4

    def test_sync_nonexistent_file_raises(self, demo_db):
        """
        异常路径: 同步不存在的数据文件抛CaseManagerError，
        context携带操作与文件路径上下文

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        bad_file = PROJECT_ROOT / "testdata" / "yaml" / "not_exist_data.yaml"
        with pytest.raises(CaseManagerError) as exc_info:
            CaseManager.sync_cases_from_file(bad_file)

        # 异常上下文携带文件路径，便于定位
        assert "not_exist_data.yaml" in str(exc_info.value.context.get("file_path", ""))


@allure.feature("用例调度管理")
@allure.story("用例查询")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestListCases:
    """list_cases多维度查询与排序验证"""

    @pytest.fixture(autouse=True)
    def setup_cases(self, demo_db):
        """
        类内共用前置: 每个测试先同步4条标准用例入库

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        CaseManager.sync_cases_from_file(DATA_FILE)

    def test_list_all_sorted_by_priority_and_case_id(self):
        """
        全量查询排序: P0→P1→P1→P2优先级升序，
        同优先级P1两条按case_id升序

        参数:
            无

        返回:
            无
        """
        cases = CaseManager.list_cases()

        assert len(cases) == 4
        priorities = [case["priority"] for case in cases]
        assert priorities == ["P0", "P1", "P1", "P2"], (
            f"优先级排序异常: {priorities}，期望P0→P1→P1→P2"
        )
        case_ids = [case["case_id"] for case in cases]
        assert case_ids == [
            "TM-API-0201", "TM-API-0202", "TM-API-0203", "TM-API-0204",
        ], f"case_id排序异常: {case_ids}"

    def test_filter_by_module(self):
        """
        模块筛选: 命中模块返回4条，未命中模块返回0条，
        列表入参多模块任一命中

        参数:
            无

        返回:
            无
        """
        matched = CaseManager.list_cases(module="用户管理")
        assert len(matched) == 4

        not_matched = CaseManager.list_cases(module="不存在的模块")
        assert len(not_matched) == 0

        multi_matched = CaseManager.list_cases(module=["用户管理", "订单管理"])
        assert len(multi_matched) == 4

    def test_filter_by_priority(self):
        """
        优先级筛选: 单值命中P0仅1条；列表P0+P2命中2条；
        小写p0容错转大写后命中

        参数:
            无

        返回:
            无
        """
        p0_cases = CaseManager.list_cases(priority="P0")
        assert len(p0_cases) == 1
        assert p0_cases[0]["case_id"] == "TM-API-0201"

        p0_p2_cases = CaseManager.list_cases(priority=["P0", "P2"])
        assert len(p0_p2_cases) == 2
        assert [case["case_id"] for case in p0_p2_cases] == [
            "TM-API-0201", "TM-API-0204",
        ]

        # 小写优先级容错: 统一大写后匹配
        lowercase_cases = CaseManager.list_cases(priority="p0")
        assert len(lowercase_cases) == 1

    def test_filter_by_case_type(self):
        """
        类型筛选: api类型命中4条（入库时按路径推断为api），chip类型命中0条

        参数:
            无

        返回:
            无
        """
        api_cases = CaseManager.list_cases(case_type="api")
        assert len(api_cases) == 4

        chip_cases = CaseManager.list_cases(case_type="chip")
        assert len(chip_cases) == 0
