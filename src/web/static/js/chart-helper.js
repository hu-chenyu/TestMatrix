/* ==========================================================================
   TestMatrix ECharts 统一封装（Day36 框架，Day38/39 图表开发直接复用）
   职责：
     1. initChart：按 DOM id 初始化实例并登记，重复调用幂等不报警告；
     2. resizeAllCharts：窗口尺寸变化时统一 resize（带防抖）；
     3. renderChartEmpty：空数据时在容器居中绘制“暂无数据”占位；
     4. TM_CHART_COLORS：与 main.css 中 --tm-primary 对齐的统一调色板。
   依赖：echarts@5.5.1（由 dashboard.html 的 extra_js 经 CDN 先行加载）。
   ========================================================================== */

// 平台统一图表调色板：首色与主色一致，后续为语义色与中性互补色
const TM_CHART_COLORS = [
    "#0d6efd", // 主色蓝（主系列/通过率）
    "#198754", // 成功绿（通过）
    "#ffc107", // 警示黄（跳过/中性）
    "#dc3545", // 危险红（失败）
    "#6f42c1", // 紫色（模块分布补充色）
    "#0dcaf0", // 青色
    "#fd7e14", // 橙色
    "#20c997", // 蓝绿
];

// 实例登记表：domId -> echarts 实例，统一持有便于 resize/dispose
const _chartInstances = new Map();

/**
 * 按容器 id 初始化 ECharts 实例（幂等）
 *
 * @param {string} domId 图表容器 DOM 的 id
 * @returns {echarts.ECharts|null} 容器不存在返回 null；已初始化则返回既有实例，
 *                                 避免对同一 DOM 重复 init 产生控制台警告
 */
function initChart(domId) {
    // 取容器节点，不存在直接返回 null，调用方按空值跳过即可
    const el = document.getElementById(domId);
    if (!el) {
        return null;
    }
    // 已登记过同一容器：幂等返回既有实例（刷新场景安全）
    const existing = _chartInstances.get(domId);
    if (existing) {
        return existing;
    }
    // 初始化并登记
    const chart = echarts.init(el);
    _chartInstances.set(domId, chart);
    return chart;
}

/**
 * 统一调整全部已登记图表的尺寸
 *
 * @returns {void}
 */
function resizeAllCharts() {
    // 遍历登记表逐个 resize，适配窗口拖拽/侧边栏折叠后的容器变化
    _chartInstances.forEach(function (chart) {
        chart.resize();
    });
}

/**
 * 渲染图表空态：清空旧配置后居中绘制“暂无数据”文案
 *
 * @param {echarts.ECharts} chart initChart 返回的图表实例
 * @param {string} [message] 空态提示文案，缺省“暂无数据”
 * @returns {void}
 */
function renderChartEmpty(chart, message) {
    // 实例无效时直接跳过（如容器缺失 initChart 返回 null）
    if (!chart) {
        return;
    }
    // 缺省文案兜底，允许调用方自定义（如“暂无趋势数据”）
    const emptyMessage = message || "暂无数据";
    // 先清空历史 series/option，避免空态与旧系列叠加
    chart.clear();
    // graphic 文本元素绝对居中，灰色弱化，不绑定任何坐标轴
    chart.setOption({
        color: TM_CHART_COLORS,
        graphic: {
            type: "text",
            left: "center",
            top: "middle",
            style: {
                text: emptyMessage,
                fill: "#6c757d",
                fontSize: 14,
            },
        },
    });
}

// resize 防抖计时器引用（经典脚本闭包持有，无需模块系统）
let _resizeTimer = null;
// 防抖间隔（毫秒）：拖拽窗口期间只在停顿后触发一次 resize
const RESIZE_DEBOUNCE_MS = 200;

// 监听窗口尺寸变化：重置计时器，停顿 RESIZE_DEBOUNCE_MS 后才统一 resize
window.addEventListener("resize", function () {
    if (_resizeTimer !== null) {
        clearTimeout(_resizeTimer);
    }
    _resizeTimer = setTimeout(resizeAllCharts, RESIZE_DEBOUNCE_MS);
});

// 显式导出：各页面经典脚本通过 window.chartHelper 调用
window.chartHelper = {
    initChart: initChart,
    resizeAllCharts: resizeAllCharts,
    renderChartEmpty: renderChartEmpty,
    TM_CHART_COLORS: TM_CHART_COLORS,
};
