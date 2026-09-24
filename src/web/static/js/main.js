/* ==========================================================================
   TestMatrix 公共前端脚本（Day35 前端骨架）
   职责：
     1. 通用工具函数：formatDate（ISO 时间转可读格式）；
     2. showToast：基于 Bootstrap5 Toast 的成功/失败两态消息提示；
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
 * 展示 Bootstrap5 Toast 浮动消息（成功/失败两态）
 *
 * @param {string} message 消息正文
 * @param {string} [type="success"] 消息类型：success 成功（绿）/danger 失败（红）
 * @returns {void}
 */
function showToast(message, type) {
    // 未显式指定类型时按成功态处理
    type = type === "danger" ? "danger" : "success";

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
    toastEl.innerHTML =
        '<div class="d-flex">' +
        '<div class="toast-body">' + message + "</div>" +
        '<button type="button" class="btn-close btn-close-white me-2 m-auto" ' +
        'data-bs-dismiss="toast" aria-label="关闭"></button>' +
        "</div>";
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

// 显式暴露工具函数，供 Day36+ 各页面脚本调用
window.formatDate = formatDate;
window.showToast = showToast;
