"""
SerialClient 串口封装单元测试（loop:// 伪串口方案，不依赖真实硬件）

覆盖范围:
    - SerialClientError 异常消息格式（带/不带 port）
    - SerialClient 构造参数校验（空 port、非法 baudrate）
    - loop:// 回环伪串口的 open/close/is_open 与重复调用幂等性
    - 上下文管理器 __enter__/__exit__ 自动开关
    - send_command 回环读取（无 expect 全量收割 / 有 expect 特征等待）
    - send_command/read_until/read_all 的异常路径（空命令、未打开、超时、空缓冲）
    - list_available_ports 静态方法返回类型

测试方案:
    使用 pyserial 内置 loop:// 协议 URL：写入的字节经回环立即可读，
    无需物理串口/USB 转串口模块。SerialClient.open() 对含 "://" 的
    端口标识走 serial_for_url 分支，与物理 COM 口走同一套读写封装。
"""

import pytest
from src.common.serial_client import SerialClient, SerialClientError


# ----------------------------------------------------------------------
# fixture
# ----------------------------------------------------------------------
@pytest.fixture
def loop_client() -> SerialClient:
    """提供一个已打开的 loop:// 回环客户端，用例结束后保证关闭。

    返回:
        SerialClient: 已 open 的回环串口客户端实例
    """
    client = SerialClient(port="loop://", baudrate=115200, timeout=0.5)
    client.open()
    yield client
    client.close()


# ----------------------------------------------------------------------
# SerialClientError
# ----------------------------------------------------------------------
class TestSerialClientError:
    """串口统一异常类的消息封装测试。"""

    def test_异常消息_不带端口时为原文(self) -> None:
        """不带 port 初始化时，异常字符串就是原始消息，不加端口前缀。"""
        err = SerialClientError("读取失败")
        assert str(err) == "读取失败"
        assert err.port is None

    def test_异常消息_带端口时含端口前缀(self) -> None:
        """带 port 初始化时，异常字符串含 [串口 COMx] 前缀且 port 被保存。"""
        err = SerialClientError("打开失败", port="COM3")
        assert err.port == "COM3"
        assert str(err) == "[串口 COM3] 打开失败"


# ----------------------------------------------------------------------
# 构造参数校验
# ----------------------------------------------------------------------
class TestSerialClientInit:
    """SerialClient 初始化参数校验测试。"""

    def test_空端口抛value错误(self) -> None:
        """port 为空字符串时抛 ValueError，阻止生成无标识客户端。"""
        with pytest.raises(ValueError, match="非法串口标识"):
            SerialClient(port="")

    def test_纯空白端口抛value错误(self) -> None:
        """port 为纯空白字符时抛 ValueError（strip 后仍为空）。"""
        with pytest.raises(ValueError, match="非法串口标识"):
            SerialClient(port="   ")

    def test_零波特率抛value错误(self) -> None:
        """baudrate=0 非正数，抛 ValueError。"""
        with pytest.raises(ValueError, match="非法波特率"):
            SerialClient(port="loop://", baudrate=0)

    def test_负波特率抛value错误(self) -> None:
        """baudrate 为负数时抛 ValueError。"""
        with pytest.raises(ValueError, match="非法波特率"):
            SerialClient(port="loop://", baudrate=-9600)

    def test_合法参数保存并处于未打开状态(self) -> None:
        """合法参数仅保存配置，构造后不自动打开设备。"""
        client = SerialClient(port="loop://", baudrate=9600, timeout=1.0)
        assert client.port == "loop://"
        assert client.baudrate == 9600
        assert client.timeout == 1.0
        assert client.is_open is False
        client.close()


# ----------------------------------------------------------------------
# loop:// 生命周期
# ----------------------------------------------------------------------
class TestSerialClientLoopback:
    """loop:// 伪串口打开/关闭/上下文管理器测试。"""

    def test_open成功后is_open为真(self) -> None:
        """open 回环伪串口成功，is_open 返回 True。"""
        client = SerialClient(port="loop://")
        try:
            client.open()
            assert client.is_open is True
        finally:
            client.close()

    def test_重复open幂等不抛异常(self) -> None:
        """已打开状态下再次 open 直接跳过，不重复创建设备也不报错。"""
        loop_client_proxy = SerialClient(port="loop://")
        loop_client_proxy.open()
        try:
            loop_client_proxy.open()
            assert loop_client_proxy.is_open is True
        finally:
            loop_client_proxy.close()

    def test_close幂等_重复关闭不抛异常(self, loop_client: SerialClient) -> None:
        """连续 close 多次无副作用（底层实例已置 None）。"""
        loop_client.close()
        loop_client.close()
        assert loop_client.is_open is False

    def test_上下文管理器自动开关(self) -> None:
        """with 语句进入时打开、退出时自动关闭。"""
        with SerialClient(port="loop://", timeout=0.5) as client:
            assert client.is_open is True
        assert client.is_open is False


# ----------------------------------------------------------------------
# 命令读写
# ----------------------------------------------------------------------
class TestSerialClientCommands:
    """loop:// 下回环命令收发与异常路径测试。"""

    def test_send_command无expect回环读取全部输出(self, loop_client: SerialClient) -> None:
        """无 expect 时收割 wait_time 内全部回环输出，内容为发送命令本身。"""
        result = loop_client.send_command("PING\n", wait_time=0.1)
        assert "PING" in result

    def test_send_command有expect等待特征字符串(self, loop_client: SerialClient) -> None:
        """发送含 OK 的命令，expect=OK 在回环数据中命中后返回。"""
        result = loop_client.send_command("AT+VERSION OK\n", expect="OK", wait_time=0.1)
        assert "OK" in result

    def test_send_command空命令抛value错误(self, loop_client: SerialClient) -> None:
        """空字符串命令抛 ValueError，不触达底层串口。"""
        with pytest.raises(ValueError, match="不能为空"):
            loop_client.send_command("")

    def test_send_command未打开抛串口异常(self) -> None:
        """未 open 直接发命令抛 SerialClientError，消息提示先打开。"""
        client = SerialClient(port="loop://")
        with pytest.raises(SerialClientError, match="未打开"):
            client.send_command("ATI\n")
        client.close()

    def test_read_until超时抛串口异常(self, loop_client: SerialClient) -> None:
        """expect 特征在超时窗口内不出现（只写不含特征的数据）时抛 SerialClientError。"""
        loop_client._serial.write(b"NO_MARKER_HERE\n")  # noqa: SLF001 白盒构造超时输入
        loop_client._serial.flush()  # noqa: SLF001
        with pytest.raises(SerialClientError, match="读取超时"):
            loop_client.read_until(expect="##NEVER_APPEAR##", timeout=0.2)

    def test_read_until未打开抛串口异常(self) -> None:
        """未打开时 read_until 抛 SerialClientError。"""
        client = SerialClient(port="loop://")
        with pytest.raises(SerialClientError, match="未打开"):
            client.read_until(expect="OK")
        client.close()

    def test_read_all空缓冲返回空字符串(self, loop_client: SerialClient) -> None:
        """接收缓冲无数据时 read_all 返回空字符串而非报错。"""
        loop_client._serial.reset_input_buffer()  # noqa: SLF001 确保缓冲为空
        assert loop_client.read_all() == ""

    def test_read_all未打开抛串口异常(self) -> None:
        """未打开时 read_all 抛 SerialClientError。"""
        client = SerialClient(port="loop://")
        with pytest.raises(SerialClientError, match="未打开"):
            client.read_all()
        client.close()


# ----------------------------------------------------------------------
# 工具方法
# ----------------------------------------------------------------------
class TestSerialClientEdgeCases:
    """其余工具方法与边界行为测试。"""

    def test_list_available_ports返回列表类型(self) -> None:
        """list_available_ports 恒返回 list（无物理串口时为空列表，不抛异常）。"""
        ports = SerialClient.list_available_ports()
        assert isinstance(ports, list)
