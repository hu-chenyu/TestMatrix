"""
TestMatrix Day59: xdist 并发结果合并正确性验收用例

验证目标（对应 Day59 任务一）: `-n 2` 下多个 worker 的结果到达 master 时
**顺序不确定**，但合并后的口径必须与串行执行完全一致，且不因重复上报、
worker 崩溃、乱序到达而漂移。

三条被测链路:
  1. **执行结果归位**: worker 把每条阶段报告回传 master，master 的
     `pytest_runtest_logreport` 逐条累加。归位的含义是"同一条用例的
     setup/call/teardown 只计入它自己的计数"，与到达顺序无关。
  2. **收集统计合并**: master 不做收集（stash 为空），worker 经
     `config.workeroutput` 上报，master 在 `pytest_testnodedown` 收下。
     合并口径是"取 total 更大的一份"而非相加——N 个 worker 收集的是
     同一批用例，相加会把总数放大 N 倍。
  3. **写库去重**: 两个 worker 对同一 (execution_id, case_id) 各写一次时，
     `record_execution` 的第二道防线（写前查重）必须让第二次返回 False，
     库里只留一条明细。

边界声明（本文件刻意不测的东西，避免把"没有的能力"测成"有"）:
  - 插件层**没有**按 nodeid 的逐用例去重表: `record_report` 按阶段报告条数
    累加，是 pytest 自身口径（见 `SessionExecutionStats` 的 docstring）。
    真正的逐用例幂等由写库层的 (execution_id, case_id) 查重承担，多 worker
    幂等对账（含 claim 失效后的重投）属 Day60 范围。
  - 本文件不真起多进程: xdist 的进程调度不在本文件断言范围内（Day58 已有
    `-n 2` 端到端用例），这里用替身报告流精确控制"到达顺序"这一变量。

时间处理：本文件不涉及等待与真实并发，不出现任何固定 sleep；
"乱序"由 `random.Random(固定种子)` 生成，可复现且不依赖系统时钟。
"""

import importlib
import json
import random
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import allure
import pytest
from src.core.case_manager import CaseManager
from src.db.db_session import DatabaseSession

# 别名导入: 模型类原名以 Test 开头，直接导入会被 pytest 当成待收集的测试类
# 并抛 PytestCollectionWarning（0 warning 门禁不允许）
from src.db.models import TestExecution as ExecutionRow

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
  sys.path.insert(0, str(PROJECT_ROOT))

# 插件模块导入名（与 tests/test_pytest_plugin.py 同口径: src 布局真实路径）
PLUGIN_MODULE_NAME = "src.testmatrix_plugin"

# 乱序生成的固定种子: 失败时可原样复现同一条到达顺序，不靠"再跑一次碰运气"
SHUFFLE_SEED = 20261019

# 场景用用例编号（与 worker 归属绑定，便于断言"归位"而不是只看总数）
CASE_PASS = "TM-D59-0001"
CASE_FAIL = "TM-D59-0002"
CASE_SKIP = "TM-D59-0003"
CASE_SETUP_ERROR = "TM-D59-0004"


# ===========================================================================
# 夹具
# ===========================================================================
@pytest.fixture(scope="module")
def plugin() -> ModuleType:
  """
  加载被测插件模块（与 entry point 那份是两个模块对象，互不干扰）

  参数:
    无

  返回:
    ModuleType: 插件模块对象

  异常:
    ImportError: 模块不存在时抛出（用例应当直接红）
  """
  return importlib.import_module(PLUGIN_MODULE_NAME)


@pytest.fixture(autouse=True)
def reset_plugin_state(plugin: ModuleType) -> Iterator[None]:
  """
  用例前后清空插件进程内状态（模块级单例会被同进程后续用例复用）

  参数:
    plugin (ModuleType): 被测插件模块

  返回:
    Iterator[None]: yield 无数据，teardown 再清一次

  异常:
    无
  """
  plugin._STATE.clear()
  yield
  plugin._STATE.clear()


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
  """
  临时 SQLite 库（仅建表，不造用例种子: 写明细不依赖用例表存在）

  参数:
    tmp_path (Path): pytest 提供的用例级临时目录
    monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

  返回:
    Iterator[None]: yield 后复位数据库引擎

  异常:
    无
  """
  monkeypatch.setenv("TM_DB_TYPE", "sqlite")
  monkeypatch.setenv("TM_DB_SQLITE_PATH", str(tmp_path / "day59_merge.db"))
  DatabaseSession.reset()
  DatabaseSession.init_db()
  yield
  DatabaseSession.reset()


# ===========================================================================
# 替身构造
# ===========================================================================
def make_report(
  nodeid: str, when: str, outcome: str, duration: float = 0.01
) -> SimpleNamespace:
  """
  构造一条 pytest 阶段报告替身（插件只读取 when / outcome / nodeid / duration）

  参数:
    nodeid (str): 用例节点 ID
    when (str): 阶段，setup / call / teardown
    outcome (str): 结果，passed / failed / skipped
    duration (float): 阶段耗时（秒）

  返回:
    SimpleNamespace: 报告替身

  异常:
    无
  """
  return SimpleNamespace(nodeid=nodeid, when=when, outcome=outcome, duration=duration)


def case_reports(nodeid: str) -> list[SimpleNamespace]:
  """
  给一条用例生成完整的阶段报告序列（四类结果各一条，覆盖全部计数分支）

  参数:
    nodeid (str): 用例节点 ID（必须是本模块四个 CASE_* 常量之一）

  返回:
    list[SimpleNamespace]: 该用例的阶段报告列表

  异常:
    ValueError: nodeid 不在预设场景内时抛出（防止静默生成空序列让断言永真）
  """
  if nodeid == CASE_PASS:
    return [
      make_report(nodeid, "setup", "passed"),
      make_report(nodeid, "call", "passed"),
      make_report(nodeid, "teardown", "passed"),
    ]
  if nodeid == CASE_FAIL:
    return [
      make_report(nodeid, "setup", "passed"),
      make_report(nodeid, "call", "failed"),
      make_report(nodeid, "teardown", "passed"),
    ]
  if nodeid == CASE_SKIP:
    # fixture 级跳过不会产生 call 报告，只有 setup(skipped) + teardown(passed)
    return [
      make_report(nodeid, "setup", "skipped"),
      make_report(nodeid, "teardown", "passed"),
    ]
  if nodeid == CASE_SETUP_ERROR:
    return [
      make_report(nodeid, "setup", "failed"),
      make_report(nodeid, "teardown", "passed"),
    ]
  raise ValueError(f"未知场景用例: {nodeid}")


def shuffled_stream(plugin: ModuleType, worker_cases: dict[str, list[str]]) -> list[Any]:
  """
  生成"跨 worker 乱序、但单用例阶段顺序不变"的报告流

  为什么按用例块乱序而不是逐条乱序: xdist 里同一个 worker 对同一条用例
  的 setup→call→teardown 必然有序（worker 本地串行执行），跨用例之间
  才是真乱序。逐条乱序会造出现实中不存在的到达形态。

  参数:
    plugin (ModuleType): 被测插件模块（用于生成 TestReport 替身）
    worker_cases (dict[str, list[str]]): worker 名 -> 该 worker 执行的用例列表

  返回:
    list[Any]: 乱序后的阶段报告列表

  异常:
    无
  """
  blocks: list[list[Any]] = []
  for cases in worker_cases.values():
    for nodeid in cases:
      blocks.append(case_reports(nodeid))
  rng = random.Random(SHUFFLE_SEED)
  rng.shuffle(blocks)
  return [report for block in blocks for report in block]


def collect_into_stats(plugin: ModuleType, reports: list[Any]) -> Any:
  """
  把一批报告累加进新的会话统计容器

  参数:
    plugin (ModuleType): 被测插件模块
    reports (list[Any]): 阶段报告列表

  返回:
    Any: `SessionExecutionStats` 实例

  异常:
    无
  """
  stats = plugin.SessionExecutionStats()
  for report in reports:
    plugin.record_report(stats, report)
  return stats


def stats_kwargs(total: int) -> dict[str, Any]:
  """
  构造一份收集统计（结构与项目根 conftest 的 CollectionStats 对齐）

  参数:
    total (int): 收集到的用例总数

  返回:
    dict[str, Any]: 收集统计字典

  异常:
    无
  """
  return {
    "total": total,
    "marker_counts": {"smoke": 1},
    "module_counts": {"tests.test_day59_probe": total},
    "unregistered_marker_counts": {},
  }


# ===========================================================================
# 一、乱序归位
# ===========================================================================
@allure.feature("Day59 并发结果合并")
@allure.story("乱序归位")
@allure.title("两个 worker 的报告乱序到达，仍按用例 ID 归位到各自计数")
def test_out_of_order_reports_are_attributed_by_case_id(plugin: ModuleType) -> None:
  """
  归位的可证形态: 每个用例的计数只由它自己的阶段报告决定，与它在流中的
  位置无关。断言四类结果的计数与失败明细的 nodeid 集合，而不是只看 total
  ——total 相同的错误归位（比如把 skipped 记成 passed）也能让总数对上。

  场景: worker A 执行 0001（通过）+ 0003（跳过），worker B 执行
  0002（失败）+ 0004（setup 报错）；两 worker 的用例块交错到达。
  """
  reports = shuffled_stream(
    plugin,
    {"gw0": [CASE_PASS, CASE_SKIP], "gw1": [CASE_FAIL, CASE_SETUP_ERROR]},
  )
  stats = collect_into_stats(plugin, reports)

  assert (stats.passed, stats.failed, stats.skipped, stats.errors) == (1, 1, 1, 1)
  assert stats.total == 4
  # 阶段报告条数: 3 + 3 + 2 + 2 = 10（skip 用例没有 call 报告）
  assert stats.report_count == 10
  assert stats.phase_counts["setup"] == {"passed": 2, "skipped": 1, "failed": 1}
  assert stats.phase_counts["call"] == {"passed": 1, "failed": 1}
  assert stats.phase_counts["teardown"] == {"passed": 4}

  # 失败明细只含真正没通过的用例，且每条只出现一次（按 nodeid + 阶段归位）
  failed_pairs = {(item["nodeid"], item["phase"]) for item in stats.failed_cases}
  assert failed_pairs == {(CASE_FAIL, "call"), (CASE_SETUP_ERROR, "setup")}


@allure.feature("Day59 并发结果合并")
@allure.story("乱序归位")
@allure.title("同一批报告的任意到达顺序产出完全相同的执行统计")
def test_any_arrival_order_yields_identical_execution_stats(plugin: ModuleType) -> None:
  """
  对照组设计: 把同一批用例按"worker 串行顺序"喂一遍，再按固定种子的乱序
  喂一遍，两份统计必须逐字段相等。没有这条对照，前一条用例只能证明"某次
  乱序结果正确"，证明不了"与顺序无关"。

  刻意比较 `failed_cases` 之前先按 (nodeid, phase) 排序: 该列表保留的是
  到达顺序（插件不做排序），本用例要钉的是**集合与计数**，不是列表顺序。
  """
  worker_cases = {"gw0": [CASE_PASS, CASE_FAIL], "gw1": [CASE_SKIP, CASE_SETUP_ERROR]}
  serial_reports = [report for cases in worker_cases.values() for case in cases
                    for report in case_reports(case)]
  shuffled_reports = shuffled_stream(plugin, worker_cases)

  serial_stats = collect_into_stats(plugin, serial_reports)
  shuffled_stats = collect_into_stats(plugin, shuffled_reports)

  assert serial_stats.total == shuffled_stats.total == 4
  assert serial_stats.report_count == shuffled_stats.report_count == 10
  assert serial_stats.phase_counts == shuffled_stats.phase_counts
  assert sorted(
    (item["nodeid"], item["phase"]) for item in serial_stats.failed_cases
  ) == sorted(
    (item["nodeid"], item["phase"]) for item in shuffled_stats.failed_cases
  )


# ===========================================================================
# 二、计数一致性
# ===========================================================================
@allure.feature("Day59 并发结果合并")
@allure.story("计数一致性")
@allure.title("master 合并计数等于各 worker 单独计数之和")
def test_merged_counts_equal_sum_of_worker_counts(plugin: ModuleType) -> None:
  """
  master 口径为什么天然是全量: worker 把每条阶段报告回传 master，master
  的 `pytest_runtest_logreport` 累加到的就是整场会话。本用例把"各 worker
  本地统计"与"master 汇总统计"分别算一遍，逐项对账。

  只对账、不相加收集统计——收集统计的合并口径见下一组用例（取 total 更大者）。
  """
  worker_cases = {"gw0": [CASE_PASS, CASE_SKIP], "gw1": [CASE_FAIL, CASE_SETUP_ERROR]}
  per_worker = {
    worker: collect_into_stats(
      plugin, [report for case in cases for report in case_reports(case)]
    )
    for worker, cases in worker_cases.items()
  }
  merged = collect_into_stats(
    plugin,
    [
      report
      for cases in worker_cases.values()
      for case in cases
      for report in case_reports(case)
    ],
  )

  assert merged.passed == sum(stats.passed for stats in per_worker.values())
  assert merged.failed == sum(stats.failed for stats in per_worker.values())
  assert merged.skipped == sum(stats.skipped for stats in per_worker.values())
  assert merged.errors == sum(stats.errors for stats in per_worker.values())
  assert merged.report_count == sum(
    stats.report_count for stats in per_worker.values()
  )
  # 总数守恒: 汇总 total 恰为 4 个用例的结果条数，不因多 worker 而翻倍
  assert merged.total == 4


# ===========================================================================
# 三、收集统计合并（重复上报去重 + 取更大者）
# ===========================================================================
@allure.feature("Day59 并发结果合并")
@allure.story("收集统计合并")
@allure.title("同一份收集统计被两个 worker 重复上报，只计一次（幂等）")
def test_duplicate_collection_stats_count_once(plugin: ModuleType) -> None:
  """
  合并函数的幂等性就是"重复上报去重"的可证形态: 合并口径是取 total 更大者，
  不是相加，故同一份统计收两次结果不变。若哪天有人把口径改成累加，这条
  会立刻变红（total 变成 2N）。
  """
  stats = stats_kwargs(4)
  once = plugin.merge_worker_collection_stats(None, stats)
  twice = plugin.merge_worker_collection_stats(once, dict(stats))

  assert once is stats
  assert twice["total"] == 4
  # 对象可以不同（master 收的是 xdist 反序列化出来的新字典），口径必须相同
  assert twice == stats


@allure.feature("Day59 并发结果合并")
@allure.story("收集统计合并")
@allure.title("按 total 更大者合并，与两个 worker 的到达顺序无关")
def test_merge_keeps_larger_total_regardless_of_arrival_order(plugin: ModuleType) -> None:
  """
  两个 worker 收集的是同一批用例，只有"收集完整度"可能不同（某 worker 启动
  早期被打断）。取 total 更大者既能拿到最接近完整会话的一份，也保证结果确定:
  small→full 与 full→small 必须得到同一个值。
  """
  small = stats_kwargs(3)
  full = stats_kwargs(4)

  forward = plugin.merge_worker_collection_stats(None, small)
  forward = plugin.merge_worker_collection_stats(forward, full)
  backward = plugin.merge_worker_collection_stats(None, full)
  backward = plugin.merge_worker_collection_stats(backward, small)

  assert forward["total"] == backward["total"] == 4


# ===========================================================================
# 四、worker 崩溃后的部分结果保留
# ===========================================================================
@allure.feature("Day59 并发结果合并")
@allure.story("worker 崩溃")
@allure.title("worker 崩溃（workeroutput 为 None）不覆盖已合并统计")
def test_crashed_worker_keeps_previous_merged_stats(plugin: ModuleType) -> None:
  """
  崩溃 worker 的真实形态: `pytest_testnodedown` 的 `error` 非 None，且
  `workeroutput` 可能为 None（来不及上报就退出）。此时合并逻辑必须
  ①不抛异常（否则 master 收尾被打断）；②保留先到 worker 的统计。

  随后再来一个健康 worker，仍应正常合并——证明崩溃分支没有把状态改成
  "永久不可写"。
  """
  healthy = stats_kwargs(4)
  plugin._STATE.worker_collection_stats = None
  plugin.pytest_testnodedown(
    SimpleNamespace(workeroutput={plugin.WORKER_COLLECTION_STATS_KEY: healthy}), None
  )
  assert plugin._STATE.worker_collection_stats == healthy

  crashed = SimpleNamespace(workeroutput=None)
  plugin.pytest_testnodedown(crashed, RuntimeError("worker exited unexpectedly"))
  assert plugin._STATE.worker_collection_stats == healthy

  # 崩溃后再来一个健康 worker（统计更完整）: 合并继续生效
  fuller = stats_kwargs(5)
  plugin.pytest_testnodedown(
    SimpleNamespace(workeroutput={plugin.WORKER_COLLECTION_STATS_KEY: fuller}), None
  )
  assert plugin._STATE.worker_collection_stats == fuller


@allure.feature("Day59 并发结果合并")
@allure.story("worker 崩溃")
@allure.title("workeroutput 缺键或形态非法时降级为跳过该 worker")
def test_invalid_worker_output_is_skipped(plugin: ModuleType) -> None:
  """
  崩溃/半崩溃 worker 的三种非法上报形态，逐一验证"跳过该 worker、不崩":
    1. 有其他键但没有本插件的键（别的插件也在用 workeroutput）；
    2. 整个 workeroutput 不是字典（xdist 版本差异或被替身污染）；
    3. 节点对象上根本没有 workeroutput 属性（早期退出，构造不完整）。
  """
  baseline = stats_kwargs(4)
  plugin._STATE.worker_collection_stats = baseline

  plugin.pytest_testnodedown(
    SimpleNamespace(workeroutput={"other_plugin": {"total": 99}}), None
  )
  plugin.pytest_testnodedown(SimpleNamespace(workeroutput=[baseline]), None)
  plugin.pytest_testnodedown(SimpleNamespace(), None)

  assert plugin._STATE.worker_collection_stats == baseline


@allure.feature("Day59 并发结果合并")
@allure.story("worker 崩溃")
@allure.title("master stash 为空时从 worker 上报恢复收集统计并落盘")
def test_collection_stats_recovered_from_worker_output(
  plugin: ModuleType, tmp_path: Path
) -> None:
  """
  端到端拼装（替身 config，不启进程）: master 不做收集 → stash 里没有统计
  → sessionfinish 回落到 `_STATE.worker_collection_stats`。断言落到报告 JSON
  里的 `collection_stats` 是完整口径，而不是 null。

  这条同时是"worker 崩溃后部分结果保留"的收尾验证: 只有一个 worker 成功
  上报时，报告仍应带上这份统计，不能因为另一个 worker 崩了就整体丢弃。
  """
  report_path = tmp_path / "day59_report.json"
  values = {
    plugin.INI_REPORT_PATH: str(report_path),
    plugin.INI_REPORT_ENABLED: True,
    plugin.INI_SUMMARY_ENABLED: False,
  }
  config = SimpleNamespace(
    rootpath=tmp_path,
    getini=lambda name: values[name],
    pluginmanager=SimpleNamespace(get_plugins=lambda: set()),
    stash=pytest.Stash(),
  )
  plugin._STATE.start_session(config)

  # worker 侧上报 + master 侧合并（崩溃 worker 先到、healthy worker 后到）
  plugin.pytest_testnodedown(SimpleNamespace(workeroutput=None), RuntimeError("crash"))
  plugin.pytest_testnodedown(
    SimpleNamespace(
      workeroutput={plugin.WORKER_COLLECTION_STATS_KEY: stats_kwargs(4)}
    ),
    None,
  )
  # master 自身执行统计按正常链路累加
  for report in case_reports(CASE_PASS):
    plugin.record_report(plugin._STATE.stats, report)

  plugin.pytest_sessionfinish(SimpleNamespace(config=config), 0)

  payload = json.loads(report_path.read_text(encoding="utf-8"))
  assert payload["collection_stats"] is not None
  assert payload["collection_stats"]["total"] == 4
  assert payload["execution_stats"]["passed"] == 1


# ===========================================================================
# 五、写库去重（第二道防线）
# ===========================================================================
@allure.feature("Day59 并发结果合并")
@allure.story("写库去重")
@allure.title("同一 (execution_id, case_id) 第二次写入返回 False 且库里只有一条")
def test_duplicate_case_write_is_deduplicated(temp_db: None) -> None:
  """
  两个 worker 各写同一条用例明细时的顺序化验证: 第一次返回 True，第二次
  返回 False，明细表只有一行。

  为什么放在本文件: 插件层按阶段报告条数累加（pytest 自身口径），**不做**
  逐用例去重；真正挡住双写的是写库层的写前查重。并发窗口下 SELECT 与
  INSERT 之间理论上仍有 TOCTOU 间隙（表上只有普通复合索引、没有
  (execution_id, case_id) 唯一约束），该缺陷的修复与多 worker 幂等对账
  按排期归 Day60，本用例只钉"顺序重复写入必须被吸收"这一既有契约。
  """
  started = datetime(2026, 10, 19, 10, 0, 0)
  finished = datetime(2026, 10, 19, 10, 0, 1)
  first = CaseManager.record_execution(
    "RUN-D59-MERGE",
    CASE_PASS,
    "并发合并验证用例",
    "passed",
    started,
    finished,
    1.0,
  )
  second = CaseManager.record_execution(
    "RUN-D59-MERGE",
    CASE_PASS,
    "并发合并验证用例",
    "passed",
    started,
    finished,
    1.0,
  )

  assert first is True
  assert second is False
  with DatabaseSession.session_scope() as session:
    rows = (
      session.query(ExecutionRow)
      .filter_by(execution_id="RUN-D59-MERGE", case_id=CASE_PASS)
      .all()
    )
  assert len(rows) == 1
