"""
SerialClient 串口封装单元测试（loop:// 伪串口 + MagicMock/monkeypatch 双方案）

覆盖范围:
    - SerialClientError 异常消息格式（带/不带 port）
    - SerialClient 构造参数校验（空 port、非法 baudrate）
    - loop:// 回环伪串口的 open/close/is_open 与重复调用幂等性
      （幂等断言到底层 serial 实例同一性，防旧连接泄漏）
    - 上下文管理器 __enter__/__exit__ 自动开关
    - send_command 分派验证（有 expect 必须走 read_until、无 expect 走 read_all，
      用 MagicMock 断言调用路径，特征串由 mock 注入而非命令回显自带）
    - _reset_input_buffer 每次发送前调用一次、read_until 显式 timeout 真正生效
    - send_command/read_until/read_all 的异常路径（空命令、未打开、超时、空缓冲）
    - 物理端口不在枚举、Serial 构造/write/read/close/reset 抛 SerialException
      时统一包装为 SerialClientError（含失败容错不阻断）
    - list_available_ports 对 comports() 枚举结果原样透传（monkeypatch 假设备）

测试方案:
    使用 pyserial 内置 loop:// 协议 URL：写入的字节经回环立即可读，
    无需物理串口/USB 转串口模块。SerialClient.open() 对含 "://" 的
    端口标识走 serial_for_url 分支，与物理 COM 口走同一套读写封装；
    无真机难以触发的物理端口/SerialException 分支用 MagicMock 与
    monkeypatch 注入。变异测试（Day39-fix）9 个盲区点全部可被本文件抓住。
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock

import pytest
import serial
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

    def test_重复open幂等不抛异常且底层实例不重建(self) -> None:
        """已打开状态下再次 open 直接跳过：不报错，且底层串口实例必须是同一个对象。

        变异守卫：若删除 open() 中的 is_open 提前返回，第二次 open 会新建
        serial 实例覆盖 self._serial，本断言即失败（防止旧连接泄漏）。
        """
        loop_client_proxy = SerialClient(port="loop://")
        loop_client_proxy.open()
        first_serial = loop_client_proxy._serial
        try:
            loop_client_proxy.open()
            assert loop_client_proxy.is_open is True
            assert loop_client_proxy._serial is first_serial
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

    def test_send_command有expect走read_until分支_响应不依赖命令回显(self) -> None:
        """expect 非 None 时必须走 read_until 分派，且响应来自设备而非命令自身回显。

        变异守卫（S2）：命令 "AT+VERSION\\n" 不含特征串 "DEVICE_READY"，
        read_until 被 mock 为返回设备注入的响应；若源码误删 expect 分支
        恒走 read_all，则 read_until 零调用且拿不到注入响应，断言失败。
        """
        fake_serial = MagicMock()
        fake_serial.is_open = True
        client = SerialClient(port="loop://")
        client._serial = fake_serial
        client.read_until = MagicMock(return_value="DEVICE_READY")  # send_command 调 self.read_until
        client.read_all = MagicMock(return_value="ALL_OUTPUT")
        try:
            result = client.send_command("AT+VERSION\n", expect="DEVICE_READY", wait_time=0)
            assert result == "DEVICE_READY"
            client.read_until.assert_called_once()
            assert "DEVICE_READY" not in "AT+VERSION\n"  # 特征串确实不来自命令回显
            client.read_all.assert_not_called()
        finally:
            client.close()

    def test_send_command无expect走read_all分支(self) -> None:
        """expect=None 时走 read_all 收割分支，read_until 不被调用（S2 对照）。"""
        fake_serial = MagicMock()
        fake_serial.is_open = True
        client = SerialClient(port="loop://")
        client._serial = fake_serial
        client.read_until = MagicMock(return_value="DEVICE_READY")
        client.read_all = MagicMock(return_value="ALL_OUTPUT")
        try:
            result = client.send_command("AT+VERSION\n", wait_time=0)
            assert result == "ALL_OUTPUT"
            client.read_all.assert_called_once()
            client.read_until.assert_not_called()
        finally:
            client.close()

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

    def test_read_until真实回环命中特征返回内容(self, loop_client: SerialClient) -> None:
        """真 loop:// 端到端：read_until 在回环缓冲中命中特征串并返回（覆盖成功返回分支）。"""
        loop_client._serial.write(b"BOOT_OK\n")
        loop_client._serial.flush()
        result = loop_client.read_until(expect="BOOT_OK", timeout=1.0)
        assert "BOOT_OK" in result

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

    def test_list_available_ports返回系统枚举内容(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """monkeypatch comports 返回含假设备的枚举，断言结果原样透传设备名。

        变异守卫（S6）：若 list_available_ports 恒返回 []，假设备名丢失即失败，
        取代旧版恒真的 isinstance(list) 同义反复断言。
        """
        monkeypatch.setattr(
            serial.tools.list_ports,
            "comports",
            lambda: [SimpleNamespace(device="COM_FAKE_99")],
        )
        ports = SerialClient.list_available_ports()
        assert isinstance(ports, list)
        assert "COM_FAKE_99" in ports


# ----------------------------------------------------------------------
# mock 驱动：分派验证（S7/S8）与物理/异常路径（E 类）
# ----------------------------------------------------------------------
class TestSerialClientMockedPaths:
    """通过 monkeypatch/MagicMock 覆盖无真机难以触发的分支。

    覆盖：send_command 前置清缓冲调用（S7）、read_until 显式超时生效（S8）、
    物理端口不存在、串口构造/写入/读取/关闭异常包装、清缓冲失败容错。
    """

    def test_send_command发送前调用reset_input_buffer一次(self) -> None:
        """S7：每次发命令前必须先清接收缓冲，防止上次残留串入本次响应。"""
        fake_serial = MagicMock()
        fake_serial.is_open = True
        fake_serial.read_all.return_value = ""
        client = SerialClient(port="loop://")
        client._serial = fake_serial
        try:
            client.send_command("AT\n", wait_time=0)
            fake_serial.reset_input_buffer.assert_called_once()
        finally:
            client.close()

    def test_read_until显式timeout短于客户端默认超时_快速失败(self) -> None:
        """S8：显式 timeout 必须真正缩短等待，而非被默认 3.0s 覆盖。

        真 loop:// 且缓冲无数据：client 默认 timeout=2.0，显式传 0.05，
        必须在 1 秒内抛超时；若源码忽略显式值恒用默认值，耗时约 2 秒断言失败。
        （断言的是 deadline 生效，不依赖固定 sleep 赌结果，超时是确定性事件。）
        """
        client = SerialClient(port="loop://", timeout=2.0)
        client.open()
        start = time.perf_counter()
        try:
            with pytest.raises(SerialClientError, match="读取超时"):
                client.read_until(expect="##NEVER_APPEAR##", timeout=0.05)
            elapsed = time.perf_counter() - start
            assert elapsed < 1.0, f"显式 0.05s 超时未生效，实际耗时 {elapsed:.2f}s（疑似用了默认 2.0s）"
        finally:
            client.close()

    def test_物理端口不在枚举时open抛串口异常(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """
        E：comports 枚举为空时 open 物理端口，抛带设备列表提示的 SerialClientError。

        注: Day43 起不再做 comports 前置硬拦截（macOS 不枚举 /dev/cu.*，
        硬拦截会让板卡测试在该平台完全不可用），改为"先尝试打开、失败时
        用 comports 补充提示"。断言口径随之从"设备不存在"改为
        "打开失败且附可用设备列表"。
        """
        monkeypatch.setattr(serial.tools.list_ports, "comports", lambda: [])
        client = SerialClient(port="COM_NOT_EXIST")
        with pytest.raises(SerialClientError) as exc_info:
            client.open()
        message = str(exc_info.value)
        assert "串口打开失败" in message, f"应报告打开失败 | 实际: {message}"
        assert "可用串口" in message, (
            f"失败信息应附带可用串口列表供排障 | 实际: {message}"
        )
        assert client._serial is None

    def test_串口构造serial异常包装为串口异常(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """E：物理 serial.Serial 构造抛 SerialException 时包装为 SerialClientError。"""
        monkeypatch.setattr(
            serial.tools.list_ports,
            "comports",
            lambda: [SimpleNamespace(device="COM1")],
        )
        monkeypatch.setattr(
            serial,
            "Serial",
            MagicMock(side_effect=serial.SerialException("access denied")),
        )
        client = SerialClient(port="COM1")
        with pytest.raises(SerialClientError, match="串口打开失败"):
            client.open()
        assert client._serial is None

    def test_命令写入serial异常包装为串口异常(self) -> None:
        """E：write 抛 SerialException 时包装为 SerialClientError（命令写入失败）。"""
        fake_serial = MagicMock()
        fake_serial.is_open = True
        fake_serial.write.side_effect = serial.SerialException("write io error")
        client = SerialClient(port="loop://")
        client._serial = fake_serial
        try:
            with pytest.raises(SerialClientError, match="命令写入失败"):
                client.send_command("AT\n", wait_time=0)
        finally:
            client.close()

    def test_read_until读取serial异常包装为串口异常(self) -> None:
        """E：read_until 轮询中 in_waiting 抛 SerialException 时包装为读取异常。"""
        fake_serial = MagicMock()
        type(fake_serial).in_waiting = PropertyMock(
            side_effect=serial.SerialException("read io error")
        )
        client = SerialClient(port="loop://")
        client._serial = fake_serial
        try:
            with pytest.raises(SerialClientError, match="读取异常"):
                client.read_until(expect="X", timeout=0.3)
        finally:
            client.close()

    def test_read_all读取serial异常包装为串口异常(self) -> None:
        """E：read_all 中 in_waiting 抛 SerialException 时包装为读取异常。"""
        fake_serial = MagicMock()
        type(fake_serial).in_waiting = PropertyMock(
            side_effect=serial.SerialException("read io error")
        )
        client = SerialClient(port="loop://")
        client._serial = fake_serial
        try:
            with pytest.raises(SerialClientError, match="读取异常"):
                client.read_all()
        finally:
            client.close()

    def test_close吞掉底层关闭异常不向上抛(self) -> None:
        """E：底层 close 抛 SerialException 时仅记录警告，close() 不抛且实例置 None。"""
        fake_serial = MagicMock()
        fake_serial.is_open = True
        fake_serial.close.side_effect = serial.SerialException("close io error")
        client = SerialClient(port="loop://")
        client._serial = fake_serial
        client.close()  # 不抛异常
        assert client._serial is None

    def test_清空接收缓冲失败不阻断命令发送(self) -> None:
        """E：reset_input_buffer 抛异常仅警告，send_command 继续执行并正常返回。"""
        fake_serial = MagicMock()
        fake_serial.is_open = True
        fake_serial.in_waiting = 16
        fake_serial.reset_input_buffer.side_effect = serial.SerialException("reset io error")
        fake_serial.read.return_value = b"AFTER_RESET_FAIL"
        client = SerialClient(port="loop://")
        client._serial = fake_serial
        try:
            result = client.send_command("AT\n", wait_time=0)
            assert result == "AFTER_RESET_FAIL"
            fake_serial.write.assert_called_once()
        finally:
            client.close()
