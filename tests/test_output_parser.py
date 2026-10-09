"""
Day50 执行器输出解析与 Allure 目录隔离测试

验证目标:
    1. 解析器: 汇总行数字、失败明细、诊断尾部回溯、边界场景、进度百分比
    2. Allure 目录隔离: 独立目录生成、命令行注入位置、陈旧目录清理
    3. 桥接调用: 批次号推导、解析兜底、桥接三态透传

为什么第 2/3 部分放在本文件（而不是新建或改 test_pytest_runner.py）:
    当日白名单只允许改 `tests/test_output_parser.py` 与
    `tests/test_report_analyzer_demo.py` 两个测试文件，而目录隔离正是
    Day50 任务二的交付物。把它并进本文件并在此说明，比擅自扩白名单
    更守边界；也让「输出解析 + 结果目录隔离 + 桥接」这一条链路的首尾
    落在同一处可读。

数据说明:
    解析用例只使用**手工构造的 pytest 输出文本片段**（形状取自
    `pytest -q --tb=short` 的真实输出），**不启动真实 pytest 子进程**——
    真跑一次 pytest 冷启动就要 1.5 秒以上（Day45 POC 实测），而本文件
    要验证的是"文本解析口径"，跑真进程既慢又让失败原因难定位。

    同时**刻意不引用** output/allure_results/ 目录下的真实报告：那目录
    会随 Day50 引入的独立结果目录机制持续变化，用它做断言会让本文件
    变成全量回归里最不稳定的一个。
"""

import os
import sys
import time
from pathlib import Path

import allure
import pytest
from src.core import report_analyzer as report_analyzer_module
from src.core.executors import (
    ALLURE_DIR_MIN_AGE_SECONDS,
    ALLURE_DIR_PREFIX,
    PytestRunner,
    build_allure_results_dir,
    inject_allure_options,
    prune_stale_allure_dirs,
    resolve_bridge_execution_id,
)
from src.core.output_parser import (
    ParsedResult,
    PytestOutputParser,
    _parse_duration,
    parse_progress_percent,
)
from src.core.report_analyzer import BridgeResult

# ---------------------------------------------------------------------------
# 手工构造的 pytest 输出片段（形状取自 `pytest -q --tb=short` 真实输出）
# ---------------------------------------------------------------------------

# 全通过 + 进度条
OUTPUT_ALL_PASSED = """\
.                                                                    [100%]
============================= 3 passed in 0.42s ==============================
"""

# 混合计数：失败 + 错误 + 跳过 + 警告 + 期望失败 + 被过滤
OUTPUT_MIXED_COUNTS = """\
.sF.E                                                            [100%]
==================================== ERRORS ====================================
_____ ERROR at setup of test_with_fixture ______________________
tests/conftest.py:10: in test_with_fixture
    fixture 'tmp' not found
E   TypeError: fixture 'tmp' not found
=========================== short test summary info ============================
ERROR tests/test_demo.py::test_with_fixture - TypeError: fixture 'tmp' not found
======= 1 failed, 1 error, 2 passed, 3 skipped, 1 warning in 1.05s =========
"""

# 完整失败明细：短摘要行 + 对应 FAILURES 小节
OUTPUT_FAILED_WITH_TRACE = """\
.                                                                     [ 50%]
F                                                                     [100%]
=================================== FAILURES ===================================
_______________________________ test_login_success ______________________________
tests/test_login.py:23: in test_login_success
    assert result["code"] == 0
E   assert 2001 == 0
tests/test_login.py:23: AssertionError
=============================== short test summary info ============================
FAILED tests/test_login.py::test_login_success - assert 2001 == 0
============================= 1 failed, 1 passed in 0.68s ==============================
"""

# 失败明细带参数化后缀（用例名末段形如 test_x[1-2]）
OUTPUT_FAILED_PARAMETRIZED = """\
=================================== FAILURES ===================================
__________________________________ test_add[1-2] __________________________________
tests/test_math.py:11: in test_add
    assert add(1, 2) == 4
E   assert 3 == 4
=========================== short test summary info ============================
FAILED tests/test_math.py::test_add[1-2] - assert 3 == 4
============================= 1 failed in 0.11s ==============================
"""

# 选择器过滤过头：无任何计数项，但属合法执行结果
OUTPUT_NO_TESTS_RAN = """\
============================= no tests ran in 0.01s ==============================
"""

# 用法错误（退出码 4）：没有汇总行、没有计数项
OUTPUT_USAGE_ERROR = """\
ERROR: file or directory not found: tests/does_not_exist.py
"""

# pytest 崩溃（退出码 3）：有堆栈但没有汇总行
OUTPUT_INTERNAL_ERROR = """\
Traceback (most recent call last):
  File "C:\\Python\\Lib\\site-packages\\_pytest\\main.py", line 1, in <module>
ImportError: cannot import name 'missing'
=========================== no tests ran ===========================
"""


@allure.feature("Day50 输出解析器")
class TestSummaryLineParsing:
    """汇总行：计数与耗时"""

    @allure.story("全通过输出提取通过数与耗时")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_all_passed(self) -> None:
        """
        `3 passed in 0.42s` 必须解析出 passed=3、耗时 0.42、parse_ok=True

        进度百分比顺带断言：这条输出里有 `[100%]`，而"跑完才解析"的
        典型场景里进度恒为 100——正好用来钉住 progress_percent 的取值。
        """
        parsed = PytestOutputParser.parse(OUTPUT_ALL_PASSED)

        assert parsed.parse_ok is True, "识别出 pytest 汇总行即应 parse_ok=True"
        assert parsed.passed_count == 3
        assert parsed.failed_count == 0
        assert parsed.error_count == 0
        assert parsed.total_count == 3, "total_count 应为四项核心计数之和"
        assert parsed.duration_sec == pytest.approx(0.42)
        assert parsed.progress_percent == 100
        assert parsed.failed_cases == [], "无失败明细"
        assert parsed.failure_count == 0
        assert "3 passed" in parsed.summary_line, "应保留原始汇总行便于回溯"

    @allure.story("混合计数含单复数、警告与被过滤")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_mixed_counts_and_singular_labels(self) -> None:
        """
        混合汇总行的各项计数必须各自准确，且单数标签不漏计

        这里的样本用的是 **单数** 写法（`1 failed` / `1 warning` /
        `1 error`），正是"只认复数就会静默漏计"的回归点——数字恰好也
        是 1，漏计后看不出来的是 passed/skipped，只有把断言写成
        精确值才能逮住。
        """
        parsed = PytestOutputParser.parse(OUTPUT_MIXED_COUNTS)

        assert parsed.parse_ok is True
        assert parsed.failed_count == 1
        assert parsed.error_count == 1, "error/errors 两种写法都要计入 error_count"
        assert parsed.passed_count == 2
        assert parsed.skipped_count == 3
        assert parsed.warnings_count == 1, "warning/warnings 都要计入 warnings_count"
        assert parsed.total_count == 7
        assert parsed.duration_sec == pytest.approx(1.05)
        assert parsed.failure_count == 2, "失败合计口径 = failed + error"

    @allure.story("耗时为时分秒形态时正确换算为秒")
    @allure.severity(allure.severity_level.NORMAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_minute_form_duration(self) -> None:
        """
        长跑用例的耗时会被 pytest 渲染成 `in 65.43s (0:01:05)`，
        必须取秒值 65.43 而不是括号里的 0:01:05

        只匹配 `\\d+s` 的实现会让真实执行器的主要场景（长跑）
        耗时恒为 0，故此处对括号形态一并钉死。
        """
        parsed = PytestOutputParser.parse(
            "======================== 2 passed in 65.43s (0:01:05) ========================\n"
        )

        assert parsed.passed_count == 2
        assert parsed.duration_sec == pytest.approx(65.43)

    @allure.story("纯时分秒形态（无秒后缀）")
    @allure.severity(allure.severity_level.NORMAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_clock_form_duration(self) -> None:
        """
        形如 `in 1:05` 的纯时钟耗时也要换算成秒（1:05 → 65.0）

        pytest 在某些版本/配置下会直接给时钟形态；两种形态的换算
        写在同一个函数里，少覆盖一种就等于留了一条 0 秒的暗路。
        """
        parsed = PytestOutputParser.parse("=========== 1 passed in 1:05 ============\n")

        assert parsed.passed_count == 1
        assert parsed.duration_sec == pytest.approx(65.0)


@allure.feature("Day50 输出解析器")
class TestFailedCaseExtraction:
    """失败/错误用例明细"""

    @allure.story("短摘要行解析用例名/阶段/原因并回溯诊断尾部")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_failed_case_with_trace_tail(self) -> None:
        """
        从 `FAILED tests/test_login.py::test_login_success - assert 2001 == 0`
        提取用例节点、失败阶段、原因，并回溯到 FAILURES 小节取诊断尾部

        这里同时钉住两个易错点：
            - node_id 与 name 的切分（name 是末段，不含路径）
            - 诊断尾部取的是**小节末尾**若干行（含 AssertionError 那一行），
              而不是小节标题行或空行
        """
        parsed = PytestOutputParser.parse(OUTPUT_FAILED_WITH_TRACE)

        assert len(parsed.failed_cases) == 1
        case = parsed.failed_cases[0]
        assert case.node_id == "tests/test_login.py::test_login_success"
        assert case.name == "test_login_success"
        assert case.stage == "failed", "FAILED 行归入 failed 阶段"
        assert case.reason == "assert 2001 == 0"
        assert "AssertionError" in case.trace_tail, "诊断尾部须含异常类型行"
        assert "assert 2001 == 0" in case.trace_tail
        assert case.trace_tail.splitlines()[-1] == (
            "tests/test_login.py:23: AssertionError"
        ), "诊断尾部须是小节**末尾**若干行，末行应是异常定位行而非小节标题"

    @allure.story("ERROR at setup 小节的标题变体也能回溯到诊断尾部")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_error_case_setup_section(self) -> None:
        """
        setup 阶段错误的小节标题是 `ERROR at setup of test_with_fixture`，
        与用例名只是**包含**关系——精确相等匹配会让这类失败拿不到任何
        诊断信息，而它恰恰是最需要堆栈的一类

        故本例同时断言 stage 为 error、诊断尾部含 TypeError 消息。
        """
        parsed = PytestOutputParser.parse(OUTPUT_MIXED_COUNTS)

        error_cases = [c for c in parsed.failed_cases if c.stage == "error"]
        assert len(error_cases) == 1
        case = error_cases[0]
        assert case.name == "test_with_fixture"
        assert "TypeError" in case.trace_tail, (
            "标题为 'ERROR at setup of X' 时也必须能回溯到诊断尾部"
        )
        assert "fixture 'tmp' not found" in case.trace_tail

    @allure.story("参数化用例名保留参数后缀")
    @allure.severity(allure.severity_level.NORMAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_parametrized_case_name(self) -> None:
        """
        `tests/test_math.py::test_add[1-2]` 的用例名必须是 `test_add[1-2]`

        去参数后缀会让人工定位时看不出是哪组数据失败；去掉路径前缀
        则会让不同文件里的同名用例在报告里撞成一条。
        """
        parsed = PytestOutputParser.parse(OUTPUT_FAILED_PARAMETRIZED)

        assert len(parsed.failed_cases) == 1
        assert parsed.failed_cases[0].name == "test_add[1-2]"

    @allure.story("只有短摘要行、没有汇总行时仍算识别成功")
    @allure.severity(allure.severity_level.NORMAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_ok_when_only_short_summary_present(self) -> None:
        """
        输出被腰斩（进程被杀、日志截断）时可能只剩短摘要行没有汇总行，
        此时明细仍然可用，parse_ok 应为 True

        反过来，若把 parse_ok 判成 False 而又把明细丢掉（Day50 实现
        里的兜底逻辑），这次执行就什么都不剩——腰斩的那一次恰恰是
        最需要现场的一次。
        """
        parsed = PytestOutputParser.parse(
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_demo.py::test_broken - RuntimeError: boom\n"
        )

        assert parsed.parse_ok is True
        assert len(parsed.failed_cases) == 1
        assert parsed.failed_cases[0].reason == "RuntimeError: boom"


@allure.feature("Day50 输出解析器")
class TestParserBoundaries:
    """边界与异常输入"""

    @allure.story("空输出与纯空白输出不抛异常且不谎报全通过")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_empty_output(self) -> None:
        """
        空串与纯空白必须返回全零 + parse_ok=False

        为什么"全零但 parse_ok=False"是必须的: 若 parse_ok 判 True，
        调用方就会把"零失败"当成"全部通过"落库——把没跑成功说成跑成功
        是这类解析器最危险的一种错。
        """
        for raw in ("", "   \n\n\t "):
            parsed = PytestOutputParser.parse(raw)

            assert parsed.parse_ok is False
            assert parsed.passed_count == 0
            assert parsed.failed_count == 0
            assert parsed.failure_count == 0
            assert parsed.duration_sec == 0.0
            assert parsed.failed_cases == []
            assert parsed.summary_line == ""
            assert parsed.progress_percent is None

    @allure.story("非 pytest 输出与崩溃输出标记为未识别")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_non_pytest_output(self) -> None:
        """
        用法错误（退出码 4）与内部崩溃（退出码 3）的输出都不含汇总特征，
        必须 parse_ok=False

        崩溃输出里那句 `=========================== no tests ran ===========================`
        是 pytest 自己打印的、**没有耗时**的一行；本解析器要求同时命中
        计数项与耗时，故不会把它误当成合法汇总——这正是"两者都要"的
        设计意图。
        """
        for raw in (OUTPUT_USAGE_ERROR, OUTPUT_INTERNAL_ERROR):
            parsed = PytestOutputParser.parse(raw)

            assert parsed.parse_ok is False, f"不应识别为合法汇总: {raw[:40]!r}"
            assert parsed.failed_cases == [], "未识别时不得对外宣称失败明细"

    @allure.story("no tests ran 是合法执行结果")
    @allure.severity(allure.severity_level.NORMAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_no_tests_ran(self) -> None:
        """
        `no tests ran in 0.01s` 是选择器过滤过头的常态结果，
        必须 parse_ok=True 且耗时照常提取

        把它判成"无法识别"会让这类执行在统计里凭空消失——
        而"选错了 marker"恰恰是需要被看见的一类失败。
        """
        parsed = PytestOutputParser.parse(OUTPUT_NO_TESTS_RAN)

        assert parsed.parse_ok is True
        assert parsed.total_count == 0
        assert parsed.duration_sec == pytest.approx(0.01)


@allure.feature("Day50 输出解析器")
class TestProgressPercent:
    """进度百分比提取"""

    @allure.story("取最后一个进度值并钳制上界")
    @allure.severity(allure.severity_level.NORMAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_progress_percent(self) -> None:
        """
        多段进度行取**最后一个**（实时展示需要最新值），
        超过 100 的值被钳制（被测代码自己打印形如 `[999%]` 的文本
        不该污染展示层）

        同时覆盖：无进度行与空输入时返回 None。
        """
        assert parse_progress_percent("[  9%]\n[ 50%]\n[100%]") == 100
        assert parse_progress_percent("[999%]") == 100
        assert parse_progress_percent("没有进度行") is None
        assert parse_progress_percent("") is None

    @allure.story("ParsedResult 默认值可安全构造")
    @allure.severity(allure.severity_level.NORMAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parsed_result_defaults(self) -> None:
        """
        空 ParsedResult 的失败明细必须是独立列表而非共享默认对象

        dataclass 的可变默认值若写错成 `= []`，两个实例会共享同一个
        list，其中一个 append 会污染另一个——这是本项目多个统计
        dataclass 都用 field(default_factory=list) 的同一原因。
        """
        first = ParsedResult()
        second = ParsedResult()

        first.failed_cases.append  # noqa: B018 - 确认属性存在即可
        assert first.failed_cases == []
        assert first.failed_cases is not second.failed_cases, (
            "可变默认值必须用 default_factory 隔离"
        )
        assert first.parse_ok is False
        assert first.progress_percent is None

# ---------------------------------------------------------------------------
# Day50 任务二：Allure 目录隔离与桥接调用
# ---------------------------------------------------------------------------


def _make_dir(path: Path, age_seconds: float = 0.0) -> Path:
    """
    建目录并把 mtime 回拨指定秒数（让"陈旧"判据无需真实等待即可命中）

    为什么用 os.utime 而不是 time.sleep 等它自然变旧:
        陈旧判据是"存活超过 300 秒"，等 300 秒不现实；而 utime 直接改
        mtime 是**确定性**的，不给这个用例引入任何时序抖动（固定 sleep
        赌时序是本项目明令禁止的）。

    参数:
        path (Path): 待创建目录路径
        age_seconds (float): 回拨秒数；0 表示保持当前时间

    返回:
        Path: 创建好的目录路径
    """
    path.mkdir(parents=True, exist_ok=True)
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    return path


@allure.feature("Day50 Allure 目录隔离")
class TestAllureDirIsolation:
    """独立结果目录的生成、注入与清理"""

    @allure.story("每次执行生成独立目录且不提前建目录")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_build_allure_results_dir(self, tmp_path) -> None:
        """
        目录名须含前缀+用例编号+微秒时间戳，且方法**只算路径不落盘**

        不落盘是刻意的：build_command 失败、子进程启动失败等场景根本
        不会有结果写入，提前 mkdir 只会攒出一堆空目录。

        同时验证用例编号里的路径分隔符被替换——目录名直接进 pytest 命令
        行参数，含分隔符会变成路径穿越。
        """
        first = build_allure_results_dir("TM-UC-0001", root=tmp_path)
        second = build_allure_results_dir("TM-UC-0001", root=tmp_path)

        assert first.parent == tmp_path
        assert first.name.startswith(ALLURE_DIR_PREFIX)
        assert "TM-UC-0001" in first.name
        assert first != second, (
            "微秒级时间戳不够——Windows 时钟粒度约 15.6ms，同节拍内两次 "
            "调用返回相同微秒值（Day50 实测撞名），必须靠序号+pid 补齐"
        )

        sanitized = build_allure_results_dir("../evil/../x", root=tmp_path)
        assert sanitized.parent == tmp_path, "用例编号不得把目录带出父目录"
        assert ".." not in sanitized.name

        assert not first.exists(), "本方法只算路径，目录由 pytest 子进程创建"

    @allure.story("alluredir 插在选项终止符之前")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_inject_allure_options_before_terminator(self, tmp_path) -> None:
        """
        --alluredir/--clean-alluredir 必须排在 `--` **之前**

        append 到末尾会让它们落到终止符之后被 pytest 当成第二个测试
        文件路径，报 "file or directory not found" 并以退出码 4 收场——
        与 Day44 P1-01 是同一个坑，只是这次错的是我们新加的参数。

        同时钉死"不修改原命令"：build_command 的返回值可能被调用方复用。
        """
        command = [sys.executable, "-m", "pytest", "-q", "--tb=short", "--", "tests/a.py"]
        allure_dir = tmp_path / "allure_results_x"
        injected = inject_allure_options(command, allure_dir)

        terminator_index = injected.index("--")
        marker_index = injected.index(f"--alluredir={allure_dir}")
        assert marker_index < terminator_index, "alluredir 必须排在终止符之前"
        assert "--clean-alluredir" in injected[:terminator_index]
        assert injected[-1] == "tests/a.py", "执行目标必须仍是最后一个位置参数"
        assert command == [
            sys.executable, "-m", "pytest", "-q", "--tb=short", "--", "tests/a.py"
        ], "原命令不得被就地修改"

        # 无终止符时按"追加到末尾"退化，不抛错
        bare = inject_allure_options(["pytest", "tests/a.py"], allure_dir)
        assert bare[-2:] == [f"--alluredir={allure_dir}", "--clean-alluredir"]

    @allure.story("清理陈旧目录时跳过当前目录与过新的目录")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_prune_stale_allure_dirs(self, tmp_path) -> None:
        """
        只删"够旧 + 超出保留数 + 不是自己"的目录

        三条守卫各挡一种事故：
          - 跳过 current_dir：把自己正在写的目录删了，本次结果永远丢
          - 跳过存活不足阈值的：并发执行中刚建的目录不能删，否则那次
            执行的结果解析不到
          - 只删超出 keep 的：否则一次执行会清空全部历史结果

        目录名按序号递增模拟"创建时刻递增"，清理后应只剩最近 10 个。
        """
        old_age = ALLURE_DIR_MIN_AGE_SECONDS + 60
        # 13 个陈旧目录，序号 0001..0013（序号越大 = 排序上越新）
        old_dirs = [
            _make_dir(tmp_path / f"{ALLURE_DIR_PREFIX}case_{index:04d}", age_seconds=old_age)
            for index in range(1, 14)
        ]
        # 第 14 个目录序号最小（排序上最旧）却刚建好：它会落到保留数之外，
        # 只能靠"存活时长"这条守卫幸存——否则本例就测不到该守卫
        fresh = _make_dir(tmp_path / f"{ALLURE_DIR_PREFIX}case_0000")
        # 当前执行目录刻意选**最旧**那个（同样会落到保留数之外），
        # 让"跳过自己"这条守卫也被真正触发
        current = old_dirs[0]

        removed = prune_stale_allure_dirs(current_dir=current, root=tmp_path, keep=10)

        assert removed == [old_dirs[2].name, old_dirs[1].name], (
            "只应删除超出保留数且已够旧、且不是当前目录的那两个"
        )
        assert fresh.exists(), "存活不足阈值的目录必须跳过（尽管已超出保留数）"
        assert current.exists(), "当前执行目录必须跳过（尽管已超出保留数）"
        assert all(path.exists() for path in old_dirs[3:]), "保留数内的十个目录必须原样留下"

    @allure.story("目录根不存在时安全返回空列表")
    @allure.severity(allure.severity_level.NORMAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_prune_missing_root_returns_empty(self, tmp_path) -> None:
        """
        output/ 尚未生成时清理必须安静返回空列表

        清理是旁路能力，"目录还不存在"是完全正常的初始状态，
        不该让一次正常执行留下一条异常日志。
        """
        assert prune_stale_allure_dirs(root=tmp_path / "never_created") == []


@allure.feature("Day50 Allure 桥接调用")
class TestBridgeInvocation:
    """执行器侧的桥接调用契约"""

    @allure.story("批次号优先取用例字典，回落值确定")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_resolve_bridge_execution_id(self) -> None:
        """
        带 execution_id 时直接复用；缺失/空白/非字符串时回落 pytest-{case_id}

        回落值必须是确定性的：defect_statistics.execution_id 有唯一约束
        且 save_statistics 刻意不静默更新，每次生成新批次号会让每条用例
        每跑一次就往统计表插一行，把批次数与通过率趋势口径撑坏。

        另外必须保证结果非空——空串会被 save_statistics 抛 ValueError。
        """
        assert resolve_bridge_execution_id(
            {"case_id": "TM-1", "execution_id": "RUN-2026-10-10"}
        ) == "RUN-2026-10-10"
        assert resolve_bridge_execution_id({"case_id": "TM-1"}) == "pytest-TM-1"
        assert resolve_bridge_execution_id(
            {"case_id": "TM-1", "execution_id": "   "}
        ) == "pytest-TM-1"
        assert resolve_bridge_execution_id(
            {"case_id": "TM-1", "execution_id": 123}
        ) == "pytest-TM-1", "非字符串批次号同样视为未配置"
        assert resolve_bridge_execution_id({}) == "pytest-unknown", "绝不能返回空串"

    @allure.story("解析器意外抛错时降级为 None")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_output_falls_back_to_none(self, monkeypatch) -> None:
        """
        解析器被 monkeypatch 成抛错时，run_one 侧只拿到 None 而非异常

        PytestOutputParser.parse 自身已设计为不抛异常，但执行器是执行链路
        的最后一道兜底：解析器将来新增字段处理时引入的任何意外，都不该
        让已经跑完的执行丢掉结果（异常上抛会把整批判 failed）。
        """
        def _boom(output: str):
            raise RuntimeError("模拟解析器内部故障")

        monkeypatch.setattr(PytestOutputParser, "parse", staticmethod(_boom))

        assert PytestRunner.parse_output("任意输出") is None

    @allure.story("桥接三态透传且任何异常都被兜住")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_bridge_allure_results_propagates_three_states(
        self, tmp_path, monkeypatch
    ) -> None:
        """
        成功/失败/无结果三态都要如实反映到返回值，桥接自身抛错也不外泄

        `bridge_results_dir` 是函数内延迟导入的，故此处替换的是
        `src.core.report_analyzer` 模块上的属性——这既验证了调用点确实
        取的是模块当前绑定，也避免了为测试改生产代码的导入方式。
        """
        allure_dir = tmp_path / f"{ALLURE_DIR_PREFIX}case_0001"

        # 1) 有结果 → 返回空串（成功）
        monkeypatch.setattr(
            report_analyzer_module,
            "bridge_results_dir",
            lambda *args, **kwargs: BridgeResult(
                parsed_count=2, statistics=report_analyzer_module.StatisticsResult(total=2)
            ),
        )
        assert PytestRunner().bridge_allure_results(allure_dir, {"case_id": "TM-1"}) == ""

        # 2) 桥接内部报错 → 原样带回原因
        monkeypatch.setattr(
            report_analyzer_module,
            "bridge_results_dir",
            lambda *args, **kwargs: BridgeResult(error="ValueError: 执行批次号不能为空"),
        )
        assert PytestRunner().bridge_allure_results(allure_dir, {"case_id": "TM-1"}) == (
            "ValueError: 执行批次号不能为空"
        )

        # 3) 桥接调用本身抛错（导入失败/DB 不可达等）→ 兜底成字符串
        def _explode(*args, **kwargs):
            raise RuntimeError("数据库不可达")

        monkeypatch.setattr(
            report_analyzer_module, "bridge_results_dir", _explode
        )
        assert PytestRunner().bridge_allure_results(allure_dir, {"case_id": "TM-1"}) == (
            "RuntimeError: 数据库不可达"
        )


@allure.feature("Day50 输出解析器")
class TestParserDegradation:
    """畸形输入下的降级契约：宁可少信息，绝不抛异常"""

    @allure.story("尾部空行、空小节、超长诊断尾部均安全降级")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_parse_degrades_on_malformed_output(self) -> None:
        """
        四类畸形输入的降级行为全部钉死

        - 汇总行后带空行: 倒序扫描须跳过空行（否则 `splitlines` 的尾部
          空串会被当成最后一行，汇总行永远找不到）
        - 小节内全是分隔行: 诊断尾部为空串而非 None/抛错
        - 诊断尾部超长: 被测代码在失败前打印大量日志是常态，
          不截断会让 ExecutionResult 撑爆，必须走截断分支并留前缀标记
        - 耗时串形态不识别: 降级为 0.0 而不是抛 ValueError
        """
        parsed = PytestOutputParser.parse(
            "============================= 2 passed in 0.05s ==============================\n"
            "\n"
            "\n"
        )
        assert parsed.passed_count == 2, "汇总行之后的空行不得影响识别"

        empty_section = (
            "=================================== FAILURES ===================================\n"
            "__________________________________ test_void ____________________________________\n"
            "=================================== ERRORS ====================================\n"
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_void.py::test_void - assert None\n"
            "============================= 1 failed in 0.02s ==============================\n"
        )
        void_parsed = PytestOutputParser.parse(empty_section)
        assert len(void_parsed.failed_cases) == 1
        assert void_parsed.failed_cases[0].trace_tail == "", (
            "小节内没有实质内容时诊断尾部应为空串，不能是 None 或抛错"
        )

        # 每行都要够长（单行约 150 字符）：诊断尾部只保留末尾
        # TRACE_TAIL_MAX_LINES=3 行，行短则 3 行之和不足以触发截断，
        # 那样这个用例就测不到截断分支了
        noisy_body = "\n".join(
            f"E   打印噪声第 {i} 行 " + "x" * 140 for i in range(400)
        )
        noisy = (
            "=================================== FAILURES ===================================\n"
            "______________________________ test_noisy ______________________________\n"
            f"{noisy_body}\n"
            "=========================== short test summary info ============================\n"
            "FAILED tests/test_noisy.py::test_noisy - RuntimeError: boom\n"
            "============================= 1 failed in 0.10s ==============================\n"
        )
        noisy_parsed = PytestOutputParser.parse(noisy)
        tail = noisy_parsed.failed_cases[0].trace_tail
        assert tail.startswith("...[诊断尾部过长"), "超长诊断尾部必须走截断分支"
        assert len(tail) < len(noisy_body), "截断后长度必须显著小于原文"

        assert _parse_duration("xs") == 0.0, "形态不识别的耗时降级为 0 而非抛错"
        assert _parse_duration("1:xx") == 0.0

    @allure.story("用例名为空时不回溯")
    @allure.severity(allure.severity_level.NORMAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_pick_trace_tail_with_empty_name(self) -> None:
        """
        空用例名直接返回空串，不进入小节查找

        该守卫平时不可达（`_SHORT_SUMMARY_PATTERN` 的 node 恒非空），
        但 `_pick_trace_tail` 是静态方法且要做全字符串匹配，
        空值守卫属于"契约的一部分"而非凑覆盖。
        """
        assert PytestOutputParser._pick_trace_tail({}, "") == ""


@allure.feature("Day50 Allure 目录隔离")
class TestPruneFailureTolerance:
    """清理过程中的文件系统异常不得外泄"""

    @allure.story("stat 与 glob 抛 OSError 时安全跳过")
    @allure.severity(allure.severity_level.CRITICAL)
    @pytest.mark.api
    @pytest.mark.regression
    def test_prune_tolerates_filesystem_errors(self, tmp_path, monkeypatch) -> None:
        """
        两种真实的文件系统异常都要被吞掉并继续

        - `path.stat()` 抛 OSError: 目录在"扫描到"与"取 mtime"之间被
          另一个进程删掉（并发执行场景下极常见）
        - 整段 glob 抛 OSError: output/ 被外部工具短暂占用

        清理是旁路能力：任何一种异常都只能记日志，绝不能把一次已经
        跑完的执行带崩——那会让整批判 failed。
        """
        real_stat = Path.stat
        old_age = ALLURE_DIR_MIN_AGE_SECONDS + 60

        for index in range(12):
            _make_dir(tmp_path / f"{ALLURE_DIR_PREFIX}case_{index:04d}", age_seconds=old_age)

        # 模拟"目录在扫描与 stat 之间被删掉"这一真实竞态。
        #
        # 必须**第二次**调用才抛：pathlib 的 `Path.is_dir()` 内部同样调
        # `self.stat()`，而它在扫描阶段就会执行到本用例注入的替身上。
        # 若第一次就抛，异常会从 is_dir 冒泡出去，被 prune 的**外层**
        # except 接走（内层守卫根本没被测到），且外层会把整批结果
        # 退化成空列表——那才是错误的降级方式。
        target = f"{ALLURE_DIR_PREFIX}case_0000"
        seen: dict[str, int] = {}

        def _stat_then_gone(self: Path, *args, **kwargs):
            if self.name == target:
                seen[self.name] = seen.get(self.name, 0) + 1
                if seen[self.name] >= 2:
                    raise FileNotFoundError("模拟目录在扫描与 stat 之间被删除")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _stat_then_gone)
        removed = prune_stale_allure_dirs(root=tmp_path, keep=10)
        assert target not in removed, "stat 失败的目录必须被跳过"
        assert len(removed) == 1, (
            "其余超出保留数的陈旧目录仍应被正常清理，"
            "不能因为一个目录的异常就放弃整批"
        )

        def _glob_raises(*args, **kwargs):
            raise OSError("模拟 output 目录被外部工具占用")

        monkeypatch.setattr(Path, "glob", _glob_raises)
        assert prune_stale_allure_dirs(root=tmp_path, keep=10) == [], (
            "glob 失败必须降级为空列表并记 warning"
        )
