"""
TestMatrix 大扫除 v2 · 任务一：report_analyzer 边缘路径覆盖补齐

覆盖目标
--------
本文件针对 src/core/report_analyzer.py 的 29 条未覆盖语句（94% -> 100%）。
未覆盖清单与对应场景：

    行号       场景                                     本文件用例
    ---------  ---------------------------------------  ---------------------
    362        _merge_labels 入参非 list                test_merge_labels_non_list_input
    365        labels 数组中混入非字典元素               test_merge_labels_skips_non_dict
    369        label 缺 name 或 value 为 None           test_merge_labels_skips_incomplete
    391-392    _safe_int 无法转换的异常值               test_safe_int_unconvertible
    671        _calc_pass_rate 分母 <= 0                test_pass_rate_zero_total
    694        _calc_duration_stats 空列表              test_duration_stats_empty_list
    822        status_details 非字典时兜底为空          test_failed_details_non_dict_status
    963        save_statistics 批次号为空               test_save_statistics_rejects_empty_id
    1182-1184  全局汇总查询数据库异常                   test_overview_summary_db_error
    1276-1278  模块分布聚合数据库异常                   test_module_distribution_db_error
    1281       模块分布无数据行                         test_module_distribution_empty
    1370-1372  优先级分布聚合数据库异常                 test_priority_distribution_db_error
    1427       非标准优先级的排序兜底                   test_priority_distribution_unknown_key
    1468       get_failed_top limit 越界                test_failed_top_rejects_bad_limit
    1535-1537  失败Top聚合数据库异常                    test_failed_top_db_error
    1628-1630  质量度量查询数据库异常                   test_quality_metrics_db_error
    1666-1667  质量度量 P95 大样本分支                  test_quality_metrics_p95_large_sample

设计要点
--------
- 数据库异常用桩 session 制造：这些 except 分支的唯一入口是 DB 抛错，
  没有任何公开 API 能触达。桩只替换 DatabaseSession.get_session 的
  返回值（外部边界），不改动被测模块内部逻辑。
- 断言一律校验"异常照原样上抛且不吞掉"，不锁死日志文案全文。
- 不覆盖的语句：无。本文件 29 条目标语句全部可测。

测试铁律（对齐项目既有测试约定：数据隔离）
- 每条涉及数据库的用例独立临时 SQLite 库，teardown 严格 reset
- 无 time.sleep 固定等待，无 print
"""

from collections.abc import Iterator
from pathlib import Path

import allure
import pytest
from sqlalchemy.exc import OperationalError
from src.core.report_analyzer import (
    P95_MIN_SAMPLE_SIZE,
    AllureResult,
    ReportAnalyzer,
    ReportRepository,
    ReportStatistics,
)
from src.db import models
from src.db.db_session import DatabaseSession


# ===========================================================================
# 夹具与桩
# ===========================================================================
@pytest.fixture
def empty_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Path]:
    """
    独立临时 SQLite 库（空表，用于"无数据行"分支）

    环境准备:
        - TM_DB_TYPE=sqlite + 临时路径，reset 引擎单例后 init_db

    teardown:
        - reset 引擎（不释放句柄则临时文件删不掉）后删除库文件

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
        tmp_path (Path): pytest 临时目录

    返回:
        Iterator[Path]: yield 临时库文件路径
    """
    db_path = tmp_path / "report_analyzer_v2.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_path
    DatabaseSession.reset()
    db_path.unlink(missing_ok=True)


class _RaisingQuerySession:
    """
    桩 Session：query() 一调用就抛 SQLAlchemyError（模拟数据库不可用）

    为什么用桩而不是真造坏库: 这些 except 分支要验的是"DB 异常时
    记 error 日志后原样上抛、不静默吞掉"。真造坏库需要动文件权限或
    换驱动，跨平台不可靠且会污染其他用例的引擎单例。

    close() 保留为 no-op：被测代码在 finally 里会调它，桩缺这个方法
    会让 AttributeError 顶替掉我们要验的 SQLAlchemyError。
    """

    def __init__(self) -> None:
        """初始化桩（无状态）"""
        self.closed = False

    def query(self, *_args, **_kwargs):
        """任何查询都抛 SQLAlchemyError"""
        raise OperationalError("SELECT 1", {}, Exception("模拟数据库不可用"))

    def close(self) -> None:
        """关闭为 no-op（保留被测代码 finally 调用的语义）"""
        self.closed = True


@pytest.fixture
def db_error_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    让 DatabaseSession.get_session 返回抛错桩 Session 的夹具

    teardown:
        monkeypatch 自动恢复真实实现

    参数:
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

    返回:
        Iterator[None]: yield None（仅用于设置环境）
    """
    monkeypatch.setattr(
        DatabaseSession, "get_session", staticmethod(lambda: _RaisingQuerySession())
    )
    yield


def _make_result(
    case_id: str,
    status: str = "passed",
    duration_ms: int = 100,
    **kwargs,
) -> AllureResult:
    """构造一个 AllureResult（内部工具）"""
    return AllureResult(
        uuid=f"uuid-{case_id}",
        name=f"用例{case_id}",
        full_name=f"tests.demo.TestX#{case_id}",
        status=status,
        start=1_000,
        stop=1_000 + duration_ms,
        **kwargs,
    )


# ===========================================================================
# A组: 标签合并与安全整数转换（纯函数）
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("report_analyzer 标签与类型归一")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestReportAnalyzerNormalization:
    """labels 脏数据 / 时间戳异常值 / 空集合聚合"""

    def test_merge_labels_non_list_input(self) -> None:
        """
        labels 入参非 list 时返回空字典（行362）

        Allure 结果文件里 labels 写成字典/字符串时不能崩——整批统计
        直接抛异常会让一次损坏的产物毁掉整个报告。
        """
        for bad_input in (None, {"name": "owner"}, "owner=alice", 123):
            assert ReportAnalyzer._merge_labels(bad_input) == {}, (
                f"非list输入 {bad_input!r} 应返回空字典而非抛异常"
            )

    def test_merge_labels_skips_non_dict(self) -> None:
        """
        labels 数组中混入非字典元素时跳过（行365）

        只跳过不抛异常：一条脏 label 不该让整份报告失败。
        """
        merged = ReportAnalyzer._merge_labels(
            ["owner=alice", 123, None, {"name": "owner", "value": "bob"}]
        )

        assert merged == {"owner": ["bob"]}, "只应保留合法的字典项"

    def test_merge_labels_skips_incomplete(self) -> None:
        """
        缺 name 或 value 为 None 的 label 被跳过（行369）

        value 为 None 是 Allure 真实会产出的形态（label 有名字无值）。
        保留它会得到 {"owner": ["None"]} 这种假负责人，通知渠道会
        去 @ 一个叫 "None" 的人。
        """
        merged = ReportAnalyzer._merge_labels(
            [
                {"value": "无名字"},
                {"name": "owner"},
                {"name": "owner", "value": "alice"},
                {"name": "owner", "value": None},
                {"name": "", "value": "空名字"},
            ]
        )

        assert merged == {"owner": ["alice"]}, (
            "缺 name 或 value 为 None 的 label 都应被丢弃"
        )

    def test_merge_labels_merges_same_name(self) -> None:
        """
        同名 label 的多个值合并为列表（正向对照）

        证明上一条拦的是"残缺项"，没有把多值合并能力改掉。
        """
        merged = ReportAnalyzer._merge_labels(
            [
                {"name": "tag", "value": "smoke"},
                {"name": "tag", "value": "regression"},
            ]
        )

        assert merged == {"tag": ["smoke", "regression"]}

    @pytest.mark.parametrize(
        ("raw_value", "expected"),
        [
            (None, 0),
            (True, 0),
            (False, 0),
            ("not-a-number", 0),
            ([], 0),
            ({}, 0),
            (1700000000000, 1700000000000),
            ("1700000000000", 1700000000000),
        ],
        ids=["none", "true", "false", "str-nan", "empty-list", "empty-dict",
             "int", "numeric-str"],
    )
    def test_safe_int_unconvertible(
        self, raw_value: object, expected: int
    ) -> None:
        """
        _safe_int 对无法转换的值返回 0 而非抛异常（行391-392）

        bool 单列一条：bool 是 int 子类，不单独拦的话 True 会变成 1，
        把"时间戳缺失"伪装成"1970年的执行"。
        """
        assert ReportAnalyzer._safe_int(raw_value) == expected

    def test_pass_rate_zero_total(self) -> None:
        """
        通过率分母 <= 0 时返回 0.0（行671）

        空批次的通过率若不特判会 ZeroDivisionError，把"没有数据"变成
        500。负分母同样兜住（口径同 6.3 统计铁律）。
        """
        assert ReportStatistics._calc_pass_rate(0, 0) == 0.0
        assert ReportStatistics._calc_pass_rate(5, 0) == 0.0
        assert ReportStatistics._calc_pass_rate(1, -3) == 0.0, (
            "负分母同样应兜底为 0.0，而不是抛 ZeroDivisionError"
        )
        assert ReportStatistics._calc_pass_rate(1, 2) == 0.5, "正常分母仍正常计算"

    def test_duration_stats_empty_list(self) -> None:
        """
        耗时统计传入空列表时全部指标为 0（行694）

        空批次的 avg/max/min/p95 若走通用算法会因空序列抛异常。
        """
        assert ReportStatistics._calc_duration_stats([]) == {
            "total": 0,
            "avg": 0.0,
            "max": 0,
            "min": 0,
            "p95": 0.0,
        }


# ===========================================================================
# B组: 失败明细提取的脏数据兜底
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("report_analyzer 失败明细提取")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestFailedDetailExtraction:
    """status_details 形态异常时兜底"""

    def test_failed_details_non_dict_status(self) -> None:
        """
        status_details 不是字典时兜底为空字典（行822）

        提取失败明细时会 .get() 取 message/trace，非字典输入（如
        Allure 偶发把 statusDetails 写成字符串）会抛 AttributeError，
        整份失败报告随之生成不出来。
        """
        results = [
            _make_result(
                "TM-RA-0001",
                status="failed",
                status_details="这是一段纯文本错误信息",
            ),
            _make_result(
                "TM-RA-0002", status="broken", status_details=["列表形态"]
            ),
        ]

        details = ReportStatistics._extract_failed_details(results)

        assert len(details) == 2, "非字典 status_details 不得中断明细提取"
        assert details[0].error_message == "", "异常形态应兜底为空串"
        assert details[1].error_message == "", "异常形态应兜底为空串"

    def test_failed_details_skips_passed(self) -> None:
        """
        只有 failed/broken 进入明细，passed/skipped 被排除

        与上一条成对，证明上一条不是因为"全收"才凑够 2 条。
        """
        results = [
            _make_result("TM-RA-0003", status="passed"),
            _make_result("TM-RA-0004", status="skipped"),
            _make_result("TM-RA-0005", status="failed",
                         status_details={"message": "断言失败"}),
        ]

        details = ReportStatistics._extract_failed_details(results)

        assert [item.name for item in details] == ["用例TM-RA-0005"]


# ===========================================================================
# C组: 入库前置校验
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("report_analyzer 统计入库")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestSaveStatisticsValidation:
    """批次号为空时拒绝入库"""

    def test_save_statistics_rejects_empty_id(self, empty_db: Path) -> None:
        """
        批次号为空必须显式拒绝（行963）

        批次号是 defect_statistics 的唯一键，空值会让所有批次的统计
        挤进同一行/或触发唯一约束异常，错误信息完全指不到真因。
        """
        empty_stat = ReportStatistics.aggregate([])

        for bad_id in ("", "   "):
            with pytest.raises(ValueError, match="执行批次号不能为空"):
                ReportRepository.save_statistics(empty_stat, bad_id)

    def test_save_statistics_accepts_valid_id(self, empty_db: Path) -> None:
        """
        合法批次号正常入库（正向对照）

        与上一条成对，证明拒绝的是"空批次号"而非"入库"本身。
        """
        empty_stat = ReportStatistics.aggregate([])

        ReportRepository.save_statistics(empty_stat, "RUN-V2-0001", remark="v2测试")

        latest = ReportRepository.get_latest_statistics()
        assert latest, "合法批次号应成功入库"
        # get_latest_statistics 返回 ORM 对象列表而非字典
        assert latest[0].execution_id == "RUN-V2-0001"


# ===========================================================================
# D组: 聚合查询的数据库异常与空结果
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("report_analyzer 聚合查询异常")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestAggregationDbErrors:
    """SQLAlchemyError 照原样上抛 / 空表返回空结构"""

    def test_overview_summary_db_error(self, db_error_session: None) -> None:
        """
        全局汇总查询 DB 异常时原样上抛（行1182-1184）

        绝不能吞掉：DB 故障时若返回全 0，看板会显示"通过率 0%"，
        用户会以为测试全挂了而实际只是数据库连不上。
        """
        with pytest.raises(OperationalError):
            ReportRepository.get_overview_summary()

    def test_module_distribution_db_error(self, db_error_session: None) -> None:
        """
        模块分布聚合 DB 异常时原样上抛（行1276-1278）
        """
        with pytest.raises(OperationalError):
            ReportRepository.get_module_distribution()

    def test_priority_distribution_db_error(self, db_error_session: None) -> None:
        """
        优先级分布聚合 DB 异常时原样上抛（行1370-1372）
        """
        with pytest.raises(OperationalError):
            ReportRepository.get_priority_distribution()

    def test_failed_top_db_error(self, db_error_session: None) -> None:
        """
        失败Top聚合 DB 异常时原样上抛（行1535-1537）

        若吞掉返回空列表，看板"失败Top"会显示"暂无失败"，与真实的
        数据库故障完全相反，排查方向被彻底带偏。
        """
        with pytest.raises(OperationalError):
            ReportRepository.get_failed_top(limit=5)

    def test_quality_metrics_db_error(self, db_error_session: None) -> None:
        """
        质量度量查询 DB 异常时原样上抛（行1628-1630）
        """
        with pytest.raises(OperationalError):
            ReportRepository.get_quality_metrics()

    def test_module_distribution_empty(self, empty_db: Path) -> None:
        """
        无执行记录时模块分布返回空列表（行1281）

        空表返回 [] 而非 [{module: None, total: 0}]：前者前端渲染
        "暂无数据"，后者会显示一行全 0 的假模块数据。
        """
        assert ReportRepository.get_module_distribution() == []

    def test_priority_distribution_empty(self, empty_db: Path) -> None:
        """
        无执行记录时优先级分布返回空列表（对照组）
        """
        assert ReportRepository.get_priority_distribution() == []


# ===========================================================================
# E组: 失败Top 入参与优先级排序
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("report_analyzer 失败Top与优先级排序")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.regression
class TestFailedTopAndPriorityOrder:
    """limit 越界拒绝 / 非标准优先级排序兜底"""

    @pytest.mark.parametrize(
        "bad_limit",
        [0, -1, 101, 1000, True, False, "10", 3.7, None],
        ids=["zero", "negative", "over-100", "way-over", "true", "false",
             "str", "float", "none"],
    )
    def test_failed_top_rejects_bad_limit(
        self, empty_db: Path, bad_limit: object
    ) -> None:
        """
        get_failed_top 的 limit 越界/类型非法必须拒绝（行1468）

        显式拒绝而非静默夹到默认值的理由同项目设计决策：不报错会让"传错参数"
        和"真的只有 10 条"在响应里长得一模一样。
        bool 单独拦：bool 是 int 子类，True 会被当成 limit=1 放行。
        """
        with pytest.raises(ValueError, match="limit必须在1到100之间"):
            ReportRepository.get_failed_top(bad_limit)  # type: ignore[arg-type]

    def test_failed_top_accepts_boundary_limits(self, empty_db: Path) -> None:
        """
        limit 取边界值 1 与 100 均合法（正向对照）
        """
        assert ReportRepository.get_failed_top(limit=1) == []
        assert ReportRepository.get_failed_top(limit=100) == []

    def test_priority_distribution_unknown_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        非标准优先级（如历史遗留的 "HIGH"）排在标准 P0-P3 之后、
        未知占位之前（行1427）

        直接构造含非标准值的数据文件入库：优先级列在模型上无
        CheckConstraint（属需迁移的 backlog），脏值确实可能进库，
        排序必须有兜底而不是 KeyError。
        """
        db_path = tmp_path / "priority_sort.db"
        monkeypatch.setenv("TM_DB_TYPE", "sqlite")
        monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
        DatabaseSession.reset()
        DatabaseSession.init_db()

        try:
            with DatabaseSession.session_scope() as session:
                for case_id, priority in [
                    ("TM-RA-P0", "P0"),
                    ("TM-RA-P1", "P1"),
                    ("TM-RA-UNKNOWN", "unknown"),
                    ("TM-RA-HIGH", "HIGH"),
                ]:
                    session.add(
                        models.TestCase(
                            case_id=case_id,
                            name=f"用例{case_id}",
                            module="用户中心",
                            priority=priority,
                            case_type="api",
                            status="active",
                            description="优先级排序兜底测试",
                            creator="admin",
                        )
                    )
                    session.add(
                        models.TestExecution(
                            execution_id="RUN-V2-SORT",
                            case_id=case_id,
                            case_name=f"用例{case_id}",
                            result="passed",
                            start_time=None,
                            end_time=None,
                            duration=0.1,
                            error_message=None,
                        )
                    )

            distribution = ReportRepository.get_priority_distribution()
            order = [item["priority"] for item in distribution]

            assert order[:2] == ["P0", "P1"], "标准优先级须按 P0->P3 在最前"
            assert order[-1] == "unknown", "未知占位值须排在所有具体值之后"
            assert order.index("HIGH") < order.index("unknown"), (
                f"非标准具体值应排在 unknown 之前，实际顺序: {order}"
            )
        finally:
            DatabaseSession.reset()
            db_path.unlink(missing_ok=True)


# ===========================================================================
# F组: 质量度量的 P95 大样本分支
# ===========================================================================
@allure.feature("大扫除v2覆盖率补齐")
@allure.story("report_analyzer 质量度量 P95")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.regression
class TestQualityMetricsP95:
    """样本 >= 阈值时走真百分位算法"""

    def test_quality_metrics_p95_large_sample(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        样本数 >= P95_MIN_SAMPLE_SIZE 时用 ceil(0.95*n)-1 取真百分位
        （行1666-1667），不足时取最大值近似

        两种口径的差别在 n=20 时体现得最清楚：
        - 近似口径取 durations[-1]（最大值）
        - 真百分位取 durations[ceil(0.95*20)-1] = durations[18]（次大）
        故造 20 条递增耗时，断言 p95 落在次大而非最大。
        """
        db_path = tmp_path / "quality_p95.db"
        monkeypatch.setenv("TM_DB_TYPE", "sqlite")
        monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
        DatabaseSession.reset()
        DatabaseSession.init_db()

        try:
            with DatabaseSession.session_scope() as session:
                for index in range(P95_MIN_SAMPLE_SIZE):
                    case_id = f"TM-RA-DUR-{index:03d}"
                    session.add(
                        models.TestCase(
                            case_id=case_id,
                            name=f"耗时用例{index}",
                            module="用户中心",
                            priority="P1",
                            case_type="api",
                            status="active",
                            description="P95大样本分支",
                            creator="admin",
                        )
                    )
                    session.add(
                        models.TestExecution(
                            execution_id="RUN-V2-P95",
                            case_id=case_id,
                            case_name=f"耗时用例{index}",
                            result="passed",
                            start_time=None,
                            end_time=None,
                            # 1,2,...,20 秒，升序
                            duration=float(index + 1),
                            error_message=None,
                        )
                    )

            metrics = ReportRepository.get_quality_metrics()
            efficiency = metrics["execution_efficiency"]

            assert efficiency["p95_duration_sec"] == 19.0, (
                "n=20 的真百分位应为 durations[ceil(0.95*20)-1]=durations[18]=19.0；"
                f"若等于 20.0 说明走了小样本近似分支。实际: "
                f"{efficiency['p95_duration_sec']}"
            )
            assert efficiency["avg_duration_sec"] == 10.5, (
                "1..20 的算术平均为 10.5，可作为数据确实落库的旁证"
            )
        finally:
            DatabaseSession.reset()
            db_path.unlink(missing_ok=True)
