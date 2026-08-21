"""
Telnet网口通信封装模块（芯片板卡远程控制适配层）

功能:
    - 基于telnetlib的远程设备控制封装（板卡Linux系统常用登录方式）
    - 支持连接、账号登录、命令执行、期望特征等待全流程
    - 上下文管理器协议支持（with语句自动连接与断开）
    - 完整异常处理: 连接失败、认证失败、执行超时均有明确异常与日志
    - 预留扩展位: 批量命令脚本执行、多板卡并行控制（第二阶段按需实现）

兼容性说明:
    telnetlib为Python标准库，自3.11起标记Deprecated（3.13移除）。
    本项目锁定Python 3.11，可正常使用；此处的DeprecationWarning已被定向屏蔽，
    后续如升级Python版本，可平滑切换至telnetlib3或自研socket实现。

使用示例:
    from src.common.telnet_client import TelnetClient

    with TelnetClient(host="192.168.1.100", port=23) as client:
        client.login(username="root", password="admin")
        output = client.execute("uname -a", expect="#")
"""

import time
import warnings
from typing import Optional

from src.common.logger import LogManager

logger = LogManager.get_logger()

# 定向屏蔽telnetlib的弃用告警（项目已锁定Python 3.11，功能可用）
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    import telnetlib


class TelnetClientError(Exception):
    """
    Telnet通信统一异常类

    封装远程连接、登录认证、命令执行中的异常，
    携带目标设备信息，便于板卡测试问题时快速定位。
    """

    def __init__(self, message: str, host: Optional[str] = None):
        """
        初始化异常

        参数:
            message (str): 异常描述信息
            host (str | None): 目标设备地址，用于日志排查

        返回:
            无
        """
        self.host = host
        super().__init__(f"[Telnet {host}] {message}" if host else message)


class TelnetClient:
    """
    Telnet远程控制客户端

    面向芯片板卡远程控制场景（板卡跑Linux系统的网口登录），
    封装连接、登录、命令执行与断开全生命周期。

    属性:
        host (str): 目标板卡IP地址
        port (int): Telnet服务端口，标准端口23
        timeout (float): 连接与读取超时时间（秒）
        _conn (telnetlib.Telnet | None): 底层Telnet连接实例
    """

    def __init__(self, host: str, port: int = 23, timeout: float = 10.0):
        """
        初始化Telnet客户端（仅保存配置，不立即连接）

        参数:
            host (str): 目标板卡IP地址，如192.168.1.100
            port (int): Telnet端口，默认23
            timeout (float): 连接与读取超时时间（秒），默认10.0

        返回:
            无

        异常:
            ValueError: host为空或port非法时抛出
        """
        if not host or not str(host).strip():
            raise ValueError(f"非法目标地址: {host}")
        if not 0 < port < 65536:
            raise ValueError(f"非法端口号: {port}，有效范围1-65535")

        self.host = str(host).strip()
        self.port = port
        self.timeout = timeout
        self._conn: Optional[telnetlib.Telnet] = None

        logger.debug(
            f"TelnetClient配置就绪 | 目标: {self.host}:{port} | "
            f"超时: {timeout}s（连接将在connect()或with语句时建立）"
        )

    # ------------------------------------------------------------------
    # 连接生命周期管理
    # ------------------------------------------------------------------
    def connect(self) -> None:
        """
        建立Telnet连接

        参数:
            无

        返回:
            无

        异常:
            TelnetClientError: 连接超时、拒绝或主机不可达时抛出
        """
        if self.is_connected:
            logger.debug(f"Telnet {self.host}:{self.port} 已连接，跳过重复连接")
            return

        try:
            self._conn = telnetlib.Telnet(self.host, self.port, timeout=self.timeout)
            logger.info(f"Telnet连接成功 | {self.host}:{self.port}")
        except TimeoutError as exc:
            self._conn = None
            logger.error(f"Telnet连接超时 | {self.host}:{self.port} | {self.timeout}s内未建立连接")
            raise TelnetClientError(
                f"连接超时: {self.host}:{self.port}（{self.timeout}s），请检查网络连通性",
                host=self.host,
            ) from exc
        except OSError as exc:
            self._conn = None
            logger.error(f"Telnet连接失败 | {self.host}:{self.port} | {exc}")
            raise TelnetClientError(
                f"连接失败: {self.host}:{self.port} - {exc}",
                host=self.host,
            ) from exc

    def login(
        self,
        username: str,
        password: str,
        login_timeout: Optional[float] = None,
    ) -> bool:
        """
        登录板卡系统（自动识别Login/Password提示）

        参数:
            username (str): 登录用户名
            password (str): 登录密码
            login_timeout (float | None): 登录流程超时（秒）；None时使用默认超时

        返回:
            bool: 登录成功返回True

        异常:
            TelnetClientError: 未连接、登录超时或认证失败时抛出
            ValueError: 用户名或密码为空时抛出
        """
        if not self.is_connected:
            raise TelnetClientError("Telnet未连接，请先调用connect()", host=self.host)
        if not username or not password:
            raise ValueError("登录用户名与密码不能为空")

        timeout = login_timeout or self.timeout

        try:
            # 等待用户名提示符（兼容login:/Login:/Username:多种风格）
            self._conn.expect([b"[Ll]ogin:", b"[Uu]sername:"], timeout)
            self._conn.write(username.encode("ascii") + b"\n")

            # 等待密码提示符
            self._conn.expect([b"[Pp]assword:"], timeout)
            self._conn.write(password.encode("ascii") + b"\n")

            # 登录成功特征: 普通用户$或root用户#的Shell提示符
            index, _, _ = self._conn.expect([b"[$#]"], timeout)
        except EOFError as exc:
            logger.error(f"Telnet登录认证失败（连接被对端关闭） | {self.host}")
            raise TelnetClientError(
                "登录认证失败: 用户名或密码错误（对端关闭连接）", host=self.host
            ) from exc
        except Exception as exc:  # noqa: BLE001 expect超时等统一兜底
            logger.error(f"Telnet登录流程异常 | {self.host} | {exc}")
            raise TelnetClientError(f"登录流程异常: {exc}", host=self.host) from exc

        if index < 0:
            logger.error(
                f"Telnet登录超时 | {self.host} | {timeout}s内未识别到Shell提示符（$或#）"
            )
            raise TelnetClientError(
                f"登录超时: {timeout}s内未出现Shell提示符，认证可能失败", host=self.host
            )

        logger.info(f"Telnet登录成功 | {self.host} | 用户: {username}")
        return True

    def close(self) -> None:
        """
        断开Telnet连接并释放资源（幂等，重复调用无副作用）

        参数:
            无

        返回:
            无

        异常:
            无（关闭失败仅记录警告，不向上抛出）
        """
        if self._conn is not None:
            try:
                self._conn.close()
                logger.info(f"Telnet连接已断开 | {self.host}:{self.port}")
            except Exception as exc:  # noqa: BLE001 关闭异常统一兜底
                logger.warning(f"Telnet断开异常（已忽略） | {self.host} | {exc}")
        self._conn = None

    @property
    def is_connected(self) -> bool:
        """
        Telnet连接是否处于建立状态

        参数:
            无

        返回:
            bool: True表示连接可用
        """
        return self._conn is not None

    # ------------------------------------------------------------------
    # 命令执行
    # ------------------------------------------------------------------
    def execute(
        self,
        command: str,
        expect: Optional[str] = None,
        wait_time: float = 1.0,
        timeout: Optional[float] = None,
    ) -> str:
        """
        在板卡远程Shell中执行命令并获取输出

        参数:
            command (str): 待执行的Shell命令
            expect (str | None): 输出完成特征字符串（如提示符#）；None时固定等待wait_time后收割输出
            wait_time (float): 命令执行后的稳定等待时间（秒），默认1.0
            timeout (float | None): 等待特征字符串的超时（秒）；None时使用默认超时

        返回:
            str: 命令完整输出文本（含回显，去除首尾空白）

        异常:
            TelnetClientError: 未连接、写入失败或等待特征超时时抛出
            ValueError: command为空时抛出
        """
        if not command or not command.strip():
            raise ValueError("待执行命令不能为空")
        if not self.is_connected:
            raise TelnetClientError("Telnet未连接，请先调用connect()", host=self.host)

        try:
            self._conn.write(command.encode("utf-8") + b"\n")
            logger.debug(f"Telnet命令已发送 >>> [{self.host}] {command!r}")
        except OSError as exc:
            logger.error(f"Telnet命令写入失败 | {self.host} | {exc}")
            raise TelnetClientError(f"命令写入失败: {exc}", host=self.host) from exc

        if expect is not None:
            # 等待特征字符串出现，超时即失败
            deadline = time.monotonic() + (timeout or self.timeout)
            expect_bytes = expect.encode("utf-8")
            buffer = bytearray()
            while time.monotonic() < deadline:
                try:
                    chunk = self._conn.read_very_eager()
                except EOFError as exc:
                    raise TelnetClientError(
                        f"读取输出失败: 连接已被对端关闭", host=self.host
                    ) from exc
                if chunk:
                    buffer.extend(chunk)
                    if expect_bytes in buffer:
                        output = buffer.decode("utf-8", errors="replace").strip()
                        logger.debug(f"Telnet输出已接收 <<< [{self.host}] {output!r}")
                        return output
                time.sleep(0.1)
            output = buffer.decode("utf-8", errors="replace")
            logger.error(
                f"Telnet等待特征超时 | {self.host} | 特征: {expect!r} | 已收到: {output!r}"
            )
            raise TelnetClientError(
                f"等待特征字符串 {expect!r} 超时，已收到内容: {output!r}", host=self.host
            )

        # 无特征字符串模式: 固定等待后收割当前全部输出
        time.sleep(wait_time)
        try:
            output = self._conn.read_very_eager().decode("utf-8", errors="replace").strip()
        except EOFError as exc:
            raise TelnetClientError("读取输出失败: 连接已被对端关闭", host=self.host) from exc
        logger.debug(f"Telnet输出已接收 <<< [{self.host}] {output!r}")
        return output

    # ------------------------------------------------------------------
    # 上下文管理器协议
    # ------------------------------------------------------------------
    def __enter__(self) -> "TelnetClient":
        """
        进入上下文管理器，自动建立连接

        返回:
            TelnetClient: 当前客户端实例

        异常:
            TelnetClientError: 连接失败时抛出（透传connect()异常）
        """
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        退出上下文管理器，自动断开连接

        参数:
            exc_type: 异常类型（无异常时为None）
            exc_val: 异常值
            exc_tb: 异常堆栈

        返回:
            无（不吞异常，原样透传）
        """
        self.close()
