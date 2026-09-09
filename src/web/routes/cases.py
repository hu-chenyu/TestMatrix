"""
用例管理API蓝图

功能（第三阶段Day19交付）:
    - GET /api/cases/  用例列表（分页 + module/priority/case_type/
      status/keyword六维筛选，全部可选、可组合使用）

功能（第三阶段Day20交付）:
    - POST   /api/cases/          创建用例（marshmallow入参校验，
      case_id重复返回409）
    - GET    /api/cases/<case_id> 用例详情（不存在返回404）
    - PUT    /api/cases/<case_id> 更新用例（只更新传入字段，
      case_id业务编号不可修改，空body返回400）
    - DELETE /api/cases/<case_id> 删除用例（物理删除，成功204无响应体）

入参校验说明:
    - 请求体经marshmallow Schema校验（必填/长度/枚举），
      校验失败抛ValidationError(400)，message含具体字段错误
    - case_id重复创建转ConflictError(409)；
      用例不存在转NotFoundError(404)
"""

from typing import Optional

import marshmallow
from flask import Blueprint, request
from marshmallow import EXCLUDE, Schema, fields, validate

from src.core.case_manager import MAX_PAGE_SIZE, CaseManager, CaseManagerError
from src.web.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from src.web.response import created, no_content, success

cases_bp = Blueprint("cases", __name__, url_prefix="/api/cases")

# 用例类型合法值
VALID_CASE_TYPES = ("api", "chip")

# 用例状态查询合法值（all为"查全部"语义，透传核心层时转为None）
VALID_CASE_STATUSES = ("active", "disabled", "all")

# 分页参数默认值
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20


# ===========================================================================
# marshmallow入参校验Schema（Day20，创建/更新用例请求体）
# ===========================================================================
class CaseCreateSchema(Schema):
    """
    创建用例入参校验Schema

    校验规则:
        - case_id/name必填且非空（case_id长度1-64，name长度1-200）
        - module/priority/case_type/status/description/creator可选，
          缺省值与数据库模型默认值一致（default/P2/api/active/""/admin）
        - priority只允许P0-P3，case_type只允许api/chip，
          status只允许active/disabled（枚举校验）

    说明:
        缺省值用load_default而非missing（marshmallow 3.13起missing
        已废弃，使用会触发RemovedInMarshmallow4Warning）
    """

    case_id = fields.String(
        required=True, validate=validate.Length(min=1, max=64)
    )
    name = fields.String(
        required=True, validate=validate.Length(min=1, max=200)
    )
    module = fields.String(
        load_default="default", validate=validate.Length(max=64)
    )
    priority = fields.String(
        load_default="P2", validate=validate.OneOf(["P0", "P1", "P2", "P3"])
    )
    case_type = fields.String(
        load_default="api", validate=validate.OneOf(["api", "chip"])
    )
    status = fields.String(
        load_default="active", validate=validate.OneOf(["active", "disabled"])
    )
    description = fields.String(load_default="")
    creator = fields.String(
        load_default="admin", validate=validate.Length(max=64)
    )


class CaseUpdateSchema(Schema):
    """
    更新用例入参校验Schema（所有字段可选）

    与CaseCreateSchema的差异:
        - 全部字段可选（partial更新，只校验传入字段）
        - 不含case_id字段（业务编号不可修改，由URL路径参数定位）
        - unknown=EXCLUDE: body中回传的case_id等未知字段静默忽略，
          兼容前端编辑表单回传完整对象的习惯

    校验规则（传入才校验）:
        - name长度1-200，module长度最大64，creator长度最大64
        - priority只允许P0-P3，case_type只允许api/chip，
          status只允许active/disabled
    """

    class Meta:
        """未知字段处理策略: 静默忽略（case_id回传不报错也不生效）"""

        unknown = EXCLUDE

    name = fields.String(validate=validate.Length(min=1, max=200))
    module = fields.String(validate=validate.Length(max=64))
    priority = fields.String(
        validate=validate.OneOf(["P0", "P1", "P2", "P3"])
    )
    case_type = fields.String(validate=validate.OneOf(["api", "chip"]))
    status = fields.String(validate=validate.OneOf(["active", "disabled"]))
    description = fields.String()
    creator = fields.String(validate=validate.Length(max=64))


def _parse_int_param(name: str, default: int) -> int:
    """
    解析整型查询参数（内部方法）

    参数缺省或空白串时返回默认值；传入非合法整数时抛ValidationError。

    参数:
        name (str): 查询参数名（page/page_size）
        default (int): 参数缺省时的默认值

    返回:
        int: 解析后的整数值

    异常:
        ValidationError: 参数值不是合法整数时抛出
    """
    raw_value = request.args.get(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        raise ValidationError("page和page_size必须为正整数")


def _parse_optional_str(name: str) -> Optional[str]:
    """
    解析可选字符串查询参数（内部方法）

    参数缺省或空白串视为未传入（返回None），其余返回strip后的值，
    避免"?keyword="这类空参数下发起无意义的全量过滤。

    参数:
        name (str): 查询参数名

    返回:
        str | None: 解析后的字符串值，未传入时为None

    异常:
        无
    """
    raw_value = request.args.get(name)
    if raw_value is None:
        return None
    stripped = raw_value.strip()
    return stripped if stripped else None


def _get_json_body() -> dict:
    """
    获取JSON请求体（内部方法）

    请求体缺失、非JSON格式或非JSON对象（如数组）时抛ValidationError；
    空对象{}为合法请求体（由后续Schema校验兜底必填字段）。

    参数:
        无

    返回:
        dict: 解析后的JSON对象请求体

    异常:
        ValidationError: 请求体缺失/非JSON格式/非JSON对象时抛出
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValidationError("请求体必须为JSON格式")
    return body


def _format_field_errors(messages: dict) -> str:
    """
    格式化marshmallow字段校验错误（内部方法）

    将形如{"priority": ["Must be one of: P0..."]}的错误字典
    拼接为"priority: Must be one of: P0..."的可读文本，
    保证错误message中包含具体字段名，便于客户端定位问题字段。

    参数:
        messages (dict): marshmallow ValidationError.messages错误字典

    返回:
        str: "字段名: 错误1; 错误2"格式文本，多字段以中文逗号分隔

    异常:
        无
    """
    parts: list[str] = []
    for field, errors in messages.items():
        if isinstance(errors, (list, tuple)):
            text = "; ".join(str(item) for item in errors)
        else:
            text = str(errors)
        parts.append(f"{field}: {text}")
    return "，".join(parts)


def _load_case_payload(
    schema: Schema, body: dict, partial: bool = False
) -> dict:
    """
    用marshmallow Schema校验并加载请求体（内部方法）

    校验失败时将marshmallow.ValidationError转换为项目统一
    ValidationError(400)，message含具体字段错误，detail携带
    结构化错误字典（字段名→错误列表）。

    参数:
        schema (Schema): marshmallow Schema实例
                         （CaseCreateSchema/CaseUpdateSchema）
        body (dict): JSON请求体字典
        partial (bool): 是否partial校验（更新场景全字段可选），默认False

    返回:
        dict: 校验通过并应用缺省值后的字段字典

    异常:
        ValidationError: 入参校验失败时抛出（已转换项目统一格式）
    """
    try:
        return schema.load(body, partial=partial)
    except marshmallow.ValidationError as exc:
        raise ValidationError(
            f"用例入参校验失败: {_format_field_errors(exc.messages)}",
            detail=exc.messages,
        ) from exc


@cases_bp.route("/")
def list_cases():
    """
    用例列表接口

    支持六维筛选（全部可选、可组合）:
        page        页码，默认1，必须>=1
        page_size   每页条数，默认20，1到100
        module      模块筛选（精确匹配）
        priority    优先级筛选（不区分大小写，统一大写匹配）
        case_type   类型筛选（api/chip）
        status      状态筛选（active/disabled/all），默认active
        keyword     关键字模糊搜索（case_id/name/description）

    参数:
        无（从request.args解析查询参数）

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为分页结构:
            {"items": list[dict], "total": int, "page": int,
             "page_size": int, "total_pages": int}

    异常:
        ValidationError: 分页参数非法 / case_type或status取值非法时
                         抛出（全局异常处理器统一转400响应）
    """
    # 1. 分页参数解析与范围校验
    page = _parse_int_param("page", DEFAULT_PAGE)
    page_size = _parse_int_param("page_size", DEFAULT_PAGE_SIZE)
    if page < 1:
        raise ValidationError("page必须为大于等于1的整数")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValidationError(f"page_size必须在1到{MAX_PAGE_SIZE}之间")

    # 2. 筛选参数解析与取值校验
    module = _parse_optional_str("module")
    priority = _parse_optional_str("priority")
    keyword = _parse_optional_str("keyword")

    case_type = _parse_optional_str("case_type")
    if case_type is not None and case_type not in VALID_CASE_TYPES:
        raise ValidationError("case_type只能为api或chip")

    status = _parse_optional_str("status") or "active"
    if status not in VALID_CASE_STATUSES:
        raise ValidationError("status只能为active、disabled或all")

    # 3. 调用核心层分页查询（status=all转None查全部状态）
    result = CaseManager.list_cases_paged(
        module=module,
        priority=priority,
        case_type=case_type,
        status=None if status == "all" else status,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )
    return success(data=result)


@cases_bp.route("/", methods=["POST"])
def create_case():
    """
    创建用例接口

    请求体（JSON，经CaseCreateSchema校验）:
        case_id     必填，长度1-64，业务编号唯一
        name        必填，长度1-200
        module      可选，默认"default"
        priority    可选，P0-P3，默认"P2"
        case_type   可选，api/chip，默认"api"
        status      可选，active/disabled，默认"active"
        description 可选，默认""
        creator     可选，长度最大64，默认"admin"

    参数:
        无（从request.get_json解析请求体）

    返回:
        tuple[dict, int]: (统一响应体, 201)，data为新建用例完整字段

    异常:
        ValidationError: 请求体非JSON / 入参校验失败时抛出（400）
        ConflictError: case_id已存在时抛出（409）
    """
    # 1. 请求体解析与Schema校验
    body = _get_json_body()
    data = _load_case_payload(CaseCreateSchema(), body)

    # 2. 调用核心层创建（编号重复转409，数据库异常原样上抛兜底500）
    try:
        case = CaseManager.create_case(data)
    except CaseManagerError as exc:
        if "已存在" in str(exc):
            raise ConflictError(
                "用例编号已存在", detail={"case_id": data.get("case_id")}
            ) from exc
        raise

    return created(data=case)


@cases_bp.route("/<case_id>")
def get_case(case_id: str):
    """
    用例详情接口

    参数:
        case_id (str): 业务用例编号（URL路径参数）

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为用例完整字段

    异常:
        NotFoundError: 用例不存在时抛出（404）
    """
    case = CaseManager.get_case(case_id)
    if case is None:
        raise NotFoundError("用例不存在", detail={"case_id": case_id})
    return success(data=case)


@cases_bp.route("/<case_id>", methods=["PUT"])
def update_case(case_id: str):
    """
    更新用例接口（只更新传入字段）

    请求体（JSON，经CaseUpdateSchema校验，全字段可选）:
        name/module/priority/case_type/status/description/creator
        任意子集；case_id不可修改（body中回传被静默忽略）

    参数:
        case_id (str): 业务用例编号（URL路径参数定位目标用例）

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为更新后用例完整字段

    异常:
        ValidationError: 请求体非JSON / 入参校验失败 /
                         无任何待更新字段时抛出（400）
        NotFoundError: 用例不存在时抛出（404）
    """
    # 1. 请求体解析与Schema校验（partial=True全字段可选）
    body = _get_json_body()
    data = _load_case_payload(CaseUpdateSchema(), body, partial=True)

    # 2. 空更新防御: 校验后无任何可更新字段直接拒绝
    if not data:
        raise ValidationError("至少提供一个待更新字段")

    # 3. 调用核心层更新（用例不存在转404，数据库异常原样上抛兜底500）
    try:
        case = CaseManager.update_case(case_id, data)
    except CaseManagerError as exc:
        if "不存在" not in str(exc):
            raise
        raise NotFoundError(
            "用例不存在", detail={"case_id": case_id}
        ) from exc

    return success(data=case)


@cases_bp.route("/<case_id>", methods=["DELETE"])
def delete_case(case_id: str):
    """
    删除用例接口（物理删除）

    参数:
        case_id (str): 业务用例编号（URL路径参数）

    返回:
        tuple[str, int]: ("", 204)，按HTTP语义204不携带响应体

    异常:
        NotFoundError: 用例不存在时抛出（404）
    """
    # 核心层删除（用例不存在转404，数据库异常原样上抛兜底500）
    try:
        CaseManager.delete_case(case_id)
    except CaseManagerError as exc:
        if "不存在" not in str(exc):
            raise
        raise NotFoundError(
            "用例不存在", detail={"case_id": case_id}
        ) from exc

    return no_content()
