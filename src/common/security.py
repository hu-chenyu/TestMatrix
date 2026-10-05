"""
凭据脱敏公共原语（Day45 全量审查第 2 批，方案 A：消除三处重复实现）

**为什么单独成模块**：本项目此前有三份各不相同的"打码"逻辑——
    - `http_client.py` 实现了请求头/请求体/URL 查询串三处脱敏，
      但**不递归嵌套 dict**、**不脱敏 URL userinfo 段**、**响应体完全不过**
    - `cache.py` 直接把 Redis URL（含内嵌密码）原样打进日志
    - `assertion.py` 把 `response.url` 与全部响应头原样写进断言失败信息
三处都在"同一类问题、同一套判据"上各自实现，新增渠道时极易再漏。
本模块把判据与算法收敛为一份纯函数，**只依赖标准库**，任何层都可导入
（http_client / cache / assertion 均在 src 或其下层，不产生反向依赖）。

设计约束（都是踩过的坑，勿回退）:
    1. **dict 与 list 必须同等递归**：修复前 list 分支递归、dict 分支不递归，
       导致 `{"user":{"password":"x"}}` 与 `{"items":[{"token":"y"}]}` 明文泄露。
    2. **必须有深度上限**：被测系统可以构造任意深的 JSON，不设上限会栈溢出。
    3. **必须有循环引用保护**：Python 结构体可自引用，朴素递归会无限循环。
    4. **userinfo 与 query 是两段凭据**：`_safe_url` 只覆盖 query，
       而 `redis://user:pass@host` 的密码在 userinfo 段，必须单独处理。
    5. **字段表按"数据的语义位置"选择**：查询串用 `SENSITIVE_QUERY_FIELDS`
       （含裸 key，因 ?key=xxx 是回调凭据形态），请求体用
       `SENSITIVE_BODY_FIELDS`（不含裸 key，请求体里的 key 多是业务数据）。
       两表的关系是"body ⊆ query"，由测试锁定为结构不变量。
"""

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# 字段表（三张表的收录口径是本项目踩过两轮坑换来的，勿随意增删）
# ---------------------------------------------------------------------------

# 需要脱敏的请求/响应头字段（小写匹配）
SENSITIVE_HEADERS = ("authorization", "token", "cookie", "set-cookie", "api-key")

# 请求体中需要脱敏的字段名（小写匹配）
# 口径对齐 SENSITIVE_QUERY_FIELDS 的凭据类字段：历史上本元组漏收
# access_token / api_key / apikey，而查询串侧已收录——同一份凭据放在
# JSON body 里（OAuth 风格接口的标准做法）就会原样进日志。
# 刻意不收裸 "key"：请求体里名为 key 的字段通常是业务数据
# （如字典键、分片键），误打码会显著削弱排障能力，收益与风险不成正比。
SENSITIVE_BODY_FIELDS = (
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "api_key",
    "apikey",
    "access_key",
)

# URL 查询串中需要脱敏的参数名（小写匹配）。常见于 token 走 query 的
# OAuth 风格接口、以及 webhook key 这类把凭据放在 ?key= 的回调地址。
#
# 与 SENSITIVE_BODY_FIELDS 的关系（v3 修复定下的结构不变量）:
#   本元组 ⊇ SENSITIVE_BODY_FIELDS。两张表各自独立维护时曾出现双向缺口:
#     - access_token 只在 query 侧 -> 放 JSON body 里明文进日志
#     - access_key    只在 body 侧 -> 放 ?access_key= 里明文进日志
#   今后新增任一凭据名只改一侧，CI 立刻变红。
#   两表允许的差异只有"裸 key"：查询串侧的 ?key=xxx 是回调凭据，
#   请求体里名为 key 的字段通常是业务数据，故只进 query 表。
SENSITIVE_QUERY_FIELDS = (
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "key",
    "apikey",
    "api_key",
    "access_key",
)

# 脱敏后的占位值
MASK = "***"

# 递归脱敏的最大深度。超深部分原样保留（不再下钻），
# 避免被测系统构造超深结构把栈打爆。
MAX_MASK_DEPTH = 10

# 循环引用/超深位置的占位标记
CIRCULAR_MARK = "[循环引用]"
DEPTH_EXCEEDED_MARK = "[嵌套过深]"


def mask_data(
    data: Any,
    fields: tuple[str, ...] = SENSITIVE_BODY_FIELDS,
    _depth: int = 0,
    _seen: set[int] | None = None,
) -> Any:
    """
    递归脱敏任意结构的 dict / list / 标量

    修复的核心缺陷（Day45 全量审查 P1-6 实测确认）：修复前 dict 分支
    只看顶层键、value 原样返回，而 list 分支却递归——两者不一致，
    于是 `{"user":{"password":"x"}}`（嵌套 dict）与
    `{"items":[{"token":"y"}]}`（dict 里的 list）双双明文泄露，
    而顶层 `[{"password":"x"}]` 正确打码。现统一为两者都递归。

    参数:
        data (Any): 原始数据（dict / list / 标量 / 任意对象）
        fields (tuple[str, ...]): 该数据位置适用的敏感字段名集合
        _depth (int): 递归深度（内部参数，外部不应传入）
        _seen (set[int] | None): 本次递归已访问对象的 id 集合（内部参数，
            用于检测循环引用）

    返回:
        Any: 脱敏后的副本；None 输入返回 "-"；标量原样返回

    异常:
        无（不因任何输入结构抛异常，脱敏失败绝不能搞挂日志本身）
    """
    if data is None:
        return "-"
    if _depth > MAX_MASK_DEPTH:
        return DEPTH_EXCEEDED_MARK
    if not isinstance(data, (dict, list)):
        return data

    # 循环引用保护：同一对象被二次访问即判定为环，直接标记不再下钻。
    # 用 id() 而非对象本身做键，避免不可哈希对象（如 dict/list）无法入 set。
    if _seen is None:
        _seen = set()
    marker = id(data)
    if marker in _seen:
        return CIRCULAR_MARK
    _seen = _seen | {marker}

    if isinstance(data, list):
        return [
            mask_data(item, fields, _depth + 1, _seen) for item in data
        ]

    masked: dict[Any, Any] = {}
    for key, value in data.items():
        if str(key).lower() in fields:
            masked[key] = MASK
        else:
            # 关键修复点：未命中敏感键的 value 同样递归（此前原样返回）
            masked[key] = mask_data(value, fields, _depth + 1, _seen)
    return masked


def mask_headers(headers: dict[str, Any] | None) -> dict[str, Any] | str:
    """
    请求/响应头脱敏（敏感头的值替换为占位符）

    参数:
        headers (dict | None): 原始头字典；None 表示没有传头

    返回:
        dict | str: 脱敏后的副本；入参为 None 时返回 "-"

    异常:
        无
    """
    if headers is None:
        return "-"
    return {
        key: (MASK if str(key).lower() in SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


def mask_url_credentials(url: str) -> str:
    """
    脱敏 URL **userinfo 段**的口令（redis://user:pass@host 形态）

    与 mask_url 的分工：mask_url 只管 query 参数，而口令在 userinfo 段，
    两者是两段独立凭据，必须各自处理。`urlsplit` 把 netloc 原样返回，
    这里在 netloc 内定位最后一个 "@"，把它之前的部分做口令打码。

    参数:
        url (str): 可能含 userinfo 的 URL

    返回:
        str: userinfo 口令已打码的 URL；解析失败或本无 userinfo 时原样返回

    异常:
        无（畸形 URL 原样返回，由调用方决定后续截断策略）
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return str(url)
    netloc = parts.netloc
    if "@" not in netloc:
        return str(url)
    userinfo, _, hostpart = netloc.rpartition("@")
    if ":" not in userinfo:
        # 只有用户名没有口令（如 redis://user@host），无需打码
        return str(url)
    user, _, _password = userinfo.partition(":")
    safe_netloc = f"{user}:{MASK}@{hostpart}"
    return urlunsplit(
        (parts.scheme, safe_netloc, parts.path, parts.query, parts.fragment)
    )


def mask_url(url: str) -> str:
    """
    构造可安全落日志/进异常消息的 URL（userinfo + 查询串双重脱敏）

    背景：本项目此前对 Authorization/Token 头与 password/token 请求体
    字段都做了脱敏，但 URL 原样进日志；凭据走 query 的接口极常见
    （?token=xxx / ?key=xxx），且 requests 连接层异常文本自带完整 URL，
    会二次绕过脱敏。Day45 第 2 批再补上 userinfo 段
    （redis://user:pass@host 这类）。

    参数:
        url (str): 原始 URL

    返回:
        str: userinfo 与查询串凭据均已打码、且已剥离 fragment 的 URL
    """
    safe = mask_url_credentials(str(url))
    try:
        parts = urlsplit(safe)
    except ValueError:
        # 极端畸形 URL 连解析都失败时，直接按纯文本截断，不因脱敏而抛错
        return safe

    if not parts.query:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    pairs = parse_qsl(parts.query, keep_blank_values=True)
    masked_pairs = [
        (name, MASK if name.lower() in SENSITIVE_QUERY_FIELDS else value)
        for name, value in pairs
    ]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(masked_pairs), "")
    )


def mask_sql_parameters(message: str) -> str:
    """
    抹掉 SQLAlchemy 异常文本里的**参数值**，只保留语句骨架

    场景：`DBAPIError.__str__` 形如
        (sqlite3.OperationalError) no such table: t
        [SQL: SELECT * FROM t WHERE id = ?]
        [parameters: ('TM-SECRET-0001', 1, 0)]
    第三行是**参数值**——它可能正是用例编号、文件名、连接串等业务输入，
    在 TESTING 档位被原样回显进 HTTP 500 响应体即构成信息泄露
    （Day45 全量审查 P2-3 实测：`[parameters: ('TM-SECRET-0001', 1, 0)]`
    完整出现在响应 JSON 中）。

    策略：把 `[parameters: ...]` 整段替换为不含值的占位。语句本身
    （`[SQL: ...]`）保留——它是排障最关键的线索且不含具体取值；
    完整信息仍在服务端日志里（`logger.error` 不做处理）。

    参数:
        message (str): 异常原始文本

    返回:
        str: 参数值已被抹除的文本；无 parameters 段时原样返回
    """
    text = str(message)
    if "[parameters:" not in text:
        return text
    out_lines: list[str] = []
    for line in text.splitlines():
        if "[parameters:" in line:
            out_lines.append("[parameters: " + MASK + "]")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)
