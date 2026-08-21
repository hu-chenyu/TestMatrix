"""
通用增强断言库

功能:
    - 响应类断言: 状态码、响应耗时、响应头字段校验
    - JSON断言: 点号路径取值（支持数组索引）、字段相等、子集包含、字段类型校验
    - 通用断言: 相等/不相等/包含/为空/非空/大于/小于等逻辑断言
    - 全部断言失败时自动记录ERROR日志并抛出携带上下文的AssertionError
    - 断言成功记录DEBUG日志，形成完整断言审计链路

使用示例:
    from src.common.assertion import assert_status_code, assert_json_value

    resp = http_client.get("/get")
    assert_status_code(resp, 200)                 # 状态码断言
    assert_json_value(resp, "args.key", "value")  # JSON路径断言
"""

import json
from typing import Any, Optional, Union

import requests

from src.common.logger import LogManager

logger = LogManager.get_logger()


def _extract_json(response: requests.Response) -> Any:
    """
    从响应对象安全提取JSON数据（断言模块内部工具）

    参数:
        response (requests.Response): HTTP响应对象

    返回:
        Any: 解析后的JSON数据

    异常:
        AssertionError: 响应体不是合法JSON时抛出（附原始内容片段）
    """
    try:
        return response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise AssertionError(
            f"响应体不是合法JSON，无法执行JSON断言 | 错误: {exc} | "
            f"原始内容: {response.text[:200]}"
        ) from exc


def _parse_json_path(data: Any, path: str) -> Any:
    """
    按点号路径从嵌套结构中取值（断言模块内部工具）

    支持格式:
        - 纯字段名: data.user.name
        - 数组索引: data.items[0].id
        - 混合路径: data.list[2].info.type

    参数:
        data (Any): JSON数据（dict/list嵌套结构）
        path (str): 点号分隔的字段路径

    返回:
        Any: 路径对应的值

    异常:
        KeyError: 字段不存在时抛出（附完整路径）
        IndexError: 数组索引越界时抛出
        TypeError: 路径中间节点非dict/list时抛出
    """
    current = data
    # 将 items[0].id 拆解为 ['items', 0, 'id']
    segments = []
    for part in path.split("."):
        if "[" in part:
            field, _, index_part = part.partition("[")
            if field:
                segments.append(field)
            index = int(index_part.rstrip("]"))
            segments.append(index)
        else:
            segments.append(part)

    for segment in segments:
        if isinstance(segment, int):
            if not isinstance(current, list):
                raise TypeError(f"路径'{path}'中索引{segment}期望数组，实际类型: {type(current).__name__}")
            if segment >= len(current):
                raise IndexError(f"路径'{path}'中索引{segment}越界，数组长度: {len(current)}")
            current = current[segment]
        else:
            if not isinstance(current, dict):
                raise TypeError(f"路径'{path}'中字段'{segment}'期望字典，实际类型: {type(current).__name__}")
            if segment not in current:
                raise KeyError(f"路径'{path}'中字段'{segment}'不存在，可用字段: {list(current.keys())}")
            current = current[segment]
    return current


# ----------------------------------------------------------------------
# 响应类断言
# ----------------------------------------------------------------------
def assert_status_code(
    response: requests.Response,
    expected: Union[int, list, tuple],
) -> None:
    """
    断言HTTP响应状态码

    参数:
        response (requests.Response): HTTP响应对象
        expected (int | list | tuple): 期望状态码，单值或合法值列表（如200或[200, 201]）

    返回:
        无

    异常:
        AssertionError: 实际状态码不在期望范围内时抛出，信息包含实际/期望值
    """
    expected_list = [expected] if isinstance(expected, int) else list(expected)
    if response.status_code not in expected_list:
        logger.error(
            f"断言失败[状态码] | 实际: {response.status_code} | 期望: {expected_list} | "
            f"URL: {response.url}"
        )
        raise AssertionError(
            f"状态码断言失败: 实际 {response.status_code}，期望 {expected_list}，"
            f"URL: {response.url}，响应片段: {response.text[:200]}"
        )
    logger.debug(f"断言通过[状态码] | 实际: {response.status_code}")


def assert_response_time(
    response: requests.Response,
    max_elapsed_ms: float,
) -> None:
    """
    断言响应耗时不超过阈值（性能基线校验）

    参数:
        response (requests.Response): HTTP响应对象
        max_elapsed_ms (float): 最大允许耗时（毫秒）

    返回:
        无

    异常:
        AssertionError: 实际耗时超阈值时抛出
    """
    elapsed_ms = response.elapsed.total_seconds() * 1000
    if elapsed_ms > max_elapsed_ms:
        logger.error(f"断言失败[响应耗时] | 实际: {elapsed_ms:.1f}ms | 阈值: {max_elapsed_ms}ms")
        raise AssertionError(
            f"响应耗时断言失败: 实际 {elapsed_ms:.1f}ms 超过阈值 {max_elapsed_ms}ms，"
            f"URL: {response.url}"
        )
    logger.debug(f"断言通过[响应耗时] | 实际: {elapsed_ms:.1f}ms")


def assert_header(
    response: requests.Response,
    header_name: str,
    expected: Optional[str] = None,
) -> None:
    """
    断言响应头存在且（可选）值匹配

    参数:
        response (requests.Response): HTTP响应对象
        header_name (str): 响应头字段名（大小写不敏感）
        expected (str | None): 期望值；None时仅校验字段存在

    返回:
        无

    异常:
        AssertionError: 响应头不存在或值不匹配时抛出
    """
    actual = response.headers.get(header_name)
    if actual is None:
        logger.error(f"断言失败[响应头] | 响应头'{header_name}'不存在")
        raise AssertionError(f"响应头断言失败: '{header_name}'不存在，实际响应头: {dict(response.headers)}")
    if expected is not None and actual != expected:
        logger.error(f"断言失败[响应头] | '{header_name}'实际: {actual} | 期望: {expected}")
        raise AssertionError(f"响应头断言失败: '{header_name}'实际值 '{actual}'，期望 '{expected}'")
    logger.debug(f"断言通过[响应头] | {header_name}: {actual}")


# ----------------------------------------------------------------------
# JSON数据断言
# ----------------------------------------------------------------------
def assert_json_value(
    response: requests.Response,
    json_path: str,
    expected: Any,
) -> None:
    """
    断言JSON响应中指定路径的值与期望相等

    参数:
        response (requests.Response): HTTP响应对象（响应体须为合法JSON）
        json_path (str): 点号路径，如 args.key / data.items[0].id
        expected (Any): 期望值（==比较）

    返回:
        无

    异常:
        AssertionError: 路径不存在、类型不匹配或值不相等时抛出，
                        信息包含实际值、期望值与完整路径
    """
    data = _extract_json(response)
    try:
        actual = _parse_json_path(data, json_path)
    except (KeyError, IndexError, TypeError) as exc:
        logger.error(f"断言失败[JSON取值] | 路径'{json_path}'解析失败: {exc}")
        raise AssertionError(f"JSON路径断言失败: 路径'{json_path}'解析失败，原因: {exc}") from exc

    if actual != expected:
        logger.error(f"断言失败[JSON值] | 路径'{json_path}'实际: {actual!r} | 期望: {expected!r}")
        raise AssertionError(
            f"JSON值断言失败: 路径'{json_path}'实际值 {actual!r}，期望值 {expected!r}"
        )
    logger.debug(f"断言通过[JSON值] | {json_path} = {actual!r}")


def assert_json_contains(
    response: requests.Response,
    expected_subset: dict,
) -> None:
    """
    断言期望字典是JSON响应的子集（只校验期望中列出的键，适合部分字段校验）

    参数:
        response (requests.Response): HTTP响应对象
        expected_subset (dict): 期望的字段子集，如 {"code": 0, "msg": "success"}

    返回:
        无

    异常:
        AssertionError: 期望字段缺失或值不匹配时抛出，逐字段列出差异
    """
    data = _extract_json(response)
    if not isinstance(data, dict):
        raise AssertionError(f"子集断言失败: 响应JSON顶层不是对象，实际类型: {type(data).__name__}")

    diff = []
    for key, expected_value in expected_subset.items():
        if key not in data:
            diff.append(f"字段'{key}'缺失")
        elif data[key] != expected_value:
            diff.append(f"字段'{key}'实际: {data[key]!r}，期望: {expected_value!r}")

    if diff:
        logger.error(f"断言失败[JSON子集] | 差异: {'; '.join(diff)}")
        raise AssertionError(f"JSON子集断言失败: {'; '.join(diff)}")
    logger.debug(f"断言通过[JSON子集] | 校验字段: {list(expected_subset.keys())}")


def assert_json_path_exists(
    response: requests.Response,
    json_path: str,
) -> None:
    """
    断言JSON响应中指定路径存在（仅存在性校验，不比较值）

    参数:
        response (requests.Response): HTTP响应对象
        json_path (str): 点号路径

    返回:
        无

    异常:
        AssertionError: 路径不存在时抛出
    """
    data = _extract_json(response)
    try:
        _parse_json_path(data, json_path)
    except (KeyError, IndexError, TypeError) as exc:
        logger.error(f"断言失败[JSON路径存在性] | 路径'{json_path}': {exc}")
        raise AssertionError(f"JSON路径存在性断言失败: 路径'{json_path}'，原因: {exc}") from exc
    logger.debug(f"断言通过[JSON路径存在性] | {json_path}")


# ----------------------------------------------------------------------
# 通用逻辑断言
# ----------------------------------------------------------------------
def assert_equal(actual: Any, expected: Any, message: str = "") -> None:
    """
    断言两值相等

    参数:
        actual (Any): 实际值
        expected (Any): 期望值
        message (str): 失败时的附加说明信息

    返回:
        无

    异常:
        AssertionError: 两值不相等时抛出
    """
    if actual != expected:
        extra = f" | 附加信息: {message}" if message else ""
        logger.error(f"断言失败[相等] | 实际: {actual!r} | 期望: {expected!r}{extra}")
        raise AssertionError(f"相等断言失败: 实际 {actual!r}，期望 {expected!r}{extra}")
    logger.debug("断言通过[相等]")


def assert_not_equal(actual: Any, unexpected: Any, message: str = "") -> None:
    """
    断言两值不相等

    参数:
        actual (Any): 实际值
        unexpected (Any): 不期望的值
        message (str): 失败时的附加说明信息

    返回:
        无

    异常:
        AssertionError: 两值相等时抛出
    """
    if actual == unexpected:
        extra = f" | 附加信息: {message}" if message else ""
        logger.error(f"断言失败[不相等] | 两值均为: {actual!r}{extra}")
        raise AssertionError(f"不相等断言失败: 实际值与不期望值相同，均为 {actual!r}{extra}")
    logger.debug("断言通过[不相等]")


def assert_contains(container: Any, member: Any, message: str = "") -> None:
    """
    断言包含关系（字符串包含子串 / 列表包含元素 / 字典包含键）

    参数:
        container (Any): 容器（str/list/dict/tuple）
        member (Any): 期望包含的成员
        message (str): 失败时的附加说明信息

    返回:
        无

    异常:
        AssertionError: 不包含时抛出（container不支持in操作时也抛出）
    """
    try:
        result = member in container
    except TypeError as exc:
        raise AssertionError(
            f"包含断言失败: 类型 {type(container).__name__} 不支持包含判断，值: {container!r}"
        ) from exc

    if not result:
        extra = f" | 附加信息: {message}" if message else ""
        logger.error(f"断言失败[包含] | {container!r} 不包含 {member!r}{extra}")
        raise AssertionError(f"包含断言失败: {container!r} 不包含 {member!r}{extra}")
    logger.debug(f"断言通过[包含] | {member!r}")


def assert_is_empty(value: Any, message: str = "") -> None:
    """
    断言值为空（None/空字符串/空列表/空字典）

    参数:
        value (Any): 待校验值
        message (str): 失败时的附加说明信息

    返回:
        无

    异常:
        AssertionError: 值非空时抛出
    """
    if value:
        extra = f" | 附加信息: {message}" if message else ""
        logger.error(f"断言失败[为空] | 实际值非空: {value!r}{extra}")
        raise AssertionError(f"为空断言失败: 实际值 {value!r} 非空{extra}")
    logger.debug("断言通过[为空]")


def assert_is_not_empty(value: Any, message: str = "") -> None:
    """
    断言值非空

    参数:
        value (Any): 待校验值
        message (str): 失败时的附加说明信息

    返回:
        无

    异常:
        AssertionError: 值为空时抛出
    """
    if not value:
        extra = f" | 附加信息: {message}" if message else ""
        logger.error(f"断言失败[非空] | 实际值为空: {value!r}{extra}")
        raise AssertionError(f"非空断言失败: 实际值为空{extra}")
    logger.debug("断言通过[非空]")


def assert_greater(actual: Union[int, float], threshold: Union[int, float], message: str = "") -> None:
    """
    断言实际值严格大于阈值

    参数:
        actual (int | float): 实际值
        threshold (int | float): 阈值
        message (str): 失败时的附加说明信息

    返回:
        无

    异常:
        AssertionError: 实际值小于等于阈值时抛出
    """
    if actual <= threshold:
        extra = f" | 附加信息: {message}" if message else ""
        logger.error(f"断言失败[大于] | 实际: {actual} | 阈值: {threshold}{extra}")
        raise AssertionError(f"大于断言失败: 实际 {actual} 未大于阈值 {threshold}{extra}")
    logger.debug(f"断言通过[大于] | {actual} > {threshold}")


def assert_less(actual: Union[int, float], threshold: Union[int, float], message: str = "") -> None:
    """
    断言实际值严格小于阈值

    参数:
        actual (int | float): 实际值
        threshold (int | float): 阈值
        message (str): 失败时的附加说明信息

    返回:
        无

    异常:
        AssertionError: 实际值大于等于阈值时抛出
    """
    if actual >= threshold:
        extra = f" | 附加信息: {message}" if message else ""
        logger.error(f"断言失败[小于] | 实际: {actual} | 阈值: {threshold}{extra}")
        raise AssertionError(f"小于断言失败: 实际 {actual} 未小于阈值 {threshold}{extra}")
    logger.debug(f"断言通过[小于] | {actual} < {threshold}")
