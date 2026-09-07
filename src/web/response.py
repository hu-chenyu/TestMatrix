"""
统一API响应封装模块

所有Web API接口的响应必须经过本模块封装，禁止在视图中直接
return jsonify(...)，保证全平台响应格式统一为:
    {"code": int, "message": str, "data": any}

设计说明:
    - 各函数返回(payload字典, HTTP状态码)元组，作为Flask视图返回值时
      由Flask自动序列化为JSON响应（与jsonify走同一序列化通道，
      JSON_AS_ASCII等应用配置对二者行为一致）
    - code同时作为业务码与HTTP状态码，客户端可通过响应体code字段
      或HTTP状态码统一判断处理结果
    - 函数返回纯字典元组而非Response对象，便于在应用上下文之外
      单元测试（直接断言字典内容）

使用示例:
    from src.web.response import success, error

    @bp.route("/cases")
    def list_cases():
        return success(data=[{"case_id": "TM-API-0001"}])

    @bp.route("/cases", methods=["POST"])
    def create_case():
        return created(data={"case_id": "TM-API-0002"})
"""

from typing import Any


def success(
    data: Any = None, message: str = "ok", code: int = 200
) -> tuple[dict[str, Any], int]:
    """
    成功响应封装

    使用场景: 查询、更新等常规成功请求（HTTP 200），
    所有查询类接口的默认返回方式。

    参数:
        data (Any): 响应数据，可为None/list/dict，默认None
        message (str): 成功描述信息，默认"ok"
        code (int): HTTP状态码，默认200

    返回:
        tuple[dict[str, Any], int]: (统一响应体, HTTP状态码)
    """
    payload: dict[str, Any] = {"code": code, "message": message, "data": data}
    return payload, code


def error(
    message: str, code: int = 400, data: Any = None
) -> tuple[dict[str, Any], int]:
    """
    失败响应封装

    使用场景: 业务失败、参数校验失败、权限不足、资源不存在等
    （常用code: 400/401/403/404/409/500/503）。

    参数:
        message (str): 失败描述信息（必填，面向客户端可读）
        code (int): HTTP状态码，默认400
        data (Any): 可选失败详情（如校验失败的字段列表），默认None

    返回:
        tuple[dict[str, Any], int]: (统一响应体, HTTP状态码)
    """
    payload: dict[str, Any] = {"code": code, "message": message, "data": data}
    return payload, code


def created(
    data: Any = None, message: str = "created"
) -> tuple[dict[str, Any], int]:
    """
    资源创建成功响应封装

    使用场景: POST创建资源成功（HTTP 201），
    RESTful语义下新建资源应返回201而非200。

    参数:
        data (Any): 新建资源数据（通常含资源ID），默认None
        message (str): 成功描述信息，默认"created"

    返回:
        tuple[dict[str, Any], int]: (统一响应体, 201)
    """
    payload: dict[str, Any] = {"code": 201, "message": message, "data": data}
    return payload, 201


def no_content() -> tuple[str, int]:
    """
    无内容响应封装

    使用场景: DELETE删除成功（HTTP 204）。
    按HTTP语义，204响应不携带响应体，故返回空字符串而非JSON。

    参数:
        无

    返回:
        tuple[str, int]: ("", 204)
    """
    return "", 204
