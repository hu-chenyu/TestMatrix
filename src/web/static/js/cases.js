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
    knownModules: [],     // 历次列表累积发现的模块名（跨页填充模块下拉）
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
    fPriority: document.getElementById("fPriority"),
    fCaseType: document.getElementById("fCaseType"),
    fStatus: document.getElementById("fStatus"),
    fDescription: document.getElementById("fDescription"),
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
 * @param {*} value 任意输入值（非字符串按空串处理）
 * @returns {string} 转义后的安全字符串（null/undefined 返回空串）
 */
function escapeHtml(value) {
    if (value === null || value === undefined) {
        return "";
    }
    return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

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
    const cls = colorMap[priority] || "text-bg-secondary";
    return '<span class="badge ' + cls + '">' + escapeHtml(priority) + "</span>";
}

/**
 * 生成状态 badge 的 HTML
 *
 * @param {string} status active/disabled（其他值降级为次级灰 badge）
 * @returns {string} 带 Bootstrap badge 类的 HTML 字符串
 */
function statusBadge(status) {
    // active 绿（启用）/ disabled 灰（停用），与验收 4.23 一致
    const cls = status === "active" ? "text-bg-success" : "text-bg-secondary";
    return '<span class="badge ' + cls + '">' + escapeHtml(status) + "</span>";
}

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
 * 切换列表错误提示条显隐
 *
 * @param {boolean} show 是否显示
 * @param {string} [message] 错误文案（show=true 时生效）
 * @returns {void}
 */
function setListError(show, message) {
    if (show && message) {
        els.listErrorText.textContent = message;
    }
    els.listErrorAlert.classList.toggle("d-none", !show);
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
        '<i class="bi bi-inbox me-1"></i>暂无用例数据</td></tr>';
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
            const safeId = escapeHtml(item.case_id);
            return (
                "<tr>" +
                '<td class="fw-semibold text-nowrap">' + safeId + "</td>" +
                "<td>" + escapeHtml(item.name) + "</td>" +
                "<td>" + escapeHtml(item.module) + "</td>" +
                "<td>" + priorityBadge(item.priority) + "</td>" +
                "<td>" + escapeHtml(item.case_type) + "</td>" +
                "<td>" + statusBadge(item.status) + "</td>" +
                "<td>" + escapeHtml(item.creator) + "</td>" +
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
 * 规则（验收 1.4）：总页数 ≤7 全部显示；>7 时显示首页、末页、
 * 当前页前后各 1 页，其余位置用省略号占位。
 *
 * @param {number} current 当前页
 * @param {number} totalPages 总页数
 * @returns {Array<number|string>} 页码序列，省略号位为 "..."
 */
function buildPageSequence(current, totalPages) {
    if (totalPages <= 7) {
        // 1..N 全部显示
        const all = [];
        for (let i = 1; i <= totalPages; i++) {
            all.push(i);
        }
        return all;
    }
    // 从含首页、末页、当前页±1 的集合出发，排序后在缺口处插省略号
    const pages = new Set([1, totalPages, current - 1, current, current + 1]);
    const valid = [];
    pages.forEach(function (p) {
        if (p >= 1 && p <= totalPages) {
            valid.push(p);
        }
    });
    valid.sort(function (a, b) {
        return a - b;
    });
    const sequence = [];
    let prev = 0;
    valid.forEach(function (p) {
        if (p - prev > 1) {
            sequence.push("..."); // 相邻页码不连续 → 省略号
        }
        sequence.push(p);
        prev = p;
    });
    return sequence;
}

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
                return '<option value="' + escapeHtml(name) + '">' +
                    escapeHtml(name) + "</option>";
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
        // 错误态：页内 alert 明示原因 + 表格空态占位，不白屏（验收 4.21）
        // 同时重置分页计数：否则页脚仍显示上一次的"共 137 条"+ 空表格，
        // 用户看到自相矛盾的画面，易误判为数据丢失
        state.total = 0;
        state.totalPages = 0;
        setListError(true, "用例列表加载失败：" + error.message);
        renderEmptyRow();
        renderPagination();
    } finally {
        state.loading = false;
        // 二次刷新分页按钮禁用态（成功/失败都要解锁）
        renderPagination();
        // 锁释放后补刷被登记的刷新请求
        if (state.pendingRefresh) {
            state.pendingRefresh = false;
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
 * 清空全部字段错误态
 *
 * @returns {void}
 */
function clearAllFieldErrors() {
    clearFieldError(els.fCaseId, document.getElementById("fCaseIdError"));
    clearFieldError(els.fName, document.getElementById("fNameError"));
    clearFieldError(els.fModule, document.getElementById("fModuleError"));
    clearFieldError(els.fCreator, document.getElementById("fCreatorError"));
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
    els.fDescription.value = "";
    clearAllFieldErrors();
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
 * 前端表单校验
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
    return { valid: Object.keys(errors).length === 0, errors: errors };
}

/**
 * 收集表单为请求体（枚举值与文本 trim 后提交）
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
    const result = validateForm();
    if (!result.valid) {
        // 校验不过：字段级红框 + 文案，不发起任何请求（验收 4.12）
        if (result.errors.case_id) {
            setFieldError(
                els.fCaseId,
                document.getElementById("fCaseIdError"),
                result.errors.case_id
            );
        }
        if (result.errors.name) {
            setFieldError(
                els.fName,
                document.getElementById("fNameError"),
                result.errors.name
            );
        }
        if (result.errors.module) {
            setFieldError(
                els.fModule,
                document.getElementById("fModuleError"),
                result.errors.module
            );
        }
        if (result.errors.creator) {
            setFieldError(
                els.fCreator,
                document.getElementById("fCreatorError"),
                result.errors.creator
            );
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
        // 弹窗保留（不 hide）让用户可改正后重提，按钮在 finally 中恢复
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
    // 用户以为导入完成实则数据未入库。code 兼容 0/200 两种成功约定；
    // 响应体无 code 字段时（如后端简化契约）不做拦截，保持向后兼容。
    if (
        payload &&
        payload.code !== undefined &&
        payload.code !== null &&
        payload.code !== 0 &&
        payload.code !== 200
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
    els.pageSizeSelect.addEventListener("change", function () {
        state.pageSize = parseInt(els.pageSizeSelect.value, 10) || 20;
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
        if (state.loading || link.parentElement.classList.contains("disabled")) {
            return; // 加载中/禁用态不响应
        }
        const action = link.getAttribute("data-action");
        if (action === "prev" && state.page > 1) {
            state.page -= 1;
        } else if (action === "next" && state.page < state.totalPages) {
            state.page += 1;
        } else if (action === "page") {
            const targetPage = parseInt(link.getAttribute("data-page"), 10);
            if (!isNaN(targetPage)) {
                state.page = targetPage;
            }
        }
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

    // --- 首次加载（state 默认 status=active、page=1、pageSize=20） ---
    loadCases();
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
    escapeHtml: escapeHtml,
    buildPageSequence: buildPageSequence,
};
