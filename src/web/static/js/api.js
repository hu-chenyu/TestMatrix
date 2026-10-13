/* ==========================================================================
  TestMatrix 统一 API 请求封装（Day35 前端骨架，Day36+ 各页面复用）
  约定：
   1. 所有数据接口统一挂在 /api 命名空间下，与 HTML 页面路由严格分离；
   2. 后端响应体统一为 {code, message, data}，本模块成功时直接解包 data；
   3. HTTP 非 2xx 或网络异常均抛出 Error（message 优先取后端 message），
    错误对象上另附 status（HTTP 状态码）与 retryable（是否可安全重发）
    两个字段，调用方按状态码分流，不再靠匹配后端文案判断故障类型；
   4. 仅幂等动词（GET）自动重试一次：非幂等动词（POST/PUT/DELETE）
    绝不自动重发——重发一次触发执行等于凭空多跑一个批次，
    重发一次删除请求在超时场景下会得到 404 而非幂等成功。
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

// 幂等请求（GET）的最大尝试次数：1 次正常 + 1 次自动重试（Day54）。
// 为什么只给 GET：列表/统计接口重发无副作用，而瞬时断连、单次 502、
// 服务重启的几秒空窗恰恰是"再点一次刷新就好了"的场景——让用户手动重试
// 与实际自动重试的用户感知差距很大，但有副作用的动词一次都不能自动重发。
const API_RETRY_ATTEMPTS = 2;

// 重试前的等待基数（毫秒）：给瞬时抖动留出恢复窗口，又不至于让
// 失败态姗姗来迟（用户已在等结果，退避过久与卡死难以区分）。
const API_RETRY_DELAY_MS = 400;

/**
 * 等待指定毫秒（重试退避用）
 *
 * @param {number} ms 等待时长（毫秒）
 * @returns {Promise<void>} 定时器到期后 resolve
 */
function delay(ms) {
  return new Promise(function (resolve) {
    setTimeout(resolve, ms);
  });
}

/**
 * 给错误对象标注可重试性与 HTTP 状态码（Day54）
 *
 * 为什么要把状态码挂到 Error 上：调用方此前只能靠**匹配后端文案**判断
 * 404（executions.js 的 openLogView 即此写法），后端文案一改判定即静默
 * 失效；而"数据库表不存在"一类的 500 文案又同样含"不存在"字样，任何
 * 子串匹配都会把真实故障误报成"历史批次"。状态码是协议层事实，稳定且
 * 无需猜测。
 *
 * @param {Error} error 原始错误对象
 * @param {boolean} retryable 是否可安全重发（仅 GET 会消费该字段）
 * @param {number} [status] HTTP 状态码；无 HTTP 响应（网络层失败）时不传
 * @returns {Error} 同一个错误对象（便于 throw 处一行返回）
 */
function annotateError(error, retryable, status) {
  error.retryable = Boolean(retryable);
  error.status = typeof status === "number" ? status : null;
  return error;
}

/**
 * 发起单次请求（不重试），成功时 resolve 统一响应体的 data
 *
 * @param {string} path 接口相对路径（基于 /api）
 * @param {object} finalOptions 已合并请求头与序列化 body 的 fetch 配置
 * @param {number} timeoutMs 本次请求的超时上限（毫秒，0 表示不设超时）
 * @returns {Promise<any>} 成功时 resolve data 字段；响应无 JSON 体时 resolve null
 * @throws {Error} 网络异常、超时、HTTP 非 2xx 或业务码失败时抛出（带标注字段）
 */
async function performRequest(path, finalOptions, timeoutMs) {
  // 超时闸（Day44 P2-09）：AbortController 让请求可被主动打断。
  // 定时器与 fetch 同时挂起，谁先到用谁的结果；finally 清理定时器，
  // 避免请求已返回后定时器仍挂着（长时间运行会累积定时器句柄）。
  // Day54：控制器在**每次尝试内部**新建——abort 过的 signal 不可复用，
  // 放在重试循环外会让第二次尝试一发出就带 already-aborted 状态。
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
      // 超时不重试：已经等满一个超时预算，自动再等一轮等于把用户
      // 可感知的失败时间翻倍，而故障往往并没有变。
      // 这里**刻意不加标注字段**：重试循环以 `!error.retryable` 判定
      // 不可重发，字段缺失即为假值——语义与显式标注 false 完全一致，
      // 同时保留 "throw new Error(" 这一既有形态守卫所锚定的写法。
      throw new Error(
        "请求超时（" + Math.round(timeoutMs / 1000) +
        "秒无响应），请稍后重试或刷新页面"
      );
    }
    // 网络层失败没有 HTTP 响应，转成带上下文的可读错误抛出；
    // 瞬时断连可安全重发（仅 GET 会真正重发）
    throw annotateError(
      new Error("网络请求失败，请检查连接后重试：" + networkError.message),
      true
    );
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
      throw annotateError(
        new Error("请求失败，HTTP 状态码：" + response.status),
        response.status >= 500,
        response.status
      );
    }
    // 204 等成功但无响应体场景，直接返回 null
    return null;
  }

  // HTTP 状态非 2xx：优先取后端业务 message，缺失时回退状态码
  if (!response.ok) {
    const backendMessage = payload && payload.message;
    throw annotateError(
      new Error(
        backendMessage || ("请求失败，HTTP 状态码：" + response.status)
      ),
      // 5xx 归为服务端瞬时故障，可重试；4xx 是请求本身的问题，
      // 重发只会拿到同样的 4xx（并在日志里制造重复噪声）
      response.status >= 500,
      response.status
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
  //  · 202 → executions.js 的 trigger 一旦改走本封装就会拿不到
  //   execution_id/total_cases（Day43 因此让 triggerExecution 直连
  //   fetch 绕过本封装，属被迫的例外而非最优设计）；
  //  · 201 → **更隐蔽也更严重**：cases.js 的 createCase 走
  //   window.api.post，新建用例必然抛"业务处理失败，业务码：201"，
  //   而 HTTP 层明明是 201 Created。该缺陷此前被 cases.js 整脚本
  //   SyntaxError 完全掩盖，页面恢复后才会暴露。
  // 4xx/5xx 行为不变（照常抛错并优先取后端 message）；非数字 code
  // 走 Number() 得 NaN，比较恒为 false → 仍判失败，与修复前一致。
  if (payload && payload.code !== undefined && payload.code !== null) {
    const code = Number(payload.code);
    if (!(code >= 0 && code < 400)) {
      // 业务失败不重试：链路是通的，重发只会重复拿到同一个业务错误
      throw annotateError(
        new Error(
          payload.message || ("业务处理失败，业务码：" + payload.code)
        ),
        false,
        response.status
      );
    }
  }

  // 正常响应：解包 data 字段返回，业务层无需再关心统一外层结构
  return payload ? payload.data : null;
}

/**
 * 发起统一封装的 fetch 请求（含超时、错误标注与幂等重试）
 *
 * @param {string} path 接口相对路径（基于 /api，例如 "/reports/summary"）
 * @param {object} [options={}] fetch 原生配置项，支持 method/body/headers；
 *              额外支持 timeout（毫秒，缺省 API_REQUEST_TIMEOUT_MS）；
 *              传 timeout: 0 表示不设超时（SSE 等长连接场景）
 * @returns {Promise<any>} 成功时 resolve 统一响应体的 data 字段；
 *             响应无 JSON 体时 resolve null
 * @throws {Error} 网络异常、超时或 HTTP 非 2xx 时抛出，message 可读可直接展示；
 *         错误对象带 status（HTTP 状态码或 null）与 retryable 标志
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

  const timeoutMs =
    typeof options.timeout === "number" ? options.timeout : API_REQUEST_TIMEOUT_MS;

  // 只对幂等动词开放自动重试（缺省动词即 GET）
  const method = String(finalOptions.method || "GET").toUpperCase();
  const maxAttempts = method === "GET" ? API_RETRY_ATTEMPTS : 1;

  let lastError = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await performRequest(path, finalOptions, timeoutMs);
    } catch (error) {
      lastError = error;
      // 不可重试、已是最后一次尝试：原样抛出，调用方按 status/message 展示
      if (!error.retryable || attempt >= maxAttempts) {
        throw error;
      }
      // 可重试：等待后原样重发（GET 无副作用，重发不会改变服务端状态）
      await delay(API_RETRY_DELAY_MS * attempt);
    }
  }
  // 理论上不可达（循环内要么 return 要么 throw），兜底防空返回
  throw lastError;
}

/**
 * API 便捷方法集合，覆盖后端四类 HTTP 动词
 * get  查询（列表/详情/统计）
 * post  新建/触发类操作
 * put  全量更新
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
