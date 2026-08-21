"""
HTTP请求统一封装模块

功能:
    - 基于requests.Session的连接池复用，提升批量用例执行性能
    - 内置超时控制与网络级自动重试（连接失败/5xx状态码触发）
    - 统一异常捕获: 网络异常包装为HttpClientError，附带请求上下文
    - 全链路日志: 请求与响应自动脱敏记录（Authorization/Token/密码字段打码）
    - 统一入口request()方法支撑get/post/put/delete/patch快捷方法

使用示例:
    from src.common.http_client import HttpClient

    client = HttpClient(base_url="https://httpbin.org", timeout=10)
    resp = client.get("/get", params={"key": "value"})
    resp = client.post("/post", json={"name": "TestMatrix"})
"""

import time
from typing import Any, Optional, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.common.logger import LogManager

logger = LogManager.get_logger()

# 请求与响应体在日志中的最大打印长度（字符），超出部分截断，防止大报文刷爆日志
MAX_LOG_BODY_LENGTH = 2048
# 需要脱敏的请求头字段（小写匹配）
SENSITIVE_HEADERS = ("authorization", "token", "cookie", "set-cookie", "api-key")
# 请求体中需要脱敏的字段名（小写匹配）
SENSITIVE_BODY_FIELDS = ("password", "passwd", "secret", "token", "access_key")


class HttpClientError(Exception):
    """
    HTTP客户端统一异常类

    封装网络层异常（超时/连接失败/DNS解析失败等），
    携带请求上下文信息，便于用例失败时快速定位问题。
    """

    def __init__(self, message: str, request_info: Optional[dict] = None):
        """
        初始化异常

        参数:
            message (str): 异常描述信息
            request_info (dict | None): 请求上下文（method/url/kwargs），用于日志排查

        返回:
            无
        """
        self.request_info = request_info or {}
        super().__init__(message)


class HttpClient:
    """
    HTTP请求客户端

    基于requests.Session封装，提供统一的请求入口、
    超时控制、自动重试、日志记录与异常包装能力。
    Session级实例可跨用例复用（配合scope="session"的fixture）。

    属性:
        base_url (str): 被测服务基础地址
        timeout (int): 统一超时时间（秒）
        session (requests.Session): 底层HTTP会话，维护连接池
    """

    def __init__(
        self,
        base_url: str,
        timeout: int = 10,
        max_retries: int = 2,
        verify_ssl: bool = True,
    ):
        """
        初始化HTTP客户端

        参数:
            base_url (str): 被测服务基础地址，如 https://httpbin.org
            timeout (int): 单次请求超时时间（秒），默认10秒
            max_retries (int): 网络失败自动重试次数，默认2次
            verify_ssl (bool): 是否校验SSL证书，自签名环境可关闭，默认True

        返回:
            无

        异常:
            ValueError: base_url为空或格式非法（非http/https开头）时抛出
        """
        # 入参校验: base_url必须为合法的http/https地址
        if not base_url or not str(base_url).strip().lower().startswith(("http://", "https://")):
            raise ValueError(f"非法的base_url: {base_url}，必须以http://或https://开头")

        self.base_url = str(base_url).strip().rstrip("/")
        self.timeout = timeout
        self.verify_ssl = verify_ssl

        # 构建带重试策略的Session
        self.session = requests.Session()
        self.session.verify = verify_ssl
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=0.5,
            # 仅对5xx服务端错误与连接错误重试，4xx客户端错误不重试
            status_forcelist=(500, 502, 503, 504),
            # None表示所有HTTP方法均允许重试（默认POST等不安全方法不重试）
            allowed_methods=None,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=10, pool_maxsize=20)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        logger.info(
            f"HttpClient初始化完成 | base_url: {self.base_url} | "
            f"timeout: {timeout}s | retries: {max_retries}"
        )

    # ------------------------------------------------------------------
    # 快捷请求方法
    # ------------------------------------------------------------------
    def get(self, path: str, params: Optional[dict] = None, **kwargs) -> requests.Response:
        """
        发送GET请求

        参数:
            path (str): 接口路径，如 /get 或完整URL
            params (dict | None): URL查询参数
            **kwargs: 透传给requests的额外参数（headers/cookies等）

        返回:
            requests.Response: 服务端响应对象

        异常:
            HttpClientError: 网络异常（超时/连接失败等）时抛出
        """
        return self.request("GET", path, params=params, **kwargs)

    def post(
        self,
        path: str,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        **kwargs,
    ) -> requests.Response:
        """
        发送POST请求

        参数:
            path (str): 接口路径
            json (Any | None): JSON请求体（自动序列化并设置Content-Type）
            data (Any | None): 表单请求体
            **kwargs: 透传给requests的额外参数

        返回:
            requests.Response: 服务端响应对象

        异常:
            HttpClientError: 网络异常时抛出
        """
        return self.request("POST", path, json=json, data=data, **kwargs)

    def put(
        self,
        path: str,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        **kwargs,
    ) -> requests.Response:
        """
        发送PUT请求

        参数:
            path (str): 接口路径
            json (Any | None): JSON请求体
            data (Any | None): 表单请求体
            **kwargs: 透传给requests的额外参数

        返回:
            requests.Response: 服务端响应对象

        异常:
            HttpClientError: 网络异常时抛出
        """
        return self.request("PUT", path, json=json, data=data, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        """
        发送DELETE请求

        参数:
            path (str): 接口路径
            **kwargs: 透传给requests的额外参数

        返回:
            requests.Response: 服务端响应对象

        异常:
            HttpClientError: 网络异常时抛出
        """
        return self.request("DELETE", path, **kwargs)

    def patch(
        self,
        path: str,
        json: Optional[Any] = None,
        data: Optional[Any] = None,
        **kwargs,
    ) -> requests.Response:
        """
        发送PATCH请求

        参数:
            path (str): 接口路径
            json (Any | None): JSON请求体
            data (Any | None): 表单请求体
            **kwargs: 透传给requests的额外参数

        返回:
            requests.Response: 服务端响应对象

        异常:
            HttpClientError: 网络异常时抛出
        """
        return self.request("PATCH", path, json=json, data=data, **kwargs)

    # ------------------------------------------------------------------
    # 统一请求入口
    # ------------------------------------------------------------------
    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        """
        统一请求入口（所有快捷方法的最终汇聚点）

        参数:
            method (str): HTTP方法大写（GET/POST/PUT/DELETE/PATCH）
            path (str): 接口路径；以http开头时视为完整URL直接请求
            **kwargs: 透传给requests.Session.request的参数

        返回:
            requests.Response: 服务端响应对象（网络层成功但业务4xx/5xx同样返回）

        异常:
            ValueError: method为空时抛出
            HttpClientError: 网络异常（超时/连接失败/DNS错误）时抛出，
                             异常信息附带请求方法、URL与原始异常描述
        """
        if not method or not method.strip():
            raise ValueError(f"非法的HTTP方法: {method}")

        method = method.strip().upper()
        url = self._build_url(path)
        # 未显式传超时时使用客户端统一超时配置
        kwargs.setdefault("timeout", self.timeout)

        # 请求前置日志（脱敏）
        logger.debug(
            f"HTTP请求 >>> {method} {url} | "
            f"params: {self._mask_data(kwargs.get('params'))} | "
            f"headers: {self._mask_headers(kwargs.get('headers'))} | "
            f"body: {self._truncate(self._mask_data(kwargs.get('json') or kwargs.get('data')))}"
        )

        start_time = time.perf_counter()
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.exceptions.Timeout as exc:
            logger.error(f"HTTP请求超时 | {method} {url} | 超时配置: {kwargs.get('timeout')}s")
            raise HttpClientError(
                f"请求超时: {method} {url}（{kwargs.get('timeout')}s）",
                request_info={"method": method, "url": url, "type": "timeout"},
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            logger.error(f"HTTP连接失败 | {method} {url} | {exc}")
            raise HttpClientError(
                f"连接失败: {method} {url}，请检查网络与服务可达性",
                request_info={"method": method, "url": url, "type": "connection_error"},
            ) from exc
        except requests.exceptions.RequestException as exc:
            logger.error(f"HTTP请求异常 | {method} {url} | {exc}")
            raise HttpClientError(
                f"请求异常: {method} {url} - {exc}",
                request_info={"method": method, "url": url, "type": "request_error"},
            ) from exc

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # 响应后置日志（脱敏+截断）
        logger.debug(
            f"HTTP响应 <<< {method} {url} | "
            f"状态码: {response.status_code} | 耗时: {elapsed_ms:.1f}ms | "
            f"body: {self._truncate(self._safe_body(response))}"
        )

        # 状态码异常时提升日志级别为警告（不中断用例，由断言层决定成败）
        if response.status_code >= 400:
            logger.warning(
                f"HTTP响应异常状态 | {method} {url} | 状态码: {response.status_code}"
            )

        return response

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    def _build_url(self, path: str) -> str:
        """
        拼接完整请求URL

        参数:
            path (str): 接口路径；以http(s)开头视为完整URL，否则与base_url拼接

        返回:
            str: 完整请求URL

        异常:
            无
        """
        path = str(path).strip()
        if path.lower().startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _mask_headers(headers: Optional[dict]) -> Union[dict, str]:
        """
        请求头脱敏（敏感字段的值替换为***）

        参数:
            headers (dict | None): 原始请求头

        返回:
            dict | str: 脱敏后的请求头副本；入参为None时返回'-'
        """
        if headers is None:
            return "-"
        masked = {}
        for key, value in headers.items():
            masked[key] = "***" if str(key).lower() in SENSITIVE_HEADERS else value
        return masked

    @staticmethod
    def _mask_data(data: Any) -> Any:
        """
        请求数据脱敏（password/token等字段值替换为***）

        参数:
            data (Any): 原始数据（dict/list/其他类型）

        返回:
            Any: 脱敏后的数据副本；入参为None时返回'-'

        异常:
            无（非dict/list类型原样返回，不递归深度处理）
        """
        if data is None:
            return "-"
        if isinstance(data, dict):
            return {
                key: ("***" if str(key).lower() in SENSITIVE_BODY_FIELDS else value)
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [HttpClient._mask_data(item) for item in data]
        return data

    @staticmethod
    def _truncate(text: Any, limit: int = MAX_LOG_BODY_LENGTH) -> str:
        """
        截断超长文本，防止大报文刷爆日志

        参数:
            text (Any): 原始文本（非字符串自动str转换）
            limit (int): 最大保留长度（字符），默认2048

        返回:
            str: 截断后的文本，超长时尾部追加...[截断]标记
        """
        text = str(text)
        if len(text) <= limit:
            return text
        return f"{text[:limit]}...[已截断,原始长度{len(text)}字符]"

    @staticmethod
    def _safe_body(response: requests.Response) -> str:
        """
        安全读取响应体（避免二进制/解压异常导致日志逻辑崩溃）

        参数:
            response (requests.Response): 响应对象

        返回:
            str: 响应体文本；读取失败时返回错误占位描述
        """
        try:
            return response.text
        except Exception as exc:  # noqa: BLE001 二进制/编码异常统一兜底
            return f"<响应体读取失败: {exc}>"

    def close(self) -> None:
        """
        关闭底层HTTP会话，释放连接池资源

        参数:
            无

        返回:
            无

        异常:
            无
        """
        self.session.close()
        logger.info("HttpClient会话已关闭")

    def __enter__(self) -> "HttpClient":
        """
        进入上下文管理器，返回客户端自身

        返回:
            HttpClient: 当前客户端实例
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        退出上下文管理器，自动关闭会话

        参数:
            exc_type: 异常类型（无异常时为None）
            exc_val: 异常值
            exc_tb: 异常堆栈

        返回:
            无（不吞异常，原样透传）
        """
        self.close()
