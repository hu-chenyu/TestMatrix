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
    优先使用真实output/allure_results/数据（pytest每轮运行自动生成）；
    目录缺失时自动降级为tmp_path构造的最小Allure结构，
    保证测试在任何环境可离线运行。
"""

import json
from pathlib import Path

import allure
import pytest

from src.core.report_analyzer import AllureResult, ReportAnalyzer

# 项目根目录（本文件位于 tests/ 下，向上一级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 真实Allure结果目录
REAL_RESULTS_DIR = PROJECT_ROOT / "output" / "allure_results"

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
    提供Allure结果目录（模块级共用）

    优先使用真实output/allure_results/（存在且非空时）；
    否则降级为tmp_path构造的最小目录（3个result+1个container+1个损坏文件），
    保证测试离线可跑。

    参数:
        tmp_path_factory (pytest.TempPathFactory): 模块级临时目录工厂

    返回:
        Path: 可用的Allure结果目录路径
    """
    if REAL_RESULTS_DIR.is_dir() and any(REAL_RESULTS_DIR.glob("*-result.json")):
        return REAL_RESULTS_DIR

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
