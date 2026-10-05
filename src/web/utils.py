"""
Web 层公共工具（Day45 全量审查第 3 批）

**为什么独立成模块**：`src/web/routes/cases.py` 早已实现了 `_sanitize_log_field`
（日志字段单行化 + 截断，防日志注入），但它是路由内私有函数，
`app.py`（入口请求日志）与 `routes/executions.py`（受理埋点日志）用不上，
于是两处**用户可控文本直接进日志**：
    - `app.py` 的 `request.path`：按 PEP 3333 已被 URL 解码，
      `%0d%0a` 会变成真实换行符，攻击者可借此伪造一整行审计记录
    - `executions.py` 的 module/priority/tags 埋点：直接来自请求体 JSON
把判据收敛到一处，避免"每个新触点都是一个新漏检面"（7.45 的教训）。
`cases.py` 本轮不在改动白名单内，其私有副本保持原样，
待后续批次把它的调用点也切到这里即可完成最终收敛。
"""

import re

# 控制字符集：换行/回车/制表/垂直制表/换页/退格/转义等
# 全部折叠为空格，让日志字段恒为单行
_CONTROL_CHARS = re.compile(r"[\r\n\t\v\f\b\x1b\x00-\x1f\x7f]")

# 日志字段最大长度（字符），超出部分截断并标记
MAX_LOG_FIELD_LENGTH = 200


def sanitize_log_field(value: object) -> str:
    """
    日志字段单行化与截断（防日志注入）

    为什么需要：日志是**审计材料**。用户可控文本（URL 路径、请求体筛选值）
    若原样写入，一次 `GET /api/cases/a%0d%0a[INFO] forged-log` 就能
    在日志里凭空造出一行"看起来像系统产生的"记录，
    污染按 method+path 配对的检索口径，也污染事后追溯的证据链。

    参数:
        value (object): 原始字段值（通常是用户可控字符串；非字符串按 str 转换）

    返回:
        str: 折叠为单行并截断后的安全文本
    """
    collapsed = _CONTROL_CHARS.sub(" ", str(value))
    if len(collapsed) > MAX_LOG_FIELD_LENGTH:
        return f"{collapsed[:MAX_LOG_FIELD_LENGTH]}...[已截断]"
    return collapsed
