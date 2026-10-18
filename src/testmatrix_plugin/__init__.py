r"""
TestMatrix 平台专用 pytest 插件（Day56）

一、插件两条能力（与项目根 `conftest.py` 零职责重叠）:

  1. **结果上报**（`pytest_sessionfinish`）: 把"本次会话跑了什么、结果如何"
    汇总成一份结构化 JSON 报告落盘，同时在终端输出一行摘要。报告里的
    收集侧数据**不自己重算**，而是消费项目根 `conftest.py` 在
    `pytest_collection_modifyitems` 写进 `config.stash` 的收集统计
    （总数 / marker 分布 / 模块分布 / 未注册标记），避免同一口径出现
    两份实现（该口径由 Day55 固定，见 PROJECT_CONTEXT 6.62）。
  2. **日志钩子**（`pytest_runtest_logreport`）: 逐条记录每个用例在
    setup / call / teardown 三个阶段的 outcome 与耗时，累加成会话级
    统计，供报告的执行侧使用。

二、为什么走 entry point 而不是往 conftest 里再加钩子:

  conftest 的加载范围是"被收集路径的全部祖先目录"，只有项目内运行时才
  生效；插件经 `[project.entry-points."pytest11"]` 注册并安装后，对整个
  解释器生效（**包括真实执行器拉起的 pytest 子进程**），无需任何显式
  import，也不需要每条入口都记得挂载。

三、与 conftest 收集统计的对接方式（关键，不能想当然）:

  `COLLECTION_STATS_STASH_KEY` 是 `pytest.StashKey` 实例，**以对象身份为键**。
  本插件若 `import conftest` 去拿那个常量，会得到**另一份模块对象**、
  另一把键，`config.stash.get(键)` 永远读到 None（Day55 已踩过该坑，
  见 6.62 决策二）。故这里遍历 `config.pluginmanager.get_plugins()`，
  用**真实加载的那份 conftest 模块**自带的键去读；读不到时报告中该字段
  为 `null`，绝不阻断会话。

四、旁路铁律:

  报告写不出、收集统计读不到、终端不可用（没有 writer，或 writer 写失败）
 ——任何一步意外都只降级为一条终端提示或静默跳过，**绝不改变 pytest 的
  退出码与任何用例结果**（与通知旁路、Allure 目录修复同一口径）。
  "writer 写失败"这一条是 Day57 打包验证时由补测实测补上的：原文只写了
  "没有 writer"，而终端关闭/管道断开时 writer 是存在却会抛错的。

五、终端摘要行的格式约束:

  摘要刻意不含 nodeid（不含 `::`），否则会被"按 `::` 过滤 nodeid"的收集
  输出解析误当成用例行；耗时写成 `X.XXs` 而不写 `in X.XXs`，避免与
  `src/core/output_parser.py` 的耗时正则（`\bin\s+...`）撞车。

六、xdist 多进程并发（Day58）:

  xdist 下 master 与每个 worker 都会走同一套钩子，但**落盘与终端输出权只归
  master**: worker 的 `pytest_sessionfinish` 一律直接返回（不写报告、不出摘要、
  不清状态），故报告仍是单一固定路径、由 master 唯一写入，多进程共写冲突在
  结构上不存在。进程角色判定见 `_is_xdist_worker`。

  执行侧统计天然是全量口径: worker 把每条阶段报告回传 master，master 的
  `pytest_runtest_logreport` 累加到的就是整场会话的结果。

  收集侧统计的来路要单独说明（实测结论）: xdist 的 master **不做收集**
  （其 `pytest_collection` 直接 return True，禁止控制进程收集用例），所以
  master 既不会执行项目根 conftest 的 `pytest_collection_modifyitems`，
  stash 里也就没有收集统计——报告里该字段会是 null。真正持有完整统计的是
  worker（它们各自收集整场会话的全部用例、只执行其中一部分），故由 worker
  在收集结束时把统计写进 xdist 的 `config.workeroutput` 通道，master 在
  `pytest_testnodedown` 收下并按"total 更大者优先"合并（各 worker 口径相同，
  相加会把总数放大 N 倍）。
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import pytest

# ===========================================================================
# 常量（对外可配置项的默认值集中在此，插件行为只有这一处事实来源）
# ===========================================================================

# 插件名（与 pyproject.toml 的 pytest11 entry point 名一致，`-p no:testmatrix` 用它禁用）
PLUGIN_NAME = "testmatrix"

# 插件版本（与 pyproject.toml 的 project.version 保持同一口径）
PLUGIN_VERSION = "0.1.0"

# 报告结构的 schema 版本（结构变更时自增，供下游判定兼容性）
REPORT_SCHEMA_VERSION = 1

# 终端输出前缀（与项目根 conftest 的收集摘要保持同一锚点，便于终端里定位本平台输出）
SUMMARY_PREFIX = "[TestMatrix]"

# 默认报告路径（相对路径以 pytest 的 rootpath 为基准解析）
DEFAULT_REPORT_PATH = "output/testmatrix_report.json"

# ini 选项名（可用 pytest.ini / pyproject 的 [tool.pytest.ini_options] 配置，或用 -o 覆盖）
INI_REPORT_PATH = "testmatrix_report_path"
INI_REPORT_ENABLED = "testmatrix_report_enabled"
INI_SUMMARY_ENABLED = "testmatrix_summary_enabled"

# 用例执行的三个阶段（pytest 报告里 report.when 的取值域）
EXECUTION_PHASES: tuple[str, ...] = ("setup", "call", "teardown")

# worker 向 master 上报收集统计时使用的键（走 xdist 的 config.workeroutput 通道）；
# 前缀带插件名，避免与其它使用 workeroutput 的插件撞键
WORKER_COLLECTION_STATS_KEY = "testmatrix_collection_stats"

# 结果上报的两类终态: call 阶段失败是 failed，setup/teardown 阶段失败是 error
FAILURE_OUTCOMES: frozenset[str] = frozenset({"failed"})


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass(frozen=True)
class PluginConfig:
  """
  本插件的运行时配置快照（ini 选项读出后固化，避免钩子里重复读 ini）

  属性:
    report_path (str): JSON 报告输出路径（相对 rootpath，可为绝对路径）
    report_enabled (bool): 是否输出 JSON 报告
    summary_enabled (bool): 是否输出终端摘要行
  """

  report_path: str
  report_enabled: bool
  summary_enabled: bool


@dataclass
class SessionExecutionStats:
  """
  会话级执行统计（可变容器，被 `pytest_runtest_logreport` 逐条累加）

  计数口径（与 pytest 自身的终端汇总对齐，**按阶段报告条数**累加，不按用例去重）:
    - call 阶段 passed / failed / skipped 直接计入对应计数
    - setup / teardown 阶段 failed 计入 `errors`: 每收到一条失败的阶段报告计 1 条
    - setup 阶段 skipped 计入 `skipped`（fixture 级跳过不会进 call 阶段）

  因此各计数之间并非严格互斥、`total` 也可能**大于实际收集用例数**:
    - 同一用例 setup 与 teardown 都失败时 `errors` 为 2（阶段报告两条）；
    - 用例 call 阶段通过、teardown 阶段失败时，`passed` 与 `errors` 各计 1。
  这是 pytest 自身口径（它的终端汇总同样把 setup/teardown 的 error 与用例
  结果分列），报告侧对齐它是为了"报告数字可与 pytest 输出逐项对账"。

  属性:
    started_monotonic (float): 会话开始的单调时钟读数（time.perf_counter）
    report_count (int): 收到的阶段报告总条数（含 setup / call / teardown）
    passed (int): call 阶段通过的用例数
    failed (int): call 阶段失败的用例数
    skipped (int): 被跳过的用例数（call 阶段跳过 + fixture 级 setup 跳过）
    errors (int): setup / teardown 阶段失败的**阶段失败条数**（含 fixture 报错；
      同一用例两个阶段都失败时计 2 条，故不等于"失败的用例数"）
    phase_counts (dict[str, dict[str, int]]): 阶段 -> outcome -> 条数
    failed_cases (list[dict[str, Any]]): 失败/错误明细（nodeid + 阶段 + 耗时；
      同一用例多个阶段失败会出现多条，与 `errors` 口径一致）
  """

  started_monotonic: float = 0.0
  report_count: int = 0
  passed: int = 0
  failed: int = 0
  skipped: int = 0
  errors: int = 0
  phase_counts: dict[str, dict[str, int]] = field(default_factory=dict)
  failed_cases: list[dict[str, Any]] = field(default_factory=list)

  @property
  def total(self) -> int:
    """
    四类计数之和（passed + failed + errors + skipped）

    注意这是**结果条数**口径而非"用例数"口径: `errors` 按阶段失败条数计
    （见类 docstring），且各类之间不保证互斥（用例 call 通过 + teardown
    失败会同时计入 `passed` 与 `errors`），故本值可能大于实际收集用例数。

    参数:
      无

    返回:
      int: passed + failed + errors + skipped

    异常:
      无
    """
    return self.passed + self.failed + self.errors + self.skipped


class CollectionStatsView(Protocol):
  """
  项目根 conftest 收集统计快照的**读取面**（结构化子类型）

  刻意不 import conftest 取类型: 那会引入第二份模块对象（键对象不同），
  也会让插件对 conftest 产生导入依赖。本协议只声明插件真正读取的四个字段。
  """

  total: int
  marker_counts: dict[str, int]
  module_counts: dict[str, int]
  unregistered_marker_counts: dict[str, int]


@dataclass
class PluginState:
  """
  插件的进程内会话状态（单进程单会话；configure 重建、sessionfinish/退出清空）

  属性:
    config (pytest.Config | None): 当前会话配置（`pytest_runtest_logreport`
      只拿到 report 没有 config，故在配置阶段记下引用）
    stats (SessionExecutionStats | None): 当前会话的执行统计容器
    worker_collection_stats (dict[str, Any] | None): 仅在 master 进程使用——
      xdist worker 上报来的收集统计（master 自身不做收集，故没有别的来源）
  """

  config: pytest.Config | None = None
  stats: SessionExecutionStats | None = None
  worker_collection_stats: dict[str, Any] | None = None

  def start_session(self, config: pytest.Config) -> None:
    """
    为一次新会话重建状态（幂等: 进程内第二次会话覆盖第一次，不残留旧数据）

    参数:
      config (pytest.Config): 当前会话配置

    返回:
      无

    异常:
      无
    """
    self.config = config
    self.stats = SessionExecutionStats()
    self.worker_collection_stats = None

  def clear(self) -> None:
    """
    清空会话状态（会话收尾与进程退出时调用，防止跨会话读到旧统计）

    参数:
      无

    返回:
      无

    异常:
      无
    """
    self.config = None
    self.stats = None
    self.worker_collection_stats = None


# 模块级单例（pytest 每个进程只加载一份插件，模块级不可变单例与此语义一致）
_STATE = PluginState()


# ===========================================================================
# xdist 进程角色判定（Day58）
# ===========================================================================
def _is_xdist_worker(config: pytest.Config) -> bool:
  """
  判定当前进程是否为 xdist 的 worker 进程（非 master）

  判据（按可靠性排序）:
    1. `config` 上有 **worker 专有通道属性**——`workerinput`（xdist 官方
      推荐判据）或 `workeroutput`（worker 结束回传结果用的字典）。这两个
      属性只由 xdist 的 worker 端在 `remote_initconfig` 里注入，controller
      与普通串行会话都没有；
    2. 拿不到 pluginmanager 的替身配置（测试用 `SimpleNamespace` 等）:
      退回 `PYTEST_XDIST_WORKER` 环境变量；
    3. 其余情况（有 pluginmanager、但上面两个属性都没有）一律判为 master。

  第 3 条为什么要**盖过**环境变量（实测踩到的坑）: `PYTEST_XDIST_WORKER` 是
  进程环境变量，会被子进程继承。真实执行器拉起的 pytest 子进程、本文件测试里
  的嵌套 pytest，只要父进程是 xdist worker 就天然带着这个变量——单凭它判定会
  把"普通 pytest 进程"误判成 worker，结果是**报告静默不落盘、摘要不输出**。
  而真正的 worker 必然带第 1 条的通道属性，所以环境变量只作为替身配置下的
  兜底，绝不在有完整 config 时单独使用。

  为什么必须要这条判定: 插件的报告路径是**固定单一路径**，worker 若照常落盘
  就是多进程共写同一个文件（最终内容取决于写盘先后）；终端摘要也会被每个
  进程各打一行。落盘与输出权只归 master，"谁是 master"必须先判得出来。

  参数:
    config (pytest.Config): 当前会话配置

  返回:
    bool: worker 进程返回 True；master 或普通串行会话返回 False

  异常:
    无（判定失败一律按 master 处理，串行行为不受影响）
  """
  try:
    if hasattr(config, "workerinput") or hasattr(config, "workeroutput"):
      return True
    if getattr(config, "pluginmanager", None) is None:
      # 替身/极端场景: 没有任何可自证的通道，只能看环境变量
      return bool(os.environ.get("PYTEST_XDIST_WORKER", ""))
  except Exception:  # noqa: BLE001 替身对象上取属性抛错时按 master 处理
    return False
  return False


# ===========================================================================
# 配置读取与路径解析
# ===========================================================================
def read_plugin_config(config: pytest.Config) -> PluginConfig:
  """
  读出插件配置项（ini 未注册或读取异常时整体回落默认值，绝不抛错）

  参数:
    config (pytest.Config): 当前会话配置

  返回:
    PluginConfig: 配置快照；report_path 为空串时回落默认路径

  异常:
    无（配置读取失败只降级为默认行为，报告能力是旁路能力）
  """
  try:
    report_path = str(config.getini(INI_REPORT_PATH))
    report_enabled = bool(config.getini(INI_REPORT_ENABLED))
    summary_enabled = bool(config.getini(INI_SUMMARY_ENABLED))
  except Exception:  # noqa: BLE001 ini 不可读时按默认行为继续
    return PluginConfig(
      report_path=DEFAULT_REPORT_PATH,
      report_enabled=True,
      summary_enabled=True,
    )
  return PluginConfig(
    report_path=report_path or DEFAULT_REPORT_PATH,
    report_enabled=report_enabled,
    summary_enabled=summary_enabled,
  )


def resolve_report_path(config: pytest.Config, plugin_config: PluginConfig) -> Path:
  """
  把配置中的报告路径解析为绝对路径（相对路径以 pytest 的 rootpath 为基准）

  为什么以 rootpath 而非进程 cwd 为基准: 真实执行器拉起的子进程 cwd 虽然
  也是项目根，但队列 worker / 外部调用方未必——用 rootpath 让"报告落在
  项目内"这件事与调用方从哪个目录启动无关。

  参数:
    config (pytest.Config): 当前会话配置（取 rootpath）
    plugin_config (PluginConfig): 插件配置快照

  返回:
    Path: 报告文件绝对路径

  异常:
    无
  """
  report_path = Path(plugin_config.report_path)
  if report_path.is_absolute():
    return report_path
  return Path(str(config.rootpath)) / report_path


def collect_collection_stats(config: pytest.Config) -> dict[str, Any] | None:
  """
  从当前会话的 stash 读出项目根 conftest 写下的收集统计

  实现要点（Day55 踩坑的直接应用）: `COLLECTION_STATS_STASH_KEY` 以对象身份
  为键，必须用**真实加载的那份 conftest 模块**自带的键去读；自己 import
  一份会得到另一把键、永远读到 None。故遍历已注册插件，取"带该属性且
  stash 里确有值"的那一份。

  参数:
    config (pytest.Config): 当前会话配置

  返回:
    dict[str, Any] | None: 四个字段的普通字典（total / marker_counts /
      module_counts / unregistered_marker_counts）；读不到时返回 None

  异常:
    无（收集统计是旁路能力，任何意外都不影响报告生成与用例结果）
  """
  try:
    for plugin in config.pluginmanager.get_plugins():
      stash_key = getattr(plugin, "COLLECTION_STATS_STASH_KEY", None)
      if stash_key is None:
        continue
      value = config.stash.get(stash_key, None)
      if value is None or not hasattr(value, "total"):
        continue
      view = cast(CollectionStatsView, value)
      return {
        "total": int(view.total),
        "marker_counts": dict(view.marker_counts),
        "module_counts": dict(view.module_counts),
        "unregistered_marker_counts": dict(view.unregistered_marker_counts),
      }
  except Exception:  # noqa: BLE001 同上
    return None
  return None


def merge_worker_collection_stats(
  current: dict[str, Any] | None, incoming: dict[str, Any] | None
) -> dict[str, Any] | None:
  """
  合并 master 侧已收到的收集统计与某个 worker 新上报的那一份（纯函数）

  合并口径: 取 `total` 更大的一份，相等时保留先到的那一份。
    为什么不是把各 worker 的统计相加: xdist 的 worker 在收集阶段收集的是
    **整场会话的完整用例集**（各自收到全部 nodeid、只执行其中一部分），
    把 N 份同口径统计相加会把总数放大 N 倍。而 `total` 更大者优先，是为了
    在"某 worker 收集不完整"（配置差异、部分 worker 提前退出）时仍取到
    最接近完整会话的那一份，同时保持结果确定。

  参数:
    current (dict[str, Any] | None): master 已记录的统计（可能为空）
    incoming (dict[str, Any] | None): 本次新上报的统计（可能为空）

  返回:
    dict[str, Any] | None: 合并结果；两者皆空时返回 None

  异常:
    无（total 字段缺失或类型异常时按 0 比较，绝不抛错）
  """
  if incoming is None:
    return current
  if current is None:
    return incoming
  try:
    current_total = int(current.get("total", 0))
    incoming_total = int(incoming.get("total", 0))
  except (TypeError, ValueError):
    return current
  return incoming if incoming_total > current_total else current


# ===========================================================================
# 执行统计累加（纯逻辑，可被单测直接驱动）
# ===========================================================================
def record_report(stats: SessionExecutionStats, report: pytest.TestReport) -> None:
  """
  把一条 `pytest_runtest_logreport` 记录累加进会话统计（就地更新 stats）

  计数口径见 `SessionExecutionStats` 的 docstring；失败/错误用例会被追加
  进 failed_cases（只留 nodeid / 阶段 / 耗时，不留堆栈——堆栈由 Allure 与
  日志承担，报告只做"哪些用例没通过"的索引）。

  参数:
    stats (SessionExecutionStats): 会话统计容器（就地更新）
    report (pytest.TestReport): 单条阶段报告

  返回:
    无

  异常:
    无（字段缺失一律按默认值处理；统计是旁路能力）
  """
  phase = str(getattr(report, "when", "") or "")
  outcome = str(getattr(report, "outcome", "") or "")
  if phase not in EXECUTION_PHASES or not outcome:
    # 非标准节点（如自定义 Item 上报的非常规阶段）只跳过，不打断统计
    return

  bucket = stats.phase_counts.setdefault(phase, {})
  bucket[outcome] = bucket.get(outcome, 0) + 1
  stats.report_count += 1

  if phase == "call":
    if outcome == "passed":
      stats.passed += 1
      return
    if outcome == "skipped":
      stats.skipped += 1
      return
    if outcome not in FAILURE_OUTCOMES:
      return
    stats.failed += 1
  elif outcome in FAILURE_OUTCOMES:
    # setup / teardown 阶段失败意味着用例没能正常跑完，计入 errors
    stats.errors += 1
  elif phase == "setup" and outcome == "skipped":
    # fixture 级跳过不会产生 call 报告，必须在这里计入，否则跳过数会漏；
    # **必须就地 return**——否则这条"成功跳过"的用例会掉进下面的失败明细
    # 追加逻辑，被当成失败用例上报（本插件开发期实测踩到）
    stats.skipped += 1
    return
  else:
    return

  stats.failed_cases.append(
    {
      "nodeid": str(getattr(report, "nodeid", "") or ""),
      "phase": phase,
      "duration": round(float(getattr(report, "duration", 0.0) or 0.0), 6),
    }
  )


def elapsed_seconds(stats: SessionExecutionStats) -> float:
  """
  计算会话墙钟耗时（未记录起点时返回 0.0，绝不返回负数）

  参数:
    stats (SessionExecutionStats): 会话统计容器

  返回:
    float: 非负耗时（秒）

  异常:
    无
  """
  if stats.started_monotonic <= 0.0:
    return 0.0
  return max(0.0, time.perf_counter() - stats.started_monotonic)


# ===========================================================================
# 报告组装与落盘
# ===========================================================================
def build_report_payload(
  stats: SessionExecutionStats,
  collection_stats: dict[str, Any] | None,
  exitstatus: int,
  duration_seconds: float,
) -> dict[str, Any]:
  """
  组装报告字典（纯函数；落盘内容与终端摘要共用这一份数据，口径不可能漂移）

  结构:
    schema_version / plugin / timestamp / exit_status
    collection_stats: 收集侧统计（来自 conftest stash，读不到为 null）
    execution_stats: 执行侧统计（total/passed/failed/errors/skipped/
      duration/report_count/failed_cases/phase_counts）

  `phase_counts` 固定输出三个阶段键（未出现的阶段为空字典），保证下游按键
  取值时不必先判存在性。

  参数:
    stats (SessionExecutionStats): 会话执行统计
    collection_stats (dict[str, Any] | None): 收集侧统计
    exitstatus (int): pytest 退出码（如实记录，不参与任何判定）
    duration_seconds (float): 会话墙钟耗时（秒）

  返回:
    dict[str, Any]: 可直接 json.dumps 的报告字典

  异常:
    无
  """
  return {
    "schema_version": REPORT_SCHEMA_VERSION,
    "plugin": {"name": PLUGIN_NAME, "version": PLUGIN_VERSION},
    "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
    "exit_status": int(exitstatus),
    "collection_stats": collection_stats,
    "execution_stats": {
      "total": stats.total,
      "passed": stats.passed,
      "failed": stats.failed,
      "errors": stats.errors,
      "skipped": stats.skipped,
      "duration": round(float(duration_seconds), 6),
      "report_count": stats.report_count,
      "failed_cases": list(stats.failed_cases),
      "phase_counts": {
        phase: dict(stats.phase_counts.get(phase, {}))
        for phase in EXECUTION_PHASES
      },
    },
  }


def format_plugin_summary(payload: dict[str, Any]) -> str:
  """
  生成终端摘要行（固定格式，便于人与机器同时消费）

  格式: `[TestMatrix] 会话完成：N 用例，passed=a failed=b skipped=c error=d，耗时 X.XXs`

  参数:
    payload (dict[str, Any]): `build_report_payload` 产出的报告字典

  返回:
    str: 单行摘要（不含换行符）

  异常:
    无（字段缺失或类型异常一律按 0 处理）
  """
  raw_execution = payload.get("execution_stats")
  execution: dict[str, Any] = raw_execution if isinstance(raw_execution, dict) else {}
  try:
    duration = float(execution.get("duration", 0.0))
  except (TypeError, ValueError):
    duration = 0.0
  return (
    f"{SUMMARY_PREFIX} 会话完成：{execution.get('total', 0)} 用例，"
    f"passed={execution.get('passed', 0)} "
    f"failed={execution.get('failed', 0)} "
    f"skipped={execution.get('skipped', 0)} "
    f"error={execution.get('errors', 0)}，"
    f"耗时 {duration:.2f}s"
  )


def write_report_file(path: Path, payload: dict[str, Any]) -> str | None:
  """
  把报告写入磁盘（失败只回传错误描述，绝不抛异常）

  捕获的异常类型不是只有 `OSError`（Day58 P4-2）:
    - `OSError`: 目录建不出来 / 文件写不进去（目标被目录占用、父路径被
     同名文件占用、磁盘写失败）；
    - `TypeError`: `json.dumps` 遇到不可序列化对象（payload 里混进了自定义
     对象或函数时抛）；
    - `ValueError`: `json.dumps` 的数值越界 / 循环引用等序列化失败。
  只捕 `OSError` 的话，后两类会穿透 `pytest_sessionfinish`，把"报告写不出"
  升级成"会话故障"（退出码被改写），与旁路铁律相反。

  参数:
    path (Path): 报告文件目标路径（父目录不存在时自动创建）
    payload (dict[str, Any]): 报告字典

  返回:
    str | None: 成功返回 None；失败返回 `<异常类型>: <消息>`

  异常:
    无
  """
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
      json.dumps(payload, ensure_ascii=False, indent=2),
      encoding="utf-8",
    )
  except (OSError, TypeError, ValueError) as exc:
    return f"{type(exc).__name__}: {exc}"
  return None


def emit_message(config: pytest.Config, message: str) -> None:
  """
  向终端写一行（terminalreporter 缺失或写失败时静默跳过）

  走 terminal writer 而不是 print: print 归 pytest 的输出捕获机制管，
  是否出现在终端取决于捕获层级；terminal writer 与 pytest 自身汇总同通道
  （与项目根 conftest 的收集摘要同一口径，项目铁律: 不用 print）。

  三种失败成因都必须吞掉（旁路铁律的完整表述是"终端不可用时不出声、也不出错"）:
    - **没有 writer**: `-p no:terminal` 时 `get_plugin` 返回 None；
    - **取 writer 时本身抛错**: 插件管理器状态异常（插件卸载顺序问题等）会让
     `get_plugin` 直接抛异常。此前这一步裸奔在 try 之外，同样会把展示层
     故障升级为会话故障（Day58 P4-1）；
    - **writer 存在但写失败**: 终端已关闭 / 输出管道断开（真实执行器把子进程
     stdout 接到管道上，读端先退出即属此例）。该异常若不吞，会从
     `pytest_sessionfinish` 穿透出去，把"展示层故障"升级成"会话故障"
     （pytest 记 INTERNALERROR，退出码被改写），与"绝不改变退出码与
     任何用例结果"的承诺相反——Day57 打包验证时由补测实测逮到并修复。

  参数:
    config (pytest.Config): 当前会话配置
    message (str): 待输出的一行文本

  返回:
    无

  异常:
    无
  """
  try:
    terminal = config.pluginmanager.get_plugin("terminalreporter")
  except Exception:  # noqa: BLE001 取 writer 失败同样只降级（P4-1）
    return
  if terminal is None:
    return
  try:
    terminal.write_line(message)
  except Exception:  # noqa: BLE001 终端输出是旁路能力，写不出去不得影响会话收尾
    return


# ===========================================================================
# pytest 钩子（薄壳：只负责取状态与兜底，逻辑全在可单测的纯函数里）
# ===========================================================================
def pytest_addoption(parser: pytest.Parser) -> None:
  """
  注册插件配置项（pytest.ini / pyproject 的 [tool.pytest.ini_options] / -o 均可配置）

  参数:
    parser (pytest.Parser): pytest 命令行解析器

  返回:
    无

  异常:
    无
  """
  # ini 选项挂在 Parser 上（OptionGroup 只有 addoption，没有 addini），
  # 三条都走 addini 注册而非 addoption: 它们本就属于"配置项"而非命令行开关，
  # 注册后既能在 pytest.ini / pyproject 里写，也能用 -o 临时覆盖。
  parser.addini(
    INI_REPORT_PATH,
    "TestMatrix 插件: JSON 结果报告的输出路径（相对 pytest rootpath）",
    type="string",
    default=DEFAULT_REPORT_PATH,
  )
  parser.addini(
    INI_REPORT_ENABLED,
    "TestMatrix 插件: 是否输出 JSON 结果报告（true/false）",
    type="bool",
    default=True,
  )
  parser.addini(
    INI_SUMMARY_ENABLED,
    "TestMatrix 插件: 是否在终端输出一行会话摘要（true/false）",
    type="bool",
    default=True,
  )


def pytest_configure(config: pytest.Config) -> None:
  """
  配置阶段: 建立本次会话的统计容器与配置引用

  xdist 下 worker 也会走到这里（每个 worker 是独立进程、独立模块对象、
  独立 `_STATE`），这一步**刻意不加 worker 分支**: 建状态只是内存里的对象
  初始化，不产生任何跨进程可见的副作用；worker 的统计虽然是"以自己执行的
  用例为范围"，但它根本不会被落盘（sessionfinish 在 worker 直接返回），
  因此没有"部分口径被写进报告"的风险，保持单一代码路径更容易维护。

  参数:
    config (pytest.Config): 当前会话配置

  返回:
    无

  异常:
    无
  """
  _STATE.start_session(config)


def pytest_sessionstart(session: pytest.Session) -> None:
  """
  会话启动: 记录单调时钟起点（供 sessionfinish 计算会话墙钟耗时）

  参数:
    session (pytest.Session): 测试会话对象

  返回:
    无

  异常:
    无（统计容器缺失时直接跳过）
  """
  stats = _STATE.stats
  if stats is None:
    return
  stats.started_monotonic = time.perf_counter()


def pytest_collection_finish(session: pytest.Session) -> None:
  """
  收集完成: **worker 侧**把本进程读到的收集统计上报给 master

  为什么必须由 worker 上报（Day58 实测结论，与最初的猜测相反）:
    xdist 的 master **不做收集**——它在自己的 `pytest_collection` 里直接
    return True（"prohibit collection of test items in controller process"），
    所以 master 既不会跑项目根 conftest 的 `pytest_collection_modifyitems`，
    也不会写它的 stash，报告里 `collection_stats` 只能是 null。而 worker 收集
    的是**整场会话的完整用例集**（只执行其中一部分），其 stash 里的统计才是
    完整口径。故这里把 worker 的统计塞进 xdist 的 `config.workeroutput`
    通道，由 master 在 `pytest_testnodedown` 收下来。

  与"worker 零副作用"的关系: 落盘 / 终端输出 / 状态清理仍然全部跳过；这里
  只是**通信**——写一个纯字典给 master，不碰磁盘、不碰终端、不改本进程状态。

  参数:
    session (pytest.Session): 测试会话对象

  返回:
    无

  异常:
    无（配置缺 workeroutput、statistics 读不到、写入失败一律静默跳过）
  """
  config = session.config
  if not _is_xdist_worker(config):
    # 串行会话由 master 自己的 stash 直接供给，无需上报
    return
  workeroutput = getattr(config, "workeroutput", None)
  if not isinstance(workeroutput, dict):
    # 非 xdist 的 worker 通道（理论上不会出现），静默跳过
    return
  stats = collect_collection_stats(config)
  if stats is None:
    return
  workeroutput[WORKER_COLLECTION_STATS_KEY] = stats


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
  """
  xdist master 侧: 收下某个 worker 上报的收集统计

  `optionalhook=True` 是必须的: 本钩子由 xdist 注册的 hookspec 定义，未安装
  xdist 时 pluggy 会把"未知钩子实现"当成插件校验错误，直接让插件加载失败。

  只在 master 收到 worker 的 `workerfinished` 事件时触发（早于 master 的
  `pytest_sessionfinish`），故 sessionfinish 里读到的已是最新合并结果。

  参数:
    node (Any): xdist 的 WorkerController（带 `workeroutput` 属性）
    error (Any): worker 异常退出的错误对象；正常退出为 None（不参与判定）

  返回:
    无

  异常:
    无（数据结构不符预期一律忽略，收集统计是旁路能力）
  """
  output = getattr(node, "workeroutput", None)
  if not isinstance(output, dict):
    return
  incoming = output.get(WORKER_COLLECTION_STATS_KEY)
  if not isinstance(incoming, dict):
    return
  _STATE.worker_collection_stats = merge_worker_collection_stats(
    _STATE.worker_collection_stats, cast(dict[str, Any], incoming)
  )


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
  """
  日志钩子: 逐个用例阶段（setup / call / teardown）记录结果与耗时

  pytest 的钩子规格只传入 report、不传 config，故配置引用在
  `pytest_configure` 阶段记进模块级单例（单进程单会话，语义等价）。

  参数:
    report (pytest.TestReport): 单条阶段报告

  返回:
    无

  异常:
    无（统计是旁路能力，任何意外都不得影响用例执行与退出码）
  """
  stats = _STATE.stats
  if stats is None:
    return
  try:
    record_report(stats, report)
  except Exception:  # noqa: BLE001 同上
    return


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
  """
  会话收尾: 组装并落盘 JSON 报告 + 输出一行终端摘要（仅 master 进程执行）

  xdist 分工（Day58）: worker 进程在这里**直接返回、零副作用**——不写报告、
  不输出摘要、不清状态。理由有两条: ①报告是固定单一路径，worker 也落盘就是
  多进程共写同一个文件，最终内容取决于写盘先后；②摘要会按进程数重复输出，
  终端上分不清哪一行是整场会话的口径。master 侧的执行统计天然是全量口径:
  worker 把每条阶段报告回传 master，master 的 `pytest_runtest_logreport`
  累加到的就是整场会话的结果。

  顺序刻意固定为"先落盘、后摘要": 摘要里回显的是同一份 payload，
  落盘失败也只多一条提示行，绝不改变 exitstatus（与 conftest 的收集摘要
  同一口径）。

  收集统计的来源分两种（Day58）: 串行会话读本进程 stash 即可；xdist 下 master
  **不做收集**（stash 里没有统计），故回落使用 worker 上报上来的那一份
  （见 `pytest_collection_finish` 与 `pytest_testnodedown`）。两者都没有时
  仍按原口径输出 `null`，绝不为此抛错。

  `_STATE.clear()` 放在 finally（Day58 P4-3）: 报告写入与摘要输出虽然都自带
  降级，但降级覆盖不到的路径上仍可能抛错，而模块级单例会被**同一进程**的
  后续会话复用（pytester、真实执行器复用常驻进程），残留状态会让下一场会话
  读到上一场的旧统计。finally 保证"无论收尾是否顺利，状态必被清空"。

  参数:
    session (pytest.Session): 测试会话对象
    exitstatus (int): pytest 退出码（只如实记录，绝不影响它）

  返回:
    无

  异常:
    无（常规失败路径均已内部降级；降级覆盖不到的意外异常会继续上抛，
      但会话状态仍由 finally 清空）
  """
  config = session.config
  if _is_xdist_worker(config):
    # worker 侧零副作用: 报告与摘要的唯一生产者是 master
    return
  stats = _STATE.stats
  if stats is None:
    return
  try:
    plugin_config = read_plugin_config(config)
    collection_stats = collect_collection_stats(config)
    if collection_stats is None:
      # xdist 场景: master 不做收集，靠 worker 上报的那一份兜底
      collection_stats = _STATE.worker_collection_stats
    payload = build_report_payload(
      stats,
      collection_stats,
      exitstatus,
      elapsed_seconds(stats),
    )
    if plugin_config.report_enabled:
      write_error = write_report_file(
        resolve_report_path(config, plugin_config), payload
      )
      # 写入失败不短路摘要: 摘要末尾仍是"降级提示 + 汇总行"两行，
      # 调用方据此判断"报告没落盘"而不是"插件没工作"
      if write_error is not None:
        emit_message(config, f"{SUMMARY_PREFIX} 报告写入失败：{write_error}")
    if plugin_config.summary_enabled:
      emit_message(config, format_plugin_summary(payload))
  finally:
    _STATE.clear()


def pytest_unconfigure(config: pytest.Config) -> None:
  """
  退出阶段: 清空会话状态（进程内多次会话之间不残留任何统计）

  两处刻意保持原样（Day58 结论，与 P4-3 同一轮复查）:
    1. **不加 worker 分支**: worker 的 `_STATE.clear()` 语义上是空转，但独立
      进程退出后状态本就自然消亡，加分支只是给"每个进程都要维护两套路径"
      增加维护面，没有实际收益；
    2. **不加 try/finally**: `PluginState.clear()` 只是两个属性赋 None，没有
      任何可能抛错的调用，再包一层属于形式主义的防御。

  参数:
    config (pytest.Config): 当前会话配置

  返回:
    无

  异常:
    无
  """
  _STATE.clear()
