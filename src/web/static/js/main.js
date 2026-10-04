/* ==========================================================================
   TestMatrix 公共前端脚本
   职责：
     1. 通用工具函数：formatDate（ISO 时间转可读格式）、escapeHtml（HTML 转义）；
     2. showToast：基于 Bootstrap5 Toast 的多态消息提示；
     3. DOMContentLoaded 后初始化全局 toast 容器，并对导航当前项做
        active 高亮兜底（服务端已按 active_nav 渲染时不重复处理）。
   ========================================================================== */

/**
 * ISO 时间字符串转本地可读格式（YYYY-MM-DD HH:mm:ss）
 *
 * @param {string} isoString ISO 8601 时间字符串（如后端返回的 created_at）
 * @returns {string} 格式化后的时间文本；入参为空或非法时返回 "-"
 */
function formatDate(isoString) {
    // 空值（null/undefined/空串）统一显示占位符，保证表格不出现空白错乱
    if (!isoString) {
        return "-";
    }
    const date = new Date(isoString);
    // 非法日期对象 getTime 返回 NaN，同样降级为占位符
    if (isNaN(date.getTime())) {
        return "-";
    }
    // 逐段补零，避免 padStart 兼容问题的写法足够直观
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    const hours = String(date.getHours()).padStart(2, "0");
    const minutes = String(date.getMinutes()).padStart(2, "0");
    const seconds = String(date.getSeconds()).padStart(2, "0");
    return year + "-" + month + "-" + day + " " + hours + ":" + minutes + ":" + seconds;
}

/**
 * HTML 特殊字符转义（XSS 防御单一入口）
 *
 * 任何把后端数据拼进 HTML 字符串的汇点都必须先过这里。注意这不能替代
 * textContent —— 首选 textContent/ createElement，本函数仅用于"必须产出
 * HTML 字符串"的第三方 API（如 ECharts tooltip formatter，其返回值会被
 * 当作 HTML 直接注入 tooltip DOM）。
 *
 * @param {*} value 任意值（null/undefined 归一为空串）
 * @returns {string} 转义后的纯文本，可安全拼入 HTML
 */
function escapeHtml(value) {
    if (value === null || value === undefined) {
        return "";
    }
    return String(value).replace(/[&<>"']/g, function (char) {
        return {
            "&": "&amp;",
            "<": "&lt;",
            ">": "&gt;",
            '"': "&quot;",
            "'": "&#39;",
        }[char];
    });
}

// showToast 支持的语义类型（Bootstrap5 text-bg-* 配色，warning/info 为官方内置）
const TM_TOAST_TYPES = ["success", "danger", "warning", "info"];

/**
 * 各类型 toast 的图标与自动消失时长（Day44 UX 统一）
 *
 * 时长分级：成功/普通信息 3 秒（用户扫一眼即可），警告/失败 5 秒
 * （错误文案通常更长，且用户需要时间读完并处理）；两种异常态同时
 * 保留右上角手动关闭按钮，不强制等待自动消失。
 */
const TM_TOAST_ICONS = {
    success: "bi-check-circle-fill",
    danger: "bi-x-circle-fill",
    warning: "bi-exclamation-triangle-fill",
    info: "bi-info-circle-fill",
};
const TM_TOAST_DELAY_MS = {
    success: 3000,
    info: 3000,
    warning: 5000,
    danger: 5000,
};

/**
 * 展示 Bootstrap5 Toast 浮动消息
 *
 * @param {string} message 消息正文
 * @param {string} [type="success"] 消息类型：success 成功（绿）/danger 失败（红）
 *        /warning 警告（黄）/info 提示（蓝）；无法识别的取值降级为 success
 * @returns {void}
 */
function showToast(message, type) {
    // 白名单归一化：未显式指定或取值非法时按成功态处理。
    // 修复前只映射 danger/success 两态，导致调用方传 "warning" 时被渲染成
    // 绿色成功样式，却配着"加载失败"文案，告警在视觉上被彻底弱化。
    type = TM_TOAST_TYPES.indexOf(type) >= 0 ? type : "success";

    // 取初始化阶段创建的全局容器，不存在则兜底直接返回（非 DOM 环境保护）
    const container = document.getElementById("tm-toast-container");
    if (!container) {
        return;
    }

    // 按 Bootstrap5 Toast 结构创建节点，text-bg-* 控制成功/失败配色
    const toastEl = document.createElement("div");
    toastEl.className =
        "toast align-items-center text-bg-" + type + " border-0";
    toastEl.setAttribute("role", "alert");
    // 用 DOM API 构造 toast 内容：message 走 textContent，浏览器自动转义
    // < > & " 等字符。改用 createElement 是为消除字符串拼接式 HTML 赋值
    // 形成的 XSS 汇点——调用方（cases.js 等）会把后端 error.message 喂进来，
    // 一旦后端错误文案回显用户输入，拼接过来的 message 就会被解析执行
    const toastInner = document.createElement("div");
    toastInner.className = "d-flex align-items-center w-100";

    // 类型图标：类名取自固定白名单映射，用户文本绝不进入 className
    const iconEl = document.createElement("i");
    iconEl.className = "bi " + TM_TOAST_ICONS[type] + " ms-3 me-2";
    iconEl.setAttribute("aria-hidden", "true");

    const toastBody = document.createElement("div");
    toastBody.className = "toast-body flex-grow-1";
    // 纯文本赋值：即使 message 含 <script> 也只作为字面量显示，不被解析执行
    toastBody.textContent = message;

    // 关闭按钮：结构固定无用户数据，用 DOM API 保持与原结构完全一致
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "btn-close btn-close-white me-2 m-auto";
    closeBtn.setAttribute("data-bs-dismiss", "toast");
    closeBtn.setAttribute("aria-label", "关闭");

    toastInner.appendChild(iconEl);
    toastInner.appendChild(toastBody);
    toastInner.appendChild(closeBtn);
    toastEl.appendChild(toastInner);
    container.appendChild(toastEl);

    // 实例化 Toast 并展示：成功/信息 3 秒，警告/失败 5 秒；
    // 关闭后移除节点避免 DOM 堆积
    const toast = new bootstrap.Toast(toastEl, {
        delay: TM_TOAST_DELAY_MS[type],
    });
    toastEl.addEventListener("hidden.bs.toast", function () {
        toastEl.remove();
    });
    toast.show();
}

// DOM 就绪后执行容器初始化与导航兜底，保证脚本放在 body 底部也无时序问题
document.addEventListener("DOMContentLoaded", function () {
    // 1. 初始化全局 toast 容器（页面唯一，固定在顶部居中偏下）
    if (!document.getElementById("tm-toast-container")) {
        const container = document.createElement("div");
        container.id = "tm-toast-container";
        container.className =
            "toast-container position-fixed top-0 start-50 " +
            "translate-middle-x p-3";
        container.style.zIndex = "1080";
        document.body.appendChild(container);
    }

    // 2. 导航当前项高亮兜底：仅当服务端未渲染任何 active 时才处理，
    //    避免与 base.html 中 active_nav 逻辑重复加类
    const navLinks = document.querySelectorAll(".navbar-nav .nav-link");
    let hasActive = false;
    navLinks.forEach(function (link) {
        if (link.classList.contains("active")) {
            hasActive = true;
        }
    });
    if (!hasActive) {
        const currentPath = window.location.pathname;
        navLinks.forEach(function (link) {
            if (link.getAttribute("href") === currentPath) {
                link.classList.add("active");
            }
        });
    }
});

// 显式暴露工具函数，供各页面脚本调用
// escapeHtml 必须挂到 window：main.js 是 base.html 唯一全站加载的脚本，
// 是 XSS 转义能力的唯一公共入口（dashboard.html 不加载 cases.js）
window.formatDate = formatDate;
window.showToast = showToast;
window.escapeHtml = escapeHtml;

// ===========================================================================
// 跨页面共享的纯展示工具（Day44 收尾下沉）
// ===========================================================================
// 为什么用 window.xxx = function(){} 而不是顶层 function xxx(){}：
// 顶层 function 声明会创建**不可配置**的全局对象属性，页面脚本再用同名
// const 声明会触发 GlobalDeclarationInstantiation 的 SyntaxError，整个
// 页面脚本不执行（cases.js 的 escapeHtml 事故即此因，6.33/7.36 坑 1）。
// 赋值形式创建的是可配置属性，页面脚本 `const xxx = window.xxx` 安全。
// 页面脚本一律用异名 const 引用，不再各自持有实现（6.33：收敛的是实现
// 唯一性，不是模块 API 面）。

/**
 * 状态徽章 HTML（用例启停状态 + 批次执行状态共用）
 *
 * 两个域的状态值都在下表里，用例页的 active/disabled 原样显示英文
 * （保持 Day42 既有呈现），批次页的 finished/failed 等显示中文。
 * 未知值降级为灰色徽章并原样显示入参。
 *
 * @param {string} status active/disabled/finished/failed/running/pending
 * @returns {string} Bootstrap badge HTML 片段
 */
window.statusBadge = function (status) {
    const styleMap = {
        // 用例启停域（cases.js）：active 绿 / disabled 灰
        active: { cls: "text-bg-success", text: "active" },
        disabled: { cls: "text-bg-secondary", text: "disabled" },
        // 批次执行域（executions.js）：完成绿 / 失败红 / 执行中蓝 / 等待中黄
        finished: { cls: "text-bg-success", text: "已完成" },
        failed: { cls: "text-bg-danger", text: "失败" },
        running: { cls: "text-bg-primary", text: "执行中" },
        pending: { cls: "text-bg-warning", text: "等待中" },
        // 单用例执行结果域中与状态同值的 skipped
        skipped: { cls: "text-bg-secondary", text: "跳过" },
    };
    // 判自有属性而非直接查表：直接 map[status] 会命中 Object.prototype 上的
    // 键（constructor/toString 等）并把函数对象拼进 class 属性
    const conf = Object.prototype.hasOwnProperty.call(styleMap, status)
        ? styleMap[status]
        : null;
    const cls = conf ? conf.cls : "text-bg-secondary";
    const text = conf ? conf.text : status || "未知";
    return '<span class="badge ' + cls + '">' + escapeHtml(text) + "</span>";
};

/**
 * 分页序列"全部显示"的阈值（总页数 ≤ 该值时不插省略号）
 */
const PAGE_SEQUENCE_FULL_THRESHOLD = 7;

/**
 * 计算分页页码序列（cases.js / executions.js 共用同一实现）
 *
 * 规则（验收 1.4）：总页数 ≤7 全部显示；>7 时显示首页、末页、
 * 当前页前后各 1 页，其余位置用省略号占位。
 *
 * @param {number} current 当前页
 * @param {number} totalPages 总页数
 * @returns {Array<number|string>} 页码序列，省略号位为 "..."
 */
window.buildPageSequence = function (current, totalPages) {
    if (totalPages <= PAGE_SEQUENCE_FULL_THRESHOLD) {
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
};
