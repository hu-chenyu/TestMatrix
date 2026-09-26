/* ==========================================================================
   TestMatrix 质量看板业务脚本（Day36 框架 + Day37 统计卡片渲染）
   Day37 能力：
     - loadSummary 成功后串联 loadCaseTotal（用例总数走 cases 列表 total）
       与 renderStatCards（渲染 4 个卡片真实数值 + 语义着色）；
     - 用例总数接口失败不阻塞其他卡片（该卡片降级 “--” + toast 轻提示）。
   边界（不提前实现后续天数功能）：
     - 三个图表仅初始化并显示“暂无数据”空态（折线 Day38、饼/柱 Day39）；
     - 失败 Top 表保持 HTML 空态行（Day39 填充）。
   三态约定：loading（按钮 spinner + aria-busy）/ error（alert + toast）/
            empty（chart-helper 空态 + 表格“暂无数据”行）。
   说明：/health 不在 /api 前缀下，api.js 会自动拼 /api，
        故健康检查此处直接使用原生 fetch，不改动 api.js。
   ========================================================================== */

/**
 * 切换统计区域加载态（仅刷新按钮与无障碍标记，不提前渲染数字）
 *
 * @param {boolean} isLoading 是否处于加载中
 * @returns {void}
 */
function setSummaryLoading(isLoading) {
    // 刷新按钮：加载中禁用并显示 spinner、隐藏刷新图标，防重复点击
    const refreshBtn = document.getElementById("refreshBtn");
    const refreshSpinner = document.getElementById("refreshSpinner");
    const refreshIcon = document.getElementById("refreshIcon");
    if (refreshBtn) {
        refreshBtn.disabled = isLoading;
    }
    if (refreshSpinner) {
        refreshSpinner.classList.toggle("d-none", !isLoading);
    }
    if (refreshIcon) {
        refreshIcon.classList.toggle("d-none", isLoading);
    }
    // 卡片区无障碍加载标记（loading 态样式钩子）
    const cardsRow = document.getElementById("statsCardsRow");
    if (cardsRow) {
        cardsRow.setAttribute("aria-busy", isLoading ? "true" : "false");
    }
}

/**
 * 切换统计区域错误态（错误提示条显隐）
 *
 * @param {boolean} hasError 是否处于错误态
 * @returns {void}
 */
function setSummaryError(hasError) {
    // error 态提示条：默认 d-none，失败时展示
    const errorAlert = document.getElementById("statsErrorAlert");
    if (errorAlert) {
        errorAlert.classList.toggle("d-none", !hasError);
    }
}

/**
 * 加载用例总数（复用既有 cases 列表接口的 total，不新增后端端点）
 *
 * 设计：只请求第 1 页且 page_size=1，用分页响应的 total 字段取得
 *      用例资产总数，避免为一个数字拉全量列表。
 *
 * @returns {Promise<number|null>} 用例总数（非负整数）；接口失败时
 *          返回 null，由调用方让该卡片优雅降级为 “--”，不阻塞其他卡片
 */
async function loadCaseTotal() {
    try {
        // 经统一封装请求（自动拼接 /api 前缀并解包 {code,message,data}）
        const casesData = await window.api.get("/cases/?page=1&page_size=1");
        // total 为用例总数；契约缺失时按 null 降级，避免渲染 undefined
        return typeof casesData.total === "number" ? casesData.total : null;
    } catch (error) {
        // 轻提示但不抛错：用例总数是独立数据源，失败不影响另外三个卡片
        showToast("用例总数加载失败，已显示为占位：" + error.message, "danger");
        return null;
    }
}

/**
 * 设置单个统计卡片数值位的文本与语义颜色（统一收口 data-state 切换）
 *
 * @param {string} elementId 数值位 DOM id
 * @param {string|number} text 待显示内容（数字或格式化后的字符串）
 * @param {string} tone 语义色调：default（深色）/ success（绿）/ danger（红）
 * @returns {void}
 */
function setStatValue(elementId, text, tone) {
    // 取数值位节点，不存在直接跳过
    const el = document.getElementById(elementId);
    if (!el) {
        return;
    }
    // 渲染真实值：切换为 ready 态并移除占位灰
    el.setAttribute("data-state", "ready");
    el.classList.remove("text-muted");
    // 先清掉旧语义色，再按当前 tone 上色（刷新后颜色不会残留）
    el.classList.remove("text-success", "text-danger");
    if (tone === "success") {
        el.classList.add("text-success");
    } else if (tone === "danger") {
        el.classList.add("text-danger");
    }
    // default 不上语义色，保持 fw-bold 默认深色
    el.textContent = text;
}

/**
 * 渲染四个统计卡片（跨数据源整合：summary + cases total）
 *
 * 字段口径:
 *   - 用例总数 statTotal  = caseTotal（cases 列表 total，失败降级 “--”）
 *   - 通过率 statPassRate = overall_pass_rate 转百分比保留 1 位小数；
 *     total_executed=0（从未执行）显示 “--”，避免空库误显 0.0%
 *   - 执行批次 statBatches = total_batches
 *   - 失败数 statFailed   = failed + error（广义未通过口径），
 *     大于 0 红色、等于 0 绿色
 *
 * @param {object} summaryData /api/reports/summary 的 data
 * @param {number|null} caseTotal 用例总数，null 表示该数据源加载失败
 * @returns {void}
 */
function renderStatCards(summaryData, caseTotal) {
    // 1. 用例总数：cases total 失败时降级占位，默认深色
    setStatValue("statTotal", caseTotal === null ? "--" : caseTotal, "default");

    // 2. 通过率：无执行记录不算 0%，显示占位；有执行才转百分比
    let passRateText = "--";
    let passRateTone = "default";
    if (summaryData.total_executed > 0) {
        // 0~1 小数乘 100 后保留 1 位，如 0.7857 -> "78.6%"
        passRateText = (summaryData.overall_pass_rate * 100).toFixed(1) + "%";
        passRateTone = "success";
    }
    setStatValue("statPassRate", passRateText, passRateTone);

    // 3. 执行批次：直接取 total_batches，默认深色
    setStatValue("statBatches", summaryData.total_batches, "default");

    // 4. 失败数：failed + error 合计；0 全绿、大于 0 红色警示
    const failedTotal = summaryData.failed + summaryData.error;
    setStatValue(
        "statFailed",
        failedTotal,
        failedTotal > 0 ? "danger" : "success"
    );
}

/**
 * 加载全局执行汇总并渲染统计卡片（Day37：summary + cases total 双数据源）
 *
 * @returns {Promise<void>} 无返回值；成功渲染四卡片并更新最后更新时间，
 *                          summary 失败时 toast 提示并切换整体 error 态
 */
async function loadSummary() {
    // 进入 loading 态并清理上一次的错误提示
    setSummaryLoading(true);
    setSummaryError(false);
    try {
        // 主数据源：经统一封装请求（自动拼 /api 前缀并解包 {code,message,data}）
        const summary = await window.api.get("/reports/summary");

        // 暂存原始数据，供后续天数（卡片交互/图表）复用
        window.__dashboardSummary = summary;

        // 次要数据源：用例总数（独立 try/catch，失败返回 null 不阻塞主流程）
        const caseTotal = await loadCaseTotal();

        // 双数据源汇合后统一渲染四个卡片
        renderStatCards(summary, caseTotal);

        // 填充“最后更新”为当前本地时间（formatDate 由 main.js 提供）
        const lastUpdatedEl = document.getElementById("lastUpdated");
        if (lastUpdatedEl) {
            lastUpdatedEl.textContent = formatDate(new Date().toISOString());
        }
    } catch (error) {
        // summary 主数据源失败：toast 明示原因并展示页内重试提示条，整体 error 态
        showToast("统计数据加载失败：" + error.message, "danger");
        setSummaryError(true);
        // finally 会退出 loading 态，此处提前结束后续渲染
        return;
    } finally {
        // 无论成功失败都退出 loading 态
        setSummaryLoading(false);
    }
}

/**
 * 渲染导航栏健康状态（三态：正常/降级/未知）
 *
 * @param {string} state 健康状态标识：healthy（绿）/ degraded（红）/ unknown（灰）
 * @returns {void}
 */
function renderHealthState(state) {
    // 健康状态占位在基模板导航栏，仅看板页加载本脚本时更新
    const healthEl = document.getElementById("healthStatus");
    if (!healthEl) {
        return;
    }
    // 三态各自的图标、文案、语义颜色
    const stateMap = {
        healthy: { icon: "bi-check-circle-fill", text: "服务正常", cls: "text-success" },
        degraded: { icon: "bi-x-circle-fill", text: "服务降级", cls: "text-danger" },
        unknown: { icon: "bi-dash-circle-fill", text: "状态未知", cls: "text-secondary" },
    };
    // 非法 state 兜底为未知态
    const current = stateMap[state] || stateMap.unknown;
    // 重置颜色类并写入当前态颜色
    healthEl.classList.remove("text-success", "text-danger", "text-secondary");
    healthEl.classList.add(current.cls);
    // 整体替换图标与文案
    healthEl.innerHTML =
        '<i class="bi ' + current.icon + ' me-1"></i>' + current.text;
}

/**
 * 加载服务健康状态（原生 fetch /health，不走 api.js 的 /api 前缀）
 *
 * @returns {Promise<void>} 无返回值；HTTP 200 且 healthy 显示正常，
 *                          503 显示降级，网络异常/其他异常显示未知
 */
async function loadHealthStatus() {
    try {
        // 注意：/health 为根路径接口，不能用 window.api（会被拼成 /api/health）
        const response = await fetch("/health");
        if (response.ok) {
            // 200：解析统一响应体，按 data.status 严格判定
            const body = await response.json();
            const isHealthy = body && body.data && body.data.status === "healthy";
            renderHealthState(isHealthy ? "healthy" : "degraded");
        } else if (response.status === 503) {
            // 503：数据库探测失败，后端明确语义为服务降级
            renderHealthState("degraded");
        } else {
            // 其他非预期状态码统一归为未知
            renderHealthState("unknown");
        }
    } catch (networkError) {
        // 网络层异常（服务未启动/中断/CORS 等）：灰色未知态，不抛错不打扰
        renderHealthState("unknown");
    }
}

/**
 * 初始化看板三个图表（empty 态占位，证明 ECharts 基础设施可用）
 *
 * @returns {void}
 */
function initDashboardCharts() {
    // 三个容器 id 与 dashboard.html 逐一对应
    const chartIds = ["trendChart", "modulePieChart", "priorityBarChart"];
    chartIds.forEach(function (chartId) {
        // initChart 幂等；拿到实例后立即绘制空态
        const chart = window.chartHelper.initChart(chartId);
        window.chartHelper.renderChartEmpty(chart);
    });
}

/**
 * 看板页面初始化：健康检查 + 图表空态 + 刷新绑定 + 首次汇总加载
 *
 * @returns {void}
 */
function initDashboard() {
    // 1. 先刷新导航栏健康状态（独立链路，失败不影响统计）
    loadHealthStatus();

    // 2. 初始化三个图表并显示空态（Day38/39 在此基础上 setOption 真实数据）
    initDashboardCharts();

    // 3. 刷新按钮绑定重新加载汇总
    const refreshBtn = document.getElementById("refreshBtn");
    if (refreshBtn) {
        refreshBtn.addEventListener("click", loadSummary);
    }

    // 4. 首次加载汇总（loading/error/empty 三态由 loadSummary 内部处理）
    loadSummary();
}

// DOM 就绪后执行初始化（脚本位于 body 底部 extra_js，双保险无时序问题）
document.addEventListener("DOMContentLoaded", initDashboard);

// 显式导出，便于测试控制台联调与后续天数脚本复用
window.dashboardPage = {
    loadSummary: loadSummary,
    loadCaseTotal: loadCaseTotal,
    renderStatCards: renderStatCards,
    loadHealthStatus: loadHealthStatus,
    initDashboard: initDashboard,
};
