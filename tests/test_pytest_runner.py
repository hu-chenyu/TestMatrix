"""
PytestRunner 真实执行器测试（Day48 建立，Day49 扩充）

覆盖范围（21 条）:
    1. build_command 拼装（1 条）: source_ref 唯一执行目标、命令形状、
       终止符位置、case_id 不进命令
    2. build_command 收口（1 条）: source_ref 为空一律抛 ValueError
                              （**不再回落 script_path**，Day47 过渡分支已移除）
    3. pytest 选择器透传（4 条，Day49）: -m/-k 透传、path 字段优先级与
       存在性校验、非法 selectors/path 的拒绝矩阵、两个纯函数直测
    4. run_one 退出码映射（4 条）: 0→passed / 1→failed / 其余→error，
       **外加 Day49 的 exit_code + exit_reason 细粒度分类**（2/3/4/5 与未知值）
    5. run_one 异常降级（5 条）: 超时（含输出保留）、子进程启动失败、
       启动参数非法、source_ref 缺失
    6. 进程树清理（5 条，Day49）: 假 pid 守卫 / Windows taskkill 分支 /
       POSIX killpg 的 SIGTERM 与 SIGKILL 分支 / 直接子进程补杀兜底
    7. 进程树清理真实端到端（1 条，Day49）: 真实拉起 pytest，其 spawn 出的
       孙进程在超时后确认已被清理，无僵尸残留

测试策略:
    - 绝大多数用例用 monkeypatch 替换 `subprocess.Popen`，不真启动子进程。
      本文件要断言的是"命令拼装 + 退出码映射 + 异常降级 + 清理逻辑"这四段
      纯逻辑，真跑子进程会把断言的不确定性换成 CI 机器的负载、文件系统缓存
      与平台差异——真跑子进程是 Day44 用例的职责，两者互补而非重复。
    - **唯一的例外是 Day49 新增的进程树端到端用例**：清理"是否真的发生"
      只能靠真实进程证明，mock 掉就等于用被测逻辑证明被测逻辑。
    - 模拟对象用自写 FakePopen，只提供 run_one/清理逻辑真正读取的属性
      （pid/returncode/stdout/stderr/communicate/wait/poll/kill），
      不臆造其余 CompletedProcess/Popen 字段。
    - **零固定 time.sleep**：耗时断言只校验 duration >= 0（与真实执行用例的
      > 0 互补），进程存活一律用轮询等待终态，不赌任何时序。
    - 零真实网络 / 硬件 / 数据库依赖。

别名导入约定: 直接 import 模块名 src.core.executors 并以属性访问
subprocess，避免 `from subprocess import Popen` 造成"打桩打在副本上"——
run_one 内部走的是 subprocess.Popen 属性查找，副本不会被替换到。
"""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import allure
import pytest
from src.common.env_manager import PROJECT_ROOT
from src.core import executors as executors_mod
from src.core.executors import (
    EXIT_REASON_INTERNAL_ERROR,
    EXIT_REASON_INTERRUPTED,
    EXIT_REASON_INVALID_ARGUMENTS,
    EXIT_REASON_INVALID_CASE,
    EXIT_REASON_NO_TESTS_COLLECTED,
    EXIT_REASON_SPAWN_FAILED,
    EXIT_REASON_TESTS_FAILED,
    EXIT_REASON_TIMEOUT,
    EXIT_REASON_UNKNOWN_EXIT_CODE,
    EXIT_REASON_USAGE_ERROR,
    OUTPUT_TRUNCATE_LENGTH,
    PROCESS_TREE_KILL_GRACE_SECONDS,
    PROCESS_TREE_POLL_INTERVAL_SECONDS,
    PYTEST_TIMEOUT_SECONDS,
    PytestRunner,
    decode_subprocess_stream,
    truncate_output_tail,
    validate_target_path,
)

# 供各用例复用的合法 source_ref（形态须过 validate_source_ref）
VALID_SOURCE_REF = "tests/api_demo/test_api_login.py"

# 库中常见的用例编号
CASE_ID = "TM-PR-0001"

# 进程树端到端用例的探针脚本（在子目录里跑 pytest，避免加载项目 pytest.ini
# 的 --alluredir/--clean-alluredir 与外层跑的这一份抢同一个报告目录）
TREE_PROBE_SOURCE = '''\
"""Day49 进程树探针：模块导入期就拉起孙进程，随后长时间挂起"""

import pathlib
import subprocess
import sys
import time

# 为什么放在模块导入期（收集阶段）而不是 test 函数里:
#   外层给的超时预算有限，若孙进程在测试函数开始后才拉起，清理动作可能
#   发生在孙进程存在之前，"清理成功"就成了"压根没生成过"的假通过。
#   收集阶段拉起可把"孙进程一定存在"这件事变成可控前提。
GRANDCHILD = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
pathlib.Path("grandchild.pid").write_text(str(GRANDCHILD.pid), encoding="utf-8")


def test_hang():
    """挂起直到被外层超时清理"""
    time.sleep(120)
'''

# 端到端用例的超时预算（秒）。
#
# 取 8 秒而非更小的理由: 预算要覆盖"子进程 pytest 冷启动 + 收集 + 导入探针"
# 整段，CI 上冷启动含插件加载可达数秒；预算太短会让探针来不及拉起孙进程，
# 用例变成随机失败。探针本身睡 120 秒，远大于任何合理的启动耗时。
TREE_PROBE_TIMEOUT_SECONDS = 8

# 清理后等待"孙进程确实消失"的轮询上限（秒）与间隔（秒）。
# 轮询而非固定 sleep：强杀后进程回收存在调度延迟，固定等待要么不够
# （假失败）要么白白拖长（固定等待足够）。
TREE_GONE_POLL_SECONDS = 5.0
TREE_GONE_POLL_INTERVAL_SECONDS = 0.05


def _pid_alive(pid: int) -> bool:
    """
    判断进程是否仍存活（跨平台，纯标准库）

    为什么不能用 `os.kill(pid, 0)` 一把梭:
        POSIX 下 `os.kill(pid, 0)` 只做存在性探测，安全；但 **Windows 上
        os.kill 是 TerminateProcess**——`os.kill(pid, 0)` 会真的把目标
        进程杀掉并置退出码 0，探测函数反而成了杀手，测出来的"已清理"
        是我们自己动手的结果。

    为什么还要把僵尸(Z)算作已死:
        被 kill 的孙进程若尚未被其新父进程回收，会停在僵尸态，
        `os.kill(pid, 0)` 对僵尸仍返回成功。不把 Z 判成已死的话，
        Linux CI 上这条断言会随机失败，且失败信息极具误导性
        （"没清理干净"其实只是"还没被 init 回收"）。

    参数:
        pid (int): 待探测的进程号

    返回:
        bool: True=进程仍在运行；False=不存在或已是僵尸

    异常:
        无
    """
    if sys.platform == "win32":
        import ctypes

        # PROCESS_QUERY_LIMITED_INFORMATION：只查询、不终止，最小权限
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                # 查不到退出码时无法判定，保守认为它还在
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    # POSIX: /proc 可判定僵尸态；无 /proc 的平台（如 macOS）退回存在性探测
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        stat_text = ""
    if stat_text:
        # comm 字段可能含空格与括号，取最后一个 ")" 之后的状态位
        tail = stat_text.rsplit(")", 1)[-1].split()
        if tail and tail[0] == "Z":
            return False
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在，只是我们没权限查
    return True


def _wait_process_gone(pid: int, limit: float = TREE_GONE_POLL_SECONDS) -> bool:
    """
    轮询等待进程消失，返回是否在限期内消失

    为什么是轮询而不是"杀完立刻查一次":
        强杀只是向内核递了一个终止请求，进程真正被回收要等调度。
        杀完立刻查会在慢机器上读到"还活着"，把清理成功的实现判成失败
        （假失败）。反过来用固定 sleep 睡够 2 秒，则会让这条用例在
        明明已经清理干净时也白等 2 秒。
    """
    import time as _time

    deadline = _time.monotonic() + limit
    while _time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        _time.sleep(TREE_GONE_POLL_INTERVAL_SECONDS)
    return not _pid_alive(pid)


class FakePopen:
    """
    subprocess.Popen 的可控替身

    记录调用参数并返回预置输出（或抛出预置异常），供各用例断言
    "命令拼装成了什么样"、"cwd/env 传没传下去"、"超时清理有没有接上"。
    """

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        exc: BaseException | None = None,
        wait_exc: BaseException | None = None,
        pid: object = 0,
        poll_return: int | None = 0,
    ) -> None:
        """
        构造替身

        参数:
            stdout (str): communicate 返回的 stdout
            stderr (str): communicate 返回的 stderr
            returncode (int): 进程退出码
            exc (BaseException | None): communicate 要抛出的异常（超时模拟）
            wait_exc (BaseException | None): wait 要抛出的异常（补杀分支模拟）
            pid (object): 进程号。**默认可刻意给 0**，让清理逻辑走
                          "未取得真实 pid"的守卫分支，避免单测误杀真实进程
            poll_return (int | None): poll() 的返回值。**做成实例字段而不是
                          在测试里改类属性**——改类会污染同文件后续所有用例
                          （pytest 单进程复用模块级类），表现为"某个测试改
                          了 FakePopen，后面无关的测试莫名挂掉"
        """
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.pid = pid
        self.poll_return = poll_return
        self._exc = exc
        self._wait_exc = wait_exc
        self.communicate_calls: list[float | None] = []
        self.wait_calls: list[float | None] = []
        self.kill_called = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        """记录本次 communicate 并返回预置输出或抛出预置异常"""
        self.communicate_calls.append(timeout)
        if self._exc is not None:
            raise self._exc
        return self.stdout, self.stderr

    def wait(self, timeout: float | None = None) -> int:
        """记录本次 wait 并按预置配置抛出异常"""
        self.wait_calls.append(timeout)
        if self._wait_exc is not None:
            raise self._wait_exc
        return self.returncode

    def poll(self) -> int | None:
        """返回预置的轮询状态（None 表示进程仍活着）"""
        return self.poll_return

    def kill(self) -> None:
        """记录补杀动作"""
        self.kill_called = True


class FakeSpawner:
    """
    subprocess.Popen 的可断言替身入口

    与 FakePopen 分开是因为两者职责不同：FakePopen 扮演"子进程"，
    FakeSpawner 扮演"造子进程的那次调用"——调用参数（cwd/env）记在它身上。
    """

    def __init__(
        self, process: FakePopen | None = None, exc: BaseException | None = None
    ) -> None:
        """
        构造替身入口

        参数:
            process (FakePopen | None): 每次调用返回的子进程替身；None 时新建
            exc (BaseException | None): 要在**起进程这一步**抛出的异常
                                        （模拟启动失败/参数非法）
        """
        self.process = process if process is not None else FakePopen()
        self.exc = exc
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, command: list, **kwargs: Any) -> FakePopen:
        """记录本次调用并返回预置子进程或抛出预置异常"""
        self.calls.append((command, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.process


@pytest.fixture
def patch_subprocess(monkeypatch: pytest.MonkeyPatch) -> FakeSpawner:
    """
    把 subprocess.Popen 换成可断言的 FakeSpawner 并自动还原

    参数:
        monkeypatch (pytest.MonkeyPatch): pytest 自动还原补丁的夹具

    返回:
        FakeSpawner: 替身实例，测试内可直接读 calls 断言调用参数
    """
    fake = FakeSpawner()
    monkeypatch.setattr(executors_mod.subprocess, "Popen", fake)
    return fake


@pytest.fixture
def fake_popen() -> FakePopen:
    """
    构造一个只带清理逻辑真正读取的属性的 FakePopen

    返回:
        FakePopen: pid/returncode/stdout/stderr/communicate/wait 可控
    """
    return FakePopen(pid=0)


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

        Day49 追加的回归: 即便调用方新增了 path 字段，只要 path 也没给，
        空目标依然必须报错——**不引入"默认测试目录"回落**，否则 Day48
        刚堵上的"每条未回填用例都去跑全量 tests/"缺陷会被重新打开。
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

        # 只有 markers/keyword、没有执行目标 → 同样必须报错（不得回落默认目录）
        with pytest.raises(ValueError) as excinfo:
            PytestRunner().build_command({"case_id": CASE_ID, "markers": "smoke"})
        assert "source_ref" in str(excinfo.value), "只有选择器也算无可执行目标"

    @allure.story("Day49：-m / -k 选择器透传且排在终止符之前")
    def test_build_command_selector_passthrough(self) -> None:
        """
        markers/keyword 必须透传为 -m/-k，且全部排在 "--" 之前。

        为什么"排在 -- 之前"是要断言的硬点而非风格问题:
            "--" 之后 pytest 停止解析选项、其后元素一律作位置参数。
            把 -m/-k 放到 "--" 之后，pytest 会把标记表达式当文件路径去
            收集，报 file or directory not found 并以退出码 4 收场——
            与 Day44 P1-01 是同一个坑，故此处用精确命令形状钉死。

        同时钉死"无选择器时与 Day48 完全一致"（向后兼容护栏）：
            既有的 10 条用例都不传选择器，多一个多余参数就会让它们全红。
        """
        runner = PytestRunner()
        case = {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}

        both = runner.build_command({**case, "markers": "smoke", "keyword": "login"})
        assert both == [
            sys.executable, "-m", "pytest", "-q", "--tb=short",
            "-m", "smoke", "-k", "login", "--", VALID_SOURCE_REF,
        ], "选择器必须紧跟已有选项、在终止符之前、位置参数之前"

        only_markers = runner.build_command({**case, "markers": "smoke and api"})
        assert "-k" not in only_markers, "未传 keyword 就不该出现 -k"
        # 从下标 3 起找 "-m"：前两个元素是 [解释器, "-m", "pytest"]，
        # 那是解释器调用 pytest 的 -m，与选择器的 -m 同名不同义
        marker_index = only_markers.index("-m", 3)
        assert only_markers[marker_index + 1] == "smoke and api", (
            "标记表达式必须作为 -m 的紧邻取值原样透传"
        )

        only_keyword = runner.build_command({**case, "keyword": "login or logout"})
        assert "-m" not in only_keyword[3:], "未传 markers 就不该出现选择器的 -m"
        assert only_keyword[only_keyword.index("-k", 3) + 1] == "login or logout"

        assert runner.build_command(case) == runner.build_command(
            {**case, "markers": None, "keyword": None}
        ), "显式传 None 与不传是同一语义：不加入命令行"
        assert "-m" not in runner.build_command(case)[3:], (
            "无选择器时命令与 Day48 完全一致（[:3] 跳过解释器调用 pytest 的 -m）"
        )

    @allure.story("Day49：path 字段优先于 source_ref 并校验存在性")
    def test_build_command_path_field_precedence(self) -> None:
        """
        path 是本日新增的目录/文件选择器，取值优先于 source_ref。

        目录选择是本任务的核心新能力（source_ref 恒为 .py 文件），故
        必须真的把目录传通；同时 path 指向的路径必须真实存在，否则
        应在我们这边报错，而不是让 pytest 抛晦涩的 file or directory
        not found 后以退出码 4 收场。
        """
        runner = PytestRunner()

        both = runner.build_command(
            {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF, "path": "tests/api_demo"}
        )
        assert both[-1] == "tests/api_demo", "path 必须压过 source_ref 成为位置参数"
        assert both[-2] == "--", "位置参数仍必须在终止符之后"

        directory = runner.build_command({"case_id": CASE_ID, "path": "tests"})
        assert directory[-1] == "tests", "目录形式的选择目标必须原样透传"

        with pytest.raises(ValueError) as excinfo:
            runner.build_command({"case_id": CASE_ID, "path": "tests/不存在的目录"})
        message = str(excinfo.value)
        assert "不存在" in message, "路径不存在必须被拦在我们这边并说明原因"
        assert CASE_ID in message, "报错须指明是哪条用例"

        # cwd 是相对基准：换一个 cwd 后同一个 path 就不存在了
        elsewhere = PytestRunner(cwd=Path(__file__).parent)
        with pytest.raises(ValueError, match="不存在"):
            elsewhere.build_command({"case_id": CASE_ID, "path": "tests/api_demo"})

    @allure.story("Day49：非法 selectors 与 path 的拒绝矩阵")
    def test_build_command_rejects_invalid_selectors_and_paths(self) -> None:
        """
        markers/keyword/path 的非法值必须在拼命令阶段就被拒。

        校验放在拼装阶段的理由: 让 pytest 去报这些错，错误信息出现在
        pytest 内部，调用方看到的是"用法错误"而非"你的用例配置有问题"；
        而本方法是数据进入子进程命令的**唯一闸门**，放过即等于放行。

        重点覆盖"看起来像选项的值": `-m -p` 这类以 `-` 开头的表达式，
        pytest 会把后半段当下一个选项解析，行为完全不可预期。
        """
        runner = PytestRunner()
        base = {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}

        for field, bad_value, expected in (
            ("markers", 123, "必须是 str"),
            ("markers", True, "必须是 str"),
            ("markers", ["smoke"], "必须是 str"),
            ("markers", "", "不能为空串"),
            ("markers", "   ", "不能为空串"),
            ("markers", "-p no:cacheprovider", "不能以 '-' 开头"),
            ("markers", "smoke\x00", "不能包含 NUL"),
            ("keyword", 123, "必须是 str"),
            ("keyword", "", "不能为空串"),
            ("keyword", "--maxfail=1", "不能以 '-' 开头"),
            ("keyword", "a\x00b", "不能包含 NUL"),
        ):
            with pytest.raises(ValueError) as excinfo:
                runner.build_command({**base, field: bad_value})
            message = str(excinfo.value)
            assert field in message, f"报错须点名字段 {field}: {message}"
            assert expected in message, f"报错须说明具体原因（{expected}）: {message}"
            assert CASE_ID in message, "报错须指明是哪条用例"

        # path 的形态校验：绝对路径 / 盘符 / 上级目录 / 反斜杠 / 空串 / 非 str
        for bad_path, expected in (
            ("/etc/passwd", "禁绝对路径"),
            ("C:/Windows/system32", "禁绝对路径"),
            ("../outside", "不得越出项目根"),
            ("tests\\api_demo", "不能包含反斜杠"),
            ("-p no:cacheprovider", "不能以 '-' 开头"),
            ("a\x00b.py", "不能包含 NUL"),
            ("", "不能为空串"),
            ("   ", "不能为空串"),
            (123, "必须是 str"),
        ):
            with pytest.raises(ValueError) as excinfo:
                runner.build_command({**base, "path": bad_path})
            assert expected in str(excinfo.value), f"path={bad_path!r} 报错不符预期"

        # path=None 与"不传"同义（回落 source_ref），不是类型错误
        # ——与 markers/keyword 的 None 语义保持一致
        assert runner.build_command({**base, "path": None})[-1] == VALID_SOURCE_REF, (
            "path=None 表示未配置，应回落 source_ref 而不是报错"
        )

        # 节点选择形态（pytest tests/x.py::test_y）必须放行
        node = runner.build_command({**base, "path": f"{VALID_SOURCE_REF}::test_login"})
        assert node[-1] == f"{VALID_SOURCE_REF}::test_login", "节点选择形态应原样透传"

    @allure.story("Day49：两个纯函数的行为（目标校验 + 流解码）")
    def test_helpers_target_path_and_stream_decode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        validate_target_path 与 decode_subprocess_stream 的直接单测。

        前者值得单独守: 它是位置参数进命令前的唯一闸门，且存在多处
        分支（空值/类型/盘符/反斜杠/..），靠 build_command 的间接断言
        容易漏掉某一支的判定逻辑。

        后者守的是**跨平台类型不一致**这个真实坑: TimeoutExpired 上的流
        在 POSIX 是 bytes、Windows 是 str，即便传了 text=True 也一样。
        不做归一，Linux 上超时路径会 AttributeError，把"超时"这个真实
        原因掩盖成一条无关的崩溃。

        同时守住 _resolve_source_ref 的 None 兜底: 上游 validate_source_ref
        的返回类型是 `str | None`，本地已排除空值故该分支理论不可达，
        但它不能靠"不可达"来自证——用 monkeypatch 把它逼出来，确认它
        给的是可读的业务错误而不是裸 TypeError。
        """
        assert validate_target_path("tests/a.py", "ctx: ") == "tests/a.py"
        assert validate_target_path("  tests/a.py  ", "ctx: ") == "tests/a.py", (
            "首尾空白应被 strip 掉，否则拼进命令会带上不可见字符"
        )
        assert validate_target_path("tests/dir/", "ctx: ") == "tests/dir/"

        assert decode_subprocess_stream(None) == ""
        assert decode_subprocess_stream("abc") == "abc"
        assert decode_subprocess_stream(b"abc") == "abc", "bytes 必须被解码成 str"
        assert decode_subprocess_stream(bytearray(b"abc")) == "abc"
        # 非法字节序列不得抛异常：UTF-8 解不开的字节按 replace 处理
        assert "\ufffd" in decode_subprocess_stream(b"\xff\xfe")

        assert truncate_output_tail("") == ""
        assert truncate_output_tail("x" * 10, 20) == "x" * 10, "未超限原样返回"
        assert truncate_output_tail("xx", 5) == "xx"
        long_text = "y" * (OUTPUT_TRUNCATE_LENGTH + 10)
        assert truncate_output_tail(long_text).endswith("y" * OUTPUT_TRUNCATE_LENGTH)

        # 上游契约若改成"非空输入也返回 None"，此处必须给出可读报错
        monkeypatch.setattr(executors_mod, "validate_source_ref", lambda *a, **k: None)
        with pytest.raises(ValueError, match="校验后为空"):
            PytestRunner().build_command(
                {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}
            )


@allure.feature("Day48 真实执行器")
class TestPytestRunnerRunOne:
    """run_one：退出码映射、异常降级与三项可配置项接线"""

    @allure.story("退出码 0 → passed，且 cwd/env/timeout 已传给子进程")
    def test_run_one_passed(
        self, patch_subprocess: FakeSpawner, fake_popen: FakePopen
    ) -> None:
        """
        通过场景：result=passed、error_message 为 None、耗时非负。

        同时借这条用例验证三项可配置项真的传到了子进程：
        cwd 必须是项目根（pytest 靠它定位相对路径用例与 pytest.ini）、
        env 必须是"当前环境 + 注入项"的合并结果、timeout 必须真的
        交给了 communicate（Day49 改用 Popen 后超时不再是 Popen 的参数，
        而是 communicate 的参数——漏传等于超时功能整体失效）。
        这三项一旦漏传，真实执行会在 Web 触发/worker 场景下全量落
        error，但纯退出码断言完全看不出来。
        """
        fake_popen.stdout = "1 passed in 0.12s"
        fake_popen.returncode = 0
        patch_subprocess.process = fake_popen
        case = {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}

        runner = PytestRunner(timeout=7, env={"TM_DAY48_MARK": "1"})
        result = runner.run_one(case)

        assert result.result == "passed"
        assert result.error_message is None
        # 只校验非负（Day48-fix P3-2 方案 A）：此处子进程被 mock，
        # duration 落在 1e-5 量级，写死 >0 会引入时钟分辨率相关的 flaky
        # 风险。"确实执行过而非短路返回"由 Day44 三条**真实子进程**用例
        # 以 duration > 0.0 兜底——两者互补，不是重复断言。
        assert result.duration >= 0.0, "耗时为非负测量值"
        assert result.exit_code == 0, "原始退出码必须记录"
        assert result.exit_reason == "passed"
        assert "1 passed" in result.output, "通过场景的输出同样要留（Day50 解析器要用）"

        # 命令里不能出现用于识别这条用例的编号
        command, kwargs = patch_subprocess.calls[0]
        assert command[-1] == VALID_SOURCE_REF
        assert CASE_ID not in command

        # 三项可配置项接线
        assert Path(str(kwargs["cwd"])) == PROJECT_ROOT, "默认工作目录必须是项目根"
        assert fake_popen.communicate_calls == [7], (
            "超时预算必须传给 communicate（改用 Popen 后不再是 Popen 的参数）"
        )
        merged_env = kwargs["env"]
        assert isinstance(merged_env, dict)
        assert merged_env["TM_DAY48_MARK"] == "1", "注入的环境变量必须生效"
        assert "PATH" in merged_env, "必须继承当前进程环境，而不是只留注入项"
        assert kwargs["stdout"] == subprocess.PIPE, "必须捕获 stdout"
        assert kwargs["stderr"] == subprocess.PIPE, "必须捕获 stderr"

    @allure.story("退出码 1 → failed，error_message 含输出尾部")
    def test_run_one_failed(
        self, patch_subprocess: FakeSpawner, fake_popen: FakePopen
    ) -> None:
        """
        失败场景：pytest 约定退出码 1 表示存在失败用例，详情在 stdout。

        断言 error_message 带上真实输出内容（而非泛化文案），否则报告里
        每条失败都只写"pytest退出码: 1"，排障还得回机器复跑一遍。

        顺带钉住"未注入 env 时传 None"这条语义（Day48-fix P3-3）：
        `_build_env` 的 docstring 承诺不注入就交由 subprocess 直接继承
        当前进程环境。改成"总是返回环境拷贝"是**等价变异**（子进程所见
        环境相同、不报错），因此只能靠这条断言钉住——否则该语义细节
        完全无人看守，改了也没人知道。本测试用默认构造的 PytestRunner()，
        正是"未注入"的场景。
        """
        detail = "tests/test_login.py::test_wrong_password AssertionError: boom"
        fake_popen.returncode = 1
        fake_popen.stdout = f"FAILURES\n{detail}\n1 failed, 2 passed in 1.20s"
        patch_subprocess.process = fake_popen

        result = PytestRunner().run_one(
            {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}
        )

        assert result.result == "failed", "退出码 1 必须映射为 failed 而非 error"
        assert result.error_message, "failed 必须带失败详情"
        assert detail in result.error_message, "error_message 必须含失败输出尾部"
        assert result.duration >= 0.0
        assert result.exit_code == 1
        assert result.exit_reason == EXIT_REASON_TESTS_FAILED

        _, kwargs = patch_subprocess.calls[0]
        assert kwargs["env"] is None, (
            "未注入 env 时应传 None（交由 subprocess 继承），而非环境拷贝"
        )

    @allure.story("非 0/1 的退出码仍为 error，细节在 stderr")
    def test_run_one_error_exit_code(
        self, patch_subprocess: FakeSpawner, fake_popen: FakePopen
    ) -> None:
        """
        非 0/1 的退出码必须映射为 error。

        与 failed 严格区分：failed 是"用例断言不通过"（业务结论），
        error 是"这次执行本身没跑成"（工具结论），两者在报告统计与
        通知分级里的口径不同，混在一起会让通过率失真。

        注意这里刻意用退出码 2（interrupted）作为 error 样本：Day49
        把 2/3/4/5 细分成了四种 exit_reason，但 result 口径**不变**，
        仍是 error——下游 VALID_RESULTS 只认四值。
        """
        stderr_detail = "ERROR: file or directory not found: tests/missing.py"
        fake_popen.returncode = 2
        fake_popen.stdout = ""
        fake_popen.stderr = stderr_detail
        patch_subprocess.process = fake_popen

        result = PytestRunner().run_one(
            {"case_id": CASE_ID, "source_ref": "tests/missing.py"}
        )

        assert result.result == "error", "非 0/1 退出码必须映射为 error"
        assert result.error_message, "error 必须带原因"
        assert stderr_detail in result.error_message, "error_message 必须含 stderr 尾部"
        assert result.duration >= 0.0
        assert result.exit_code == 2
        assert result.exit_reason == EXIT_REASON_INTERRUPTED

    @allure.story("Day49：六个标准退出码 + 未知值的完整分类矩阵")
    def test_run_one_maps_every_exit_code(
        self, patch_subprocess: FakeSpawner, fake_popen: FakePopen
    ) -> None:
        """
        逐个退出码断言 (result, exit_code, exit_reason) 三元组。

        为什么整张表都要守:
            退出码分类的价值全在"区分得开"——只有 0/1 两档时，
            "收集阶段就炸了"和"命令行写错了"在报告里长得一模一样。
            少映射一档不会报错，只会静默退化成笼统的 error。

        同时钉死 result 的四值词表边界: 3/4/5 全部落 error 而不是
        各自的细分名。理由是下游 record_execution 的 VALID_RESULTS
        只认 passed/failed/error/skipped，词表外的取值会让**单条用例**
        把整批判 failed，细分语义由 exit_reason 承载。
        """
        matrix = (
            (0, "passed", "passed"),
            (1, "failed", EXIT_REASON_TESTS_FAILED),
            (2, "error", EXIT_REASON_INTERRUPTED),
            (3, "error", EXIT_REASON_INTERNAL_ERROR),
            (4, "error", EXIT_REASON_USAGE_ERROR),
            (5, "error", EXIT_REASON_NO_TESTS_COLLECTED),
            (255, "error", EXIT_REASON_UNKNOWN_EXIT_CODE),
        )
        patch_subprocess.process = fake_popen
        for code, expected_result, expected_reason in matrix:
            fake_popen.returncode = code
            fake_popen.stdout = f"stdout for exit {code}"
            fake_popen.stderr = f"stderr for exit {code}"
            fake_popen._exc = None

            result = PytestRunner().run_one(
                {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}
            )

            assert result.result == expected_result, f"退出码 {code} 的 result 不符"
            assert result.exit_code == code, f"退出码 {code} 必须被原样记录"
            assert result.exit_reason == expected_reason, (
                f"退出码 {code} 的细分分类不符"
            )
            assert f"exit {code}" in result.output, f"退出码 {code} 的输出必须保留"
            if expected_result == "passed":
                assert result.error_message is None
            else:
                assert result.error_message, f"退出码 {code} 必须带原因"

        # result 只能取下游词表内的值（词表外会让整批判 failed）
        seen_results = {expected for _, expected, _ in matrix}
        assert seen_results <= {"passed", "failed", "error", "skipped"}, (
            f"映射表里出现了下游 VALID_RESULTS 词表外的取值: {seen_results}"
        )

        # 处置建议只对"能给出行动指引"的退出码出现，passed 不该有
        assert "处置建议" not in (result.error_message or "")

    @allure.story("超时预算三级优先级与超时降级")
    def test_run_one_timeout(
        self,
        patch_subprocess: FakeSpawner,
        fake_popen: FakePopen,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        超时必须降级为 error 结果而不是抛异常。

        编排层逐条调用 run_one，异常上抛会把整批执行带崩且丢失失败原因；
        消息里要带超时阈值与完整命令，否则复现时不知道当时的预算是多少。
        **并必须真的接上进程树清理**（Day49 核心），否则超时后 xdist
        worker 与被测代码开的子进程会变孤儿继续占资源。

        同时覆盖预算的三级优先级（构造参数 > TM_PYTEST_TIMEOUT > 模块常量）
        与非法值的显式拒绝：静默回落会让"我明明把预算调到 5 秒"却仍按 30
        秒跑，而超时消息里的阈值看起来又是合法的，排查时完全看不出配置
        没生效，故非法值一律抛错而不是回落。

        同一条用例还覆盖 cwd / env 的**显式类型校验**（Day48-fix P3-6）：
        三参数此前只有 timeout 有业务级校验，cwd/env 传错类型会在
        `Path()`/`dict()` 处抛标准库消息（"expected str, bytes or
        os.PathLike object, not int"），读不出"是我把 cwd 传成了 int"。
        校验补齐后错误信息带字段名与实际类型，与 timeout 口径统一。
        """
        fake_timeout = 5
        fake_popen._exc = subprocess.TimeoutExpired(
            cmd="pytest", timeout=fake_timeout
        )
        patch_subprocess.process = fake_popen

        runner = PytestRunner(timeout=fake_timeout)
        # 拦掉真实的树杀：单测里绝不能让替身 pid 触发系统级 taskkill
        cleanup_calls: list[str] = []
        monkeypatch.setattr(
            runner, "kill_process_tree", lambda process: cleanup_calls.append("called") or "ok"
        )

        result = runner.run_one({"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF})

        assert result.result == "error", "超时不抛异常，降级为 error 结果"
        assert "超时" in (result.error_message or ""), "消息必须说明是超时"
        assert f"执行超时(>{fake_timeout}s)" in result.error_message, (
            "超时消息必须带实际生效的阈值，便于复现"
        )
        assert result.duration >= 0.0
        assert result.exit_code is None, "被 kill 的进程没有可用退出码"
        assert result.exit_reason == EXIT_REASON_TIMEOUT, "超时必须有专属分类"
        assert cleanup_calls == ["called"], "超时后必须走进程树清理，不能只杀直接子进程"

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

        # 空串同样视为"未配置"（容器/CI 常把未设置的变量渲染成空串）
        monkeypatch.setenv("TM_PYTEST_TIMEOUT", "")
        assert PytestRunner().timeout == PYTEST_TIMEOUT_SECONDS, (
            "空串是'未配置'而非'配错了'，应回落而非报错"
        )

        # cwd / env 非法类型显式拒绝，消息须带字段名与实际类型
        for bad_cwd in (123, 1.5, ["a"], {"a": 1}):
            with pytest.raises(TypeError, match=r"cwd|工作目录"):
                PytestRunner(cwd=bad_cwd)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=r"str 或 Path"):
            PytestRunner(cwd=123)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=r"int"):
            PytestRunner(cwd=123)  # type: ignore[arg-type]
        for bad_env in ("PATH=x", ["PATH=x"], ("PATH", "x")):
            with pytest.raises(TypeError, match=r"env|环境变量"):
                PytestRunner(env=bad_env)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match=r"dict 或 None"):
            PytestRunner(env="PATH=x")  # type: ignore[arg-type]

        # 合法类型照常通过（cwd 收 str/Path 并归一为 Path，env 收 dict）
        assert PytestRunner(cwd="sub/dir").cwd == Path("sub/dir"), (
            "str 形式的 cwd 应被接受并归一为 Path"
        )
        assert PytestRunner(env={}).env == {}, "空 dict 是合法注入（区别于 None）"

        # env **内容**校验（Day48-fix2）：容器类型对了不等于内容对
        # `dict[str, str]` 只是类型标注、运行时不生效，而 subprocess 要求
        # 环境块全为 str。漏掉这层时 TypeError 会从子进程创建逃逸。
        for bad_value in (123, None, True, 1.5, b"bytes", ["x"]):
            with pytest.raises(TypeError, match="CUSTOM_VAR") as excinfo:
                PytestRunner(env={"CUSTOM_VAR": bad_value})  # type: ignore[dict-item]
            message = str(excinfo.value)
            assert "值必须是 str" in message
            assert type(bad_value).__name__ in message, "消息须带实际类型"
        with pytest.raises(TypeError, match="键必须是 str"):
            PytestRunner(env={123: "v"})  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="不能包含"):
            PytestRunner(env={"A=B": "v"})
        with pytest.raises(ValueError, match="不能为空串"):
            PytestRunner(env={"": "v"})
        # 合法 env 仍放行（含形似非法但确实合法的边界）
        legal_env = {"A": "", "B_C-D": "x y z", "TM_长变量名": "值"}
        assert PytestRunner(env=legal_env).env == legal_env, (
            "空串值、含空格/连字符/中文的合法变量名都应放行"
        )

    @allure.story("Day49：超时前已捕获的输出必须保留")
    def test_run_one_timeout_preserves_partial_output(
        self,
        patch_subprocess: FakeSpawner,
        fake_popen: FakePopen,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        超时 kill 前捕获到的部分输出必须进 error_message 与 output。

        这是"超时场景下不丢现场"这条任务要求的直接验收点。丢掉它等于
        让最需要现场的那次执行什么都不留：超时恰恰是"必须复现才能定位"
        的故障，而复现前手里连半行输出都没有。

        bytes / str 两种形态都要测: POSIX 的 TimeoutExpired 带的是 bytes
        （即便传了 text=True），Windows 带 str。不做归一，Linux 上这条
        路径会直接 AttributeError——症状是"超时变成了崩溃"。
        """
        monkeypatch.setattr(
            PytestRunner, "kill_process_tree", lambda self, process: "mocked"
        )
        patch_subprocess.process = fake_popen

        for raw_stdout in (
            b"collected 12 items\ntests/test_login.py ..F",
            "collected 12 items\ntests/test_login.py ..F",
        ):
            fake_popen._exc = subprocess.TimeoutExpired(
                cmd="pytest", timeout=3, output=raw_stdout, stderr="worker crashed"
            )
            result = PytestRunner(timeout=3).run_one(
                {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}
            )

            assert result.result == "error"
            assert result.exit_reason == EXIT_REASON_TIMEOUT
            assert "collected 12 items" in (result.output or ""), (
                f"超时前的部分输出必须保留（形态 {type(raw_stdout).__name__}）"
            )
            assert "worker crashed" in result.output, "stderr 部分同样必须保留"
            assert "超时前输出" in (result.error_message or ""), (
                "error_message 里要能看到现场，而不只是'超时了'三个字"
            )

        # 输出为空时不能编造内容，但 output 字段仍须存在且为空串
        fake_popen._exc = subprocess.TimeoutExpired(cmd="pytest", timeout=3)
        empty_result = PytestRunner(timeout=3).run_one(
            {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}
        )
        assert empty_result.output == "", "无输出时不得凭空造内容"
        assert "超时前输出" not in (empty_result.error_message or "")

    @allure.story("子进程启动失败 → error，消息含启动失败原因")
    def test_run_one_os_error(self, patch_subprocess: FakeSpawner) -> None:
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
        assert result.exit_code is None, "启动失败没有退出码"
        assert result.exit_reason == EXIT_REASON_SPAWN_FAILED

    @allure.story("启动参数非法（TypeError/ValueError）→ 降级为 error，不逃逸")
    def test_run_one_type_error_degraded(self, patch_subprocess: FakeSpawner) -> None:
        """
        子进程启动**参数**非法时 run_one 不得抛异常，必须降级为该条 error。

        背景（Day48-fix2）：创建子进程要求环境块全为 str，非 str 会抛
        TypeError；env 键含 "=" 或含 NUL 抛 ValueError；cwd 含 NUL 同样
        抛 ValueError。这些异常在**创建进程之前**抛出，既不是
        TimeoutExpired 也不是 OSError——修复前的 except 链只捕这两类，
        于是异常直接上抛穿透 run_one，违反 docstring "异常: 无"，更违反
        "单条用例失败不升级为整批 failed"的编排契约。

        构造期校验（_validate_env_content）已挡住正常路径，本用例守的是
        **兜底层**：未来有人直接给 self.env 赋值绕过构造函数，或出现
        校验未覆盖的新参数形态时，这一层仍必须生效。

        断言要点：降级为 error、消息点明"启动失败"、**保留标准库原文**
        （否则排障时要重新翻 subprocess 源码才知道是哪类参数问题）。
        """
        for exc in (
            TypeError("environment can only contain strings"),
            ValueError("embedded null character"),
            ValueError("illegal environment variable name"),
        ):
            patch_subprocess.exc = exc

            result = PytestRunner().run_one(
                {"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF}
            )

            assert result.result == "error", (
                f"{type(exc).__name__} 必须降级为 error 而非上抛穿透 run_one"
            )
            assert "启动失败" in (result.error_message or ""), (
                "消息须归入'启动失败'类，便于与超时/断言失败区分"
            )
            assert str(exc) in result.error_message, "必须保留标准库异常原文"
            assert result.duration >= 0.0
            assert result.exit_reason == EXIT_REASON_INVALID_ARGUMENTS

    @allure.story("绕过构造期校验后兜底层仍生效（直接改 self.env）")
    def test_run_one_degrades_when_env_mutated_after_construction(
        self, patch_subprocess: FakeSpawner
    ) -> None:
        """
        构造后直接改 self.env 注入非字符串值，run_one 仍须兜住。

        这条用例证明兜底层不是"构造期校验的附属品"：`_validate_env_content`
        只在构造期跑一次，之后 self.env 是可变的。任何人（包括未来新增的
        批次级 env 注入逻辑）绕过构造函数写坏 env，run_one 都必须把它
        降级为单条 error——这正是 Day48-fix2 保留两层的原因。
        """
        runner = PytestRunner()
        runner.env = {"BROKEN": 8080}  # 绕过构造期校验直接赋值
        patch_subprocess.exc = TypeError("environment can only contain strings")

        result = runner.run_one({"case_id": CASE_ID, "source_ref": VALID_SOURCE_REF})

        assert result.result == "error"
        assert "environment can only contain strings" in result.error_message

    @allure.story("source_ref 缺失 → 降级为单条 error，且不启动子进程")
    def test_run_one_build_command_failure_degraded(self) -> None:
        """
        缺少执行目标时 run_one 不得抛异常，且**根本不该走到子进程**。

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
        assert result.exit_reason == EXIT_REASON_INVALID_CASE, (
            "配置问题与执行失败必须能被区分开"
        )


@allure.feature("Day49 进程树清理")
class TestProcessTreeCleanup:
    """超时后整棵进程树的清理逻辑"""

    @allure.story("拿不到真实 pid 时跳过系统调用")
    def test_kill_process_tree_skips_without_real_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        pid 不是正整数时直接跳过清理，且不得触发任何系统调用。

        为什么要这道守卫: 清理逻辑会调 taskkill / killpg，参数就是 pid。
        若把一个非真实 pid（单测替身、尚未赋值的属性）传下去，要么在
        Windows 上误杀一个**恰好同号**的无关进程，要么刷一条无意义的
        taskkill 错误进超时消息。宁可明说"没清理"也不能误伤。

        这里同时验证"跳过"不是静默的：返回值必须说清楚为什么跳过。

        末段把平台切到 posix 跑一遍真实 pid 的分支：kill_process_tree 的
        平台分流在 Windows 上只走 taskkill 一侧，不覆盖的话 POSIX 分流
        就成了无人验证的死路径。
        """
        runner = PytestRunner()

        for bad_pid in (0, -1, "4321", None):
            note = runner.kill_process_tree(FakePopen(pid=bad_pid))
            assert "跳过" in note, f"pid={bad_pid!r} 应走跳过分支，实际: {note}"
            assert "未取得真实 pid" in note, f"跳过原因必须写明: {note}"

        posix_calls: list[tuple[int, int]] = []
        monkeypatch.setattr(
            executors_mod, "_POSIX_KILLPG", lambda pgid, sig: posix_calls.append((pgid, sig))
        )
        monkeypatch.setattr(executors_mod.os, "name", "posix")
        note = runner.kill_process_tree(FakePopen(pid=4321))
        assert posix_calls, "POSIX 平台必须走进程组清理而不是 taskkill"
        assert note.startswith("pid=4321 "), f"描述必须带 pid: {note}"
        assert "已清理" in note

    @allure.story("Windows：taskkill /T /F 的调用形状与结果解析")
    def test_kill_tree_windows_shapes_and_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        taskkill 必须带 /T（递归子进程）与 /F（强制），并正确解析返回码。

        为什么 /T 与 /F 都不能省:
            /T 决定是否递归到子进程——正是本任务要消灭的僵尸来源；
            /F 决定是否强制终止，不带 /F 时被测代码可以无限期忽略关闭，
            超时清理就退化成"发了个信号然后走人"。

        taskkill 自身失败（权限不足/进程已退出）也必须有可读描述，
        不能把异常抛进超时路径——清理是兜底动作，兜底自己再炸就没人兜底了。
        """
        recorded: list[list[str]] = []

        def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            recorded.append(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout="成功: 已终止 PID 1234", stderr=""
            )

        monkeypatch.setattr(executors_mod.subprocess, "run", _fake_run)
        success = PytestRunner._kill_tree_windows(1234)
        assert recorded == [["taskkill", "/PID", "1234", "/T", "/F"]], (
            "taskkill 参数形状不符：缺 /T 就清不掉后代，缺 /F 就可能清不掉"
        )
        assert "已清理整棵进程树" in success, f"成功描述不符预期: {success}"

        monkeypatch.setattr(
            executors_mod.subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 128, stdout="", stderr="没有该进程"),
        )
        failure = PytestRunner._kill_tree_windows(1234)
        assert "128" in failure, "失败时必须报出返回码"
        assert "没有该进程" in failure, "失败时必须带上 taskkill 的原文"

        def _raise_oserror(cmd: list[str], **kwargs: Any) -> None:
            raise OSError("taskkill 不存在")

        monkeypatch.setattr(executors_mod.subprocess, "run", _raise_oserror)
        crashed = PytestRunner._kill_tree_windows(1234)
        assert "taskkill 调用失败" in crashed, "taskkill 自身异常也必须被吞成描述"

    @allure.story("POSIX：SIGTERM 收尾成功 / 不退出则升级 SIGKILL")
    def test_kill_group_posix_term_then_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        先 SIGTERM 给被测代码收尾机会，仍不退出才升级 SIGKILL。

        覆盖四条路径：非 POSIX 平台降级 / SIGTERM 发送失败 / 轮询中退出 /
        轮询超时升级 SIGKILL（含强杀也失败）。

        本机是 Windows，故用 monkeypatch 注入 _POSIX_KILLPG 替身来跑
        这段 POSIX 逻辑——它同样要在 Windows 开发机上被覆盖，否则
        真正的 POSIX 分支就是无人验证的死路径。
        """
        runner = PytestRunner()

        # 非 POSIX 平台：优雅降级而不是抛异常
        monkeypatch.setattr(executors_mod, "_POSIX_KILLPG", None)
        assert "非 POSIX" in runner._kill_group_posix(FakePopen(pid=111))

        calls: list[tuple[int, int]] = []

        def _record_ok(pgid: int, sig: int) -> None:
            calls.append((pgid, sig))

        monkeypatch.setattr(executors_mod, "_POSIX_KILLPG", _record_ok)

        # 路径一：SIGTERM 发出后进程退出
        calls.clear()
        note = runner._kill_group_posix(FakePopen(pid=222, returncode=0))
        assert calls == [(222, executors_mod.signal.SIGTERM)], "必须先发 SIGTERM"
        assert "SIGTERM" in note and "已清理" in note

        # 路径二：轮询到宽限期结束仍未退出 → 升级 SIGKILL
        calls.clear()
        stuck = FakePopen(pid=333, poll_return=None)
        monkeypatch.setattr(executors_mod, "PROCESS_TREE_TERM_GRACE_SECONDS", 0.0)
        note = runner._kill_group_posix(stuck)
        assert calls == [(333, executors_mod.signal.SIGTERM), (333, executors_mod._SIGKILL)], (
            "未在宽限期内退出必须升级为强杀（Windows 上两者同为 15，"
            "此处只校验'发了两次信号'，信号区分由 Linux CI 验证）"
        )
        assert "强制清理" in note, f"强杀路径描述不符预期: {note}"

        # 路径三：发信号就失败（进程组已消失等）
        def _record_fail(pgid: int, sig: int) -> None:
            raise OSError("no such process")

        monkeypatch.setattr(executors_mod, "_POSIX_KILLPG", _record_fail)
        assert "SIGTERM 失败" in runner._kill_group_posix(FakePopen(pid=444))

        # 路径四：SIGTERM 之后进程在宽限期内自行退出 → 轮询等到终态即收手
        calls.clear()
        slept: list[float] = []
        monkeypatch.setattr(executors_mod, "_POSIX_KILLPG", _record_ok)
        monkeypatch.setattr(executors_mod.time, "sleep", slept.append)
        monkeypatch.setattr(executors_mod, "PROCESS_TREE_TERM_GRACE_SECONDS", 1.0)

        slow = FakePopen(pid=555, poll_return=None)
        poll_states = [None, None, 0]
        slow.poll = lambda: poll_states.pop(0) if poll_states else 0  # type: ignore[method-assign]
        note = runner._kill_group_posix(slow)
        assert len(slept) == 2, "宽限期内必须轮询等待，而不是一次性判死"
        assert slept == [PROCESS_TREE_POLL_INTERVAL_SECONDS] * 2
        assert calls == [(555, executors_mod.signal.SIGTERM)], (
            "自行退出的进程不该再被 SIGKILL——升级强杀只针对赖着不走的"
        )
        assert "SIGTERM" in note and "已清理" in note

        # 路径五：SIGKILL 也失败（组已彻底消失），必须如实上报而不是静默
        calls.clear()
        seen: list[int] = []

        def _fail_on_second(pgid: int, sig: int) -> None:
            # 按"第几次调用"判失败而不是比信号值: Windows 上 SIGKILL 不存在
            # 会回落成 SIGTERM，两者同为 15，按值判会连第一步就误判成失败
            seen.append(sig)
            if len(seen) == 2:
                raise OSError("killpg: no such process group")

        monkeypatch.setattr(executors_mod, "_POSIX_KILLPG", _fail_on_second)
        monkeypatch.setattr(executors_mod, "PROCESS_TREE_TERM_GRACE_SECONDS", 0.0)
        note = runner._kill_group_posix(FakePopen(pid=666, poll_return=None))
        assert "SIGKILL 失败" in note, f"强杀失败必须如实上报: {note}"

    @allure.story("直接子进程未随树清理退出时的补杀兜底")
    def test_kill_process_tree_fallback_kills_direct_child(self) -> None:
        """
        树清理之后直接子进程仍未退出，必须单独补一刀再 wait。

        为什么需要这层兜底: 极端情况下（taskkill 返回非 0、killpg 的
        组已消失）树清理可能没带走直接子进程。此时它会变成僵尸，
        而僵尸在进程列表里仍可见——任何"跑完检查有没有残留"的排查都会
        被它误导，而且它持有的文件句柄不释放。
        """
        runner = PytestRunner()
        timed_out = subprocess.TimeoutExpired(cmd="pytest", timeout=1)
        process = FakePopen(pid=555, wait_exc=timed_out)

        note = runner.kill_process_tree(process)

        assert process.kill_called, "直接子进程未退出时必须补杀"
        assert len(process.wait_calls) == 2, "补杀后还要再等一次确认"
        assert process.wait_calls == [
            PROCESS_TREE_KILL_GRACE_SECONDS,
            PROCESS_TREE_KILL_GRACE_SECONDS,
        ]
        assert "补杀仍未退出" in note, f"补杀也失败时必须如实上报: {note}"

    @allure.story("真实端到端：超时后孙进程确认被清理")
    def test_run_one_timeout_kills_real_process_tree(self, tmp_path: Path) -> None:
        """
        真实拉起 pytest → 探针在收集阶段拉起孙进程 → 超时 → 确认无残留。

        这是本任务最核心的一条验收点，且**不能用 mock**：清理是否真的
        发生，只有真实进程能证明；用替身验证等于用被测逻辑证明被测逻辑
        （Day44 P1-01 当初就是这么漏掉缺陷的）。

        两个防假通过的断言缺一不可:
            ① 孙进程的 pid 文件必须存在——它证明孙进程**真的被创建过**，
               否则"清理成功"可能只是"压根没生成"
            ② 孙进程必须已消失——这才是清理生效的证据

        判定进程存活用 _pid_alive（Windows 走 OpenProcess 查询而非
        os.kill，因为 Windows 上 os.kill(pid, 0) 会真的把进程杀掉）。
        """
        probe_dir = tmp_path / "day49_tree_probe"
        probe_dir.mkdir()
        (probe_dir / "test_probe_hang.py").write_text(TREE_PROBE_SOURCE, encoding="utf-8")
        pid_file = probe_dir / "grandchild.pid"

        runner = PytestRunner(timeout=TREE_PROBE_TIMEOUT_SECONDS, cwd=probe_dir)
        grandchild_pid: int | None = None
        try:
            result = runner.run_one(
                {"case_id": CASE_ID, "source_ref": "test_probe_hang.py"}
            )

            assert result.result == "error", "挂起的探针必须以超时收场"
            assert result.exit_reason == EXIT_REASON_TIMEOUT
            assert "进程树清理" in (result.error_message or ""), (
                "超时消息必须带上清理结果，排障时才能判断是否真的清干净"
            )
            # 超时消息与日志刻意保留完整命令行（Day48-fix P3-4 决策）
            assert "test_probe_hang.py" in result.error_message

            assert pid_file.exists(), (
                "探针未拉起孙进程，这条用例就只是在验证'没有东西需要清理'；"
                "若 CI 机器启动偏慢，请提高 TREE_PROBE_TIMEOUT_SECONDS"
            )
            grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
            assert _wait_process_gone(grandchild_pid), (
                f"孙进程 {grandchild_pid} 在超时后仍然存活——进程树清理失效"
            )
        finally:
            # 兜底：断言失败时也不要给机器留一个睡 120 秒的孤儿进程
            if grandchild_pid is None and pid_file.exists():
                grandchild_pid = int(pid_file.read_text(encoding="utf-8").strip())
            if grandchild_pid is not None and _pid_alive(grandchild_pid):
                subprocess.run(
                    ["taskkill", "/PID", str(grandchild_pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )