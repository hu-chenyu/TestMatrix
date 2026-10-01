"""
TelnetClient 网口通信封装单元测试（本地 socket mock telnet 服务端方案）

覆盖范围:
    - TelnetClientError 异常消息格式（带/不带 host）
    - TelnetClient 构造参数校验（空 host、port=0、port 越界）
    - 本地 socket mock 服务端的 connect/login/execute/close 全流程
    - 连接失败、未连接调用、空命令、expect 超时等异常路径
    - 上下文管理器 __enter__/__exit__ 自动连接与断开

测试方案:
    不连接任何真实外部设备：在 127.0.0.1 上用 socket 监听 OS 分配的随机端口，
    子线程接受单个连接后按 "login: → Password: → $ 提示符" 序列与客户端交互，
    命令阶段回显命令并返回固定输出与新提示符。用 threading.Event 通知主线程
    服务端已 listen，用 fixture 的 yield/finally 保证 socket 关闭与线程 join，
    杜绝端口与线程泄漏。全程禁用固定 sleep 赌时序，超时由 socket/telnet
    客户端的 timeout 参数控制。
"""

import socket
import threading
import time
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
import src.common.telnet_client as tc
from src.common.telnet_client import TelnetClient, TelnetClientError


# ----------------------------------------------------------------------
# 本地 socket mock telnet 服务端
# ----------------------------------------------------------------------
class MockTelnetServer:
    """单连接的极简 telnet 服务端模拟器。

    在线程中接受一个连接，完成登录提示序列后进入命令回显循环：
    收到命令字节后回显命令本身并返回固定结果行与新的 ``$ `` 提示符。
    仅用于本地回环测试，不实现任何 telnet IAC 协议协商。
    """

    def __init__(self, mode: str = "ok") -> None:
        """初始化监听 socket 与服务线程，绑定本地随机端口并开始监听。

        参数:
            mode: 服务端行为模式——"ok" 正常登录序列+命令回显；
                  "auth_fail" 用户名后只回 ERROR 不发 Password/提示符（认证失败）；
                  "silent" accept 后不发任何 banner（login_timeout 行使）。
        """
        self.mode = mode
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port: int = self._sock.getsockname()[1]
        self.ready = threading.Event()
        self._conn: socket.socket | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self.ready.set()

    def _serve(self) -> None:
        """线程主体：recv 驱动状态机，严格按"收到上一步输入才发下一步提示"交互。

        旧版无条件连发 login:/Password:/$ 会让客户端即使匹配串写错也能
        被后续数据"喂对"；recv 驱动后每一步必须真正等到客户端输入，
        客户端 expect 匹配串写错时只会超时，匹配逻辑第一次有了牙齿（T2）。
        """
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        self._conn = conn
        try:
            if self.mode == "silent":
                # 不发送任何 banner，仅挂起保持连接（供 login_timeout 测试）
                while conn.recv(1024):
                    pass
                return

            conn.sendall(b"login: ")
            if not conn.recv(1024):
                return

            if self.mode == "auth_fail":
                # 不回 Password 提示符也不给 shell 提示符，登录必须超时失败（T7）
                conn.sendall(b"ERROR: invalid credentials\n")
                while conn.recv(1024):
                    pass
                return

            conn.sendall(b"Password: ")
            if not conn.recv(1024):
                return
            conn.sendall(b"$ ")
            while True:
                data = conn.recv(1024)
                if not data:
                    break
                # 回显命令 + 固定结果行 + 新提示符，供无 expect/有 expect 两种模式断言
                conn.sendall(data + b"OK_RESULT\n$ ")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        """关闭监听 socket 与已建立连接，并等待服务线程退出（带超时防卡死）。"""
        if self._conn is not None:
            try:
                self._conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


@pytest.fixture
def mock_server() -> Iterator[MockTelnetServer]:
    """启动一个本地 mock telnet 服务端，用例结束后关闭并回收线程。

    返回:
        MockTelnetServer: 已就绪的服务端（含实际监听端口）
    """
    server = MockTelnetServer()
    yield server
    server.close()


@pytest.fixture
def logged_in_client(mock_server: MockTelnetServer) -> Iterator[TelnetClient]:
    """提供已 connect 且 login 成功的 TelnetClient，用例结束后关闭。

    返回:
        TelnetClient: 处于已登录 shell 状态的客户端
    """
    client = TelnetClient(host="127.0.0.1", port=mock_server.port, timeout=3.0)
    client.connect()
    client.login(username="root", password="admin")
    yield client
    client.close()


# ----------------------------------------------------------------------
# TelnetClientError
# ----------------------------------------------------------------------
class TestTelnetClientError:
    """Telnet 统一异常类的消息封装测试。"""

    def test_异常消息_不带主机时为原文(self) -> None:
        """不带 host 初始化时，异常字符串就是原始消息。"""
        err = TelnetClientError("连接失败")
        assert str(err) == "连接失败"
        assert err.host is None

    def test_异常消息_带主机时含主机前缀(self) -> None:
        """带 host 初始化时，异常字符串含 [Telnet host] 前缀。"""
        err = TelnetClientError("连接失败", host="127.0.0.1")
        assert err.host == "127.0.0.1"
        assert str(err) == "[Telnet 127.0.0.1] 连接失败"


# ----------------------------------------------------------------------
# 构造参数校验
# ----------------------------------------------------------------------
class TestTelnetClientInit:
    """TelnetClient 初始化参数校验测试。"""

    def test_空主机抛value错误(self) -> None:
        """host 为空字符串时抛 ValueError。"""
        with pytest.raises(ValueError, match="非法目标地址"):
            TelnetClient(host="")

    def test_纯空白主机抛value错误(self) -> None:
        """host 为纯空白字符时抛 ValueError。"""
        with pytest.raises(ValueError, match="非法目标地址"):
            TelnetClient(host="   ")

    def test_零端口抛value错误(self) -> None:
        """port=0 不在 1-65535 范围，抛 ValueError。"""
        with pytest.raises(ValueError, match="非法端口号"):
            TelnetClient(host="127.0.0.1", port=0)

    def test_端口越界抛value错误(self) -> None:
        """port=70000 超过 65535，抛 ValueError。"""
        with pytest.raises(ValueError, match="非法端口号"):
            TelnetClient(host="127.0.0.1", port=70000)

    def test_合法参数保存且初始未连接(self) -> None:
        """合法参数仅保存配置，构造后不自动连接。"""
        client = TelnetClient(host="127.0.0.1", port=2323, timeout=2.0)
        assert client.host == "127.0.0.1"
        assert client.port == 2323
        assert client.timeout == 2.0
        assert client.is_connected is False


# ----------------------------------------------------------------------
# 连接与登录
# ----------------------------------------------------------------------
class TestTelnetClientConnect:
    """connect/login 生命周期测试。"""

    def test_connect成功后is_connected为真(self, mock_server: MockTelnetServer) -> None:
        """连接本地 mock 服务端成功，is_connected 返回 True。"""
        client = TelnetClient(host="127.0.0.1", port=mock_server.port, timeout=3.0)
        try:
            client.connect()
            assert client.is_connected is True
        finally:
            client.close()

    def test_connect连接被拒绝走OSError失败分支(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """D 类：telnetlib 构造抛 ConnectionRefusedError 时精确走"连接失败"分支。

        用 monkeypatch 注入消除平台漂移（Windows 实连可能走超时而非拒绝），
        断言精确匹配"连接失败"，且该消息不会误命中"连接超时"。
        """
        monkeypatch.setattr(
            tc.telnetlib,
            "Telnet",
            MagicMock(side_effect=ConnectionRefusedError("[Errno 111] Connection refused")),
        )
        client = TelnetClient(host="127.0.0.1", port=2323, timeout=1.0)
        try:
            with pytest.raises(TelnetClientError, match="连接失败") as exc_info:
                client.connect()
            assert "连接超时" not in str(exc_info.value)
            assert client.is_connected is False
        finally:
            client.close()

    def test_connect连接超时走TimeoutError分支(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """D 类：telnetlib 构造抛 TimeoutError 时精确走"连接超时"分支。"""
        monkeypatch.setattr(
            tc.telnetlib,
            "Telnet",
            MagicMock(side_effect=TimeoutError("timed out")),
        )
        client = TelnetClient(host="127.0.0.1", port=2323, timeout=1.0)
        try:
            with pytest.raises(TelnetClientError, match="连接超时") as exc_info:
                client.connect()
            assert "连接失败" not in str(exc_info.value)
        finally:
            client.close()

    def test_重复connect幂等且底层连接不重建(self, mock_server: MockTelnetServer) -> None:
        """T1：已连接后再次 connect 跳过，且 self._conn 必须是同一个对象。

        变异守卫：删除 connect() 的 is_connected 提前返回会新建第二条 TCP
        连接覆盖旧连接（旧连接泄漏），同一性断言即失败。
        """
        client = TelnetClient(host="127.0.0.1", port=mock_server.port, timeout=3.0)
        try:
            client.connect()
            first_conn = client._conn
            client.connect()
            assert client.is_connected is True
            assert client._conn is first_conn
        finally:
            client.close()

    def test_login完整流程成功(self, mock_server: MockTelnetServer) -> None:
        """服务端依次给出 login:/Password:/$ 时，login 返回 True。"""
        client = TelnetClient(host="127.0.0.1", port=mock_server.port, timeout=3.0)
        try:
            client.connect()
            assert client.login(username="root", password="admin") is True
        finally:
            client.close()

    def test_login空用户名抛value错误(self, logged_in_client: TelnetClient) -> None:
        """已连接但用户名为空时抛 ValueError（先于网络交互）。"""
        with pytest.raises(ValueError, match="不能为空"):
            logged_in_client.login(username="", password="admin")

    def test_login空密码抛value错误(self, logged_in_client: TelnetClient) -> None:
        """已连接但密码为空时抛 ValueError。"""
        with pytest.raises(ValueError, match="不能为空"):
            logged_in_client.login(username="root", password="")

    def test_login未连接抛telnet异常(self) -> None:
        """未 connect 直接 login 抛 TelnetClientError。"""
        client = TelnetClient(host="127.0.0.1", port=2323)
        try:
            with pytest.raises(TelnetClientError, match="未连接"):
                client.login(username="root", password="admin")
        finally:
            client.close()

    def test_login认证失败index小于0抛telnet异常(self) -> None:
        """T7：服务端只回 ERROR、不给 Password/shell 提示符时，expect 全超时
        （index<0），login 必须抛"登录超时"TelnetClientError。

        变异守卫：删除源码 index<0 分支会让超时被当成登录成功返回，本测试失败。
        """
        server = MockTelnetServer(mode="auth_fail")
        client = TelnetClient(host="127.0.0.1", port=server.port, timeout=0.5)
        try:
            client.connect()
            with pytest.raises(TelnetClientError, match="登录超时"):
                client.login(username="root", password="wrong")
        finally:
            client.close()
            server.close()

    def test_login_timeout显式参数被行使_快速失败(self) -> None:
        """T9：静默 server 不发 banner，login_timeout=0.1 必须在 ~0.3s 内超时。

        客户端默认 timeout=3.0；若源码忽略 login_timeout 恒用默认值，
        三次 expect 各等 3 秒，总耗时约 9 秒，elapsed<2 断言失败。
        """
        server = MockTelnetServer(mode="silent")
        client = TelnetClient(host="127.0.0.1", port=server.port, timeout=3.0)
        start = time.perf_counter()
        try:
            client.connect()
            with pytest.raises(TelnetClientError, match="登录超时"):
                client.login(username="root", password="admin", login_timeout=0.1)
            elapsed = time.perf_counter() - start
            assert elapsed < 2.0, f"login_timeout=0.1 未生效，实际 {elapsed:.2f}s"
        finally:
            client.close()
            server.close()


# ----------------------------------------------------------------------
# 命令执行
# ----------------------------------------------------------------------
class TestTelnetClientExecute:
    """execute 命令执行与异常路径测试。"""

    def test_execute无expect返回服务端输出(self, logged_in_client: TelnetClient) -> None:
        """无 expect 时收割服务端回显与固定结果行。"""
        output = logged_in_client.execute("uname", wait_time=0.2)
        assert "uname" in output
        assert "OK_RESULT" in output

    def test_execute有expect命中特征返回(self, logged_in_client: TelnetClient) -> None:
        """expect=OK_RESULT 时在服务端回复中命中后立即返回。"""
        output = logged_in_client.execute("whoami", expect="OK_RESULT", timeout=3.0)
        assert "OK_RESULT" in output

    def test_execute空命令抛value错误(self, logged_in_client: TelnetClient) -> None:
        """空字符串/纯空白命令抛 ValueError。"""
        with pytest.raises(ValueError, match="不能为空"):
            logged_in_client.execute("")
        with pytest.raises(ValueError, match="不能为空"):
            logged_in_client.execute("   ")

    def test_execute未连接抛telnet异常(self) -> None:
        """未 connect 直接 execute 抛 TelnetClientError。"""
        client = TelnetClient(host="127.0.0.1", port=2323)
        try:
            with pytest.raises(TelnetClientError, match="未连接"):
                client.execute("ls")
        finally:
            client.close()

    def test_expect特征超时抛telnet异常(self, logged_in_client: TelnetClient) -> None:
        """服务端回复不含 expect 特征时，超时抛 TelnetClientError（短超时加速）。"""
        with pytest.raises(TelnetClientError, match="超时"):
            logged_in_client.execute("ls", expect="##NEVER_APPEAR##", timeout=0.3)


# ----------------------------------------------------------------------
# 上下文管理器与关闭
# ----------------------------------------------------------------------
class TestTelnetClientLifecycle:
    """close 幂等性与 with 语句测试。"""

    def test_close幂等_重复关闭不抛异常(self, mock_server: MockTelnetServer) -> None:
        """连续 close 多次无副作用，is_connected 恒为 False。"""
        client = TelnetClient(host="127.0.0.1", port=mock_server.port, timeout=3.0)
        client.connect()
        client.close()
        client.close()
        assert client.is_connected is False

    def test_上下文管理器自动连接断开(self, mock_server: MockTelnetServer) -> None:
        """with 语句进入时建立连接、退出时自动断开。"""
        with TelnetClient(host="127.0.0.1", port=mock_server.port, timeout=3.0) as client:
            assert client.is_connected is True
        assert client.is_connected is False


# ----------------------------------------------------------------------
# mock 驱动：telnetlib 缺失与 login/execute/close 异常路径（E 类）
# ----------------------------------------------------------------------
class TestTelnetClientMockedPaths:
    """通过 monkeypatch/MagicMock 覆盖无真机难以触发的异常分支。

    覆盖：telnetlib 被移除（PEP 594）、login 的 EOFError/通用异常包装、
    execute 写入失败与读取 EOFError 包装、close 吞 OSError 容错。
    """

    def test_telnetlib不可用时connect抛运行时错误(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """E：模块级 telnetlib 为 None（Python 3.13+）时，connect 抛明确 RuntimeError。"""
        monkeypatch.setattr(tc, "telnetlib", None)
        client = TelnetClient(host="127.0.0.1", port=2323)
        try:
            with pytest.raises(RuntimeError, match="telnetlib 已在当前 Python 版本移除"):
                client.connect()
        finally:
            client.close()

    def test_login对端关闭EOFError包装为认证失败(self) -> None:
        """E：expect 抛 EOFError（对端关闭连接）时包装为"认证失败"TelnetClientError。"""
        fake_conn = MagicMock()
        fake_conn.expect.side_effect = EOFError()
        client = TelnetClient(host="127.0.0.1", port=2323)
        client._conn = fake_conn
        try:
            with pytest.raises(TelnetClientError, match="认证失败"):
                client.login(username="root", password="admin")
        finally:
            client.close()

    def test_login其他异常包装为登录流程异常(self) -> None:
        """E：expect 抛非 EOF 异常时走统一兜底，包装为"登录流程异常"。"""
        fake_conn = MagicMock()
        fake_conn.expect.side_effect = ValueError("unexpected expect state")
        client = TelnetClient(host="127.0.0.1", port=2323)
        client._conn = fake_conn
        try:
            with pytest.raises(TelnetClientError, match="登录流程异常"):
                client.login(username="root", password="admin")
        finally:
            client.close()

    def test_execute写入OSError包装为命令写入失败(self) -> None:
        """E：write 抛 OSError（连接断开）时包装为"命令写入失败"。"""
        fake_conn = MagicMock()
        fake_conn.write.side_effect = OSError("broken pipe")
        client = TelnetClient(host="127.0.0.1", port=2323)
        client._conn = fake_conn
        try:
            with pytest.raises(TelnetClientError, match="命令写入失败"):
                client.execute("ls")
        finally:
            client.close()

    def test_expect模式读取EOFError包装为读取输出失败(self) -> None:
        """E：expect 收割循环中 read_very_eager 抛 EOFError 时包装为"读取输出失败"。"""
        fake_conn = MagicMock()
        fake_conn.read_very_eager.side_effect = EOFError()
        client = TelnetClient(host="127.0.0.1", port=2323)
        client._conn = fake_conn
        try:
            with pytest.raises(TelnetClientError, match="读取输出失败"):
                client.execute("ls", expect="$", timeout=0.3)
        finally:
            client.close()

    def test_无expect收割时EOFError包装为读取输出失败(self) -> None:
        """E：无 expect 的固定收割中 read_very_eager 抛 EOFError 同样包装。"""
        fake_conn = MagicMock()
        fake_conn.read_very_eager.side_effect = EOFError()
        client = TelnetClient(host="127.0.0.1", port=2323)
        client._conn = fake_conn
        try:
            with pytest.raises(TelnetClientError, match="读取输出失败"):
                client.execute("ls", wait_time=0)
        finally:
            client.close()

    def test_close吞掉底层OSError不向上抛(self) -> None:
        """E：底层 close 抛 OSError 时仅警告，client.close() 不抛且实例置 None。"""
        fake_conn = MagicMock()
        fake_conn.close.side_effect = OSError("close failed")
        client = TelnetClient(host="127.0.0.1", port=2323)
        client._conn = fake_conn
        client.close()
        assert client._conn is None
