"""
TestMatrix 阶段B-4: P2 前端竞态修复的回归测试

测试覆盖（本文件 3 组，对应本阶段修复的前端 P2）:
    A组 P2-34 api.js 业务码校验
        1. test_api_js_checks_business_code
        2. test_api_js_keeps_success_codes_backward_compatible
    B组 P2-32 / P2-35 cases.js 刷新补偿与失败态重置
        3. test_load_cases_registers_pending_refresh_when_locked
        4. test_load_cases_replays_pending_refresh_after_unlock
        5. test_load_cases_resets_pagination_on_error
    C组 P2-33 dashboard.js 图表并发保护
        6. test_dashboard_awaits_charts_before_releasing_lock
        7. test_dashboard_uses_all_settled_not_all

关于测试口径的说明:
    本项目无 JS 测试运行器（环境亦无 node），前端修复采用**源码语义断言**：
    锁定"必须存在什么"（关键调用/标志/关键字），不绑定具体排版与行号。
    这类断言能挡住"有人把修复删回去"，但不能替代真实浏览器行为验证——
    该验证列为 backlog。断言刻意避开易漂移的字符串字面量与缩进。

    行为变更同步更新 0 条既有断言（4 条前端修复均不改变任何既有断言口径）。
"""

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _extract_function_body(source: str, func_name: str) -> str:
    """
    截取指定 JS 函数的函数体文本（花括号配对，跳过字符串与注释）

    为什么需要（v3 修复 V2-P2-8）: cases.js 里有 5 处 `catch (error)`
    与 5 处 `finally`。直接对全文做 `catch...finally` 正则，取到的是
    **全文件第一个** catch 到最近 finally 之间的整段；一旦更靠前的函数
    新增一个 catch，捕获区会跨越函数边界膨胀，只要任意位置存在目标
    赋值就通过——"代码存在但逻辑仍错"会漏报。限定到函数体内匹配即可
    消除这种误报。

    花括号必须配对而非"到下一个 } 为止": 函数体内的对象字面量、
    模板字符串、嵌套块都会让朴素的截断提前结束。字符串/注释里的
    花括号与引号同样需要跳过，否则 `"}"` 这类字面量会破坏配对。

    参数:
        source (str): 整个 JS 文件文本
        func_name (str): 函数名（如 loadCases）

    返回:
        str: 函数体大括号内部的文本（不含最外层花括号）

    异常:
        AssertionError: 找不到该函数或花括号不配平时抛出
    """
    match = re.search(
        r"(?:async\s+)?function\s+" + re.escape(func_name) + r"\s*\([^)]*\)\s*\{",
        source,
    )
    assert match is not None, f"未找到函数定义: {func_name}"

    index = match.end() - 1  # 指向函数体的开括号
    depth = 0
    quote: str | None = None
    position = index
    while position < len(source):
        char = source[position]
        if quote is not None:
            # 字符串内部：处理转义，等闭合引号
            if char == "\\":
                position += 2
                continue
            if char == quote:
                quote = None
            position += 1
            continue
        if char in ("'", '"', "`"):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[index + 1: position]
        position += 1
    raise AssertionError(f"函数 {func_name} 的花括号不配对，无法确定函数体范围")
JS_DIR = PROJECT_ROOT / "src" / "web" / "static" / "js"


@pytest.fixture
def api_js() -> str:
    """
    读取 api.js 源码

    返回:
        str: api.js 全文
    """
    return (JS_DIR / "api.js").read_text(encoding="utf-8")


@pytest.fixture
def cases_js() -> str:
    """
    读取 cases.js 源码

    返回:
        str: cases.js 全文
    """
    return (JS_DIR / "cases.js").read_text(encoding="utf-8")


@pytest.fixture
def dashboard_js() -> str:
    """
    读取 dashboard.js 源码

    返回:
        str: dashboard.js 全文
    """
    return (JS_DIR / "dashboard.js").read_text(encoding="utf-8")


# ======================================================================
# A组: P2-34 api.js 业务码校验
# ======================================================================
class TestApiBusinessCodeGuard:
    """P2-34: HTTP 200 + 非成功业务码不得被静默解包成 data"""

    def test_api_js_checks_business_code(self, api_js: str) -> None:
        """
        api.js 必须校验统一响应体的业务码

        回归点: 修复前只看 response.ok。HTTP 200 + {code:500} 被静默解包成
        data=null，用例列表渲染成"暂无数据"（用户误判为库是空的），
        看板统计卡片则抛 Cannot read properties of null，把后端真实
        message 覆盖掉，排查方向被彻底带偏。
        """
        assert "payload.code" in api_js, (
            "api.js 必须读取并校验响应体业务码 payload.code"
        )
        # 必须同时排除两种成功约定（0 与 200），否则 200 成功会被误杀
        assert re.search(r"code\s*!==\s*0", api_js), (
            "业务码校验必须放行 code=0（统一响应封装用的是 0）"
        )
        assert re.search(r"code\s*!==\s*200", api_js), (
            "业务码校验必须放行 code=200（部分端点沿用 HTTP 语义）"
        )
        # 业务失败必须抛错，而不是返回一个"看起来正常"的值
        assert re.search(r"throw new Error\(", api_js), (
            "业务码非成功时必须抛 Error，不能静默返回 data"
        )

    def test_api_js_keeps_success_codes_backward_compatible(
        self, api_js: str
    ) -> None:
        """
        校验必须发生在 HTTP 状态判断之后、解包之前

        回归点: 校验插错位置会造成两类问题——插在 response.ok 判断之前会把
        正常错误响应的 message 换掉；插在解包之后就毫无意义。
        """
        ok_pos = api_js.index("if (!response.ok)")
        code_pos = api_js.index("payload.code")
        unpack_pos = api_js.index("return payload ? payload.data : null")

        assert ok_pos < code_pos < unpack_pos, (
            "业务码校验必须在 HTTP 状态判断之后、data 解包之前"
        )

    def test_api_js_tolerates_missing_code_field(self, api_js: str) -> None:
        """
        响应体无 code 字段时不得拦截（向后兼容）
        """
        assert "payload.code !== undefined" in api_js, (
            "code 字段缺失时应跳过校验，避免拦截后端简化契约的响应"
        )


# ======================================================================
# B组: P2-32 / P2-35 cases.js
# ======================================================================
class TestCasesListRefreshCompensation:
    """P2-32/P2-35: 刷新补偿 + 失败态分页计数重置"""

    def test_state_declares_pending_refresh(self, cases_js: str) -> None:
        """
        state 必须声明 pendingRefresh 标志
        """
        assert re.search(r"pendingRefresh\s*:\s*false", cases_js), (
            "state 必须声明 pendingRefresh 标志（默认 false）"
        )

    def test_load_cases_registers_pending_refresh_when_locked(
        self, cases_js: str
    ) -> None:
        """
        撞上互斥锁时必须登记待刷新而不是静默丢弃

        回归点: 修复前 `if (state.loading) return;` 直接吞掉本次调用。
        保存/删除/导入成功后的 await loadCases() 若恰有请求在途，
        刷新被丢弃——用户看到 toast 说"保存成功"、列表还是旧数据，
        只能手动刷新页面才发现。
        """
        match = re.search(
            r"if\s*\(state\.loading\)\s*\{(.*?)\}", cases_js, re.DOTALL
        )
        assert match is not None, "loadCases 必须保留 loading 互斥锁"
        locked_body = match.group(1)
        assert "state.pendingRefresh = true" in locked_body, (
            "撞上互斥锁时必须登记 pendingRefresh=true，"
            "不能静默 return 丢弃本次刷新请求"
        )
        assert "return" in locked_body, "登记后仍应立即返回，避免并发发出第二个请求"

    def test_load_cases_replays_pending_refresh_after_unlock(
        self, cases_js: str
    ) -> None:
        """
        锁释放后必须自动补刷登记的刷新

        回归点: 只登记不消费等于换个地方泄漏。补刷必须放在 finally 的锁
        释放之后（state.loading = false 之后），否则补刷会立刻再次撞锁。
        """
        assert re.search(
            r"state\.loading\s*=\s*false\s*;[\s\S]{0,400}?"
            r"if\s*\(state\.pendingRefresh\)\s*\{[\s\S]{0,200}?loadCases\(\)",
            cases_js,
        ), (
            "锁释放后必须检查 pendingRefresh 并补刷一次 loadCases()"
        )
        # 补刷前应清标志，避免异常路径下无限递归
        assert re.search(
            r"if\s*\(state\.pendingRefresh\)\s*\{\s*"
            r"state\.pendingRefresh\s*=\s*false\s*;",
            cases_js,
        ), "补刷前必须先清 pendingRefresh 标志，防止异常时无限递归补刷"

    def test_load_cases_resets_pagination_on_error(self, cases_js: str) -> None:
        """
        加载失败必须重置 total/totalPages

        回归点: 修复前失败分支只渲染错误条与空态行，页脚仍显示上一次的
        "共 137 条"+ 空表格，用户看到自相矛盾的画面，易误判为数据丢失
        并触发重复操作。

        **v3 修正匹配范围**（原版未按函数作用域锚定）: cases.js 里有 5 处
        `catch (error)` 与 5 处 `finally`，`re.search` 取的是**全文件第一个**
        catch 到最近 finally 之间的整段。一旦更靠前的函数新增一个
        catch，捕获区会跨越函数边界膨胀，只要任意位置存在
        `state.total = 0` 就通过——"代码存在但逻辑仍错"会漏报。
        现先切出 loadCases 函数体，再在切片内匹配 catch。
        """
        catch_body = _extract_function_body(cases_js, "loadCases")
        match = re.search(
            r"catch\s*\(error\)\s*\{([\s\S]*?)finally\s*\{", catch_body
        )
        assert match is not None, (
            "loadCases 必须有 catch 失败分支（匹配范围已限定在该函数体内）"
        )
        body = match.group(1)
        assert re.search(r"state\.total\s*=\s*0", body), (
            "失败分支必须重置 state.total，否则页脚残留旧总数"
        )
        assert re.search(r"state\.totalPages\s*=\s*0", body), (
            "失败分支必须重置 state.totalPages，否则分页条残留旧值"
        )


# ======================================================================
# C组: P2-33 dashboard.js 图表并发保护
# ======================================================================
class TestDashboardChartConcurrency:
    """P2-33: 图表请求必须在释放在途标志前完成"""

    def test_dashboard_awaits_charts_before_releasing_lock(
        self, dashboard_js: str
    ) -> None:
        """
        四个图表加载必须被 await，且在 finally 释放标志之前

        回归点: 修复前是 fire-and-forget（不 await），finally 立即执行、
        _dashboardLoading 释放，此时 4 个图表请求仍在途——用户再点刷新
        就是 8 个请求，且同一图表可能后发先至、用旧数据覆盖新数据，
        看板与实际统计不一致且无任何提示。
        """
        match = re.search(
            r"async function loadAllDashboardData\(\)\s*\{([\s\S]*?)\n\}",
            dashboard_js,
        )
        assert match is not None, "应能找到 loadAllDashboardData 函数体"
        body = match.group(1)

        for loader in (
            "loadTrendChart()",
            "loadModulePieChart()",
            "loadPriorityBarChart()",
            "loadFailedTopTable()",
        ):
            assert loader in body, f"{loader} 应在全量加载函数内被调用"

        # 图表调用必须位于 try 块内且被 await 包裹
        assert "await Promise.allSettled" in body or re.search(
            r"await\s+Promise\.all\(", body
        ), (
            "四个图表必须被 await（Promise.all/allSettled）包裹，"
            "不能是 fire-and-forget"
        )

        # await 必须出现在 finally 之前：调用顺序即语义顺序
        # 精确定位 finally 块（直接搜 "finally" 会误命中注释文本）
        await_pos = body.index("await Promise.allSettled")
        finally_pos = body.index("} finally {")
        assert await_pos < finally_pos, (
            "图表 await 必须在 finally 释放 _dashboardLoading 之前完成"
        )
        # 释放标志的动作必须落在 finally 块内，而不是 try 块末尾
        assert body.index("_dashboardLoading = false") > finally_pos, (
            "_dashboardLoading 的释放必须位于 finally 块内（异常路径也要释放）"
        )

    def test_dashboard_uses_all_settled_not_all(self, dashboard_js: str) -> None:
        """
        必须用 allSettled 而非 all

        回归点: 各图表加载器内部虽已独立 try/catch，但用 all 时任一
        reject 仍会让整体 reject 跳过 finally 之外的处理；allSettled
        语义上才真正保证"任一失败不阻断其余且本次刷新正常收尾"。
        """
        match = re.search(
            r"async function loadAllDashboardData\(\)\s*\{([\s\S]*?)\n\}",
            dashboard_js,
        )
        body = match.group(1) if match else ""
        assert "Promise.allSettled" in body, (
            "应使用 Promise.allSettled（任一图表失败不阻断其余与本次收尾）"
        )
        assert not re.search(r"await Promise\.all\(\s*\[", body), (
            "不应使用 Promise.all：任一 reject 会中断整体收尾"
        )

    def test_dashboard_keeps_independent_error_handling(
        self, dashboard_js: str
    ) -> None:
        """
        各图表加载器必须仍保留独立 try/catch（不回归）
        """
        for loader in (
            "loadTrendChart",
            "loadModulePieChart",
            "loadPriorityBarChart",
            "loadFailedTopTable",
        ):
            match = re.search(
                rf"(async )?function {loader}\(\)\s*\{{([\s\S]*?)\n\}}",
                dashboard_js,
            )
            assert match is not None, f"应能找到 {loader} 函数"
            assert "catch" in match.group(2), (
                f"{loader} 必须保留独立 try/catch 降级"
            )
