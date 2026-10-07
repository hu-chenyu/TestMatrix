"""
source_ref（pytest 可执行目标）校验公共模块（Day47-fix P3-3）

为什么独立成模块（而不是继续放在 case_manager.py）:
    Day47 把 `validate_source_ref` 放在 `src/core/case_manager.py`，供
    YAML/Excel/创建/更新接口四处复用。Day47-fix 要给**执行侧**
    （`PytestRunner.build_command`）再加一道同样的校验，而
    `case_manager` 本身 `from src.core.executors import get_executor`
    —— executors 反向 import case_manager 会构成**循环导入**。
    下沉到本模块（纯标准库 + 正则，零业务依赖）后，
    core 与 web 两侧都从 common 导入，依赖方向保持 core → common 单向。

为什么执行侧也要校验（纵深防御，不是重复劳动）:
    录入侧校验是第一道闸，但库里的值**不只经录入侧写入**——
    回填脚本按 description 文本猜路径、人工直接改库、
    未来的数据迁移都可能塞进非法值。`build_command` 会把它原样拼进
    pytest 子进程命令，故执行侧必须自己再拒一次：
        - `../secret/x.py` 这类越界路径，放行等于让一条用例的测试
          目标跑到项目根之外；
        - 形如 `tests/a.py && evil` 的值，进了命令行就是一次事实上的
          参数注入（当前 subprocess 列表形式无 shell，但拼接进
          其它调用点时风险随实现变化）。

    两侧共用**同一个函数**，因此不会出现"录入侧放行、执行侧拒绝"
    的口径分叉（那会让同一条数据在不同链路上表现不一致，最难排查）。

三条校验规则（与 models.py 的 String(512)、ADR-001 决策③对齐）:
    1. 形态: `^[\\w./-]+\\.py(::\\w+)*$`
       路径段允许字母数字/点/下划线/连字符，函数级可带 `::` 分段；
       不含空格与 shell 元字符，天然阻断选项注入（形如 `--version`
       的值也过不了这条正则）。
    2. 长度: ≤512 字符。**SQLite 不实现 VARCHAR 长度约束**（Day46 实测），
       库层不会拦，本函数是唯一兜底闸。
    3. 禁上级目录 `..`: 该值会拼进子进程命令，放行等于让路径越出项目根。

空值语义: None / 空串 / 纯空格一律归一为 None，含义是
"该用例不可被 pytest 执行"（ADR-001 决策③），不是校验通过。
"""

import re

# source_ref 形态正则（整串匹配）：路径[::类名::函数名]
SOURCE_REF_PATTERN = re.compile(r"^[\w./-]+\.py(::\w+)*$")

# source_ref 长度上限（与 src/db/models.py 的 String(512) 一致）
MAX_SOURCE_REF_LENGTH = 512


def validate_source_ref(value: object, context: str = "") -> str | None:
    r"""
    校验并归一化 source_ref（单一事实来源）

    三条规则: 形态（SOURCE_REF_PATTERN）、长度（≤512）、禁上级目录 ".."。

    参数:
        value (object): 待校验的原始值（None/空串/空格串视为未配置）
        context (str): 错误定位前缀（如 "用例TM-0001: "），为空串时
                       错误消息不带前缀；**调用方应把用例编号与名称
                       都放进前缀**，否则报错只说"哪条数据不合规"、
                       不说"是哪条用例"，排查要回表里逐行找。

    返回:
        str | None: 归一化后的 source_ref（已 strip）；未配置时为 None

    异常:
        ValueError: 非字符串 / 形态非法 / 超长 / 含 ".." 时抛出，
                    消息含 context、字段名与具体原因
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"{context}字段'source_ref'非法: {value!r}，要求为字符串或留空".strip()
        )
    stripped = value.strip()
    if not stripped:
        # 空串与 None 同义（"未配置执行目标"），归一为 None
        return None
    if len(stripped) > MAX_SOURCE_REF_LENGTH:
        raise ValueError(
            f"{context}字段'source_ref'超长: {len(stripped)} 字符，"
            f"上限 {MAX_SOURCE_REF_LENGTH} 字符".strip()
        )
    if not SOURCE_REF_PATTERN.match(stripped):
        raise ValueError(
            f"{context}字段'source_ref'格式非法: {stripped!r}，"
            f"要求形如 'tests/x.py' 或 'tests/x.py::TestC::test_y'"
            f"（.py 结尾，不含空格）".strip()
        )
    path_part = stripped.split("::")[0]
    if ".." in path_part.split("/"):
        raise ValueError(
            f"{context}字段'source_ref'非法: {stripped!r}，"
            f"路径不得含上级目录 '..'".strip()
        )
    return stripped