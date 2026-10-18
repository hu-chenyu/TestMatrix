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

Day57 追加 4 条（打包验证日）:
  1. **打包契约**: 构建后端声明、白名单包发现对 src/ 下真实目录的筛选语义、
    三处版本号同口径（pyproject / 插件常量 / 报告）。
  2. **报告 schema**: 字段集、类型与"无收集统计时给 null"的空值语义。
  3. **写入失败第二成因**: 父路径被同名文件占用（与既有"目标路径被目录占用"互补）。
  4. **终端降级**: writer 存在却抛错时的旁路铁律边界（修复前会把异常抛出收尾钩子）。

Day58 追加 8 条（xdist 并发接入日）:
  1. **进程角色判定**: worker 专有通道属性为主、环境变量只作替身兜底
    （子进程会继承 `PYTEST_XDIST_WORKER`，单凭它判定会把普通 pytest 进程误判成 worker）。
  2. **worker 零副作用**: 会话收尾不写报告、不出摘要、不清状态。
  3. **master 唯一落盘**: `-n 2` 真跑一次，报告一份、摘要一条、口径是整场会话。
  4. **收集统计上报通道**: worker 经 `config.workeroutput` 上报、master 合并。
  5-8. **四条 P4 降级**: `get_plugin` 抛错 / payload 不可序列化 / 收尾抛错仍清状态 /
    源码无非法转义告警。

Day59 追加 2 条（Day58 复核归位）:
  1. **崩溃 worker 的 error 路径**: `pytest_testnodedown` 的 `error` 非 None 且
    `workeroutput` 缺失/为 None 时，合并逻辑不抛错、保留先到 worker 的统计。
  2. **嵌套 pytest 的 env 注入**: `_run_nested_pytest` 的可选 `env` 参数按"叠加"
    语义生效（同名覆盖、基础 `PYTHONPATH` 不丢），供需要显式注入
    `PYTEST_XDIST_WORKER` 等变量的场景解耦使用。
"""

import fnmatch
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import warnings
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

# 插件包目录名（`[tool.setuptools.packages.find]` 的 include 必须只命中它）
PLUGIN_PACKAGE_DIR = "testmatrix_plugin"

# src/ 下的业务子包（必须被白名单挡在分发包之外；前置守卫与断言共用同一份清单）
BUSINESS_PACKAGE_DIRS: frozenset[str] = frozenset(
  {"common", "core", "db", "web", "scripts"}
)

# 报告 JSON 的字段集契约（下游按键取值，缺键会静默取到 None 而不是报错）
REPORT_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
  {
    "schema_version",
    "plugin",
    "timestamp",
    "exit_status",
    "collection_stats",
    "execution_stats",
  }
)
REPORT_PLUGIN_KEYS: frozenset[str] = frozenset({"name", "version"})
REPORT_COLLECTION_KEYS: frozenset[str] = frozenset(
  {"total", "marker_counts", "module_counts", "unregistered_marker_counts"}
)
REPORT_EXECUTION_KEYS: frozenset[str] = frozenset(
  {
    "total",
    "passed",
    "failed",
    "errors",
    "skipped",
    "duration",
    "report_count",
    "failed_cases",
    "phase_counts",
  }
)
REPORT_FAILED_CASE_KEYS: frozenset[str] = frozenset({"nodeid", "phase", "duration"})

# xdist 并发探针的 worker 数（与 ci.yml 的 -n 参数同口径；固定值不用 auto——
# auto 会按核数探测，行为与耗时不可预期，也无法断言"确实起了 N 个 worker"）
XDIST_WORKER_COUNT = 2


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


class _ExplodingTerminal:
  """
  写一行即抛错的 terminalreporter 替身

  模拟"writer 存在但已不可写"（终端关闭 / 输出管道断开）——它与
  "没有 writer"（`-p no:terminal`）是两种不同成因，前者此前未被覆盖。
  """


class _ExplodingPluginManager:
  """
  取插件即抛错的 pluginmanager 替身（Day58 P4-1）

  模拟"插件管理器本身状态异常"（插件卸载顺序问题、管理器未就绪等）: 此时
  `get_plugin("terminalreporter")` 会直接抛错，而"没有 writer"是返回 None。
  两者都必须只降级、不得把展示层故障升级成会话故障。
  """

  def get_plugin(self, name: str) -> Any:
    """
    参数:
      name (str): 插件名（替身直接抛错）

    返回:
      无（必然抛错）

    异常:
      RuntimeError: 恒抛出，驱动 `emit_message` 的降级分支
    """
    raise RuntimeError(f"插件管理器不可用: {name}")

  def get_plugins(self) -> set[Any]:
    """
    参数:
      无

    返回:
      无（必然抛错）

    异常:
      RuntimeError: 恒抛出，驱动收集统计读取的降级分支
    """
    raise RuntimeError("插件枚举不可用")

  def write_line(self, message: str) -> None:
    """
    参数:
      message (str): 待输出文本（替身直接抛错）

    返回:
      无（必然抛错）

    异常:
      ValueError: 恒抛出，驱动插件收尾钩子的降级分支
    """
    raise ValueError(f"终端不可写: {len(message)} 字符")


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
  extra_args: list[str],
  targets: list[str],
  basetemp: Path,
  env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
  """
  跑一次嵌套 pytest（显式加载被测插件、禁用 entry point 那一份）

  刻意显式指定 `--basetemp`: 不自带时子进程会与**主会话**共用同一个
  `%TEMP%/pytest-of-<user>/` 根目录，多进程交替创建/回收 `garbage-*`
  目录在 Windows 上会偶发删除失败并抛出 `PytestWarning`（环境层噪声，
  会污染"0 warning"门禁）。把它钉在探针目录下（已被 .gitignore 覆盖，
  且随 fixture 一并清理）即从结构上消除这类争用。

  `env` 参数刻意做成"叠加"而不是"替换"（Day59 P3-④）: 调用方往往只想
  补一个变量（例如显式注入 `PYTEST_XDIST_WORKER` 复现 worker 身份），
  替换整份环境会丢掉 `PYTHONPATH` 与编码设置，让子进程加载不到插件。

  参数:
    extra_args (list[str]): 追加在固定参数之后、目标之前的参数
    targets (list[str]): 传给 pytest 的目标路径
    basetemp (Path): 子进程专属的基础临时目录（存在即被清空重建）
    env (dict[str, str] | None): 额外注入的环境变量（None 表示不额外注入）；
      同名键覆盖 `_nested_env()` 的默认值

  返回:
    subprocess.CompletedProcess[bytes]: 子进程结果（含 returncode 与原始输出）

  异常:
    subprocess.TimeoutExpired: 子进程超出预算时抛出（视为严重失败）
  """
  child_env = _nested_env()
  if env:
    child_env.update(env)
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
    env=child_env,
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


@allure.feature("Day57 插件打包验证")
@allure.story("打包契约")
@allure.title("打包元数据把业务子包挡在分发之外，且三处版本号同口径")
def test_packaging_metadata_whitelists_only_plugin_package(
  plugin: ModuleType, probe_run: ProbeRun
) -> None:
  """
  静态声明（无条件执行，CI 未安装本项目时同样生效）:
    1. `[build-system]` 必须声明 setuptools 构建后端——本仓没有 setup.py /
      setup.cfg，缺这一段时 pip 会走 legacy 路径直接报"无法确定构建后端"。
    2. `[project]` 的版本必须与插件常量、以及真实报告里的 `plugin.version`
      三处同口径——"装的 0.1.0 但报告写 0.2.0"这类漂移只能靠断言发现。
    3. 白名单包发现的**语义**要拿 `src/` 下的真实目录逐一匹配验证: include
      规则命中的目录集合必须恰好是插件包。只断言配置文本等于某字面量的话，
      "配置没动但 src 下新增了一个业务包"这种情况会被整个漏掉。

  补充证据（仅本项目已安装时）: `importlib.metadata` 记录的发行文件里不得出现
  业务子包路径——这是"装出来的东西真的只含插件"的产物级证据；
  CI 不安装本项目，该条件分支不触发，而上面的静态断言已覆盖注册契约。
  """
  pyproject = tomllib.loads(
    (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
  )
  build_system = pyproject["build-system"]
  assert build_system["build-backend"] == "setuptools.build_meta"
  assert any(
    requirement.startswith("setuptools") for requirement in build_system["requires"]
  ), build_system["requires"]

  project = pyproject["project"]
  assert project["name"] == "TestMatrix"
  assert project["version"] == plugin.PLUGIN_VERSION
  assert probe_run.report()["plugin"]["version"] == plugin.PLUGIN_VERSION

  setuptools_config = pyproject["tool"]["setuptools"]
  assert setuptools_config["package-dir"] == {"": "src"}
  find_config = setuptools_config["packages"]["find"]
  assert find_config["where"] == ["src"]
  patterns = find_config["include"]

  src_dirs = {
    path.name
    for path in (PROJECT_ROOT / "src").iterdir()
    if path.is_dir() and path.name != "__pycache__"
  }
  # 前置守卫: 业务子包与插件包都必须真实存在，否则下面的"都不命中"会退化成永真
  assert BUSINESS_PACKAGE_DIRS <= src_dirs, sorted(src_dirs)
  assert PLUGIN_PACKAGE_DIR in src_dirs, sorted(src_dirs)

  matched = {
    name
    for name in src_dirs
    if any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
  }
  assert matched == {PLUGIN_PACKAGE_DIR}, sorted(matched)

  try:
    distribution = importlib.metadata.distribution("TestMatrix")
  except importlib.metadata.PackageNotFoundError:
    distribution = None
  if distribution is not None:
    recorded = distribution.files or []
    top_levels = {path.parts[0] for path in recorded if path.parts}
    assert not (top_levels & BUSINESS_PACKAGE_DIRS), sorted(top_levels)


@allure.feature("Day57 插件打包验证")
@allure.story("结果上报")
@allure.title("报告 JSON 的字段集、类型与空值语义稳定")
def test_report_payload_schema_is_complete_and_stable(
  plugin: ModuleType, probe_run: ProbeRun
) -> None:
  """
  端到端: 真实会话报告逐层核对字段集与类型，`phase_counts` 固定输出
  setup / call / teardown 三键（下游按键取值不必先判存在性）。

  直接驱动: 没有收集统计（项目根 conftest 未加载）时 `collection_stats`
  必须是 **null 而不是缺键**——缺键与"读不到统计"在消费侧不可区分，
  而这两件事的处置完全不同；空会话的计数为 0、耗时为 0.0，整份 payload
  可直接 json.dumps（报告的下游形态就是磁盘上的 JSON）。
  """
  report = probe_run.report()
  assert set(report) == REPORT_TOP_LEVEL_KEYS
  assert set(report["plugin"]) == REPORT_PLUGIN_KEYS
  assert set(report["collection_stats"]) == REPORT_COLLECTION_KEYS

  execution = report["execution_stats"]
  assert set(execution) == REPORT_EXECUTION_KEYS
  assert set(execution["phase_counts"]) == set(plugin.EXECUTION_PHASES)

  assert isinstance(report["schema_version"], int)
  assert report["schema_version"] == plugin.REPORT_SCHEMA_VERSION
  assert isinstance(report["exit_status"], int)
  assert isinstance(report["timestamp"], str)
  for counter in ("total", "passed", "failed", "errors", "skipped", "report_count"):
    assert isinstance(execution[counter], int), counter
  assert isinstance(execution["duration"], float)
  assert isinstance(execution["failed_cases"], list)
  for failed_case in execution["failed_cases"]:
    assert set(failed_case) == REPORT_FAILED_CASE_KEYS, failed_case
    assert isinstance(failed_case["duration"], float)
  for phase_counts in execution["phase_counts"].values():
    assert all(isinstance(count, int) for count in phase_counts.values())

  empty = plugin.build_report_payload(plugin.SessionExecutionStats(), None, 0, 0.0)
  assert empty["collection_stats"] is None
  assert empty["execution_stats"] == {
    "total": 0,
    "passed": 0,
    "failed": 0,
    "errors": 0,
    "skipped": 0,
    "duration": 0.0,
    "report_count": 0,
    "failed_cases": [],
    "phase_counts": {phase: {} for phase in plugin.EXECUTION_PHASES},
  }
  assert json.dumps(empty, ensure_ascii=False)


@allure.feature("Day57 插件打包验证")
@allure.story("结果上报")
@allure.title("报告写入失败的第二成因（父路径被同名文件占用）同样只降级不抛错")
def test_write_report_file_degrades_when_parent_is_not_directory(
  plugin: ModuleType, tmp_path: Path
) -> None:
  """
  契约是"失败只回传错误描述、绝不抛异常"，两个成因必须都验: "目标路径已被
  同名目录占用"（既有断言）与**父路径被同名文件占用**（`mkdir` 在真正写文件
  之前就抛 `OSError`，Windows 实测为 FileExistsError）。只测前者会让
  `mkdir` 这一步的异常处理失去牙齿。

  同时断言那个占位文件**未被改写**：降级路径不得顺手删掉或覆盖别人的文件，
  否则"报告写不出去"会升级成"写坏了一个无关文件"。
  """
  occupied = tmp_path / "plain.txt"
  original = "占位文件，不是目录"
  occupied.write_text(original, encoding="utf-8")
  target = occupied / "nested" / "report.json"

  write_error = plugin.write_report_file(target, {"ok": False})

  assert write_error is not None
  # 契约格式为 `<异常类型>: <消息>`，异常类型名以 Error 结尾（跨平台同为 OSError 子类）
  assert write_error.split(":")[0].endswith("Error"), write_error
  assert not target.exists()
  assert occupied.is_file()
  assert occupied.read_text(encoding="utf-8") == original


@allure.feature("Day57 插件打包验证")
@allure.story("旁路铁律边界")
@allure.title("终端 writer 抛错时会话收尾仍安全返回且报告照常落盘")
def test_emit_message_survives_terminal_writer_failure(
  plugin: ModuleType, tmp_path: Path
) -> None:
  """
  旁路铁律的边界: "终端**没有** writer"已有降级（`-p no:terminal` 时静默跳过），
  但"writer 存在却写失败"（终端已关闭 / 输出管道断开）此前会把异常抛出收尾钩子
 ——那等于让展示层故障升级成会话故障（pytest 会记成 INTERNALERROR），
  与"任何一步意外都只降级为一条提示、绝不改变退出码与用例结果"的承诺相反。

  本用例驱动修复后的行为: 摘要丢弃、报告仍落盘、会话状态仍被清空
  （清空在钩子末尾，异常一旦穿透就等于状态残留给下一次同进程会话）。
  """
  report_path = tmp_path / "broken_terminal_report.json"
  values = {
    plugin.INI_REPORT_PATH: str(report_path),
    plugin.INI_REPORT_ENABLED: True,
    plugin.INI_SUMMARY_ENABLED: True,
  }
  broken_config = SimpleNamespace(
    rootpath=tmp_path,
    getini=lambda name: values[name],
    pluginmanager=SimpleNamespace(
      get_plugins=lambda: set(),
      get_plugin=lambda name: _ExplodingTerminal(),
    ),
    stash=pytest.Stash(),
  )

  plugin._STATE.start_session(broken_config)
  plugin.pytest_sessionfinish(SimpleNamespace(config=broken_config), 0)

  assert report_path.is_file()
  assert json.loads(report_path.read_text(encoding="utf-8"))["exit_status"] == 0
  assert plugin._STATE.stats is None
  assert plugin._STATE.config is None


@allure.feature("Day58 xdist 并发接入")
@allure.story("进程角色判定")
@allure.title("worker 判定以 worker 专有通道属性为准，继承来的环境变量不得单独生效")
def test_is_xdist_worker_prefers_worker_channels_over_inherited_env(
  plugin: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
  """
  判定口径分三层:
    1. `workerinput` / `workeroutput` 任一存在 → worker（xdist 只给 worker 注入）；
    2. 拿不到 pluginmanager 的替身配置 → 退回 `PYTEST_XDIST_WORKER` 环境变量；
    3. 有 pluginmanager 但两个通道属性都没有 → master（串行会话同样落在这里）。

  第 3 条是**实测踩到的回归点**: `PYTEST_XDIST_WORKER` 是进程环境变量，会被
  子进程继承。真实执行器拉起的 pytest 子进程、本文件的嵌套 pytest，只要父进程
  是 xdist worker 就天然带着它；若单凭环境变量判定，这些"普通 pytest 进程"会
  被误判成 worker，表现为报告静默不落盘、摘要不输出（CI 一旦启用 `-n 2`，
  所有嵌套 pytest 用例都会撞上）。
  """
  monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)

  # 串行会话: 有 pluginmanager、无通道属性
  assert (
    plugin._is_xdist_worker(SimpleNamespace(pluginmanager=SimpleNamespace()))
    is False
  )
  # 两个 worker 专有通道各自成立
  assert plugin._is_xdist_worker(SimpleNamespace(workerinput={"workerid": "gw0"})) is True
  assert plugin._is_xdist_worker(SimpleNamespace(workeroutput={})) is True

  # 环境变量被子进程继承: 有 pluginmanager 时不得据此判成 worker
  monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
  assert (
    plugin._is_xdist_worker(SimpleNamespace(pluginmanager=SimpleNamespace()))
    is False
  )
  # 只有拿不到 pluginmanager 的替身配置才回落到环境变量
  assert plugin._is_xdist_worker(SimpleNamespace()) is True

  class _ExplodingChannels:
    """通道属性访问即抛错的配置替身（判定失败必须按 master 处理）"""

    @property
    def workerinput(self) -> Any:
      """
      参数:
        无

      返回:
        无（必然抛错）

      异常:
        RuntimeError: 恒抛出，驱动判定函数的兜底分支
      """
      raise RuntimeError("通道属性不可用")

  assert plugin._is_xdist_worker(_ExplodingChannels()) is False


@allure.feature("Day58 xdist 并发接入")
@allure.story("worker 零副作用")
@allure.title("worker 进程的会话收尾不写报告、不输出摘要、不清状态")
def test_sessionfinish_in_worker_has_no_plugin_side_effects(
  plugin: ModuleType, tmp_path: Path
) -> None:
  """
  worker 侧"零副作用"逐项取证: 报告文件不存在、终端一行未写、`_STATE` 未被
  清空（worker 的清理统一交给 `pytest_unconfigure`，进程随会话结束退出）。

  为什么 worker 不能落盘也不能出摘要: 报告是固定单一路径，多进程写同一个文件
  等于"最后落盘者胜"；摘要按进程数重复输出后，终端上分不清哪一行才是整场会话
  的口径。落盘与输出权只归 master，是本日改造的核心约束。
  """
  report_path = tmp_path / "worker_report.json"
  emitted: list[str] = []
  values = {
    plugin.INI_REPORT_PATH: str(report_path),
    plugin.INI_REPORT_ENABLED: True,
    plugin.INI_SUMMARY_ENABLED: True,
  }
  worker_config = SimpleNamespace(
    rootpath=tmp_path,
    getini=lambda name: values[name],
    pluginmanager=SimpleNamespace(
      get_plugins=lambda: set(),
      get_plugin=lambda name: SimpleNamespace(write_line=emitted.append),
    ),
    stash=pytest.Stash(),
    # 两个 worker 通道属性都按 xdist 真实注入形态给出
    workerinput={"workerid": "gw0"},
    workeroutput={},
  )

  plugin._STATE.start_session(worker_config)
  plugin.pytest_sessionfinish(SimpleNamespace(config=worker_config), 1)

  assert not report_path.exists()
  assert emitted == []
  # 状态**刻意不清**: worker 退出即消亡，清理职责在 unconfigure
  assert plugin._STATE.stats is not None

  plugin.pytest_unconfigure(worker_config)
  assert plugin._STATE.stats is None
  assert plugin._STATE.config is None
  assert plugin._STATE.worker_collection_stats is None


@allure.feature("Day58 xdist 并发接入")
@allure.story("master 唯一落盘")
@allure.title("-n 2 真跑: 报告只落一份、摘要只出一条、口径是整场会话")
def test_xdist_run_keeps_single_report_and_single_summary(
  plugin: ModuleType, probe_run: ProbeRun
) -> None:
  """
  端到端验收（真起 xdist，非替身驱动）:
    1. **阳性对照**: 输出里必须出现 `created: 2/2 workers`，否则本条会退化成
      "串行也能过"（`-n` 被忽略时不会报错，断言会失去意义）；
    2. **摘要唯一**: `[TestMatrix] 会话完成` 只出现 1 次——每个 worker 各打一行
      是本日要消灭的形态；
    3. **口径完整**: 报告的执行侧计数是 3 条用例的全量结果（探针 1 通过 +
      1 失败 + 1 跳过）与 8 条阶段报告，而不是某个 worker 分到的那一部分；
    4. **收集统计不再为 null**: master 自身不做收集，靠 worker 上报补齐，
      故 `collection_stats.total` 必须是完整的 3；
    5. 失败明细只有 1 条——两份报告叠加/重复合并会让它变成 2 条。
  """
  report_path = probe_run.report_path.parent / "xdist_report.json"
  completed = _run_nested_pytest(
    [
      "-q",
      "-n",
      str(XDIST_WORKER_COUNT),
      "-o",
      f"testmatrix_report_path={report_path}",
    ],
    [probe_run.target],
    probe_run.basetemp,
  )
  stdout = _decode(completed.stdout)
  stderr = _decode(completed.stderr)

  created = re.search(r"created: (\d+)/\d+ workers", stdout)
  assert created is not None, stdout
  assert int(created.group(1)) == XDIST_WORKER_COUNT, stdout
  assert completed.returncode == 1, stderr
  assert "INTERNALERROR" not in stderr, stderr

  assert stdout.count(f"{plugin.SUMMARY_PREFIX} 会话完成") == 1, stdout

  report = json.loads(report_path.read_text(encoding="utf-8"))
  execution = report["execution_stats"]
  assert execution["total"] == 3
  assert (execution["passed"], execution["failed"], execution["skipped"]) == (1, 1, 1)
  assert execution["report_count"] == 8
  assert len(execution["failed_cases"]) == 1
  assert report["exit_status"] == 1

  collection = report["collection_stats"]
  assert collection is not None
  assert collection["total"] == 3
  assert collection["marker_counts"]["smoke"] == 1
  assert sum(collection["module_counts"].values()) == 3


@allure.feature("Day58 xdist 并发接入")
@allure.story("收集统计上报通道")
@allure.title("worker 上报收集统计、master 收下并按完整口径合并")
def test_worker_collection_stats_are_reported_and_merged(
  plugin: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
  """
  为什么需要这条通道（当日实测结论）: xdist 的 master **不做收集**——它的
  `pytest_collection` 直接 return True（禁止控制进程收集用例），因此项目根
  conftest 的 `pytest_collection_modifyitems` 不会在 master 执行，master 的
  stash 里也就没有收集统计，报告该字段只能是 null。真正持有完整统计的是
  worker（它们收集整场会话的全部用例、只执行其中一部分）。

  覆盖点:
    1. 合并口径是"取 total 更大的一份"而不是相加——各 worker 收集的是同一批
      用例，相加会把总数放大 N 倍；
    2. worker 侧只负责写 `config.workeroutput`（纯字典，不碰磁盘/终端/状态）；
    3. master 侧只认插件自己的键，坏数据（非字典、缺键、无该属性）一律忽略；
    4. master 进程自己不上报（它的统计来源是本进程 stash）。
  """
  stats_small = {
    "total": 2,
    "marker_counts": {"smoke": 1},
    "module_counts": {"m": 2},
    "unregistered_marker_counts": {},
  }
  stats_full = {
    "total": 5,
    "marker_counts": {"smoke": 1, "regression": 4},
    "module_counts": {"m": 5},
    "unregistered_marker_counts": {},
  }

  assert plugin.merge_worker_collection_stats(None, None) is None
  assert plugin.merge_worker_collection_stats(None, stats_small) is stats_small
  assert plugin.merge_worker_collection_stats(stats_full, None) is stats_full
  assert plugin.merge_worker_collection_stats(stats_small, stats_full) is stats_full
  assert plugin.merge_worker_collection_stats(stats_full, stats_small) is stats_full
  # total 非法时保留已有值，绝不因为一份坏统计丢掉全部
  assert plugin.merge_worker_collection_stats({"total": "非数字"}, stats_full) == {
    "total": "非数字"
  }

  stash_key = pytest.StashKey()
  stash = pytest.Stash()
  stash[stash_key] = SimpleNamespace(
    total=FAKE_COLLECTION_TOTAL,
    marker_counts={"smoke": 2},
    module_counts={"tests.test_fake": FAKE_COLLECTION_TOTAL},
    unregistered_marker_counts={"e2e": 1},
  )
  worker_output: dict[str, Any] = {}
  worker_config = SimpleNamespace(
    pluginmanager=SimpleNamespace(get_plugins=lambda: {_PluginWithStatsKey(stash_key)}),
    stash=stash,
    workerinput={"workerid": "gw0"},
    workeroutput=worker_output,
  )
  plugin.pytest_collection_finish(SimpleNamespace(config=worker_config))
  reported = worker_output[plugin.WORKER_COLLECTION_STATS_KEY]
  assert reported["total"] == FAKE_COLLECTION_TOTAL
  assert reported["unregistered_marker_counts"] == {"e2e": 1}

  # master 进程（无 worker 通道属性）不写任何上报
  master_output: dict[str, Any] = {}
  plugin.pytest_collection_finish(
    SimpleNamespace(
      config=SimpleNamespace(
        pluginmanager=SimpleNamespace(get_plugins=lambda: set()),
        stash=pytest.Stash(),
      )
    )
  )
  assert master_output == {}

  # worker 通道缺失（属性不是字典）与"stash 里没有统计"两条降级路径
  plugin.pytest_collection_finish(
    SimpleNamespace(config=SimpleNamespace(workerinput={}, workeroutput=None))
  )
  empty_output: dict[str, Any] = {}
  plugin.pytest_collection_finish(
    SimpleNamespace(
      config=SimpleNamespace(
        workerinput={"workerid": "gw9"},
        workeroutput=empty_output,
        pluginmanager=SimpleNamespace(get_plugins=lambda: set()),
        stash=pytest.Stash(),
      )
    )
  )
  assert empty_output == {}

  monkeypatch.setattr(plugin._STATE, "worker_collection_stats", None)
  plugin.pytest_testnodedown(
    SimpleNamespace(workeroutput={plugin.WORKER_COLLECTION_STATS_KEY: stats_small}), None
  )
  assert plugin._STATE.worker_collection_stats == stats_small
  plugin.pytest_testnodedown(
    SimpleNamespace(workeroutput={plugin.WORKER_COLLECTION_STATS_KEY: stats_full}), None
  )
  assert plugin._STATE.worker_collection_stats == stats_full

  for alien in (
    SimpleNamespace(workeroutput={"other_plugin": stats_small}),
    SimpleNamespace(workeroutput=[stats_small]),
    SimpleNamespace(),
  ):
    plugin.pytest_testnodedown(alien, None)
  assert plugin._STATE.worker_collection_stats == stats_full
  monkeypatch.setattr(plugin._STATE, "worker_collection_stats", None)


@allure.feature("Day58 xdist 并发接入")
@allure.story("P4-1 终端降级")
@allure.title("取 terminalreporter 本身抛错时静默降级，会话收尾与落盘不受影响")
def test_emit_message_survives_plugin_manager_failure(
  plugin: ModuleType, tmp_path: Path
) -> None:
  """
  P4-1 的靶子: `emit_message` 里 `get_plugin("terminalreporter")` 原先是裸调用。
  "没有 writer"（返回 None）已有降级，但**取 writer 这一步本身抛错**（插件管理器
  状态异常）会把展示层故障升级成会话故障。

  验两层: 单次调用安静返回；整条收尾流程仍照常落盘并清空状态。
  """
  plugin.emit_message(SimpleNamespace(pluginmanager=_ExplodingPluginManager()), "丢弃")

  report_path = tmp_path / "pm_failure_report.json"
  values = {
    plugin.INI_REPORT_PATH: str(report_path),
    plugin.INI_REPORT_ENABLED: True,
    plugin.INI_SUMMARY_ENABLED: True,
  }
  broken_config = SimpleNamespace(
    rootpath=tmp_path,
    getini=lambda name: values[name],
    pluginmanager=_ExplodingPluginManager(),
    stash=pytest.Stash(),
  )

  plugin._STATE.start_session(broken_config)
  plugin.pytest_sessionfinish(SimpleNamespace(config=broken_config), 0)

  assert report_path.is_file()
  assert json.loads(report_path.read_text(encoding="utf-8"))["collection_stats"] is None
  assert plugin._STATE.stats is None
  assert plugin._STATE.config is None


@allure.feature("Day58 xdist 并发接入")
@allure.story("P4-2 报告写入降级")
@allure.title("payload 不可序列化时只回传错误描述，不抛 TypeError/ValueError")
def test_write_report_file_degrades_on_unserializable_payload(
  plugin: ModuleType, tmp_path: Path
) -> None:
  """
  P4-2 的靶子: 捕获范围原先是 `OSError`，而 `json.dumps` 自己的失败既不是文件
  系统问题也不是 OSError——不可序列化对象抛 `TypeError`、循环引用抛
  `ValueError`，两者都会穿透 `pytest_sessionfinish`。

  契约仍是"失败只回传 `<异常类型>: <消息>`"，且**不得留下半份文件**。
  """
  target = tmp_path / "unserializable.json"

  type_error = plugin.write_report_file(target, {"bad": object()})
  assert type_error is not None and type_error.startswith("TypeError:"), type_error

  circular: dict[str, Any] = {}
  circular["self"] = circular
  value_error = plugin.write_report_file(target, circular)
  assert value_error is not None and value_error.startswith("ValueError:"), value_error

  assert not target.exists()


@allure.feature("Day58 xdist 并发接入")
@allure.story("P4-3 状态必被清空")
@allure.title("收尾环节抛异常时，finally 仍会清空会话状态")
def test_sessionfinish_clears_state_even_when_report_write_raises(
  plugin: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  """
  P4-3 的靶子: `_STATE.clear()` 原先是收尾流程的最后一条裸语句，任何未覆盖的
  异常都会让它跳过——模块级单例被同一进程的后续会话复用（pytester、真实执行器
  复用常驻进程），残留状态会让下一场会话读到上一场的旧统计。

  这里用"报告写入抛错"构造未覆盖路径: 异常如实上抛（展示层故障不掩盖），但
  状态必须已被 finally 清空。
  """
  report_path = tmp_path / "exploding_report.json"
  values = {
    plugin.INI_REPORT_PATH: str(report_path),
    plugin.INI_REPORT_ENABLED: True,
    plugin.INI_SUMMARY_ENABLED: False,
  }
  config = SimpleNamespace(
    rootpath=tmp_path,
    getini=lambda name: values[name],
    pluginmanager=SimpleNamespace(
      get_plugins=lambda: set(),
      get_plugin=lambda name: None,
    ),
    stash=pytest.Stash(),
  )
  plugin._STATE.start_session(config)

  def _explode_writer(path: Path, payload: dict[str, Any]) -> str | None:
    """替身写入器: 直接抛错，模拟降级分支覆盖不到的意外路径"""
    raise RuntimeError(f"报告写入未预期失败: {path} / {len(payload)} 字段")

  monkeypatch.setattr(plugin, "write_report_file", _explode_writer)

  with pytest.raises(RuntimeError):
    plugin.pytest_sessionfinish(SimpleNamespace(config=config), 0)

  assert plugin._STATE.stats is None
  assert plugin._STATE.config is None
  assert not report_path.exists()


@allure.feature("Day58 xdist 并发接入")
@allure.story("P4-6 源码转义")
@allure.title("插件源码编译无非法转义告警，docstring 里的正则文本原样保留")
def test_source_compiles_without_invalid_escape_warnings(plugin: ModuleType) -> None:
  """
  P4-6 的靶子: 模块 docstring 里的 `\\bin\\s+` 在非 raw 字符串中属于无效转义，
  Python 3.11 起首编译给 DeprecationWarning（3.12+ 升级为 SyntaxWarning），
  干净环境里会直接出现在输出上、污染"0 warning"门禁。

  验两层: ①整份源码重新编译不产生这两类告警；②文档里的正则文本仍然是原样
  （改成 raw string 是为了让转义合法，不是把这段说明删掉）。
  """
  source_path = Path(cast(str, plugin.__file__))
  source = source_path.read_text(encoding="utf-8")

  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    compile(source, str(source_path), "exec")

  escape_warnings = [
    str(item.message)
    for item in caught
    if issubclass(item.category, (DeprecationWarning, SyntaxWarning))
  ]
  assert escape_warnings == [], escape_warnings
  assert r"\bin\s+" in (plugin.__doc__ or "")


@allure.feature("Day59 Day58复核归位")
@allure.story("P3-④ 嵌套 pytest env 注入")
@allure.title("_run_nested_pytest 的 env 参数按叠加语义生效，不替换 PYTHONPATH")
def test_run_nested_pytest_merges_optional_env(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """
  P3-④ 的目标: e2e 用例需要显式注入 `PYTEST_XDIST_WORKER` 等变量时，不必
  再复制整份环境构造（复制会丢掉 `PYTHONPATH`，子进程随即加载不到插件）。
  `env` 参数只做叠加，同名键覆盖默认值。

  用替身 `subprocess.run` 取证而不是真起进程: 本用例要钉的是"传下去的环境
  变量是什么"，真跑子进程只能间接观察，且要多付一次冷启动。替身同时断言
  `env=None` 时传下去的就是 `_nested_env()` 本身，防止默认路径被悄悄改掉。
  """
  captured: dict[str, Any] = {}

  def _fake_run(
    args: list[str], **kwargs: Any
  ) -> subprocess.CompletedProcess[bytes]:
    """替身子进程: 只记录实参，不真起进程"""
    captured["args"] = list(args)
    captured.update(kwargs)
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")

  monkeypatch.setattr(subprocess, "run", _fake_run)
  baseline_env = _nested_env()

  _run_nested_pytest(["-q"], ["probe_demo.py"], Path("probe_basetemp"))
  assert captured["env"] == baseline_env

  payload = {"PYTEST_XDIST_WORKER": "gw9", "TM_DAY59_PROBE": "1"}
  _run_nested_pytest(
    ["-q"], ["probe_demo.py"], Path("probe_basetemp"), env=payload
  )
  merged = captured["env"]
  assert merged["PYTEST_XDIST_WORKER"] == "gw9"
  assert merged["TM_DAY59_PROBE"] == "1"
  # 基础环境不丢: 编码设置与 PYTHONPATH 与默认值逐字相同
  assert merged["PYTHONPATH"] == baseline_env["PYTHONPATH"]
  assert merged["PYTHONIOENCODING"] == baseline_env["PYTHONIOENCODING"]
  # 传的是副本: 调用方的 dict 不被就地修改，可安全复用
  assert payload == {"PYTEST_XDIST_WORKER": "gw9", "TM_DAY59_PROBE": "1"}


@allure.feature("Day59 Day58复核归位")
@allure.story("P3-⑤ testnodedown 崩溃路径")
@allure.title("worker 崩溃（error 非 None 且无 workeroutput）时合并静默跳过、保留已有统计")
def test_testnodedown_survives_crashed_worker(
  plugin: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
  """
  P3-⑤ 的靶子: 崩溃 worker 不会走正常的 `workerfinished` 上报，master 收到的
  `error` 非 None，`workeroutput` 可能为 None / 缺键 / 形态非法。三条路径都
  必须 ①不抛异常（否则 master 收尾被打断、整场报告的写入也随之中断）；
  ②保留先到 worker 的统计，绝不因为一个 worker 崩了就把已有数据清空。

  与既有用例的分工: 既有 `test_worker_collection_stats_are_reported_and_merged`
  覆盖的是"error 恒为 None 的非法上报形态"，本用例专门把 `error` 设为异常对象
  ——崩溃路径的真实入参形状，防止后续重构把 `error` 当成"必须为 None"。
  """
  healthy = {
    "total": 4,
    "marker_counts": {"smoke": 1},
    "module_counts": {"tests.test_probe": 4},
    "unregistered_marker_counts": {},
  }
  monkeypatch.setattr(plugin._STATE, "worker_collection_stats", None)
  plugin.pytest_testnodedown(
    SimpleNamespace(workeroutput={plugin.WORKER_COLLECTION_STATS_KEY: healthy}), None
  )
  assert plugin._STATE.worker_collection_stats == healthy

  crash = RuntimeError("worker gw1 exited unexpectedly during collection")
  for crashed_node in (
    SimpleNamespace(workeroutput=None),
    SimpleNamespace(workeroutput={}),
    SimpleNamespace(workeroutput="broken"),
    SimpleNamespace(),
  ):
    plugin.pytest_testnodedown(crashed_node, crash)
    assert plugin._STATE.worker_collection_stats == healthy

  # 崩溃之后健康 worker 仍可继续合并（状态未被崩溃分支改成不可写）
  fuller = dict(healthy, total=5)
  plugin.pytest_testnodedown(
    SimpleNamespace(workeroutput={plugin.WORKER_COLLECTION_STATS_KEY: fuller}), crash
  )
  assert plugin._STATE.worker_collection_stats == fuller
  monkeypatch.setattr(plugin._STATE, "worker_collection_stats", None)

