/* ==========================================================================
   TestMatrix 统一 API 请求封装（Day35 前端骨架，Day36+ 各页面复用）
   约定：
     1. 所有数据接口统一挂在 /api 命名空间下，与 HTML 页面路由严格分离；
     2. 后端响应体统一为 {code, message, data}，本模块成功时直接解包 data；
     3. HTTP 非 2xx 或网络异常均抛出 Error（message 优先取后端 message），
        由调用方按业务场景展示 toast 或兜底视图。
   ========================================================================== */

// API 统一前缀：页面只传相对路径（如 "/cases/"），前缀在此处统一拼接
const API_BASE = "/api";

// 统一请求超时（毫秒，Day44 P2-09）：修复前 fetch 完全不设超时，
// 数据库锁等待、网络黑洞或代理挂起时 await 永不返回——看板的刷新按钮、
// 用例页的保存/删除确认、触发的提交按钮会一直停在 disabled + spinner，
// 没有任何错误提示，用户唯一能做的只有刷新整个页面。服务端只有
// /health 有 3 秒探测超时，列表与统计接口无任何上限。
// 取 30000 的依据：覆盖"后端一次冷启动/慢查询"的正常上限，又不至于
// 让用户干等；超过即主动 abort 并给出可读文案，按钮由调用方的 finally
// 恢复（各页面的互斥锁复位均在 finally 路径上）。
const API_REQUEST_TIMEOUT_MS = 30000;

/**
 * 发起统一封装的 fetch 请求
 *
 * @param {string} path 接口相对路径（基于 /api，例如 "/reports/summary"）
 * @param {object} [options={}] fetch 原生配置项，支持 method/body/headers；
 *                            额外支持 timeout（毫秒，缺省 API_REQUEST_TIMEOUT_MS）；
 *                            传 timeout: 0 表示不设超时（SSE 等长连接场景）
 * @returns {Promise<any>} 成功时 resolve 统一响应体的 data 字段；
 *                         响应无 JSON 体时 resolve null
 * @throws {Error} 网络异常、超时或 HTTP 非 2xx 时抛出，message 可读可直接展示
 */
async function request(path, options) {
    // 未传配置时兜底为空对象，避免解构/读取 undefined 报错
    options = options || {};

    // 合并请求头：默认 JSON 内容类型，调用方传入的同名头可覆盖
    const headers = Object.assign(
        { "Content-Type": "application/json" },
        options.headers || {}
    );

    // 组装最终请求配置（浅拷贝避免污染调用方对象）
    const finalOptions = Object.assign({}, options, { headers: headers });

    // body 若为对象则统一 JSON 序列化；字符串（已序列化）原样透传
    if (finalOptions.body && typeof finalOptions.body !== "string") {
        finalOptions.body = JSON.stringify(finalOptions.body);
    }

    // 超时闸（Day44 P2-09）：AbortController 让请求可被主动打断。
    // 定时器与 fetch 同时挂起，谁先到用谁的结果；finally 清理定时器，
    // 避免请求已返回后定时器仍挂着（长时间运行会累积定时器句柄）。
    const timeoutMs =
        typeof options.timeout === "number" ? options.timeout : API_REQUEST_TIMEOUT_MS;
    let controller = null;
    let timeoutId = null;
    if (timeoutMs > 0 && typeof AbortController !== "undefined") {
        controller = new AbortController();
        finalOptions.signal = controller.signal;
        timeoutId = setTimeout(function () {
            controller.abort();
        }, timeoutMs);
    }

    // 发起网络请求，单独捕获网络层异常（断网/CORS/超时等）
    let response;
    try {
        response = await fetch(API_BASE + path, finalOptions);
    } catch (networkError) {
        // 超时与普通网络故障给出不同文案：前者是"等太久主动放弃"，
        // 后者是"根本没连上"，排障方向不同，不应混为一谈
        if (controller && controller.signal.aborted) {
            throw new Error(
                "请求超时（" + Math.round(timeoutMs / 1000) +
                "秒无响应），请稍后重试或刷新页面"
            );
        }
        // 网络层失败没有 HTTP 响应，转成带上下文的可读错误抛出
        throw new Error("网络请求失败，请检查连接后重试：" + networkError.message);
    } finally {
        if (timeoutId !== null) {
            clearTimeout(timeoutId);
        }
    }

    // 先尝试按统一响应体 {code, message, data} 解析 JSON
    let payload = null;
    try {
        payload = await response.json();
    } catch (parseError) {
        // 响应不是 JSON（如网关返回 HTML 错误页）：失败按状态码抛错
        if (!response.ok) {
            throw new Error("请求失败，HTTP 状态码：" + response.status);
        }
        // 204 等成功但无响应体场景，直接返回 null
        return null;
    }

    // HTTP 状态非 2xx：优先取后端业务 message，缺失时回退状态码
    if (!response.ok) {
        const backendMessage = payload && payload.message;
        throw new Error(
            backendMessage || ("请求失败，HTTP 状态码：" + response.status)
        );
    }

    // 业务码校验: HTTP 200 + {code:500} 这类"传输成功但业务失败"的响应，
    // 修复前被静默解包成 data=null。后果: 用例列表把 null 渲染成"暂无数据"
    // （用户误判为库是空的），看板统计卡片则抛 Cannot read properties of
    // null，把真实的后端 message 覆盖掉——排查方向被彻底带偏。
    // 与 cases.js 的 importCases 保持同一口径。
    //
    // 判据由"白名单 [0, 200]"改为"**2xx 全放行**"（v5 遗留修复）:
    // 本项目 code 与 HTTP 状态码同值（response.py 统一封装），成功码
    // **不止 200**——创建用例回 201（response.created）、触发执行回 202
    // （trigger 固定 202 受理）。原白名单把这两个成功响应判成业务失败:
    //   · 202 → executions.js 的 trigger 一旦改走本封装就会拿不到
    //     execution_id/total_cases（Day43 因此让 triggerExecution 直连
    //     fetch 绕过本封装，属被迫的例外而非最优设计）；
    //   · 201 → **更隐蔽也更严重**：cases.js 的 createCase 走
    //     window.api.post，新建用例必然抛"业务处理失败，业务码：201"，
    //     而 HTTP 层明明是 201 Created。该缺陷此前被 cases.js 整脚本
    //     SyntaxError 完全掩盖，页面恢复后才会暴露。
    // 4xx/5xx 行为不变（照常抛错并优先取后端 message）；非数字 code
    // 走 Number() 得 NaN，比较恒为 false → 仍判失败，与修复前一致。
    if (payload && payload.code !== undefined && payload.code !== null) {
        const code = Number(payload.code);
        if (!(code >= 0 && code < 400)) {
            throw new Error(
                payload.message || ("业务处理失败，业务码：" + payload.code)
            );
        }
    }

    // 正常响应：解包 data 字段返回，业务层无需再关心统一外层结构
    return payload ? payload.data : null;
}

/**
 * API 便捷方法集合，覆盖后端四类 HTTP 动词
 * get    查询（列表/详情/统计）
 * post   新建/触发类操作
 * put    全量更新
 * delete 删除
 */
const api = {
    // GET 请求：仅需相对路径
    get(path) {
        return request(path, { method: "GET" });
    },
    // POST 请求：body 为普通对象，由 request 统一序列化
    post(path, body) {
        return request(path, { method: "POST", body: body });
    },
    // PUT 请求：与 POST 同构，语义为更新
    put(path, body) {
        return request(path, { method: "PUT", body: body });
    },
    // DELETE 请求：通常无请求体
    delete(path) {
        return request(path, { method: "DELETE" });
    },
};

// 显式挂载到 window，供后续页面内联脚本与 extra_js 子脚本直接 window.api 调用
window.api = api;
