"""
report_analyzer解析器演示与验证用例（第二阶段Day6）

验证目标:
    1. scan_results_dir: 只返回*-result.json（排除container）、不存在目录抛异常
    2. parse_result_file: 单文件解析字段正确、duration_ms计算、labels合并
    3. 容错: 缺失字段默认值、非法JSON跳过不中断
    4. parse_results_dir: 批量解析数量与类型正确
    5. get_by_status/get_failed_results: 状态筛选准确性
    6. AllureResult: 直接构造对象字段与duration_ms属性

数据说明:
    全部用例只使用 tmp_path_factory 构造的隔离临时目录与固定样本文件，
    绝不引用 output/allure_results/（该目录是当轮 pytest --alluredir 的
    实时写入目录，边跑边长会导致 glob 计数竞态），保证任何环境离线可跑、
    全量连跑结果确定。
"""

import json
from pathlib import Path

import allure
import pytest
from src.core.report_analyzer import (
    AllureResult,
    BridgeResult,
    ReportAnalyzer,
    ReportRepository,
    bridge_results_dir,
)
from src.db.db_session import DatabaseSession

# 最小可用Allure结果JSON模板（覆盖全部核心字段）
SAMPLE_RESULT = {
    "uuid": "aaaa1111-2222-3333-4444-555566667777",
    "name": "test_login_success",
    "fullName": "tests.api_demo.test_login.TestLogin#test_login_success",
    "status": "passed",
    "description": "登录成功场景验证",
    "start": 1787800000000,
    "stop": 1787800001500,
    "historyId": "abc123def456",
    "labels": [
        {"name": "severity", "value": "critical"},
        {"name": "feature", "value": "用户管理"},
        {"name": "tag", "value": "api"},
        {"name": "tag", "value": "smoke"},
    ],
    "parameters": [{"name": "username", "value": "admin"}],
}

# 失败状态结果模板（含statusDetails）
FAILED_RESULT = {
    "uuid": "bbbb1111-2222-3333-4444-555566667777",
    "name": "test_login_wrong_password",
    "fullName": "tests.api_demo.test_login.TestLogin#test_login_wrong_password",
    "status": "failed",
    "start": 1787800002000,
    "stop": 1787800002100,
    "labels": [{"name": "severity", "value": "normal"}],
    "statusDetails": {
        "message": "AssertionError: 业务码期望0实际2001",
        "trace": "AssertionError ...",
    },
}

# broken状态结果模板
BROKEN_RESULT = {
    "uuid": "cccc1111-2222-3333-4444-555566667777",
    "name": "test_query_timeout",
    "fullName": "tests.api_demo.test_query.TestQuery#test_query_timeout",
    "status": "broken",
    "start": 1787800003000,
    "stop": 1787800003200,
    "labels": [],
}


@pytest.fixture(scope="module")
def results_dir(tmp_path_factory) -> Path:
    """
    提供Allure结果目录（模块级共用，隔离临时目录）

    永远使用 tmp_path_factory 构造的独立临时目录，写入固定样本
    （3个result+1个container）；不得引用 output/allure_results/——
    那是本轮 pytest --alluredir 的实时写入目录，全量跑时文件持续增加，
    两次 glob 之间落入新文件会导致计数断言间歇性失败。

    参数:
        tmp_path_factory (pytest.TempPathFactory): 模块级临时目录工厂

    返回:
        Path: 内容固定的Allure结果临时目录路径
    """
    tmp_dir = tmp_path_factory.mktemp("allure_results")
    for sample in (SAMPLE_RESULT, FAILED_RESULT, BROKEN_RESULT):
        file_name = f"{sample['uuid']}-result.json"
        (tmp_dir / file_name).write_text(
            json.dumps(sample), encoding="utf-8"
        )
    # container文件（应被scan排除）
    (tmp_dir / "container-0000-1111-container.json").write_text("{}", encoding="utf-8")
    return tmp_dir


@allure.feature("报告解析引擎")
@allure.story("目录扫描")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestScanResultsDir:
    """scan_results_dir结果目录扫描验证"""

    def test_scan_returns_only_result_files(self, results_dir):
        """
        扫描过滤: 返回的全部为*-result.json路径，
        不包含*-container.json步骤容器文件

        参数:
            results_dir (Path): Allure结果目录fixture

        返回:
            无
        """
        result_files = ReportAnalyzer.scan_results_dir(results_dir)

        assert len(result_files) > 0
        assert all(file_path.endswith("-result.json") for file_path in result_files)
        assert all("container" not in Path(file_path).name for file_path in result_files)

    def test_scan_count_matches_glob(self, results_dir):
        """
        扫描完整性: scan返回数量与目录glob统计的*-result.json数量一致

        参数:
            results_dir (Path): Allure结果目录fixture

        返回:
            无
        """
        result_files = ReportAnalyzer.scan_results_dir(results_dir)
        glob_count = len(list(results_dir.glob("*-result.json")))
        assert len(result_files) == glob_count

    def test_scan_nonexistent_dir_raises(self, tmp_path):
        """
        异常路径: 不存在的目录抛FileNotFoundError

        参数:
            tmp_path (Path): pytest临时目录fixture

        返回:
            无
        """
        with pytest.raises(FileNotFoundError):
            ReportAnalyzer.scan_results_dir(tmp_path / "not_exist_dir")


@allure.feature("报告解析引擎")
@allure.story("单文件解析")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestParseResultFile:
    """parse_result_file单文件解析验证"""

    def test_parse_single_file_fields(self, results_dir):
        """
        字段解析: 从真实目录取一个文件解析，
        name/status/uuid字段非空且与JSON原始值一致

        参数:
            results_dir (Path): Allure结果目录fixture

        返回:
            无
        """
        result_file = ReportAnalyzer.scan_results_dir(results_dir)[0]
        with open(result_file, encoding="utf-8") as file_handle:
            raw_data = json.load(file_handle)

        result = ReportAnalyzer.parse_result_file(result_file)

        assert result.name == raw_data["name"]
        assert result.status == raw_data["status"]
        assert result.uuid == raw_data["uuid"]

    def test_parse_sample_duration_and_labels(self, tmp_path):
        """
        标准模板解析: duration_ms=stop-start；
        labels数组转字典且同name合并（tag两个值）

        参数:
            tmp_path (Path): pytest临时目录fixture

        返回:
            无
        """
        file_path = tmp_path / f"{SAMPLE_RESULT['uuid']}-result.json"
        file_path.write_text(json.dumps(SAMPLE_RESULT), encoding="utf-8")

        result = ReportAnalyzer.parse_result_file(file_path)

        assert result.duration_ms == SAMPLE_RESULT["stop"] - SAMPLE_RESULT["start"]
        assert result.full_name == SAMPLE_RESULT["fullName"]
        assert result.history_id == SAMPLE_RESULT["historyId"]
        # labels转换与合并
        assert result.labels["severity"] == ["critical"]
        assert result.labels["feature"] == ["用户管理"]
        assert sorted(result.labels["tag"]) == ["api", "smoke"]
        assert result.get_label("severity") == ["critical"]
        assert result.parameters == [{"name": "username", "value": "admin"}]
        assert result.status_details is None

    def test_parse_missing_fields_defaults(self, tmp_path):
        """
        缺失字段容错: 仅含name的最小JSON解析不抛异常，
        status默认unknown、start/stop默认0、duration_ms为0

        参数:
            tmp_path (Path): pytest临时目录fixture

        返回:
            无
        """
        minimal = {"name": "test_minimal", "uuid": "dddd-0000"}
        file_path = tmp_path / "minimal-result.json"
        file_path.write_text(json.dumps(minimal), encoding="utf-8")

        result = ReportAnalyzer.parse_result_file(file_path)

        assert result.name == "test_minimal"
        assert result.status == "unknown"
        assert result.start == 0
        assert result.stop == 0
        assert result.duration_ms == 0
        assert result.labels == {}
        assert result.parameters == []
        assert result.status_details is None

    def test_parse_invalid_json_skipped_in_batch(self, tmp_path):
        """
        非法JSON容错: 目录含损坏文件时批量解析跳过该文件，
        不抛异常且成功数正确

        参数:
            tmp_path (Path): pytest临时目录fixture

        返回:
            无
        """
        (tmp_path / "good-result.json").write_text(
            json.dumps(SAMPLE_RESULT), encoding="utf-8"
        )
        (tmp_path / "bad-result.json").write_text(
            "{not a valid json!!!", encoding="utf-8"
        )

        results = ReportAnalyzer.parse_results_dir(tmp_path)

        assert len(results) == 1
        assert results[0].name == SAMPLE_RESULT["name"]

    def test_parse_null_fields_fallback(self, tmp_path):
        """
        null值容错: 字段显式为null时不得到字符串"None"，
        status回退unknown、其余字符串字段回退空串、时间戳回退0

        参数:
            tmp_path (Path): pytest临时目录fixture

        返回:
            无
        """
        null_fields = {
            "uuid": None, "name": None, "fullName": None,
            "status": None, "description": None, "historyId": None,
            "start": None, "stop": None,
        }
        file_path = tmp_path / "null-fields-result.json"
        file_path.write_text(json.dumps(null_fields), encoding="utf-8")

        result = ReportAnalyzer.parse_result_file(file_path)

        assert result.status == "unknown"
        assert result.uuid == ""
        assert result.name == ""
        assert result.full_name == ""
        assert result.history_id == ""
        assert result.start == 0
        assert result.stop == 0

    def test_parse_non_dict_top_level_skipped_in_batch(self, tmp_path):
        """
        顶层结构容错: 合法JSON但顶层为列表时，
        单文件解析抛ValueError、批量解析跳过该文件且不拖垮同批正常文件

        参数:
            tmp_path (Path): pytest临时目录fixture

        返回:
            无
        """
        # 顶层是列表（合法JSON但非JSON对象）
        (tmp_path / "list-top-result.json").write_text(
            json.dumps([{"status": "passed"}]), encoding="utf-8"
        )
        # 同目录放一个正常文件，证明整批解析不被畸形文件拖垮
        (tmp_path / "good-result.json").write_text(
            json.dumps(SAMPLE_RESULT), encoding="utf-8"
        )

        with pytest.raises(ValueError):
            ReportAnalyzer.parse_result_file(tmp_path / "list-top-result.json")

        results = ReportAnalyzer.parse_results_dir(tmp_path)
        assert len(results) == 1
        assert results[0].name == SAMPLE_RESULT["name"]


@allure.feature("报告解析引擎")
@allure.story("批量解析")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestParseResultsDir:
    """parse_results_dir批量解析验证"""

    def test_batch_parse_types_and_count(self, results_dir):
        """
        批量解析: 返回数量>0、全部为AllureResult实例，
        且数量等于目录中*-result.json文件数

        参数:
            results_dir (Path): Allure结果目录fixture

        返回:
            无
        """
        results = ReportAnalyzer.parse_results_dir(results_dir)
        file_count = len(ReportAnalyzer.scan_results_dir(results_dir))

        assert len(results) > 0
        assert len(results) == file_count
        assert all(isinstance(result, AllureResult) for result in results)

    def test_batch_parse_constructed_dir(self, tmp_path):
        """
        构造目录批量解析: 3个result（passed/failed/broken）全解析成功，
        container文件不参与

        参数:
            tmp_path (Path): pytest临时目录fixture

        返回:
            无
        """
        for sample in (SAMPLE_RESULT, FAILED_RESULT, BROKEN_RESULT):
            file_name = f"{sample['uuid']}-result.json"
            (tmp_path / file_name).write_text(json.dumps(sample), encoding="utf-8")

        results = ReportAnalyzer.parse_results_dir(tmp_path)

        assert len(results) == 3
        statuses = {result.status for result in results}
        assert statuses == {"passed", "failed", "broken"}


@allure.feature("报告解析引擎")
@allure.story("状态筛选")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestStatusFilter:
    """get_by_status/get_failed_results状态筛选验证"""

    @pytest.fixture()
    def mixed_results(self) -> list:
        """
        构造混合状态结果列表（2passed+1failed+1broken+1skipped）

        返回:
            List[AllureResult]: 混合状态的结果对象列表
        """
        return [
            AllureResult(uuid="u1", name="case1", status="passed", start=100, stop=200),
            AllureResult(uuid="u2", name="case2", status="passed", start=100, stop=150),
            AllureResult(uuid="u3", name="case3", status="failed",
                         status_details={"message": "断言失败"}),
            AllureResult(uuid="u4", name="case4", status="broken",
                         status_details={"message": "环境异常"}),
            AllureResult(uuid="u5", name="case5", status="skipped"),
        ]

    def test_filter_by_status_passed(self, mixed_results):
        """
        按状态筛选: passed命中2条且均为passed状态

        参数:
            mixed_results (List[AllureResult]): 混合状态结果fixture

        返回:
            无
        """
        passed = ReportAnalyzer.get_by_status(mixed_results, "passed")
        assert len(passed) == 2
        assert all(result.status == "passed" for result in passed)

    def test_get_failed_includes_failed_and_broken(self, mixed_results):
        """
        失败筛选: failed+broken均视为失败命中2条，
        passed/skipped不计入

        参数:
            mixed_results (List[AllureResult]): 混合状态结果fixture

        返回:
            无
        """
        failed = ReportAnalyzer.get_failed_results(mixed_results)
        assert len(failed) == 2
        assert {result.status for result in failed} == {"failed", "broken"}


@allure.feature("报告解析引擎")
@allure.story("数据模型")
@allure.severity(allure.severity_level.NORMAL)
@pytest.mark.api
@pytest.mark.regression
class TestAllureResultModel:
    """AllureResult数据模型直接构造验证"""

    def test_construct_allure_result(self):
        """
        直接构造: 全字段赋值与duration_ms属性计算正确，
        默认构造字段为出厂默认值

        参数:
            无

        返回:
            无
        """
        result = AllureResult(
            uuid="uuid-001",
            name="test_construct",
            full_name="tests.demo#test_construct",
            status="failed",
            description="模型构造验证",
            start=1000,
            stop=3500,
            history_id="hist-001",
            labels={"tag": ["api"], "severity": ["critical"]},
            parameters=[{"name": "env", "value": "dev"}],
            status_details={"message": "断言失败", "trace": "..."},
        )
        assert result.uuid == "uuid-001"
        assert result.duration_ms == 2500
        assert result.get_label("tag") == ["api"]
        assert result.get_label("suite") == []

        # 默认构造: 可变字段独立（dataclass field default_factory）
        default_result = AllureResult()
        default_result.labels["tag"] = ["x"]
        assert AllureResult().labels == {}


# ---------------------------------------------------------------------------
# Day50 任务二：Allure 结果桥接（解析 → 聚合 → 入库）
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """
    临时SQLite数据库fixture（桥接入库用例独享干净数据库）

    与 tests/test_report_repository_demo.py 用同一套隔离手法：
    monkeypatch 覆盖环境变量 → reset 引擎单例 → init_db 建表，
    用例结束后 reset 还原，绝不触碰项目真实库。

    参数:
        tmp_path (Path): pytest 临时目录fixture
        monkeypatch (pytest.MonkeyPatch): 环境变量覆写fixture

    返回:
        Generator[Path, None, None]: yield 临时数据库文件路径
    """
    db_file = tmp_path / "test_bridge.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_file))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_file
    DatabaseSession.reset()


@allure.feature("报告解析引擎")
@allure.story("Day50 结果桥接")
@allure.severity(allure.severity_level.CRITICAL)
@pytest.mark.api
@pytest.mark.regression
class TestBridgeResultsDir:
    """bridge_results_dir: 解析→聚合→入库全链路"""

    def test_bridge_parses_aggregates_and_saves(self, tmp_path, temp_db):
        """
        端到端：tmp_path 构造2个Allure结果文件 → 桥接 → 解析数/统计/入库
        三者必须一致

        本例顺带钉死库表字段映射口径（failed 剔除 broken、broken 落
        error 列）：桥接是这条口径在真实执行链路上的唯一入口，映射错
        了 Web 报告的"失败数"就会与真实情况系统性偏差。

        参数:
            tmp_path (Path): 用例级临时目录fixture
            temp_db (Generator): 临时SQLite库fixture
        """
        allure_dir = tmp_path / "allure_results_case_0001"
        allure_dir.mkdir()
        for sample in (SAMPLE_RESULT, BROKEN_RESULT):
            (allure_dir / f"{sample['uuid']}-result.json").write_text(
                json.dumps(sample), encoding="utf-8"
            )

        bridge = bridge_results_dir(
            allure_dir, "RUN-DAY50-0001", remark="pytest执行器桥接冒烟"
        )

        assert isinstance(bridge, BridgeResult), "桥接必须返回 BridgeResult 三态对象"
        assert bridge.ok is True, f"桥接不应报错: {bridge.error}"
        assert bridge.has_result is True
        assert bridge.parsed_count == 2
        assert bridge.statistics is not None
        assert bridge.statistics.total == 2
        assert bridge.statistics.passed == 1
        assert bridge.statistics.broken == 1

        # 入库校验：走真实查询接口，而不是直接摸表
        record = ReportRepository.get_by_execution_id("RUN-DAY50-0001")
        assert record is not None, "桥接后必须能在 defect_statistics 查到记录"
        assert record.total_cases == 2
        assert record.passed == 1
        assert record.error == 1, "broken 必须映射到 error 列"
        assert record.failed == 0, "failed 列是剔除 broken 后的纯断言失败数"
        assert record.remark == "pytest执行器桥接冒烟"

    def test_bridge_skips_missing_directory(self, tmp_path):
        """
        目录不存在时返回"无结果"且**不算错误**

        pytest 没产出 Allure 结果是常态（被测文件里没有测试、退出码 4
        用法错误等）；把它当异常会让每次这类执行都留下一条红色错误日志。

        参数:
            tmp_path (Path): 用例级临时目录fixture
        """
        bridge = bridge_results_dir(tmp_path / "not_exist", "RUN-DAY50-0002")

        assert bridge.ok is True
        assert bridge.has_result is False
        assert bridge.statistics is None
        assert bridge.error == ""

    def test_bridge_skips_directory_without_results(self, tmp_path):
        """
        目录存在但无 *-result.json 时同样按"无结果"处理

        同时覆盖"结果文件全部损坏"的相邻分支：文件在但解析不出来，
        也不能抛异常——那会让单条用例的脏结果升级成整批判 failed。

        参数:
            tmp_path (Path): 用例级临时目录fixture
        """
        empty_dir = tmp_path / "allure_empty"
        empty_dir.mkdir()

        bridge = bridge_results_dir(empty_dir, "RUN-DAY50-0003")
        assert bridge.ok is True
        assert bridge.has_result is False

        broken_dir = tmp_path / "allure_broken"
        broken_dir.mkdir()
        (broken_dir / "bad-result.json").write_text("{不是合法 JSON", encoding="utf-8")

        broken_bridge = bridge_results_dir(broken_dir, "RUN-DAY50-0004")
        assert broken_bridge.ok is True
        assert broken_bridge.has_result is False

    def test_bridge_reports_duplicate_execution_id(self, tmp_path, temp_db):
        """
        批次号重复时如实返回失败原因，绝不静默写入第二行

        defect_statistics.execution_id 有唯一约束且 save_statistics 刻意
        不做静默更新（批次唯一性由调用方保证）。桥接层若把 IntegrityError
        吞掉假装成功，调用方就会以为统计已入库——那比报错糟得多。

        参数:
            tmp_path (Path): 用例级临时目录fixture
            temp_db (Generator): 临时SQLite库fixture
        """
        allure_dir = tmp_path / "allure_results_dup"
        allure_dir.mkdir()
        (allure_dir / f"{SAMPLE_RESULT['uuid']}-result.json").write_text(
            json.dumps(SAMPLE_RESULT), encoding="utf-8"
        )

        first = bridge_results_dir(allure_dir, "RUN-DAY50-DUP")
        assert first.ok is True and first.has_result is True

        second = bridge_results_dir(allure_dir, "RUN-DAY50-DUP")
        assert second.ok is False, "重复批次号必须报告失败"
        assert "IntegrityError" in second.error
        assert second.has_result is False

    def test_bridge_reports_empty_execution_id(self, tmp_path, temp_db):
        """
        空批次号必须被拒（save_statistics 对空串抛 ValueError）

        桥接层不能把"批次号缺失"悄悄吞掉变成"没入库但也没报错"——
        执行记录与报告统计的关联键丢了，是排障时最难查的一类问题。

        参数:
            tmp_path (Path): 用例级临时目录fixture
            temp_db (Generator): 临时SQLite库fixture
        """
        allure_dir = tmp_path / "allure_results_no_exec"
        allure_dir.mkdir()
        (allure_dir / f"{SAMPLE_RESULT['uuid']}-result.json").write_text(
            json.dumps(SAMPLE_RESULT), encoding="utf-8"
        )

        bridge = bridge_results_dir(allure_dir, "")

        assert bridge.ok is False
        assert "ValueError" in bridge.error
        assert bridge.has_result is False
