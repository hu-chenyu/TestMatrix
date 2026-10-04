/* ==========================================================================
   TestMatrix 质量看板业务脚本（Day36 框架 + Day37 统计卡片 + Day41 四图表）
   Day37 能力：
     - loadSummary 成功后串联 loadCaseTotal（用例总数走 cases 列表 total）
       与 renderStatCards（渲染 4 个卡片真实数值 + 语义着色）；
     - 用例总数接口失败不阻塞其他卡片（该卡片降级 “--” + toast 轻提示）。
   Day41 能力：
     - loadTrendChart/loadModulePieChart/loadPriorityBarChart 对接三个
       reports 接口渲染真实 ECharts（趋势面积折线/模块环形饼图/优先级
       堆叠柱），loadFailedTopTable 填充失败 Top 榜；
     - 四个加载器各自独立 try/catch + toast 降级，单个接口失败不阻塞
       其余三个；空数据统一走 chart-helper 空态/表格占位行；
     - loadAllDashboardData 串联卡片+四图表，刷新按钮与首屏共用。
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
        // 显式带 status=all（Day44 P3-18）：后端对 status 的缺省值是
        // "active"，修复前不传该参数拿到的是**启用用例数**而非总数，
        // 卡片却标着"用例总数"——存在 disabled 用例时两个数字对不上，
        // 标签与实际口径矛盾。后端已支持 status=all 全量口径。
        const casesData = await window.api.get(
            "/cases/?page=1&page_size=1&status=all"
        );
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
    // ECharts 资源存在性守卫（Day44 P2-19）
    // 为什么需要：initChart 内部直接调 echarts.init(el)，而 initDashboard
    // 是本页唯一的初始化入口、四个加载器（统计卡片 + 三图表 + Top 表）
    // 全部由它串联。ECharts 是 vendored 静态资源（echarts.min.js），一旦
    // 该文件缺失、404、被 CSP 拦下或部署漏拷，echarts 就是 undefined，
    // ReferenceError 会一路逃出 initDashboard() —— 结果是**整页数据都不
    // 加载**，卡片与 Top 表也跟着停在占位状态，排查成本远高于"图表没画出来"。
    // 这里提前判存在：资源缺失时只放弃图表，给出可读提示，其余内容照常渲染。
    if (typeof echarts === "undefined" || !window.chartHelper) {
        showToast("图表资源加载失败，看板图表不可用（统计数据不受影响）", "warning");
        renderChartResourceMissing();
        return;
    }
    // 三个容器 id 与 dashboard.html 逐一对应
    const chartIds = ["trendChart", "modulePieChart", "priorityBarChart"];
    chartIds.forEach(function (chartId) {
        // initChart 幂等；拿到实例后立即绘制空态（Day41 数据返回后被 setOption 替换）
        const chart = window.chartHelper.initChart(chartId);
        window.chartHelper.renderChartEmpty(chart);
    });
}

/**
 * ECharts 资源缺失时在三个图表容器内绘制占位文案（Day44 P2-19）
 *
 * 不走 chart-helper（它内部依赖 echarts），直接用 DOM API 写文本，
 * 与 renderChartEmpty 的视觉口径保持一致（灰色居中）。
 *
 * @returns {void}
 */
function renderChartResourceMissing() {
    const notice = "图表资源未加载";
    ["trendChart", "modulePieChart", "priorityBarChart"].forEach(function (chartId) {
        const el = document.getElementById(chartId);
        if (!el) {
            return;
        }
        el.textContent = notice;
        el.style.display = "flex";
        el.style.alignItems = "center";
        el.style.justifyContent = "center";
        el.style.color = "#6c757d";
        el.style.fontSize = "14px";
    });
}

/* ==========================================================================
   Day41 四个图表/表格加载器
   约定：每个加载器独立 try/catch——接口失败 toast 轻提示并保持空态，
   互不阻塞；空列表走 renderChartEmpty/表格占位行，不画空坐标轴。
   ========================================================================== */

/**
 * 把批次 created_at（ISO 8601，如 2026-10-03T06:03:53+00:00）格式化为本地时间 MM-DD HH:mm
 *
 * 为什么必须解析而不能用定长切片: 后端时间序列化统一带时区标识
 * （UTC 来源补 +00:00，本地来源换算成 UTC，见 time_utils 双来源规范），
 * 前端只有经 new Date() 解析、再用本地 getter 取值，才能得到正确的本地
 * 时间。原实现用定长切片（slice(5,10)/slice(11,16)）隐含"第 11-16 位
 * 就是本地时间"的假设，对带 +00:00 的串会**直接显示 UTC**，偏一个时区
 * 偏移（UTC+8 用户看到的趋势图横轴比实际早 8 小时）。
 *
 * @param {string} isoTime 后端 created_at ISO 字符串（带时区标识）
 * @returns {string} 横轴短标签 MM-DD HH:mm；异常/空值返回 "--"
 */
function formatBatchLabel(isoTime) {
    if (typeof isoTime !== "string" || isoTime.length < 16) {
        return "--";
    }
    // 带 +00:00 的 ISO 串按 UTC 解析，getMonth/getHours 等自动返回本地值；
    // 无时区标识的历史串按本地解析，与旧行为一致
    const date = new Date(isoTime);
    // 非法日期（脏数据）降级，避免渲染出 NaN-NaN
    if (isNaN(date.getTime())) {
        return "--";
    }
    // 逐段补零；输出格式与原实现完全一致（MM-DD HH:mm，共 11 字符）
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    const hours = String(date.getHours()).padStart(2, "0");
    const minutes = String(date.getMinutes()).padStart(2, "0");
    return month + "-" + day + " " + hours + ":" + minutes;
}

/**
 * 加载通过率趋势折线图（GET /reports/trend?limit=20，时间升序）
 *
 * @returns {Promise<void>} 无返回值；失败 toast 并保持空态
 */
async function loadTrendChart() {
    const chart = window.chartHelper.initChart("trendChart");
    if (!chart) {
        return;
    }
    try {
        const rows = await window.api.get("/reports/trend?limit=20");
        if (!Array.isArray(rows) || rows.length === 0) {
            markChartLoadState("trendChart", false);
            window.chartHelper.renderChartEmpty(chart, "暂无执行趋势");
            return;
        }
        // 时间升序（后端契约）直接作为横轴；tooltip 闭包持有整行数据
        const labels = rows.map(function (row) {
            return formatBatchLabel(row.created_at);
        });
        const rates = rows.map(function (row) {
            return (Number(row.pass_rate) || 0) * 100;
        });
        markChartLoadState("trendChart", false);
        chart.setOption({
            color: [TM_CHART_COLORS[0]],
            tooltip: {
                trigger: "axis",
                formatter: function (params) {
                    const idx = params[0].dataIndex;
                    const row = rows[idx];
                    const ratePct = (Number(row.pass_rate) * 100).toFixed(1);
                    // 同 P0 饼图 tooltip：自定义 formatter 返回值不转义，
                    // 一旦 execution_id 改为可由用户指定即为等价 XSS；
                    // 时间取横轴同源标签 labels[idx]（本地 MM-DD HH:mm）
                    return (
                        "<div style='max-width:260px;word-break:break-all'>" +
                        "批次：" + escapeHtml(row.execution_id) + "<br>" +
                        "时间：" + labels[idx] + "<br>" +
                        "通过：" + row.passed + " / 失败：" + row.failed +
                        " / 异常：" + row.error + "<br>" +
                        "通过率：<b>" + ratePct + "%</b></div>"
                    );
                },
            },
            grid: { left: 52, right: 48, top: 36, bottom: 48 },
            xAxis: {
                type: "category",
                data: labels,
                boundaryGap: false,
                axisLabel: { fontSize: 11 },
            },
            yAxis: {
                type: "value",
                min: 0,
                max: 100,
                axisLabel: { formatter: "{value}%" },
            },
            series: [
                {
                    name: "通过率",
                    type: "line",
                    smooth: true,
                    data: rates,
                    symbolSize: 7,
                    lineStyle: { width: 2 },
                    label: {
                        show: true,
                        position: "top",
                        formatter: function (p) {
                            return p.value.toFixed(1) + "%";
                        },
                    },
                    // 标签防重叠（Day44 走查发现）：近 20 个批次全部打标签时，
                    // 平台段（连续 100% / 33.3%）的标签会挤成
                    // "100100100100100.0%" 这种糊成一团的字符串，完全不可读。
                    // hideOverlap 让 ECharts 自动隐藏放不下的标签，密集段留白、
                    // 稀疏段照常显示；被隐藏处的精确值仍可由 tooltip 查看，
                    // 横轴刻度与 Y 轴百分比也始终在，信息不丢。
                    labelLayout: { hideOverlap: true },
                    // 面积渐变：主色蓝自上而下淡化，增强趋势可读性
                    areaStyle: {
                        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                            { offset: 0, color: "rgba(13,110,253,0.35)" },
                            { offset: 1, color: "rgba(13,110,253,0.03)" },
                        ]),
                    },
                },
            ],
            // notMerge=true：全量替换空态 option，避免空态 graphic"暂无数据"
            // 元素经默认合并模式残留叠加在真实图表上
        }, true);
    } catch (error) {
        markChartLoadState("trendChart", true);
        window.chartHelper.renderChartEmpty(
            chart, "加载失败，请点右上角「刷新」重试"
        );
        showToast("通过率趋势加载失败：" + error.message, "warning");
    }
}

/**
 * 标记图表容器的加载态（成功/失败/空），用于把"接口失败"与"确实无数据"区分开
 *
 * 为什么需要（Day43 收尾 D11）: 三个图表加载器的 catch 分支原本与空态
 * 渲染**完全相同**的文案，用户无法分辨"这个接口挂了"和"这个项目还没数据"。
 * 失败时既有 toast 提示（3 秒即逝），图表区本身没有任何持久痕迹。
 *
 * 本函数不碰 chart-helper（不在本次改动范围），只在容器元素上打
 * data-load-state 属性 + 一层淡红底色，样式与空态在视觉上可区分。
 * 成功/空态调用 markChartLoadState(id, false) 清除。
 *
 * @param {string} containerId 图表容器 DOM id
 * @param {boolean} isError true=加载失败（错误态）；false=成功或空态
 * @returns {void}
 */
function markChartLoadState(containerId, isError) {
    const container = document.getElementById(containerId);
    if (!container) {
        return;
    }
    if (isError) {
        container.setAttribute("data-load-state", "error");
        container.style.backgroundColor = "rgba(220, 53, 69, 0.06)";
        container.style.borderRadius = "0.25rem";
    } else {
        container.removeAttribute("data-load-state");
        container.style.backgroundColor = "";
        container.style.borderRadius = "";
    }
}

/**
 * 加载模块分布环形饼图（GET /reports/module-distribution）
 *
 * @returns {Promise<void>} 无返回值；失败 toast 并保持空态
 */
async function loadModulePieChart() {
    const chart = window.chartHelper.initChart("modulePieChart");
    if (!chart) {
        return;
    }
    try {
        const rows = await window.api.get("/reports/module-distribution");
        if (!Array.isArray(rows) || rows.length === 0) {
            markChartLoadState("modulePieChart", false);
            window.chartHelper.renderChartEmpty(chart, "暂无模块数据");
            return;
        }
        chart.setOption({
            color: TM_CHART_COLORS,
            tooltip: {
                trigger: "item",
                formatter: function (param) {
                    const row = param.data._row;
                    const ratePct = (Number(row.pass_rate) * 100).toFixed(1);
                    // ECharts 5 tooltip 默认 renderMode:"html"，自定义 formatter 的
                    // 返回值被当作 HTML 直接注入 tooltip DOM（ECharts 只对 {b}/{c}
                    // 模板占位符做 encodeHTML，不转义自定义返回值）。module 来自
                    // POST /api/cases/ 且后端仅校验长度不校验字符集，必须转义，
                    // 否则构成存储型 XSS。escapeHtml 由 main.js 全局提供。
                    return (
                        escapeHtml(row.module) + "<br>总数：" + row.total +
                        " / 通过：" + row.passed +
                        " / 失败：" + row.failed +
                        " / 异常：" + row.error +
                        "<br>通过率：<b>" + ratePct + "%</b>"
                    );
                },
            },
            legend: { bottom: 0, type: "scroll" },
            series: [
                {
                    name: "模块分布",
                    type: "pie",
                    radius: ["38%", "62%"],
                    center: ["50%", "46%"],
                    // 原始行挂到 _row（下划线字段不参与渲染），tooltip 取明细
                    data: rows.map(function (row) {
                        return { name: row.module, value: row.total, _row: row };
                    }),
                    label: {
                        formatter: "{b}\n{d}%",
                        fontSize: 11,
                    },
                },
            ],
            // notMerge=true：移除空态 graphic，避免"暂无数据"叠加
        }, true);
    } catch (error) {
        markChartLoadState("modulePieChart", true);
        window.chartHelper.renderChartEmpty(
            chart, "加载失败，请点右上角「刷新」重试"
        );
        showToast("模块分布加载失败：" + error.message, "warning");
    }
}

/**
 * 加载优先级分布堆叠柱状图（GET /reports/priority-distribution，Day41 新接口）
 *
 * @returns {Promise<void>} 无返回值；失败 toast 并保持空态
 */
async function loadPriorityBarChart() {
    const chart = window.chartHelper.initChart("priorityBarChart");
    if (!chart) {
        return;
    }
    try {
        const rows = await window.api.get("/reports/priority-distribution");
        if (!Array.isArray(rows) || rows.length === 0) {
            markChartLoadState("priorityBarChart", false);
            window.chartHelper.renderChartEmpty(chart, "暂无优先级数据");
            return;
        }
        // 堆叠分段固定顺序与语义色：通过绿/失败红/异常橙/跳过黄
        const segments = [
            { key: "passed", label: "通过", color: TM_CHART_COLORS[1] },
            { key: "failed", label: "失败", color: TM_CHART_COLORS[3] },
            { key: "error", label: "异常", color: TM_CHART_COLORS[6] },
            { key: "skipped", label: "跳过", color: TM_CHART_COLORS[2] },
        ];
        chart.setOption({
            tooltip: {
                trigger: "axis",
                axisPointer: { type: "shadow" },
            },
            legend: { top: 0, data: segments.map(function (s) { return s.label; }) },
            grid: { left: 48, right: 16, top: 36, bottom: 36 },
            xAxis: {
                type: "category",
                data: rows.map(function (row) { return row.priority; }),
            },
            yAxis: { type: "value", minInterval: 1 },
            series: segments.map(function (seg) {
                return {
                    name: seg.label,
                    type: "bar",
                    stack: "total",
                    itemStyle: { color: seg.color },
                    barMaxWidth: 48,
                    data: rows.map(function (row) { return row[seg.key]; }),
                };
            }),
            // notMerge=true：移除空态 graphic，避免"暂无数据"叠加
        }, true);
    } catch (error) {
        markChartLoadState("priorityBarChart", true);
        window.chartHelper.renderChartEmpty(
            chart, "加载失败，请点右上角「刷新」重试"
        );
        showToast("优先级分布加载失败：" + error.message, "warning");
    }
}

/**
 * 渲染失败用例 Top 榜表格行（DOM API 构造，textContent 天然防注入）
 *
 * @param {Array<object>} items /reports/failed-top 返回列表
 * @returns {void}
 */
function renderFailedTopRows(items) {
    const tbody = document.getElementById("failedTopTableBody");
    if (!tbody) {
        return;
    }
    // 每次渲染前清空（刷新场景移除上一批真实行/占位行）
    tbody.innerHTML = "";

    if (!Array.isArray(items) || items.length === 0) {
        // 空态：恢复 4 列占位行（列数与表头一致，module 列已按方案A移除）
        const emptyTr = document.createElement("tr");
        emptyTr.id = "failedTopEmpty";
        const emptyTd = document.createElement("td");
        emptyTd.colSpan = 4;
        emptyTd.className = "text-center text-muted py-4";
        emptyTd.textContent = "暂无失败记录";
        const emptyIcon = document.createElement("i");
        emptyIcon.className = "bi bi-inbox me-1";
        emptyTd.insertBefore(emptyIcon, emptyTd.firstChild);
        emptyTr.appendChild(emptyTd);
        tbody.appendChild(emptyTr);
        return;
    }

    items.forEach(function (item) {
        const tr = document.createElement("tr");
        // 失败堆栈作为整行悬浮提示，不占列宽
        if (item.last_error_message) {
            tr.title = item.last_error_message;
        }

        // 用例编号
        const tdId = document.createElement("td");
        tdId.className = "fw-semibold text-nowrap";
        tdId.textContent = item.case_id;
        tr.appendChild(tdId);

        // 用例名称
        const tdName = document.createElement("td");
        tdName.textContent = item.case_name || "";
        tr.appendChild(tdName);

        // 失败次数（右对齐 + 红色 badge，>0 恒为警示色）
        const tdCount = document.createElement("td");
        tdCount.className = "text-end";
        const badge = document.createElement("span");
        badge.className = "badge text-bg-danger";
        badge.textContent = String(item.fail_count);
        tdCount.appendChild(badge);
        tr.appendChild(tdCount);

        // 最近失败时间（formatDate 由 main.js 提供；异常值原样展示）
        const tdTime = document.createElement("td");
        tdTime.className = "text-nowrap";
        try {
            tdTime.textContent = item.last_failed_at
                ? formatDate(item.last_failed_at)
                : "--";
        } catch (formatError) {
            tdTime.textContent = item.last_failed_at || "--";
        }
        tr.appendChild(tdTime);

        tbody.appendChild(tr);
    });
}

/**
 * 加载失败用例 Top 榜（GET /reports/failed-top?limit=10）
 *
 * @returns {Promise<void>} 无返回值；失败 toast 并保持空态
 */
async function loadFailedTopTable() {
    try {
        const rows = await window.api.get("/reports/failed-top?limit=10");
        renderFailedTopRows(Array.isArray(rows) ? rows : []);
    } catch (error) {
        // 失败保持“暂无数据”占位行（renderFailedTopRows 已在首屏初始化）
        renderFailedTopRows([]);
        showToast("失败 Top 榜加载失败：" + error.message, "warning");
    }
}

/**
 * 加载看板全部数据（统计卡片 + 四图表/表格）
 *
 * 卡片 await 保证 loading/error 三态完整；四个图表加载器互不依赖，
 * 不 await 并行发出，各自内部独立降级。
 *
 * @returns {Promise<void>} 卡片加载完成即 resolve（图表失败不影响其返回）
 */
// 看板全量加载在途标志：防止刷新按钮连续点击导致并发请求
let _dashboardLoading = false;

/**
 * 看板全量数据加载：统计卡片 + 四个图表/表格（刷新按钮与首屏共用）
 *
 * 在途保护：加载期间禁用刷新按钮，避免连续点击触发并发请求；
 * 四个图表加载器各自独立 try/catch，任一失败不阻塞其余。
 * allSettled 而非 all：图表全部失败也应让本次刷新正常收尾。
 *
 * @returns {Promise<void>} 无返回值
 */
async function loadAllDashboardData() {
    // 在途保护：已在加载中则直接忽略本次点击
    if (_dashboardLoading) {
        return;
    }
    _dashboardLoading = true;
    // 刷新按钮禁用 + spinner（与 loadSummary 内部的 setSummaryLoading 互补，
    // 确保图表加载期间按钮保持禁用态）
    const refreshBtn = document.getElementById("refreshBtn");
    if (refreshBtn) {
        refreshBtn.disabled = true;
    }
    try {
        await loadSummary();
        // 四个图表请求必须在释放 _dashboardLoading **之前**完成。
        // 修复前是 fire-and-forget（不 await）：finally 立刻执行、标志释放，
        // 此时 4 个图表请求仍在途——用户再点一次刷新就是 8 个请求，
        // 且同一图表可能出现后发先至、用旧数据覆盖新数据，看板与实际
        // 统计不一致且无任何提示。
        // 各加载器内部已有独立 try/catch，这里用 allSettled 而不是 all：
        // 任一图表失败不应阻断其余，也不应让本次刷新整体 reject。
        await Promise.allSettled([
            loadTrendChart(),
            loadModulePieChart(),
            loadPriorityBarChart(),
            loadFailedTopTable(),
        ]);
    } finally {
        // 无论成功失败都释放在途标志并恢复按钮
        _dashboardLoading = false;
        if (refreshBtn) {
            refreshBtn.disabled = false;
        }
    }
}

/**
 * 看板页面初始化：健康检查 + 图表空态 + 刷新绑定 + 首屏全量加载
 *
 * @returns {void}
 */
function initDashboard() {
    // 1. 先刷新导航栏健康状态（独立链路，失败不影响统计）
    loadHealthStatus();

    // 2. 初始化三个图表并显示空态（数据返回后 setOption 替换空态）
    initDashboardCharts();

    // 3. 失败 Top 表首屏占位行已在 HTML 中（id=failedTopEmpty），加载后替换

    // 4. 刷新按钮绑定：统计卡片 + 四个图表/表格统一刷新。
    //    仅手动点击给"已刷新"成功 toast（首屏加载不提示，避免进页即弹）；
    //    各加载器失败已有独立 toast/错误条，这里不重复失败提示。
    const refreshBtn = document.getElementById("refreshBtn");
    if (refreshBtn) {
        refreshBtn.addEventListener("click", async function () {
            await loadAllDashboardData();
            showToast("看板数据已刷新");
        });
    }

    // 4.5 页面卸载时销毁全部 ECharts 实例（D16：释放 canvas 与内部监听，
    //     防御未来多页/路由切换场景的实例堆积；单页跳转浏览器本会回收，
    //     此处显式处置形成统一约定）
    window.addEventListener("beforeunload", function () {
        window.chartHelper.disposeAllCharts();
    });

    // 5. 首屏全量加载（loading/error/empty 三态由各加载器内部处理）
    loadAllDashboardData();
}

// DOM 就绪后执行初始化（脚本位于 body 底部 extra_js，双保险无时序问题）
document.addEventListener("DOMContentLoaded", initDashboard);

// 显式导出，便于测试控制台联调与后续天数脚本复用
window.dashboardPage = {
    loadSummary: loadSummary,
    loadAllDashboardData: loadAllDashboardData,
    loadTrendChart: loadTrendChart,
    loadModulePieChart: loadModulePieChart,
    loadPriorityBarChart: loadPriorityBarChart,
    loadFailedTopTable: loadFailedTopTable,
    renderFailedTopRows: renderFailedTopRows,
    loadCaseTotal: loadCaseTotal,
    renderStatCards: renderStatCards,
    loadHealthStatus: loadHealthStatus,
    initDashboard: initDashboard,
};
