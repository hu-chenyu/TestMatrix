"""
PytestRunner 真实执行器测试（Day48）

覆盖范围（8 条）:
    1. build_command 拼装（1 条）: source_ref 唯一执行目标、命令形状、
       终止符位置、case_id 不进命令
    2. build_command 收口（1 条）: source_ref 为空一律抛 ValueError
                              （**不再回落 script_path**，Day47 过渡分支已移除）
    3. run_one 退出码映射（3 条）: 0→passed / 1→failed / 其他→error
    4. run_one 异常降级（3 条）: 超时、子进程启动失败、source_ref 缺失
    5. 三项可配置项接线（并入上述用例）: cwd 默认项目根、env 叠加注入项、
       timeout 三级优先级与非法值拒绝

测试策略:
    - **全部用 monkeypatch 替换 subprocess.run，不真启动 pytest 子进程**。
      本文件要断言的是"命令拼装 + 退出码映射 + 异常降级"这三段纯逻辑，
      真跑子进程会把断言的不确定性换成 CI 机器的负载、文件系统缓存与
      平台差异——那属于 tests/test_day44_bugfix_demo.py 中"真实子进程"
      用例的职责，两者互补而非重复。
    - 模拟对象用 MagicMock 构造，只提供 returncode / stdout / stderr
      三个被 run_one 真正读取的属性，不臆造其余 CompletedProcess 字段。
    - **零固定 time.sleep**：耗时断言只校验 duration >= 0（与真实执行
      用例的 > 0 互补），不赌任何时序，故本文件可连跑多次零 flaky。
    - 零真实网络 / 硬件 / 数据库依赖。

别名导入约定: 直接 import 模块名 src.core.executors 并以属性访问
subprocess，避免 `from subprocess import run` 造成"打桩打在副本上"——
run_one 内部走的是 subprocess.run 属性查找，副本不会被替换到。
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import allure
import pytest
from src.common.env_manager import PROJECT_ROOT
from src.core import executors as executors_mod
from src.core.executors import (
    PYTEST_TIMEOUT_SECONDS,
    PytestRunner,
)

# 供各用例复用的合法 source_ref（形态须过 validate_source_ref）
VALID_SOURCE_REF = "tests/api_demo/test_api_login.py"

# 库中常见的用例编号
CASE_ID = "TM-PR-0001"


class FakeRun:
    """
    subprocess.run 的可控替身

    记录调用参数并返回预置结果（或抛出预置异常），供各用例断言
    "命令拼装成了什么样"与"cwd/env/timeout 有没有真的传下去"。
    """

    def __init__(self, result: object = None, exc: BaseException | None = None) -> None:
        """
        构造替身

        参数:
            result (object): subprocess.run 的返回值（模拟 CompletedProcess）
            exc (BaseException | None): 要抛出的异常；非 None 时忽略 result
        """
        self.result = result
        self.exc = exc
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, command: list, **kwargs: object) -> object:
        """记录本次调用并返回预置结果或抛出预置异常"""
        self.calls.append((command, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture
def fake_completed() -> MagicMock:
    """
    构造一个只带 run_one 真正读取的三个属性的 CompletedProcess 替身

    返回:
        MagicMock: returncode/stdout/stderr 三个属性可写
    """
    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = ""
    completed.stderr = ""
    return completed


@pytest.fixture
def patch_subprocess(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    """
    把 subprocess.run 换成可断言的 FakeRun 并自动还原

    参数:
        monkeypatch (pytest.MonkeyPatch): pytest 自动还原补丁的夹具

    返回:
        FakeRun: 替身实例，测试内可直接读 calls 断言调用参数
    """
    fake = FakeRun()
    monkeypatch.setattr(executors_mod.subprocess, "run", fake)
    return fake


@allure.feature("Day48 真实执行器")
class TestPytestRunnerBuildCommand:
    """命令拼装：执行目标取值与命令形状"""

    @allure.story("source_ref 是唯一执行目标且命令形状固定")
    def test_build_command_source_ref_priority(self) -> None:
        """
        命令必须取 source_ref，且形状为
        [sys.executable, "-m", "pytest", "-q", "--tb=short", "--", path]

        终止符 "--" 必须在所有选项之后、紧邻 path 之前：其后元素一律
        作位置参数，写错会让 "-q" 被当成路径并以退出码 4 收场。
        同时断言 case_id 绝不进命令——业务编号不是文件路径。
        """
        command = PytestRunner().build_command(
            {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}
        )

        assert command == [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=short",
            "--",
            VALID_SOURCE_REF,
        ], "命令形状必须与约定完全一致"
        assert command[-1] == VALID_SOURCE_REF, "path 必须是终止符后的唯一位置参数"
        assert CASE_ID not in command, "case_id 是业务编号，绝不能进 pytest 命令"
        # 显式传 script_path 也不得干扰 source_ref 取值
        shadowed = PytestRunner().build_command(
            {
                "case_id": CASE_ID,
                "source_ref": VALID_SOURCE_REF,
                "script_path": "tests/legacy_should_not_win.py",
            }
        )
        assert shadowed[-1] == VALID_SOURCE_REF, "source_ref 必须压过 script_path"

    @allure.story("source_ref 为空一律抛 ValueError（Day48 移除 script_path 回落）")
    def test_build_command_empty_source_ref_raises(self) -> None:
        """
        None / 空串 / 纯空格三种空值都要报错，且报错消息点名字段与用例编号。

        关键回归点: 只带 script_path 的用例现在**也**必须报错——Day47
        的过渡兼容分支已在 Day48 随回填收口移除，若误留，该用例会静默
        继续拼出一个命令，掩盖"执行目标已彻底收口到 source_ref"这件事。
        """
        for empty_value in (None, "", "   "):
            with pytest.raises(ValueError) as excinfo:
                PytestRunner().build_command(
                    {"case_id": CASE_ID, "source_ref": empty_value}
                )
            message = str(excinfo.value)
            assert "source_ref" in message, f"异常消息必须点名字段: {message}"
            assert CASE_ID in message, f"异常消息必须指明是哪条用例: {message}"

        # 仅带 script_path 的旧调用方现在必须显式失败，而不是被静默兜住
        with pytest.raises(ValueError) as excinfo:
            PytestRunner().build_command(
                {"case_id": CASE_ID, "script_path": VALID_SOURCE_REF}
            )
        assert "source_ref" in str(excinfo.value), "script_path 已不再是合法执行目标"


@allure.feature("Day48 真实执行器")
class TestPytestRunnerRunOne:
    """run_one：退出码映射、异常降级与三项可配置项接线"""

    @allure.story("退出码 0 → passed，且 cwd/env/timeout 已传给 subprocess")
    def test_run_one_passed(
        self, patch_subprocess: FakeRun, fake_completed: MagicMock
    ) -> None:
        """
        通过场景：result=passed、error_message 为 None、耗时非负。

        同时借这条用例验证三项可配置项真的传到了 subprocess.run：
        cwd 必须是项目根（pytest 靠它定位相对路径用例与 pytest.ini）、
        env 必须是"当前环境 + 注入项"的合并结果、timeout 取构造参数值。
        这三项一旦漏传，真实执行会在 Web 触发/worker 场景下全量落
        error，但纯退出码断言完全看不出来。
        """
        fake_completed.returncode = 0
        fake_completed.stdout = "1 passed in 0.12s"
        patch_subprocess.result = fake_completed
        case = {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}

        runner = PytestRunner(timeout=7, env={"TM_DAY48_MARK": "1"})
        result = runner.run_one(case)

        assert result.result == "passed"
        assert result.error_message is None
        assert result.duration >= 0.0, "耗时必须是真实测量值而非硬编码 0"

        # 命令里不能出现用于识别这条用例的编号
        command, kwargs = patch_subprocess.calls[0]
        assert command[-1] == VALID_SOURCE_REF
        assert CASE_ID not in command

        # 三项可配置项接线
        assert Path(str(kwargs["cwd"])) == PROJECT_ROOT, "默认工作目录必须是项目根"
        assert kwargs["timeout"] == 7, "超时必须取构造参数值"
        merged_env = kwargs["env"]
        assert isinstance(merged_env, dict)
        assert merged_env["TM_DAY48_MARK"] == "1", "注入的环境变量必须生效"
        assert "PATH" in merged_env, "必须继承当前进程环境，而不是只留注入项"

    @allure.story("退出码 1 → failed，error_message 含输出尾部")
    def test_run_one_failed(
        self, patch_subprocess: FakeRun, fake_completed: MagicMock
    ) -> None:
        """
        失败场景：pytest 约定退出码 1 表示存在失败用例，详情在 stdout。

        断言 error_message 带上真实输出内容（而非泛化文案），否则报告里
        每条失败都只写"pytest退出码: 1"，排障还得回机器复跑一遍。
        """
        detail = "tests/test_login.py::test_wrong_password AssertionError: boom"
        fake_completed.returncode = 1
        fake_completed.stdout = f"FAILURES\n{detail}\n1 failed, 2 passed in 1.20s"
        fake_completed.stderr = ""
        patch_subprocess.result = fake_completed

        result = PytestRunner().run_one(
            {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}
        )

        assert result.result == "failed", "退出码 1 必须映射为 failed 而非 error"
        assert result.error_message, "failed 必须带失败详情"
        assert detail in result.error_message, "error_message 必须含失败输出尾部"
        assert result.duration >= 0.0

    @allure.story("退出码 2 → error，error_message 含 stderr 尾部")
    def test_run_one_error_exit_code(
        self, patch_subprocess: FakeRun, fake_completed: MagicMock
    ) -> None:
        """
        非 0/1 的退出码（用法错误 2、中断 5 等）必须映射为 error。

        与 failed 严格区分：failed 是"用例断言不通过"（业务结论），
        error 是"这次执行本身没跑成"（工具结论），两者在报告统计与
        通知分级里的口径不同，混在一起会让通过率失真。
        """
        stderr_detail = "ERROR: file or directory not found: tests/missing.py"
        fake_completed.returncode = 2
        fake_completed.stdout = ""
        fake_completed.stderr = stderr_detail
        patch_subprocess.result = fake_completed

        result = PytestRunner().run_one(
            {"case_id": CASE_ID, "source_ref": "tests/missing.py"}
        )

        assert result.result == "error", "非 0/1 退出码必须映射为 error"
        assert result.error_message, "error 必须带原因"
        assert stderr_detail in result.error_message, "error_message 必须含 stderr 尾部"
        assert result.duration >= 0.0

    @allure.story("超时预算三级优先级与超时降级")
    def test_run_one_timeout(
        self,
        patch_subprocess: FakeRun,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        超时必须降级为 error 结果而不是抛异常。

        编排层逐条调用 run_one，异常上抛会把整批执行带崩且丢失失败原因；
        消息里要带超时阈值与完整命令，否则复现时不知道当时的预算是多少。

        同时覆盖预算的三级优先级（构造参数 > TM_PYTEST_TIMEOUT > 模块常量）
        与非法值的显式拒绝：静默回落会让"我明明把预算调到 5 秒"却仍按 30
        秒跑，而超时消息里的阈值看起来又是合法的，排查时完全看不出配置
        没生效，故非法值一律抛错而不是回落。
        """
        fake_timeout = 5
        patch_subprocess.exc = subprocess.TimeoutExpired(
            cmd="pytest", timeout=fake_timeout
        )

        runner = PytestRunner(timeout=fake_timeout)
        result = runner.run_one({"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF})

        assert result.result == "error", "超时不抛异常，降级为 error 结果"
        assert "超时" in (result.error_message or ""), "消息必须说明是超时"
        assert f"执行超时(>{fake_timeout}s)" in result.error_message, (
            "超时消息必须带实际生效的阈值，便于复现"
        )
        assert result.duration >= 0.0

        # 环境变量可覆盖预算（运维改配置即可，不必改代码）
        monkeypatch.setenv("TM_PYTEST_TIMEOUT", "11")
        assert PytestRunner().timeout == 11, "TM_PYTEST_TIMEOUT 应覆盖模块常量"
        assert PytestRunner(timeout=3).timeout == 3, "显式构造参数优先级最高"

        # 纯空白的环境变量值视为未配置，回落到模块常量
        monkeypatch.setenv("TM_PYTEST_TIMEOUT", "   ")
        assert PytestRunner().timeout == PYTEST_TIMEOUT_SECONDS

        # 非法预算一律显式拒绝，不静默回落
        monkeypatch.setenv("TM_PYTEST_TIMEOUT", "not-a-number")
        with pytest.raises(ValueError, match="TM_PYTEST_TIMEOUT"):
            PytestRunner()
        for illegal in (0, -1, True):
            with pytest.raises(ValueError, match="正整数"):
                PytestRunner(timeout=illegal)  # type: ignore[arg-type]

    @allure.story("子进程启动失败 → error，消息含启动失败原因")
    def test_run_one_os_error(self, patch_subprocess: FakeRun) -> None:
        """
        解释器不存在 / 权限不足等 OSError 必须降级为 error 结果。

        这类故障的表现是"整批每条都同样地启动失败"，若上抛会让批次直接
        failed 且看不出根因是环境问题而非用例问题。
        """
        patch_subprocess.exc = OSError(2, "No such file or directory")

        result = PytestRunner().run_one(
            {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}
        )

        assert result.result == "error", "子进程启动失败必须降级为 error"
        assert "启动失败" in (result.error_message or ""), "消息必须点明是启动失败"
        assert "No such file or directory" in result.error_message, (
            "必须保留底层 OSError 原文，否则无法判断是路径错还是权限错"
        )
        assert result.duration >= 0.0

    @allure.story("source_ref 缺失 → 降级为单条 error，且不启动子进程")
    def test_run_one_build_command_failure_degraded(self) -> None:
        """
        缺少执行目标时 run_one 不得抛异常，且**根本不该走到 subprocess**。

        两条都要断言：上抛会让整批判 failed（配置问题升级成批次故障）；
        而若仍去启动子进程，pytest 会拿空路径报 file or directory not
        found 并以退出码 4 收场，error_message 说的是"路径错了"，
        把"这条用例本就不该走 pytest"这一真实原因彻底掩盖。
        """
        result = PytestRunner().run_one({"case_id": CASE_ID, "source_ref": None})

        assert result.result == "error", "缺少执行目标必须降级为该条用例的 error"
        assert "source_ref" in (result.error_message or ""), (
            "error_message 必须点明缺的是 source_ref，而不是伪装成路径错误"
        )
        assert CASE_ID in result.error_message, "error_message 必须指明是哪条用例"
        assert result.duration >= 0.0
