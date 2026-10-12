"""
per-run 唯一 Allure 结果目录的隔离验证（Day53 任务五）

被验证对象: 项目根 `conftest.py`（不是 tests/conftest.py）
 ——嵌套 pytest（真实执行器拉起、收集 output/ 下的探针文件）只有加载
  项目根 conftest 才会套用同一套隔离规则，tests/conftest.py 不是它的祖先。

覆盖场景:
  1. 纯函数: per-run 目录唯一性、已隔离目录不二次嵌套、清理保留策略
  2. 真实会话配置: 外层会话生效的 alluredir 必须是 per-run 独立目录，
    且破坏性清理（--clean-alluredir）被关闭
  3. 集成: 真起一个嵌套 pytest（收集 output/ 下的探针文件），断言它
    写入自己的独立目录、**不清理也不占用**外层会话的共享目录

为什么必须验证"不清理外层目录": 修复前外层与嵌套进程都会对同一个共享
目录做 `--clean-alluredir` 的"先删后建"，Windows 上实测会抛
WinError 145 / FileNotFoundError 并升级为 INTERNALERROR（整轮退出码 3），
嵌套运行被腰斩后退出码 3 被映射成 error——即 KNOWN_LIMITATIONS 第 6 条
记录的偶发 error vs failed 根因。
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import allure
import pytest

# 项目根（本文件位于 tests/ 下，向上一级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 被验证的项目根 conftest
ROOT_CONFTEST = PROJECT_ROOT / "conftest.py"

# 嵌套运行的探针目录（output/ 已被 .gitignore 忽略，且不在外层收集范围内）
PROBE_DIRNAME = "day53_nested_probe"

# 嵌套 pytest 的子进程超时（秒）: 只跑一个 assert True 的小文件，
# 预算留足以覆盖冷启动，不做赌时序的固定等待
NESTED_PYTEST_TIMEOUT_SECONDS = 180.0

# 外层共享目录里的标记文件名（用于证明"外层目录内容没被嵌套运行清掉"）
OUTER_MARKER_NAME = "OUTER_SESSION_MARKER.txt"


def _load_root_conftest() -> ModuleType:
  """
  以独立模块名加载项目根 conftest（不污染 pytest 自身的 conftest 插件）

  为什么不让 pytest 直接 import: 项目根 conftest 已被 pytest 作为插件
  加载，模块名固定为 "conftest"；再用同名导入会拿到 pytest 那份（且
  语义耦合）。按文件路径独立加载既干净，也保证拿到的是**磁盘当前版本**。

  参数:
    无

  返回:
    ModuleType: 项目根 conftest 模块对象

  异常:
    FileNotFoundError: conftest.py 不存在时由加载器抛出
  """
  spec = importlib.util.spec_from_file_location("tm_day53_root_conftest", ROOT_CONFTEST)
  assert spec is not None and spec.loader is not None, "根 conftest 必须可加载"
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _shared_allure_dir(pytestconfig: pytest.Config) -> Path:
  """
  推导外层会话的**共享** Allure 目录（per-run 目录的父目录）

  隔离生效时 `--alluredir` 已被改写成 `<共享目录>/run-<...>`，故父目录
  即"修复前会被两个进程抢的那个目录"。先断言隔离确实生效，避免在
  修复未生效时拿到一个错误的父目录、把用例变成永真断言。

  参数:
    pytestconfig (pytest.Config): 当前会话配置

  返回:
    Path: 共享 Allure 目录（如 output/allure_results）

  异常:
    AssertionError: alluredir 未被改写为 per-run 目录时失败
  """
  effective = Path(str(pytestconfig.getoption("--alluredir")))
  assert effective.name.startswith("run-"), (
    f"外层会话的 alluredir 未被改写为 per-run 目录: {effective}"
  )
  return effective.parent


@allure.feature("Day53 全链路联调")
@allure.story("per-run唯一Allure目录")
class TestPerRunAllureDirResolution:
  """纯函数行为：唯一性 / 不二次嵌套 / 清理保留策略"""

  @allure.title("同一基目录下的 per-run 目录逐次唯一")
  def test_resolve_per_run_dir_is_unique(self, tmp_path: Path) -> None:
    """
    用显式 stamp/pid 注入造两次"同一微秒节拍"的解析，断言目录名不同
    （时间戳+pid 双要素的唯一性），且都落在同一父目录下（不改写根位置）。
    """
    module = _load_root_conftest()
    base = tmp_path / "allure_results"
    first = module.resolve_per_run_allure_dir(base, stamp="100", pid=1)
    second = module.resolve_per_run_allure_dir(base, stamp="100", pid=2)
    third = module.resolve_per_run_allure_dir(base, stamp="101", pid=1)

    assert first != second != third != first
    assert {path.parent for path in (first, second, third)} == {base}
    assert all(
      path.name.startswith(module.PER_RUN_ALLURE_DIR_PREFIX)
      for path in (first, second, third)
    )

  @allure.title("已自带隔离的目录不再二次嵌套")
  def test_isolated_prefix_detection(self, tmp_path: Path) -> None:
    """
    真实执行器为每条用例分配 `allure_results_<用例>_<时间戳>_...` 已是
    per-run 唯一目录；本机制必须识别它并**跳过**，否则会再嵌一层，
    让 PytestRunner 按原路径解析不到结果文件、桥接入库静默失效。
    """
    module = _load_root_conftest()
    assert module.is_isolated_allure_dir("output/allure_results_ab_1_2_3")
    assert module.is_isolated_allure_dir(str(tmp_path / "run-100-1"))
    assert not module.is_isolated_allure_dir("output/allure_results")
    assert not module.is_isolated_allure_dir(str(tmp_path / "other"))

  @allure.title("清理保留最近N个且绝不删当前目录/新目录")
  def test_prune_keeps_recent_and_skips_current(self, tmp_path: Path) -> None:
    """
    清理策略三要素：①当前目录强制保留；②名称倒序前 N 个保留（名称里
    带微秒时间戳，名称序即创建序）；③存活时长不足阈值的保留
    （防误删并发运行中的目录）。

    目录名倒序排列: run-999999-2(当前) > run-000004-1 > run-000003-1
    > run-000002-1 > run-000001-1 > run-000000-1。
    keep=2 时前两个位置（当前 + run-000004-1）受保护；其余四个进入
    清理判定——其中 run-000001-1 被本条用例刻意置为"新鲜"（mtime=now），
    必须因存活时长不足而保留。故最终删除 3 个、保留 3 个。
    """
    module = _load_root_conftest()
    base = tmp_path / "allure_results"
    base.mkdir()
    old_time = time.time() - module.PER_RUN_ALLURE_DIR_MIN_AGE_SECONDS - 300
    for index in range(5):
      directory = base / f"run-{index:06d}-1"
      directory.mkdir()
      os.utime(directory, (old_time, old_time))
    # 新鲜目录: 在待删区间内，但存活时长不足阈值 → 必须保留
    fresh = base / "run-000001-1"
    os.utime(fresh, (time.time(), time.time()))
    current = base / "run-999999-2"
    current.mkdir()

    removed = module.prune_old_per_run_dirs(base, current, keep=2)

    remaining = {path.name for path in base.iterdir()}
    assert set(removed) == {
      "run-000003-1",
      "run-000002-1",
      "run-000000-1",
    }, removed
    assert remaining == {"run-999999-2", "run-000004-1", "run-000001-1"}, (
      remaining
    )


@allure.feature("Day53 全链路联调")
@allure.story("per-run唯一Allure目录")
class TestOuterSessionAllureDir:
  """外层会话自身的生效配置（确定性断言，跑在真实会话里）"""

  @allure.title("外层会话的 alluredir 是 per-run 独立目录")
  def test_outer_session_uses_per_run_dir(self, pytestconfig: pytest.Config) -> None:
    """
    外层会话生效的 alluredir 必须已被改写成
    `<pytest.ini 里的共享目录>/run-<...>`：该目录必然不存在，
    `AllureFileLogger` 的 `is_dir() and clean` 判定为假 → 从根本上
    不可能触发删除动作（也就没有 WinError 145 这一类失败）。
    """
    effective = Path(str(pytestconfig.getoption("--alluredir")))
    assert effective.name.startswith("run-"), effective
    # 父目录必须仍是 pytest.ini addopts 里写死的那个共享目录名
    # （本修复只往共享目录下挂一层 per-run 子目录，不搬走结果目录）
    assert effective.parent.name == "allure_results", effective
    # 本用例的父目录一定先于自己存在（外侧是本轮/历史运行建的）
    assert effective.parent.is_dir(), effective.parent

  @allure.title("--clean-alluredir 已被关闭（不再有破坏性清理）")
  def test_clean_alluredir_is_disabled(self, pytestconfig: pytest.Config) -> None:
    """
    第二道闸：即使将来 alluredir 指向一个已存在的目录，也不得再有
    "先删后建"。pytest.ini 里的配置文案不改，只在运行时关闭生效值。
    """
    assert pytestconfig.getoption("clean_alluredir") is False


@allure.feature("Day53 全链路联调")
@allure.story("per-run唯一Allure目录")
class TestNestedPytestIsolation:
  """集成：真起一个嵌套 pytest，验证不清理/不占用外层共享目录"""

  @allure.title("嵌套 pytest 写自己的独立目录且不动外层目录")
  def test_nested_pytest_does_not_touch_outer_dir(
    self, pytestconfig: pytest.Config
  ) -> None:
    """
    真起子进程跑一个收集 output/ 下探针文件的嵌套 pytest，断言:
      ①子进程退出码 0（不再出现 INTERNALERROR / 退出码 3）；
      ②外层共享目录里的标记文件仍在（目录没被清掉）；
      ③共享目录里多出一个属于嵌套运行的 run-* 独立目录。

    为什么探针放在 output/ 下: 只有"能被外层 pytest.ini 的 addopts
    命中（rootdir=项目根）"的运行才是修复前的受害者；放在 tmp_path 下
    会因找不到 pytest.ini 而绕过冲突，测不出真问题。
    """
    shared_dir = _shared_allure_dir(pytestconfig)
    probe_dir = PROJECT_ROOT / "output" / PROBE_DIRNAME
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe_file = probe_dir / "test_day53_probe.py"
    probe_file.write_text(
      "def test_probe():\n  assert True\n", encoding="utf-8"
    )
    marker = shared_dir / OUTER_MARKER_NAME
    marker.write_text("outer session marker\n", encoding="utf-8")
    try:
      before = {path.name for path in shared_dir.glob("run-*")}
      completed = subprocess.run(
        [
          sys.executable,
          "-m",
          "pytest",
          "-q",
          f"output/{PROBE_DIRNAME}/test_day53_probe.py",
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        # 刻意不解码: Windows 控制台编码与项目路径中的非 ASCII 字符
        # 组合会让 text=True 的解码本身抛错，掩盖真正的失败原因
        check=False,
        timeout=NESTED_PYTEST_TIMEOUT_SECONDS,
      )
      stdout = completed.stdout.decode("utf-8", errors="replace")
      assert completed.returncode == 0, (
        f"嵌套 pytest 退出码 {completed.returncode}\n{stdout}"
      )
      assert marker.exists(), "外层共享目录的内容被嵌套运行清掉了"
      after = {path.name for path in shared_dir.glob("run-*")}
      new_dirs = after - before
      assert new_dirs, "嵌套运行没有产出自己的独立目录"
      assert all(name.startswith("run-") for name in new_dirs)
    finally:
      shutil.rmtree(probe_dir, ignore_errors=True)
      marker.unlink(missing_ok=True)
