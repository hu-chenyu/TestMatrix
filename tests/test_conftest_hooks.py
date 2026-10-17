"""
项目根 conftest 收集期钩子的行为验证（Day55 任务二）

被验证对象: 项目根 `conftest.py`（不是 tests/conftest.py）
 ——收集排序与统计的口径必须对整个会话唯一，而项目根 conftest 是外层
  会话与嵌套运行（收集 output/ 下的探针文件）都会加载的那一份。

覆盖场景:
  1. 稳定排序: 三级字典序（模块 → 类 → 用例名）生效，三级键相同的用例
    保持收集时的相对顺序
  2. 收集统计: marker / 模块 / 总数与传入用例集一致；未注册标记被登记，
    pytest 内建标记不误判；空用例集与 ini 读取异常等退化输入不炸钩子
  3. 不破坏性: 排序后用例数量不变、item 对象自身零改动
  4. 真实会话: 根 conftest 的两个钩子确实注册进当前会话，且收集统计
    真的写进了当前会话的 stash
  5. 端到端: 真起一个嵌套 pytest，断言 `--collect-only` 的输出顺序已是
    排好序的，且收集摘要行出现在终端输出里
  6. xdist 守卫（Day58-fix）: worker 进程（config 带 `workerinput`）不输出
    收集摘要，master/串行仍照常输出（阳性对照），且都不抛异常

为什么用鸭子类型替身而不是构造真实 _pytest Item: 真实 Item 的构造需要会话、
路径与节点树三者齐备，而本钩子只用 item 的四个属性面（module / cls / name /
iter_markers），替身足以把行为逐项钉死；真实链路一侧由第 4、5 组覆盖——
两条路合起来，既精确到单条断言，又不脱离真实收集流程。
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import allure
import pytest

# 项目根（本文件位于 tests/ 下，向上一级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 被验证的项目根 conftest
ROOT_CONFTEST = PROJECT_ROOT / "conftest.py"

# 与 pytest.ini 同形的 markers 段条目（钩子从 ini 条目里解析已注册标记名）
MARKER_INI_ENTRIES = [
  "smoke: 冒烟测试用例，核心链路验证",
  "regression: 回归测试用例，全量功能回归",
  "api: HTTP接口自动化测试",
  "slow: 耗时较长的用例",
]

# 嵌套运行探针目录（output/ 已被 .gitignore 忽略，也不在外层收集范围内）
PROBE_DIRNAME = "day55_hooks_probe"

# 嵌套 pytest 子进程超时（秒）: 只做收集不执行用例，预算留足冷启动，不赌时序
NESTED_PYTEST_TIMEOUT_SECONDS = 180.0

# 探针模块源码: 定义顺序刻意逆字典序（类内 test_b 在 test_a 之前、模块级函数
# 定义在类之后），用于区分"钩子排过序"与"本来就是收集顺序"
PROBE_MODULE_M_SOURCE = '''\
class TestZulu:
  def test_b(self):
    assert True

  def test_a(self):
    assert True


def test_module_level():
  assert True
'''

PROBE_MODULE_Z_SOURCE = '''\
def test_probe_z():
  assert True
'''


def _normalized(path: str | Path) -> str:
  """
  归一化路径用于比较（Windows 下大小写与分隔符差异不应造成假失败）

  参数:
    path (str | Path): 待归一化的路径

  返回:
    str: 绝对路径 + 平台大小写归一化后的字符串

  异常:
    无
  """
  return os.path.normcase(os.path.abspath(str(path)))


class _FakeMark:
  """最小标记替身（钩子只读它的 name 属性）"""

  def __init__(self, name: str) -> None:
    """
    参数:
      name (str): 标记名
    """
    self.name = name


class _FakeItem:
  """
  最小用例替身（鸭子类型）: 只实现钩子用到的四个属性面

  参数:
    module_name (str): 模块名（对应真实 Item 的 item.module.__name__）
    class_name (str | None): 类名；None 表示模块级用例（对应 item.cls）
    name (str): 用例名（对应 item.name）
    markers (tuple[str, ...]): 该用例携带的标记名
  """

  def __init__(
    self,
    module_name: str,
    class_name: str | None,
    name: str,
    markers: tuple[str, ...] = (),
  ) -> None:
    """
    参数:
      module_name (str): 模块名
      class_name (str | None): 类名；None 表示模块级用例
      name (str): 用例名
      markers (tuple[str, ...]): 该用例携带的标记名
    """
    self.module = SimpleNamespace(__name__=module_name)
    self.cls = SimpleNamespace(__name__=class_name) if class_name else None
    self.name = name
    self._marks = [_FakeMark(marker) for marker in markers]

  def iter_markers(self) -> Iterator[_FakeMark]:
    """
    返回本用例的标记迭代器（对齐真实 Item.iter_markers 的无参调用形态）

    参数:
      无

    返回:
      Iterator[_FakeMark]: 标记迭代器（每次调用都是新的迭代器）
    """
    return iter(self._marks)


class _FakeConfig:
  """
  最小配置替身: 提供钩子用到的 getini 与 stash 两个面

  stash 刻意用真实的 `pytest.Stash`（键以对象身份匹配），避免替身把
  "键写错位/读错位"这类问题一并掩盖掉。

  参数:
    markers (list[str]): markers 段的原始 ini 条目
  """

  def __init__(self, markers: list[str]) -> None:
    """
    参数:
      markers (list[str]): markers 段的原始 ini 条目
    """
    self.stash: pytest.Stash = pytest.Stash()
    self._markers = markers

  def getini(self, name: str) -> list[str]:
    """
    返回 markers 段的原始条目（与真实 config.getini 同形态）

    参数:
      name (str): ini 键名；替身只认 markers

    返回:
      list[str]: ini 条目副本
    """
    assert name == "markers", f"替身只支持 markers，收到: {name}"
    return list(self._markers)


class _BrokenIniConfig(_FakeConfig):
  """getini 直接抛错的配置替身（模拟 ini 不可读的极端情况）"""

  def getini(self, name: str) -> list[str]:
    """
    参数:
      name (str): ini 键名（替身直接抛错）

    返回:
      无（必然抛出）

    异常:
      RuntimeError: 恒抛出，用于验证钩子的降级路径
    """
    raise RuntimeError(f"ini 不可读: {name}")


def _snapshot(item: _FakeItem) -> tuple[str, str, str, tuple[str, ...]]:
  """
  记录替身用例的属性快照（用于断言钩子不改动 item 对象）

  参数:
    item (_FakeItem): 用例替身

  返回:
    tuple[str, str, str, tuple[str, ...]]: (模块名, 类名, 用例名, 标记名元组)

  异常:
    无
  """
  return (
    item.module.__name__,
    "" if item.cls is None else item.cls.__name__,
    item.name,
    tuple(mark.name for mark in item.iter_markers()),
  )


@pytest.fixture(scope="module")
def root_conftest() -> ModuleType:
  """
  按文件路径加载项目根 conftest（模块级复用，避免重复执行模块体）

  为什么不让 pytest 直接 import: 项目根 conftest 已被 pytest 作为插件加载、
  模块名固定为 "conftest"；按路径独立加载既干净，也保证取到的是磁盘当前版本
  （真实会话里那份由 TestCollectionHookWiring 从插件管理器取出比对）。

  参数:
    无

  返回:
    ModuleType: 项目根 conftest 模块对象
  """
  spec = importlib.util.spec_from_file_location("tm_day55_root_conftest", ROOT_CONFTEST)
  assert spec is not None and spec.loader is not None, "根 conftest 必须可加载"
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@pytest.fixture()
def fake_config() -> _FakeConfig:
  """
  最小配置替身（markers 段与项目 pytest.ini 同形）

  参数:
    无

  返回:
    _FakeConfig: 配置替身
  """
  return _FakeConfig(MARKER_INI_ENTRIES)


@pytest.fixture()
def unsorted_items() -> list[_FakeItem]:
  """
  刻意乱序的用例集（三级键各有一处逆序，并含一对三级键完全相同的用例）

  覆盖点: ①模块名排序 ②类名排序（含模块级用例的空串类名） ③用例名排序
      ④三级键相同时的稳定性（第 3、5 条键相同，必须保持输入先后）

  参数:
    无

  返回:
    list[_FakeItem]: 乱序用例集（钩子会就地排序，故每个用例独立取一份）
  """
  return [
    _FakeItem("tests.test_probe_b", None, "test_first", ("smoke",)),
    _FakeItem("tests.test_probe_a", "TestBeta", "test_second", ("api",)),
    _FakeItem("tests.test_probe_a", None, "test_zulu"),
    _FakeItem("tests.test_probe_a", "TestAlpha", "test_beta", ("smoke", "slow")),
    _FakeItem("tests.test_probe_a", None, "test_zulu", ("regression",)),
    _FakeItem("tests.test_probe_a", "TestAlpha", "test_alpha"),
  ]


@allure.feature("Day55 pytest钩子进阶")
@allure.story("pytest_collection_modifyitems")
class TestCollectionModifyItemsSorting:
  """排序能力: 模块 → 类 → 用例名三级字典序"""

  @allure.title("按模块→类→用例名三级字典序就地排序")
  def test_sorts_by_three_level_key(
    self, root_conftest: ModuleType, fake_config: _FakeConfig, unsorted_items: list[_FakeItem]
  ) -> None:
    """
    乱序输入经钩子处理后必须严格等于三级字典序；模块级用例（类名空串）
    必须排在同一模块的类成员之前（空串是三级键里的最小串）。
    """
    root_conftest.pytest_collection_modifyitems(fake_config, unsorted_items)

    keys = [root_conftest.collection_sort_key(item) for item in unsorted_items]
    assert keys == [
      ("tests.test_probe_a", "", "test_zulu"),
      ("tests.test_probe_a", "", "test_zulu"),
      ("tests.test_probe_a", "TestAlpha", "test_alpha"),
      ("tests.test_probe_a", "TestAlpha", "test_beta"),
      ("tests.test_probe_a", "TestBeta", "test_second"),
      ("tests.test_probe_b", "", "test_first"),
    ]

  @allure.title("三级键相同的用例保持收集时的相对顺序（稳定排序）")
  def test_sort_is_stable_for_equal_keys(
    self, root_conftest: ModuleType, fake_config: _FakeConfig, unsorted_items: list[_FakeItem]
  ) -> None:
    """
    先取的替身与后取的替身三级键完全一致（同模块、同为空类名、同用例名），
    排序后前者必须仍在前——这正是 `list.sort` 稳定性的行为承诺，
    也是同一用例多组参数保持定义顺序的前提。
    """
    first_duplicate = unsorted_items[2]
    second_duplicate = unsorted_items[4]
    first_key = root_conftest.collection_sort_key(first_duplicate)
    assert first_key == root_conftest.collection_sort_key(second_duplicate)

    root_conftest.pytest_collection_modifyitems(fake_config, unsorted_items)

    assert unsorted_items.index(first_duplicate) < unsorted_items.index(
      second_duplicate
    )


@allure.feature("Day55 pytest钩子进阶")
@allure.story("pytest_collection_modifyitems")
class TestCollectionModifyItemsStats:
  """统计能力: marker 分组 / 模块分组 / 总数 / 标记完整性"""

  @allure.title("统计结果与传入用例集逐项一致")
  def test_stats_match_collected_items(
    self, root_conftest: ModuleType, fake_config: _FakeConfig, unsorted_items: list[_FakeItem]
  ) -> None:
    """
    marker 分组计数按标记出现次数累加（同一条用例带 smoke+slow 则各计一次）、
    模块分组按模块名计数、总数等于用例条数；未注册标记在正常输入下恒为空。
    """
    root_conftest.pytest_collection_modifyitems(fake_config, unsorted_items)
    stats = fake_config.stash[root_conftest.COLLECTION_STATS_STASH_KEY]

    assert stats.total == len(unsorted_items)
    assert stats.marker_counts == {
      "smoke": 2,
      "api": 1,
      "slow": 1,
      "regression": 1,
    }
    assert stats.module_counts == {"tests.test_probe_a": 5, "tests.test_probe_b": 1}
    assert stats.unregistered_marker_counts == {}

  @allure.title("未注册自定义标记被登记，pytest 内建标记不误判")
  def test_unregistered_markers_are_recorded(
    self, root_conftest: ModuleType, fake_config: _FakeConfig
  ) -> None:
    """
    未注册标记（如 e2e）在 `--strict-markers` 下本就会在收集期报错，
    钩子额外把它登记进"未注册标记"分组，供排查与统计使用；而 pytest
    内建标记（parametrize / skip / xfail 等）属于框架注入，不得被判成未注册。
    """
    items = [
      _FakeItem("tests.test_probe_a", None, "test_unknown", ("e2e", "smoke")),
      _FakeItem(
        "tests.test_probe_a",
        None,
        "test_builtin",
        ("parametrize", "skip", "xfail", "usefixtures"),
      ),
    ]

    root_conftest.pytest_collection_modifyitems(fake_config, items)

    stats = fake_config.stash[root_conftest.COLLECTION_STATS_STASH_KEY]
    # 内建标记按"已注册"正常计入使用情况（只是不进业务标记的摘要位）
    assert stats.marker_counts == {
      "smoke": 1,
      "parametrize": 1,
      "skip": 1,
      "xfail": 1,
      "usefixtures": 1,
    }
    assert stats.unregistered_marker_counts == {"e2e": 1}

  @allure.title("空用例集与 ini 读取异常都不影响钩子（降级路径）")
  def test_degenerate_inputs_keep_hook_working(
    self, root_conftest: ModuleType, fake_config: _FakeConfig, unsorted_items: list[_FakeItem]
  ) -> None:
    """
    两处退化输入: ①空用例集 → 零统计且不报错；②`getini` 抛错 →
    注册表退化为内建标记集合，钩子仍完成排序与统计（四个业务标记
    因此全部落入"未注册"分支被登记），绝不因 ini 不可读而炸整轮收集。
    """
    empty_items: list[_FakeItem] = []
    root_conftest.pytest_collection_modifyitems(fake_config, empty_items)
    empty_stats = fake_config.stash[root_conftest.COLLECTION_STATS_STASH_KEY]
    assert empty_items == []
    assert empty_stats.total == 0
    assert empty_stats.module_counts == {}

    broken_config = _BrokenIniConfig(MARKER_INI_ENTRIES)
    root_conftest.pytest_collection_modifyitems(broken_config, unsorted_items)
    broken_stats = broken_config.stash[root_conftest.COLLECTION_STATS_STASH_KEY]
    assert broken_stats.total == len(unsorted_items)
    keys = [root_conftest.collection_sort_key(item) for item in unsorted_items]
    assert keys == sorted(keys)
    assert set(broken_stats.unregistered_marker_counts) == {
      "smoke",
      "regression",
      "api",
      "slow",
    }


@allure.feature("Day55 pytest钩子进阶")
@allure.story("pytest_collection_modifyitems")
class TestCollectionModifyItemsNonDestructive:
  """不破坏性: 排序不得增删用例，也不得改动 item 对象自身"""

  @allure.title("排序后用例数量与 item 对象内容均不变")
  def test_count_and_item_objects_are_unchanged(
    self, root_conftest: ModuleType, fake_config: _FakeConfig, unsorted_items: list[_FakeItem]
  ) -> None:
    """
    钩子只允许改变顺序: 用例条数不变、集合里还是同一批对象（按身份比对）、
    每个对象的 (模块名, 类名, 用例名, 标记) 快照逐项一致。
    """
    before_snapshots = {id(item): _snapshot(item) for item in unsorted_items}

    root_conftest.pytest_collection_modifyitems(fake_config, unsorted_items)

    assert len(unsorted_items) == len(before_snapshots)
    assert {id(item) for item in unsorted_items} == set(before_snapshots)
    assert [_snapshot(item) for item in unsorted_items] == [
      before_snapshots[id(item)] for item in unsorted_items
    ]


@allure.feature("Day55 pytest钩子进阶")
@allure.story("收集摘要输出")
class TestCollectionSummaryLine:
  """摘要行: 固定四标记位 + 模块数，未注册标记按需追加"""

  @allure.title("摘要行格式固定为四标记位+模块数，未注册标记追加在末尾")
  def test_summary_line_format(
    self, root_conftest: ModuleType, fake_config: _FakeConfig, unsorted_items: list[_FakeItem]
  ) -> None:
    """
    常规输入下摘要行必须是
    `[TestMatrix] 收集 N 用例：smoke=a / regression=b / api=c / slow=d，模块=M`
    （未命中的标记也输出 0，便于横向对比）；出现未注册标记时按
    "名称×次数"追加、名称排序稳定。
    """
    root_conftest.pytest_collection_modifyitems(fake_config, unsorted_items)
    stats = fake_config.stash[root_conftest.COLLECTION_STATS_STASH_KEY]
    assert root_conftest.format_collection_summary(stats) == (
      "[TestMatrix] 收集 6 用例：smoke=2 / regression=1 / api=1 / slow=1，模块=2"
    )

    dirty_config = _FakeConfig(MARKER_INI_ENTRIES)
    root_conftest.pytest_collection_modifyitems(
      dirty_config,
      [_FakeItem("tests.test_probe_a", None, "test_unknown", ("e2e", "smoke"))],
    )
    dirty_stats = dirty_config.stash[root_conftest.COLLECTION_STATS_STASH_KEY]
    assert root_conftest.format_collection_summary(dirty_stats) == (
      "[TestMatrix] 收集 1 用例：smoke=1 / regression=0 / api=0 / slow=0，"
      "模块=1，未注册标记=e2e×1"
    )


@allure.feature("Day55 pytest钩子进阶")
@allure.story("真实会话接线")
class TestCollectionHookWiring:
  """真实会话: 钩子确实注册、统计确实写进了当前会话的 stash"""

  @allure.title("根 conftest 的收集钩子与收尾钩子已注册进当前会话")
  def test_root_conftest_hooks_registered(self, pytestconfig: pytest.Config) -> None:
    """
    逐钩子核对注册面: 当前会话的钩子实现里必须有一条来自项目根 conftest
    （按实现函数的源文件路径比对，而不是按模块名猜）。
    """
    hook_names = ("pytest_collection_modifyitems", "pytest_sessionfinish")
    for hook_name in hook_names:
      implementations = getattr(
        pytestconfig.pluginmanager.hook, hook_name
      ).get_hookimpls()
      source_files = {
        _normalized(impl.function.__code__.co_filename)
        for impl in implementations
      }
      assert _normalized(ROOT_CONFTEST) in source_files, f"{hook_name} 未注册"

  @allure.title("当前会话的 stash 持有本次收集的统计")
  def test_live_session_stash_holds_collection_stats(
    self, pytestconfig: pytest.Config
  ) -> None:
    """
    从插件管理器取出**真实加载**的项目根 conftest 模块，用它的 StashKey
    去读当前会话的 stash——才能保证读到的是本次收集写下的那一份
    （自行 importlib 复制的模块会带来另一个键对象，那样读永远是 None，
    断言会退化成永真）。统计里必须能查到本测试文件所在模块。
    """
    plugin_module = next(
      (
        plugin
        for plugin in pytestconfig.pluginmanager.get_plugins()
        if getattr(plugin, "__file__", None) is not None
        and _normalized(plugin.__file__) == _normalized(ROOT_CONFTEST)
      ),
      None,
    )
    assert plugin_module is not None, "项目根 conftest 不是当前会话的插件"

    stats = pytestconfig.stash.get(plugin_module.COLLECTION_STATS_STASH_KEY, None)
    assert stats is not None, "收集期钩子没有把统计写进当前会话"
    assert stats.total > 0
    assert __name__ in stats.module_counts, stats.module_counts


@allure.feature("Day55 pytest钩子进阶")
@allure.story("收集期钩子端到端")
class TestNestedCollectionEndToEnd:
  """集成: 真起一个嵌套 pytest，验证真实收集的排序效果与摘要输出"""

  @allure.title("嵌套 pytest 的收集顺序已排好序且输出了收集摘要")
  def test_nested_collect_only_is_sorted_with_summary(self) -> None:
    """
    探针设计（顺序判据必须能区分"排过序"与"本来就这个顺序"）:
      - 同模块内: 类成员按定义顺序是 test_b → test_a（逆字典序），
       模块级函数定义在类之后；排序后必须变成 模块级 → test_a → test_b
      - 跨模块: 子进程**故意把 test_probe_z 放在 test_probe_m 之前**传参，
       排序后必须变成 test_probe_m 的两个 nodeid 在前
    同时断言摘要行出现: 证明 `pytest_collection_modifyitems` 与
    `pytest_sessionfinish` 两个钩子在真实会话里都真的生效了。

    取 `-qq` 而非 `-q`: 本环境（pytest 7.4.4）下 `--collect-only -q` 打印的
    是节点树，只有 `-qq` 才是"一行一个 nodeid"，后者可逐行精确断言顺序。
    子进程额外注入 PYTHONIOENCODING=utf-8: 管道默认走本机 ANSI 代码页，
    摘要在子进程侧就会编成乱码（摘要行含中文，无法按原文断言）。
    """
    probe_dir = PROJECT_ROOT / "output" / PROBE_DIRNAME
    probe_dir.mkdir(parents=True, exist_ok=True)
    (probe_dir / "test_probe_m.py").write_text(
      PROBE_MODULE_M_SOURCE, encoding="utf-8"
    )
    (probe_dir / "test_probe_z.py").write_text(
      PROBE_MODULE_Z_SOURCE, encoding="utf-8"
    )
    try:
      completed = subprocess.run(
        [
          sys.executable,
          "-m",
          "pytest",
          "--collect-only",
          "-qq",
          f"output/{PROBE_DIRNAME}/test_probe_z.py",
          f"output/{PROBE_DIRNAME}/test_probe_m.py",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        # 刻意不解码: Windows 控制台编码与项目路径中的非 ASCII 字符
        # 组合会让 text=True 的解码本身抛错，掩盖真正的失败原因
        check=False,
        timeout=NESTED_PYTEST_TIMEOUT_SECONDS,
      )
      stdout = completed.stdout.decode("utf-8", errors="replace")
      assert completed.returncode == 0, (
        f"嵌套 pytest 退出码 {completed.returncode}\n{stdout}"
      )

      node_ids = [line.strip() for line in stdout.splitlines() if "::" in line]
      assert node_ids == [
        f"output/{PROBE_DIRNAME}/test_probe_m.py::test_module_level",
        f"output/{PROBE_DIRNAME}/test_probe_m.py::TestZulu::test_a",
        f"output/{PROBE_DIRNAME}/test_probe_m.py::TestZulu::test_b",
        f"output/{PROBE_DIRNAME}/test_probe_z.py::test_probe_z",
      ], stdout
      assert (
        "[TestMatrix] 收集 4 用例：smoke=0 / regression=0 / api=0 / slow=0，"
        "模块=2" in stdout
      ), stdout
    finally:
      shutil.rmtree(probe_dir, ignore_errors=True)


@allure.feature("Day58-fix 收集摘要xdist守卫")
@allure.story("收集摘要输出")
@allure.title("worker 进程不输出收集摘要，master/串行仍照常输出（阳性对照）")
def test_sessionfinish_skips_collection_summary_in_xdist_worker(
  root_conftest: ModuleType, fake_config: _FakeConfig, unsorted_items: list[_FakeItem]
) -> None:
  """
  为什么需要这条守卫（Day58 实测口径）: `-n` 模式下 xdist 的 master 不做收集
  （其在 `pytest_collection` 里直接 return True），master 的 stash 里没有统计、
  走到 `stats is None` 就返回；worker 虽有统计，但它的 `write_line` 不会被转发到
  master 终端。两种情况的结果都是摘要消失，但成因不同——本用例把"worker 侧"
  这一半用**显式守卫**钉死，避免日后有人误以为它是"恰好读不到"而把统计补回去。

  阳性对照不可省: 同一份 stash、同一个 session 替身，只把 `workerinput` 拿掉，
  摘要就必须照常输出——否则"没输出"完全可能只是因为终端替身接错了，断言会退化成永真。
  """
  root_conftest.pytest_collection_modifyitems(fake_config, unsorted_items)
  stats = fake_config.stash[root_conftest.COLLECTION_STATS_STASH_KEY]
  expected_line = root_conftest.format_collection_summary(stats)

  def _session_with(**extra: Any) -> tuple[Any, list[str]]:
    """构造 session 替身（config 只带 stash 与 terminalreporter 两个取用面）"""
    emitted: list[str] = []
    config = SimpleNamespace(
      stash=fake_config.stash,
      pluginmanager=SimpleNamespace(
        get_plugin=lambda name: SimpleNamespace(write_line=emitted.append)
      ),
      **extra,
    )
    return SimpleNamespace(config=config), emitted

  # 阳性对照: 串行/master 形态（无 workerinput）必须照常输出一行摘要
  serial_session, serial_lines = _session_with()
  root_conftest.pytest_sessionfinish(serial_session, 0)
  assert serial_lines == [expected_line]

  # worker 形态（xdist 只给 worker 注入 workerinput）: 一行都不许写
  worker_session, worker_lines = _session_with(workerinput={"workerid": "gw0"})
  root_conftest.pytest_sessionfinish(worker_session, 0)
  assert worker_lines == []

  # 兜底: config 缺失时同样安全返回（不抛异常、不写终端）
  root_conftest.pytest_sessionfinish(SimpleNamespace(), 0)
