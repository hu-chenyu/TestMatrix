"""
TestMatrix 平台专用 pytest 插件的验收测试（Day56 任务二）

被测对象: `src/testmatrix_plugin/__init__.py`（经 pytest11 entry point 注册）

两条互补的验证路径（缺一不可）:
    1. **直接驱动**: 用替身 config / session / report 调用插件的纯函数与钩子，
       把每个分支口径钉在单条断言上——JSON 写入失败、stash 读不到、ini 不可读、
       非常规阶段等降级路径只有这条路能稳定覆盖。
    2. **嵌套 pytest 端到端**: 真起子进程跑探针用例与项目根 conftest，
       断言"注册 → 加载 → 钩子生效 → 报告落盘"整条链路真实可用。

为什么子进程要显式 `-p src.testmatrix_plugin` 并同时 `-p no:testmatrix`:
    本地开发环境里插件已按 entry point 安装，子进程会**自动**加载它；CI 环境
    不安装项目、不会加载。若不做处理，同一份用例在两种环境下跑的是不同链路
    （本地自动加载的那份根本不经过本文件的断言）。显式 `-p` 让两边行为一致；
    `-p no:testmatrix` 挡住 entry point 那份，避免同一份源码被加载成两个模块
    对象（会注册两套钩子、向同一路径写两份报告）。

探针用例刻意覆盖"通过 / 断言失败 / 跳过"三类，让执行侧三项计数与失败明细
都有真实的非零观测值；探针文件写在 `output/` 下（已被 .gitignore 覆盖），
因为只有那里才处在项目根 conftest 的祖先链上——收集统计的对接链路正是靠
那一份 conftest 写入的 stash 数据验证的。
"""

import importlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import allure
import pytest
from src.core.output_parser import PytestOutputParser

# 项目根（本文件位于 tests/ 下，向上一级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 插件模块的导入名（src 布局下的真实路径；entry point 侧的名字是 testmatrix）
PLUGIN_MODULE_NAME = "src.testmatrix_plugin"

# entry point 组名与注册名（须与 pyproject.toml 完全一致）
ENTRY_POINT_GROUP = "pytest11"
ENTRY_POINT_NAME = "testmatrix"
ENTRY_POINT_VALUE = "testmatrix_plugin"

# 探针目录前缀（output/ 已被 .gitignore 忽略，也不在外层收集范围内）
PROBE_DIR_PREFIX = "day56_plugin_probe"
PROBE_FILE_NAME = "test_day56_probe.py"

# 嵌套 pytest 子进程超时（秒）: 探针只有三条用例，预算只覆盖冷启动，不赌时序
NESTED_PYTEST_TIMEOUT_SECONDS = 180.0

# 探针模块源码: 一条通过、一条断言失败、一条跳过（三类结果各一条）
PROBE_SOURCE = '''\
"""Day56 插件探针用例（由 test_pytest_plugin.py 动态落盘，跑完即删）"""

import pytest


@pytest.mark.smoke
def test_probe_pass():
    """通过的探针用例（验证 passed 计数与阶段分布）"""
    assert True


@pytest.mark.regression
def test_probe_fail():
    """失败的探针用例（验证 failed 计数与失败明细汇总）"""
    assert 1 == 2


@pytest.mark.skip(reason="探针: 刻意跳过的用例")
def test_probe_skip():
    """被跳过的探针用例（验证 skipped 计数，且不得混进失败明细）"""
    assert True
'''

# 收集统计替身的总数（与项目根 conftest 的 CollectionStats 同结构，插件按结构化子类型读）
FAKE_COLLECTION_TOTAL = 7


@dataclass(frozen=True)
class ProbeRun:
    """
    一次嵌套 pytest 的运行结果快照

    属性:
        returncode (int): 子进程退出码（探针含失败用例，预期为 1）
        stdout (str): 子进程 stdout（已按 utf-8 容错解码）
        stderr (str): 子进程 stderr（已按 utf-8 容错解码）
        report_path (Path): 该次运行报告的输出路径
        target (str): 探针文件相对项目根的路径（供额外子进程复用）
        basetemp (Path): 子进程专属的基础临时目录（供额外子进程复用）
    """

    returncode: int
    stdout: str
    stderr: str
    report_path: Path
    target: str
    basetemp: Path

    def report(self) -> dict[str, Any]:
        """
        读回报告 JSON（缺失即让用例直接红，而不是静默返回空字典）

        参数:
            无

        返回:
            dict[str, Any]: 报告字典

        异常:
            FileNotFoundError: 报告未生成时抛出（这正是被测行为）
        """
        return cast(
            dict[str, Any], json.loads(self.report_path.read_text(encoding="utf-8"))
        )


class _ExplodingReport:
    """属性访问即抛错的报告替身（验证日志钩子的兜底不影响用例执行）"""

    @property
    def when(self) -> str:
        """
        参数:
            无

        返回:
            无（必然抛错）

        异常:
            RuntimeError: 恒抛出，用于驱动钩子的兜底分支
        """
        raise RuntimeError("替身属性访问失败")


class _BrokenIniConfig:
    """ini 读取即抛错的配置替身（验证配置读取的降级路径）"""

    def getini(self, name: str) -> Any:
        """
        参数:
            name (str): ini 键名（替身直接抛错）

        返回:
            无（必然抛错）

        异常:
            ValueError: 恒抛出，模拟"平台未注册该 ini 选项"
        """
        raise ValueError(f"ini 不可读: {name}")


class _PluginWithStatsKey:
    """
    带收集统计 stash 键的插件替身

    刻意不用 `SimpleNamespace`: 它定义了 `__eq__`（按内容比较），因而
    `__hash__` 为 None，**不可哈希**——放进 `get_plugins()` 返回的集合会抛
    TypeError，被插件的兜底分支接走后表现为"读不到统计"，断言就此失真
    （开发期实测踩到）。
    """

    def __init__(self, key: pytest.StashKey) -> None:
        """
        参数:
            key (pytest.StashKey): 收集统计在 stash 中的键

        返回:
            无
        """
        self.COLLECTION_STATS_STASH_KEY = key


class _RecordingParser:
    """记录 addini 调用的解析器替身（验证配置项的注册口径）"""

    def __init__(self) -> None:
        """初始化空的注册记录"""
        self.registered: list[tuple[str, str | None, Any]] = []

    def addini(
        self, name: str, help: str, type: str | None = None, default: Any = None
    ) -> None:
        """
        记录一次 ini 注册（对齐 pytest 侧 addini 的关键字调用形态）

        参数:
            name (str): ini 键名
            help (str): 帮助文本
            type (str | None): 值类型
            default (Any): 默认值

        返回:
            无
        """
        self.registered.append((name, type, default))


def _report_stub(
    phase: str, outcome: str, nodeid: str = "probe.py::test_case", duration: float = 0.1
) -> Any:
    """
    构造最小报告替身（钩子只读 when / outcome / nodeid / duration 四个属性）

    参数:
        phase (str): 执行阶段
        outcome (str): 阶段结果
        nodeid (str): 用例节点 id
        duration (float): 阶段耗时（秒）

    返回:
        Any: 报告替身

    异常:
        无
    """
    return SimpleNamespace(when=phase, outcome=outcome, nodeid=nodeid, duration=duration)


def _unique_probe_dir() -> Path:
    """
    生成本次运行的探针目录（会话级唯一后缀，避免并行 pytest 会话互踩）

    后缀取"微秒时间戳 + 进程号"双要素: 时间戳解决先后启动的两次运行，
    pid 解决同一时钟节拍内的两次运行（Windows 时钟粒度约 15.6ms）。

    参数:
        无

    返回:
        Path: 尚未创建的探针目录路径

    异常:
        无
    """
    stamp = f"{time.time():.6f}".replace(".", "")
    return PROJECT_ROOT / "output" / f"{PROBE_DIR_PREFIX}-{stamp}-{os.getpid()}"


def _nested_env() -> dict[str, str]:
    """
    构造子进程环境变量

    必须显式注入 `PYTHONPATH=项目根`: `-p <模块名>` 的解析发生在 pytest 处理
    ini 的 `pythonpath` 配置之前，靠 ini 兜底会让插件在子进程里导入失败。
    另外固定 `PYTHONIOENCODING=utf-8`: 子进程 stdout 走管道时默认用本机 ANSI
    代码页，摘要行里的中文会在子进程侧就被编坏，无法按原文断言（Day55 踩坑）。

    参数:
        无

    返回:
        dict[str, str]: 子进程环境变量（继承当前进程后叠加两处覆盖）

    异常:
        无
    """
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT}{os.pathsep}{existing}" if existing else str(PROJECT_ROOT)
    )
    return env


def _decode(raw: bytes) -> str:
    """
    按 utf-8 容错解码子进程输出

    刻意不用 `text=True`: Windows 控制台编码与项目路径中的非 ASCII 字符组合
    会让解码本身抛错，从而掩盖真正的失败原因（沿用 Day53/Day55 的口径）。

    参数:
        raw (bytes): 子进程原始输出

    返回:
        str: 解码后的文本（非法字节以替换字符呈现）

    异常:
        无
    """
    return raw.decode("utf-8", errors="replace")


def _run_nested_pytest(
    extra_args: list[str], targets: list[str], basetemp: Path
) -> subprocess.CompletedProcess[bytes]:
    """
    跑一次嵌套 pytest（显式加载被测插件、禁用 entry point 那一份）

    刻意显式指定 `--basetemp`: 不自带时子进程会与**主会话**共用同一个
    `%TEMP%/pytest-of-<user>/` 根目录，多进程交替创建/回收 `garbage-*`
    目录在 Windows 上会偶发删除失败并抛出 `PytestWarning`（环境层噪声，
    会污染"0 warning"门禁）。把它钉在探针目录下（已被 .gitignore 覆盖，
    且随 fixture 一并清理）即从结构上消除这类争用。

    参数:
        extra_args (list[str]): 追加在固定参数之后、目标之前的参数
        targets (list[str]): 传给 pytest 的目标路径
        basetemp (Path): 子进程专属的基础临时目录（存在即被清空重建）

    返回:
        subprocess.CompletedProcess[bytes]: 子进程结果（含 returncode 与原始输出）

    异常:
        subprocess.TimeoutExpired: 子进程超出预算时抛出（视为严重失败）
    """
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            PLUGIN_MODULE_NAME,
            "-p",
            f"no:{ENTRY_POINT_NAME}",
            "--tb=no",
            "--basetemp",
            str(basetemp),
            *extra_args,
            *targets,
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        env=_nested_env(),
        check=False,
        timeout=NESTED_PYTEST_TIMEOUT_SECONDS,
    )


@pytest.fixture(scope="module")
def plugin() -> ModuleType:
    """
    加载被测插件模块（供"直接驱动"路径调用其纯函数与钩子）

    刻意按包路径 `src.testmatrix_plugin` 导入: 与 entry point 加载的
    `testmatrix_plugin` 是两个模块对象、两把 stash 键，互不干扰，
    这样即使本地已安装插件（外层会话正在跑它），本文件的断言也只作用于
    自己手里这一份。

    参数:
        无

    返回:
        ModuleType: 插件模块对象

    异常:
        ImportError: 模块不存在时抛出（用例应当直接红）
    """
    return importlib.import_module(PLUGIN_MODULE_NAME)


@pytest.fixture(scope="module")
def probe_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ProbeRun]:
    """
    跑一次嵌套 pytest（探针含通过/失败/跳过三类用例），供多条用例共享

    报告输出重定向到临时目录: 不污染项目 `output/` 下插件默认路径的报告，
    也让"报告是否生成"这类断言不受其它运行残留文件干扰。

    参数:
        tmp_path_factory (pytest.TempPathFactory): 会话级临时目录工厂

    返回:
        Iterator[ProbeRun]: 子进程结果与报告路径（模块级只跑一次）

    异常:
        无（子进程失败由 returncode 断言呈现，不在 fixture 里抛）
    """
    probe_dir = _unique_probe_dir()
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe_file = probe_dir / PROBE_FILE_NAME
    probe_file.write_text(PROBE_SOURCE, encoding="utf-8")
    report_path = tmp_path_factory.mktemp("day56_report") / "testmatrix_report.json"
    target = probe_file.relative_to(PROJECT_ROOT).as_posix()
    basetemp = probe_dir / "_nested_basetemp"
    try:
        completed = _run_nested_pytest(
            ["-q", "-o", f"testmatrix_report_path={report_path}"],
            [target],
            basetemp,
        )
        yield ProbeRun(
            returncode=completed.returncode,
            stdout=_decode(completed.stdout),
            stderr=_decode(completed.stderr),
            report_path=report_path,
            target=target,
            basetemp=basetemp,
        )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)


@allure.feature("Day56 自定义pytest插件")
@allure.story("插件注册")
@allure.title("pytest11 entry point 声明正确且插件可被 pytest 真实加载")
def test_entry_point_registration_is_declared_and_loadable(tmp_path: Path) -> None:
    """
    注册链路两段各自取证:
        1. **声明**: pyproject.toml 的 `[project.entry-points."pytest11"]` 段必须
           逐字段等于约定值（这是"安装后自动激活"的唯一事实来源）；环境里已
           安装本项目时，importlib.metadata 读到的注册项必须与声明一致。
        2. **可加载**: 真起一个 pytest 子进程用 `-p` 显式加载该模块，退出码必须
           为 0；同时跑一个**阳性对照**——加载不存在的模块名时退出码必须非 0。
           没有这个对照，"退出码 0"完全可能只是因为 pytest 把加载失败静默吞掉，
           断言会退化成永真（本机实测: 正向 0 / 对照 1 且报 Error importing plugin）。
    """
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    declared = pyproject["project"]["entry-points"][ENTRY_POINT_GROUP]
    assert declared == {ENTRY_POINT_NAME: ENTRY_POINT_VALUE}

    installed = {
        entry_point.name: entry_point.value
        for entry_point in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    }
    if ENTRY_POINT_NAME in installed:
        # 已安装（本地 editable 安装）时必须与声明一致；CI 未安装时该分支不触发，
        # 但声明断言已在上面无条件执行，注册契约始终被覆盖
        assert installed[ENTRY_POINT_NAME] == declared[ENTRY_POINT_NAME]

    loaded = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            PLUGIN_MODULE_NAME,
            "-p",
            f"no:{ENTRY_POINT_NAME}",
            "--basetemp",
            str(tmp_path / "basetemp"),
            "--version",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        env=_nested_env(),
        check=False,
        timeout=NESTED_PYTEST_TIMEOUT_SECONDS,
    )
    assert loaded.returncode == 0, _decode(loaded.stderr)

    missing = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "src.definitely_missing_plugin",
            "--basetemp",
            str(tmp_path / "basetemp"),
            "--version",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        env=_nested_env(),
        check=False,
        timeout=NESTED_PYTEST_TIMEOUT_SECONDS,
    )
    assert missing.returncode != 0, "阳性对照不成立: 插件加载失败未被 pytest 反映"
    assert "Error importing plugin" in _decode(missing.stderr)


@allure.feature("Day56 自定义pytest插件")
@allure.story("结果上报")
@allure.title("JSON 报告结构完整且写入失败只降级不抛错")
def test_report_json_structure_and_writer_contract(
    plugin: ModuleType, probe_run: ProbeRun, tmp_path: Path
) -> None:
    """
    端到端: 子进程退出码必须呈现探针真实结果（含失败 → 1），报告文件必须存在、
    可解析，且验收要求的字段一个不少——`total / passed / failed / skipped /
    duration / marker_counts`（前五项在执行侧、末项在收集侧）。

    直接驱动: 报告路径解析的"相对/绝对"两个分支、以及写入失败时**只回传错误
    描述而不抛异常**的契约（用"目标路径已被目录占用"制造真实 OSError）。
    """
    assert probe_run.returncode == 1, probe_run.stdout
    report = probe_run.report()

    assert report["schema_version"] == 1
    assert report["plugin"] == {"name": "testmatrix", "version": "0.1.0"}
    assert report["timestamp"].endswith("+00:00")
    assert report["exit_status"] == 1

    execution = report["execution_stats"]
    assert execution["total"] == 3
    assert execution["passed"] == 1
    assert execution["failed"] == 1
    assert execution["skipped"] == 1
    assert execution["duration"] > 0.0
    assert report["collection_stats"]["marker_counts"]["smoke"] == 1

    config = SimpleNamespace(rootpath=tmp_path)
    relative_config = plugin.PluginConfig(
        report_path="output/nested/report.json",
        report_enabled=True,
        summary_enabled=True,
    )
    assert plugin.resolve_report_path(config, relative_config) == (
        tmp_path / "output" / "nested" / "report.json"
    )

    elsewhere = tmp_path / "elsewhere.json"
    absolute_config = plugin.PluginConfig(
        report_path=str(elsewhere),
        report_enabled=True,
        summary_enabled=True,
    )
    assert plugin.resolve_report_path(config, absolute_config) == elsewhere

    written = tmp_path / "nested" / "written.json"
    assert plugin.write_report_file(written, {"ok": True}) is None
    assert json.loads(written.read_text(encoding="utf-8")) == {"ok": True}

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    write_error = plugin.write_report_file(blocked, {"ok": False})
    assert write_error is not None and "Error" in write_error


@allure.feature("Day56 自定义pytest插件")
@allure.story("日志钩子")
@allure.title("runtest_logreport 记录 setup/call/teardown 三阶段结果与耗时")
def test_logreport_records_phase_outcomes_and_duration(
    plugin: ModuleType, probe_run: ProbeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    端到端: 报告里的阶段分布必须与探针的真实执行过程吻合——teardown 对每条
    用例都会跑（3 条）、call 只对未跳过的用例产生（2 条）、setup 覆盖全部 3 条；
    阶段报告总条数是这三者之和。

    直接驱动: 逐组 (阶段, 结果) 驱动钩子，把计数口径钉死；同时覆盖两条兜底
    路径——统计容器缺失时直接跳过、记录过程抛错时静默返回（都不得影响用例）。
    """
    execution = probe_run.report()["execution_stats"]
    assert execution["phase_counts"] == {
        "setup": {"passed": 2, "skipped": 1},
        "call": {"passed": 1, "failed": 1},
        "teardown": {"passed": 3},
    }
    assert execution["report_count"] == 8

    stats = plugin.SessionExecutionStats()
    monkeypatch.setattr(plugin._STATE, "stats", stats)
    matrix = [
        ("call", "passed"),
        ("call", "failed"),
        ("call", "skipped"),
        ("call", "unknown_outcome"),
        ("setup", "passed"),
        ("setup", "failed"),
        ("setup", "skipped"),
        ("teardown", "passed"),
        ("teardown", "failed"),
        ("teardown", "skipped"),
    ]
    for phase, outcome in matrix:
        plugin.pytest_runtest_logreport(_report_stub(phase, outcome, duration=0.5))
    plugin.pytest_runtest_logreport(_report_stub("non_pytest_phase", "passed"))

    assert stats.report_count == len(matrix)
    assert stats.passed == 1
    assert stats.failed == 1
    assert stats.skipped == 2
    assert stats.errors == 2
    # 阶段分布按"实际收到的 outcome 字面量"计数，未知 outcome 也如实登记；
    # 用例级计数则只认标准结果，两者口径刻意不同（分布用于诊断，计数用于报告）
    assert stats.phase_counts["call"] == {
        "passed": 1,
        "failed": 1,
        "skipped": 1,
        "unknown_outcome": 1,
    }
    # 非常规阶段只被跳过，不会在阶段分布里凭空造出一个键
    assert "non_pytest_phase" not in stats.phase_counts

    plugin.pytest_runtest_logreport(_ExplodingReport())
    assert stats.report_count == len(matrix)

    monkeypatch.setattr(plugin._STATE, "stats", None)
    plugin.pytest_runtest_logreport(_report_stub("call", "failed"))


@allure.feature("Day56 自定义pytest插件")
@allure.story("结果上报")
@allure.title("失败用例汇总包含失败用例 ID，error 与 failed 口径分离")
def test_failed_cases_are_collected(
    plugin: ModuleType, probe_run: ProbeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    端到端: 失败明细必须点名到 nodeid、标出失败阶段，且**只含真正没通过的用例**
    ——被跳过的用例出现在这里会让"哪些用例挂了"这个索引直接失真。

    直接驱动: call 阶段失败计 failed、setup/teardown 阶段失败计 errors，
    两者都进失败明细但分属不同计数，绝不合流。
    """
    failed_cases = probe_run.report()["execution_stats"]["failed_cases"]
    assert len(failed_cases) == 1
    assert failed_cases[0]["nodeid"].endswith("::test_probe_fail")
    assert failed_cases[0]["phase"] == "call"
    assert failed_cases[0]["duration"] >= 0.0

    stats = plugin.SessionExecutionStats()
    monkeypatch.setattr(plugin._STATE, "stats", stats)
    for phase in ("call", "setup", "teardown"):
        plugin.pytest_runtest_logreport(
            _report_stub(phase, "failed", nodeid=f"probe.py::test_{phase}", duration=0.25)
        )

    assert stats.failed == 1
    assert stats.errors == 2
    assert [case["phase"] for case in stats.failed_cases] == [
        "call",
        "setup",
        "teardown",
    ]
    assert stats.failed_cases[0]["nodeid"] == "probe.py::test_call"
    assert stats.failed_cases[0]["duration"] == 0.25


@allure.feature("Day56 自定义pytest插件")
@allure.story("结果上报")
@allure.title("跳过用例正确计入 skipped 且不污染失败明细")
def test_skipped_cases_are_counted(
    plugin: ModuleType, probe_run: ProbeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    端到端: `@pytest.mark.skip` 的用例必须计入 skipped（既不能漏、也不能被算成
    失败），且**不得出现在失败明细里**——setup 阶段 skipped 分支漏掉提前返回时，
    跳过用例会掉进失败明细的追加逻辑，这是本插件开发期真实出现过的一处缺陷，
    本条即其回归守护。

    直接驱动: call 阶段跳过（用例体内 pytest.skip）与 setup 阶段跳过（fixture
    级跳过）两条路径的计数口径一致，且都不进失败明细。
    """
    execution = probe_run.report()["execution_stats"]
    assert execution["skipped"] == 1
    assert all(
        "::test_probe_skip" not in case["nodeid"] for case in execution["failed_cases"]
    )

    stats = plugin.SessionExecutionStats()
    monkeypatch.setattr(plugin._STATE, "stats", stats)
    plugin.pytest_runtest_logreport(
        _report_stub("call", "skipped", nodeid="probe.py::test_in_body", duration=0.1)
    )
    plugin.pytest_runtest_logreport(
        _report_stub("setup", "skipped", nodeid="probe.py::test_fixture", duration=0.2)
    )

    assert stats.skipped == 2
    assert stats.failed_cases == []
    assert stats.total == 2


@allure.feature("Day56 自定义pytest插件")
@allure.story("插件配置")
@allure.title("ini 配置项控制报告路径与开关，读取失败时回落默认值")
def test_plugin_options_control_report_output(
    plugin: ModuleType, probe_run: ProbeRun
) -> None:
    """
    端到端: `-o testmatrix_report_path=<路径>` 必须让报告落到指定位置；
    `-o testmatrix_report_enabled=false` 必须让报告**一个字节都不写**
    （两个方向都验，只验前者无法区分"配置生效"与"配置被忽略"）。

    直接驱动: 配置项注册口径（三条 ini 的名字、类型与默认值）与读取的两个
    分支——ini 正常返回时逐字段落地，ini 不可读时整体回落默认值。
    """
    custom_path = probe_run.report_path.parent / "custom_report.json"
    custom = _run_nested_pytest(
        ["-q", "-o", f"testmatrix_report_path={custom_path}"],
        [probe_run.target],
        probe_run.basetemp,
    )
    assert custom.returncode == 1, _decode(custom.stderr)
    assert custom_path.is_file()
    assert json.loads(custom_path.read_text(encoding="utf-8"))["exit_status"] == 1

    disabled_path = probe_run.report_path.parent / "disabled_report.json"
    disabled = _run_nested_pytest(
        [
            "-q",
            "-o",
            f"testmatrix_report_path={disabled_path}",
            "-o",
            "testmatrix_report_enabled=false",
        ],
        [probe_run.target],
        probe_run.basetemp,
    )
    assert disabled.returncode == 1, _decode(disabled.stderr)
    assert not disabled_path.exists()
    assert "会话完成" in _decode(disabled.stdout)

    parser = _RecordingParser()
    plugin.pytest_addoption(parser)
    assert parser.registered == [
        (plugin.INI_REPORT_PATH, "string", plugin.DEFAULT_REPORT_PATH),
        (plugin.INI_REPORT_ENABLED, "bool", True),
        (plugin.INI_SUMMARY_ENABLED, "bool", True),
    ]

    values = {
        plugin.INI_REPORT_PATH: "output/custom.json",
        plugin.INI_REPORT_ENABLED: False,
        plugin.INI_SUMMARY_ENABLED: True,
    }
    parsed = plugin.read_plugin_config(SimpleNamespace(getini=lambda name: values[name]))
    assert (parsed.report_path, parsed.report_enabled, parsed.summary_enabled) == (
        "output/custom.json",
        False,
        True,
    )

    fallback = plugin.read_plugin_config(_BrokenIniConfig())
    assert (fallback.report_path, fallback.report_enabled, fallback.summary_enabled) == (
        plugin.DEFAULT_REPORT_PATH,
        True,
        True,
    )

    blank_config = SimpleNamespace(
        getini=lambda name: "" if name == plugin.INI_REPORT_PATH else True
    )
    assert plugin.read_plugin_config(blank_config).report_path == (
        plugin.DEFAULT_REPORT_PATH
    )


@allure.feature("Day56 自定义pytest插件")
@allure.story("与conftest收集统计集成")
@allure.title("插件从 conftest 写入的 stash 中读到收集统计")
def test_collection_stats_integration_with_conftest_stash(
    plugin: ModuleType, probe_run: ProbeRun, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    端到端: 报告里的收集统计必须来自项目根 conftest 的写入口径——总数等于
    探针收集数、marker 分布按标记命中计数、模块分布覆盖探针模块。

    直接驱动: 遍历插件列表时的四种情形——没有插件带该键、插件带键但 stash
    里没有值、stash 里的值不是统计对象、值正常；前三种都必须回 None 而不抛错
    （插件不得因 conftest 缺失或版本不匹配而崩掉整个收尾流程）。收尾钩子
    再完整走一遍，确认摘要行输出的就是这份统计，且会话状态被清空。
    """
    collection = probe_run.report()["collection_stats"]
    assert collection is not None
    assert collection["total"] == 3
    assert collection["marker_counts"]["smoke"] == 1
    assert collection["marker_counts"]["regression"] == 1
    assert sum(collection["module_counts"].values()) == 3
    assert "test_day56_probe" in "".join(collection["module_counts"])
    assert collection["unregistered_marker_counts"] == {}

    stats_key = pytest.StashKey()
    plugin_with_key = _PluginWithStatsKey(stats_key)

    no_key_config = SimpleNamespace(
        pluginmanager=SimpleNamespace(get_plugins=lambda: {object()}),
        stash=pytest.Stash(),
    )
    assert plugin.collect_collection_stats(no_key_config) is None

    empty_stash_config = SimpleNamespace(
        pluginmanager=SimpleNamespace(get_plugins=lambda: {plugin_with_key}),
        stash=pytest.Stash(),
    )
    assert plugin.collect_collection_stats(empty_stash_config) is None

    alien_stash = pytest.Stash()
    alien_stash[stats_key] = 12345
    alien_config = SimpleNamespace(
        pluginmanager=SimpleNamespace(get_plugins=lambda: {plugin_with_key}),
        stash=alien_stash,
    )
    assert plugin.collect_collection_stats(alien_config) is None

    real_stash = pytest.Stash()
    real_stash[stats_key] = SimpleNamespace(
        total=FAKE_COLLECTION_TOTAL,
        marker_counts={"smoke": 2},
        module_counts={"tests.test_fake": FAKE_COLLECTION_TOTAL},
        unregistered_marker_counts={"e2e": 1},
    )
    assert plugin.collect_collection_stats(
        SimpleNamespace(
            pluginmanager=SimpleNamespace(get_plugins=lambda: {plugin_with_key}),
            stash=real_stash,
        )
    ) == {
        "total": FAKE_COLLECTION_TOTAL,
        "marker_counts": {"smoke": 2},
        "module_counts": {"tests.test_fake": FAKE_COLLECTION_TOTAL},
        "unregistered_marker_counts": {"e2e": 1},
    }

    captured: list[str] = []
    values = {
        plugin.INI_REPORT_PATH: str(probe_run.report_path.parent / "finish.json"),
        plugin.INI_REPORT_ENABLED: True,
        plugin.INI_SUMMARY_ENABLED: True,
    }
    session_config = SimpleNamespace(
        rootpath=probe_run.report_path.parent,
        getini=lambda name: values[name],
        pluginmanager=SimpleNamespace(
            get_plugins=lambda: {plugin_with_key},
            get_plugin=lambda name: SimpleNamespace(write_line=captured.append),
        ),
        stash=real_stash,
    )
    monkeypatch.setattr(plugin._STATE, "config", None)
    monkeypatch.setattr(plugin._STATE, "stats", None)

    # 状态为空时两个会话钩子都必须安全返回（不得抛错，也不得写任何输出）
    plugin.pytest_sessionstart(SimpleNamespace())
    plugin.pytest_sessionfinish(SimpleNamespace(config=session_config), 0)
    assert captured == []

    # 没有 terminalreporter 时摘要写入静默跳过（`-p no:terminal` 场景）
    plugin.emit_message(
        SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda name: None)),
        "丢弃",
    )

    def _broken_plugins() -> set[Any]:
        """插件枚举即抛错的替身（模拟插件管理器本身不可用）"""
        raise RuntimeError("插件管理器不可用")

    assert plugin.collect_collection_stats(
        SimpleNamespace(
            pluginmanager=SimpleNamespace(get_plugins=_broken_plugins),
            stash=pytest.Stash(),
        )
    ) is None

    # 正常会话: configure → sessionstart → sessionfinish
    plugin.pytest_configure(session_config)
    assert plugin._STATE.config is session_config
    plugin.pytest_sessionstart(SimpleNamespace())
    assert plugin._STATE.stats.started_monotonic > 0.0

    plugin.pytest_sessionfinish(SimpleNamespace(config=session_config), 0)
    assert captured and captured[0].startswith(plugin.SUMMARY_PREFIX)
    assert "0 用例" in captured[0]
    assert plugin._STATE.stats is None

    finished = json.loads(
        (probe_run.report_path.parent / "finish.json").read_text(encoding="utf-8")
    )
    assert finished["collection_stats"]["total"] == FAKE_COLLECTION_TOTAL
    assert finished["execution_stats"]["total"] == 0

    # 报告写入失败只降级为一条提示行（目标路径被同名目录占用 → 真实 OSError）
    blocked_dir = probe_run.report_path.parent / "blocked_target"
    blocked_dir.mkdir()
    blocked_captured: list[str] = []
    blocked_values = {
        plugin.INI_REPORT_PATH: str(blocked_dir),
        plugin.INI_REPORT_ENABLED: True,
        plugin.INI_SUMMARY_ENABLED: True,
    }
    blocked_config = SimpleNamespace(
        rootpath=probe_run.report_path.parent,
        getini=lambda name: blocked_values[name],
        pluginmanager=SimpleNamespace(
            get_plugins=lambda: {plugin_with_key},
            get_plugin=lambda name: SimpleNamespace(write_line=blocked_captured.append),
        ),
        stash=real_stash,
    )
    plugin._STATE.start_session(blocked_config)
    plugin.pytest_sessionfinish(SimpleNamespace(config=blocked_config), 0)
    assert any("报告写入失败" in line for line in blocked_captured)
    assert blocked_captured[-1].startswith(plugin.SUMMARY_PREFIX)
    assert plugin._STATE.stats is None

    plugin.pytest_unconfigure(session_config)
    assert plugin._STATE.config is None


@allure.feature("Day56 自定义pytest插件")
@allure.story("非侵入性")
@allure.title("启用插件不改变用例的收集数、结果与退出码")
def test_plugin_does_not_disturb_test_results(
    plugin: ModuleType, probe_run: ProbeRun
) -> None:
    """
    同一份探针在"加载插件"与"不加载插件"两种配置下各跑一次，用项目自己的
    输出解析器（`PytestOutputParser`）解析两次的真实终端输出并逐项比对——
    通过/失败/跳过计数与退出码必须完全一致，这是"插件只做旁路、不动用例
    结果"最直接的证据（只跑一次无法排除"插件恰好没影响到"的巧合）。

    同时覆盖直接驱动路径上的两个退化输入: 统计容器为空时的耗时计算、
    摘要行字段缺失或类型异常时的默认值渲染。
    """
    without_plugin = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:src.testmatrix_plugin",
            "-p",
            f"no:{ENTRY_POINT_NAME}",
            "-q",
            "--tb=no",
            "--basetemp",
            str(probe_run.basetemp),
            probe_run.target,
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        env=_nested_env(),
        check=False,
        timeout=NESTED_PYTEST_TIMEOUT_SECONDS,
    )
    baseline = PytestOutputParser.parse(_decode(without_plugin.stdout))
    enabled = PytestOutputParser.parse(probe_run.stdout)

    assert baseline.parse_ok and enabled.parse_ok
    assert without_plugin.returncode == probe_run.returncode == 1
    assert (baseline.passed_count, baseline.failed_count, baseline.skipped_count) == (
        enabled.passed_count,
        enabled.failed_count,
        enabled.skipped_count,
    ) == (1, 1, 1)

    empty_stats = plugin.SessionExecutionStats()
    assert plugin.elapsed_seconds(empty_stats) == 0.0
    empty_stats.started_monotonic = time.perf_counter()
    assert plugin.elapsed_seconds(empty_stats) >= 0.0

    assert plugin.format_plugin_summary({}).endswith("耗时 0.00s")
    assert "耗时 0.00s" in plugin.format_plugin_summary(
        {"execution_stats": {"total": 1, "duration": "非数字"}}
    )
