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
  * textContent——首选 textContent/ createElement，本函数仅用于"必须产出
  * HTML 字符串"的外部 API（如 ECharts tooltip formatter，其返回值会被
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
  *    /warning 警告（黄）/info 提示（蓝）；无法识别的取值降级为 success
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
    //  避免与 base.html 中 active_nav 逻辑重复加类
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
  * 页面内错误提示条的统一显隐入口（Day54）
  *
  * 为什么要有它：三个页面此前各写一套「classList.toggle('d-none') +
  * 手工定位文案节点」，同一语义三份实现（与 6.37 前端共享工具下沉同源：
  * 只改一处即产生分叉）。提示条内的文案节点约定用
  * `data-role="error-text"` 标记，本函数按标记定位；找不到标记时退回
  * 容器内第一个 <span>，保证既有结构不改也能用。
  *
  * @param {string|HTMLElement} target 提示条容器（元素对象或其 id）
  * @param {string} [message] 错误文案；空串/null/undefined 表示隐藏提示条
  * @returns {boolean} 是否作用到容器（容器不存在返回 false）
  */
window.showError = function (target, message) {
    const container =
        typeof target === "string" ? document.getElementById(target) : target;
    if (!container) {
        return false;
    }
    const textEl =
        container.querySelector('[data-role="error-text"]') ||
        container.querySelector("span");
    // 只在展示时改写文案：隐藏路径不覆盖上一次内容，便于排查时回看
    if (textEl && message) {
        textEl.textContent = message;
    }
    container.classList.toggle("d-none", !message);
    return true;
};

/**
  * 错误提示条配套「重试」按钮的统一显隐（Day54）
  *
  * 重试按钮只在错误态出现：无错时留在页面上会让用户误以为"这里有操作
  * 入口"，点了却只是重放一次正常请求。与 showError 同时调用即可。
  *
  * @param {string|HTMLElement} button 重试按钮（元素对象或其 id）
  * @param {boolean} visible 是否可见
  * @returns {boolean} 是否作用到按钮（按钮不存在返回 false）
  */
window.showRetry = function (button, visible) {
    const btn = typeof button === "string" ? document.getElementById(button) : button;
    if (!btn) {
        return false;
    }
    btn.classList.toggle("d-none", !visible);
    btn.disabled = false; // 每次显隐都复位禁用态，防上一轮 loading 残留
    return true;
};

/**
  * 按钮 loading 态统一开关（Day54）
  *
  * 语义固定为「禁用 + 换文案」，用于重试/刷新这类会重复发起请求的按钮：
  * 只置 disabled 而不换文案时，用户看到的是一个毫无反馈的灰按钮。
  * 原文案在首次进入 loading 时存进 dataset，退出时还原。
  *
  * @param {string|HTMLElement} button 目标按钮（元素对象或其 id）
  * @param {boolean} isLoading 是否处于加载中
  * @param {string} [loadingText] 加载中文案，缺省「处理中...」
  * @returns {boolean} 是否作用到按钮（按钮不存在返回 false）
  */
window.setButtonLoading = function (button, isLoading, loadingText) {
    const btn = typeof button === "string" ? document.getElementById(button) : button;
    if (!btn) {
        return false;
    }
    if (isLoading) {
        // 只在进入 loading 时记录原文案，避免被"处理中..."反复覆盖
        if (btn.dataset.tmOriginalText === undefined) {
            btn.dataset.tmOriginalText = btn.textContent;
        }
        btn.textContent = loadingText || "处理中...";
        btn.disabled = true;
    } else {
        if (btn.dataset.tmOriginalText !== undefined) {
            btn.textContent = btn.dataset.tmOriginalText;
            delete btn.dataset.tmOriginalText;
        }
        btn.disabled = false;
    }
    return true;
};

/**
  * 表单字段错误态的统一开关（Day54）
  *
  * 用例页与执行记录页的表单校验共用同一动作：控件加 .is-invalid（Bootstrap
  * 靠它显示同级的 .invalid-feedback）+ 错误容器写入文案。两页各写一套时
  * 极易只改一处，出现"红框有、文案没"或反之的半截状态。
  *
  * @param {string|HTMLElement} field 表单控件（元素对象或其 id）
  * @param {string|HTMLElement} errorTarget 错误容器（元素对象或其 id）
  * @param {string} [message] 错误文案；空值表示清除该字段的错误态
  * @returns {boolean} 是否作用到控件（控件或容器缺失返回 false）
  */
window.setFieldError = function (field, errorTarget, message) {
    const control =
        typeof field === "string" ? document.getElementById(field) : field;
    const box =
        typeof errorTarget === "string"
            ? document.getElementById(errorTarget)
            : errorTarget;
    if (!control || !box) {
        return false;
    }
    if (message) {
        control.classList.add("is-invalid");
        box.textContent = message;
    } else {
        control.classList.remove("is-invalid");
        box.textContent = "";
    }
    return true;
};

/**
  * 把 api.js 抛出的错误对象转成含 HTTP 状态码的可读文案（Day54）
  *
  * 为什么带上状态码：后端业务 message 有时很笼统（"参数校验失败"），
  * 而 400 / 404 / 409 / 500 的处置方式完全不同（改输入 / 换目标 /
  * 换编号 / 等重试）。状态码是 api.js 挂在 Error 上的协议层事实，
  * 不依赖后端文案措辞，也不会被文案改版打偏。
  *
  * @param {*} error 捕获到的错误（可能是 Error，也可能是被抛出的任意值）
  * @returns {string} 形如 "HTTP 409 · 用例编号已存在"；无状态码时为纯 message
  */
window.describeApiError = function (error) {
    const message =
        error && error.message ? String(error.message) : "未知错误";
    const status = error && error.status ? String(error.status) : "";
    return status ? "HTTP " + status + " · " + message : message;
};

// ===========================================================================
// 全局错误兜底（Day54）
// ===========================================================================
// 为什么需要：页面脚本里任何一处漏掉的 await / 未捕获异常，浏览器只在
// 控制台留一行红字，用户看到的是"点了没反应"。这里把两类全局错误转成
// 一次可见提示，让"静默失败"在 UI 上也有落点。
// 去重上界：同一消息只提示一次且总量封顶，防止循环报错把页面刷成 toast 墙。

// 已提示过的全局错误文案（去重，上界 20 条）
const TM_GLOBAL_ERROR_SEEN = [];
const TM_GLOBAL_ERROR_MAX = 20;

/**
  * 全局错误去重并提示（内部方法）
  *
  * @param {string} message 错误文案
  * @returns {void}
  */
function reportGlobalError(message) {
    const text = message || "页面发生未知错误";
    if (TM_GLOBAL_ERROR_SEEN.indexOf(text) >= 0) {
        return;
    }
    if (TM_GLOBAL_ERROR_SEEN.length >= TM_GLOBAL_ERROR_MAX) {
        return;
    }
    TM_GLOBAL_ERROR_SEEN.push(text);
    showToast("页面发生未预期的错误：" + text, "danger");
}

// 资源加载/脚本执行期异常（含语法错误的运行时表现）
window.addEventListener("error", function (event) {
    // 只处理脚本/运行时错误；资源加载失败（img/script 的 target 无 message）
    // 交给浏览器控制台与 Log 通道，避免把外部库加载噪声变成用户提示
    if (event && event.message) {
        reportGlobalError(event.message);
    }
});

// 未被 catch 的 Promise 拒绝（async 函数漏 await 时的典型表现）
window.addEventListener("unhandledrejection", function (event) {
    const reason = event && event.reason;
    const message = reason && reason.message ? reason.message : String(reason);
    reportGlobalError(message);
    // 阻止浏览器把同一条未处理拒绝再打一遍控制台 error：
    // 提示已经给出，重复输出只会污染"控制台零 error"的判据
    //
    // 走查等效判据：preventDefault 抑制了浏览器默认的控制台 error 输出，
    // 因此浏览器走查时必须同时观察"是否弹出全局错误 toast（reportGlobalError）"
    // 作为控制台零 error 的等效判据——toast 弹出即等同于有未捕获错误。
    event.preventDefault();
});

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
