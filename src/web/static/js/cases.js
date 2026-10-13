/* ==========================================================================
   TestMatrix 用例管理页业务脚本（Day42）
   能力：
     1. 用例列表：GET /cases/ 分页加载，loading/empty/error 三态渲染；
     2. 筛选搜索：模块/优先级/类型/状态下拉 + 关键字（回车/按钮触发），
        任一条件变化自动回到第 1 页重新查询；
     3. 新增/编辑：Bootstrap Modal 表单 + 前端必填/长度校验，编辑时
        case_id 禁用，POST/PUT 成功后刷新当前页；
     4. 删除：确认弹窗 + DELETE 物理删除；
     5. 批量导入：隐藏 file input + multipart/form-data 直连上传。
   安全：所有来自后端的文本渲染前经 escapeHtml 转义，防 XSS。
   并发：state.loading 互斥锁拦截重复翻页/查询。
   ========================================================================== */

/* ==========================================================================
   2.1 页面级状态
   ========================================================================== */
const state = {
    page: 1,              // 当前页码（从 1 起）
    pageSize: 20,         // 每页条数（10/20/50/100）
    total: 0,             // 后端返回的总条数
    totalPages: 0,        // 后端返回的总页数
    filters: {            // 当前生效的筛选条件（空串=不筛选；status 默认 active）
        module: "",
        priority: "",
        case_type: "",
        status: "active",
        keyword: "",
    },
    editingCaseId: null,  // 编辑中的用例编号；null=新增模式
    pendingDeleteId: null,// 删除确认弹窗暂存的待删用例编号
    loading: false,       // 列表加载互斥锁（防并发重复请求）
    pendingRefresh: false,// 锁释放后需补刷一次的标志（保存/删除/导入后
                          // 的刷新撞上在途请求时登记，避免刷新被丢弃）
    pendingPage: null,    // 列表在途期间被点击的目标页码，锁释放后自动补刷
                          // （与 pendingRefresh 同机制，但需记住目标页）
    pendingPageSize: null,// 列表在途期间被改动的每页条数，锁释放后回第 1 页补刷
    knownModules: [],     // 历次列表累积发现的模块名（跨页填充模块下拉）
    executorMode: null,   // 当前执行器模式（服务端为权威，仅用于表单必填口径）
};

// 编辑弹窗详情请求的在途序号（防竞态）：
// openEditModal 内 await 详情到返回之间存在空窗期，此间用户可点「新增用例」
// 或点另一行的「编辑」。若无序号校验，旧请求后到会把已打开的新增弹窗改写成
// 旧用例的编辑态（标题/编号禁用/editingCaseId 全被覆盖），用户保存时走 PUT
// 分支误改已有用例。序号单调递增，await 回来后与当前值不符即判定过期丢弃。
let editRequestSeq = 0;

// 模块下拉累积上界：knownModules 跨翻页累积且无自然终点（每翻一页都可能
// 遇到新模块），长期使用/超大数据集下会无界增长。超过该上限后停止追加，
// 防止下拉选项与内存占用随会话时长线性膨胀。当前已选模块不计入此上界，
// 保证「已选模块始终可选」优先于上界。
const MAX_KNOWN_MODULES = 200;

// 用例编号字符集（与后端 CaseCreateSchema 的 validate.Regexp 同源，
// 也与 data_driver.CASE_ID_PATTERN 一致：首字符必须字母或数字，后续允许
// 字母数字与 . _ -）。首字符限制不是洁癖：case_id 会被当作 pytest 的测试
// 目标传下去，`--version` 这类以连字符开头的值能过宽松字符集却被 argparse
// 当成选项（Day44 P2-12 / Day43 收尾 D6 的同一个洞）。
const CASE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

// 执行目标（source_ref）校验口径（与 src/common/source_ref.py 逐条对齐）：
// 形态正则 / 长度 ≤512 / 路径段禁 ".."。前端持有一份只为"提交前拦下"，
// 后端仍会独立校验（纵深防御；两侧规则同源，改一处必须同步另一处）。
//
// Day54-fix P2-④：形态正则**不能用 \w**——JS 的 \w 恒为 ASCII-only，
// 而 Python 的 \w 在默认（re.UNICODE）下匹配 Unicode 字母与数字。后端会
// 放行 `测试/用例.py` 这类合法 pytest 目标，前端却判"格式非法"把它拦下
// （方向是误拦合法值、无注入风险，但"逐条对齐"的名头与实际不符）。
// 故显式写 Unicode 属性类：\p{L} 字母、\p{N} 数字，与 Python \w 的
// Unicode 语义等价；**u 标志必填**，否则 \p{...} 只是普通转义序列，
// 正则会静默失配（把合法值全判成非法）。
const SOURCE_REF_PATTERN = /^[\p{L}\p{N}_./-]+\.py(::[\p{L}\p{N}_]+)*$/u;
const MAX_SOURCE_REF_LENGTH = 512;

// 用例描述展示层长度上限（后端 schema 对 description 无长度约束，
// 此处防误贴整份日志把卡片撑爆；见 cases.html 的字段注释）
const MAX_DESCRIPTION_LENGTH = 1000;

// 执行器模式接口（Day53 双模式切换）：真实执行（pytest）模式下，
// 没有 source_ref 的用例会被执行器逐条降级为 error，故该模式下表单必填它。
const EXECUTOR_MODE_API = "/executor/mode";

// 执行器模式枚举（与后端 _current_executor_mode 的取值域一致）
const EXECUTOR_MODE_PYTEST = "pytest";

/* ==========================================================================
   DOM 引用集中获取（节点均为模板静态元素，脚本在 body 底部加载）
   ========================================================================== */
const els = {
    filterModule: document.getElementById("filterModule"),
    filterPriority: document.getElementById("filterPriority"),
    filterType: document.getElementById("filterType"),
    filterStatus: document.getElementById("filterStatus"),
    filterKeyword: document.getElementById("filterKeyword"),
    searchBtn: document.getElementById("searchBtn"),
    resetBtn: document.getElementById("resetBtn"),
    listErrorAlert: document.getElementById("listErrorAlert"),
    listErrorText: document.getElementById("listErrorText"),
    listErrorRetryBtn: document.getElementById("listErrorRetryBtn"),
    tableBody: document.getElementById("casesTableBody"),
    totalCount: document.getElementById("totalCount"),
    pageSizeSelect: document.getElementById("pageSizeSelect"),
    pagination: document.getElementById("pagination"),
    importBtn: document.getElementById("importBtn"),
    createBtn: document.getElementById("createBtn"),
    importFileInput: document.getElementById("importFileInput"),
    formModalEl: document.getElementById("caseFormModal"),
    formModalTitle: document.getElementById("caseFormModalTitle"),
    caseForm: document.getElementById("caseForm"),
    fCaseId: document.getElementById("fCaseId"),
    fCaseIdHint: document.getElementById("fCaseIdHint"),
    fName: document.getElementById("fName"),
    fModule: document.getElementById("fModule"),
    fCreator: document.getElementById("fCreator"),
    fSourceRef: document.getElementById("fSourceRef"),
    fSourceRefModeHint: document.getElementById("fSourceRefModeHint"),
    fPriority: document.getElementById("fPriority"),
    fCaseType: document.getElementById("fCaseType"),
    fStatus: document.getElementById("fStatus"),
    fDescription: document.getElementById("fDescription"),
    formAlert: document.getElementById("caseFormAlert"),
    saveBtn: document.getElementById("caseSaveBtn"),
    saveSpinner: document.getElementById("caseSaveSpinner"),
    deleteModalEl: document.getElementById("deleteConfirmModal"),
    deleteCaseIdText: document.getElementById("deleteCaseIdText"),
    confirmDeleteBtn: document.getElementById("confirmDeleteBtn"),
};

// Bootstrap Modal 实例（懒获取，同一弹窗复用单例）
let formModal = null;
let deleteModal = null;

/* ==========================================================================
   2.2 工具函数
   ========================================================================== */

/**
 * 转义 HTML 特殊字符
 *
 * 用途：所有后端返回的文本（case_id/name/module/creator 等）拼入
 * innerHTML 前必须经此函数转义，防止用例名/描述中夹带的 <script>
 * 等内容被解析执行（存储型 XSS）。
 *
 * 实现说明（v5 收敛，V2-P3-4）: 本文件原先**自带一份** escapeHtml
 * 实现，与 main.js:47 的 window.escapeHtml 行为等价（v4 审查逐字符
 * 比对确认覆盖字符集相同）。两份定义构成同一安全基元的两个事实来源，
 * 将来只改一处即产生分叉；且本文件的顶层函数声明会覆盖
 * main.js 挂到 window 上的那份（加载序：base.html 先 main.js→api.js，
 * cases.html 后 cases.js），使"改 main.js"实际不生效。
 * 现统一复用 window.escapeHtml，本文件不再持有实现。
 *
 * 加载序依赖: cases.html 经 base.html 继承，main.js 先于 cases.js 加载，
 * 故此处读取 window.escapeHtml 时它必然已就绪。
 *
 * 为什么本地名是 escHtml 而不叫 escapeHtml（v5 遗留 bug 修复）:
 * main.js 的 escapeHtml 是**顶层 function 声明**，在浏览器里会绑定成
 * 全局对象的**不可配置**属性；本文件若再用 const/function 同名声明，
 * 按 ECMAScript GlobalDeclarationInstantiation 规则，词法声明遇到同名
 * 不可配置属性直接抛 SyntaxError——**整个 cases.js 不实例化**，页面
 * 停在初始 HTML（列表永远"加载中"）且控制台只有一条语法错误。
 * 与 executions.js 同样处理：换异名别名引用同一函数。症状只能靠浏览器
 * 控制台发现，ruff/pytest 都管不到 JS。
 *
 * @param {*} value 任意输入值（非字符串按空串处理）
 * @returns {string} 转义后的安全字符串（null/undefined 返回空串）
 */
const escHtml = window.escapeHtml;

/**
 * 生成优先级 badge 的 HTML（枚举固定值，颜色不走转义）
 *
 * @param {string} priority P0/P1/P2/P3（其他值降级为次级灰 badge）
 * @returns {string} 带 Bootstrap badge 类的 HTML 字符串
 */
function priorityBadge(priority) {
    // P0 红（最高风险）/ P1 黄 / P2 蓝 / P3 灰，与验收 4.22 颜色约定一致
    const colorMap = {
        P0: "text-bg-danger",
        P1: "text-bg-warning",
        P2: "text-bg-primary",
        P3: "text-bg-secondary",
    };
    const cls = Object.prototype.hasOwnProperty.call(colorMap, priority)
        ? colorMap[priority]
        : "text-bg-secondary";
    return '<span class="badge ' + cls + '">' + escHtml(priority) + "</span>";
}

/**
 * 生成状态 badge 的 HTML
 *
 * 实现已下沉到 main.js 的 window.statusBadge（Day44 收尾，消除两页重复
 * 实现）。此处仅做异名引用，不再持有实现——与 escHtml 同一模式，且避开
 * 与 main.js 全局同名的 SyntaxError 风险。
 */
const statusBadge = window.statusBadge;

/**
 * 根据 state.filters + 分页参数构造查询字符串
 *
 * 空值维度跳过不传（后端对缺省维度不筛选）；status 始终传——
 * 需要“全部”时显式传 all（后端缺省值是 active，不传会被误过滤）。
 *
 * @returns {string} 以 & 连接的 application/x-www-form-urlencoded 查询串
 */
function buildQueryString() {
    const params = new URLSearchParams();
    params.set("page", String(state.page));
    params.set("page_size", String(state.pageSize));
    params.set("status", state.filters.status);
    if (state.filters.module) {
        params.set("module", state.filters.module);
    }
    if (state.filters.priority) {
        params.set("priority", state.filters.priority);
    }
    if (state.filters.case_type) {
        params.set("case_type", state.filters.case_type);
    }
    if (state.filters.keyword) {
        params.set("keyword", state.filters.keyword);
    }
    return params.toString();
}

/**
 * 切换列表错误提示条显隐（含配套重试按钮）
 *
 * 文案与显隐统一走 main.js 的 window.showError（Day54 收敛，三页面同一实现）；
 * 重试按钮与提示条同生命周期——只在错误态出现，无错时留在页面上会让用户
 * 误以为"这里有个操作入口"，点下去却发现只是重放一次正常请求。
 *
 * @param {boolean} show 是否显示
 * @param {string} [message] 错误文案（show=true 时生效）
 * @returns {void}
 */
function setListError(show, message) {
    window.showError(els.listErrorAlert, show ? message : "");
    window.showRetry(els.listErrorRetryBtn, Boolean(show));
}

/* ==========================================================================
   2.3 列表加载与渲染（loading/empty/data 三态）
   ========================================================================== */

/**
 * 渲染表格 loading 态行
 *
 * @returns {void}
 */
function renderLoadingRow() {
    els.tableBody.innerHTML =
        '<tr><td colspan="9" class="text-center text-muted py-4">' +
        '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>' +
        "加载中...</td></tr>";
}

/**
 * 渲染表格空态行
 *
 * @returns {void}
 */
function renderEmptyRow() {
    els.tableBody.innerHTML =
        '<tr><td colspan="9" class="text-center text-muted py-4">' +
        '<i class="bi bi-inbox me-1"></i>暂无符合条件的用例，' +
        "试试调整筛选条件或点击新增用例</td></tr>";
}

/**
 * 渲染表格错误态行（与空态刻意区分，Day54）
 *
 * 为什么必须分开：接口失败时原先渲染的是「暂无符合条件的用例，试试调整
 * 筛选条件」——用户会去反复调筛选，而真实原因是请求根本没成功。两种状态
 * 在表格主视觉上完全一致（都是空表），只有页头一行提示条不同。错误行的
 * 文案直接指路到重试按钮，且走 text-danger 与空态的 text-muted 视觉可分。
 *
 * @returns {void}
 */
function renderErrorRow() {
    els.tableBody.innerHTML =
        '<tr><td colspan="9" class="text-center text-danger py-4">' +
        '<i class="bi bi-exclamation-triangle me-1"></i>' +
        "用例列表加载失败，请点击上方提示条中的「重试」</td></tr>";
}

/**
 * 渲染用例数据行（所有字段经 escapeHtml 后再拼接）
 *
 * @param {Array<object>} items 后端分页响应的 items 列表
 * @returns {void}
 */
function renderTableRows(items) {
    // 操作列按钮携带 data-action 与 data-case-id，点击由 tbody 事件委托统一处理；
    // data-case-id 同样转义，防止编号中的引号截断属性
    els.tableBody.innerHTML = items
        .map(function (item) {
            const safeId = escHtml(item.case_id);
            return (
                "<tr>" +
                '<td class="fw-semibold text-nowrap">' + safeId + "</td>" +
                "<td>" + escHtml(item.name) + "</td>" +
                "<td>" + escHtml(item.module) + "</td>" +
                "<td>" + priorityBadge(item.priority) + "</td>" +
                "<td>" + escHtml(item.case_type) + "</td>" +
                "<td>" + statusBadge(item.status) + "</td>" +
                "<td>" + escHtml(item.creator) + "</td>" +
                '<td class="text-nowrap">' + window.formatDate(item.created_at) + "</td>" +
                '<td class="text-end text-nowrap">' +
                '<button type="button" class="btn btn-outline-primary btn-sm me-1" ' +
                'data-action="edit" data-case-id="' + safeId + '">编辑</button>' +
                '<button type="button" class="btn btn-outline-danger btn-sm" ' +
                'data-action="delete" data-case-id="' + safeId + '">删除</button>' +
                "</td>" +
                "</tr>"
            );
        })
        .join("");
}

/**
 * 计算分页页码按钮序列
 *
 * 实现已下沉到 main.js 的 window.buildPageSequence（Day44 收尾，与
 * executions.js 原本逐字重复的两份实现合并为一处）。此处仅做异名引用。
 */
const buildPageSequence = window.buildPageSequence;

/**
 * 渲染分页控件（上一页/页码/下一页）+ 总条数
 *
 * 加载期间给按钮加 disabled 防止翻页（与 state.loading 双保险）。
 *
 * @returns {void}
 */
function renderPagination() {
    els.totalCount.textContent = String(state.total);

    if (state.totalPages <= 0) {
        els.pagination.innerHTML = "";
        return;
    }

    const disabledAttr = state.loading ? " aria-disabled='true'" : "";
    const prevDisabled = state.page <= 1 || state.loading;
    const nextDisabled = state.page >= state.totalPages || state.loading;

    let html =
        '<li class="page-item' + (prevDisabled ? " disabled" : "") + '">' +
        '<a class="page-link" href="#" data-action="prev"' + disabledAttr +
        ">上一页</a></li>";

    buildPageSequence(state.page, state.totalPages).forEach(function (p) {
        if (p === "...") {
            // 省略号不可点击
            html +=
                '<li class="page-item disabled"><span class="page-link">…</span></li>';
            return;
        }
        const activeCls = p === state.page ? " active" : "";
        html +=
            '<li class="page-item' + activeCls + '">' +
            '<a class="page-link" href="#" data-action="page" data-page="' + p + '">' +
            p + "</a></li>";
    });

    html +=
        '<li class="page-item' + (nextDisabled ? " disabled" : "") + '">' +
        '<a class="page-link" href="#" data-action="next"' + disabledAttr +
        ">下一页</a></li>";

    els.pagination.innerHTML = html;
}

/**
 * 从当前页 items 累积模块名并刷新模块下拉选项
 *
 * 设计：后端无“模块字典”接口，模块选项从列表数据去重提取；跨翻页
 * 累积到 state.knownModules，避免只保留当前页模块。两项保护：
 *   1. 上界：累积集合超过 MAX_KNOWN_MODULES 即截断，防止无界增长；
 *   2. 已选保留：当前选中的模块即使被上界截断，也会补回列表——否则
 *      选中某模块后下拉只剩该模块，用户无法切回“全部模块”或其他模块。
 * 当前选中值在重渲染后恢复，不打断筛选。
 *
 * @param {Array<object>} items 当前页用例行
 * @returns {void}
 */
function renderModuleOptions(items) {
    // 合并来源：历史累积 + 当前页提取（去重，保留首次出现顺序）
    const merged = state.knownModules.slice();
    items.forEach(function (item) {
        const name = item.module || "";
        if (name && merged.indexOf(name) === -1) {
            merged.push(name);
        }
    });
    // 上界保护：超限截断（不排序，保留已累积的靠前项，行为更稳定）
    if (merged.length > MAX_KNOWN_MODULES) {
        merged.length = MAX_KNOWN_MODULES;
    }
    merged.sort(function (a, b) {
        return a.localeCompare(b, "zh-Hans-CN");
    });

    // 当前已选模块：state.filters 为准，DOM 值为兜底（双保险防 desync）
    const selected = state.filters.module || els.filterModule.value;
    if (selected && merged.indexOf(selected) === -1) {
        // 被上界截断掉的已选模块补回，保证用户始终能选回它
        merged.push(selected);
        merged.sort(function (a, b) {
            return a.localeCompare(b, "zh-Hans-CN");
        });
    }
    state.knownModules = merged;

    els.filterModule.innerHTML =
        '<option value="">全部模块</option>' +
        state.knownModules
            .map(function (name) {
                return '<option value="' + escHtml(name) + '">' +
                    escHtml(name) + "</option>";
            })
            .join("");
    // 恢复选中；若已选模块不在累积列表中（如重置瞬间），回落到“全部”
    els.filterModule.value = state.knownModules.indexOf(selected) !== -1 ? selected : "";
}

/**
 * 加载用例列表（核心：loading 互斥 + 三态渲染 + 分页/模块联动）
 *
 * @returns {Promise<void>} 无返回值；失败时渲染错误条与空态，不抛出
 */
async function loadCases() {
    // 防并发：快速连点页码/筛选时只允许一个在途请求（验收 4.26）
    // 但"有在途请求"不等于"不需要再刷一次"：保存/删除/导入成功后的
    // await loadCases() 若撞上锁会被静默丢弃，用户看到的是 toast 说
    // "保存成功"、列表却还是旧数据。修复前只能手动刷新页面才发现。
    // 改为登记待刷新标志，锁释放时自动补刷一次。
    if (state.loading) {
        state.pendingRefresh = true;
        return;
    }
    state.loading = true;
    state.pendingRefresh = false;
    setListError(false);
    renderLoadingRow();
    renderPagination();

    try {
        const data = await window.api.get("/cases/?" + buildQueryString());
        const items = Array.isArray(data.items) ? data.items : [];

        // 删除最后一条导致当前页空掉：自动回退一页重查（仅回退一次）
        if (items.length === 0 && state.page > 1) {
            state.page = Math.max(1, state.page - 1);
            state.loading = false; // 递归前释放锁
            await loadCases();
            return;
        }

        state.total = Number(data.total) || 0;
        state.totalPages = Number(data.total_pages) || 0;

        if (items.length === 0) {
            renderEmptyRow();
        } else {
            renderTableRows(items);
        }
        renderModuleOptions(items);
        renderPagination();
    } catch (error) {
        // 错误态：页内 alert 明示原因 + 表格错误态占位，不白屏（验收 4.21）
        // 同时重置分页计数：否则页脚仍显示上一次的"共 137 条"+ 空表格，
        // 用户看到自相矛盾的画面，易误判为数据丢失
        state.total = 0;
        state.totalPages = 0;
        setListError(true, "用例列表加载失败：" + describeApiError(error));
        renderErrorRow();
        renderPagination();
    } finally {
        state.loading = false;
        // 二次刷新分页按钮禁用态（成功/失败都要解锁）
        renderPagination();
        // 锁释放后补刷被登记的刷新请求
        if (state.pendingRefresh) {
            state.pendingRefresh = false;
            loadCases();
            return;
        }
        // 补刷在途期间登记的分页变更（Day43 收尾 D10）。
        // 先置 null 再递归：补刷请求若再次撞锁会重新登记而非立即递归，
        // 杜绝无限循环。pendingPageSize 优先级更高（改页大小要回第 1 页）。
        if (state.pendingPageSize !== null || state.pendingPage !== null) {
            const size = state.pendingPageSize;
            const page = state.pendingPage;
            state.pendingPageSize = null;
            state.pendingPage = null;
            if (size !== null) {
                state.pageSize = size;
                state.page = 1;
            } else {
                state.page = page;
            }
            loadCases();
        }
    }
}

/* ==========================================================================
   2.4 筛选搜索联动
   ========================================================================== */

/**
 * 从筛选控件读取值写入 state.filters 并回到第 1 页
 *
 * @returns {void}
 */
function syncFiltersAndResetPage() {
    state.filters.module = els.filterModule.value;
    state.filters.priority = els.filterPriority.value;
    state.filters.case_type = els.filterType.value;
    state.filters.status = els.filterStatus.value;
    state.filters.keyword = els.filterKeyword.value.trim();
    state.page = 1; // 任一筛选条件变化都从第 1 页开始
}

/**
 * 重置所有筛选控件与 state 到默认口径（status=active，其余空）
 *
 * @returns {void}
 */
function resetFilters() {
    els.filterModule.value = "";
    els.filterPriority.value = "";
    els.filterType.value = "";
    els.filterStatus.value = "active"; // 与后端列表缺省口径一致
    els.filterKeyword.value = "";
    state.knownModules = []; // 模块字典随筛选重置清空，随数据重新累积
    syncFiltersAndResetPage();
}

/* ==========================================================================
   2.5 新增/编辑弹窗
   ========================================================================== */

/**
 * 获取表单弹窗 Bootstrap Modal 单例
 *
 * @returns {bootstrap.Modal} Modal 实例
 */
function getFormModal() {
    if (!formModal) {
        formModal = new bootstrap.Modal(els.formModalEl);
    }
    return formModal;
}

/**
 * 清空单个字段的校验错误态
 *
 * 为什么本页**不**委托给 main.js 的同名工具（Day54 走查实测踩到）:
 * 经典脚本里的顶层 `function setFieldError(){}` 会绑定成**全局对象**的
 * 属性，于是在本页 `window.setFieldError` 恰好指向本函数自己——委托写法
 * 变成自我递归，浏览器直接抛 `RangeError: Maximum call stack size
 * exceeded`，表单校验整体失效（与 6.33/7.36 的全局同名陷阱同族，
 * 区别只是那次炸的是 SyntaxError，这次炸的是无限递归）。
 * 故本页保留自己的实现；跨页共用的那份只在**没有同名顶层声明**的页面
 * （executions.js）直接调用。
 *
 * @param {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement} field 表单控件
 * @param {HTMLElement} errorEl 对应的 invalid-feedback 容器
 * @returns {void}
 */
function clearFieldError(field, errorEl) {
    field.classList.remove("is-invalid");
    errorEl.textContent = "";
}

/**
 * 显示单个字段校验错误
 *
 * @param {HTMLInputElement|HTMLSelectElement|HTMLTextAreaElement} field 表单控件
 * @param {HTMLElement} errorEl invalid-feedback 容器
 * @param {string} message 错误文案
 * @returns {void}
 */
function setFieldError(field, errorEl, message) {
    field.classList.add("is-invalid");
    errorEl.textContent = message;
}

/**
 * 校验错误与表单控件的绑定表（字段名 → 控件 + 错误容器）
 *
 * 表驱动而非逐字段 if：clearAllFieldErrors 与 submitForm 原先各自逐字段
 * 写一遍，新增字段时漏改任何一处都表现为"错误静默不显示"（有 is-invalid
 * 却没有文案，或反之）。收敛成一张表后两处自动同步。
 */
const FIELD_ERROR_BINDINGS = {
    case_id: { field: els.fCaseId, errorId: "fCaseIdError" },
    name: { field: els.fName, errorId: "fNameError" },
    module: { field: els.fModule, errorId: "fModuleError" },
    creator: { field: els.fCreator, errorId: "fCreatorError" },
    source_ref: { field: els.fSourceRef, errorId: "fSourceRefError" },
    description: { field: els.fDescription, errorId: "fDescriptionError" },
};

/**
 * 清空全部字段错误态
 *
 * @returns {void}
 */
function clearAllFieldErrors() {
    Object.keys(FIELD_ERROR_BINDINGS).forEach(function (key) {
        const binding = FIELD_ERROR_BINDINGS[key];
        clearFieldError(binding.field, document.getElementById(binding.errorId));
    });
}

/**
 * 把校验结果里的字段错误逐条渲染到对应控件（Day54 表驱动）
 *
 * @param {object} errors 形如 {字段名: 错误文案} 的错误表
 * @returns {number} 实际渲染的字段数（便于调用方判断是否有未绑定字段）
 */
function applyFieldErrors(errors) {
    let applied = 0;
    Object.keys(errors).forEach(function (key) {
        const binding = FIELD_ERROR_BINDINGS[key];
        if (!binding) {
            return; // 未在绑定表内的字段（理论上不存在）静默跳过，不抛错
        }
        setFieldError(
            binding.field,
            document.getElementById(binding.errorId),
            errors[key]
        );
        applied += 1;
    });
    return applied;
}

/**
 * 单字段即时校验（失焦/输入触发，Day54）
 *
 * 复用 validateForm 的全量判定，但只落到被触发的那一个字段：用户填到一半
 * 时不应该看到其它空字段的红字（那会让"必填"提示在打开弹窗瞬间铺满全屏）。
 *
 * @param {string} key 字段名（FIELD_ERROR_BINDINGS 的键）
 * @param {object} errors 已算好的错误表（避免每字段重复整表校验）
 * @returns {void}
 */
function revalidateField(key, errors) {
    const binding = FIELD_ERROR_BINDINGS[key];
    if (!binding) {
        return;
    }
    const errorEl = document.getElementById(binding.errorId);
    if (errors[key]) {
        setFieldError(binding.field, errorEl, errors[key]);
    } else {
        clearFieldError(binding.field, errorEl);
    }
}

/**
 * 按控件反查绑定表并清除它的错误态（Day54）
 *
 * 为什么需要：只在校验点（提交/失焦）写错误、不在输入时清错误，会出现
 * "名称已经填好了、红字还写着'用例名称必填'"这种自相矛盾的画面——浏览器
 * 走查截图时实测到。用户在改正内容的那一刻，这条提示就已经过期了。
 *
 * @param {HTMLElement} element 触发输入的控件
 * @returns {void}
 */
function clearFieldErrorForElement(element) {
    const keys = Object.keys(FIELD_ERROR_BINDINGS);
    for (let index = 0; index < keys.length; index++) {
        const binding = FIELD_ERROR_BINDINGS[keys[index]];
        if (binding.field === element) {
            clearFieldError(
                binding.field,
                document.getElementById(binding.errorId)
            );
            return;
        }
    }
}

/**
 * 重置表单到新增模式默认值并清除错误态
 *
 * @returns {void}
 */
function resetForm() {
    els.caseForm.reset(); // select 的 selected 默认项随之恢复（P2/api/active）
    // input/textarea 需显式置默认：reset 只还原 HTML value 属性
    els.fModule.value = "default";
    els.fCreator.value = "admin";
    els.fSourceRef.value = "";
    els.fDescription.value = "";
    clearAllFieldErrors();
    // 上一次提交失败留下的表单级错误条随之清空（否则新开弹窗就带着旧报错）
    window.showError(els.formAlert, "");
}

/**
 * 打开新增弹窗：case_id 可编辑，editingCaseId 置 null
 *
 * @returns {void}
 */
function openCreateModal() {
    // 递增在途序号：作废所有尚未返回的 openEditModal 详情请求
    editRequestSeq += 1;
    state.editingCaseId = null;
    resetForm();
    els.formModalTitle.textContent = "新增用例";
    els.fCaseId.disabled = false; // 新增时编号必填可编辑
    els.fCaseIdHint.classList.add("d-none");
    getFormModal().show();
}

/**
 * 打开编辑弹窗：拉取详情回填，case_id 禁用并显示不可修改说明
 *
 * @param {string} caseId 用例业务编号
 * @returns {Promise<void>} 无返回值；详情拉取失败 toast 提示且不开窗
 */
async function openEditModal(caseId) {
    // 取本次请求序号：await 回来后若序号已被后续操作（再次点编辑/点新增）
    // 推进，说明用户意图已变，本响应必须丢弃
    const seq = ++editRequestSeq;
    try {
        const detail = await window.api.get(
            "/cases/" + encodeURIComponent(caseId)
        );
        if (seq !== editRequestSeq) {
            return; // 过期响应：丢弃，绝不改写当前弹窗
        }
        state.editingCaseId = caseId;
        resetForm();
        els.formModalTitle.textContent = "编辑用例";
        // 编号禁用 + 说明（提交时 PUT body 也不带 case_id，双保险）
        els.fCaseId.disabled = true;
        els.fCaseIdHint.classList.remove("d-none");

        // 回填全部可编辑字段（null/undefined 安全回落空串）
        els.fCaseId.value = detail.case_id || caseId;
        els.fName.value = detail.name || "";
        els.fModule.value = detail.module || "";
        els.fCreator.value = detail.creator || "";
        // source_ref 由详情接口无条件返回（Day48 起键恒存在，未配置为 null）
        els.fSourceRef.value = detail.source_ref || "";
        els.fPriority.value = detail.priority || "P2";
        els.fCaseType.value = detail.case_type || "api";
        els.fStatus.value = detail.status || "active";
        els.fDescription.value = detail.description || "";

        getFormModal().show();
    } catch (error) {
        window.showToast("获取用例详情失败：" + error.message, "danger");
    }
}

/**
 * 校验执行目标（source_ref），与 core 层 validate_source_ref 逐条对齐
 *
 * 三条规则：形态（SOURCE_REF_PATTERN）/ 长度 ≤512 / 路径段禁 ".."；
 * 真实执行（pytest）模式下额外要求非空——该模式下没有执行目标的用例会被
 * 执行器逐条降级成 error，等批次跑完再回看"为什么全是异常"成本高得多。
 *
 * @param {string} rawValue 输入框原始值
 * @param {string|null} mode 当前执行器模式（simulated/pytest/null 未知）
 * @returns {string} 错误文案；合法时返回空串
 */
function validateSourceRefValue(rawValue, mode) {
    const value = String(rawValue || "").trim();
    const required = mode === EXECUTOR_MODE_PYTEST;
    if (!value) {
        return required ? "真实执行模式下执行目标必填" : "";
    }
    if (value.length > MAX_SOURCE_REF_LENGTH) {
        return "执行目标长度不能超过" + MAX_SOURCE_REF_LENGTH + "个字符";
    }
    if (!SOURCE_REF_PATTERN.test(value)) {
        return "执行目标格式非法：要求形如 tests/x.py 或 " +
            "tests/x.py::TestC::test_y（.py 结尾，不含空格）";
    }
    // 与 core 层一致：只看 :: 之前的路径段，禁上级目录
    const pathPart = value.split("::")[0];
    if (pathPart.split("/").indexOf("..") >= 0) {
        return "执行目标路径不得含上级目录 ..";
    }
    return "";
}

/**
 * 前端表单校验
 *
 * 覆盖：编号必填/长度、名称必填/长度、模块与创建人长度、描述长度上限、
 * 执行目标（格式 + 真实模式必填）。校验不过时不发起任何请求。
 *
 * @returns {{valid: boolean, errors: object}} 校验结果；errors 为
 *          {字段名: 错误文案}，valid=false 时非空
 */
function validateForm() {
    const errors = {};
    // 编号仅新增模式校验（编辑时控件 disabled，值不参与提交）
    if (state.editingCaseId === null) {
        const caseId = els.fCaseId.value.trim();
        if (!caseId) {
            errors.case_id = "用例编号必填";
        } else if (caseId.length > 64) {
            errors.case_id = "用例编号长度不能超过64个字符";
        } else if (!CASE_ID_PATTERN.test(caseId)) {
            // 与后端 schema 同一字符集口径：首字符须为字母或数字，
            // 后续允许字母数字与 ._-
            errors.case_id = "用例编号只能包含字母、数字、点、下划线与连字符，" +
                "且首字符必须为字母或数字";
        }
    }
    const name = els.fName.value.trim();
    if (!name) {
        errors.name = "用例名称必填";
    } else if (name.length > 200) {
        errors.name = "用例名称长度不能超过200个字符";
    }
    if (els.fModule.value.trim().length > 64) {
        errors.module = "模块长度不能超过64个字符";
    }
    if (els.fCreator.value.trim().length > 64) {
        errors.creator = "创建人长度不能超过64个字符";
    }
    if (els.fDescription.value.length > MAX_DESCRIPTION_LENGTH) {
        errors.description =
            "用例描述长度不能超过" + MAX_DESCRIPTION_LENGTH + "个字符";
    }
    const sourceRefError = validateSourceRefValue(
        els.fSourceRef.value,
        state.executorMode
    );
    if (sourceRefError) {
        errors.source_ref = sourceRefError;
    }
    return { valid: Object.keys(errors).length === 0, errors: errors };
}

/**
 * 收集表单为请求体（枚举值与文本 trim 后提交）
 *
 * source_ref 为空串时如实下发空串（后端归一为 None＝"未配置执行目标"），
 * 这样编辑时把执行目标清空能真的清掉，而不是"看起来清空了、库里还在"。
 *
 * @returns {object} 可直接 JSON 序列化的用例字段对象
 */
function collectFormData() {
    return {
        name: els.fName.value.trim(),
        module: els.fModule.value.trim() || "default", // 空值回落后端默认
        priority: els.fPriority.value,
        case_type: els.fCaseType.value,
        status: els.fStatus.value,
        description: els.fDescription.value,
        creator: els.fCreator.value.trim() || "admin",
        source_ref: els.fSourceRef.value.trim(),
    };
}

/**
 * 切换保存按钮 loading（禁用防重复提交）
 *
 * @param {boolean} loading 是否加载中
 * @returns {void}
 */
function setSaveLoading(loading) {
    els.saveBtn.disabled = loading;
    els.saveSpinner.classList.toggle("d-none", !loading);
}

/**
 * 提交表单（新增 POST / 编辑 PUT），含前端校验与后端字段错误回填
 *
 * @returns {Promise<void>} 无返回值；成功关窗并刷新列表
 */
async function submitForm() {
    clearAllFieldErrors();
    window.showError(els.formAlert, ""); // 清掉上一次提交失败留下的错误条
    const result = validateForm();
    if (!result.valid) {
        // 校验不过：字段级红框 + 文案，不发起任何请求（验收 4.12）。
        // 表驱动渲染（applyFieldErrors），新增字段无需再改这里。
        applyFieldErrors(result.errors);
        // 焦点移到第一个出错的字段：字段多时用户不必自己找哪一行红了
        const firstKey = Object.keys(result.errors)[0];
        const binding = FIELD_ERROR_BINDINGS[firstKey];
        if (binding && binding.field && binding.field.focus) {
            binding.field.focus();
        }
        return;
    }

    const payload = collectFormData();
    // 新增模式补 case_id；编辑模式不带该字段（后端 CaseUpdateSchema 也会忽略）
    if (state.editingCaseId === null) {
        payload.case_id = els.fCaseId.value.trim();
    }

    setSaveLoading(true);
    try {
        if (state.editingCaseId === null) {
            await window.api.post("/cases/", payload);
            window.showToast("用例保存成功");
        } else {
            await window.api.put(
                "/cases/" + encodeURIComponent(state.editingCaseId),
                payload
            );
            window.showToast("用例更新成功");
        }
        getFormModal().hide();
        await loadCases(); // 刷新当前页看到新增/更新结果
    } catch (error) {
        // 409 重复编号 / 400 校验失败 / 404 不存在：message 已由 api.js 解包，
        // 弹窗保留（不 hide）让用户可改正后重提，按钮在 finally 中恢复。
        // Day54：表单内显式错误区（含 HTTP 状态码）替代"只弹 5 秒 toast"——
        // toast 消失后用户看不到失败原因，只能重试碰运气。
        window.showError(els.formAlert, "保存失败：" + describeApiError(error));
        window.showToast(error.message, "danger");
    } finally {
        setSaveLoading(false);
    }
}

/* ==========================================================================
   2.6 删除确认
   ========================================================================== */

/**
 * 获取删除确认弹窗 Modal 单例
 *
 * @returns {bootstrap.Modal} Modal 实例
 */
function getDeleteModal() {
    if (!deleteModal) {
        deleteModal = new bootstrap.Modal(els.deleteModalEl);
    }
    return deleteModal;
}

/**
 * 打开删除确认弹窗并暂存待删编号
 *
 * @param {string} caseId 待删除用例编号
 * @returns {void}
 */
function openDeleteModal(caseId) {
    state.pendingDeleteId = caseId;
    els.deleteCaseIdText.textContent = caseId; // 编号经后端数据，写入 textContent 安全
    getDeleteModal().show();
}

/**
 * 确认删除：DELETE 成功后关窗刷新；失败 toast 提示保持弹窗
 *
 * @returns {Promise<void>} 无返回值
 */
async function confirmDelete() {
    if (!state.pendingDeleteId) {
        return;
    }
    const caseId = state.pendingDeleteId;
    els.confirmDeleteBtn.disabled = true; // 删除请求期间防重复点击
    try {
        await window.api.delete("/cases/" + encodeURIComponent(caseId));
        window.showToast("用例已删除");
        getDeleteModal().hide();
        state.pendingDeleteId = null;
        await loadCases(); // 当前页可能因删空自动回退一页
    } catch (error) {
        window.showToast(error.message, "danger");
    } finally {
        els.confirmDeleteBtn.disabled = false;
    }
}

/* ==========================================================================
   2.7 批量导入
   ========================================================================== */

/**
 * 上传用例数据文件（multipart/form-data）
 *
 * 绕过 window.api 的理由：api.js 对非 string 请求体统一 JSON.stringify，
 * 而文件上传必须以 multipart/form-data 发送且由浏览器自动生成
 * boundary；手动 JSON 化 FormData 会破坏上传协议，故此处直接 fetch
 * （本页唯一允许绕过 window.api 的场景）。路径仍以 /api/cases 开头，
 * 且不手设 Content-Type（交由浏览器带 multipart boundary）。
 *
 * @param {File} file 用户选择的 .yaml/.yml/.xlsx 文件
 * @returns {Promise<object>} 导入统计 {file_name,total,inserted,updated}
 * @throws {Error} 网络失败或 HTTP 非 2xx（message 取后端业务文案）
 */
async function importCases(file) {
    const formData = new FormData();
    formData.append("file", file);

    let response;
    try {
        response = await fetch("/api/cases/import", {
            method: "POST",
            body: formData,
        });
    } catch (networkError) {
        throw new Error("网络请求失败，请检查连接后重试：" + networkError.message);
    }

    // 手动按统一响应体 {code,message,data} 解包（api.js 未覆盖此场景）
    let payload = null;
    try {
        payload = await response.json();
    } catch (parseError) {
        throw new Error("导入响应解析失败，HTTP 状态码：" + response.status);
    }
    if (!response.ok) {
        throw new Error(
            (payload && payload.message) ||
                "导入失败，HTTP 状态码：" + response.status
        );
    }
    // 业务层防御：统一响应体 {code,message,data} 中 code 非成功值时，
    // 即使 HTTP 状态为 2xx 也按业务失败抛出。若只判 response.ok，
    // 后端一旦改用「HTTP 200 + code=500」风格返回错误，这里会静默
    // 返回 data=null，调用方回落成「新增0条，更新0条」的假成功 toast，
    // 用户以为导入完成实则数据未入库。
    // 成功判据与 api.js 统一为区间 code >= 0 && code < 400（6.36 决策2）：
    // 本项目 code 与 HTTP 状态码同值，成功码是 0 与全部 2xx，枚举白名单
    // （只认 0/200）会在导入改用 created() 返回 201 时把成功判成失败；
    // 响应体无 code 字段时（如后端简化契约）不做拦截，保持向后兼容。
    if (
        payload &&
        payload.code !== undefined &&
        payload.code !== null &&
        !(payload.code >= 0 && payload.code < 400)
    ) {
        throw new Error(
            payload.message || "导入失败，业务错误码：" + payload.code
        );
    }
    return payload ? payload.data : null;
}

/* ==========================================================================
   2.8 事件绑定与初始化
   ========================================================================== */

/**
 * 绑定页面全部事件并触发首次列表加载
 *
 * @returns {void}
 */
function initCasesPage() {
    // --- 筛选下拉：变化即重置页码重查（状态默认 active 已在 HTML 选中） ---
    [
        els.filterModule,
        els.filterPriority,
        els.filterType,
        els.filterStatus,
    ].forEach(function (selectEl) {
        selectEl.addEventListener("change", function () {
            syncFiltersAndResetPage();
            loadCases();
        });
    });

    // 关键字：回车触发；逐键 input 不查询（避免无意义请求）
    els.filterKeyword.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
            event.preventDefault();
            syncFiltersAndResetPage();
            loadCases();
        }
    });
    els.searchBtn.addEventListener("click", function () {
        syncFiltersAndResetPage();
        loadCases();
    });

    // 重置：控件回默认 → state 回默认 → 第 1 页重查
    els.resetBtn.addEventListener("click", function () {
        resetFilters();
        loadCases();
    });

    // 每页条数变化：重置第 1 页
    els.pageSizeSelect.addEventListener("change", function (event) {
        const newSize = parseInt(event.target.value, 10) || 20;
        // 列表在途：登记待补刷（改页大小要回第 1 页），否则下拉框已显示
        // 新值而列表仍是旧数据，直到用户再次操作才生效
        if (state.loading) {
            state.pendingPageSize = newSize;
            return;
        }
        state.pageSize = newSize;
        state.page = 1;
        loadCases();
    });

    // --- 分页事件委托：页码/上一页/下一页统一在容器上处理 ---
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
        let targetPage = state.page;
        if (action === "prev" && state.page > 1) {
            targetPage = state.page - 1;
        } else if (action === "next" && state.page < state.totalPages) {
            targetPage = state.page + 1;
        } else if (action === "page") {
            const parsed = parseInt(link.getAttribute("data-page"), 10);
            if (!isNaN(parsed)) {
                targetPage = parsed;
            }
        }
        if (targetPage === state.page) {
            return;
        }
        // 列表在途：登记待补刷的目标页，锁释放后自动跳过去
        // （与每页条数变更的 pendingPageSize 同一机制；修复前此处撞上
        //  state.loading 直接 return，加载中点页码被静默丢弃）
        if (state.loading) {
            state.pendingPage = targetPage;
            return;
        }
        state.page = targetPage;
        loadCases();
    });

    // --- 列表操作列事件委托：编辑/删除 ---
    els.tableBody.addEventListener("click", function (event) {
        const btn = event.target.closest("button[data-action]");
        if (!btn) {
            return;
        }
        const caseId = btn.getAttribute("data-case-id");
        const action = btn.getAttribute("data-action");
        if (action === "edit") {
            openEditModal(caseId);
        } else if (action === "delete") {
            openDeleteModal(caseId);
        }
    });

    // --- 新增按钮 ---
    els.createBtn.addEventListener("click", openCreateModal);

    // --- 表单提交拦截 ---
    els.caseForm.addEventListener("submit", function (event) {
        event.preventDefault(); // 阻止原生表单提交导致的页面跳转/刷新
        submitForm();
    });

    // --- 输入即清除该字段错误态（Day54）：改正内容后红字必须立刻消失 ---
    els.caseForm.addEventListener("input", function (event) {
        clearFieldErrorForElement(event.target);
    });

    // --- 字段即时校验（Day54）：失焦即校验单一字段 ---
    // 只回填被触发字段的错误，避免用户刚开始填就看到其余空字段铺满红字
    els.fCaseId.addEventListener("blur", function () {
        revalidateField("case_id", validateForm().errors);
    });
    els.fSourceRef.addEventListener("blur", function () {
        revalidateField("source_ref", validateForm().errors);
    });

    // --- 列表错误态重试（Day54）：错误态不再只有"刷新整页"一条路 ---
    // loadCases 内部自带加载态、错误态与互斥锁，直接复用即可
    els.listErrorRetryBtn.addEventListener("click", function () {
        loadCases();
    });

    // --- 删除确认 ---
    els.confirmDeleteBtn.addEventListener("click", confirmDelete);

    // --- 批量导入：按钮触发文件选择，change 后上传 ---
    els.importBtn.addEventListener("click", function () {
        els.importFileInput.click();
    });
    els.importFileInput.addEventListener("change", async function () {
        const file = els.importFileInput.files && els.importFileInput.files[0];
        if (!file) {
            return;
        }
        // 后缀白名单与后端一致（.yaml/.yml/.xlsx），前置拦截非法文件
        const lowerName = file.name.toLowerCase();
        if (!/\.(yaml|yml|xlsx)$/.test(lowerName)) {
            window.showToast("仅支持 .yaml/.yml/.xlsx 文件", "danger");
            els.importFileInput.value = "";
            return;
        }
        try {
            const stats = await importCases(file);
            const inserted = stats && stats.inserted !== undefined ? stats.inserted : 0;
            const updated = stats && stats.updated !== undefined ? stats.updated : 0;
            window.showToast("导入完成：新增" + inserted + "条，更新" + updated + "条");
            resetFilters(); // 导入后可能新增任意模块/状态，重置视角回到默认列表
            await loadCases();
        } catch (error) {
            window.showToast(error.message, "danger");
        } finally {
            // 清空 value，否则重复选择同一文件不再触发 change
            els.importFileInput.value = "";
        }
    });

    // --- 执行器模式回读：决定"执行目标"是否必填（Day54） ---
    // 不 await：模式读取失败只影响必填口径，不该拖慢首屏列表
    loadExecutorModeForForm();

    // --- 首次加载（state 默认 status=active、page=1、pageSize=20） ---
    loadCases();
}

/* ==========================================================================
   2.9 执行器模式回读（Day54）
   ========================================================================== */

/**
 * 读取当前执行器模式，用于刷新表单必填口径
 *
 * 为什么必须回读而不能本地推断：模式是进程级运行时选择（Day53 落在
 * TM_EXECUTOR 环境变量上），页面脚本没有别的途径知道"现在是不是真实
 * 执行"。读失败时按"未知"处理——**不施加必填约束**：宁可少一次前端
 * 拦截，也不能因为读不到模式就把一条合法用例卡死在提交前（后端仍有
 * 独立校验，真实模式缺执行目标最终会由执行器如实报 error）。
 *
 * @returns {Promise<void>} 无返回值（失败不抛出，只退化必填口径）
 */
async function loadExecutorModeForForm() {
    try {
        const data = await window.api.get(EXECUTOR_MODE_API);
        state.executorMode = data && data.mode ? data.mode : null;
    } catch (error) {
        state.executorMode = null;
    }
    renderSourceRefModeHint();
}

/**
 * 刷新"执行目标"字段的模式提示徽章文案
 *
 * @returns {void}
 */
function renderSourceRefModeHint() {
    if (!els.fSourceRefModeHint) {
        return;
    }
    els.fSourceRefModeHint.textContent =
        state.executorMode === EXECUTOR_MODE_PYTEST
            ? "当前真实模式：必填"
            : "真实模式下必填";
}

// DOM 就绪后初始化（脚本位于 body 底部 extra_js，双保险无时序问题）
document.addEventListener("DOMContentLoaded", initCasesPage);

// 显式导出，便于控制台联调
window.casesPage = {
    state: state,
    loadCases: loadCases,
    openCreateModal: openCreateModal,
    openEditModal: openEditModal,
    confirmDelete: confirmDelete,
    importCases: importCases,
    validateForm: validateForm,
    loadExecutorModeForForm: loadExecutorModeForForm,
    // 保留导出以不改变本模块的公开面：本地名改为 escHtml 后，导出键
    // 仍是 escapeHtml（6.33 决策——收敛的是实现唯一性，不是模块 API 面），
    // 其值指向 window.escapeHtml，该赋值是幂等的
    escapeHtml: escHtml,
    buildPageSequence: buildPageSequence,
};
