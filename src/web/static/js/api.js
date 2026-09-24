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

/**
 * 发起统一封装的 fetch 请求
 *
 * @param {string} path 接口相对路径（基于 /api，例如 "/reports/summary"）
 * @param {object} [options={}] fetch 原生配置项，支持 method/body/headers 等
 * @returns {Promise<any>} 成功时 resolve 统一响应体的 data 字段；
 *                         响应无 JSON 体时 resolve null
 * @throws {Error} 网络异常或 HTTP 非 2xx 时抛出，message 可读可直接展示
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

    // 发起网络请求，单独捕获网络层异常（断网/CORS/超时等）
    let response;
    try {
        response = await fetch(API_BASE + path, finalOptions);
    } catch (networkError) {
        // 网络层失败没有 HTTP 响应，转成带上下文的可读错误抛出
        throw new Error("网络请求失败，请检查连接后重试：" + networkError.message);
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
