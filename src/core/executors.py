"""
用例执行器抽象层（策略模式 + 依赖倒置）

功能（第三阶段Day24交付）:
    - ExecutionResult          单用例执行结果数据类
      （result/error_message/duration三字段）
    - BaseExecutor             执行器抽象基类（统一执行契约）
    - SimulatedExecutor        模拟执行器（0.01s耗时 + case_id末位
                              奇偶定通过/失败，与既有_simulate_execute
                              规则完全一致）
    - PytestRunner             真实pytest执行器（subprocess 同步执行 +
                              可配置超时/cwd/env + 输出捕获 + 退出码映射）
    - get_executor             执行器工厂（读TM_EXECUTOR环境变量，
                              默认simulated）

功能（真实执行器Phase-1 Day50交付）:
    - 每次执行写入**独立** Allure 结果目录（output/allure_results_*），
      并在 pytest 命令行显式覆盖 pytest.ini addopts 里的默认 alluredir
    - pytest 终端输出经 output_parser 解析为结构化 ParsedResult，
      随 ExecutionResult 一并返回（原始 output 文本保留不变）
    - 执行完成后自动调用 report_analyzer.bridge_results_dir 解析本次独立
      目录并入库 defect_statistics

设计说明:
    - 策略模式: 编排代码（CaseManager._execute_batch_async）只依赖
      BaseExecutor抽象契约，不感知具体执行器实现
    - 依赖倒置: 高层编排模块不直接依赖低层执行细节，二者都依赖
      抽象接口；后续Day32换Redis任务队列、Day52-79接入真实pytest
      执行器时，编排代码零改动
    - 不引入新第三方依赖: subprocess为标准库
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from itertools import count
from pathlib import Path

from src.common.env_manager import PROJECT_ROOT, env_manager
from src.common.logger import LogManager
from src.common.source_ref import validate_source_ref
from src.core.output_parser import ParsedResult, PytestOutputParser

logger = LogManager.get_logger()

# 执行器类型合法值（工厂与Web入参校验共用，单一事实来源）
VALID_EXECUTORS = ("simulated", "pytest")

# pytest子进程执行超时时间（秒）：PytestRunner.timeout 未显式传参、
# TM_PYTEST_TIMEOUT 未设置时的兜底默认值
PYTEST_TIMEOUT_SECONDS = 30

# 子进程超时阈值的环境变量名（运维可在不改代码的情况下放宽/收紧单条用例预算）
TM_PYTEST_TIMEOUT_KEY = "TM_PYTEST_TIMEOUT"

# 子进程输出截断长度（防超长堆栈撑爆error_message与库表）
OUTPUT_TRUNCATE_LENGTH = 2000

# 子进程输出解码策略：pytest 输出含中文用例名/路径，Windows 默认 cp936
# 遇 UTF-8 字节会抛 UnicodeDecodeError（而 run_one 只捕 TimeoutExpired/
# OSError，异常会穿透到批次级 except 导致整批 failed）。errors="replace"
# 把无法解码的字节替换为 � 而非抛异常，保证单条用例的编码问题不会
# 升级成整批失败。
SUBPROCESS_ENCODING = "utf-8"
SUBPROCESS_ERRORS = "replace"

# ---------------------------------------------------------------------------
# pytest 标准退出码（Day49）
# ---------------------------------------------------------------------------
# 取自 pytest 官方 "Exit codes" 约定，是解释器与测试运行工具之间的稳定契约：
#   0 OK            全部通过
#   1 Tests failed  收集成功但存在失败用例
#   2 Interrupted  收集/执行阶段被中断（Ctrl-C、conftest 抛 KeyboardInterrupt）
#   3 Internal error pytest 自身缺陷或插件抛了未捕获异常
#   4 Usage error   命令行/环境用法错误（如路径不存在、选项非法）
#   5 No tests collected 选择器过滤后一条都没收集到
EXIT_CODE_PASSED = 0
EXIT_CODE_TESTS_FAILED = 1
EXIT_CODE_INTERRUPTED = 2
EXIT_CODE_INTERNAL_ERROR = 3
EXIT_CODE_USAGE_ERROR = 4
EXIT_CODE_NO_TESTS_COLLECTED = 5

# ---------------------------------------------------------------------------
# 退出码细分分类（ExecutionResult.exit_reason 的取值，单一事实来源）
# ---------------------------------------------------------------------------
# 为什么细分分类单独占一个字段、而不直接做成 ExecutionResult.result 的取值
# （Day49 决策，见 PROJECT_CONTEXT 6.x）:
#     编排层 case_manager.record_execution 只认 VALID_RESULTS 四值
#     （passed/failed/error/skipped），落库前逐条校验，不在集合内直接抛
#     CaseManagerError；而该异常被 _execute_batch_async 的批次级 except 吞掉
#     → **单条用例的退出码问题会把整批判 failed**，恰好违反本项目最核心的
#     编排契约。且 _aggregate_execution_results 只按四个值分桶，未知取值
#     既不进任何桶、又被算进 total，pass_rate 静默失真。
#   故 result 保持下游四值词表，细分语义由 exit_code（原始退出码）+ exit_reason
#   （细分分类）承载，两字段均带默认值、不改数据库 schema、不动 case_manager。
EXIT_REASON_PASSED = "passed"
EXIT_REASON_TESTS_FAILED = "tests_failed"
EXIT_REASON_INTERRUPTED = "interrupted"
EXIT_REASON_INTERNAL_ERROR = "internal_error"
EXIT_REASON_USAGE_ERROR = "usage_error"
EXIT_REASON_NO_TESTS_COLLECTED = "no_tests_collected"
EXIT_REASON_UNKNOWN_EXIT_CODE = "unknown_exit_code"
# 以下四类不对应任何退出码：进程根本没正常跑完，用退出码无从表达
EXIT_REASON_TIMEOUT = "timeout"
EXIT_REASON_SPAWN_FAILED = "spawn_failed"
EXIT_REASON_INVALID_ARGUMENTS = "invalid_arguments"
EXIT_REASON_INVALID_CASE = "invalid_case"

# 退出码 → 细分分类。未收录的退出码回落为 unknown_exit_code
EXIT_CODE_REASON_MAP: dict[int, str] = {
    EXIT_CODE_PASSED: EXIT_REASON_PASSED,
    EXIT_CODE_TESTS_FAILED: EXIT_REASON_TESTS_FAILED,
    EXIT_CODE_INTERRUPTED: EXIT_REASON_INTERRUPTED,
    EXIT_CODE_INTERNAL_ERROR: EXIT_REASON_INTERNAL_ERROR,
    EXIT_CODE_USAGE_ERROR: EXIT_REASON_USAGE_ERROR,
    EXIT_CODE_NO_TESTS_COLLECTED: EXIT_REASON_NO_TESTS_COLLECTED,
}

# 退出码 → 下游四值词表。只有 0/1 是"跑完了且有业务结论"，其余都是"这次执行
# 本身没跑成/没测到东西"，一律 error——与 Day48 的既有语义一致，不改口径。
EXIT_CODE_RESULT_MAP: dict[int, str] = {
    EXIT_CODE_PASSED: "passed",
    EXIT_CODE_TESTS_FAILED: "failed",
    EXIT_CODE_INTERRUPTED: "error",
    EXIT_CODE_INTERNAL_ERROR: "error",
    EXIT_CODE_USAGE_ERROR: "error",
    EXIT_CODE_NO_TESTS_COLLECTED: "error",
}

# 各退出码对应的中文处置建议，出现在 error_message 前缀里，让运维一眼看出
# 该改数据、改选择器还是改环境（Day49 任务要求"更新 docstring 说明各退出码
# 含义与处置建议"，落到消息里比只写在 docstring 更难被漏看）
EXIT_CODE_ADVICE_MAP: dict[int, str] = {
    EXIT_CODE_INTERRUPTED: "执行被中断（多为 Ctrl-C 或 conftest/conftest 插件抛 KeyboardInterrupt）",
    EXIT_CODE_INTERNAL_ERROR: "pytest 自身内部错误（插件缺陷或未捕获异常），需看 traceback 定位",
    EXIT_CODE_USAGE_ERROR: "命令行用法错误（路径不存在/选项非法），核对 source_ref 与 -m/-k 参数",
    EXIT_CODE_NO_TESTS_COLLECTED: "选择器过滤后未收集到任何用例，核对 -m/-k 与执行目标路径",
}

# ---------------------------------------------------------------------------
# 进程树清理（Day49）
# ---------------------------------------------------------------------------
# taskkill / killpg 之后等待直接子进程真正退出的宽限期（秒）。
# 树清理是强杀（/F、SIGKILL），进程通常毫秒级退出；留 5 秒仅兜住系统繁忙场景。
PROCESS_TREE_KILL_GRACE_SECONDS = 5.0

# POSIX 侧先发 SIGTERM 温和终止、再轮询等待的时长（秒）。给被测代码一次
# 自己收尾（释放端口、写临时文件）的机会，避免一律强杀造成数据损坏假象。
PROCESS_TREE_TERM_GRACE_SECONDS = 2.0

# taskkill 自身也可能挂起（系统繁忙/权限），给它独立的短预算
TASKKILL_TIMEOUT_SECONDS = 10.0

# POSIX 轮询间隔（秒）。仅用于"等进程退出"的轮询循环，不是赌时序的固定等待
PROCESS_TREE_POLL_INTERVAL_SECONDS = 0.05

# POSIX 进程组强杀原语（os.killpg）在 Windows 上**不存在**，直接写
# `os.killpg(...)` 会让 mypy 在 Windows 主机上报 attr-defined 错误。
# 故在模块导入期解析一次：存在则绑定、不存在置 None（由调用方按"非 POSIX
# 平台"优雅降级）。单测用 monkeypatch 替换本模块级名字即可注入替身，
# 使这段 POSIX 逻辑在 Windows 开发机上也能被真实执行与覆盖。
_POSIX_KILLPG: Callable[[int, int], None] | None = getattr(os, "killpg", None)

# SIGKILL 同理只在 POSIX 存在；Windows 上退回 SIGTERM（仅用于兜底返回，
# 真正执行强杀的路径不会在 Windows 上走到）
_SIGKILL: int = getattr(signal, "SIGKILL", signal.SIGTERM)

# ---------------------------------------------------------------------------
# pytest 选择器透传（Day49 任务二）
# ---------------------------------------------------------------------------
# 用例 dict 中承载选择器的可选字段名（缺省即"不加入命令行"）
CASE_FIELD_MARKERS = "markers"
CASE_FIELD_KEYWORD = "keyword"
CASE_FIELD_PATH = "path"

# ---------------------------------------------------------------------------
# Allure 结果目录隔离（Day50 任务二）
# ---------------------------------------------------------------------------
# 每次执行写入独立目录的根目录名（相对项目根）
ALLURE_DIR_ROOT = "output"

# 独立目录名前缀。prune 时靠它识别"哪些兄弟目录是本机制的产物"，
# 避免把 output/ 下无关目录（报告归档等）当成陈旧结果删掉。
ALLURE_DIR_PREFIX = "allure_results_"

# 独立目录名里时间戳的格式（微秒精度）。
#
# 为什么必须到微秒: 同一条用例在 1 秒内被重复执行（回归重试、手动连点）
# 是常态，秒级命名会让两次执行抢同一个目录——而 pytest 的
# --clean-alluredir 正是"先删后写"，第二次执行会把第一次的结果整个删掉，
# 且删除动作本身在 Windows 上就是 WinError 145 的竞态来源。
ALLURE_DIR_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S-%f"

# 陈旧目录保留数量：超过该数量的旧目录在本次执行后被清理
ALLURE_DIR_KEEP_COUNT = 10

# 陈旧目录最小存活时长（秒）。只清理"创建已久"的目录，
# 是为了不误删**并发执行**中正在写入的目录——只要保留数量阈值被
# 并发数突破，正在跑的目录就可能被排进待删集合，而目录名的可比性
# 救不了它（另一个进程的执行时刻同样很新）。
ALLURE_DIR_MIN_AGE_SECONDS = 300

# 同进程内的目录序号自增器（Day50 实测修正）。
#
# **为什么时间戳不够**: Windows 系统时钟粒度约 15.6ms（GetSystemTime 的
# 定时器节拍），`datetime.now()` 的微秒位在同一节拍内**多次调用返回完全
# 相同的值**——实测连调两次得到同一个 049594。故仅靠时间戳的目录名在
# "15 毫秒内重复执行同一条用例"这一场景下必然撞名，而撞名意味着第二次
# 的 --clean-alluredir 会把第一次的结果整个删掉：那正是本机制要消灭的
# WinError 145 竞态，被绕一圈又回来了。
#
# 为什么序号 + pid 两者都要:
#   - 序号解决**同进程内**的撞名（批次并发是多条 daemon 线程）
#   - pid 解决**跨进程**的撞名（服务进程 vs CLI 批量执行同一时刻启动）
# `itertools.count` 的 next() 在 CPython 里是原子的，多线程下安全。
_ALLURE_DIR_SEQUENCE = count()


def build_allure_results_dir(case_id: str, root: str | Path | None = None) -> Path:
    """
    为单次执行生成**独立**的 Allure 结果目录（Day50 任务二）

    为什么每次执行都要独立目录（本日存在的唯一理由）:
        pytest.ini 的 addopts 固定了 `--alluredir=output/allure_results
        --clean-alluredir`，意味着**所有**执行共用一个目录。后果有两重：
          1. 结果互相覆盖/混杂——一次执行只剩最后一次写入的结果，
             而执行记录却按每次执行各存了一条，明细与报告对不上；
          2. `--clean-alluredir` 在 Windows 上要"先删整个目录"，
             目录被父 pytest（本轮回归）或另一个执行占着时直接抛
             WinError 145 / INTERNALERROR，整轮 pytest 全军覆没。

    参数:
        case_id (str): 用例编号（只用于目录名可读性与排障定位）
        root (str | Path | None): 结果目录的父目录；None 时取
                                  项目根下的 output/ 目录

    返回:
        Path: 尚未创建的独立目录路径（**本方法不创建目录**——
              目录由 pytest 子进程在写入结果时自行创建，
              调用方拿到的路径只用于拼命令行与事后回查）

    异常:
        无

    注意: 本方法只算路径不落盘，是刻意的——build_command 失败、
    子进程启动失败等场景根本不会有结果写入，提前 mkdir 只会留下一堆
    空目录。

    目录名唯一性: 序号 + pid 双重保证（见 `_ALLURE_DIR_SEQUENCE` 注释）。
    目录名按 `用例编号_时间戳_pid_序号` 排列，时间戳定序、序号补齐同节拍
    内的空档，故 `prune_stale_allure_dirs` 按名称倒序排序仍等于按创建
    时刻倒序。
    """
    base = Path(root) if root is not None else PROJECT_ROOT / ALLURE_DIR_ROOT
    stamp = datetime.now().strftime(ALLURE_DIR_TIMESTAMP_FORMAT)
    seq = next(_ALLURE_DIR_SEQUENCE)
    safe_case_id = "".join(
        char if char.isalnum() or char in "-_" else "_" for char in str(case_id)
    )[:48]
    return base / f"{ALLURE_DIR_PREFIX}{safe_case_id}_{stamp}_{os.getpid()}_{seq}"


def prune_stale_allure_dirs(
    current_dir: Path | None = None,
    root: str | Path | None = None,
    keep: int = ALLURE_DIR_KEEP_COUNT,
) -> list[str]:
    """
    清理陈旧的独立 Allure 结果目录（Day50 任务二·步骤3）

    决策: **代码自动清理，保留最近 N 次**，而不是"留给人手动删"。
        理由是目录隔离把"每次执行一个目录"变成了常态，不清理就等于
        把一个无上限增长的垃圾堆放进了 output/；而"让人定期手删"这种
        约定在没有告警的情况下必然被遗忘。

    参数:
        current_dir (Path | None): 本次执行的目录；强制跳过，绝不删自己
        root (str | Path | None): 待清理的父目录；None 时取项目根下 output/
        keep (int): 保留的最近目录数（按目录名倒序取前 keep 个不删）

    返回:
        list[str]: 已删除的目录名列表（便于日志与排障；删除失败不抛错，
                   只是不出现在返回值里）

    异常:
        无（清理是旁路能力，任何异常都吞掉并记 warning）
    """
    base = Path(root) if root is not None else PROJECT_ROOT / ALLURE_DIR_ROOT
    try:
        if not base.is_dir():
            return []
        candidates = sorted(
            (path for path in base.glob(f"{ALLURE_DIR_PREFIX}*") if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
        now = time.time()
        removed: list[str] = []
        for index, path in enumerate(candidates):
            if current_dir is not None and path == current_dir:
                continue
            if index < keep:
                continue
            try:
                # 存活时长过滤：并发执行中正在写入的目录不能删。
                # 目录名的排序只反映"创建时刻"，删一个刚被别的进程建出来
                # 的目录，会让那次执行的结果永远解析不到。
                if now - path.stat().st_mtime < ALLURE_DIR_MIN_AGE_SECONDS:
                    continue
            except OSError:
                continue
            # ignore_errors=True 而非 try/except：Windows 上目录可能被
            # 杀毒/索引服务短时占用，rmtree 会抛 PermissionError，
            # 而"这次没删掉、下次再删"是完全可接受的结果
            shutil.rmtree(path, ignore_errors=True)
            removed.append(path.name)
        if removed:
            logger.info(f"已清理陈旧Allure结果目录 {len(removed)} 个 | 目录根: {base}")
        return removed
    except OSError as exc:
        logger.warning(f"清理陈旧Allure结果目录失败（不影响执行结果）| {exc}")
        return []


def truncate_output_tail(text: str, limit: int = OUTPUT_TRUNCATE_LENGTH) -> str:
    """
    子进程输出截断（取**尾部**并加前缀标记）

    为什么取尾部而不是头部: pytest 的输出结构是「session 头 → 进度行 →
    末尾 FAILURES 段 + 汇总行（如 `3 failed, 27 passed in 1.2s`）」。
    头部是最没信息量的部分，尾部才是定位失败所需的堆栈与汇总。取头部
    会在长输出下把诊断价值最高的部分整段切掉。

    参数:
        text (str): 待截断的子进程输出
        limit (int): 保留的最大字符数（默认 OUTPUT_TRUNCATE_LENGTH）

    返回:
        str: 未超限时原样返回；超限时返回「前缀标记 + 末尾 limit 字符」

    异常:
        无
    """
    if not text or len(text) <= limit:
        return text
    return f"...[输出过长，已保留末尾 {limit} 字符]...\n{text[-limit:]}"


def validate_target_path(raw: object, context: str) -> str:
    """
    校验 pytest 位置参数（Day49 任务二的 path 字段）

    与 `src/common/source_ref.py` 的 `validate_source_ref` 分工:
        - source_ref 恒为**单个 .py 文件**（可带 ::节点选择），故其正则强制 .py 后缀
        - path 允许是**目录**（`tests/api_demo/`）或文件（`tests/a.py::test_x`），
          故另立一份校验，而不是硬套 source_ref 的正则——套了会把合法的
          目录选择判成非法。

    为什么执行侧还要再拒一次（纵深防御，理由同 Day47-fix P3-3）:
        该值会被**原样拼进 pytest 子进程命令**，而库里的数据不只经 Web/YAML
        录入侧写入，回填脚本、人工改库、未来迁移都可能塞进非法值。
          - `../secret/` 放行 = 执行目标越出项目根
          - 含空格/元字符的值进入命令行 = 事实上的参数注入
          - 以 `-` 开头 = 被 pytest 当成下一个选项而非位置参数

    参数:
        raw (object): 待校验的原始值（用例 dict 取出的未加工对象）
        context (str): 报错上下文前缀（通常是"用例 XXX: "），便于定位是哪条

    返回:
        str: 校验通过的路径字符串（已 strip）

    异常:
        ValueError: 非 str / 空串 / 含盘符、反斜杠、`..`、NUL / 以 `-` 开头
    """
    if not isinstance(raw, str):
        raise ValueError(
            f"{context}pytest 执行目标必须是 str，实际类型: {type(raw).__name__}"
        )
    value = raw.strip()
    if not value:
        raise ValueError(
            f"{context}pytest 执行目标不能为空串（既不传 path 也不传 source_ref 时"
            f"该用例不可被 pytest 执行）"
        )
    if "\x00" in value:
        raise ValueError(f"{context}pytest 执行目标不能包含 NUL 字符: {value!r}")
    if "\\" in value:
        raise ValueError(
            f"{context}pytest 执行目标不能包含反斜杠（请用 / 分隔）: {value!r}"
        )
    if value.startswith("-"):
        raise ValueError(
            f"{context}pytest 执行目标不能以 '-' 开头（会被 pytest 当成选项而非路径）: {value!r}"
        )
    # 盘符形态（C:foo）与绝对路径（/foo）判据：冒号只在 :: 节点选择处合法
    head = value.split("::", 1)[0]
    if ":" in head or head.startswith("/"):
        raise ValueError(
            f"{context}pytest 执行目标必须是相对项目根的路径（禁绝对路径/盘符）: {value!r}"
        )
    if ".." in head.split("/"):
        raise ValueError(
            f"{context}pytest 执行目标不能包含 '..'（执行目标不得越出项目根）: {value!r}"
        )
    return value


def decode_subprocess_stream(raw: object) -> str:
    """
    把子进程输出流统一解码为 str（Day49）

    为什么需要它（不是"想当然"）:
        `subprocess.TimeoutExpired` 在**两个平台携带的类型并不一致**——
        POSIX 分支的 `_communicate` 是把已累积的 **bytes** 片段拼进去的，
        即使调用方传了 text=True，异常上的 stdout/stderr 仍是 bytes；
        而 Windows 分支 `subprocess.run` 额外调了一次 communicate()，
        拿到的是已翻译过换行的 **str**。同一个字段按平台返回两种类型，
        直接 `.strip()` 会在 Linux 上抛 AttributeError，而这段代码正好在
        超时路径上跑——报错会把"超时"这一真实原因彻底掩盖。

    参数:
        raw (object): 异常或结果对象上的流值（bytes / str / None 皆可）

    返回:
        str: 解码后的文本；None 与非文本类型一律返回空串

    异常:
        无
    """
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw).decode(SUBPROCESS_ENCODING, errors=SUBPROCESS_ERRORS)
    return str(raw)


# 选项终止符：其后的元素一律作位置参数（Day44 P1-01 确立）
OPTION_TERMINATOR = "--"


def inject_allure_options(command: list[str], allure_dir: Path) -> list[str]:
    """
    把独立 Allure 结果目录注入 pytest 命令行（Day50 任务二）

    为什么**命令行参数能覆盖 pytest.ini 的 addopts**:
        pytest 把 addopts 展开后放在命令行参数**之前**，而
        `--alluredir` 是普通的 store 型选项（后写覆盖先写），
        故命令行里再传一次即可生效。这是官方稳定行为，不依赖插件实现。

    为什么必须插在 `--` **之前**而不是直接 append:
        `build_command` 产出的命令以 `--` 结尾，其后是执行目标路径。
        append 会让 `--alluredir=...` 落到终止符之后，被 pytest 当成
        第二个测试文件路径去收集，报 "file or directory not found"
        并以退出码 4 收场——与 Day44 P1-01 是同一个坑，只是这次错的是
        我们自己新加的参数。

    为什么不在 `build_command` 里直接写死:
        build_command 的命令形状被 5 个历史测试文件逐字断言
        （test_day43_closing / test_day44_bugfix / test_day47_source_ref /
        test_pytest_runner / test_web_executions_trigger）。在 run_one 里
        注入既拿到了隔离效果，又让那些契约断言保持原样成立。

    参数:
        command (list[str]): build_command 产出的命令（**不被修改**）
        allure_dir (Path): 本次执行的独立 Allure 结果目录

    返回:
        list[str]: 注入后的新命令列表；原命令不被就地修改

    异常:
        无（命令中没有终止符时按"追加到末尾"退化处理，不抛错）
    """
    marker = f"--alluredir={allure_dir}"
    terminator_index = (
        command.index(OPTION_TERMINATOR)
        if OPTION_TERMINATOR in command
        else len(command)
    )
    return [
        *command[:terminator_index],
        marker,
        "--clean-alluredir",
        *command[terminator_index:],
    ]


def resolve_bridge_execution_id(case: dict) -> str:
    """
    推导 Allure 桥接使用的执行批次号（Day50 任务二）

    取值优先级:
        1. 用例 dict 中的 `execution_id`（批次编排层若已带上则直接复用，
           这样同一批次的统计天然落在同一批次号下）
        2. 回落到 `pytest-{case_id}`

    为什么回落值是确定性的而不是"每次生成新批次号":
        `defect_statistics.execution_id` 有唯一约束，且 `save_statistics`
        刻意不做静默更新（见 ReportRepository 类注释）。若此处每次生成
        全新批次号，那么**每条用例每跑一次**就会往 defect_statistics 插
        一行——而这张表是 Web 报告统计的数据源，插进来的单条用例统计会
        把"批次数""通过率趋势""模块分布"的全部口径撑坏（一条用例被
        当成一个批次）。用确定性的 `pytest-{case_id}`，重复执行会命中
        唯一约束抛 IntegrityError，由桥接层捕获后如实记为桥接失败——
        **没入库要说没入库，而不是写一行看起来很成功的脏统计**。
        真正的批次号关联留给执行编排层（Day51 执行状态回写时接入）。

    参数:
        case (dict): 待执行用例字典

    返回:
        str: 非空批次号字符串（绝不会返回空串——空串会被 save_statistics
             抛 ValueError）

    异常:
        无
    """
    raw_execution_id = case.get("execution_id")
    if isinstance(raw_execution_id, str) and raw_execution_id.strip():
        return raw_execution_id.strip()
    case_id = str(case.get("case_id") or "").strip() or "unknown"
    return f"pytest-{case_id}"


@dataclass
class ExecutionResult:
    """
    单用例执行结果数据类

    执行器run_one的统一返回契约，编排层据此写test_executions明细。

    Day49 新增三个字段（**均带默认值，故对既有构造方式完全向后兼容**）:
        - 不改数据库 schema：编排层 case_manager 只按名取 result/duration/
          error_message 三个字段落库，没有 asdict/字段集断言，新增字段不会
          改变任何下游行为（Day48 当初"不新增字段"是为了避免动 schema，
          而 dataclass 追加带默认值字段并不触及 schema）
        - exit_code / exit_reason 承载 Day49 的退出码细化语义（口径见文件头
          EXIT_CODE_* 常量区）；result 仍取下游 VALID_RESULTS 四值

    属性:
        result (str): 执行结果，取值限定 passed/failed/error/skipped
                      （编排层 VALID_RESULTS 词表，越界会在落库校验处抛错并
                      把整批判 failed，故此处绝不使用词表外取值）
        error_message (str | None): 失败/错误时的异常信息
                                    （passed时为None）
        duration (float): 执行耗时（秒，浮点，支持亚秒精度）
        exit_code (int | None): 子进程原始退出码；进程未正常收场
                                （超时被杀/启动失败/参数非法）时为 None
        exit_reason (str): 细分分类，取值见 EXIT_REASON_* 常量，
                           区分"断言不通过"与"这次执行没跑成/没测到"
        output (str): 子进程输出（已截断）。超时场景保留 kill 前已捕获的
                      部分输出——超时恰恰最需要现场，而空输出会让排障
                      只能靠复现
        parsed_result (ParsedResult | None): Day50 新增。pytest 终端输出的
                      结构化解析结果（通过/失败/错误/跳过/警告计数、耗时、
                      失败用例明细、进度百分比）。**None = 未解析出结果**
                      （解析失败或子进程根本没跑起来），绝不因此改动
                      result 判定——result 仍只由退出码决定
        allure_dir (str): Day50 新增。本次执行的独立 Allure 结果目录
                      （绝对路径）；未进入子进程执行阶段时为空串
        bridge_error (str): Day50 新增。Allure 桥接（解析→聚合→入库）
                      失败原因；空串表示桥接成功或**无需桥接**（目录不存在
                      / 无结果文件 / 子进程未真正跑完）。桥接失败是旁路
                      故障，绝不影响 result 与 error_message
    """

    result: str
    error_message: str | None = None
    duration: float = 0.0
    exit_code: int | None = None
    exit_reason: str = ""
    output: str = ""
    parsed_result: ParsedResult | None = None
    allure_dir: str = ""
    bridge_error: str = ""


class BaseExecutor(ABC):
    """
    执行器抽象基类（策略接口）

    定义单用例执行的统一契约: 输入用例字典，输出ExecutionResult。
    所有具体执行器（模拟/真实pytest/协议扩展执行器）实现此接口，
    编排代码仅依赖本抽象，实现可插拔替换。
    """

    @abstractmethod
    def run_one(self, case: dict) -> ExecutionResult:
        """
        执行单条用例（抽象方法）

        参数:
            case (dict): 待执行用例字典（含case_id/name等字段）

        返回:
            ExecutionResult: 执行结果数据类
                             （result/error_message/duration）

        异常:
            具体实现约定: 内部异常应尽量转为error结果返回；
            未捕获异常由编排层兜底置批次failed
        """
        raise NotImplementedError


class SimulatedExecutor(BaseExecutor):
    """
    模拟执行器

    复刻CaseManager._simulate_execute的既有模拟规则（行为完全一致，
    供回归对比与无真实测试集场景使用）:
        1. time.sleep(0.01)模拟用例执行耗时
        2. 结果规则: case_id末尾数字为偶数→passed，奇数→failed
           （failed时error_message为固定模拟文案，无数字视为偶数）
    """

    def run_one(self, case: dict) -> ExecutionResult:
        """
        模拟执行单条用例

        参数:
            case (dict): 待执行用例字典（含case_id字段）

        返回:
            ExecutionResult: result为passed/failed；failed时
                             error_message为固定模拟文案；
                             duration为真实测量耗时（含0.01s睡眠）

        异常:
            无
        """
        start_time = time.perf_counter()
        time.sleep(0.01)  # 模拟用例执行耗时
        duration = time.perf_counter() - start_time

        # case_id末尾数字奇偶决定结果（无数字时视为偶数→passed）
        tail_digits = [
            char for char in str(case.get("case_id", "")) if char.isdigit()
        ]
        tail_number = int(tail_digits[-1]) if tail_digits else 0
        if tail_number % 2 == 0:
            logger.debug(f"模拟执行通过 | 用例: {case.get('case_id')}")
            return ExecutionResult(result="passed", error_message=None, duration=duration)

        error_message = "模拟执行失败: 断言不通过"
        logger.debug(
            f"模拟执行失败 | 用例: {case.get('case_id')} | {error_message}"
        )
        return ExecutionResult(
            result="failed", error_message=error_message, duration=duration
        )


class PytestRunner(BaseExecutor):
    """
    真实pytest执行器（subprocess 同步执行版）

    以子进程方式真实拉起 pytest 执行 source_ref 指向的测试文件，
    并按退出码把结果映射为 passed/failed/error。

    执行目标来源（Day47 建立、Day48 收口，与 ADR-001 决策③一致）:
        source_ref 是**唯一**合法的 pytest 执行目标，为空即语义为
        "该用例不可被 pytest 执行"。**绝不回落到 case_id，也不再回落到
        script_path**——业务编号不是文件路径，回落后每条用例都以
        "file or directory not found" 退出码 4 收场，全量落 error，
        真实执行链路功能性不可用。Day47 期间的 script_path 过渡兼容
        分支已在 Day48 随回填收口移除。

    三项可配置项（Day48）:
        - timeout: 单条用例的执行预算。优先级 构造参数 > 环境变量
          TM_PYTEST_TIMEOUT > 模块常量 PYTEST_TIMEOUT_SECONDS。
        - cwd:     子进程工作目录。pytest 靠它定位测试文件与配置文件，
          默认取项目根 PROJECT_ROOT（不传则继承调用进程 cwd，会让
          从别处触发的执行找不到 pytest.ini 与相对路径用例）。
        - env:     叠加到当前进程环境变量之上的额外环境变量
          （如 PYTEST_ADDOPTS、PYTHONPATH），用于按批次注入执行环境。

    退出码映射（Day49 细化）:
        result      取下游 VALID_RESULTS 四值（passed/failed/error/skipped）
        exit_code   原始退出码
        exit_reason 细分分类

        退出码 → result / exit_reason:
            0 → passed / passed              全部通过
            1 → failed / tests_failed        收集成功但有用例失败
            2 → error   / interrupted        执行被中断
            3 → error   / internal_error     pytest 自身内部错误
            4 → error   / usage_error        命令行/环境用法错误
            5 → error   / no_tests_collected 选择器过滤后未收集到用例
            其它 → error   / unknown_exit_code

        进程根本没正常收场时 exit_code 为 None，另有一组 exit_reason：
            timeout / spawn_failed / invalid_arguments / invalid_case

    进程树清理（Day49）:
        超时不再只杀直接子进程。pytest 自己会 spawn xdist worker、被测代码
        还会再开子进程，`subprocess.run` 内部的 process.kill() 只杀得到
        直接子进程，其余变孤儿继续占端口与内存——Day48 的"只杀不洁"。
        故本类改用 Popen 自行持有句柄（拿得到 pid，这是 run 拿不到的），
        超时后按平台清理整棵进程树：Windows 走 `taskkill /T /F`，
        POSIX 让子进程自成一个进程组后 `killpg`。

    Allure 结果目录隔离与桥接（Day50）:
        每次执行分配独立目录 `output/allure_results_{case_id}_{时间戳}`
        并以命令行参数显式覆盖 pytest.ini addopts 里的默认 alluredir。
        这同时消除两个问题：多次执行结果互相混杂，以及 --clean-alluredir
        在 Windows 上与共享目录的 WinError 145 竞态（详见
        `build_allure_results_dir`）。执行完成后由
        `bridge_allure_results` 自动解析并入库 defect_statistics。
    """

    def __init__(
        self,
        timeout: int | None = None,
        cwd: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """
        构造PytestRunner并归一化三项可配置项

        参数:
            timeout (int | None): 单条用例执行超时秒数；None表示按
                                 "环境变量TM_PYTEST_TIMEOUT → 模块常量"
                                 顺序回落。非法值（≤0 / 非整数 /
                                 环境变量值非数字）一律抛 ValueError
                                 而不是静默回落——静默回落会让"我明明
                                 把预算调到5秒"却仍按30秒跑，排查时
                                 完全看不出配置没生效。环境变量值为空串
                                 或纯空白时视为"未配置"，回落为模块
                                 常量（与"未设置"同义），不抛错。
            cwd (str | Path | None): 子进程工作目录；None时取项目根
                                     PROJECT_ROOT。
            env (dict[str, str] | None): 额外环境变量（叠加在当前进程
                                          环境变量之上）；None表示不注入。
                                          **每个键和值都必须是 str**；键
                                          不能为空串、不能含 "="。
                                          非法时构造期即抛 TypeError /
                                          ValueError，绝不自动 str()
                                          转换——见函数体注释。

        返回:
            无

        异常:
            ValueError: timeout 非法，或 env 的键为空串 / 含 "="（详见参数说明）
            TypeError: cwd 不是 str/Path，env 不是 dict，或 env 的键/值不是 str
        """
        self.timeout = self._resolve_timeout(timeout)

        # cwd / env 的显式类型校验（Day48-fix P3-6）
        #
        # 为什么已经有了"响亮失败"还要加：传错类型原本会在 `Path(cwd)` /
        # `dict(env)` 处抛 TypeError 或 ValueError，但那两条消息来自标准库
        # 内部（"expected str, bytes or os.PathLike object, not int"），
        # 读不出"是我把 cwd 传成了 int"。三参数里只有 timeout 有业务级
        # 校验，失败信息口径不齐——同一个构造函数的三个参数，报错风格
        # 却是两种。这里统一为带字段名与实际类型的业务消息。
        #
        # None 是"未配置"而非"类型错误"，故跳过校验直接走默认值分支。
        if cwd is not None and not isinstance(cwd, (str, Path)):
            raise TypeError(
                f"pytest执行工作目录必须是 str 或 Path，实际类型: {type(cwd).__name__}"
            )
        if env is not None and not isinstance(env, dict):
            raise TypeError(
                f"pytest执行环境变量必须是 dict 或 None，实际类型: {type(env).__name__}"
            )

        self._validate_env_content(env)

        self.cwd = Path(cwd) if cwd is not None else PROJECT_ROOT
        self.env = dict(env) if env is not None else None

    @staticmethod
    def _validate_env_content(env: dict[str, str] | None) -> None:
        """
        校验 env 字典内部的键与值（Day48-fix2）

        为什么要单独校验"内容"而不只校验"容器类型"（Day48-fix P3-6 的盲区）:
            `dict[str, str]` 只是个类型标注，运行时不生效——`PytestRunner(
            env={"PORT": 8080})` 在 Python 看来完全合法。而
            `subprocess.run(env=...)` 真正要求环境块**全部是字符串**，
            非 str 会抛 `TypeError: environment can only contain strings`。

        关键在于这个 TypeError **不在 run_one 的既有 except 链里**
            （只捕 TimeoutExpired 与 OSError），会直接上抛穿透 run_one，
            违反 docstring"异常: 无"的契约，更违反"单条用例的失败不升级
            为整批 failed"这条更重要的编排契约——一条用例的环境变量配错
            会让整批判 failed。

        为什么不自动 str() 转换（否决方案 C）:
            `None → "None"`、`True → "True"` 是**静默语义篡改**——子进程
            拿到的是一段毫无意义的文本，配置错误被伪装成正常值，排障时
            反而找不到根因。与 Day48 确立的"非法配置显式抛错"原则冲突。

        参数:
            env (dict[str, str] | None): 待校验的环境变量字典；
                                         None 表示"未配置"，直接跳过

        返回:
            无

        异常:
            TypeError: 键或值不是 str
            ValueError: 键为空串，或键含 "="（Windows 环境块以 "=" 分隔
                        键值，含 "=" 的键会被 subprocess 判为
                        `illegal environment variable name`）
        """
        if env is None:
            return
        for key, value in env.items():
            # 键先校验：键非法时后两条消息都指不到具体变量名
            if not isinstance(key, str):
                raise TypeError(
                    f"pytest执行环境变量的键必须是 str，实际类型: {type(key).__name__}，"
                    f"实际值: {key!r}"
                )
            if not key:
                raise ValueError(
                    "pytest执行环境变量的键不能为空串"
                    "（Windows 下空键名会被判为非法环境变量名）"
                )
            if "=" in key:
                raise ValueError(
                    f"pytest执行环境变量的键 {key!r} 不能包含 '='"
                    f"（环境块以 '=' 分隔键值，含 '=' 会被判为非法变量名）"
                )
            # bytes 同样拒：Windows CreateProcess 的环境块只接受 str，
            # 放行 bytes 只是把失败从构造期推迟到调用期
            if not isinstance(value, str):
                raise TypeError(
                    f"pytest执行环境变量 {key} 的值必须是 str，"
                    f"实际类型: {type(value).__name__}"
                )

    @staticmethod
    def _resolve_timeout(timeout: int | None) -> int:
        """
        归一化执行超时阈值（构造参数 > TM_PYTEST_TIMEOUT > 模块常量）

        参数:
            timeout (int | None): 显式传入的超时秒数；None则读环境变量

        返回:
            int: 归一化后的正整数超时秒数

        异常:
            ValueError: 三级来源任一解析出的值不是正整数

        注意（空值语义，Day48-fix P3-5 明确）:
            环境变量 TM_PYTEST_TIMEOUT 值为**空串或纯空白**时视为
            "未配置"，与"未设置"同义，回落为 PYTEST_TIMEOUT_SECONDS，
            **不抛错**。这与 `env_manager.get` 的口径一致（该方法对空串
            返回 default），也和 Day46 起 source_ref 的"空即未配置"语义
            统一。只有值为**非空白却不是正整数**时（如 "abc"、"0"、"1.5"）
            才抛 ValueError。

            为什么空串要放行而不是报错: 容器与 CI 的配置注入经常把未设置
            的变量渲染成空串（如 `-e TM_PYTEST_TIMEOUT=`），此时报
            "必须是正整数"是噪声而非信息——它看上去像运维配错了，实际
            含义就是"没配"。而真正需要拦截的是"配了但配错了"，那才会
            静默按 30 秒跑并让人查不到原因。
        """
        if timeout is not None:
            resolved = timeout
        else:
            raw_env = env_manager.get(TM_PYTEST_TIMEOUT_KEY)
            if raw_env is None or not str(raw_env).strip():
                return PYTEST_TIMEOUT_SECONDS
            try:
                resolved = int(str(raw_env).strip())
            except ValueError as exc:
                raise ValueError(
                    f"环境变量{TM_PYTEST_TIMEOUT_KEY}必须是正整数秒数，"
                    f"实际值: {raw_env!r}"
                ) from exc

        # bool 是 int 的子类，isinstance(True, int) 为真但语义不是"1秒"，
        # 故一并拒掉，避免漏写 timeout=True 时拿到 1 秒预算。
        if isinstance(resolved, bool) or not isinstance(resolved, int) or resolved <= 0:
            raise ValueError(f"pytest执行超时阈值必须是正整数秒数，实际值: {resolved!r}")
        return resolved

    def build_command(self, case: dict) -> list:
        """
        拼装单用例的pytest执行命令（Day49 增加选择器透传）

        位置参数（执行目标）取值优先级（Day49 决策）:
            1. `path`       —— 本日新增，支持目录或文件（任务二）
            2. `source_ref` —— Day48 确立的唯一执行目标，缺省即不可执行
            3. 两者都无 → 抛 ValueError

        为什么不给 path 缺省配一个“默认测试目录”（与任务书步骤2的偏差）:
            那等于把 Day48 刚移除的“回落”缺陷又请回来——source_ref 为空
            时静默跑整个 tests/ 目录，库里每条没回填 source_ref 的用例都会
            去跑**全量**测试集，结果还按 passed/failed 正常入库，运维看到
            的是“这条用例通过了”，真实原因（该用例根本没绑定执行目标）被
            彻底掩盖。Day48 的既定契约是“空即不可执行”，本日不推翻。

        选择器字段（均可选，缺省不加入命令行）:
            `markers` -> -m、`keyword` -> -k

        参数顺序（**不能照字面把 path 放到选项前面**）:
            任务书写的是 `pytest [path] [-m markers] [-k keyword]`，本方法
            必须输出 `pytest [-m ...] [-k ...] -- path`。见函数体末尾注释中
            Day44 P1-01 的回归点：`--` 之后 pytest 停止解析选项，其后元素
            一律作位置参数，path 放在 `--` 之前会被当成上一个选项的参数值。

        参数:
            case (dict): 待执行用例字典
                         （source_ref/path/markers/keyword 均为可选键）

        返回:
            list[str]: 命令参数列表，形如
                [sys.executable, "-m", "pytest", "-q", "--tb=short",
                 "-m", "smoke", "--", path]
                （无任何选择器时与 Day48 完全一致：
                 [sys.executable, "-m", "pytest", "-q", "--tb=short", "--", path]）

        异常:
            ValueError: 执行目标缺失/非法/不存在，或选择器字段非法
                        （消息均说明该补什么/改什么）
        """
        case_id = str(case.get("case_id", ""))
        source_ref = case.get("source_ref")
        raw_path = case.get(CASE_FIELD_PATH)

        # 执行目标优先级：path（Day49 新增，可为目录） > source_ref（Day48 唯一目标）
        if raw_path is not None:
            path = validate_target_path(raw_path, f"用例 {case_id}: ")
            # 存在性校验只针对 path、不针对 source_ref，理由:
            #   source_ref 是回填脚本批量产出的历史数据，逐条 stat 会让一次
            #   批量执行多出 N 次文件系统查询；且回填失败的用例本就该由
            #   pytest 自己报 4 号退出码，拦在我们这边并不增加信息量。
            #   path 是本日新增的、调用方当场传的参数，拦下来能给出明确
            #   错误，而不是让 pytest 抛一句晦涩的 file or directory not found。
            # 节点选择后缀（::test_func）不属于文件系统路径，存在性校验必须
            # 只看 :: 之前的那一段——否则 pytest 合法的节点写法会被我们
            # 误判成"路径不存在"，把选择器能力自己堵死
            fs_path = path.split("::", 1)[0]
            resolved = self.cwd / fs_path
            if not resolved.exists():
                raise ValueError(
                    f"用例 {case_id} 的 path {path!r} 在子进程工作目录下不存在: "
                    f"{resolved}（请核对用例配置的路径与执行器 cwd）"
                )
        else:
            path = self._resolve_source_ref(case_id, source_ref)

        markers = self._validate_selector(
            case.get(CASE_FIELD_MARKERS), CASE_FIELD_MARKERS, case_id
        )
        keyword = self._validate_selector(
            case.get(CASE_FIELD_KEYWORD), CASE_FIELD_KEYWORD, case_id
        )

        # sys.executable 指向当前解释器（虚拟环境下即 venv 的 python，
        # 保证用项目依赖跑 pytest），跨 Windows/Linux/macOS 通用；
        # 修前硬编码的 "py" 是 Windows 专属启动器，非 Windows 平台
        # 每条用例都会 OSError 降级为 error。
        command = [sys.executable, "-m", "pytest", "-q", "--tb=short"]
        # 选择器必须在 "--" 之前：其后 pytest 一律当位置参数
        if markers is not None:
            command += ["-m", markers]
        if keyword is not None:
            command += ["-k", keyword]
        command += ["--", path]

        # 选项终止符 "--" 的位置（Day44 P1-01）：
        #   "--" 之后 pytest 的 argparse 停止解析选项，其后**全部**元素
        #   一律作为位置参数（测试文件路径）。因此它必须放在**所有选项之后、
        #   紧邻 path 之前**：
        #       [.., "pytest", "-q", "--tb=short", "--", path]   ← 正确
        #       [.., "pytest", "--", path, "-q", "--tb=short"]   ← 错误
        #   错误写法会让 "-q" 与 "--tb=short" 同样被当成路径，pytest 报
        #   "file or directory not found: -q" 并以退出码 4 收场，每条用例
        #   都落入 error 分支——真实执行链路功能性不可用。
        #   保持 "--" 的目的不变：阻断路径形如 "--version" 时被 pytest
        #   当选项执行、退出码 0 被误判为 passed 的假通过。
        #   Day49 的 -m / -k 同理必须排在 "--" 之前，理由完全一致。
        return command

    def _validate_selector(self, raw: object, field: str, case_id: str) -> str | None:
        """
        校验选择器字段（markers / keyword）并归一化（Day49 任务二）

        为什么"subprocess 用 list 形式"不等于可以不校验:
            list 形式确实天然免疫 shell 注入（不经 shell、无需转义），但
            **pytest 自己**仍在做选项解析——`-m` 的值若以 `-` 开头，pytest
            会把它当成缺失参数后的下一个选项去解析，而不是标记表达式。
            这类"值变成了选项"的错位，报错出现在 pytest 内部、极难定位。

        参数:
            raw (object): 用例 dict 中的原始值（未加工，可能是任何类型）
            field (str): 字段名（markers / keyword），用于报错消息
            case_id (str): 用例编号，用于定位是哪条用例

        返回:
            str | None: 校验通过的字符串；字段缺省或为 None 时返回 None
                         （表示"该字段未配置，不加入命令行"）

        异常:
            ValueError: 非 str / 空串或纯空白 / 含 NUL / 以 "-" 开头
        """
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ValueError(
                f"用例 {case_id} 的 {field} 必须是 str，实际类型: {type(raw).__name__}"
            )
        value = raw.strip()
        if not value:
            raise ValueError(
                f"用例 {case_id} 的 {field} 不能为空串"
                f"（不传该字段即可关闭过滤，传空串不是'不过滤'）"
            )
        if "\x00" in value:
            raise ValueError(f"用例 {case_id} 的 {field} 不能包含 NUL 字符")
        if value.startswith("-"):
            raise ValueError(
                f"用例 {case_id} 的 {field} 不能以 '-' 开头"
                f"（会被 pytest 当成选项而非表达式）: {value!r}"
            )
        return value

    @staticmethod
    def _resolve_source_ref(case_id: str, source_ref: object) -> str:
        """
        取并校验 source_ref 执行目标（Day48 契约，Day49 原样保留）

        参数:
            case_id (str): 用例编号（仅用于报错定位）
            source_ref (object): 用例 dict 中的 source_ref 原始值

        返回:
            str: 校验通过的执行目标路径

        异常:
            ValueError: source_ref 为空，或格式非法/超长/含 ".."

        为什么 source_ref 为空必须报错而不是找个值顶上: 空值语义
        "不可被 pytest 执行"是该列存在的理由，回落到 case_id 会把这条
        语义抹平——pytest 拿业务编号当路径，报 file or directory not
        found 并以退出码 4 收场，每条用例落入 error 分支，排障时看到
        却是"路径错了"而非"该用例本就不该走 pytest"。
        """
        path = source_ref.strip() if isinstance(source_ref, str) else source_ref

        if not path:
            raise ValueError(
                f"用例 {case_id} 的 source_ref 为空，该用例不可被 pytest 执行；"
                f"请先回填 source_ref（python -m src.scripts.backfill_source_ref "
                f"--dry-run 查看待补录清单），或改用 SimulatedExecutor"
            )

        # 执行侧纵深防御（Day47-fix P3-3）
        #
        # 录入侧校验是第一道闸，但库里的值不只经录入侧写入：回填脚本
        # 按描述文本猜路径、人工直接改库、未来的数据迁移都可能塞进
        # 非法值。而本方法会把该值**原样拼进 pytest 子进程命令**，
        # 故这里必须自己再拒一次：
        #   - `../secret/x.py` 放行 = 测试目标越出项目根
        #   - 含空格/元字符的值进入命令行 = 事实上的参数注入
        #
        # 与录入侧共用 src/common/source_ref.py 的同一函数（而不是
        # 各写一份），避免"录入放行、执行拒绝"的口径分叉——那会让
        # 同一条数据在不同链路上表现不一致，最难排查。
        try:
            validated = validate_source_ref(str(path), context=f"用例 {case_id}: ")
        except ValueError as exc:
            raise ValueError(
                f"{exc}（该值来自库中 source_ref，非法值不会进入 pytest 命令；"
                f"请修正后重试）"
            ) from exc
        # 上方已排除空值，validate_source_ref 的 None 分支理论不可达；
        # 仍显式兜底而不是用断言/cast——一旦上游口径变了，这里应是可读的
        # 业务错误，而不是裸的 TypeError
        if validated is None:
            raise ValueError(
                f"用例 {case_id} 的 source_ref 校验后为空（{path!r}），"
                f"该用例不可被 pytest 执行"
            )
        return validated

    def _build_env(self) -> dict[str, str] | None:
        """
        构造子进程环境变量（当前进程环境 + 构造时注入的额外变量）

        返回:
            dict[str, str] | None: 合并后的环境变量字典；未注入额外变量时
                                   返回 None（交由 subprocess 直接继承
                                   当前进程环境，避免无谓地拷贝一份环境）

        异常:
            无
        """
        if not self.env:
            return None
        return {**os.environ, **self.env}

    def run_one(self, case: dict) -> ExecutionResult:
        """
        同步执行单条用例的pytest子进程（Day49：Popen + 进程树清理 + 退出码细化）

        执行流程:
            1. build_command拼装命令（执行目标缺失/非法时抛 ValueError，
               此处捕获后降级为该条用例的 error 结果）
            2. 为本次执行生成独立 Allure 结果目录并注入命令行
               （Day50 任务二，覆盖 pytest.ini addopts 的默认 alluredir）
            3. Popen 起子进程并 communicate(timeout=self.timeout)，
               工作目录取 self.cwd（默认项目根），环境变量为 self.env
               叠加当前进程环境
            4. 超时 → 清理**整棵进程树**（Windows taskkill /T /F、
               POSIX killpg），保住 kill 前已捕获的部分输出，再降级为
               error + exit_reason=timeout
            5. 正常收场 → 按退出码映射 result/exit_code/exit_reason，
               解析终端输出为结构化 parsed_result，并把本次独立目录的
               Allure 结果桥接入库（Day50 任务一、二）

        为什么改用 Popen 而不是 subprocess.run（Day49 核心变更）:
            `subprocess.run` 内部超时只做 `process.kill()`，杀的是**直接
            子进程**；pytest 自己 spawn 的 xdist worker、被测代码再开的
            子进程全部变孤儿，继续占端口与内存（Day48 的"只杀不洁"）。
            而要清理后代必须知道子进程 pid——`run` 不返回它，
            `TimeoutExpired` 上也不带（CPython 3.11 实测）。
            `Popen` 自带句柄与 pid，这是拿到它的唯一标准库途径。

        超时后为什么还要再 kill 一次（看起来多余，其实不是）:
            我们自己接管 communicate 后就不再有"run 内部的 kill"，直接子
            进程与整棵后代树都得由我们清理。若沿用 run 的行为只补一句
            `process.kill()`，那 process tree 的清理逻辑就没有容身之处。

        参数:
            case (dict): 待执行用例字典

        返回:
            ExecutionResult: result 为 passed/failed/error（下游四值词表）；
                             failed/error 时 error_message 非空；
                             exit_code 记录原始退出码（未正常收场为 None）；
                             exit_reason 记录细分分类；
                             output 保留子进程输出（已截断）

        异常:
            无（命令拼装失败与子进程异常均转为error结果返回，不向上抛，
            保持"单条用例的失败不升级为整批failed"的既有契约）

            被本方法兜住并降级为 error 的全部异常类型：
                - ValueError: build_command 侧的执行目标缺失/格式非法
                - subprocess.TimeoutExpired: 子进程超时（exit_reason=timeout）
                - OSError: 命令不存在/权限不足/工作目录不存在
                  （exit_reason=spawn_failed）
                - TypeError / ValueError: **子进程启动参数非法**
                  （Day48-fix2 补齐，如 env 含非字符串值、env 键含 "="、
                  cwd 含 NUL）——这两类此前会直接上抛穿透本方法
                  （exit_reason=invalid_arguments）
        """
        start_time = time.perf_counter()
        try:
            command = self.build_command(case)
        except ValueError as exc:
            # 编排层逐条 run_one，任何异常上抛都会让整批判 failed；
            # 而"执行目标缺失/非法"是**单条用例的配置问题**、不是批次故障，
            # 必须降级为该条用例的 error 结果，让批次继续跑完其余用例。
            duration = time.perf_counter() - start_time
            error_message = str(exc)
            logger.warning(
                f"pytest执行目标缺失或非法（该用例不可被 pytest 执行）| "
                f"用例: {case.get('case_id')} | {exc}"
            )
            return ExecutionResult(
                result="error",
                error_message=error_message,
                duration=duration,
                exit_code=None,
                exit_reason=EXIT_REASON_INVALID_CASE,
            )

        # Day50 任务二：为本次执行分配**独立** Allure 结果目录，并在命令行
        # 显式覆盖 pytest.ini addopts 里的默认 alluredir（命令行参数优先级
        # 高于 addopts）。放在 build_command 之后而非之内，是为了让
        # build_command 的命令形状契约与其历史断言保持原样成立。
        allure_dir = build_allure_results_dir(str(case.get("case_id", "")))
        command = inject_allure_options(command, allure_dir)

        # Day49：从 subprocess.run 改为 Popen，理由见方法 docstring
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding=SUBPROCESS_ENCODING,
                errors=SUBPROCESS_ERRORS,
                # pytest 靠 cwd 定位测试文件与 pytest.ini：不显式指定时
                # 继承调用进程的工作目录，而 Web 触发 / 任务队列 worker
                # 的 cwd 与项目根无关，source_ref 里的相对路径会全部
                # 找不到，每条用例以退出码 4 落 error。
                cwd=str(self.cwd),
                env=self._build_env(),
                # POSIX: 让子进程自成一个会话/进程组，超时才能 killpg 一次
                # 干掉整棵树；否则 killpg 会波及调用方所在的终端进程组
                # （把 IDE / CI runner 一起杀掉）。Windows 侧该参数是
                # unused_*，传什么都不生效，树杀改由 taskkill /T 负责。
                start_new_session=os.name == "posix",
            )
            # timeout 归 communicate 管（不是 Popen 的参数）——Day48 的
            # subprocess.run 也正是把 timeout 传到这里，语义一致
            stdout, stderr = process.communicate(timeout=self.timeout)
            returncode = process.returncode
        except subprocess.TimeoutExpired as exc:
            # 超时：先清整棵进程树，再组装结果
            duration = time.perf_counter() - start_time
            cleanup_note = ""
            if process is not None:
                cleanup_note = self.kill_process_tree(process)
            # 超时前已捕获的部分输出必须留下（Day49 任务一.3）。
            #
            # 丢掉它等于让最需要现场的那次执行什么都不留：超时恰恰是
            # "必须复现才能定位"的故障，而复现前手里没有半行输出。
            # 注意 POSIX 下 TimeoutExpired 上的流是 **bytes**（即便传了
            # text=True），Windows 下才是 str，故统一走 decode 归一。
            captured = "\n".join(
                part
                for part in (
                    decode_subprocess_stream(exc.stdout).strip(),
                    decode_subprocess_stream(exc.stderr).strip(),
                )
                if part
            )
            captured_tail = truncate_output_tail(captured)
            # 超时消息与日志**刻意保留完整命令行**（含 sys.executable 的
            # 解释器绝对路径），不做脱敏也不截断（Day48-fix P3-4 决策）。
            #
            # 理由: 这是本地自托管工具的解释器路径（如
            # D:\projects\TestMatrix\.venv\Scripts\python.exe），既非凭据
            # 也不含任何业务数据；而超时恰恰是"必须复现才能定位"的故障，
            # 缺了命令行就无法判断当时跑的是哪个解释器、哪些选项、哪个
            # 用例路径——把最有诊断价值的一段裁掉，保全性收益为零而排障
            # 成本陡增。
            #
            # 若将来本工具改为多租户/对外托管，需重新评估：那时解释器
            # 路径会暴露部署方的主机目录结构，口径要与项目既有日志脱敏
            # 规则对齐（见 Day39 的 _sanitize_log_field）。
            error_message = (
                f"执行超时(>{self.timeout}s): {' '.join(command)}"
                f" | 进程树清理: {cleanup_note}"
            )
            if captured_tail:
                error_message = f"{error_message}\n超时前输出:\n{captured_tail}"
            logger.warning(
                f"pytest子进程超时 | {' '.join(command)} | 进程树清理: {cleanup_note}"
            )
            return ExecutionResult(
                result="error",
                error_message=error_message,
                duration=duration,
                exit_code=None,
                exit_reason=EXIT_REASON_TIMEOUT,
                output=captured_tail,
                # 超时场景同样解析：被腰斩的执行只跑了一半，
                # 恰恰是最需要现场的那一次（Day50 任务一明确要求）
                parsed_result=self.parse_output(captured_tail),
                allure_dir=str(allure_dir),
            )
        except OSError as exc:
            # 命令不存在/权限不足等子进程启动异常（如py不在PATH）
            duration = time.perf_counter() - start_time
            error_message = f"pytest子进程启动失败: {exc}"
            logger.error(f"{error_message} | 命令: {command}")
            return ExecutionResult(
                result="error",
                error_message=error_message,
                duration=duration,
                exit_code=None,
                exit_reason=EXIT_REASON_SPAWN_FAILED,
            )
        except (TypeError, ValueError) as exc:
            # 子进程**启动参数**非法（Day48-fix2 兜底层）
            #
            # 实测逃逸面（Windows + Python 3.11.9 实测复现）：这些异常由
            # Popen 在真正创建进程**之前**抛出，既不是 TimeoutExpired 也
            # 不是 OSError，此前直接上抛穿透 run_one：
            #   env 值非 str（int/None/bool/float/bytes）
            #       → TypeError: environment can only contain strings
            #   env 键非 str（int） → TypeError: bad argument type
            #   env 键含 "="        → ValueError: illegal environment variable name
            #   env 键/值含 NUL     → ValueError: embedded null character
            #   cwd 含 NUL          → ValueError: embedded null character
            # 跨平台成立：CI 的 Linux 3.11 走 POSIX 分支（env 经
            # os.fsencode 转换），非 str 同样抛 TypeError。
            #
            # 为什么不靠构造期校验就够了——两层都要：
            #   ① 构造期校验（_validate_env_content）把**自己写的**配置
            #      错误挡在最早处，报错带变量名，比这里清楚得多；
            #   ② 但 run_one 是编排层唯一入口，"单条用例的失败绝不升级为
            #      整批 failed"是它对外的硬契约。任何未预料的参数异常都
            #      必须在此兜住——一条用例的环境变量配错，不能让整批判
            #      failed 且丢掉真正的原因。
            #   未来若有人直接给 self.env 赋值绕过构造函数，本分支仍兜底。
            #
            # 与 OSError 分支并列而非合并：两者同属"启动失败"语义，但
            # OSError 是操作系统层面的故障（解释器不存在/权限不足），
            # 本分支是我们传给 subprocess 的参数不合法，日志分级与措辞
            # 需区分——前者查环境，后者查代码。
            duration = time.perf_counter() - start_time
            error_message = f"pytest子进程启动失败(参数非法): {exc}"
            logger.error(f"{error_message} | 命令: {command}")
            return ExecutionResult(
                result="error",
                error_message=error_message,
                duration=duration,
                exit_code=None,
                exit_reason=EXIT_REASON_INVALID_ARGUMENTS,
            )
        duration = time.perf_counter() - start_time
        exit_reason = EXIT_CODE_REASON_MAP.get(returncode, EXIT_REASON_UNKNOWN_EXIT_CODE)
        result_value = EXIT_CODE_RESULT_MAP.get(returncode, "error")

        # 输出统一归一：stdout 与 stderr 都要留，Day50 的报告解析器会用到
        full_output = "\n".join(
            part for part in ((stdout or "").strip(), (stderr or "").strip()) if part
        )
        output_tail = truncate_output_tail(full_output)

        # Day50 任务一：从终端输出解析出结构化结果（计数/耗时/失败明细）
        parsed_result = self.parse_output(full_output)

        # Day50 任务二：把本次独立目录的 Allure 结果桥接入库。
        #
        # 只有子进程**真正跑完**（走到这里即 returncode 已知）才桥接：
        # 超时与启动失败分支在此之前已 return，那种执行只产出了半份甚至
        # 零份 Allure 结果，入库会把"被腰斩的执行"记成一次完整批次统计，
        # 比不入库更糟。
        bridge_error = self.bridge_allure_results(allure_dir, case)
        prune_stale_allure_dirs(current_dir=allure_dir)

        if result_value == "passed":
            logger.debug(
                f"pytest执行通过 | 用例: {case.get('case_id')} | "
                f"耗时: {duration:.3f}s | 退出码: {returncode}"
            )
            return ExecutionResult(
                result="passed",
                error_message=None,
                duration=duration,
                exit_code=returncode,
                exit_reason=exit_reason,
                output=output_tail,
                parsed_result=parsed_result,
                allure_dir=str(allure_dir),
                bridge_error=bridge_error,
            )

        if result_value == "failed":
            # pytest约定退出码1: 存在失败用例，失败详情在stdout
            message = truncate_output_tail(full_output) or f"pytest退出码: {returncode}"
            logger.debug(
                f"pytest执行失败 | 用例: {case.get('case_id')} | 退出码: {returncode}"
            )
            return ExecutionResult(
                result="failed",
                error_message=message,
                duration=duration,
                exit_code=returncode,
                exit_reason=exit_reason,
                output=output_tail,
                parsed_result=parsed_result,
                allure_dir=str(allure_dir),
                bridge_error=bridge_error,
            )

        # 其余退出码（2/3/4/5 及未收录值）：这次执行本身没跑成/没测到东西，
        # 一律 error（下游词表口径），细分语义由 exit_reason + 前缀措辞承载
        advice = EXIT_CODE_ADVICE_MAP.get(returncode, "")
        message = truncate_output_tail(full_output) or (
            f"pytest异常退出，退出码: {returncode}（{exit_reason}）"
        )
        error_message = f"pytest异常退出[{exit_reason}]，退出码: {returncode}"
        if advice:
            error_message = f"{error_message} | 处置建议: {advice}"
        error_message = f"{error_message}\n{message}"
        logger.warning(
            f"pytest异常退出 | 用例: {case.get('case_id')} | "
            f"退出码: {returncode} | 分类: {exit_reason}"
        )
        return ExecutionResult(
            result="error",
            error_message=error_message,
            duration=duration,
            exit_code=returncode,
            exit_reason=exit_reason,
            output=output_tail,
            parsed_result=parsed_result,
            allure_dir=str(allure_dir),
            bridge_error=bridge_error,
        )

    @staticmethod
    def parse_output(output: str) -> ParsedResult | None:
        """
        解析 pytest 终端输出（Day50 任务一）

        参数:
            output (str): 子进程完整输出文本

        返回:
            ParsedResult | None: 结构化解析结果；解析过程意外抛错时返回 None

        异常:
            无（硬契约：解析失败绝不影响执行结果）

        为什么还要在这一层再包一次 try:
            `PytestOutputParser.parse` 自身已设计为不抛异常，但执行器是
            **执行链路的最后一道兜底**——解析器未来新增字段处理时引入的
            任何意外，都不应该让已经跑完的执行丢掉结果。宁可返回 None
            让调用方看到"未解析"，也不能让异常上抛把整批判 failed。
        """
        try:
            return PytestOutputParser.parse(output)
        except Exception as exc:
            logger.warning(
                f"pytest输出解析异常（不影响执行结果）| "
                f"{type(exc).__name__}: {exc}"
            )
            return None

    def bridge_allure_results(self, allure_dir: Path, case: dict) -> str:
        """
        把本次执行的独立 Allure 结果目录桥接到统计库（Day50 任务二）

        参数:
            allure_dir (Path): 本次执行的独立 Allure 结果目录
            case (dict): 待执行用例字典（用于推导批次号与写备注）

        返回:
            str: 桥接失败原因；空串表示桥接成功或**无需桥接**
                （目录不存在 / 目录内无结果文件，均为常态而非故障）

        异常:
            无（桥接是旁路能力，失败只记日志与返回值，不上抛）

        为什么延迟导入 report_analyzer:
            该模块体量较大且带 DB 仓储，而执行器本身在只需要"拼命令 +
            起子进程"的场景（如 CLI 批量执行）也必须可导入。把导入推迟到
            真正桥接时发生，既避免无谓的导入开销，也避免将来报告层调整
            依赖时把执行器拖进循环导入。与 report_analyzer 内部对 db 层的
            延迟导入是同一手法。
        """
        try:
            from src.core.report_analyzer import bridge_results_dir

            bridge = bridge_results_dir(
                allure_dir,
                resolve_bridge_execution_id(case),
                remark=f"pytest执行器桥接 | 用例: {case.get('case_id')}",
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.error(f"Allure结果桥接调用失败 | 目录: {allure_dir} | {error}")
            return error
        if bridge.error:
            return bridge.error
        if bridge.has_result and bridge.statistics is not None:
            logger.info(
                f"Allure结果桥接完成 | 目录: {allure_dir} | "
                f"结果数: {bridge.parsed_count} | 通过: {bridge.statistics.passed} | "
                f"失败: {bridge.statistics.failed} | 跳过: {bridge.statistics.skipped}"
            )
        else:
            logger.debug(f"Allure结果无需桥接（目录不存在或无结果文件）| {allure_dir}")
        return ""

    def kill_process_tree(self, process: subprocess.Popen[str]) -> str:
        """
        清理子进程的整棵进程树（Day49 任务一.1）

        为什么必须显式做（`subprocess.run` 的内部行为不够）:
            run 超时时只 `process.kill()` 直接子进程。pytest 自己 spawn 的
            xdist worker、被测代码再开的子进程会变孤儿继续跑，占端口、占内存，
            且在 Windows 上**不会**随父进程退出而自动结束（POSIX 至少还有
            init 收养+回收）。一批几百条用例跑下来，残留进程会滚雪球。

        参数:
            process (subprocess.Popen[str]): 已在超时分支中持有的子进程句柄

        返回:
            str: 清理结果的可读描述（写入超时消息与日志，便于判断是否真的
                 清干净了，而不是只知道"我们尝试过"）

        异常:
            无（本方法是清理兜底，绝不因清理失败而抛出——清理失败时返回
              描述文本，由调用方拼进 error_message）
        """
        pid = process.pid
        # 句柄是假的（单测替身/MagicMock）时直接跳过：拿不到真实 pid，
        # 调 taskkill 只会得到一条无意义的错误输出，反而污染超时消息
        if not isinstance(pid, int) or pid <= 0:
            return f"跳过（未取得真实 pid: {pid!r}）"

        if os.name == "nt":
            detail = self._kill_tree_windows(pid)
        else:
            detail = self._kill_group_posix(process)

        # 树清掉后直接子进程通常已退出，但仍显式 wait 一次：
        # 不 wait 就返回会让它变成僵尸（POSIX），而僵尸在 /proc 里仍可见，
        # 任何"跑完看看有没有残留"的排查都会被它误导
        try:
            process.wait(timeout=PROCESS_TREE_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            # 极端情况下树清理没能带走直接子进程，单独补一刀
            try:
                process.kill()
                process.wait(timeout=PROCESS_TREE_KILL_GRACE_SECONDS)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                detail = f"{detail}；直接子进程补杀仍未退出(pid={pid})"
        return f"pid={pid} {detail}"

    @staticmethod
    def _kill_tree_windows(pid: int) -> str:
        """
        Windows 侧按父子关系强杀整棵进程树（taskkill /T /F）

        为什么用 taskkill 而不是自己遍历:
            Windows 没有可移植的"父子关系遍历"标准库接口（psutil 要装第三方
            依赖，而本项目明确不引入新依赖）；`taskkill /T` 正是系统自带的
            树杀工具，/T 递归子进程、/F 强制终止。

        参数:
            pid (int): 直接子进程 pid

        返回:
            str: taskkill 的输出摘要（成功为"已清理"，失败带退出码）

        异常:
            无（OSError/超时都被吞掉并转成描述文本）
        """
        try:
            completed = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding=SUBPROCESS_ENCODING,
                errors=SUBPROCESS_ERRORS,
                timeout=TASKKILL_TIMEOUT_SECONDS,
                # 不传 env：taskkill 是系统命令，显式给一份环境反而可能因
                # 继承到非 ASCII 变量名而出问题
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"taskkill 调用失败: {exc}"

        if completed.returncode == 0:
            return "taskkill /T /F 已清理整棵进程树"
        summary = (completed.stdout or completed.stderr or "").strip()
        return f"taskkill 返回码 {completed.returncode}: {truncate_output_tail(summary, 200)}"

    @staticmethod
    def _kill_group_posix(process: subprocess.Popen[str]) -> str:
        """
        POSIX 侧按进程组终止整棵进程树（SIGTERM → SIGKILL）

        为什么 pgid 直接取 pid 而不是 os.getpgid(pid):
            起进程时传了 start_new_session=True，子进程 setsid 后成为
            **会话首进程兼进程组首进程**，其 pgid 恒等于自身 pid。省掉一次
            系统调用之外更重要的是省掉一个 Windows 上不存在的符号
            （os.getpgid），从而让这段逻辑能在 Windows 开发机上被测试覆盖。

        为什么是"先温和后强杀"而不是上来就 SIGKILL:
            被测代码可能在退出钩子里释放端口/落临时文件，一律强杀会让
            "超时导致的端口占用"看起来像被测代码自己的缺陷，误导排查。

        参数:
            process (subprocess.Popen[str]): 子进程句柄（取 pid 作为组 id）

        返回:
            str: 清理结果描述

        异常:
            无（组不存在/权限不足等都被吞掉并转成描述文本）
        """
        kill_group = _POSIX_KILLPG
        if kill_group is None:
            return "非 POSIX 平台，无 os.killpg，跳过进程组清理"
        pgid = process.pid
        try:
            kill_group(pgid, signal.SIGTERM)
        except OSError as exc:
            return f"发送 SIGTERM 失败: {exc}"

        # 给被测代码一点收尾时间，用轮询而非固定 sleep 赌时序
        deadline = time.monotonic() + PROCESS_TREE_TERM_GRACE_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return f"killpg(SIGTERM, pgid={pgid}) 已清理整棵进程树"
            time.sleep(PROCESS_TREE_POLL_INTERVAL_SECONDS)

        try:
            kill_group(pgid, _SIGKILL)
        except OSError as exc:
            return f"发送 SIGKILL 失败: {exc}"
        return f"killpg(SIGTERM→SIGKILL, pgid={pgid}) 已强制清理整棵进程树"


def get_executor(kind: str | None = None) -> BaseExecutor:
    """
    执行器工厂函数

    参数解析优先级: 显式传入kind > 环境变量TM_EXECUTOR > 默认"simulated"。
    kind为None或空白串时视为未显式指定，读TM_EXECUTOR环境变量。

    参数:
        kind (str | None): 执行器类型，可选simulated/pytest；
                           None时读TM_EXECUTOR环境变量（默认simulated）

    返回:
        BaseExecutor: 对应执行器实例（每次调用新建实例，无状态可复用）

    异常:
        ValueError: kind（或TM_EXECUTOR值）不在合法值集合
                    (simulated, pytest)时抛出
    """
    if kind is None or not str(kind).strip():
        kind = str(env_manager.get("TM_EXECUTOR", "simulated"))
    kind = str(kind).strip()

    if kind == "simulated":
        logger.debug("执行器工厂创建: SimulatedExecutor")
        return SimulatedExecutor()
    if kind == "pytest":
        logger.debug("执行器工厂创建: PytestRunner")
        return PytestRunner()

    raise ValueError(
        f"非法执行器类型: {kind!r}，合法取值: {list(VALID_EXECUTORS)}"
    )
