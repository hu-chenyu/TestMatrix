"""
项目根 pytest 配置（Day53 新增）：per-run 唯一 Allure 结果目录

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
from pathlib import Path
from typing import Any

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
    pytest 会话配置钩子：应用 per-run 唯一 Allure 结果目录

    conftest 的钩子先于插件（allure-pytest）的 pytest_configure 执行，
    故此处改写后的值会被 `AllureFileLogger` 直接读到——这是本修复能生效的
    关键时序，改动本函数位置或改由插件侧钩子实现都会让修复静默失效。

    参数:
        config (Any): pytest 的 Config 对象（框架传入）

    返回:
        无
    """
    apply_per_run_allure_dir(config)
