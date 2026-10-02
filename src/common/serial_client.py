"""
串口通信封装模块（芯片嵌入式板卡测试适配层）

功能:
    - 基于pyserial的串口设备统一封装，支持Windows/Linux串口设备
    - 上下文管理器协议支持（with语句自动开关串口）
    - 命令发送与响应读取（read_until等待特征字符串 / read_all全量读取）
    - 完整异常处理: 设备不存在、端口占用、超时等场景均有明确异常与日志
    - 预留扩展位: 协议组包/解包、AT指令集解析（第二阶段按需实现）

使用示例:
    from src.common.serial_client import SerialClient

    with SerialClient(port="COM3", baudrate=115200) as client:
        output = client.send_command("AT+VERSION\r\n", expect="OK")
"""

import time

from src.common.logger import LogManager

logger = LogManager.get_logger()

# 防御性导入: pyserial未安装时给出明确安装指引，而非裸ImportError
try:
    import serial
    import serial.tools.list_ports
except ImportError as exc:  # pragma: no cover 环境缺依赖分支
    raise ImportError(
        "串口功能依赖pyserial，请先安装: pip install pyserial==3.5"
    ) from exc


class SerialClientError(Exception):
    """
    串口通信统一异常类

    封装串口操作中的设备级异常（打不开/超时/读写失败），
    携带端口上下文信息，便于板卡测试问题时快速定位。
    """

    def __init__(self, message: str, port: str | None = None):
        """
        初始化异常

        参数:
            message (str): 异常描述信息
            port (str | None): 发生异常的串口标识，用于日志排查

        返回:
            无
        """
        self.port = port
        super().__init__(f"[串口 {port}] {message}" if port else message)


class SerialClient:
    """
    串口通信客户端

    面向芯片嵌入式板卡测试场景，封装串口的打开、
    命令下发、响应读取与关闭全生命周期。

    属性:
        port (str): 串口设备标识（Windows: COM3 / Linux: /dev/ttyUSB0）
        baudrate (int): 波特率，芯片板卡常用115200
        timeout (float): 单次读取超时时间（秒）
        _serial (serial.Serial | None): 底层pyserial实例
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 3.0,
        bytesize: int = serial.EIGHTBITS,
        parity: str = serial.PARITY_NONE,
        stopbits: float = serial.STOPBITS_ONE,
    ):
        """
        初始化串口客户端（仅保存配置，不立即打开设备）

        参数:
            port (str): 串口设备标识，如COM3或/dev/ttyUSB0
            baudrate (int): 波特率，默认115200
            timeout (float): 读取超时时间（秒），默认3.0
            bytesize (int): 数据位，默认8位
            parity (str): 校验位，默认无校验
            stopbits (float): 停止位，默认1位

        返回:
            无

        异常:
            ValueError: port为空或baudrate非法时抛出
        """
        if not port or not str(port).strip():
            raise ValueError(f"非法串口标识: {port}")
        if baudrate <= 0:
            raise ValueError(f"非法波特率: {baudrate}，必须为正整数")

        self.port = str(port).strip()
        self.baudrate = baudrate
        self.timeout = timeout
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._serial: serial.Serial | None = None

        logger.debug(
            f"SerialClient配置就绪 | 端口: {self.port} | 波特率: {baudrate} | "
            f"超时: {timeout}s（设备将在open()或with语句时打开）"
        )

    # ------------------------------------------------------------------
    # 设备生命周期管理
    # ------------------------------------------------------------------
    def open(self) -> None:
        """
        打开串口设备

        参数:
            无

        返回:
            无

        异常:
            SerialClientError: 设备不存在/端口被占用/权限不足时抛出，
                               异常信息附端口名与可用串口列表提示
        """
        if self.is_open:
            logger.debug(f"串口 {self.port} 已处于打开状态，跳过重复打开")
            return

        # 协议URL（如 pyserial 的 loop:// 回环伪串口，Day39 测试用；
        # socket:// 等远端串口同理）含"://"，不在系统物理端口枚举中，
        # 需跳过 comports 前置校验并改走 serial_for_url
        port_value = str(self.port)
        is_protocol_url = "://" in port_value

        # 原先此处有 comports() 前置硬校验，端口不在枚举列表中直接拒绝打开。
        # 该校验在 macOS 上是错的: pyserial 的 comports() 不枚举 /dev/cu.*，
        # 而 serial.Serial 本身能正常打开这些设备——结果是 macOS 上板卡测试
        # 完全不可用（socat 虚拟端口、未在 /sys/class/tty 暴露的 chardev 同理）。
        # 改为"先尝试打开、失败时用 comports 补充提示信息"：既保留可读的错误
        # 提示，又不再误拒有效端口。

        try:
            if is_protocol_url:
                # 协议URL走serial_for_url：loop://可在无真机时回环自测
                self._serial = serial.serial_for_url(
                    port_value,
                    baudrate=self.baudrate,
                    bytesize=self._bytesize,
                    parity=self._parity,
                    stopbits=self._stopbits,
                    timeout=self.timeout,
                    write_timeout=self.timeout,
                )
            else:
                # 普通 COM/设备路径走物理串口
                # write_timeout 必设: pyserial 的 timeout 只作用于**读**，
                # 写路径默认永久阻塞。板卡进入复位死循环/USB转串口桥固件卡死/
                # 流控未释放时，write() 会无限期挂住整条AT指令序列，
                # 且因为阻塞发生在 with 块内部 __exit__ 永不执行、串口句柄
                # 一直不释放——这是硬件在环测试最典型的挂死形态。
                # 超时抛 SerialTimeoutException（SerialException 子类），
                # 由下面的 except 统一包装成 SerialClientError。
                self._serial = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=self._bytesize,
                    parity=self._parity,
                    stopbits=self._stopbits,
                    timeout=self.timeout,
                    write_timeout=self.timeout,
                )
            logger.info(f"串口打开成功 | {self.port} @ {self.baudrate}bps")
        except (serial.SerialException, ValueError) as exc:
            # serial_for_url 对未知协议抛 ValueError（不是 SerialException
            # 子类），修复前会裸逃出 open()，调用方按文档写
            # `except SerialClientError` 接不住
            self._serial = None
            available_ports = [
                info.device for info in serial.tools.list_ports.comports()
            ]
            logger.error(f"串口打开失败 | {self.port} | {exc}")
            raise SerialClientError(
                f"串口打开失败: {exc}"
                f"（当前系统可用串口: {available_ports or '无'}）",
                port=self.port,
            ) from exc

    def close(self) -> None:
        """
        关闭串口设备并释放资源（幂等，重复关闭无副作用）

        参数:
            无

        返回:
            无

        异常:
            无（关闭失败仅记录警告，不向上抛出）
        """
        if self._serial is not None and self._serial.is_open:
            try:
                self._serial.close()
                logger.info(f"串口已关闭 | {self.port}")
            except serial.SerialException as exc:
                logger.warning(f"串口关闭异常（已忽略） | {self.port} | {exc}")
        self._serial = None

    @property
    def is_open(self) -> bool:
        """
        串口是否处于打开状态

        参数:
            无

        返回:
            bool: True表示已打开且可用
        """
        return self._serial is not None and self._serial.is_open

    # ------------------------------------------------------------------
    # 数据读写
    # ------------------------------------------------------------------
    def send_command(
        self,
        command: str,
        expect: str | None = None,
        wait_time: float = 0.5,
        encoding: str = "utf-8",
    ) -> str:
        """
        向板卡发送命令并读取响应

        参数:
            command (str): 待发送的命令字符串（调用方自行携带\r\n等终止符）
            expect (str | None): 期望响应中出现的特征字符串；None时读取wait_time时长内的全部输出
            wait_time (float): 发送后等待板卡响应的稳定时间（秒），默认0.5
            encoding (str): 编码格式，默认utf-8

        返回:
            str: 板卡响应的解码文本（去除首尾空白）

        异常:
            SerialClientError: 串口未打开、写入失败或读取超时时抛出
            ValueError: command为空时抛出
        """
        if not command:
            raise ValueError("待发送命令不能为空")
        if not self.is_open:
            raise SerialClientError("串口未打开，请先调用open()或使用with语句", port=self.port)

        # 清空发送前残留的接收缓冲，确保响应归属当前命令
        self._reset_input_buffer()

        try:
            self._serial.write(command.encode(encoding))
            self._serial.flush()
            logger.debug(f"串口命令已发送 >>> [{self.port}] {command!r}")
        except serial.SerialException as exc:
            logger.error(f"串口写入失败 | {self.port} | {exc}")
            raise SerialClientError(f"命令写入失败: {exc}", port=self.port) from exc

        # 等待板卡处理并输出响应
        time.sleep(wait_time)

        if expect is not None:
            return self.read_until(expect=expect, encoding=encoding)
        return self.read_all(encoding=encoding)

    def read_until(
        self,
        expect: str,
        timeout: float | None = None,
        encoding: str = "utf-8",
    ) -> str:
        """
        持续读取串口数据，直到出现特征字符串或超时

        参数:
            expect (str): 期望出现的特征字符串（如命令回显或OK/ERROR）
            timeout (float | None): 超时时间（秒）；None时使用客户端默认超时
            encoding (str): 解码格式，默认utf-8

        返回:
            str: 截至特征字符串的全部响应文本（含特征字符串本身）

        异常:
            SerialClientError: 超时未读到特征字符串时抛出，信息附带已收到的内容
        """
        if not self.is_open:
            raise SerialClientError("串口未打开，无法读取", port=self.port)

        # 只把 None 当"未指定"，显式传 0（falsy）必须原样生效。
        # 原写法 `timeout or self.timeout` 是典型反模式: 调用方传 0 表达
        # "零等待/立即返回"时被静默改写成默认 3.0s，批量探测可用端口的
        # 轮询逻辑每轮多等3秒，N轮放大N倍。
        effective_timeout = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + effective_timeout
        expect_bytes = expect.encode(encoding)
        buffer = bytearray()

        try:
            while time.monotonic() < deadline:
                n_bytes = self._serial.in_waiting
                if n_bytes:
                    buffer.extend(self._serial.read(n_bytes))
                    if expect_bytes in buffer:
                        result = buffer.decode(encoding, errors="replace")
                        logger.debug(f"串口响应已接收 <<< [{self.port}] {result!r}")
                        return result
                else:
                    time.sleep(0.05)
        except serial.SerialException as exc:
            logger.error(f"串口读取异常 | {self.port} | {exc}")
            raise SerialClientError(f"串口读取异常: {exc}", port=self.port) from exc

        received = buffer.decode(encoding, errors="replace")
        logger.error(
            f"串口读取超时 | {self.port} | 等待特征: {expect!r} | 已收到: {received!r}"
        )
        raise SerialClientError(
            f"读取超时: 未等到特征字符串 {expect!r}，已收到内容: {received!r}",
            port=self.port,
        )

    def read_all(self, encoding: str = "utf-8") -> str:
        """
        读取接收缓冲区当前全部数据（非阻塞，读完即返回）

        参数:
            encoding (str): 解码格式，默认utf-8

        返回:
            str: 缓冲区全部数据的解码文本；无数据时返回空字符串

        异常:
            SerialClientError: 串口未打开或读取异常时抛出
        """
        if not self.is_open:
            raise SerialClientError("串口未打开，无法读取", port=self.port)

        try:
            n_bytes = self._serial.in_waiting
            if n_bytes:
                data = self._serial.read(n_bytes)
                result = data.decode(encoding, errors="replace")
                logger.debug(f"串口响应已接收 <<< [{self.port}] {result!r}")
                return result
            return ""
        except serial.SerialException as exc:
            logger.error(f"串口读取异常 | {self.port} | {exc}")
            raise SerialClientError(f"串口读取异常: {exc}", port=self.port) from exc

    def _reset_input_buffer(self) -> None:
        """
        清空接收缓冲区（发送新命令前调用，防止残留数据干扰）

        参数:
            无

        返回:
            无

        异常:
            无（失败仅记录警告）
        """
        try:
            self._serial.reset_input_buffer()
        except serial.SerialException as exc:
            logger.warning(f"清空接收缓冲失败（已忽略） | {self.port} | {exc}")

    # ------------------------------------------------------------------
    # 上下文管理器协议
    # ------------------------------------------------------------------
    def __enter__(self) -> "SerialClient":
        """
        进入上下文管理器，自动打开串口

        返回:
            SerialClient: 当前客户端实例

        异常:
            SerialClientError: 串口打开失败时抛出（透传open()异常）
        """
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        退出上下文管理器，自动关闭串口

        参数:
            exc_type: 异常类型（无异常时为None）
            exc_val: 异常值
            exc_tb: 异常堆栈

        返回:
            无（不吞异常，原样透传）
        """
        self.close()

    @staticmethod
    def list_available_ports() -> list:
        """
        列出当前系统全部可用串口设备（板卡接入排查工具）

        参数:
            无

        返回:
            list: 可用串口标识列表，如 ['COM3', 'COM5']
        """
        ports = [info.device for info in serial.tools.list_ports.comports()]
        logger.info(f"系统可用串口设备: {ports or '无'}")
        return ports
