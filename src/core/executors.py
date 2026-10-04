"""
用例执行器抽象层（策略模式 + 依赖倒置）

功能（第三阶段Day24交付）:
    - ExecutionResult          单用例执行结果数据类
      （result/error_message/duration三字段）
    - BaseExecutor             执行器抽象基类（统一执行契约）
    - SimulatedExecutor        模拟执行器（0.01s耗时 + case_id末位
                              奇偶定通过/失败，与既有_simulate_execute
                              规则完全一致）
    - PytestRunner             真实pytest执行器骨架（本日仅搭
                              退出码解析结构，不真跑测试集）
    - get_executor             执行器工厂（读TM_EXECUTOR环境变量，
                              默认simulated）

设计说明:
    - 策略模式: 编排代码（CaseManager._execute_batch_async）只依赖
      BaseExecutor抽象契约，不感知具体执行器实现
    - 依赖倒置: 高层编排模块不直接依赖低层执行细节，二者都依赖
      抽象接口；后续Day32换Redis任务队列、Day52-79接入真实pytest
      执行器时，编排代码零改动
    - 不引入新第三方依赖: subprocess为标准库
"""

import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.common.env_manager import env_manager
from src.common.logger import LogManager

logger = LogManager.get_logger()

# 执行器类型合法值（工厂与Web入参校验共用，单一事实来源）
VALID_EXECUTORS = ("simulated", "pytest")

# pytest子进程执行超时时间（秒）
PYTEST_TIMEOUT_SECONDS = 30

# 子进程输出截断长度（防超长堆栈撑爆error_message与库表）
OUTPUT_TRUNCATE_LENGTH = 2000

# 子进程输出解码策略：pytest 输出含中文用例名/路径，Windows 默认 cp936
# 遇 UTF-8 字节会抛 UnicodeDecodeError（而 run_one 只捕 TimeoutExpired/
# OSError，异常会穿透到批次级 except 导致整批 failed）。errors="replace"
# 把无法解码的字节替换为 � 而非抛异常，保证单条用例的编码问题不会
# 升级成整批失败。
SUBPROCESS_ENCODING = "utf-8"
SUBPROCESS_ERRORS = "replace"


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


@dataclass
class ExecutionResult:
    """
    单用例执行结果数据类

    执行器run_one的统一返回契约，编排层据此写test_executions明细。

    属性:
        result (str): 执行结果，可选passed/failed/error/skipped
        error_message (str | None): 失败/错误时的异常信息
                                    （passed时为None）
        duration (float): 执行耗时（秒，浮点，支持亚秒精度）
    """

    result: str
    error_message: str | None = None
    duration: float = 0.0


class BaseExecutor(ABC):
    """
    执行器抽象基类（策略接口）

    定义单用例执行的统一契约: 输入用例字典，输出ExecutionResult。
    所有具体执行器（模拟/真实pytest/未来的板卡执行器）实现此接口，
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
    真实pytest执行器（骨架版）

    本日仅搭命令拼装与退出码解析结构，不真跑测试集
    （真实开源项目测试集执行另行安排）。

    退出码映射（pytest约定）:
        0 → passed（全部通过）
        1 → failed（存在失败用例）
        其他（2/5等） → error（用法错误/内部错误/中断）
    """

    def build_command(self, case: dict) -> list:
        """
        拼装单用例的pytest执行命令

        参数:
            case (dict): 待执行用例字典

        返回:
            list: 命令参数列表，如
                  [sys.executable, "-m", "pytest", "-q", "--tb=short", "--", path]

        异常:
            无
        """
        # 测试文件路径：优先用 script_path，回落 case_id
        path = str(case.get("script_path") or case.get("case_id", ""))
        # sys.executable 指向当前解释器（虚拟环境下即 venv 的 python，
        # 保证用项目依赖跑 pytest），跨 Windows/Linux/macOS 通用；
        # 修前硬编码的 "py" 是 Windows 专属启动器，非 Windows 平台
        # 每条用例都会 OSError 降级为 error。
        #
        # 选项终止符 "--" 的位置（Day44 P1-01）：
        #   "--" 之后 pytest 的 argparse 停止解析选项，其后**全部**元素
        #   一律作为位置参数（测试文件路径）。因此它必须放在**所有选项之后、
        #   紧邻 path 之前**：
        #       [.., "pytest", "-q", "--tb=short", "--", path]   ← 正确
        #       [.., "pytest", "--", path, "-q", "--tb=short"]   ← 错误
        #   错误写法会让 "-q" 与 "--tb=short" 同样被当成路径，pytest 报
        #   "file or directory not found: -q" 并以退出码 4 收场，每条用例
        #   都落入 error 分支——真实执行链路功能性不可用。
        #   保持 "--" 的目的不变：阻断 case_id 形如 "--version" 时被 pytest
        #   当选项执行、退出码 0 被误判为 passed 的假通过。
        return [sys.executable, "-m", "pytest", "-q", "--tb=short", "--", path]

    def run_one(self, case: dict) -> ExecutionResult:
        """
        同步执行单条用例的pytest子进程

        执行流程:
            1. build_command拼装命令
            2. subprocess.run同步执行，超时30秒
            3. 按退出码映射结果: 0→passed / 1→failed / 其他→error
            4. 失败/错误时输出截断2000字存入error_message

        参数:
            case (dict): 待执行用例字典

        返回:
            ExecutionResult: result为passed/failed/error；
                             failed/error时error_message非空
                             （超时/命令不存在同样映射error）

        异常:
            无（子进程异常均转为error结果返回，不向上抛）
        """
        command = self.build_command(case)
        start_time = time.perf_counter()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding=SUBPROCESS_ENCODING,
                errors=SUBPROCESS_ERRORS,
                timeout=PYTEST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            duration = time.perf_counter() - start_time
            error_message = (
                f"执行超时(>{PYTEST_TIMEOUT_SECONDS}s): {' '.join(command)}"
            )
            logger.warning(f"pytest子进程超时 | {' '.join(command)}")
            return ExecutionResult(
                result="error", error_message=error_message, duration=duration
            )
        except OSError as exc:
            # 命令不存在/权限不足等子进程启动异常（如py不在PATH）
            duration = time.perf_counter() - start_time
            error_message = f"pytest子进程启动失败: {exc}"
            logger.error(f"{error_message} | 命令: {command}")
            return ExecutionResult(
                result="error", error_message=error_message, duration=duration
            )
        duration = time.perf_counter() - start_time

        # 退出码映射: 0→passed / 1→failed / 其他→error
        if completed.returncode == 0:
            logger.debug(
                f"pytest执行通过 | 用例: {case.get('case_id')} | 耗时: {duration:.3f}s"
            )
            return ExecutionResult(
                result="passed", error_message=None, duration=duration
            )

        if completed.returncode == 1:
            # pytest约定退出码1: 存在失败用例，失败详情在stdout
            output = (completed.stdout or "").strip() or (completed.stderr or "").strip()
            message = truncate_output_tail(output) or (
                f"pytest退出码: {completed.returncode}"
            )
            logger.debug(f"pytest执行失败 | 用例: {case.get('case_id')}")
            return ExecutionResult(
                result="failed", error_message=message, duration=duration
            )

        # 其他退出码(2/5等): 用法错误/内部错误/中断，详情在stderr
        stderr = (completed.stderr or "").strip()
        message = truncate_output_tail(stderr) or (
            f"pytest异常退出，退出码: {completed.returncode}"
        )
        logger.warning(
            f"pytest异常退出 | 用例: {case.get('case_id')} | "
            f"退出码: {completed.returncode}"
        )
        return ExecutionResult(
            result="error", error_message=message, duration=duration
        )


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
