"""
pytest 终端输出结构化解析器（Day50 任务一）

功能:
    - ParsedResult / FailedCase  两个结构化数据类
    - PytestOutputParser.parse   从 pytest 终端输出提取汇总数字与失败明细
    - parse_progress_percent     从输出片段提取进度百分比（供实时展示）

背景（为什么本日要补这一层）:
    Day49 的 `PytestRunner.run_one` 拿到 pytest 子进程输出后只做了尾部截断
    （`truncate_output_tail`），把一整段文本塞进 `ExecutionResult.output`。
    调用方（编排层 / Web 层 / 通知层）拿到的只有一段文本：想知道"通过几条、
    失败哪几条、失败原因是什么"只能自己写正则，而每处各写一套正则的必然
    结果是口径漂移——同一个 `3 failed, 27 passed in 1.2s`，三处解析给出
    三个不同答案。解析收敛到本模块，口径由单元测试钉死。

能力边界（**本模块只解析 pytest 终端输出，不解析 Allure**）:
    - 支持: `-q` / 默认 / `-v` 三种常规输出下的末尾汇总行
      （`5 passed in 0.12s`、`1 failed, 2 passed, 1 warning in 1.23s`、
      `2 errors, 3 passed, 1 skipped, 4 warnings in 5.67s`、
      `no tests ran in 0.01s`）
    - 支持: 末尾 short test summary info 段的 `FAILED` / `ERROR` 行，
      并回溯其对应的 FAILURES 段取诊断尾部
    - 支持: `[_ 9%]` 形态的进度百分比提取
    - 不支持: **pytest-xdist 分布式输出**（`[gw0] [ 9%]` 前缀会打断本模块
      按行匹配的假设，汇总行仍可解析但用例名会带 `[gw0]` 前缀）、
      **`-p no:terminalreporter` / 自定义 reporter** 的输出、
      **JUnit XML**（那是文件产物，应由 `report_analyzer` 走 Allure 链路）
    - 遇到无法识别的输出（空串、非 pytest 输出、pytest 崩溃无汇总行）时
      返回 `parse_ok=False` 的结果对象而**不抛异常**

设计说明:
    - 不引入新第三方依赖: 全部基于标准库 re 与字符串处理
    - 绝不抛异常是硬契约: 解析是旁路能力，解析失败绝不能让执行结果丢失
      （与通知旁路铁律同源）
"""

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# 摘要行解析（正则在模块级预编译，避免每次调用重复编译）
# ---------------------------------------------------------------------------
# 计数标签 → ParsedResult 字段名（**单一事实来源**）
#
# 为什么单复数都要收录: pytest 同一份摘要里会混用两种写法
# （`1 error` 与 `2 errors`、`1 warning` 与 `2 warnings`），只收一种会让
# 单数场景静默漏计——而"漏计错误数"恰恰是这类解析器最不能出的错。
_LABEL_FIELD_MAP = {
    "passed": "passed_count",
    "failed": "failed_count",
    "error": "error_count",
    "errors": "error_count",
    "skipped": "skipped_count",
    "xfailed": "xfailed_count",
    "xpassed": "xpassed_count",
    "warning": "warnings_count",
    "warnings": "warnings_count",
    "deselected": "deselected_count",
}

# 计数项：`(数字) + (单词标签)`。
#
# 标签分支**由 _LABEL_FIELD_MAP 反向生成**而不是另写一份字面量：两处
# 各写一份时，"给正则加了标签却忘了加映射"必然发生，而它的表现是某个
# 计数**静默为 0**——没有任何报错，只是数字对不上。从表生成让这类漂移
# 在结构上不可能发生（正则能匹配的标签必然在表里）。
# 按长度倒序拼接，保证 "warnings" 优先于 "warning" 被尝试。
_COUNT_PATTERN = re.compile(
    r"(?P<count>\d+)\s+(?P<label>"
    + "|".join(
        re.escape(label) for label in sorted(_LABEL_FIELD_MAP, key=len, reverse=True)
    )
    + r")\b"
)

# 耗时：`in 0.12s` / `in 65.43s (0:01:05)` / `in 0:01:05`。
#
# 为什么要收时分秒形态: pytest 对超过一分钟的执行会把耗时渲染成
# `in 65.43s (0:01:05)`，长跑用例（真实执行器的主要场景）恰恰只会命中
# 这个形态，只匹配 `\d+s` 会让它们的耗时恒为 0。
_DURATION_PATTERN = re.compile(
    r"\bin\s+(?P<duration>\d+(?:\.\d+)?s|\d{1,2}:\d{2}(?::\d{2})?)"
)

# `no tests ran in 0.01s`：没有任何计数项，但**是一次合法的执行结果**
# （选择器过滤过头时的常态），不能与"输出无法识别"混为一谈
_NO_TESTS_PATTERN = re.compile(r"\bno tests ran\b")

# ---------------------------------------------------------------------------
# 失败明细解析
# ---------------------------------------------------------------------------
# short test summary info 段的 `FAILED` / `ERROR` 行。
#
# 形态: `FAILED tests/test_a.py::test_x - assert 1 == 2`
#       `ERROR tests/test_b.py::test_y`
#       `ERROR tests/test_b.py - fixture 'tmp' not found`
# node 用非贪婪 `\S+?` 是为了让后面的 ` - 原因` 尽可能被分给 reason 而非
# 混进用例名；但因 `\S` 不含空格，node 不会被从中间截断。
_SHORT_SUMMARY_PATTERN = re.compile(
    r"^(?P<stage>FAILED|ERROR)\s+(?P<node>\S+?)(?:\s+-\s+(?P<reason>.+))?$"
)

# FAILURES 段内每条失败用例的小节标题（整行都是下划线 + 标题）:
#     __________________________ test_x __________________________
#     _____ ERROR at setup of test_y _____
#
# 只匹配 `_` 而不匹配 `=`，因为 `=` 是段级分隔线
# （`==== FAILURES ====`、`==== short test summary info ====`），
# 两者混在一起会把段标题识别成一条用例。
_SECTION_HEADER_PATTERN = re.compile(r"^_{3,}\s*(?P<title>.+?)\s*_{3,}\s*$")

# 进度百分比：`[  9%]` / `[100%]`
_PROGRESS_PATTERN = re.compile(r"\[\s*(?P<percent>\d{1,3})%\]")

# ---------------------------------------------------------------------------
# 解析上限（防止被测代码自己打印海量日志把结果对象撑爆）
# ---------------------------------------------------------------------------
# 每个失败用例保留的诊断尾部行数
TRACE_TAIL_MAX_LINES = 3

# 每个失败用例诊断尾部的最大字符数
TRACE_TAIL_MAX_CHARS = 300

# 进度百分比的上限（钳制到 100，防被测代码打印形如 `[999%]` 的噪声）
PROGRESS_PERCENT_MAX = 100


@dataclass
class FailedCase:
    """
    单条失败/错误用例的结构化明细

    字段说明:
        node_id    pytest 用例节点 ID（`tests/test_a.py::test_x`）；
                   xdist 下会带 `[gw0]` 前缀，保留原样便于定位
        name       用例名（node_id 末段，含参数化后缀如 `test_x[1-2]`）
        stage      失败阶段：`failed`（断言不通过）/ `error`（环境或代码异常）
        reason     失败原因摘要（short test summary 行中 ` - ` 之后的部分）；
                   该行未带原因时为空串
        trace_tail 诊断尾部：对应 FAILURES 小节的末尾若干行。短回溯
                   （`--tb=short`）下是断言/异常摘要行，完整回溯下是调用栈尾帧
    """

    node_id: str = ""
    name: str = ""
    stage: str = ""
    reason: str = ""
    trace_tail: str = ""


@dataclass
class ParsedResult:
    """
    pytest 终端输出的结构化解析结果

    字段说明:
        passed_count / failed_count / error_count / skipped_count
            四项核心计数；`error_count` 对应 pytest 汇总行里的 error/errors
            （即 Allure 口径的 broken：环境/代码异常，非断言失败）
        xfailed_count / xpassed_count
            预期失败与"意外通过"计数。**不计入 total_count**：
            pytest 自己的 `N passed` 同样不含它们，混进去会让分母口径漂移
        warnings_count            警告数
        deselected_count          被选择器过滤掉的用例数
        total_count               passed + failed + error + skipped 四项之和
                                 （见上方 xfail/xpass 说明）
        duration_sec              汇总行里的耗时（秒）
        failed_cases              失败/错误用例明细列表（按输出顺序）
        summary_line              命中的原始汇总行（便于排障时回溯上下文）
        progress_percent          输出中最后一个进度百分比；无进度行时为 None
        parse_ok                  是否识别出 pytest 汇总特征。
                                  False = 空输出 / 非 pytest 输出 /
                                  pytest 崩溃且未输出汇总行（退出码 3/4 常见）
    """

    passed_count: int = 0
    failed_count: int = 0
    error_count: int = 0
    skipped_count: int = 0
    xfailed_count: int = 0
    xpassed_count: int = 0
    warnings_count: int = 0
    deselected_count: int = 0
    total_count: int = 0
    duration_sec: float = 0.0
    failed_cases: list[FailedCase] = field(default_factory=list)
    summary_line: str = ""
    progress_percent: int | None = None
    parse_ok: bool = False

    @property
    def failure_count(self) -> int:
        """
        失败合计（断言失败 + 环境/代码异常）

        为什么与 `failed_count` 分开: 本项目统计口径铁律是
        "failed 合计 = failed + broken"（见 PROJECT_CONTEXT 6.3），
        而 pytest 侧 broken 对应 error_count。直接把两者分开暴露给
        调用方，就是为了让"合计"只有一个算法，不在三处各写一遍加法。

        返回:
            int: failed_count + error_count

        异常:
            无
        """
        return self.failed_count + self.error_count


class PytestOutputParser:
    """
    pytest 终端输出解析器（纯静态、无状态、可安全并发调用）

    全部方法均为静态方法且不持有任何状态：本解析器可能同时被批次编排
    线程与 Web 请求线程调用，有状态解析器会在并发下互相污染。
    """

    @staticmethod
    def parse(output: str) -> ParsedResult:
        """
        解析 pytest 终端输出，返回结构化结果

        识别策略（按优先级）:
            1. 从**末尾向前**找第一条含计数项且含耗时的行作为汇总行
               （倒序是为了避开被测代码自己打印的形似文本）
            2. 汇总行找不到时退而求其次：只要出现 `FAILED`/`ERROR` 短摘要行
               就算识别成功（收集阶段崩溃等场景仍能给出可用明细）
            3. 两者都没有 → `parse_ok=False`

        参数:
            output (str): pytest 子进程的完整输出文本（stdout+stderr 拼接）；
                          空串与非 pytest 文本均合法

        返回:
            ParsedResult: 结构化解析结果。**任何情况下都不抛异常**

        异常:
            无（硬契约：解析是旁路能力，失败绝不能影响执行结果落库）
        """
        parsed = ParsedResult()
        if not output or not output.strip():
            return parsed

        lines = output.splitlines()
        parsed.progress_percent = parse_progress_percent(output)
        parsed.summary_line = PytestOutputParser._extract_summary_line(lines)
        if parsed.summary_line:
            PytestOutputParser._fill_counts(parsed, parsed.summary_line)
            parsed.parse_ok = True
        else:
            # 无汇总行（退出码 3/4 等场景），只要有短摘要行仍算识别成功
            parsed.parse_ok = any(
                _SHORT_SUMMARY_PATTERN.match(line.strip()) for line in lines
            )

        parsed.failed_cases = PytestOutputParser._extract_failed_cases(lines)
        # 识别不出汇总行时不该对外宣称"零失败"——那会被下游当成
        # "全部通过"入库，故此处显式打掉两个计数，与 parse_ok 保持一致
        if not parsed.parse_ok:
            parsed.failed_cases = []
        parsed.total_count = (
            parsed.passed_count
            + parsed.failed_count
            + parsed.error_count
            + parsed.skipped_count
        )
        return parsed

    @staticmethod
    def _extract_summary_line(lines: list[str]) -> str:
        """
        从输出行中定位 pytest 末尾汇总行（倒序扫描）

        参数:
            lines (list[str]): 输出的行列表

        返回:
            str: 命中的汇总行原文；未命中时返回空串

        异常:
            无

        为什么倒序而不是取最后一行: `no tests ran in 0.01s` 之后 pytest 还会
        打印警告汇总与 Docs 提示行，最后一行未必是汇总行；而被测代码打印的
        形似文本总在汇总行之前出现，倒序扫描天然避开它们。
        """
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            # 耗时是**必需**条件，与是否有计数项无关：
            #   `no tests ran` 既可能是合法汇总行（`no tests ran in 0.01s`），
            #   也可能是 pytest 崩溃时打印的裸小节标题（不带耗时）。
            #   后者若被当成汇总行，一次内部错误就会被记成"执行成功、
            #   零个用例"——把崩溃说成正常，是这类解析器最贵的错。
            if not _DURATION_PATTERN.search(stripped):
                continue
            if _NO_TESTS_PATTERN.search(stripped) or _COUNT_PATTERN.search(stripped):
                return stripped
        return ""

    @staticmethod
    def _fill_counts(parsed: ParsedResult, summary_line: str) -> None:
        """
        从汇总行填充计数与耗时到结果对象（原地修改）

        参数:
            parsed (ParsedResult): 待填充的结果对象
            summary_line (str): 已命中的汇总行原文

        返回:
            无

        异常:
            无

        同一标签重复出现时**累加而非覆盖**: `-W` 重复参数或分段汇总可能
        让同一类计数出现在多行上，保留最后一次会让计数随输出形态漂移。
        """
        for match in _COUNT_PATTERN.finditer(summary_line):
            # 直取而非 .get()+守卫：正则的标签分支由 _LABEL_FIELD_MAP
            # 反向生成，能匹配到的标签必然在表里，键缺失即代码损坏，
            # 那种情况让解析器崩掉比静默少计一个数更容易被发现
            field_name = _LABEL_FIELD_MAP[match.group("label")]
            current = getattr(parsed, field_name)
            setattr(parsed, field_name, current + int(match.group("count")))

        duration_match = _DURATION_PATTERN.search(summary_line)
        if duration_match is not None:
            parsed.duration_sec = _parse_duration(duration_match.group("duration"))

    @staticmethod
    def _extract_failed_cases(lines: list[str]) -> list[FailedCase]:
        """
        从输出中提取失败/错误用例明细（短摘要行 + FAILURES 小节回溯）

        参数:
            lines (list[str]): 输出的行列表

        返回:
            list[FailedCase]: 失败明细列表；无失败时返回空列表

        异常:
            无
        """
        sections = PytestOutputParser._collect_failure_sections(lines)
        cases: list[FailedCase] = []
        for raw_line in lines:
            match = _SHORT_SUMMARY_PATTERN.match(raw_line.strip())
            if match is None:
                continue
            node_id = match.group("node")
            name = node_id.split("::")[-1]
            stage = match.group("stage").lower()
            cases.append(
                FailedCase(
                    node_id=node_id,
                    name=name,
                    stage=stage,
                    reason=(match.group("reason") or "").strip(),
                    trace_tail=PytestOutputParser._pick_trace_tail(sections, name),
                )
            )
        return cases

    @staticmethod
    def _collect_failure_sections(lines: list[str]) -> dict[str, list[str]]:
        """
        收集 FAILURES 段内各条用例小节的正文行

        参数:
            lines (list[str]): 输出的行列表

        返回:
            dict[str, list[str]]: 小节标题 → 该小节正文行列表；
                                  无失败段时返回空字典

        异常:
            无

        小节以 `___ title ___` 整行下划线开头，以下一个同形标题或任一
        `=` 段分隔线为界——故这里只认 `_`，让 `=` 分隔线自然收尾。
        """
        sections: dict[str, list[str]] = {}
        current_title: str | None = None
        for raw_line in lines:
            line = raw_line.strip()
            header_match = _SECTION_HEADER_PATTERN.match(line)
            if header_match is not None:
                current_title = header_match.group("title").strip()
                sections.setdefault(current_title, [])
                continue
            if line.startswith("="):
                current_title = None
                continue
            if current_title is not None:
                sections[current_title].append(line)
        return sections

    @staticmethod
    def _pick_trace_tail(sections: dict[str, list[str]], name: str) -> str:
        """
        从失败小节中取出诊断尾部

        参数:
            sections (dict[str, list[str]]): 小节标题 → 正文行
            name (str): 用例名（用于定位小节标题）

        返回:
            str: 诊断尾部文本；找不到对应小节时返回空串

        异常:
            无

        为什么匹配放宽到"包含": ERROR 小节标题是
        `ERROR at setup of test_x`，与用例名 `test_x` 只是包含关系，
        精确相等匹配会让 setup/teardown 类错误拿不到任何诊断信息——
        而那恰恰是最需要堆栈的一类失败。
        """
        if not name:
            return ""
        title = next(
            (key for key in sections if key == name),
            None,
        ) or next(
            (key for key in sections if name in key),
            None,
        )
        if title is None:
            return ""
        meaningful = [
            line
            for line in sections[title]
            if line and not line.startswith("_") and not line.startswith("=")
        ]
        if not meaningful:
            return ""
        tail = "\n".join(meaningful[-TRACE_TAIL_MAX_LINES:])
        if len(tail) > TRACE_TAIL_MAX_CHARS:
            return f"...[诊断尾部过长，已保留末尾 {TRACE_TAIL_MAX_CHARS} 字符]...\n{tail[-TRACE_TAIL_MAX_CHARS:]}"
        return tail


def parse_progress_percent(output: str) -> int | None:
    """
    提取输出中的进度百分比（供实时进度展示）

    参数:
        output (str): 待解析的输出文本（可为已完成执行的全量输出，
                      也可为运行过程中逐行读到的增量片段）

    返回:
        int | None: 输出中**最后一个**进度百分比（已钳制到 0-100）；
                     无进度行时返回 None

    异常:
        无

    取最后一个而非第一个: 进度行按时间递增，实时展示需要的是最新值。
    钳制上界是防被测代码自己打印形如 `[999%]` 的文本污染展示层。
    """
    if not output:
        return None
    percent: int | None = None
    for match in _PROGRESS_PATTERN.finditer(output):
        percent = int(match.group("percent"))
    if percent is None:
        return None
    return min(percent, PROGRESS_PERCENT_MAX)


def _parse_duration(raw: str) -> float:
    """
    把 pytest 汇总行里的耗时串解析为秒数

    支持两种形态:
        - 带单位秒: `0.12s` / `65.43s` → 直接去后缀转 float
        - 时分秒:   `1:05` → 65.0 / `0:01:05` → 65.0
                    （pytest 对超过一分钟的执行会渲染出这种括号内形态）

    参数:
        raw (str): 耗时字符串（已由 `_DURATION_PATTERN` 切出）

    返回:
        float: 秒数；形态不识别时返回 0.0

    异常:
        无
    """
    if raw.endswith("s"):
        try:
            return float(raw[:-1])
        except ValueError:
            return 0.0
    parts = raw.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return 0.0
    seconds = 0.0
    for number in numbers:
        seconds = seconds * 60 + number
    return seconds