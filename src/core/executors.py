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

设计说明:
    - 策略模式: 编排代码（CaseManager._execute_batch_async）只依赖
      BaseExecutor抽象契约，不感知具体执行器实现
    - 依赖倒置: 高层编排模块不直接依赖低层执行细节，二者都依赖
      抽象接口；后续Day32换Redis任务队列、Day52-79接入真实pytest
      执行器时，编排代码零改动
    - 不引入新第三方依赖: subprocess为标准库
"""

import os
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from src.common.env_manager import PROJECT_ROOT, env_manager
from src.common.logger import LogManager
from src.common.source_ref import validate_source_ref

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

    退出码映射（pytest约定）:
        0 → passed（全部通过）
        1 → failed（存在失败用例）
        其他（2/5等） → error（用法错误/内部错误/中断）
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
        拼装单用例的pytest执行命令（Day48 收口：只读 source_ref）

        source_ref 为空即抛 ValueError，**不回落到 case_id，也不回落到
        script_path**（Day47 的过渡兼容分支已随回填收口移除）。

        为什么 source_ref 为空必须报错而不是找个值顶上: 空值语义
        "不可被 pytest 执行"是该列存在的理由，回落到 case_id 会把这条
        语义抹平——pytest 拿业务编号当路径，报 file or directory not
        found 并以退出码 4 收场，每条用例落入 error 分支，排障时看到
        的却是"路径错了"而非"该用例本就不该走 pytest"。

        参数:
            case (dict): 待执行用例字典（source_ref 为可选键）

        返回:
            list[str]: 命令参数列表，形如
                [sys.executable, "-m", "pytest", "-q", "--tb=short", "--", path]

        异常:
            ValueError: 以下两种情况抛出，消息均说明该补什么/改什么
                - source_ref 为空（None / 空串 / 纯空格）
                - source_ref 非空但格式非法 / 超长 / 含 ".."
                  （执行侧纵深防御，详见函数体注释）
        """
        case_id = str(case.get("case_id", ""))
        source_ref = case.get("source_ref")
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
            path = validate_source_ref(path, context=f"用例 {case_id}: ")
        except ValueError as exc:
            raise ValueError(
                f"{exc}（该值来自库中 source_ref，非法值不会进入 pytest 命令；"
                f"请修正后重试）"
            ) from exc

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
        #   保持 "--" 的目的不变：阻断路径形如 "--version" 时被 pytest
        #   当选项执行、退出码 0 被误判为 passed 的假通过。
        return [sys.executable, "-m", "pytest", "-q", "--tb=short", "--", path]

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
        同步执行单条用例的pytest子进程

        执行流程:
            1. build_command拼装命令（source_ref 为空时抛 ValueError，
               此处捕获后降级为该条用例的 error 结果）
            2. subprocess.run同步执行，工作目录取 self.cwd（默认项目根），
               环境变量为 self.env 叠加当前进程环境，超时取 self.timeout
            3. 按退出码映射结果: 0→passed / 1→failed / 其他→error
            4. 失败/错误时输出截断2000字存入error_message

        参数:
            case (dict): 待执行用例字典

        返回:
            ExecutionResult: result为passed/failed/error；
                             failed/error时error_message非空
                             （超时/命令不存在/启动参数非法/
                              source_ref为空同样映射error）

        异常:
            无（命令拼装失败与子进程异常均转为error结果返回，不向上抛，
            保持"单条用例的失败不升级为整批failed"的既有契约）

            被本方法兜住并降级为 error 的全部异常类型：
                - ValueError: build_command 侧的执行目标缺失/格式非法
                - subprocess.TimeoutExpired: 子进程超时
                - OSError: 命令不存在/权限不足/工作目录不存在
                - TypeError / ValueError: **子进程启动参数非法**
                  （Day48-fix2 补齐，如 env 含非字符串值、env 键含 "="、
                  cwd 含 NUL）——这两类此前会直接上抛穿透本方法
        """
        start_time = time.perf_counter()
        try:
            command = self.build_command(case)
        except ValueError as exc:
            # 编排层逐条 run_one，任何异常上抛都会让整批判 failed；
            # 而"source_ref 为空"是**单条用例的配置问题**、不是批次故障，
            # 必须降级为该条用例的 error 结果，让批次继续跑完其余用例。
            duration = time.perf_counter() - start_time
            error_message = str(exc)
            logger.warning(
                f"pytest执行目标缺失（该用例不可被 pytest 执行）| "
                f"用例: {case.get('case_id')} | {exc}"
            )
            return ExecutionResult(
                result="error", error_message=error_message, duration=duration
            )

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding=SUBPROCESS_ENCODING,
                errors=SUBPROCESS_ERRORS,
                timeout=self.timeout,
                # pytest 靠 cwd 定位测试文件与 pytest.ini：不显式指定时
                # 继承调用进程的工作目录，而 Web 触发 / 任务队列 worker
                # 的 cwd 与项目根无关，source_ref 里的相对路径会全部
                # 找不到，每条用例以退出码 4 落 error。
                cwd=str(self.cwd),
                env=self._build_env(),
            )
        except subprocess.TimeoutExpired:
            duration = time.perf_counter() - start_time
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
            error_message = f"执行超时(>{self.timeout}s): {' '.join(command)}"
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
        except (TypeError, ValueError) as exc:
            # 子进程**启动参数**非法（Day48-fix2 兜底层）
            #
            # 实测逃逸面（Windows + Python 3.11.9 实测复现）：这些异常由
            # subprocess.run 在真正创建进程**之前**抛出，既不是
            # TimeoutExpired 也不是 OSError，此前直接上抛穿透 run_one：
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
