/* ==========================================================================
   TestMatrix 质量看板业务脚本（Day36 框架日）
   本日边界（不提前实现后续天数功能）：
     - loadSummary 仅证明 /api/reports/summary 链路真实可达：
       暂存数据到 window.__dashboardSummary（供 Day37 渲染卡片）、
       填充“最后更新”时间；四个卡片数值保持 “--”（Day37 才渲染）；
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
 * 加载全局执行汇总（Day36 只打通链路，不渲染卡片数值）
 *
 * @returns {Promise<void>} 无返回值；成功更新最后更新时间并暂存数据，
 *                          失败 toast 提示并切换 error 态
 */
async function loadSummary() {
    // 进入 loading 态并清理上一次的错误提示
    setSummaryLoading(true);
    setSummaryError(false);
    try {
        // 经统一封装请求（自动拼接 /api 前缀并解包 {code,message,data}）
        const data = await window.api.get("/reports/summary");

        // 暂存原始数据供 Day37 卡片渲染复用（本日不消费其字段渲染数字）
        window.__dashboardSummary = data;

        // 填充“最后更新”为当前本地时间（formatDate 由 main.js 提供）
        const lastUpdatedEl = document.getElementById("lastUpdated");
        if (lastUpdatedEl) {
            lastUpdatedEl.textContent = formatDate(new Date().toISOString());
        }
        // Day36 边界：四个卡片数值位保持 “--”，具体数值 Day37 渲染
    } catch (error) {
        // error 态：toast 明示失败原因，并展示页内重试提示条
        showToast("统计数据加载失败：" + error.message, "danger");
        setSummaryError(true);
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
    loadHealthStatus: loadHealthStatus,
    initDashboard: initDashboard,
};
