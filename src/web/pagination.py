"""
Web层查询参数解析公共模块

职责:
    收敛"从查询串取整型参数并做缺省/非法兜底"这一在多个路由蓝图
    中被重复实现的逻辑，统一口径与错误文案。

为什么要提取（v1 审查遗留的重复代码项）:
    `cases.py` / `executions.py` / `reports.py` 三个路由文件各自实现了
    一份 `_parse_int_param`，函数体逐行相同，差异仅在 docstring 的参数
    说明与抛错文案。任何一处改了"空白串是否算缺省""bool 是否算整数"
    这类口径，另外两处都会静默留在旧行为上——分页参数是全平台列表
    接口的公共入口，口径漂移会表现为"用例列表能翻页、执行列表翻不了"
    这种极难定位的不一致。

    提取后各路由以 `from ... import parse_int_param as _parse_int_param`
    形式保留本地名字，既有调用点与既有测试（直接引用各路由模块的
    `_parse_int_param`）均无需改动。
"""

from flask import request

from src.core.case_manager import MAX_PAGE
from src.web.exceptions import ValidationError


def parse_int_param(
    name: str, default: int, param_hint: str, max_value: int | None = None
) -> int:
    """
    解析整型查询参数

    参数缺省或空白串时返回默认值；传入非合法整数时抛 ValidationError。

    分层约定（Day44 P2-07 修正）：本函数**只负责"取值与转换"**，
    不做业务范围校验——缺省/空白/负数/零一律原样返回，由调用点决定
    是否拦截。这样三处路由可以各自持有不同的范围口径（cases 与
    executions 收 page 上界，notifications 有自己的分页语义），
    公共解析层的行为不会因某一路由的需求变化而全体漂移。
    调用方需要上界时显式传 max_value。

    参数:
        name (str): 查询参数名（page / page_size / limit）
        default (int): 参数缺省时的默认值
        param_hint (str): 错误文案中的参数名描述（如"page和page_size"），
                          同一函数在不同路由下服务不同参数组，文案随之不同
        max_value (int | None): 可选上界；给定且解析值超过时抛
                                ValidationError（400）。None 表示不设上界

    返回:
        int: 解析后的整数值

    异常:
        ValidationError: 参数值不是合法整数、或超过 max_value 时抛出（400）
    """
    raw_value = request.args.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        # 参数校验失败是干净的业务错误，与底层 TypeError/ValueError
        # 无因果链，显式抑制（__suppress_context__ 是 from None 的
        # 唯一可观测特征，既有测试据此断言）
        raise ValidationError(f"{param_hint}必须为正整数") from None
    # 上界只在校验与业务语义匹配的维度上生效（page 的上界由 MAX_PAGE 定，
    # page_size/limit 各自有独立业务上界，由调用点决定是否传 max_value）
    if max_value is not None and parsed > max_value:
        raise ValidationError(f"{param_hint}不能超过{max_value}")
    return parsed


def parse_page_param(name: str, default: int, param_hint: str) -> int:
    """解析页码并收口到 [1, MAX_PAGE]（Day44 P2-07 的路由侧入口）。

    page 无上界时，SQL 的 OFFSET 是 (page-1)*page_size，一个构造请求即可
    让数据库扫描并丢弃近乎整表的数据；SQLite 下 offset 超出 int64 还会
    抛 OverflowError，被包装成 SQLAlchemyError 后冒泡成 500 —— 一个本该
    400 的参数错误变成了服务端故障。

    参数:
        name (str): 查询参数名，固定为 "page"
        default (int): 参数缺省时的默认值
        param_hint (str): 错误文案中的参数名描述

    返回:
        int: 落在 [1, MAX_PAGE] 内的页码

    异常:
        ValidationError: 非整数 / 小于 1 / 大于 MAX_PAGE 时抛出（400）
    """
    from src.web.exceptions import ValidationError as _ValidationError

    page = parse_int_param(name, default, param_hint)
    if not (1 <= page <= MAX_PAGE):
        raise _ValidationError(f"{param_hint}必须在 1 与 {MAX_PAGE} 之间")
    return page
