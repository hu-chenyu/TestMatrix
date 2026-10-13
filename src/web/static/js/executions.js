/* ==========================================================================
   TestMatrix 执行记录页业务脚本（Day43）
   四大能力：
     1. 批次列表：GET /executions/ 分页加载（仅已完成批次，后端口径），
        loading/empty/error 三态 + 页码分页 + 每页条数切换；
     2. 批次详情：GET /executions/<id>，顶部汇总卡片 + 单用例明细
        （结果徽章 / 耗时 ms / 失败堆栈等宽可滚动区，失败行高亮）；
     3. 执行触发：POST /executions/trigger（筛选项留空=不按该维度过滤，
        注意 case_type 恒定下发，单次触发只覆盖一种用例类型），
        202 受理区展示 execution_id/pending/total_cases + 实时日志入口，
        提交互斥锁防重复；
     4. SSE 实时日志：EventSource 订阅 /executions/<id>/events，
        batch_start/case_finished/batch_finished/batch_failed 逐帧渲染，
        心跳注释帧浏览器自动忽略，终态帧后主动 close() 并给报告入口；
        终态前的单帧快照（载荷带 status）渲染后主动停订阅，避免
        EventSource 约 3s 无限重连导致日志区无界增长。
   健壮性：SSE 帧载荷统一经 parseSsePayload 兜底（解析失败只跳过该帧）；
        列表/详情/日志三处异步链路各带互斥锁或请求序号（慢响应不得覆盖
        新请求）；列表在途期间改页大小登记 pendingPageSize 由锁释放补刷。
   范式：与 cases.js（Day42/v5 收敛后）一致——const state 单页状态、
        window.api 统一请求、escapeHtml 单一来源（main.js 挂载）、
        锁防并发、Bootstrap5 模态框、事件委托。
   XSS：日志帧与表格全部走 textContent/escapeHtml，error_message 不例外。
   ========================================================================== */

/* ==========================================================================
   常量
   ========================================================================== */

/**
 * 批次状态/详情接口 404 的后端业务文案（executions.py 两处 NotFoundError
 * 的固定 message）。用于把“历史批次无批次行”与 500/网络类失败区分开：
 * 精确匹配而非子串包含，理由见 openLogView 内的注释。
 */
const EXECUTION_NOT_FOUND_MESSAGE = "执行批次不存在";

/**
 * 批次状态枚举（后端 test_execution_batches.status 的取值域）
 *
 * 具名化而非散落字面量：状态串在本文件出现在"列表状态列""SSE 终态判定"
 * "报告入口分支"三处，任一处写错大小写都只是静默降级而非报错。
 */
const BATCH_STATUS = {
    FINISHED: "finished",
    FAILED: "failed",
    RUNNING: "running",
    PENDING: "pending",
};

/**
 * 日志区最大保留行数（Day44 D9 防御性上界）
 *
 * 正常批次日志行数有天然上界（=用例数×2+汇总，百级以内）且终态主动
 * 关流；该常量只防未来新路径（如非终态正常断流后持续重连）导致日志
 * DOM 无界增长拖垮页面。超限从头部删最旧行，保留尾部最新内容。
 */
const MAX_LOG_LINES = 2000;

/**
 * 原始输出区最大保留行数（Day53）
 *
 * 真实执行器的原始输出行数**没有天然上界**（一条用例的 pytest 输出可达
 * 数百行，几百条用例就是十万行量级），服务端输出通道本身也已截断，
 * 但前端 DOM 仍需独立上界：一次长批次滚下来，十万个 div 会让页面卡死。
 * 与事件日志区同一处置口径——超限从头部删最旧行并提示一次。
 */
const MAX_OUTPUT_LINES = 2000;

/**
 * 执行器模式的中文标签（Day53 双模式切换）
 *
 * 单一事实来源：下拉文案、切换成功提示、页面当前模式提示三处共用，
 * 避免"下拉写模拟执行、提示写 simulated"这类口径漂移。
 */
const EXECUTOR_MODE_LABELS = {
    simulated: "模拟执行",
    pytest: "真实执行",
};

/**
 * 执行器模式接口路径根（Day53）
 */
const EXECUTOR_MODE_API = "/executor/mode";

/**
 * 优先级合法取值（与后端 executions.py 的 VALID_CASE_PRIORITIES 一致）
 *
 * 前端持有一份用于"提交前即时校验"：后端对非法取值回 400
 * 「priority取值非法」，用户要走一次"提交→报错→改"的往返才发现
 * 自己把 P0 写成了 P9。两侧取值域同源，改一处必须同步另一处。
 */
const VALID_PRIORITIES = ["P0", "P1", "P2", "P3"];

/**
 * 执行环境字段长度上限（与库层 environment 列 String(16)、
 * executions.html 的 maxlength 同值）
 *
 * Day54-fix P1-①: 此前这里写 32 而库层只有 String(16)，17-32 字符
 * 能连过前端与路由两道闸——MySQL 严格模式下 INSERT 抛 DataError 变成
 * 500，SQLite 不实现 VARCHAR 长度约束则静默放行。**三层口径必须同值**。
 */
const MAX_ENVIRONMENT_LENGTH = 16;

/* ==========================================================================
   页面级状态
   ========================================================================== */
const state = {
    currentPage: 1,       // 列表当前页码（从 1 起）
    pageSize: 20,         // 列表每页条数（10/20/50/100）
    total: 0,             // 已完成批次总数（后端分页响应）
    totalPages: 0,        // 总页数
    listLoading: false,   // 列表加载互斥锁（防翻页/切页大小并发重复请求）
    triggerLoading: false,// 触发提交互斥锁（防重复提交同一批次）
    detailExecutionId: null, // 详情模态框当前展示的批次号（供“实时日志”回放）
    currentLogId: null,   // 日志模态框当前订阅的批次号
    eventSource: null,    // 当前 EventSource 实例（同时只允许一个订阅）
    terminalReceived: false, // 本订阅是否已收到终态帧（防重复 close/渲染）
    pendingPageSize: null, // 列表在途期间被改动的页大小，锁释放后自动补刷
    pendingPage: null,   // 列表在途期间被点击的目标页码，锁释放后自动补刷
    detailRequestSeq: 0,  // 详情请求序号，防慢响应覆盖后发起的响应
    logRequestSeq: 0,     // 日志探活请求序号，语义同上
    // --- Day53 原始输出流 ---
    outputEventSource: null,  // 原始输出流 EventSource（与事件流分开，职责正交）
    outputSeq: 0,         // 已渲染的输出行数（上界裁剪与状态提示共用）
    outputEnded: false,   // 本订阅是否已收到 output_end（防重复收流/重复提示）
    outputCapNoticeShown: false, // 输出区是否已提示过行数裁剪（每轮订阅一次）
    executorMode: null,   // 当前生效的执行器模式（服务端为权威，本值仅缓存展示）
};

/* ==========================================================================
   DOM 引用集中获取（模板静态元素，脚本在 body 底部 defer 加载）
   ========================================================================== */
const els = {
    listErrorAlert: document.getElementById("listErrorAlert"),
    listErrorText: document.getElementById("listErrorText"),
    listErrorRetryBtn: document.getElementById("listErrorRetryBtn"),
    tableBody: document.getElementById("executionsTableBody"),
    totalCount: document.getElementById("totalCount"),
    pageSizeSelect: document.getElementById("pageSizeSelect"),
    pagination: document.getElementById("pagination"),
    triggerBtn: document.getElementById("triggerBtn"),

    detailModalEl: document.getElementById("detailModal"),
    detailModalTitle: document.getElementById("detailModalTitle"),
    detailMeta: document.getElementById("detailMeta"),
    detailItemsBody: document.getElementById("detailItemsBody"),
    detailReplayLogBtn: document.getElementById("detailReplayLogBtn"),
    sumTotal: document.getElementById("sumTotal"),
    sumPassed: document.getElementById("sumPassed"),
    sumFailed: document.getElementById("sumFailed"),
    sumError: document.getElementById("sumError"),
    sumSkipped: document.getElementById("sumSkipped"),
    sumPassRate: document.getElementById("sumPassRate"),

    triggerModalEl: document.getElementById("triggerModal"),
    triggerForm: document.getElementById("triggerForm"),
    tModule: document.getElementById("tModule"),
    tPriority: document.getElementById("tPriority"),
    tPriorityError: document.getElementById("tPriorityError"),
    tTags: document.getElementById("tTags"),
    tCaseType: document.getElementById("tCaseType"),
    tExecutor: document.getElementById("tExecutor"),
    tEnvironment: document.getElementById("tEnvironment"),
    tEnvironmentError: document.getElementById("tEnvironmentError"),
    tRemark: document.getElementById("tRemark"),
    triggerSubmitBtn: document.getElementById("triggerSubmitBtn"),
    triggerSubmitSpinner: document.getElementById("triggerSubmitSpinner"),
    triggerErrorAlert: document.getElementById("triggerErrorAlert"),
    triggerRetryBtn: document.getElementById("triggerRetryBtn"),
    triggerResultAlert: document.getElementById("triggerResultAlert"),
    triggerResultText: document.getElementById("triggerResultText"),
    triggerViewLogBtn: document.getElementById("triggerViewLogBtn"),

    logModalEl: document.getElementById("logModal"),
    logConnectionBadge: document.getElementById("logConnectionBadge"),
    logExecutionMeta: document.getElementById("logExecutionMeta"),
    logContainer: document.getElementById("logContainer"),
    logViewReportBtn: document.getElementById("logViewReportBtn"),

    // Day53: 原始输出区 + 执行器模式切换控件
    logOutputContainer: document.getElementById("logOutputContainer"),
    logOutputStatus: document.getElementById("logOutputStatus"),
    executorModeSelect: document.getElementById("executorModeSelect"),
    executorModeHint: document.getElementById("executorModeHint"),
};

// Bootstrap Modal 单例（懒获取，弹窗复用同一实例）
let detailModal = null;
let triggerModal = null;
let logModal = null;

/* ==========================================================================
   工具函数
   ========================================================================== */

/**
 * HTML 转义（单一来源，v5 起统一由 main.js 挂载到 window）
 *
 * 本文件不持有转义实现：base.html 先于本脚本加载 main.js，
 * 此处直接引用 window.escapeHtml，避免双份实现漂移。
 *
 * 别名说明（不直接命名 escapeHtml）: main.js 以顶层 function
 * 声明 escapeHtml（全局 var 绑定），本文件若再以 const/function
 * 同名声明会触发全局重复声明 SyntaxError，导致整脚本失效；
 * 故以不同名常量别名引用同一函数。
 *
 * @param {*} value 任意输入（非字符串按空串处理）
 * @returns {string} 转义后的安全字符串
 */
const escHtml = window.escapeHtml;

/**
 * 批次状态徽章 HTML
 *
 * 实现已下沉到 main.js 的 window.statusBadge（Day44 收尾）。该全局实现
 * 同时覆盖用例启停域（active/disabled）与批次执行域（finished/failed/
 * running/pending），两个域的既有文案均保持不变。此处仅做异名引用。
 */
const statusBadge = window.statusBadge;

/**
 * 单用例执行结果徽章 HTML
 *
 * 与状态徽章不同域（result 而非 status），故仍在本文件持有；但补上
 * 自有属性判定——直接 colorMap[result] 会命中 Object.prototype 上的键
 * （constructor/toString 等）并把函数对象拼进 class 属性。
 *
 * @param {string} result passed/failed/error/skipped
 * @returns {string} Bootstrap badge HTML 片段；未知值降级灰色
 */
function resultBadge(result) {
    // 通过绿 / 失败红 / 异常橙 / 跳过灰
    const colorMap = {
        passed: "text-bg-success",
        failed: "text-bg-danger",
        error: "bg-warning text-dark",
        skipped: "text-bg-secondary",
    };
    const cls = Object.prototype.hasOwnProperty.call(colorMap, result)
        ? colorMap[result]
        : "text-bg-secondary";
    return '<span class="badge ' + cls + '">' + escHtml(result) + "</span>";
}

/**
 * 通过率百分比文本（0~1 小数转百分比，非法值降级为横线）
 *
 * 为什么要 NaN 防御: 字段缺失时 Number(x) 得 NaN，直接 (NaN*100).toFixed(1)
 * 会渲染出字面量 "NaN%"。三个来源（列表汇总/详情汇总/SSE 终态帧）统一
 * 走本函数，保证异常值呈现一致。
 *
 * @param {number|string|null|undefined} passRate 0~1 的通过率
 * @returns {string} 百分比文本（如 "87.5%"）；非有限值返回 "-"
 */
function formatPassRate(passRate) {
    const value = Number(passRate);
    if (!isFinite(value)) {
        return "-";
    }
    return (value * 100).toFixed(1) + "%";
}

/**
 * 秒级耗时转毫秒展示文本
 *
 * 后端 duration 字段单位为秒（浮点，如 0.5），页面按毫秒展示。
 *
 * @param {number|null|undefined} seconds 秒
 * @returns {string} 毫秒文本（如 "500 ms"）；空值显示 "-"
 */
function formatDurationMs(seconds) {
    if (seconds === null || seconds === undefined || seconds === "") {
        return "-";
    }
    const ms = Number(seconds) * 1000;
    if (!isFinite(ms)) {
        return "-";
    }
    // 毫秒取整（模拟执行器粒度为 10ms 级，小数毫秒无业务意义）
    return Math.round(ms) + " ms";
}

/**
 * 逗号分隔的筛选值拆分为数组（触发表单 priority/tags 共用）
 *
 * 为什么必须拆：后端 _normalize_values 对 str 入参**不按逗号拆分**，
 * 整个串被当作单个维度值（"P0,P1" → ["P0,P1"]），再进 priority 的
 * in_ 过滤必然命中 0 条 → 触发接口 400「无符合条件的用例可执行」。
 * 后端多值契约是 str | list[str]，多值场景只能下发数组。
 *
 * @param {string} rawValue 原始文本（可含中英文逗号与首尾空白）
 * @returns {Array<string>} 逐段 trim 并剔除空项后的筛选值数组
 */
function splitFilterValues(rawValue) {
    return String(rawValue)
        .split(/[,，]/)
        .map(function (item) {
            return item.trim();
        })
        .filter(Boolean);
}

/**
 * 计算分页页码序列
 *
 * 实现已下沉到 main.js 的 window.buildPageSequence（Day44 收尾，本文件与
 * cases.js 原本逐字重复的两份实现合并为一处，三个页面分页交互口径统一）。
 * 此处仅做异名引用，保留 window.executionsPage 的同名导出。
 */
const buildPageSequence = window.buildPageSequence;

/* ==========================================================================
   API 语义封装（均走 window.api，自动拼 /api 前缀并解包统一响应体）
   ========================================================================== */

/**
 * 查询已完成批次分页列表
 *
 * @param {number} page 页码（从 1 起）
 * @param {number} pageSize 每页条数
 * @returns {Promise<object>} 分页结构 {items,total,page,page_size,total_pages}
 */
function listExecutions(page, pageSize) {
    return window.api.get(
        "/executions/?page=" + page + "&page_size=" + pageSize
    );
}

/**
 * 查询批次详情（汇总 + 单用例明细）
 *
 * @param {string} executionId 批次号
 * @returns {Promise<object>} {summary, items}
 */
function getExecutionDetail(executionId) {
    return window.api.get("/executions/" + encodeURIComponent(executionId));
}

/**
 * 查询批次实时状态（pending/running/finished/failed）
 *
 * @param {string} executionId 批次号
 * @returns {Promise<object>} 批次状态字典
 */
function getExecutionStatus(executionId) {
    return window.api.get(
        "/executions/" + encodeURIComponent(executionId) + "/status"
    );
}

/**
 * 触发用例异步执行（走 window.api 统一封装）
 *
 * 本函数此前直连 fetch 显式判 202，原因是当时的 api.js 业务码白名单只
 * 放行 0/200，trigger 的 202 会被误判为业务失败。Day44 已把 api.js 的
 * 判据改为区间（code >= 0 && code < 400，2xx 全放行，见 6.36 决策 2），
 * 绕封装的理由已消失，本函数随之收编回统一封装——全项目不再有第二套
 * 请求代码路径。202 受理体由 api.js 统一解包返回 data。
 *
 * @param {object} formData 触发表单（字段全可选，空对象=选中全部 api 用例）
 * @returns {Promise<object>} 202 受理结果
 *          {execution_id, status:"pending", total_cases}
 * @throws {Error} 网络失败或 HTTP 非 2xx（message 由 api.js 取后端业务文案）
 */
async function triggerExecution(formData) {
    return window.api.post("/executions/trigger", formData);
}

/* ==========================================================================
   列表加载与渲染（loading/empty/error 三态）
   ========================================================================== */

/**
 * 切换列表错误提示条显隐（含配套重试按钮）
 *
 * 文案与显隐统一走 main.js 的 window.showError（Day54 收敛，三页面同一实现）；
 * 重试按钮与提示条同生命周期——只在错误态出现，避免给用户一个"点了只是
 * 重放正常请求"的假入口。
 *
 * @param {boolean} show 是否显示
 * @param {string} [message] 错误文案
 * @returns {void}
 */
function setListError(show, message) {
    window.showError(els.listErrorAlert, show ? message : "");
    window.showRetry(els.listErrorRetryBtn, Boolean(show));
}

/**
 * 渲染 loading 态行
 * @returns {void}
 */
function renderLoadingRow() {
    els.tableBody.innerHTML =
        '<tr><td colspan="9" class="text-center text-muted py-4">' +
        '<span class="spinner-border spinner-border-sm me-1" ' +
        'aria-hidden="true"></span>加载中...</td></tr>';
}

/**
 * 渲染空态行
 * @returns {void}
 */
function renderEmptyRow() {
    els.tableBody.innerHTML =
        '<tr><td colspan="9" class="text-center text-muted py-4">' +
        '<i class="bi bi-inbox me-1"></i>暂无执行记录，' +
        "点击右上角「触发新执行」开始</td></tr>";
}

/**
 * 渲染列表错误态行（与空态刻意区分，Day54）
 *
 * 为什么必须分开：接口失败时原先渲染的是「暂无执行记录，点击右上角触发
 * 新执行」——这会直接把用户推去做一次本不该做的触发，而真实原因是列表
 * 请求根本没成功。错误行明确指路到提示条里的重试按钮，并用 text-danger
 * 与空态的 text-muted 在视觉上区分。
 *
 * @returns {void}
 */
function renderErrorRow() {
    els.tableBody.innerHTML =
        '<tr><td colspan="9" class="text-center text-danger py-4">' +
        '<i class="bi bi-exclamation-triangle me-1"></i>' +
        "执行批次加载失败，请点击上方提示条中的「重试」</td></tr>";
}

/**
 * 渲染批次数据行（所有后端文本经 escapeHtml 后再拼接）
 *
 * 列表口径只有已完成批次，状态列固定“已完成”徽章；通过率为
 * 0~1 小数转百分比保留 1 位。操作按钮 data-action 供事件委托分流。
 *
 * @param {Array<object>} items 批次汇总列表
 * @returns {void}
 */
function renderExecutionsTable(items) {
    els.tableBody.innerHTML = items
        .map(function (item) {
            // data-execution-id 同样转义，防编号中的引号截断属性
            const safeId = escHtml(item.execution_id);
            // 通过率 0~1 小数转百分比保留 1 位（统一走 formatPassRate 做 NaN 防御）
            const rateText = formatPassRate(item.pass_rate);
            // 状态列取后端返回的真实状态（Day44 收尾）：原先硬编码
            // "finished"，但核心层写汇总行与更新批次行两步非原子，会留下
            // "有汇总行但批次行非终态"的孤儿批次被误显示为已完成。字段
            // 缺失时按列表口径的既有不变量兜底 finished。
            const statusText = statusBadge(
                item.status || BATCH_STATUS.FINISHED
            );
            return (
                "<tr>" +
                '<td class="fw-semibold text-nowrap">' + safeId + "</td>" +
                "<td>" + statusText + "</td>" +
                '<td class="text-end">' + Number(item.total_cases || 0) + "</td>" +
                '<td class="text-end text-success">' +
                    Number(item.passed || 0) + "</td>" +
                '<td class="text-end text-danger">' +
                    Number(item.failed || 0) + "</td>" +
                '<td class="text-end">' + Number(item.error || 0) + "</td>" +
                '<td class="text-end fw-semibold">' + rateText + "</td>" +
                '<td class="text-nowrap">' +
                    window.formatDate(item.created_at) + "</td>" +
                '<td class="text-end text-nowrap">' +
                '<button type="button" class="btn btn-outline-primary btn-sm me-1" ' +
                'data-action="detail" data-execution-id="' + safeId + '">' +
                "详情</button>" +
                '<button type="button" class="btn btn-outline-secondary btn-sm" ' +
                // 历史批次（Day24 建表前、无 test_execution_batches 行）的
                // "日志"按钮**无法在列表层前置置灰**：列表接口只提供
                // defect_statistics 汇总口径，Day44 收尾补的 status 字段对
                // 这类批次兜底就是 "finished"，与正常已完成批次不可区分。
                // 因此保持"点击后 status 探活 + 失败 toast"方案（探活在
                // 建流前，不会触发 EventSource 无限重连）。
                'data-action="log" data-execution-id="' + safeId + '">' +
                '<i class="bi bi-broadcast me-1"></i>日志</button>' +
                "</td>" +
                "</tr>"
            );
        })
        .join("");
}

/**
 * 渲染分页控件 + 总条数（加载期禁用翻页，与列表锁双保险）
 * @returns {void}
 */
function renderPagination() {
    els.totalCount.textContent = String(state.total);

    if (state.totalPages <= 0) {
        els.pagination.innerHTML = "";
        return;
    }

    const prevDisabled = state.currentPage <= 1 || state.listLoading;
    const nextDisabled =
        state.currentPage >= state.totalPages || state.listLoading;

    let html =
        '<li class="page-item' + (prevDisabled ? " disabled" : "") + '">' +
        '<a class="page-link" href="#" data-action="prev">上一页</a></li>';

    buildPageSequence(state.currentPage, state.totalPages)
        .forEach(function (p) {
            if (p === "...") {
                html +=
                    '<li class="page-item disabled">' +
                    '<span class="page-link">…</span></li>';
                return;
            }
            const activeCls = p === state.currentPage ? " active" : "";
            html +=
                '<li class="page-item' + activeCls + '">' +
                '<a class="page-link" href="#" data-action="page" ' +
                'data-page="' + p + '">' + p + "</a></li>";
        });

    html +=
        '<li class="page-item' + (nextDisabled ? " disabled" : "") + '">' +
        '<a class="page-link" href="#" data-action="next">下一页</a></li>';

    els.pagination.innerHTML = html;
}

/**
 * 加载批次列表（列表锁互斥 + 三态渲染）
 * @returns {Promise<void>} 不抛出；失败显示错误条与空态
 */
async function loadExecutions() {
    // 防并发：翻页/切页大小快速连点只放行一个在途请求
    if (state.listLoading) {
        return;
    }
    state.listLoading = true;
    setListError(false);
    renderLoadingRow();
    renderPagination();

    try {
        const data = await listExecutions(state.currentPage, state.pageSize);
        const items = Array.isArray(data.items) ? data.items : [];

        // 越界空页（如最后一条被删）自动回退一页重查，仅回退一次
        if (items.length === 0 && state.currentPage > 1) {
            state.currentPage = Math.max(1, state.currentPage - 1);
            state.listLoading = false;
            await loadExecutions();
            return;
        }

        state.total = Number(data.total) || 0;
        state.totalPages = Number(data.total_pages) || 0;

        if (items.length === 0) {
            renderEmptyRow();
        } else {
            renderExecutionsTable(items);
        }
        renderPagination();
    } catch (error) {
        // 错误态：页内提示条 + 错误态行（与空态区分），不白屏
        setListError(true, "执行批次加载失败：" + describeApiError(error));
        renderErrorRow();
        // 错误态清零分页状态：否则“共 N 个已完成批次”仍是上一次成功的
        // 数字、分页按钮仍可点击，与空态文案自相矛盾
        state.total = 0;
        state.totalPages = 0;
        renderPagination();
    } finally {
        state.listLoading = false;
        renderPagination();
        // 锁释放后补刷在途期间登记的变更（cases.js pendingRefresh 同款机制）：
        // ①pendingPageSize 列表在途时改每页条数
        // ②pendingPage    列表在途时点数字页码/上一页/下一页
        // 修复前这两处都是直接 return，变更被静默丢弃——下拉框已显示新
        // 页大小、页码按钮已点过，但列表毫无反应。
        // 补刷前先把标志置 null 再递归：若补刷请求再次撞锁，会重新登记
        // 而非立即递归，杜绝无限循环。
        if (state.pendingPageSize !== null || state.pendingPage !== null) {
            const size = state.pendingPageSize;
            const page = state.pendingPage;
            state.pendingPageSize = null;
            state.pendingPage = null;
            if (size !== null) {
                state.pageSize = size;
                state.currentPage = 1;
            } else {
                state.currentPage = page;
            }
            loadExecutions();
        }
    }
}

/* ==========================================================================
   批次详情模态框
   ========================================================================== */

/**
 * 获取详情弹窗 Modal 单例
 * @returns {bootstrap.Modal} Modal 实例
 */
function getDetailModal() {
    if (!detailModal) {
        detailModal = new bootstrap.Modal(els.detailModalEl);
    }
    return detailModal;
}

/**
 * 渲染顶部汇总卡片与批次元信息
 *
 * @param {object} summary 批次汇总（列表 item 同构字段）
 * @returns {void}
 */
function renderSummary(summary) {
    els.sumTotal.textContent = String(summary.total_cases || 0);
    els.sumPassed.textContent = String(summary.passed || 0);
    els.sumFailed.textContent = String(summary.failed || 0);
    els.sumError.textContent = String(summary.error || 0);
    els.sumSkipped.textContent = String(summary.skipped || 0);
    els.sumPassRate.textContent = formatPassRate(summary.pass_rate);
    // 元信息走 textContent，编号天然防注入
    els.detailMeta.textContent =
        "批次号：" + summary.execution_id +
        "　完成时间：" + window.formatDate(summary.created_at);
}

/**
 * 渲染单用例明细行（失败/异常行 table-danger 高亮，堆栈等宽可滚动）
 *
 * @param {Array<object>} items 单用例明细列表（id 升序=执行先后顺序）
 * @returns {void}
 */
function renderCaseItems(items) {
    if (!Array.isArray(items) || items.length === 0) {
        els.detailItemsBody.innerHTML =
            '<tr><td colspan="5" class="text-center text-muted py-3">' +
            "该批次无单用例明细</td></tr>";
        return;
    }

    els.detailItemsBody.innerHTML = items
        .map(function (item) {
            // 失败/异常行整行浅红高亮，便于快速定位问题用例
            const isBad = item.result === "failed" || item.result === "error";
            const rowCls = isBad ? ' class="table-danger"' : "";
            // error_message 单元格：有堆栈时渲染等宽 pre 可滚动区，
            // 文本经 escapeHtml 防注入；无堆栈显示占位横线
            const errorCell = item.error_message
                ? '<pre class="mb-0 small bg-light border rounded p-1" ' +
                  'style="max-height:84px; overflow:auto; white-space:' +
                  'pre-wrap; word-break:break-all;">' +
                  escHtml(item.error_message) + "</pre>"
                : '<span class="text-muted">-</span>';
            return (
                "<tr" + rowCls + ">" +
                '<td class="text-nowrap fw-semibold">' +
                    escHtml(item.case_id) + "</td>" +
                "<td>" + escHtml(item.case_name) + "</td>" +
                "<td>" + resultBadge(item.result) + "</td>" +
                '<td class="text-end text-nowrap">' +
                    formatDurationMs(item.duration) + "</td>" +
                "<td>" + errorCell + "</td>" +
                "</tr>"
            );
        })
        .join("");
}

/**
 * 打开批次详情：拉取汇总+明细后渲染并展示
 *
 * @param {string} executionId 批次号
 * @returns {Promise<void>} 拉取失败 toast 提示且不开窗
 */
async function openDetailModal(executionId) {
    // 打开前先置加载态，避免上一批次内容闪现
    els.detailModalTitle.textContent = "批次详情";
    els.detailMeta.textContent = "批次号：" + executionId;
    els.detailItemsBody.innerHTML =
        '<tr><td colspan="5" class="text-center text-muted py-3">' +
        '<span class="spinner-border spinner-border-sm me-1"></span>' +
        "加载中...</td></tr>";
    state.detailExecutionId = executionId;
    getDetailModal().show();

    // 请求序号：用户在 await 期间点了另一个批次时，先发的慢响应不得
    // 覆盖后发批次的渲染（否则看到的内容与最后点击的行不一致）
    const reqSeq = ++state.detailRequestSeq;
    try {
        const detail = await getExecutionDetail(executionId);
        if (reqSeq !== state.detailRequestSeq) {
            return; // 过期响应，直接丢弃（不渲染、不关弹窗）
        }
        renderSummary(detail.summary);
        renderCaseItems(detail.items);
    } catch (error) {
        if (reqSeq !== state.detailRequestSeq) {
            return; // 过期响应的错误同样不打扰当前视图
        }
        window.showToast("批次详情加载失败：" + error.message, "danger");
        getDetailModal().hide();
    }
}

/* ==========================================================================
   执行触发模态框
   ========================================================================== */

/**
 * 获取触发弹窗 Modal 单例
 * @returns {bootstrap.Modal} Modal 实例
 */
function getTriggerModal() {
    if (!triggerModal) {
        triggerModal = new bootstrap.Modal(els.triggerModalEl);
    }
    return triggerModal;
}

/**
 * 重置触发表单到初始态并隐藏上一次受理结果 / 错误提示
 * @returns {void}
 */
function resetTriggerForm() {
    els.triggerForm.reset(); // select 恢复 HTML 默认（case_type=api/执行器默认）
    els.tEnvironment.value = "dev"; // reset 不保证还原 value 属性，显式置默认
    els.triggerResultAlert.classList.add("d-none");
    els.triggerResultText.textContent = "";
    // 与受理区同理：上一次的失败提示不该跟着新一次触发一起带进弹窗
    clearTriggerFieldErrors();
    window.showError(els.triggerErrorAlert, "");
}

/**
 * 清空触发表单全部字段错误态
 * @returns {void}
 */
function clearTriggerFieldErrors() {
    window.setFieldError(els.tPriority, els.tPriorityError, "");
    window.setFieldError(els.tEnvironment, els.tEnvironmentError, "");
}

/**
 * 触发表单前端校验（Day54）
 *
 * 只校验"后端一定会拒"的两项，不做多余拦截：
 *   - priority：取值必须落在 P0-P3（后端 VALID_CASE_PRIORITIES 白名单，
 *     非法值回 400）；多值按逗号拆分后逐个校验，大小写不敏感（后端也是
 *     upper() 后比对）；
 *   - environment：长度上限与输入框 maxlength、库层 String(16)、
*     路由层长度校验 同值（Day54-fix P1-① 三段口径对齐）。
 * module/tags/remark 是无格式约束的自由文本（tags 的空段在拆分时已丢弃），
 * 不设校验——凭空加规则会让原本合法的输入被前端拦住。
 *
 * @returns {{valid: boolean, errors: object}} 校验结果；errors 为
 *          {字段名: 错误文案}，valid=false 时非空
 */
function validateTriggerForm() {
    const errors = {};
    const priorityText = els.tPriority.value.trim();
    if (priorityText) {
        const invalidValues = splitFilterValues(priorityText).filter(function (item) {
            return VALID_PRIORITIES.indexOf(item.toUpperCase()) === -1;
        });
        if (invalidValues.length) {
            errors.priority = "优先级取值非法：" + invalidValues.join("、") +
                "；合法取值为 " + VALID_PRIORITIES.join("/");
        }
    }
    const environment = els.tEnvironment.value.trim();
    // 留空不算错：collectTriggerData 会回落后端默认环境 dev
    if (environment.length > MAX_ENVIRONMENT_LENGTH) {
        errors.environment =
            "执行环境长度不能超过" + MAX_ENVIRONMENT_LENGTH + "个字符";
    }
    return { valid: Object.keys(errors).length === 0, errors: errors };
}

/**
 * 渲染触发表单字段错误（字段名 → 控件/错误容器）
 *
 * @param {object} errors 形如 {priority: "..."} 的错误表
 * @returns {void}
 */
function applyTriggerFieldErrors(errors) {
    window.setFieldError(els.tPriority, els.tPriorityError, errors.priority || "");
    window.setFieldError(
        els.tEnvironment,
        els.tEnvironmentError,
        errors.environment || ""
    );
}

/**
 * 打开触发弹窗
 * @returns {void}
 */
function openTriggerModal() {
    resetTriggerForm();
    getTriggerModal().show();
}

/**
 * 收集触发表单为请求体（空文本字段不下发=不按该维度过滤）
 *
 * executor 选“默认”时不下发，由后端工厂按 TM_EXECUTOR 环境变量裁定。
 * priority/tags 按逗号拆分后**下发数组**（后端 _normalize_values 对
 * str 入参不拆逗号，整串当单值，多值下发字符串必然命中 0 条转 400）。
 * case_type 无“全部”选项，恒定下发（后端缺省亦为 api，见 6.34 决策）。
 *
 * 注意口径：其余筛选项留空=不按该维度过滤，但**不等于全量回归**——
 * case_type 恒为下拉当前值（默认 api），单次触发只能覆盖一种用例类型，
 * chip 用例需显式选 chip 才纳入。想跑全部类型需分两次触发。
 *
 * @returns {object} 仅含非空字段的请求体
 */
function collectTriggerData() {
    const payload = {
        case_type: els.tCaseType.value,
        environment: els.tEnvironment.value.trim() || "dev",
    };
    const moduleVal = els.tModule.value.trim();
    const priorityVal = els.tPriority.value.trim();
    const tagsVal = els.tTags.value.trim();
    const executorVal = els.tExecutor.value;
    const remarkVal = els.tRemark.value.trim();
    if (moduleVal) {
        payload.module = moduleVal;
    }
    // priority/tags 逗号分隔多值：逐段 trim 后剔除空项，拆分为数组下发
    if (priorityVal) {
        const priorityList = splitFilterValues(priorityVal);
        if (priorityList.length) {
            payload.priority = priorityList;
        }
    }
    if (tagsVal) {
        const tagsList = splitFilterValues(tagsVal);
        if (tagsList.length) {
            payload.tags = tagsList;
        }
    }
    if (executorVal) {
        payload.executor = executorVal;
    }
    if (remarkVal) {
        payload.remark = remarkVal;
    }
    return payload;
}

/**
 * 切换触发按钮 loading（互斥锁的视觉部分）
 * @param {boolean} loading 是否提交中
 * @returns {void}
 */
function setTriggerLoading(loading) {
    state.triggerLoading = loading;
    els.triggerSubmitBtn.disabled = loading;
    els.triggerSubmitSpinner.classList.toggle("d-none", !loading);
}

/**
 * 提交触发：前端校验 → 锁防重复 → 202 受理区 + 实时日志入口
 *
 * 失败时在弹窗内展示含 HTTP 状态码的错误（不只有 5 秒即逝的 toast），
 * 并提供就地重试按钮（Day54）——400「无符合条件的用例可执行」这类
 * 错误用户往往想换个筛选条件再试，重试入口必须在眼前。
 *
 * @returns {Promise<void>} 无返回值；失败展示错误区并恢复按钮
 */
async function submitTrigger() {
    if (state.triggerLoading) {
        return;
    }
    clearTriggerFieldErrors();
    window.showError(els.triggerErrorAlert, "");
    const check = validateTriggerForm();
    if (!check.valid) {
        // 校验不过不发起请求（与用例页表单同一口径）
        applyTriggerFieldErrors(check.errors);
        return;
    }
    setTriggerLoading(true);
    try {
        const result = await triggerExecution(collectTriggerData());
        // 受理成功：展示三要素（编号/pending/用例数），日志按钮绑定该批次
        els.triggerResultText.textContent =
            "批次号：" + result.execution_id +
            "　状态：" + result.status +
            "　待执行用例：" + result.total_cases + " 条";
        els.triggerResultAlert.classList.remove("d-none");
        // 用 dataset 暂存，按钮点击时读取（避免闭包持有跨批次旧编号）
        els.triggerViewLogBtn.dataset.executionId = result.execution_id;
        window.showToast("执行批次已受理");
    } catch (error) {
        // 400 无符合条件用例/枚举非法、500 服务端异常：错误区给状态码与原因
        window.showError(
            els.triggerErrorAlert,
            "触发执行失败：" + describeApiError(error)
        );
        window.showToast("触发执行失败：" + error.message, "danger");
    } finally {
        setTriggerLoading(false);
    }
}

/* ==========================================================================
   SSE 实时日志（EventSource 订阅四事件，终态必关流）
   ========================================================================== */

/**
 * 获取日志弹窗 Modal 单例
 * @returns {bootstrap.Modal} Modal 实例
 */
function getLogModal() {
    if (!logModal) {
        logModal = new bootstrap.Modal(els.logModalEl);
    }
    return logModal;
}

/**
 * 切换连接状态徽章文案/颜色
 *
 * @param {string} phase connecting（连接中）/running（接收事件）/
 *                     reconnecting（断线重连）/finished（已完成）/failed（失败）/
 *                     snapshot（终态前快照，已主动停止订阅）/
 *                     closed（浏览器已放弃重连，连接终止）
 * @returns {void}
 */
function setConnectionBadge(phase) {
    const map = {
        connecting: { cls: "text-bg-secondary", text: "连接中" },
        running: { cls: "text-bg-primary", text: "实时中" },
        reconnecting: { cls: "text-bg-warning", text: "重连中" },
        finished: { cls: "text-bg-success", text: "已完成" },
        failed: { cls: "text-bg-danger", text: "失败" },
        snapshot: { cls: "text-bg-secondary", text: "快照·已停订阅" },
        closed: { cls: "text-bg-danger", text: "已断开" },
    };
    const conf = map[phase] || map.connecting;
    els.logConnectionBadge.className = "badge ms-1 " + conf.cls;
    els.logConnectionBadge.textContent = conf.text;
}

// 本订阅周期是否已提示过日志清理（每次打开日志视图重置，只提示一次）
let _logCapNoticeShown = false;

/**
 * 日志区行数上界裁剪（Day44 D9）
 *
 * 子元素数超过 MAX_LOG_LINES 时从头部逐个删除最旧行，直到回到上界；
 * 首次触发清理时追加一条斜体 muted 提示行（每次打开日志只提示一次，
 * 不随裁剪反复刷屏）。提示行本身不再触发二次裁剪。
 *
 * @returns {void}
 */
function enforceLogLineCap() {
    const container = els.logContainer;
    let removedCount = 0;
    while (container.childElementCount > MAX_LOG_LINES) {
        // firstChild 即最旧行（append 顺序=时间顺序）
        container.removeChild(container.firstChild);
        removedCount += 1;
    }
    if (removedCount > 0 && !_logCapNoticeShown) {
        _logCapNoticeShown = true;
        const notice = document.createElement("div");
        notice.className = "text-muted small fst-italic";
        notice.textContent =
            "（日志超过 " + MAX_LOG_LINES +
            " 行上限，已自动清理最早的旧日志）";
        container.appendChild(notice);
    }
}

/**
 * 向日志区追加一行（全程 textContent/DOM API，杜绝 XSS）
 *
 * @param {string} text 日志正文（纯文本，特殊字符在函数内不转义——
 *                 textContent 天然按文本渲染）
 * @param {string} [tone] 语义色调：default/success/danger/warning/muted/primary
 * @returns {void}
 */
function appendLogLine(text, tone) {
    const line = document.createElement("div");
    // 语义色仅作用于固定类名，用户文本绝不进入 className
    const toneClass = {
        success: "text-success",
        danger: "text-danger",
        warning: "text-warning",
        muted: "text-muted",
        primary: "text-primary",
    }[tone || "default"];
    if (toneClass) {
        line.className = toneClass;
    }
    line.textContent = text; // textContent 天然防注入，error_message 同样安全
    els.logContainer.appendChild(line);
    // 防御性上界：防未来异常重连路径下日志 DOM 无界增长
    enforceLogLineCap();
    // 新帧到达自动滚到底部（用户手动上滚查看历史的体验待后续走查优化）
    els.logContainer.scrollTop = els.logContainer.scrollHeight;
}

/**
 * 向日志区追加多行堆栈块（等宽缩进，失败/异常事件专用）
 *
 * @param {string} text 堆栈原文
 * @returns {void}
 */
function appendLogBlock(text) {
    const block = document.createElement("div");
    block.className = "text-danger border-start border-danger ps-2 ms-3 mb-1";
    block.style.whiteSpace = "pre-wrap";
    block.style.wordBreak = "break-all";
    block.textContent = text;
    els.logContainer.appendChild(block);
    // 堆栈块同样计入上界（一行块算一个子元素）
    enforceLogLineCap();
    els.logContainer.scrollTop = els.logContainer.scrollHeight;
}

/**
 * 解析 SSE 事件帧的 JSON 载荷
 *
 * 为什么需要这层兜底: 四个事件回调直接 JSON.parse(event.data) 时，
 * 任一帧载荷异常（代理插入的错误页内容、服务端序列化异常、跨版本字段
 * 变更）都会在 DOM 事件回调里抛未捕获异常——帧被静默丢弃、连接保持、
 * 页面无任何提示，排查时几乎无迹可循。解析失败只跳过该帧并记一行，
 * 流的其余部分继续渲染。
 *
 * @param {MessageEvent} event SSE 消息事件
 * @returns {object|null} 解析后的载荷对象；解析失败或非对象返回 null
 */
function parseSsePayload(event) {
    let parsed;
    try {
        parsed = JSON.parse(event.data);
    } catch (parseError) {
        appendLogLine("事件帧解析失败，已跳过该帧：" + parseError.message, "muted");
        return null;
    }
    // 非对象载荷（数字/字符串/null）同样无法驱动 handleXxx，跳过
    if (parsed === null || typeof parsed !== "object") {
        appendLogLine("事件帧载荷不是对象，已跳过该帧。", "muted");
        return null;
    }
    return parsed;
}

/**
 * batch_start 帧处理：批次开始 + 用例总数（+执行器类型，可能为 null）
 *
 * @param {object} data {total_cases, executor_kind|status}
 * @returns {void}
 */
function handleBatchStart(data) {
    setConnectionBadge("running");
    const total = data.total_cases !== undefined ? data.total_cases : "-";
    appendLogLine(
        "── 批次开始 │ 共 " + total + " 条用例" +
        (data.executor_kind ? " │ 执行器：" + data.executor_kind : ""),
        "primary"
    );
    // pending 快照帧（无 executor_kind 而带 status=pending）只提示不推进终态
    if (data.status === BATCH_STATUS.PENDING) {
        appendLogLine("批次排队等待执行中，当前为状态快照。", "muted");
    }
    // 快照帧收束（终态前的单帧直发必须主动关流）:
    // 后端对“通道不存在且状态非终态”的批次只直发一帧 batch_start
    // （载荷含 status）随即结束响应流。该帧无 id 行、非终态，若不主动
    // 关闭，EventSource 会按规范约 3s 自动重连，每轮从头重发该帧，
    // 日志区与请求量都无界增长。识别条件取 data.status 存在且非终态——
    // live 实时流（{total_cases, executor_kind}）与 DB 重建回放
    // （{total_cases, executor_kind: null}）的 batch_start 都不带 status，
    // 不会被误判；只有单帧快照分支带 status。
    const isTerminalStatus = data.status === BATCH_STATUS.FINISHED ||
        data.status === BATCH_STATUS.FAILED;
    if (data.status && !isTerminalStatus) {
        appendLogLine(
            "批次当前为 " + data.status + "，尚未产生终态事件；已停止订阅，" +
            "请在批次进入终态后重新打开本窗口查看完整日志。",
            "muted"
        );
        closeEventSource();
        setConnectionBadge("snapshot");
    }
}

/**
 * case_finished 帧处理：逐条追加结果行，失败/异常展开完整堆栈
 *
 * @param {object} data {case_id,case_name,result,duration,error_message}
 * @returns {void}
 */
function handleCaseFinished(data) {
    const iconMap = {
        passed: ["[通过]", "success"],
        failed: ["[失败]", "danger"],
        error: ["[异常]", "warning"],
        skipped: ["[跳过]", "muted"],
    };
    const pair = iconMap[data.result] || ["[" + data.result + "]", "default"];
    appendLogLine(
        pair[0] + " " + data.case_id + " " + data.case_name +
        " — " + formatDurationMs(data.duration),
        pair[1]
    );
    if (
        (data.result === "failed" || data.result === "error") &&
        data.error_message
    ) {
        appendLogBlock(data.error_message); // 堆栈完整展示不截断
    }
}

/**
 * batch_finished 帧处理：汇总块 + 终态收束（关流+报告入口）
 *
 * @param {object} data {total,passed,failed,error,skipped,pass_rate}
 * @returns {void}
 */
function handleBatchFinished(data) {
    appendLogLine(
        "── 批次完成 │ 总 " + data.total +
        " │ 通过 " + data.passed +
        " │ 失败 " + data.failed +
        " │ 异常 " + data.error +
        " │ 跳过 " + data.skipped +
        " │ 通过率 " + formatPassRate(data.pass_rate),
        "success"
    );
    finishLogView(BATCH_STATUS.FINISHED);
}

/**
 * batch_failed 帧处理：错误信息 + 终态收束
 *
 * @param {object} data {error_message}
 * @returns {void}
 */
function handleBatchFailed(data) {
    appendLogLine("── 批次失败", "danger");
    if (data.error_message) {
        appendLogBlock(data.error_message);
    }
    finishLogView(BATCH_STATUS.FAILED);
}

/**
 * 终态收束：置徽章/报告按钮并关闭 EventSource（幂等，重复帧不重复处理）
 *
 * 报告入口按终态类型区分：仅 finished 批次有详情可看。批次异常失败
 * 走核心层 except 分支（case_manager.py:1913 起），该路径只置 failed
 * 并发 batch_failed 事件，**不写 defect_statistics 汇总行**，而详情
 * 接口以汇总行为存在与否判 404——若对 failed 批次仍亮出报告入口，
 * 用户点击后必然经历“弹窗闪现→404→hide→红字 toast”。failed 的失败
 * 原因（error_message）已在日志区由 handleBatchFailed 完整渲染。
 *
 * @param {string} phase finished/failed
 * @returns {void}
 */
function finishLogView(phase) {
    // 终态帧只处理一次：终态批次快照补发与自动重连可能让回调再入
    if (state.terminalReceived) {
        return;
    }
    state.terminalReceived = true;
    setConnectionBadge(phase);
    if (phase === BATCH_STATUS.FINISHED) {
        els.logViewReportBtn.classList.remove("d-none");
    } else {
        // failed 批次无汇总行，详情接口必 404：入口保持隐藏
        els.logViewReportBtn.classList.add("d-none");
    }
    // 终态后主动关闭：服务端终态帧后亦关闭流，双保险不残留连接
    closeEventSource();
}

/**
 * 安全关闭当前 EventSource 并释放引用
 * @returns {void}
 */
function closeEventSource() {
    if (state.eventSource) {
        state.eventSource.close();
        state.eventSource = null;
    }
    // 输出流与事件流同生命周期：收事件流时必须一并收输出流，
    // 否则关掉日志弹窗后仍有一条 EventSource 在后台空转重连
    closeOutputStream();
}

/* ==========================================================================
   Day53: 原始输出流（真实执行器 subprocess 逐行输出）
   ========================================================================== */

/**
 * 更新原始输出区状态提示文案
 *
 * @param {string} text 提示文本（如"未连接/实时接收中/输出已结束"）
 * @param {string} [tone] 语义色调：可用 Bootstrap text-* 后缀（success/danger/…）
 * @returns {void}
 */
function setOutputStatus(text, tone) {
    if (!els.logOutputStatus) {
        return;
    }
    els.logOutputStatus.className = "small " + (tone ? "text-" + tone : "text-muted");
    els.logOutputStatus.textContent = text;
}

/**
 * 安全关闭原始输出流并复位订阅态
 * @returns {void}
 */
function closeOutputStream() {
    if (state.outputEventSource) {
        state.outputEventSource.close();
        state.outputEventSource = null;
    }
}

/**
 * 原始输出区行数上界裁剪（口径同事件日志区 enforceLogLineCap）
 *
 * @returns {void}
 */
function enforceOutputLineCap() {
    const container = els.logOutputContainer;
    if (!container) {
        return;
    }
    let removed = 0;
    while (container.childElementCount > MAX_OUTPUT_LINES) {
        container.removeChild(container.firstChild);
        removed += 1;
    }
    if (removed > 0 && !state.outputCapNoticeShown) {
        state.outputCapNoticeShown = true;
        const notice = document.createElement("div");
        notice.className = "text-warning fst-italic";
        notice.textContent =
            "（原始输出超过 " + MAX_OUTPUT_LINES +
            " 行上限，已自动清理最早的旧输出）";
        container.appendChild(notice);
    }
}

/**
 * 向原始输出区追加一行（全程 textContent，杜绝 XSS）
 *
 * @param {string} text 行正文
 * @param {string} source stdout/stderr（stderr 以警示色区分）
 * @returns {void}
 */
function appendOutputLine(text, source) {
    const container = els.logOutputContainer;
    if (!container) {
        return;
    }
    const line = document.createElement("div");
    if (source === "stderr") {
        line.className = "text-warning";
    }
    line.textContent = text;
    container.appendChild(line);
    enforceOutputLineCap();
    container.scrollTop = container.scrollHeight;
}

/**
 * 渲染 output_end 终帧（收流并把状态置为已结束）
 *
 * @param {object} data 终帧载荷 {reason, lines}
 * @returns {void}
 */
function handleOutputEnd(data) {
    if (state.outputEnded) {
        return;
    }
    state.outputEnded = true;
    closeOutputStream();
    const reasonText = {
        batch_terminal: "批次已结束",
        no_capture: "该批次无原始输出（未开启捕获或已被回收）",
        lifetime_exceeded: "输出流达到存活上界，已收流",
    }[data.reason] || "输出已结束";
    appendOutputLine(
        "—— 输出结束（" + reasonText + "，共 " + (data.lines || 0) + " 行）——",
        "stdout"
    );
    setOutputStatus(reasonText, "success");
}

/**
 * 打开原始输出流（与事件流并行订阅，互不影响）
 *
 * 为什么单独一条流而不是把输出塞进 /events：两者的频率与可靠性要求
 * 差两个数量级（逐行输出可丢弃、结构化事件不可丢），合并后要么让事件
 * 被输出冲掉回放窗口，要么让输出被事件的严格语义绑死。分开后各自
 * 独立断线重连，任一条断了不影响另一条。
 *
 * @param {string} executionId 批次号
 * @returns {void}
 */
function openOutputStream(executionId) {
    closeOutputStream();
    if (!els.logOutputContainer) {
        return;
    }
    els.logOutputContainer.textContent = "";
    state.outputSeq = 0;
    state.outputEnded = false;
    state.outputCapNoticeShown = false;
    setOutputStatus("连接中 ...");

    const es = new EventSource(
        "/api/executions/" + encodeURIComponent(executionId) + "/output"
    );
    state.outputEventSource = es;

    es.addEventListener("output", function (event) {
        const data = parseSsePayload(event);
        if (data) {
            appendOutputLine(data.line, data.source);
            state.outputSeq += 1;
            setOutputStatus("实时接收中（已 " + state.outputSeq + " 行）");
        }
    });
    es.addEventListener("output_end", function (event) {
        const data = parseSsePayload(event);
        handleOutputEnd(data || {});
    });
    es.onerror = function () {
        if (state.outputEnded) {
            return;
        }
        if (es.readyState === EventSource.CLOSED) {
            closeOutputStream();
            setOutputStatus("输出流已断开（浏览器已停止重连）", "danger");
            return;
        }
        if (es.readyState === EventSource.CONNECTING) {
            setOutputStatus("输出流中断，正在自动重连 ...", "warning");
        }
    };
}

/* ==========================================================================
   Day53: 执行器双模式切换（真实/模拟）
   ========================================================================== */

/**
 * 按服务端返回的模式刷新下拉与提示（服务端为权威值）
 *
 * @param {string} mode simulated / pytest
 * @returns {void}
 */
function renderExecutorMode(mode) {
    state.executorMode = mode;
    if (els.executorModeSelect && mode) {
        els.executorModeSelect.value = mode;
    }
    if (els.executorModeHint) {
        els.executorModeHint.textContent =
            EXECUTOR_MODE_LABELS[mode] || mode;
    }
}

/**
 * 读取当前执行器模式（页面初始化时调用）
 *
 * @returns {Promise<void>} 读取失败只提示，不阻塞页面其余初始化
 */
async function loadExecutorMode() {
    try {
        const data = await window.api.get(EXECUTOR_MODE_API);
        renderExecutorMode(data.mode);
    } catch (error) {
        if (els.executorModeHint) {
            els.executorModeHint.textContent = "模式未知";
        }
        window.showToast("执行器模式读取失败：" + error.message, "warning");
    }
}

/**
 * 切换执行器模式（下拉变更时调用）
 *
 * 切换只影响**之后新触发**的批次；失败时把下拉回滚到服务端仍生效的值，
 * 避免"界面显示真实执行、服务端其实还是模拟"这种静默不一致。
 *
 * @param {string} mode 目标模式 simulated/pytest
 * @returns {Promise<void>} 无返回值
 */
async function changeExecutorMode(mode) {
    try {
        const data = await window.api.post(EXECUTOR_MODE_API, { mode: mode });
        renderExecutorMode(data.mode);
        window.showToast(
            "执行模式已切换为" + (EXECUTOR_MODE_LABELS[data.mode] || data.mode) +
            "，之后触发的批次按新模式执行",
            "success"
        );
    } catch (error) {
        // 回滚到最近一次服务端确认过的模式（读取失败时为 null，此时用
        // 服务端默认值兜底，避免留下一个"看起来切换成功"的假界面）
        const fallback = state.executorMode || "simulated";
        renderExecutorMode(fallback);
        window.showToast("执行模式切换失败：" + error.message, "danger");
    }
}

/**
 * 打开实时日志视图：先探活批次元信息，再重建订阅并绑定四事件
 *
 * 入口：触发受理区“查看实时日志”/列表行“日志”/详情弹窗“实时日志”。
 * 为什么先探活: events 接口依赖 test_execution_batches 批次行，
 * 批次表建立前的历史批次（仅有完成汇总）会返回 404；EventSource
 * 对 404 会按规范无限自动重连，必须在建流前用 status 接口拦截，
 * 给出友好提示而不是让连接空转。已终态的新批次 status=finished
 * 探活通过后由后端 DB 重建完整事件序列快照直发。探活响应带单调序号，
 * 用户在探活在途期间改点其他批次时，过期结果直接丢弃（防双流串批次）。
 *
 * @param {string} executionId 批次号
 * @returns {Promise<void>} 探活失败仅 toast 提示，不打开日志弹窗
 */
async function openLogView(executionId) {
    // 探活序号：用户在探活在途期间又点了另一个批次时，后发序号更大，
    // 先发的探活结果必须丢弃（否则会建两条流或串批次）
    const probeSeq = ++state.logRequestSeq;
    // 探活：无批次元信息行（历史批次/不存在）时拒绝建流
    try {
        await getExecutionStatus(executionId);
    } catch (error) {
        if (probeSeq !== state.logRequestSeq) {
            return; // 已被更新的探活取代，丢弃过期结果
        }
        // 404 与其他失败分流文案: 只在“批次表确无该批次行”时才断言
        // 历史批次不支持回放。Day54 起判据以 **HTTP 状态码** 为准——
        // api.js 已把状态码挂在 Error.status 上，404 即"批次表确无该行"，
        // 属协议层事实，不随后端文案改版而漂移。刻意不用“包含不存在”的
        // 子串判定：500 类数据库错误（如“表 test_execution_batches 不存在”）
        // 也含该子串，子串匹配会把真实故障误报成历史批次，正是后端路由层
        // 已明确规避的反模式（executions.py 纯类型判定，无子串兜底）。
        // 文案精确匹配保留为兜底：不带状态码的异常路径（旧后端/自定义错误）
        // 仍能正确分流。
        const isNotFound =
            error.status === 404 ||
            error.message === EXECUTION_NOT_FOUND_MESSAGE;
        if (isNotFound) {
            window.showToast(
                "该批次为历史批次，无实时事件记录，不支持日志回放。",
                "warning"
            );
        } else {
            window.showToast(
                "无法获取批次状态，日志暂不可用：" + error.message,
                "danger"
            );
        }
        return;
    }
    if (probeSeq !== state.logRequestSeq) {
        return; // 探活期间用户已改点其他批次，丢弃过期结果
    }

    // 同时只允许一个订阅：先收掉旧连接，防多批次日志串流
    closeEventSource();
    state.currentLogId = executionId;
    state.terminalReceived = false;

    // 重置日志区与连接态 UI
    els.logContainer.textContent = "";
    _logCapNoticeShown = false; // 新一轮订阅重新获得一次清理提示机会
    els.logViewReportBtn.classList.add("d-none");
    els.logExecutionMeta.textContent = "批次号：" + executionId;
    setConnectionBadge("connecting");
    appendLogLine("正在连接事件流 ...", "muted");

    // EventSource 自动管理重连并回传 Last-Event-ID（服务端支持断点续传），
    // 心跳注释帧（: heartbeat）由浏览器静默忽略，不产生 message 事件
    const es = new EventSource(
        "/api/executions/" + encodeURIComponent(executionId) + "/events"
    );
    state.eventSource = es;

    es.addEventListener("batch_start", function (event) {
        const data = parseSsePayload(event);
        if (data) {
            handleBatchStart(data);
        }
    });
    es.addEventListener("case_finished", function (event) {
        const data = parseSsePayload(event);
        if (data) {
            handleCaseFinished(data);
        }
    });
    es.addEventListener("batch_finished", function (event) {
        const data = parseSsePayload(event);
        if (data) {
            handleBatchFinished(data);
        }
    });
    es.addEventListener("batch_failed", function (event) {
        const data = parseSsePayload(event);
        if (data) {
            handleBatchFailed(data);
        }
    });
    es.onerror = function () {
        // 终态后忽略一切错误（终态回调已主动关流并亮出报告入口）
        if (state.terminalReceived) {
            return;
        }
        // 三种 readyState 必须区分（Day43 收尾修复 D8）:
        //   CLOSED(2)   浏览器**已放弃**重连（建连 500 / CORS / 网络不可达
        //               / 浏览器主动停止）。此时不会再有任何重连，若仍按
        //               "重连中"提示，用户会面对一个永不变化的徽章和永不
        //               到达的日志——UI 与真实连接状态背离。必须收流并如实
        //               告知"已断开"。
        //   CONNECTING(0) 浏览器正在按规范自动重连（正常瞬断），提示即可。
        //   OPEN(1)      连接正常，onerror 属竞态噪声，不处理。
        if (es.readyState === EventSource.CLOSED) {
            closeEventSource();
            setConnectionBadge("closed");
            appendLogLine(
                "连接已终止（服务端错误或网络不可达，浏览器已停止重连），" +
                "请关闭本窗口后重新打开查看。",
                "danger"
            );
            return;
        }
        if (es.readyState === EventSource.CONNECTING) {
            setConnectionBadge("reconnecting");
            appendLogLine(
                "连接中断，浏览器正在自动重连（断点由 Last-Event-ID 续传）...",
                "muted"
            );
        }
    };

    // Day53: 并行打开原始输出流（真实执行器 subprocess 的逐行输出走独立
    // 通道 /output；模拟批次会收到一帧 output_end(no_capture) 并如实提示）
    openOutputStream(executionId);
    getLogModal().show();
}

/**
 * 关闭日志视图：关流 + 复位连接徽章
 * @returns {void}
 */
function closeLogView() {
    closeEventSource();
    setConnectionBadge("connecting");
}

/* ==========================================================================
   事件绑定与初始化
   ========================================================================== */

/**
 * 绑定页面全部事件并触发首次列表加载
 * @returns {void}
 */
function initExecutionsPage() {
    // --- 顶部触发入口 ---
    els.triggerBtn.addEventListener("click", openTriggerModal);

    // --- 触发表单提交（拦截原生提交，锁内异步触发） ---
    els.triggerForm.addEventListener("submit", function (event) {
        event.preventDefault();
        submitTrigger();
    });

    // --- 输入即清除该字段错误态（Day54）：改正内容后红字必须立刻消失 ---
    els.triggerForm.addEventListener("input", function (event) {
        if (event.target === els.tPriority) {
            window.setFieldError(els.tPriority, els.tPriorityError, "");
        } else if (event.target === els.tEnvironment) {
            window.setFieldError(els.tEnvironment, els.tEnvironmentError, "");
        }
    });

    // --- 优先级即时校验（Day54）：失焦即校验，不必等到提交被后端拒 ---
    els.tPriority.addEventListener("blur", function () {
        applyTriggerFieldErrors(validateTriggerForm().errors);
    });

    // --- 环境名即时校验（Day54-fix P3-⑥）：与 tPriority 同口径 ---
    // 此前只有 tPriority 挂了 blur，tEnvironment 自身没有——用户填完超长
    // 环境名要等到提交才被后端 400 拒（本页其余字段都是失焦即校验，口径不一）。
    // 字段数少，失焦时直接跑全表校验即可，不必再造一套单字段 revalidate。
    els.tEnvironment.addEventListener("blur", function () {
        applyTriggerFieldErrors(validateTriggerForm().errors);
    });

    // --- 触发失败后的就地重试（Day54） ---
    // 直接重放提交：校验与锁都在 submitTrigger 内部，不需要第二套路径
    els.triggerRetryBtn.addEventListener("click", function () {
        submitTrigger();
    });

    // --- 列表错误态重试（Day54）：错误态不再只有"刷新整页"一条路 ---
    els.listErrorRetryBtn.addEventListener("click", function () {
        loadExecutions();
    });

    // --- 触发成功后的实时日志入口 ---
    els.triggerViewLogBtn.addEventListener("click", function () {
        const executionId = els.triggerViewLogBtn.dataset.executionId;
        if (executionId) {
            getTriggerModal().hide();
            openLogView(executionId);
        }
    });

    // --- 每页条数变化：重置第 1 页 ---
    els.pageSizeSelect.addEventListener("change", function (event) {
        const newSize = parseInt(event.target.value, 10) || 20;
        if (state.listLoading) {
            // 列表在途：登记待补刷而非直接 return，否则本次切换被静默
            // 丢弃（下拉显示新值、表格仍是旧数据）；锁释放时自动补刷
            state.pendingPageSize = newSize;
            return;
        }
        state.pageSize = newSize;
        state.currentPage = 1;
        loadExecutions();
    });

    // --- 分页事件委托 ---
    els.pagination.addEventListener("click", function (event) {
        const link = event.target.closest("a[data-action]");
        if (!link) {
            return;
        }
        event.preventDefault();
        // 禁用态（首/末页）直接忽略，这是正确的 UI 语义
        if (link.parentElement.classList.contains("disabled")) {
            return;
        }
        const action = link.getAttribute("data-action");
        // 解析出本次点击想要的目标页（不立即跳转）
        let targetPage = state.currentPage;
        if (action === "prev" && state.currentPage > 1) {
            targetPage = state.currentPage - 1;
        } else if (
            action === "next" &&
            state.currentPage < state.totalPages
        ) {
            targetPage = state.currentPage + 1;
        } else if (action === "page") {
            const parsed = parseInt(link.getAttribute("data-page"), 10);
            if (!isNaN(parsed)) {
                targetPage = parsed;
            }
        }
        if (targetPage === state.currentPage) {
            return;
        }
        // 列表在途：登记待补刷的目标页，锁释放后自动跳过去
        // （与每页条数变更的 pendingPageSize 同一机制；修复前此处直接
        //  return，加载中点页码被静默丢弃，用户看到列表毫无反应）
        if (state.listLoading) {
            state.pendingPage = targetPage;
            return;
        }
        state.currentPage = targetPage;
        loadExecutions();
    });

    // --- 列表操作列事件委托：详情 / 日志（行点击也打开详情） ---
    els.tableBody.addEventListener("click", function (event) {
        const btn = event.target.closest("button[data-action]");
        if (btn) {
            const executionId = btn.getAttribute("data-execution-id");
            const action = btn.getAttribute("data-action");
            if (action === "detail") {
                openDetailModal(executionId);
            } else if (action === "log") {
                openLogView(executionId);
            }
            return;
        }
        // 点击行内非按钮区域同样打开详情（最近的 tr 携带按钮编号即可）
        const row = event.target.closest("tr");
        const rowBtn = row && row.querySelector("button[data-action='detail']");
        if (rowBtn) {
            openDetailModal(rowBtn.getAttribute("data-execution-id"));
        }
    });

    // --- 详情弹窗内“实时日志”：回放该批次完整事件序列 ---
    els.detailReplayLogBtn.addEventListener("click", function () {
        if (state.detailExecutionId) {
            getDetailModal().hide();
            openLogView(state.detailExecutionId);
        }
    });

    // --- 日志终态“查看完整报告”：关日志开详情 ---
    els.logViewReportBtn.addEventListener("click", function () {
        if (state.currentLogId) {
            getLogModal().hide();
            openDetailModal(state.currentLogId);
        }
    });

    // --- 日志弹窗关闭（X/遮罩/取消）即收流，防后台空连 ---
    els.logModalEl.addEventListener("hidden.bs.modal", closeLogView);

    // --- Day53: 执行器双模式切换（下拉变更即调后端，失败自动回滚） ---
    if (els.executorModeSelect) {
        els.executorModeSelect.addEventListener("change", function (event) {
            changeExecutorMode(event.target.value);
        });
    }
    // 服务端是模式权威值：页面加载即回读一次，避免用 HTML 里的默认
    // option 冒充"当前模式"（服务端可能已被前一次切换改成真实执行）
    loadExecutorMode();

    // --- 首次加载第 1 页已完成批次 ---
    loadExecutions();
}

// DOM 就绪后初始化（defer + body 底部双保险，此处仍以事件兜底）
document.addEventListener("DOMContentLoaded", initExecutionsPage);

// 显式导出，便于控制台联调与后续天数复用
window.executionsPage = {
    state: state,
    loadExecutions: loadExecutions,
    openDetailModal: openDetailModal,
    openTriggerModal: openTriggerModal,
    submitTrigger: submitTrigger,
    openLogView: openLogView,
    closeLogView: closeLogView,
    openOutputStream: openOutputStream,
    loadExecutorMode: loadExecutorMode,
    changeExecutorMode: changeExecutorMode,
    formatDurationMs: formatDurationMs,
    buildPageSequence: buildPageSequence,
};
