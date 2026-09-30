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
from collections.abc import Iterator

import pytest
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

    def __init__(self) -> None:
        """初始化监听 socket 与服务线程，绑定本地随机端口并开始监听。"""
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
        """线程主体：接受连接，走完登录序列后循环回显命令直到对端关闭。"""
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        self._conn = conn
        try:
            conn.sendall(b"login: ")
            conn.recv(1024)  # 用户名（不校验内容）
            conn.sendall(b"Password: ")
            conn.recv(1024)  # 密码（不校验内容）
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

    def test_connect无监听端口抛telnet异常(self) -> None:
        """连接无监听的本地端口被拒绝时，包装为 TelnetClientError。"""
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()  # 关闭后该端口无监听，连接应被 RST 拒绝
        client = TelnetClient(host="127.0.0.1", port=dead_port, timeout=1.0)
        try:
            with pytest.raises(TelnetClientError, match="连接"):
                client.connect()
            assert client.is_connected is False
        finally:
            client.close()

    def test_重复connect幂等(self, mock_server: MockTelnetServer) -> None:
        """已连接状态下再次 connect 直接跳过，不抛异常。"""
        client = TelnetClient(host="127.0.0.1", port=mock_server.port, timeout=3.0)
        try:
            client.connect()
            client.connect()
            assert client.is_connected is True
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
