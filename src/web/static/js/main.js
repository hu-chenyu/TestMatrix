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
    toastInner.className = "d-flex";

    const toastBody = document.createElement("div");
    toastBody.className = "toast-body";
    // 纯文本赋值：即使 message 含 <script> 也只作为字面量显示，不被解析执行
    toastBody.textContent = message;

    // 关闭按钮：结构固定无用户数据，用 DOM API 保持与原结构完全一致
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "btn-close btn-close-white me-2 m-auto";
    closeBtn.setAttribute("data-bs-dismiss", "toast");
    closeBtn.setAttribute("aria-label", "关闭");

    toastInner.appendChild(toastBody);
    toastInner.appendChild(closeBtn);
    toastEl.appendChild(toastInner);
    container.appendChild(toastEl);

    // 实例化 Toast 并展示，3 秒自动消失；关闭后移除节点避免 DOM 堆积
    const toast = new bootstrap.Toast(toastEl, { delay: 3000 });
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
