"""
项目根 pytest 配置：收集期钩子（Day55）+ per-run 唯一 Allure 结果目录（Day53）

一、本模块的钩子（按执行时机排序；三条之间只有"统计写入 → 摘要读取"一条依赖）:

  1. `pytest_configure`（配置阶段，最早）
    职责: 应用 per-run 唯一 Allure 结果目录（见下方"Day53 修复说明"）。
  2. `pytest_collection_modifyitems`（收集阶段：收集完成、任何用例执行之前）
    职责: ①三级字典序稳定排序 ②收集统计写入 `config.stash`
       ③标记完整性检查（已注册标记使用情况 + 未注册标记登记）。
    为什么落在项目根: 排序与统计的口径必须对整个会话唯一，而项目根
    conftest 是外层会话与嵌套运行**都会加载**的那一份（理由同 Day53）。
  3. `pytest_sessionfinish`（会话阶段，收尾）
    职责: 从 `config.stash` 取第 2 步的统计，向终端输出一行收集摘要。

  与 tests/conftest.py 的关系: 后者另有 pytest_configure（Loguru 配置）/
  pytest_sessionstart（Allure 环境信息）/ pytest_runtest_makereport（失败采集）/
  pytest_sessionfinish（会话统计日志）。pytest 允许多个 conftest 各自持有同名
  钩子，按插件注册顺序逐个执行、互不覆盖；本模块三个钩子与它们零职责重叠，
  也不存在重复注册（Day55 已审查确认）。

二、Day53 修复说明（per-run 唯一 Allure 结果目录）

为什么这个修复必须落在**项目根**的 conftest.py（而不是 tests/conftest.py）:
  conftest 的加载范围是"被收集路径的全部祖先目录"。本项目的真实子进程
  用例（`tests/test_day44_bugfix_demo.py`、`tests/test_pytest_runner.py`）
  会把探针测试文件写到 `output/day44_pytest_scratch/` 之下，嵌套 pytest
  收集的是 `output/...` 里的文件——`tests/conftest.py` 不是它的祖先，
  根本不会被加载。只有项目根的 conftest.py 同时位于
  "`tests/` 的祖先"与"`output/...` 的祖先"两条路径上，才能让**外层会话
  与嵌套运行拿到同一套隔离规则**。

修的是什么（可复现的失败签名）:
  `pytest.ini` 的 addopts 固定了 `--alluredir=output/allure_results
  --clean-alluredir`，于是**每一个**读到该配置的 pytest 进程都会在
  `pytest_configure` 阶段对同一个共享目录做一次"先删后建"
  （`AllureFileLogger.__init__`: `if is_dir() and clean: rmtree(...)`）。
  本机实测的两种翻车形态都出自这一步：
   1. 目录已被同机另一个 pytest 进程删掉，删除动作抛
     `WinError 145` / `FileNotFoundError` → `INTERNALERROR` →
     整轮退出码 3。嵌套运行被腰斩，退出码 3 被退出码映射归为
     `error` 而非 `failed`——即 KNOWN_LIMITATIONS 第 6 条记录的
     `test_real_subprocess_failed` 偶发 error 根因。
   2. Windows 下目录被占用（杀毒/索引/同机另一进程正在写）时
     `rmtree` 失败，同样是整轮 INTERNALERROR。

两道闸（互相独立，任一单独成立即可消除上述失败）:
  ① **per-run 唯一目录**：把生效的 `--alluredir` 改写成
    `<原目录>/run-<微秒时间戳>-<pid>`。该目录必然是**新建**的，
    `AllureFileLogger` 的 `is_dir()` 判定为假 → 根本不进 rmtree 分支，
    "两个进程抢同一个目录"这件事在结构上不再可能。
  ② **关闭破坏性清理**：把 `clean_alluredir` 置假。即使将来有人把
    alluredir 改回一个已存在的共享目录，也不会再有"先删后建"这个
    动作——结果目录只增不删（写入方按目录隔离自证唯一，见下）。

刻意不做的两件事:
  - **不动 pytest.ini**：`--alluredir` / `--clean-alluredir` 的配置面
   被多处文档与用例引用，本修复只在**运行时**改写生效值，
   配置文案保持原样。
  - **不重定向执行器已隔离的目录**：真实执行器为每条用例分配的
   `output/allure_results_<用例>_<时间戳>_<pid>_<序号>` 已是 per-run
   唯一目录，再嵌一层会让 `PytestRunner.bridge_allure_results`
   按原路径解析不到结果文件、桥接入库静默失效。故只对**未被隔离**的
   目录（即 pytest.ini 里的共享默认目录）做重定向。

保留策略: 新旧 per-run 目录在同一父目录下共存，超量时按目录名倒序保留
最近 N 个（目录名含微秒时间戳，名称序即创建序）。只清理本机制自己产出
的 `run-` 前缀目录，且跳过刚创建（时长不足）的，避免删掉并发运行中的目录。
"""

import os
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# per-run 目录名前缀（本机制产物的唯一识别依据，清理时靠它区分"自己人"）
PER_RUN_ALLURE_DIR_PREFIX = "run-"

# 已自带 per-run 隔离的目录前缀：命中即不再嵌套（真实执行器的独立目录）
ISOLATED_ALLURE_DIR_PREFIXES = ("allure_results_", PER_RUN_ALLURE_DIR_PREFIX)

# 同一父目录下保留的 per-run 目录数量（超量按名称倒序删最旧）
PER_RUN_ALLURE_DIR_KEEP = 5

# 清理的最小存活时长（秒）：只删"创建已久"的目录，防误删并发运行中的目录
PER_RUN_ALLURE_DIR_MIN_AGE_SECONDS = 300.0


def is_isolated_allure_dir(results_dir: str | Path) -> bool:
  """
  判定目录是否**已自带** per-run 隔离（命中则本机制不再嵌套一层）

  参数:
    results_dir (str | Path): 待判定的 Allure 结果目录

  返回:
    bool: 目录名以 ISOLATED_ALLURE_DIR_PREFIXES 之一开头返回 True

  异常:
    无
  """
  return Path(str(results_dir)).name.startswith(ISOLATED_ALLURE_DIR_PREFIXES)


def resolve_per_run_allure_dir(
  base_dir: str | Path,
  stamp: str | None = None,
  pid: int | None = None,
) -> Path:
  """
  计算本次运行的 per-run 唯一 Allure 结果目录（纯函数，不落盘）

  唯一性由 <微秒时间戳> + <进程号> 双要素保证:
    - 时间戳解决**同一台机器上先后启动**的多次运行（含嵌套运行）；
    - pid 解决**同一微秒节拍内**两次运行（Windows 时钟粒度约 15.6ms，
     同节拍内 datetime 的微秒位可能完全相同）。
  两者组合后目录名不重复，故"生效目录必然不存在"，破坏性清理无从触发。

  参数:
    base_dir (str | Path): 原生效目录（通常来自 pytest.ini 的 addopts 值）
    stamp (str | None): 时间戳文本；None 时取当前时间（测试可注入）
    pid (int | None): 进程号；None 时取当前进程（测试可注入）

  返回:
    Path: 尚未创建的 per-run 目录路径（本函数**不创建目录**，
       mkdir 与清理策略均由消费者负责）

  异常:
    无
  """
  if stamp is None:
    stamp = f"{time.time():.6f}".replace(".", "")
  if pid is None:
    pid = os.getpid()
  return Path(base_dir) / f"{PER_RUN_ALLURE_DIR_PREFIX}{stamp}-{pid}"


def prune_old_per_run_dirs(
  base_dir: str | Path,
  current_dir: str | Path,
  keep: int = PER_RUN_ALLURE_DIR_KEEP,
) -> list[str]:
  """
  清理同父目录下陈旧的 per-run 结果目录（保留最近 keep 个）

  只处理以 PER_RUN_ALLURE_DIR_PREFIX 开头、且确实存在的目录；当前目录
  强制跳过（绝不删自己）；存活时长不足阈值的一律跳过（并发运行中）。
  删除失败只记入返回值的缺失，绝不抛异常——清理是旁路能力。

  参数:
    base_dir (str | Path): 父目录
    current_dir (str | Path): 本次运行目录（强制保留）
    keep (int): 保留的最近目录数量

  返回:
    list[str]: 已删除的目录名列表（删除失败的不会出现在其中）

  异常:
    无
  """
  base = Path(base_dir)
  current = Path(current_dir)
  try:
    if not base.is_dir():
      return []
    candidates = sorted(
      (
        path
        for path in base.glob(f"{PER_RUN_ALLURE_DIR_PREFIX}*")
        if path.is_dir()
      ),
      key=lambda path: path.name,
      reverse=True,
    )
    now = time.time()
    removed: list[str] = []
    for index, path in enumerate(candidates):
      if path == current or index < keep:
        continue
      try:
        if now - path.stat().st_mtime < PER_RUN_ALLURE_DIR_MIN_AGE_SECONDS:
          continue
        shutil.rmtree(path, ignore_errors=True)
      except OSError:
        continue
      removed.append(path.name)
    return removed
  except Exception:  # noqa: BLE001 清理失败绝不能把整轮 pytest 搞挂
    return []


def apply_per_run_allure_dir(config: Any) -> str:
  """
  把生效的 `--alluredir` 改写成 per-run 唯一目录，并关闭破坏性清理

  参数:
    config (Any): pytest 的 Config 对象。这里刻意用 Any 而不是精确类型：
           本模块被**每一个** pytest 进程导入（含嵌套运行），
           而 pytest 的类型桩在部分环境下不完整，写死类型会让
           conftest 在导入期就失败——那等于把整轮测试搞挂。
           取用面只有 getoption 与 option 两个鸭子类型属性。

  返回:
    str: 改写后实际生效的 Allure 结果目录；未配置 alluredir 时返回空串

  异常:
    无（配置改写失败不得让整轮 pytest 起不来）
  """
  try:
    raw_dir = config.getoption("--alluredir", default=None)
  except Exception:  # noqa: BLE001 配置访问异常不阻断会话
    return ""
  if not raw_dir:
    return ""

  effective_dir = str(raw_dir)
  if not is_isolated_allure_dir(effective_dir):
    run_dir = resolve_per_run_allure_dir(effective_dir)
    config.option.allure_report_dir = str(run_dir)
    # 清理失败绝不能把整轮 pytest 搞挂（prune 内部自身已兜底，
    # 此处再包一层是防"配置改写路径上的任何意外"）
    try:
      prune_old_per_run_dirs(effective_dir, run_dir)
    except Exception:  # noqa: BLE001 同上
      pass
    effective_dir = str(run_dir)

  # 第二道闸：关闭破坏性清理（详见模块 docstring 的"两道闸"）
  try:
    config.option.clean_alluredir = False
  except Exception:  # noqa: BLE001 无该选项时忽略（allure 插件未安装）
    return effective_dir
  return effective_dir


def pytest_configure(config: Any) -> None:
  """
  配置阶段钩子（最早）：应用 per-run 唯一 Allure 结果目录

  conftest 的钩子先于插件（allure-pytest）的 pytest_configure 执行，
  故此处改写后的值会被 `AllureFileLogger` 直接读到——这是本修复能生效的
  关键时序，改动本函数位置或改由插件侧钩子实现都会让修复静默失效。

  参数:
    config (Any): pytest 的 Config 对象（框架传入）

  返回:
    无
  """
  apply_per_run_allure_dir(config)


# ===========================================================================
# 收集期钩子（Day55）：稳定排序 + 收集统计 + 标记完整性检查
#
# 三个常量的取值口径:
#  - COLLECTION_STATS_STASH_KEY 用 StashKey 而非普通属性名: StashKey 以
#   **对象身份**为键，避免与插件/其他 conftest 的同名属性相撞。
#  - TRACKED_MARKERS 是 pytest.ini markers 段注册的四个业务标记，摘要行按
#   这个顺序固定输出（未命中的也输出 0，便于横向对比）。
#  - BUILTIN_MARKERS 是 pytest 内建标记: 它们由框架/内建插件注入，不算
#   "未注册的自定义标记"，故在完整性检查里显式豁免。
# ===========================================================================

# 收集统计在 config.stash 中的键（跨钩子传递的唯一通道）
COLLECTION_STATS_STASH_KEY: pytest.StashKey["CollectionStats"] = pytest.StashKey()

# 摘要行前缀（终端里定位本平台输出的锚点）
COLLECTION_SUMMARY_PREFIX = "[TestMatrix]"

# 业务标记（pytest.ini markers 段注册的四个）
TRACKED_MARKERS: tuple[str, ...] = ("smoke", "regression", "api", "slow")

# pytest 内建标记（不做"未注册"判定）
BUILTIN_MARKERS: frozenset[str] = frozenset({
  "parametrize",
  "skip",
  "skipif",
  "xfail",
  "usefixtures",
  "filterwarnings",
})


@dataclass(frozen=True)
class CollectionStats:
  """
  一次收集的统计快照（不可变，跨钩子只读传递）

  属性:
    total (int): 本次收集的用例总数
    marker_counts (dict[str, int]): 已注册标记名 -> 命中用例数；同一用例
      带同名标记多次时按标记出现次数计；未出现在任何用例上的标记
      不会进字典（摘要行按 TRACKED_MARKERS 取值，缺省显示 0）
    module_counts (dict[str, int]): 测试模块 __name__ -> 该模块用例数
    unregistered_marker_counts (dict[str, int]): 未注册标记名 -> 命中用例数
      （正常情况下恒为空: `--strict-markers` 会在收集期直接报错）
  """

  total: int
  marker_counts: dict[str, int]
  module_counts: dict[str, int]
  unregistered_marker_counts: dict[str, int]


def registered_marker_names(config: Any) -> frozenset[str]:
  """
  取本次会话已注册的标记名集合（pytest.ini 的 markers 段 + pytest 内建标记）

  参数:
    config (Any): pytest 的 Config 对象。与 apply_per_run_allure_dir 同款
           口径: 本模块被每一个 pytest 进程导入（含嵌套运行），
           取用面只有 getini 一个鸭子类型方法。

  返回:
    frozenset[str]: 已注册标记名；ini 读取失败时只返回内建标记集合
            （完整性检查是排查辅助，绝不因此阻断会话）

  异常:
    无
  """
  names = set(BUILTIN_MARKERS)
  try:
    for entry in config.getini("markers"):
      # ini 条目形如 "smoke: 冒烟测试用例"，标记名是冒号前的那一段
      name = str(entry).split(":", 1)[0].strip()
      if name:
        names.add(name)
  except Exception:  # noqa: BLE001 读不到 markers 配置就不做完整性检查
    pass
  return frozenset(names)


def module_name_of(item: Any) -> str:
  """
  取用例所属测试模块名（排序一级键、模块分组键）

  非 Python 节点（如 doctest 类 Item）的 `item.module` 可能不存在，
  故一律用"取不到就是空串"的口径——排序只求确定性，不追求覆盖面。

  参数:
    item (Any): pytest 的 Item（或测试用的鸭子类型替身）

  返回:
    str: 模块的 __name__；取不到时返回空串

  异常:
    无
  """
  return str(getattr(getattr(item, "module", None), "__name__", ""))


def class_name_of(item: Any) -> str:
  """
  取用例所属测试类名（排序二级键）

  参数:
    item (Any): pytest 的 Item（或测试用的鸭子类型替身）

  返回:
    str: 类名；模块级用例（item.cls 为 None）返回空串（空串最小，
       故同一模块内模块级用例永远排在类的成员之前）

  异常:
    无
  """
  return str(getattr(getattr(item, "cls", None), "__name__", ""))


def collection_sort_key(item: Any) -> tuple[str, str, str]:
  """
  收集排序键: (模块名, 类名, 用例名) 三级字典序

  参数:
    item (Any): pytest 的 Item（或测试用的鸭子类型替身）

  返回:
    tuple[str, str, str]: 三级排序键

  异常:
    无
  """
  return (
    module_name_of(item),
    class_name_of(item),
    str(getattr(item, "name", "")),
  )


def build_collection_stats(
  items: Iterable[Any], registered: frozenset[str]
) -> CollectionStats:
  """
  统计本次收集的用例分布（纯函数，不触碰任何全局状态）

  统计口径:
    - marker: 按 `item.iter_markers()` 逐个计数（同一用例带多个标记则各计一次）
    - 模块: 按 module_name_of(item)（即模块 __name__）分组计数
    - 未注册标记: 命中 registered 之外的标记名时登记（正常恒为空）

  刻意不用 `item.keywords` 判定标记: keywords 里混着节点名（模块名/类名/
  函数名）与 pytestmark 这类元键，直接当标记用会把这些名字误判成
  "未注册标记"；`iter_markers()` 才只给真实标记。未注册标记本就由
  `--strict-markers` 在收集期拦截，本函数只做**存在性登记**，
  既不重复报错，也不做"标记了 slow 是否真的耗时较长"的耗时判定（本次范围外）。

  参数:
    items (Iterable[Any]): 用例序列
    registered (frozenset[str]): 已注册标记名集合

  返回:
    CollectionStats: 不可变统计快照

  异常:
    无
  """
  marker_counts: dict[str, int] = {}
  module_counts: dict[str, int] = {}
  unregistered_marker_counts: dict[str, int] = {}
  total = 0
  for item in items:
    total += 1
    module_name = module_name_of(item)
    module_counts[module_name] = module_counts.get(module_name, 0) + 1
    for mark in item.iter_markers():
      name = str(getattr(mark, "name", ""))
      if not name:
        continue
      if name in registered:
        marker_counts[name] = marker_counts.get(name, 0) + 1
      else:
        unregistered_marker_counts[name] = (
          unregistered_marker_counts.get(name, 0) + 1
        )
  return CollectionStats(
    total=total,
    marker_counts=marker_counts,
    module_counts=module_counts,
    unregistered_marker_counts=unregistered_marker_counts,
  )


def format_collection_summary(stats: CollectionStats) -> str:
  """
  生成一行收集摘要（终端输出用）

  格式: [TestMatrix] 收集 N 用例：smoke=a / regression=b / api=c / slow=d，模块=M
  仅当出现未注册标记时追加"，未注册标记=名称×次数、…"——正常情况不出现，
  一旦出现即说明完整性检查真的抓到了 `--strict-markers` 之外的东西。

  参数:
    stats (CollectionStats): 收集统计快照

  返回:
    str: 单行摘要（不含换行符）

  异常:
    无
  """
  tracked = " / ".join(
    f"{name}={stats.marker_counts.get(name, 0)}" for name in TRACKED_MARKERS
  )
  line = (
    f"{COLLECTION_SUMMARY_PREFIX} 收集 {stats.total} 用例：{tracked}，"
    f"模块={len(stats.module_counts)}"
  )
  if stats.unregistered_marker_counts:
    detail = "、".join(
      f"{name}×{count}"
      for name, count in sorted(stats.unregistered_marker_counts.items())
    )
    line = f"{line}，未注册标记={detail}"
  return line


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
  """
  收集阶段钩子：三级字典序稳定排序 + 收集统计 + 标记完整性检查

  执行时机: 收集阶段，全部用例收集完成之后、任何用例执行之前。
    排在 `pytest_configure`（配置阶段）之后、`pytest_sessionfinish`
    （会话收尾）之前；tests/conftest.py 没有本钩子，不存在同名竞争。

  职责:
    ①**稳定排序**: 按 (模块名, 类名, 用例名) 三级字典序对 items **就地**
     排序，消除文件系统遍历顺序差异带来的执行顺序漂移，为 Day58
     xdist 并发的结果归位与 Day56 插件的结果上报提供确定性基线。
     `list.sort` 的稳定性只对**三级键完全相同**的用例保序，不覆盖参数组:
     参数化用例的 `item.name` 带 `[参数id]` 后缀，三级键两两不同，故参数组
     按**参数 id 的字典序**排列而非定义序。本项目数据驱动用例的 id 多为递增
     编号（TM-API-0001→0004），字典序恰等于定义序故无感知；id 非递增时会被重排（Day55-fix 修正）。
    ②**收集统计**: 统计 marker 分布 / 模块分布 / 总数，写入 `config.stash`
     （键为 COLLECTION_STATS_STASH_KEY），由 `pytest_sessionfinish` 读取输出。
    ③**标记完整性检查**: 登记已注册标记的使用情况与未注册标记（见
     `build_collection_stats` 的口径说明）。

  刻意不抛异常: 排序与统计都是旁路能力，任何一步意外都不该把整轮收集搞挂
  （与 Day53 的 Allure 目录修复同一口径）。

  参数:
    config (Any): pytest 的 Config 对象（框架传入）
    items (list[Any]): 本次收集到的用例列表；pytest 传入的就是会话持有的
             同一个 list，就地排序即对后续执行生效

  返回:
    无
  """
  try:
    registered = registered_marker_names(config)
    items.sort(key=collection_sort_key)
    config.stash[COLLECTION_STATS_STASH_KEY] = build_collection_stats(
      items, registered
    )
  except Exception:  # noqa: BLE001 排序/统计失败不得阻断整轮收集
    return


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
  """
  会话收尾钩子：输出一行收集摘要（读 pytest_collection_modifyitems 的统计）

  执行时机: 会话阶段收尾，且在 pytest 自身的终端汇总之前（conftest 的钩子
    实现后注册先执行）。与 tests/conftest.py 的同名钩子各自执行、互不覆盖:
    本钩子只读 `config.stash` 里的收集统计、不碰会话结果与退出码。
  `--collect-only` 同样会走到本钩子（统计在收集期已写好），故"只看收集"
  的场景也能看到摘要（Day55 验收第 4 条据此）。

  xdist 下本摘要**不输出**（Day58-fix 记录，Day58-fix3 校正机制描述）: 两侧成因
  不同，结果都是"master 终端没有这一行":
    ① **master 侧——没收集**: xdist 的 master 不做收集（其在 `pytest_collection`
      里直接 return True），master 的 stash 里没有统计，走到下面 `stats is None`
      就返回；
    ② **worker 侧——写了但到不了 master 终端**: worker 进程**确实有**
      `terminalreporter`（pytest 7.4.4 存在 `Config.InvocationParams`，xdist
      `remote.py` 因此走 `_prepareconfig(args, None)` 分支，那个只在旧 pytest 上
      执行的 `remote_initconfig`（它会往 worker 的启动参数里追加"禁用终端插件"
      一项）**不会被调用**），它的 `pytest_sessionfinish` 会照常执行、`write_line`
      也真的写；但写的是 **worker 进程自己的标准输出**，xdist 不把 worker 的终端
      输出转发到 master 终端（只在失败报告里回显），所以 master 终端看不到。
  （Day58-fix2 曾把②记成"worker 进程里没有终端插件、摘要从未产生"（依据就是上面那段
  在当前 pytest 版本下不会执行的旧分支），Day58-fix3 已三重取证证伪并改回：源码分支
  `hasattr(Config, "InvocationParams")` 在 pytest 7.4.4 为真；worker 内
  `get_plugin("terminalreporter")` 实测返回 TerminalReporter 实例；worker 打标记行后
  master 终端只出现 master 自己那行。写"没有终端"与写"没转发"在排障时指向完全不同的
  方向，故这条必须按实测写。）
  故 `-n` 模式下会话级信息由插件（`testmatrix_plugin`）的"会话完成"摘要承担。
  这里在入口显式加 worker 守卫，是把这个事实写成代码而不是留给"恰好读不到"。

  判定就地用 `hasattr(config, "workerinput")` 单判据，理由是三条:
    1. 不 import 插件的同名工具函数——conftest 被每一个 pytest 进程加载，
      反向依赖插件会把"插件没装/装错版本"变成"整轮收集起不来"；
    2. 不用 `PYTEST_XDIST_WORKER` 环境变量兜底——该变量会被嵌套 pytest /
      执行器子进程继承，用 env 判据会**误杀普通子进程的摘要输出**；
    3. 真实 xdist 进程里 `workerinput` / `workeroutput` 总是成对注入，
      只认 `workerinput` 与插件侧的双通道 + env 兜底在本进程内等价；
      插件那边之所以要双通道 + env，是因为它要在 worker 上报通道里区分
      身份、并对替身配置留兜底，与本处的取舍不同。

  参数:
    session (Any): pytest 的 Session 对象（框架传入）
    exitstatus (int): pytest 退出码；本钩子只输出摘要，绝不影响它

  返回:
    无

  异常:
    无（无统计或终端插件缺失时静默跳过）
  """
  config = getattr(session, "config", None)
  if config is not None and hasattr(config, "workerinput"):
    # xdist worker 会写摘要，但写进 worker 自己的输出流、到不了 master 终端
    return
  stash = getattr(config, "stash", None)
  if stash is None:
    return
  stats = stash.get(COLLECTION_STATS_STASH_KEY, None)
  if stats is None:
    return
  terminal = config.pluginmanager.get_plugin("terminalreporter")
  if terminal is None:
    # 终端插件被显式禁用等场景: 本就没有终端可写，静默跳过。
    # 刻意不用 print（项目铁律: 日志走 loguru，不散落 print）
    return
  # 走终端写入器而非 print，保证在 pytest 的输出捕获机制下依然可见
  terminal.write_line(format_collection_summary(stats))
