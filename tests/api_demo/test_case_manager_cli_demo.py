"""
case_manager命令行入口与批量执行验证用例（第二阶段Day3）

验证目标:
    1. run_batch完整链路: 加载4条用例不筛选，total=4、defect_statistics落表
    2. run_batch筛选执行: priority=P0仅执行1条
    3. run_batch dry_run: 返回{"dry_run": True, "count": 4}，
       test_executions表无记录
    4. main()命令行入口: monkeypatch模拟sys.argv正常退出码0
    5. main()非法文件: --file不存在时sys.exit(1)
    6. _simulate_execute: 偶数case_id返回passed、奇数返回failed

数据隔离设计:
    复用demo_db fixture模式: monkeypatch独立库 + 测试后reset释放连接并删库。
"""

import re
from pathlib import Path

import allure
import pytest

from src.core.case_manager import CaseManager, main, run_batch
from src.db import models
from src.db.db_session import DatabaseSession

# 项目根目录（本文件位于 tests/api_demo/ 下，向上两级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 演示数据文件（4条用例: TM-API-0201[P0] / 0202[P1] / 0203[P1] / 0204[P2]）
DATA_FILE = PROJECT_ROOT / "testdata" / "yaml" / "api_user_query_matrix.yaml"

# 演示数据库文件路径（与fixture中环境变量指向的路径一致）
DEMO_DB = PROJECT_ROOT / "output" / "test_case_manager_cli_demo.db"

# 批次号格式
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
    monkeypatch.setenv("TM_DB_SQLITE_PATH", "output/test_case_manager_cli_demo.db")
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield DEMO_DB
    DatabaseSession.reset()
    if DEMO_DB.exists():
        DEMO_DB.unlink()


@allure.feature("用例调度管理")
@allure.story("批量执行")
@allure.severity(allure.severity_level.BLOCKER)
@pytest.mark.api
@pytest.mark.regression
class TestRunBatch:
    """run_batch批量执行完整链路验证"""

    def test_run_batch_full_lifecycle(self, demo_db, capsys):
        """
        完整链路: 4条用例不筛选执行，total=4、
        defect_statistics落表、控制台输出汇总报告

        结果规则（case_id末尾奇偶）: 0201奇→failed / 0202偶→passed /
        0203奇→failed / 0204偶→passed，即2通过2失败

        参数:
            demo_db (Path): 独立演示数据库fixture
            capsys: pytest标准输出捕获fixture

        返回:
            无
        """
        summary = run_batch(DATA_FILE)

        assert summary["total"] == 4
        assert summary["passed"] == 2
        assert summary["failed"] == 2
        assert summary["error"] == 0
        assert summary["skipped"] == 0
        assert summary["pass_rate"] == 0.5
        assert EXECUTION_ID_PATTERN.match(summary["execution_id"])

        # defect_statistics表落库核对
        with DatabaseSession.session_scope() as session:
            statistic = (
                session.query(models.DefectStatistic)
                .filter_by(execution_id=summary["execution_id"])
                .one()
            )
            assert statistic.total_cases == 4
            assert statistic.pass_rate == 0.5

        # 控制台输出汇总报告
        captured = capsys.readouterr()
        assert "批量执行汇总报告" in captured.out
        assert "通过率: 50.00%" in captured.out

    def test_run_batch_with_priority_filter(self, demo_db):
        """
        优先级筛选执行: priority="P0"仅筛选出1条（TM-API-0201），
        末尾奇数→failed，total=1且failed=1

        参数:
            demo_db (Path): 独立演示数据库fixture

        返回:
            无
        """
        summary = run_batch(DATA_FILE, priority="P0")

        assert summary["total"] == 1
        assert summary["failed"] == 1
        assert summary["passed"] == 0
        assert summary["pass_rate"] == 0.0

        # 执行明细仅1条且case_id匹配
        with DatabaseSession.session_scope() as session:
            records = (
                session.query(models.TestExecution)
                .filter_by(execution_id=summary["execution_id"])
                .all()
            )
            assert len(records) == 1
            assert records[0].case_id == "TM-API-0201"
            assert "模拟执行失败" in records[0].error_message

    def test_run_batch_dry_run(self, demo_db, capsys):
        """
        dry_run模式: 返回{"dry_run": True, "count": 4}，
        不产生任何执行记录，控制台打印待执行用例列表

        参数:
            demo_db (Path): 独立演示数据库fixture
            capsys: pytest标准输出捕获fixture

        返回:
            无
        """
        result = run_batch(DATA_FILE, dry_run=True)

        assert result == {"dry_run": True, "count": 4}

        # test_executions表无记录
        with DatabaseSession.session_scope() as session:
            assert session.query(models.TestExecution).count() == 0
            # defect_statistics同样无记录（未进入finish环节）
            assert session.query(models.DefectStatistic).count() == 0

        # 控制台打印待执行列表
        captured = capsys.readouterr()
        assert "待执行用例列表" in captured.out
        assert "TM-API-0201" in captured.out


@allure.feature("用例调度管理")
@allure.story("命令行入口")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestCliMain:
    """main()命令行入口验证"""

    def test_main_normal_exit_code_zero(self, demo_db, monkeypatch, capsys):
        """
        正常入口: 模拟sys.argv带--dry-run运行，
        正常完成退出（不触发sys.exit(1)）

        参数:
            demo_db (Path): 独立演示数据库fixture
            monkeypatch (pytest.MonkeyPatch): sys.argv覆写
            capsys: pytest标准输出捕获fixture

        返回:
            无
        """
        monkeypatch.setattr(
            "sys.argv",
            [
                "case_manager",
                "--file", str(DATA_FILE),
                "--dry-run",
            ],
        )
        main()  # 正常运行不应抛SystemExit

        captured = capsys.readouterr()
        assert "待执行用例列表" in captured.out

    def test_main_dry_run_with_filters(self, demo_db, monkeypatch, capsys):
        """
        组合参数入口: -p P0 -m 用户管理 -t smoke多参数组合dry-run，
        筛选后仅1条待执行用例

        参数:
            demo_db (Path): 独立演示数据库fixture
            monkeypatch (pytest.MonkeyPatch): sys.argv覆写
            capsys: pytest标准输出捕获fixture

        返回:
            无
        """
        monkeypatch.setattr(
            "sys.argv",
            [
                "case_manager",
                "-f", str(DATA_FILE),
                "-p", "P0",
                "-m", "用户管理",
                "-t", "smoke",
                "--dry-run",
            ],
        )
        main()

        captured = capsys.readouterr()
        assert "共1条" in captured.out
        assert "TM-API-0201" in captured.out

    def test_main_nonexistent_file_exits_one(self, demo_db, monkeypatch, capsys):
        """
        异常路径: --file指向不存在的文件时打印错误并sys.exit(1)

        参数:
            demo_db (Path): 独立演示数据库fixture
            monkeypatch (pytest.MonkeyPatch): sys.argv覆写
            capsys: pytest标准输出捕获fixture

        返回:
            无
        """
        monkeypatch.setattr(
            "sys.argv",
            [
                "case_manager",
                "--file", str(PROJECT_ROOT / "testdata" / "yaml" / "not_exist.yaml"),
            ],
        )
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "不存在" in captured.out


@allure.feature("用例调度管理")
@allure.story("模拟执行器")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestSimulateExecute:
    """_simulate_execute模拟执行器结果规则验证"""

    @pytest.mark.parametrize(
        "case_id, expected_result",
        [
            ("TM-API-0202", "passed"),  # 末尾2偶数→通过
            ("TM-API-0204", "passed"),  # 末尾4偶数→通过
            ("TM-API-0201", "failed"),  # 末尾1奇数→失败
            ("TM-API-0203", "failed"),  # 末尾3奇数→失败
        ],
        ids=["偶数2通过", "偶数4通过", "奇数1失败", "奇数3失败"],
    )
    def test_simulate_execute_parity_rule(self, case_id, expected_result):
        """
        奇偶规则: case_id末尾数字偶数→passed，奇数→failed，
        failed时error_message为固定模拟文案，duration为正耗时

        参数:
            case_id (str): 用例编号
            expected_result (str): 期望执行结果

        返回:
            无
        """
        case = {"case_id": case_id, "name": "模拟用例"}
        result, error_message, duration = CaseManager._simulate_execute(case)

        assert result == expected_result
        assert duration > 0
        if expected_result == "failed":
            assert error_message == "模拟执行失败: 断言不通过"
        else:
            assert error_message is None
