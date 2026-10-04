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

功能（第三阶段Day21交付）:
    - POST /api/cases/import      用例批量导入（YAML/Excel上传 →
      DataDriver解析 → 幂等upsert入库，返回导入统计）

入参校验说明:
    - 请求体经marshmallow Schema校验（必填/长度/枚举），
      校验失败抛ValidationError(400)，message含具体字段错误
    - case_id重复创建转ConflictError(409)；
      用例不存在转NotFoundError(404)
"""

import os
import re
import shutil
import tempfile
from pathlib import Path

import marshmallow
from flask import Blueprint, request
from marshmallow import EXCLUDE, Schema, fields, validate
from werkzeug.utils import secure_filename

from src.common.logger import LogManager
from src.core.cache import cache_client, cases_list_key
from src.core.case_manager import (
    MAX_PAGE_SIZE,
    CaseConflictError,
    CaseDataLoadError,
    CaseManager,
    CaseNotFoundError,
)
from src.web.exceptions import (
    ConflictError,
    NotFoundError,
    ValidationError,
)
from src.web.pagination import parse_int_param, parse_page_param
from src.web.response import created, no_content, success

logger = LogManager.get_logger()

cases_bp = Blueprint("cases", __name__, url_prefix="/api/cases")

# 用例类型合法值
VALID_CASE_TYPES = ("api", "chip")

# 用例状态查询合法值（all为"查全部"语义，透传核心层时转为None）
VALID_CASE_STATUSES = ("active", "disabled", "all")

# 分页参数默认值
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 20

# 批量导入支持的文件后缀（与DataDriver支持的格式对齐）
IMPORT_SUFFIXES = (".yaml", ".yml", ".xlsx")


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

    # case_id 字符集（Day43 收尾修复 D6/M2）: 首字符必须是字母或数字，
    # 其余允许字母/数字/点/下划线/连字符。**为什么必须禁 `-` 开头**:
    # PytestRunner 把 case_id 当测试文件路径传给 pytest（build_command），
    # `case_id="--version"` 会让命令变成 `pytest --version -q`，pytest
    # 打印版本后退出码 0，被判定为 passed —— 假通过会直接虚高通过率。
    # executors 侧已用 `--` 终止符兜底，这里再加字符集约束是纵深防御：
    # 编号本就该有命名规范，同时挡住 `../` 之类的路径形态。
    # 只约束 create：update schema 不含 case_id（编号不可改，见 6.9），
    # 故存量数据（含历史遗留的非常规编号）不受影响。
    case_id = fields.String(
        required=True,
        validate=[
            validate.Length(min=1, max=64),
            validate.Regexp(
                r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
                error="用例编号只能由字母、数字、点、下划线、连字符组成，且必须以字母或数字开头",
            ),
        ],
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
    解析整型查询参数（内部方法，委托公共实现）

    参数缺省或空白串时返回默认值；传入非合法整数时抛ValidationError。

    参数:
        name (str): 查询参数名（page/page_size）
        default (int): 参数缺省时的默认值

    返回:
        int: 解析后的整数值

    异常:
        ValidationError: 参数值不是合法整数时抛出
    """
    return parse_int_param(name, default, param_hint="page和page_size")


def _parse_optional_str(name: str) -> str | None:
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

    缓存（Day31）:
        规范化七维参数哈希为key，命中缓存直接返回；写操作经
        CaseManager失效埋点主动清缓存，TM_CACHE_TTL兜底。
        TM_REDIS_ENABLED=false时整段no-op，行为与未接入一致。

    异常:
        ValidationError: 分页参数非法 / case_type或status取值非法时
                         抛出（全局异常处理器统一转400响应）
    """
    # 1. 分页参数解析与范围校验（page 经 parse_page_param 收口到
    #    [1, MAX_PAGE]；上界的单一事实来源是核心层 case_manager.MAX_PAGE，
    #    Day44 P2-07。公共解析层 parse_int_param 仍只做取值转换，
    #    范围口径留在调用点，避免三处路由被一处需求带着一起变）
    page = parse_page_param("page", DEFAULT_PAGE, "page和page_size")
    page_size = _parse_int_param("page_size", DEFAULT_PAGE_SIZE)
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

    # 3. 规范化查询参数（与透传核心层的实参口径完全一致，
    #    缺失维度统一None；status=all转None查全部状态）
    normalized_status = None if status == "all" else status
    cache_params = {
        "module": module,
        "priority": priority,
        "case_type": case_type,
        "status": normalized_status,
        "keyword": keyword,
        "page": page,
        "page_size": page_size,
    }

    # 4. 先查缓存（Day31）: 命中直接返回，未启用/未命中/Redis
    #    故障时get_json返回None走回源，对调用方完全透明
    cache_key = cases_list_key(cache_params)
    cached_result = cache_client.get_json(cache_key)
    if cached_result is not None:
        return success(data=cached_result)

    # 5. 缓存未命中: 调用核心层分页查询并回写缓存（空结果也缓存，
    #    防穿透；TTL到期前同参数请求不再查库）
    result = CaseManager.list_cases_paged(**cache_params)
    cache_client.set_json(cache_key, result)
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
    #    异常翻译按**类型**判定（CaseConflictError），不再依赖错误文案子串。
    #    子串匹配会把恰好含"已存在"的数据库错误（如"表已存在"）误判成409，
    #    真实故障被静默转成业务错误。核心层 6 个相关抛出点已全部类型化，
    #    泛型 CaseManagerError 一律原样上抛走500，不再做任何文案匹配。
    try:
        case = CaseManager.create_case(data)
    except CaseConflictError as exc:
        raise ConflictError(
            "用例编号已存在", detail={"case_id": data.get("case_id")}
        ) from exc

    # 创建成功业务埋点（Day29）: 业务编号/模块/优先级落日志，
    # 便于按用例维度检索创建轨迹；仅记日志不改响应结构
    logger.info(
        f"用例已创建 | case_id={data['case_id']} | "
        f"module={data['module']} | priority={data['priority']}"
    )

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
    #    纯类型判定，无子串兜底（理由同 create_case）
    try:
        case = CaseManager.update_case(case_id, data)
    except CaseNotFoundError as exc:
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
    except CaseNotFoundError as exc:
        raise NotFoundError(
            "用例不存在", detail={"case_id": case_id}
        ) from exc

    return no_content()


# 导入错误消息中的绝对路径特征：Windows 盘符路径 / Unix 常见根目录
#
# 修正要点（v2 审查 V2-P1-2）
# ----------------------------------------
# 1) 路径**段内允许空格**。原字符类排除了 \s，导致任何含空格的路径
#    （`C:\Users\John Smith\...`、`C:\Program Files\...`）整条匹配失败
#    原样回显；更糟的是部分匹配会把安全的 `C:\Users\` 前缀打码、留下
#    账户名与后续路径——看起来生效了，实际泄露更多。
# 2) 真正的终止边界是"错误消息把路径包起来的那层壳"：引号、括号、
#    顿号与换行（跨行必须停，否则会吞掉后续的行列号等诊断信息）。
# 3) 但空格不能无条件放行，否则贪婪匹配会跨过散文吞掉下一条路径
#    （`.../a.yaml then /tmp/.../b.yaml` 只剩 b.yaml）。故空格仅在
#    "其后紧跟的非空格串能在同段内遇到分隔符"时才算路径字符：
#      - `John Smith\AppData`  → 空格后是 `Smith\` → 放行
#      - `John  Smith\secret`  → 连续两个空格，同样放行（见下方 {1,}）
#      - `a.yaml then /tmp`    → 空格后是 `then `（非空格串后又是空格，
#                                 遇不到分隔符）→ 不放行，路径在此终止
# 4) 捕获组 1 排除分隔符，保证只保留 basename；目录段整体放进可选的
#    `(?:...)?`，使 `C:\a.yaml` 这类无目录短路径也能收敛。
# 5) **name 类的普通分支同样必须排除空格**（与 _PATH_CHAR 对齐）。
#    v3 只改了 _PATH_CHAR 而漏改两个 name 类，导致同一条消息里的第二条
#    Windows 路径整体泄露：basename 组因允许空格而吞下 `a.yaml in D:`
#    （连同下一个盘符前缀），非重叠扫描从 `D:` 之后继续，剩余
#    `\y\b.ini` 不再以 `[A-Za-z]:\` 开头 → 零匹配 → 原样回显。
#    实证：`see C:\x\a.yaml in D:\y\b.ini` 在 v3 输出 `see a.yaml in
#    D:\y\b.ini`（v2 是 `see a.yaml in b.ini`，即能力回退）。
# 注意：字符类内的 `]` 必须转义，否则会提前闭合字符类。
_PATH_STOP = r"\r\n\"'，、)\]"
# 各类字符的"禁止集合"显式拆出，避免在 f-string 里手写字符类时把
# 空格/反斜杠写到括号外面（那会让整条正则静默失配，不报错只不匹配）。
# 注意：字符类里要排除的是**正则转义后的反斜杠**，故必须用 "\\\\"（值
# 为两个反斜杠）；写成 " \\"（值仅一个）会被当成转义下一个字符。
_BS = "\\\\"  # 正则字符类中代表"一个反斜杠"的写法
# 目录段**不**排除反斜杠：目录段本就要跨 `Users\John Smith\AppData\...`
# 逐层消费；只有文件名段需要排除分隔符，否则 group 1 会把整条路径
# （含散文与下一条路径的盘符前缀）都吞进去
_PATH_BODY_CHARS = _PATH_STOP + " "             # 目录段：仅排除空格
_PATH_WIN_NAME_CHARS = _PATH_STOP + " " + _BS    # Windows 文件名段：再排除反斜杠
_PATH_POSIX_NAME_CHARS = _PATH_STOP + "/ "       # POSIX 文件名段：再排除斜杠
# 空格前瞻：其后紧跟的非空格串能在同段内遇到分隔符，才算路径字符。
# 用 {1,} 而非单个——多空格目录名（`John  Smith`）是合法形态，收紧成
# 单空格会让账户名原样回显（泄露比"多脱敏损诊断值"严重得多）。
_PATH_SPACE_WIN = r" {1,}(?=[^ \r\n\"'，、)\]]*[\\/])"
_PATH_SPACE_POSIX = r" {1,}(?=[^ \r\n\"'，、)\]]*/)"
_PATH_CHAR = rf"(?:[^{_PATH_BODY_CHARS}]|{_PATH_SPACE_WIN})"
# 文件名部分额外排除分隔符与空格，否则 group 1 会把整条路径（含散文
# 与下一条路径的盘符前缀）都吞进去
_PATH_NAME_CHAR = rf"(?:[^{_PATH_WIN_NAME_CHARS}]|{_PATH_SPACE_WIN})"
_PATH_NAME_CHAR_POSIX = rf"(?:[^{_PATH_POSIX_NAME_CHARS}]|{_PATH_SPACE_POSIX})"

_WIN_ABS_PATH = re.compile(
    rf"[A-Za-z]:\\(?:{_PATH_CHAR}*\\)?({_PATH_NAME_CHAR}+)"
)
_UNIX_ABS_PATH = re.compile(
    r"/(?:tmp|home|Users|var|opt|usr|root|private|Applications|etc|srv|data|mnt|www)"
    rf"(?:{_PATH_CHAR}*/)?({_PATH_NAME_CHAR_POSIX}+)"
)


# 单条日志字段的最大长度（字符），防止超长用户输入刷爆日志
MAX_LOG_FIELD_LENGTH = 200

# 日志字段中的控制字符（换行/回车/制表/垂直制表/换页）统一折叠为空格。
# 用户可控字段（如下载文件名）若原样写入日志，其中的换行会终止当前日志行，
# 后续内容被解析为一条独立日志——攻击者可借此伪造审计记录（日志注入）。
_CONTROL_CHARS = re.compile(r"[\r\n\t\v\f]")


def _sanitize_log_field(value: str) -> str:
    """
    日志字段单行化与截断（防日志注入）

    参数:
        value (str): 原始字段值（通常是用户可控的字符串）

    返回:
        str: 折叠为单行并截断后的安全文本
    """
    collapsed = _CONTROL_CHARS.sub(" ", str(value))
    if len(collapsed) > MAX_LOG_FIELD_LENGTH:
        return f"{collapsed[:MAX_LOG_FIELD_LENGTH]}...[已截断]"
    return collapsed


def _sanitize_error_message(raw: str) -> str:
    """
    对错误消息中的服务端绝对路径做脱敏，仅保留文件名（basename）

    背景: 导入失败时 CaseManager 抛出的错误消息含临时目录绝对路径，
        形如 "C:\\Users\\<账户名>\\AppData\\Local\\Temp\\tm_case_import_xxx\\a.yaml"，
        该消息经前端 toast 直接展示给终端用户，会泄露服务端目录结构与
        操作系统账户名。本函数把绝对路径整体替换为其末段文件名。

    保留信息（脱敏不得丢失诊断价值）:
        - 错误类型描述（如 "YAML语法解析失败"）
        - 文件名（basename）
        - 行列号等定位信息（位于路径之外，原样保留）

    参数:
        raw (str): 原始错误消息

    返回:
        str: 脱敏后的错误消息；无可脱敏内容时原样返回
    """
    if not raw:
        return raw
    # 先处理 Windows 盘符路径（C:\...），再处理 Unix 绝对路径
    sanitized = _WIN_ABS_PATH.sub(r"\1", raw)
    sanitized = _UNIX_ABS_PATH.sub(r"\1", sanitized)
    return sanitized


@cases_bp.route("/import", methods=["POST"])
def import_cases():
    """
    用例批量导入接口（YAML/Excel上传 → DataDriver解析 → 幂等upsert入库）

    请求格式:
        multipart/form-data，文件字段名固定为"file"，
        文件后缀仅支持.yaml/.yml/.xlsx

    查询参数（均可选）:
        sheet_name (str): Excel的sheet名称（仅.xlsx生效），默认None读活动sheet
        creator (str): 用例创建人（仅首次插入时写入），默认"admin"

    执行流程:
        1. 校验上传文件存在性与后缀合法性（未上传/格式不支持 → 400）
        2. secure_filename清洗文件名后保存到临时目录（防路径遍历攻击）
        3. 调CaseManager.sync_cases_from_file解析并逐条upsert入库
           （case_id存在则更新业务字段，不存在则插入，天然幂等）
        4. finally清理临时文件与临时目录，防止磁盘泄漏

    参数:
        无（从request.files与request.args解析）

    返回:
        tuple[dict, int]: (统一响应体, 200)，data为导入统计:
            {"file_name": 原始文件名, "total": 加载总数,
             "inserted": 新增数, "updated": 更新数}

    异常:
        ValidationError: 未上传文件 / 后缀不支持 / 数据校验失败
                       （DataDriver校验错误透传，含行号与字段名）时抛出（400）
        CaseManagerError: 其他核心层异常原样上抛（兜底500）
    """
    # 1. 获取上传文件（未上传或文件名为空视为非法请求）
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ValidationError("未上传用例数据文件")
    original_name = upload.filename

    # 2. 后缀白名单校验（空后缀时用文件名定位问题）
    suffix = Path(original_name).suffix.lower()
    if suffix not in IMPORT_SUFFIXES:
        # 仅取 basename：original_name 是用户可控的原始上传名，可能含路径
        # 分隔符（如 "../../secret.yaml"），直接回显会泄露客户端路径信息
        safe_name = Path(original_name).name or original_name
        raise ValidationError(
            f"不支持的文件格式: {suffix or safe_name}，"
            f"仅支持 {'/'.join(IMPORT_SUFFIXES)}"
        )

    # 3. 查询参数解析（sheet_name仅Excel生效；creator缺省admin）
    sheet_name = _parse_optional_str("sheet_name")
    creator = _parse_optional_str("creator") or "admin"

    # 4. 清洗文件名后落盘临时目录（secure_filename剥离路径分隔符等危险字符）
    tmp_dir = tempfile.mkdtemp(prefix="tm_case_import_")
    # secure_filename 会剥掉全部非 ASCII 字符：纯中文名会被压成空串
    # （secure_filename('用例数据.xlsx') == 'xlsx'，扩展名连同分隔点一起丢失），
    # 导致落盘文件名无后缀、被 data_driver 判成"格式不支持"。因此清洗后若
    # 结果不含后缀，用原始名的后缀补回。仍为纯 ASCII 安全名，不引入路径风险。
    safe_stem = secure_filename(Path(original_name).stem)
    safe_suffix = Path(original_name).suffix.lower()
    safe_name = f"{safe_stem}{safe_suffix}" if safe_stem else f"import{safe_suffix}"
    tmp_file = Path(tmp_dir) / safe_name
    try:
        upload.save(tmp_file)

        # 5. 调核心层同步入库（数据校验失败转400，数据库异常原样上抛兜底500）
        try:
            stats = CaseManager.sync_cases_from_file(
                tmp_file, sheet_name=sheet_name, creator=creator
            )
        except CaseDataLoadError as exc:
            # 路径脱敏: str(exc) 含服务端临时目录绝对路径与操作系统账户名，
            # 该消息会经前端 toast 展示给终端用户；脱敏后仅保留文件名、
            # 错误类型与行列号等定位信息（见 _sanitize_error_message）
            raise ValidationError(_sanitize_error_message(str(exc))) from exc
    finally:
        # 临时文件与临时目录必须清理（ignore_errors防Windows句柄残留导致的报错）
        if tmp_file.exists():
            os.remove(tmp_file)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # 导入完成业务埋点（Day29）: 新增/更新计数与原始文件名落日志
    # （文件名取原始上传名，非secure_filename清洗名，口径同响应data）
    # original_name 完全由客户端控制，其中的换行/控制字符会伪造日志行
    # （日志注入），因此写入前统一折叠为单行并截断长度。
    logger.info(
        f"用例导入完成 | inserted={stats['inserted']} | "
        f"updated={stats['updated']} | "
        f"文件={_sanitize_log_field(original_name)}"
    )

    return success(
        data={
            # v3 修复 V2-P3-10: 与上面的日志口径对齐，统一走
            # _sanitize_log_field。filename 完全由客户端控制，未折叠换行
            # 时回显给前端存在观感与信息暴露口径不一致（日志已清洗、响应
            # 未清洗）。已确认非 XSS——toast 走 textContent 由浏览器转义，
            # 这里修的是两处口径不一致。
            "file_name": _sanitize_log_field(original_name),
            "total": stats["total"],
            "inserted": stats["inserted"],
            "updated": stats["updated"],
        }
    )
