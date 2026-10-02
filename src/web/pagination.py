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

from src.web.exceptions import ValidationError


def parse_int_param(name: str, default: int, param_hint: str) -> int:
    """
    解析整型查询参数

    参数缺省或空白串时返回默认值；传入非合法整数时抛 ValidationError。

    参数:
        name (str): 查询参数名（page / page_size / limit）
        default (int): 参数缺省时的默认值
        param_hint (str): 错误文案中的参数名描述（如"page和page_size"），
                          同一函数在不同路由下服务不同参数组，文案随之不同

    返回:
        int: 解析后的整数值

    异常:
        ValidationError: 参数值不是合法整数时抛出（400）
    """
    raw_value = request.args.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        # 参数校验失败是干净的业务错误，与底层 TypeError/ValueError
        # 无因果链，显式抑制（__suppress_context__ 是 from None 的
        # 唯一可观测特征，既有测试据此断言）
        raise ValidationError(f"{param_hint}必须为正整数") from None
