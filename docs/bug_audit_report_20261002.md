# TestMatrix 国庆大扫除 · 阶段1 全项目代码审查报告

> 基线：HEAD=`9984d72`，85 commits，625 tests passed，全仓覆盖率 92%，ruff 全绿，CI Run #21 success
> 审查范围：33 个 Python 文件 + 5 个业务 JS + 4 个 HTML（共 11,139 行 Python + 1,613 行 JS + 512 行 HTML）
> 审查日期：2026-10-02
> 审查方式：主审（23 个文件） + 4 个独立只读审查 agent（19 个文件），P0/P1 全部由主审二次实证核实

---

## 一、总览

| 级别 | 数量 | 说明 |
|---|---|---|
| **P0 阻断** | **1** | 存储型 XSS，可直接利用 |
| **P1 严重** | **6** | 凭据泄露、批次状态被篡改、worker 线程静默死亡、数据覆盖 |
| **P2 一般** | **42** | 健壮性边界、信息泄露、竞态、配置失效、性能 |
| **P3 观察** | **30** | 代码质量、可维护性、死代码 |
| **合计** | **79** | |

### 关键结论

1. **平台全站零鉴权**：`src/web` 无任何 auth/session/CSRF 逻辑，`UnauthorizedError`/`ForbiddenError` 定义后从未被 raise（死代码）。所有 `/api/*` 对外全开，含用例 CRUD、执行触发、通知死信（完整消息全文）。
2. **SQL 注入面为零**：全仓 SQL 访问均走 SQLAlchemy ORM 表达式，无 f-string/`%` 拼 SQL（已全量检索确认）。
3. **路径遍历已封堵**：上传走 `secure_filename` + `tempfile.mkdtemp`，且有回归测试守卫。
4. **P0/P1 已实证**：3 条最严重问题用可执行脚本实测复现，非静态推断。

---

## 二、P0 阻断（1 条）

### [P0] `src/web/static/js/dashboard.js:389-399` — ECharts 饼图 tooltip 未转义 `module`，存储型 XSS

**问题描述**
ECharts 5 tooltip 默认 `renderMode: "html"`，自定义 `formatter` 的返回值被当作 HTML 直接注入 tooltip DOM，且 ECharts 只对 `{b}/{c}` 模板占位符做 `encodeHTML`，**自定义 formatter 返回值完全不转义**。此处把后端返回的 `row.module` 原样拼进 HTML 字符串。

**实测证据（完整数据流，已逐环核实）**

```
来源  src/web/routes/cases.py:131   module = fields.String(validate=validate.Length(max=64))
      ↑ 仅限长 64，无字符集白名单，<img src=x onerror=...> 可入库
      ↓
中转  src/web/routes/reports.py:185  data = ReportRepository.get_module_distribution()   # 原样下发
      ↓
汇点  src/web/static/js/dashboard.js:393
      return ( row.module + "<br>总数：" + row.total + ...   # ← 拼进 HTML tooltip
```

`escapeHtml` 保护只存在于 `cases.js`（挂在 `window.casesPage` 上），`dashboard.html` 不加载 `cases.js`，两文件互不引用 → 保护完全未覆盖。

**触发路径**
1. 未鉴权接口写入恶意用例：
   `curl -X POST /api/cases/ -H 'Content-Type: application/json' -d '{"case_id":"XSS-1","name":"x","module":"<img src=x onerror=fetch(\"/api/cases/XSS-1\",{method:\"DELETE\"})>"}'`
2. 产生执行记录使该模块进入饼图：`curl -X POST /api/executions/trigger`
3. 受害者打开 `/dashboard`，悬浮到该扇区 → payload 以应用同源身份执行，可调用全部 `/api` 写接口。

**影响范围**：所有访问质量看板的用户。因平台无鉴权，后果为数据破坏（批量 DELETE/PUT）；若后续接入登录态则升级为会话劫持。

**修复建议**：把 `escapeHtml` 上提到 `main.js`（全站唯一加载）全局导出，`dashboard.js` 两处 formatter 复用。不改后端 API、不改 schema、不换图表库。

**修复成本**：低

---

## 三、P1 严重（6 条）

### [P1-1] `src/core/notification.py:1060-1065` — 企微 webhook key 随 requests 异常原文写入日志（凭据泄露）

**问题描述**
网络异常分支把 requests 异常的 `str()` 原样拼进日志。requests 的连接层异常文本自带完整请求 URL（含 query），而企微 webhook 凭据就在 `?key=<SECRET>` 里。本文件类文档（`:946`）明确声明"webhook URL 含 key 属敏感信息，日志只打印前 30 字符脱敏"，此路径直接打破该不变量。

**实测证据（已实证）**

```
notification.py:1060-1065
    except requests.exceptions.RequestException as exc:
        logger.error(f"企微通知网络异常 | {type(exc).__name__}: {exc}")

实跑结果（fake 不可解析域名，key=SUPERSECRET-abc123）：
    EXC TYPE : ConnectionError
    EXC STR  : HTTPSConnectionPool(host='...', port=443): Max retries exceeded with url:
               /cgi-bin/webhook/send?key=SUPERSECRET-abc123 (Caused by NameResolutionError(...))
    LEAK KEY : True
```

**触发路径**：`TM_WECHAT_ENABLED=true` + webhook 主机 DNS 失败/连接被拒/代理报错 → key 明文落日志文件。现有测试 `test_wechat_notifier_demo.py:255` 用 `ConnectionError("Connection refused")`（文本不含 URL），未覆盖真实异常文本。

**影响范围**：企微渠道全部网络异常场景；日志外发或多用户可读即为凭据外泄。

**修复建议**：不打印 `exc` 原文，改打异常类型 + `urlsplit` 剥离 query 后的目标地址。

**修复成本**：低

---

### [P1-2] `src/core/cache.py:300-304` — `_get_backend` 真实 URL 分支无异常保护，配置错误冒泡成 API 500

**问题描述**
`_get_backend` 只在 fakeredis 分支包了 `try/except ImportError`，真实 Redis 分支的 `redis.from_url` 是裸调用；而 `get_json`/`set_json`/`delete_pattern` 三个公开方法都是先 `backend = self._get_backend()` **再** `try:`，后端构建期异常完全在保护范围之外。这直接违反模块 docstring 第 12 行"绝不允许缓存故障冒泡导致 API 返回 500"的铁律。

**实测证据**
```
cache.py:299-305    else: self._backend = redis.from_url(url, decode_responses=True, socket_timeout=2)
cache.py:350-354    backend = self._get_backend()   # ← 在 try 之外
                    if backend is None: return None
                    try: raw_value = backend.get(key)
实测：redis.from_url('redis://127.0.0.1:abc/0') -> ValueError: Port could not be cast to integer value as 'abc'
      redis.from_url('http://127.0.0.1:6379/0')  -> ValueError: Redis URL must specify one of the following schemes
```

**触发路径**
① `TM_REDIS_ENABLED=true` + `TM_REDIS_URL` 写错 → `GET /api/cases` 直接 500。
② 更糟：`POST /api/cases` 已 commit 成功、日志已打"用例已创建"，随后失效缓存抛 ValueError → 客户端收 500，用户重试得到 409"用例编号已存在"，**数据已写入但用户以为失败**。
因 `self._backend` 始终为 None，每次请求都会重新解析 URL 并再次抛错，**无自愈**。

**影响范围**：`/api/cases` 全部 CRUD、`/api/cases/import`、reports 全部 6 个端点。同一个 `TM_REDIS_URL` 也被 `task_queue.py:236-240` 以完全相同方式使用。

**修复成本**：低（单文件局部，签名/返回值/异常契约不变）

---

### [P1-3] `src/core/case_manager.py:1683` — finished 分支缓存失效未隔离，已 finished 批次被改写成 failed

**问题描述**
批次状态、汇总都已成功落库后调用 `cache_client.invalidate_reports()`，该调用**没有任何 try 包裹**，却被最外层 `try/except Exception`（`:1712`）捕获，最终执行 e 分支把同一个批次重写为 `status="failed"`。

**实测证据**
```
case_manager.py:1662  summary = cls.finish_execution(execution_id)
case_manager.py:1665  cls._update_batch_status(execution_id, status="finished", ...)
case_manager.py:1681  # 缓存层静默兜底，故障不影响执行主流程     ← 注释声明的契约
case_manager.py:1683  cache_client.invalidate_reports()           # ← 无隔离
case_manager.py:1712  except Exception as exc:                    # ← 兜底当成执行失败
case_manager.py:1720  cls._update_batch_status(execution_id, status="failed", ...)
```
`case_manager.py:1681` 的注释白纸黑字写着"缓存层静默兜底，故障不影响执行主流程"——而 P1-2 表明缓存层恰好违反了这个契约。两处构成完整因果链。

**触发路径**：`TM_REDIS_URL` 解析失败 + 任意批次执行完成 → 一条 100% 通过的批次（`test_executions` 全 passed、`pass_rate=1.0`）被改写为 `status="failed"`、`error_message="ValueError: Port could not be cast..."`，与真实执行结果矛盾。

**修复成本**：低

---

### [P1-4] `src/core/case_manager.py:1736` — failed 分支缓存失效未隔离，杀死守护线程并跳过 batch_failed 事件/通道关闭/通知

**问题描述**
`:1736` 位于外层 `except` 块**内部**，此处已无任何 try 承接。抛出后整个 except 块后半段（`batch_failed` 事件、关闭事件通道、失败通知）全部被跳过，异常一路冒到线程目标函数外，**daemon 线程静默死亡**。

**实测证据**
```
case_manager.py:1736  cache_client.invalidate_reports()   # ← 外层 except 块内，无 try 承接
case_manager.py:1739  cls._publish_execution_event(execution_id, "batch_failed", ...)   # 被跳过
case_manager.py:1744  cls._close_execution_channel(execution_id, reason="failed")      # 被跳过
case_manager.py:1749  try: cls.notify_execution_result(...)   # 注释自称"必须自带try兜底" ← 证明此处是遗漏
```

**触发路径**：批次因任何原因失败 + 缓存 URL 配置错误 → `batch_failed` 事件不发布、通道不关闭（订阅该批次的 SSE 客户端收不到终态，`EventSource` 一直挂到超时）、失败通知不推送；线程目标函数未捕获异常，只在 stderr 留一段栈，**日志系统里没有对应记录**。

**修复成本**：低

---

### [P1-5] `src/core/task_queue.py:485` — `dequeue` 返回非 dict 载荷时 worker 线程静默死亡

**问题描述**
`dequeue` 的返回类型标注是 `dict | None`，但只做了 `json.loads` 而**未校验结果是否为 dict**。合法的 JSON 数组/标量（历史消息、其他生产者数据）会原样返回。随后 `TaskWorker.run` 在 `:485` 执行 `payload.get("execution_id", "")`——该行位于 `try` 块（`:500`）**之前**，`while` 循环也没有外层 try。

**实测证据（已实证）**
```
dequeue 正常载荷  -> dict
dequeue 数组载荷  -> [1, 2, 3]  类型: list     # 契约要求 dict | None
返回值是否为 dict: False
worker 线程存活: False
worker 逃逸异常: AttributeError: 'list' object has no attribute 'get'
```
且预置在队列中的后续合法任务 `RUN-AFTER` **永远不会被消费**。

**触发路径**：队列中混入一条合法 JSON 但非对象的载荷（Redis 队列是共享持久存储，跨版本/跨生产者数据可能混入）→ worker 线程立即死亡 → **后续所有任务永久堆积，队列形同虚设，且无任何错误日志**。

**影响范围**：`TM_TASK_QUEUE_ENABLED=true` 时全部异步执行。

**修复建议**：`dequeue` 内增加 `isinstance(payload, dict)` 校验，不满足则记 warning 返回 `None`（与既有坏消息处理路径一致）。签名与返回类型契约不变。

**修复成本**：低

---

### [P1-6] `src/web/static/js/cases.js:554-580` — `openEditModal` 无在途序号校验，慢响应覆盖弹窗模式导致误改已有用例

**问题描述**
`openEditModal` 在 `await` 详情后才写 `state.editingCaseId` / 表单模式，期间无任何"当前用户意图"校验。

**实测证据**
```
556:  const detail = await window.api.get("/cases/" + encodeURIComponent(caseId));
559:  state.editingCaseId = caseId;
561:  els.formModalTitle.textContent = "编辑用例";
（openCreateModal 中 state.editingCaseId = null 完全无感知）
```

**触发路径**：慢网络下点行 A 的「编辑」→ 立即点「新增用例」→ A 的详情返回 → 弹窗被改写成 A 的编辑表单 → 用户填完保存走 `state.editingCaseId !== null` 分支发 `PUT /api/cases/A` → **覆盖已有用例 A**（新增意图丢失 + 既有数据被改写）。连点两个不同用例的「编辑」同理。

**修复建议**：加请求序号令牌 `let _editSeq = 0`，`openCreateModal` 中 `++`，`openEditModal` 中 `const seq = ++_editSeq` 并在 await 后 `if (seq !== _editSeq) return;`。

**修复成本**：低

---

## 四、P2 一般（42 条）

### 4.1 安全与信息泄露（8 条）

| # | 位置 | 问题 |
|---|---|---|
| P2-1 | `src/web/routes/base.py:78,123-132` | `/health` 无条件返回 `str(exc)`，**无 TESTING 门控**（与 `exceptions.py:164` 的门控不一致）。SQLAlchemy 异常字符串含完整 MySQL URL（`mysql+pymysql://user:pass@host/db`），未鉴权接口直接外泄 |
| P2-2 | `src/web/app.py:173-174` | 仅 2 个安全头（nosniff / X-Frame-Options），缺 CSP / HSTS / Referrer-Policy。XSS 无任何纵深防御 |
| P2-3 | `src/web/config.py:39-40` | `JSON_AS_ASCII` / `JSON_SORT_KEYS` 在 **Flask 2.3 已移除**（实测版本 2.3.3），是死配置；`response.py:10-11` 的注释还在引用它 |
| P2-4 | `src/web/config.py:70-71` | `TestingConfig.TM_DB_TYPE` / `TM_DB_SQLITE_PATH` 是**死配置**——DB 层走 `env_manager`（`os.environ`），从不读 Flask config。实测：全仓引用点均在 `tests/` 的 `monkeypatch.setenv`，无一处读 `app.config` |
| P2-5 | `src/web/config.py:36,74-82` | `SECRET_KEY` 在**类定义期**求值；未设 `TM_SECRET_KEY` 时随机生成 → 多 worker 部署各进程 key 不同、每次重启即失效。`ProductionConfig` 文档声称"必须从环境变量设置"但**无任何校验** |
| P2-6 | `src/web/routes/cases.py:577-580` | 上传**无 `MAX_CONTENT_LENGTH`**（已 grep 确认全仓无该配置）→ 任意大文件写满磁盘。叠加 xlsx 的 zip bomb 特性，且平台无鉴权 |
| P2-7 | `src/web/routes/cases.py:602-605` | `logger.info(f"... 文件={original_name}")` 直接打用户可控的原始文件名 → **日志注入**（伪造日志行），与项目自身的脱敏努力相悖 |
| P2-8 | `src/common/http_client.py:264,288` | **URL 查询串从不脱敏**：`params` 被 `_mask_data` 保护了，但完整 `url` 原样进日志和 `error_message` → `?password=xxx` 泄露，且会落库并在 UI 展示 |

### 4.2 健壮性与异常处理（9 条）

| # | 位置 | 问题 |
|---|---|---|
| P2-9 | `src/web/routes/cases.py:450,477,588`、`src/web/routes/executions.py:182,309` | 用**子串匹配**做异常分流（`if "不存在" not in str(exc)`）。DB 错误消息若含该关键词（如"表不存在"）会被误判成 404/409 → 真实故障被隐藏 |
| P2-10 | `src/web/routes/executions.py:598-629` | SSE 断线重连时 `Last-Event-ID` 与 DB 重建的合成 id 是**两套独立编号体系**。客户端从实时流（通道 id 可达 50+）重连后走 DB 重建分支（合成 id 仅 1..N），`next_frame_id > resume_after` 恒假 → **一帧不发**，终态事件永久丢失，`EventSource` 无限重连 |
| P2-11 | `src/web/routes/executions.py:651-655` | 终态补发分支**不按 `Last-Event-ID` 过滤**（与分支一/三分支不一致）→ 重连时收到全部积压事件，`case_finished` 重复追加 |
| P2-12 | `src/common/env_manager.py:145-162` | `get_bool` 对**任何无法识别的值返回 False 而非 `default`**，违反自身 docstring"值非法时返回default"。`TM_TASK_WORKER_ENABLED=enabled`（拼写变体）→ worker 静默不启动，任务永久堆积 |
| P2-13 | `src/core/task_queue.py:640-642` | `stop_worker` join 超时后**仍无条件清空** `_worker_thread` 引用 → 后续 `start_worker` 的存活检查失效，会起**第二个 worker**，破坏"单 worker 串行"不变量 |
| P2-14 | `src/core/report_analyzer.py:265-280` | `parse_results_dir` 的 except 元组不含 `OSError`，而 `parse_result_file` 对已消失文件抛 `FileNotFoundError` → 单文件竞态导致**整批统计丢失**，违背模块"绝不因单文件脏数据中断整批"的容错契约 |
| P2-15 | `src/core/data_driver.py:379-386,398-407` | 校验硬性要求必填字段是 `str`，但 Excel 把纯数字 `case_id` 存为 int/float/bool → openpyxl 返回非 str → 报"必填字段缺失"（实际非空），排障方向被误导 |
| P2-16 | `src/core/data_driver.py:311-329` | Excel **表头重名静默覆盖**（右侧列覆盖左侧，无报错）；内部哨兵键 `_row_number` 与用户表头同名时**用户数据被整数覆盖后 pop 掉** → 静默数据损坏 |
| P2-17 | `src/core/data_driver.py:187,478` | `filter_cases` 维度类型非法时**静默降级为不过滤**并返回全量；`tags=None` 时 `set(None)` 抛未捕获 `TypeError` |

### 4.3 串口 / 硬件在环（3 条）

| # | 位置 | 问题 |
|---|---|---|
| P2-18 | `src/common/serial_client.py:167-174,250-251` | 只设了 `timeout`（仅作用于**读**），`write_timeout` 保持默认 `None` = **永久阻塞**。板卡停收数据 → `write()`/`flush()` 无限期挂死，`__exit__` 永不执行，串口句柄不释放 |
| P2-19 | `src/common/serial_client.py:287` | `timeout or self.timeout` 典型 `x or default` 反模式：显式传 `0`（falsy）被静默改写为默认 3.0s。现有测试传的是 `0.05`（truthy），无守卫 |
| P2-20 | `src/common/serial_client.py:142-152` | `comports()` 前置硬校验。macOS 不枚举 `/dev/cu.*` → 板卡在 macOS 上**完全不可用**（`serial.Serial` 本身能打开）。被现有测试显式锁定为设计意图，改动需同步改测试 |

### 4.4 状态机 / 数据一致性（4 条）

| # | 位置 | 问题 |
|---|---|---|
| P2-21 | `src/core/case_manager.py:2129-2130` | `_infer_case_type` 对**整条绝对路径**做子串匹配。若 OS 账户名含 `chip`/`serial`/`telnet`（如 `telnet_lab`），该用户上传的所有纯 API 用例一律入库为 `case_type="chip"` → `case_type="api"` 的执行批次永远选不到它们 |
| P2-22 | `src/core/case_manager.py:2296-2298` | `run_batch` 不传 `case_type`，落到默认值 `"api"`，与上一步刚按路径标成 `chip` 的结果矛盾 → chip 用例文件必然筛出 0 条，抛误导性的"执行批次不存在" |
| P2-23 | `src/core/case_manager.py:670-677,695-702` | `create_case` 的 `case_id` 查重是 check-then-act，并发下唯一约束异常裸奔成 500 + **完整 INSERT 语句与列值进入日志/测试环境响应体** |
| P2-24 | `src/core/case_manager.py:150,202-203` | 批次号去重集合只增不删（无上限无过期），长跑进程内存单调增长；同时 check-then-act 非原子 + 仅单进程内唯一，多 worker 下 `execution_id` 可能重复（PK 冲突 / 明细混入同批次） |

### 4.5 通知链路（5 条）

| # | 位置 | 问题 |
|---|---|---|
| P2-25 | `src/core/notification.py:750-760` | 邮件报告 HTML 模板把 `detail.name`/`module`/`error_message` 等**外部可控数据** f-string 直拼进 HTML，**全文件无 `html.escape`** → 邮件端 HTML 注入（伪造报告内容 / 远程图片跟踪像素） |
| P2-26 | `src/core/notification.py:1337` | `base_delay` 只有 `float()` 强转，无下限校验。负值/NaN → `time.sleep(负数)` 抛 ValueError → 逃到 `notify` 兜底 → 渠道结果 False 但**不写死信不写历史，通知静默丢失无留痕** |
| P2-27 | `src/core/notification.py:1068-1078` | 企微 `response.json()` 只挡 `ValueError`；响应体是 JSON 数组/null 时 `result.get` 抛 `AttributeError` **逃出 `send()`**（违反"绝不向上抛"契约）→ 4 次重试 + 7 秒退避后死信原因被替换成 AttributeError，真实原因丢失 |
| P2-28 | `src/core/notification.py:1481-1483` | `notifications[channel]` 硬编码 email/wechat 两键，传入自定义渠道 notifier 必 `KeyError` → 被吞 → 该渠道永久静默不发送、不写死信 |
| P2-29 | `src/core/notification.py:1945-1958` + `src/web/routes/notifications.py:142-148` | 死信 `_to_dict` 带全文 `content`（无长度上限），路由再叠加 `limit=10000` 内存分页 → 单次 HTTP 请求把上万份完整 HTML 报告读进内存；且 `list_all` 实际取**最旧** 10000 条，死信超量后**新死信永远取不到** |

### 4.6 前端竞态与健壮性（7 条）

| # | 位置 | 问题 |
|---|---|---|
| P2-30 | `src/web/static/js/dashboard.js:310-321` | 趋势折线 tooltip 未转义 `execution_id`（当前后端生成不可控，但与 P0 同类遗漏，模式本身是缺陷来源） |
| P2-31 | `src/web/static/js/main.js:43-45` | `showToast` 把 type 归一化为 `danger`/`success` 二值，`dashboard.js` 4 处传 `"warning"` 全部渲染成**绿色成功样式配"加载失败"文案**，告警被忽略 |
| P2-32 | `src/web/static/js/cases.js:390-394` | `loadCases` 的 `if (state.loading) return;` 互斥锁**无待刷新补偿** → 保存/删除/导入后的 `await loadCases()` 被无声丢弃，用户看不到刚建的用例 |
| P2-33 | `src/web/static/js/dashboard.js:587-612` | `_dashboardLoading` 在 4 个图表请求发出后立即释放（fire-and-forget 未 await）→ 图表层裸奔，重复点击产生 2 倍请求且可能乱序覆盖 |
| P2-34 | `src/web/static/js/api.js:63-71` | 只看 `response.ok`，不校验统一响应体的业务 `code` → 后端若返回 `200 + {code:500}`，`loadCases` 把 `data=null` 渲染成"暂无用例数据"（误判为库空），dashboard 则抛 `Cannot read properties of null` 覆盖真实原因 |
| P2-35 | `src/web/static/js/cases.js:422-426` | 列表加载失败时未重置 `state.total`/`totalPages` → 页脚显示"共 137 条"+空表格，自相矛盾 |
| P2-36 | `src/web/templates/base.html:3-21` | 无 CSP meta。已核实无外链 CDN、无内联 `<script>`、无 `|safe`，因此 `script-src 'self'` 不会破坏现状（`style-src` 需保留 `'unsafe-inline'`，因 `dashboard.html:84,88` 有行内 style） |

### 4.7 其他（6 条）

| # | 位置 | 问题 |
|---|---|---|
| P2-37 | `src/common/logger.py:102-108` | **控制台 sink 未设 `diagnose=False`**（两个文件 sink 都设了）→ 异常时变量值/局部变量可能进 stderr，破坏"生产安全"不变量 |
| P2-38 | `src/common/logger.py:111-123` | 主日志文件 sink 固定 `level="DEBUG"`，配置的 `log_level` 对它**完全无效** → 生产设 ERROR 仍全量落盘，放大 P2-8 的泄露面 |
| P2-39 | `src/common/http_client.py:108-116` | `allowed_methods=None` 使 POST/PUT/PATCH/DELETE 也重试 → 非幂等请求重复副作用（测试平台会测创建类接口） |
| P2-40 | `src/core/case_manager.py:280-284` | `sync_cases_from_file` 逐条 SELECT 的 **N+1 查询**，全部包在同一写事务里；2000 条 = 2000 次 SELECT，持有写锁期间阻塞并发执行线程 |
| P2-41 | `src/core/case_manager.py:700,1022,1118,1466,1923` | 全部 SQLAlchemyError 分支把 `str(exc)` 拼进对外异常消息 → 原始 SQL + 绑定参数进日志、CLI 输出、TESTING 模式响应体 |
| P2-42 | `src/web/routes/cases.py:578` | `secure_filename` 吃掉非 ASCII 字符：**已实证** `secure_filename('用例数据.xlsx')` → `'xlsx'`（后缀丢失）。后缀白名单用原始名校验会通过，但落盘文件名为 `xlsx`（无点）→ `data_driver` 抛"不支持的文件格式: ''"，中文 xlsx 导入（中文团队高频场景）报与事实矛盾的错误 |

---

## 五、P3 观察（30 条，摘要）

**死代码 / 失效配置**
- `src/web/exceptions.py:94,103` — `UnauthorizedError`/`ForbiddenError` 定义并导出但**全仓从未 raise**（鉴权未实现的直接证据）
- `src/common/env_manager.py:57,76` — `_loaded` 标志只写不读
- `src/core/notification.py:308` — 函数内重复 `import time`（模块顶部已有）
- `src/web/routes/base.py:93,144` + `src/__init__.py` — 版本号三处不一致（`0.1.0` vs `1.0.0` vs `1.0.0`）
- `src/db/db_session.py:102-118` — MySQL 分支 `user`/`host`/`database` 未做 URL 编码（仅 password 编码了）

**重复代码（Phase 4 提取候选）**
- 分页参数解析/校验逻辑在 `cases.py`/`executions.py`/`notifications.py` **三处独立实现**（`DEFAULT_PAGE`/`DEFAULT_PAGE_SIZE`/`MAX_PAGE_SIZE`/`_parse_int_param`/`_parse_pagination`）
- `report_analyzer.py:1220-1312` vs `1314-1429` — 两个分布方法约 90 行结构同构（`valid_results` 元组重复硬编码两次）
- `logger.py:106,118,133` — 同一个 filter lambda 重复三次
- `pages.py` 三个视图函数除 `active_nav`/`page_title` 外完全相同
- `db_session.py:138-141`（双检锁）vs `cache.py:281`/`task_queue.py:220`（无双检锁）——同一模式两种写法

**文档漂移**
- `logger.py:25-26` 注释写"env.py 位于 src/common/ 下"，实际文件是 `logger.py`
- `notification.py:1875-1905` docstring 写"最近N条"，实现是最旧 N 条
- `response.py:10-11` 引用 Flask 2.3 已移除的 `JSON_AS_ASCII`
- `config.py:78` 声称生产强制 SECRET_KEY，代码无此校验

**其他**
- `case_manager.py:762-763` — `update_case` 对未知字段静默 `setattr`，不报错也不落库
- `case_manager.py:660` — `create_case` 未做 priority 枚举校验（docstring 却声称"两层各自兜底"）
- `case_manager.py:2313-2324` — `run_batch` 的 `start_time`/`end_time` 都取自执行之后，与 `duration` 不自洽
- `case_manager.py:2293-2298` — `run_batch` 不写批次行，与 `start_execution` 口径不一致（CLI 批次在列表可见但状态接口 404）
- `case_manager.py:390,402-407` — `list_cases` 全量加载后 Python 层排序
- `event_bus.py:385-390` — `get_channel(create=True)` 会复活已关闭通道（理论泄漏，未找到稳定复现时序）
- `event_bus.py:190-207` — closed 校验在锁外，与 `close()` 有竞态窗口
- `db/models.py:63-68` 等 — `created_at` 走库端 UTC、业务时间走 Python 本地时间，同表口径不一致（**修 server_default 属 schema 变更 → backlog**）
- `db/models.py:131-134` — `test_executions` 缺 `(case_id, result)` 索引（**加索引需迁移 → backlog**）
- `db/models.py:58-60,119,328` — 业务枚举列无 `CheckConstraint`（**需迁移 → backlog**）
- `report_analyzer.py:1497-1509` — `get_failed_top` N+1（代码注释已显式接受）
- `report_analyzer.py:826-827,873,897` — 模块名/优先级提取失败逐条 warning，单批次可刷万级日志
- `report_analyzer.py:1619` — `get_quality_metrics` 全表 `duration` 拉进内存算 avg/P95
- `serial_client.py:154-179` — `open()` 只捕 `SerialException`，`serial_for_url` 的 `ValueError` 逃逸
- `serial_client.py:257-262` — `expect` 分支仍固定 `sleep(wait_time)`，每条命令多付空等
- `serial_client.py` 为 CRLF，其余文件为 LF（diff 噪声，建议加 `.gitattributes`）

---

## 六、已知问题复核（提示词 3.2.2 列表）

| # | 级别 | 位置 | 复核结论 |
|---|---|---|---|
| 1 | P3 | `cases.py:487` 路径脱敏正则对含空格 Windows 路径截断 | **确认存在**。`_WIN_ABS_PATH` 的字符类 `[^\s\]\)'\"，、]+` 排除空格，`C:\Program Files\x\a.yaml` 会被截断 |
| 2 | P3 | `cases.py:488-491` Unix 正则只匹配特定目录前缀 | **确认存在**。`/data` `/mnt` `/srv` `/workspace` 等不在元组内 |
| 3 | P3 | `executors.py:167` TODO 未实现 | **确认存在**。`build_command` 用 `script_path or case_id` 占位，注释已说明"当前用例暂无 script_path 字段" |

---

## 七、阴性结论（已核查、确认无问题）

为避免后续重复排查，以下维度已全量核查并确认干净：

- **SQL 注入**：全仓 SQL 访问均走 SQLAlchemy ORM 表达式，无 f-string/`%`/`format` 拼 SQL
- **命令注入**：无 `os.system` / `shell=True`
- **反序列化**：`data_driver.py:226` 用 `yaml.safe_load`（非 `yaml.load`）；`report_analyzer.py:213` 用 `json.load`
- **公式注入**：`data_driver` 只读不写，无 Excel/CSV 写出路径
- **路径遍历**：上传走 `secure_filename` + `tempfile.mkdtemp`，`tests/test_cases_import_sanitize.py:158` 有回归守卫
- **除零**：`report_analyzer.py` 全部除法点已逐一核对有 `total <= 0` / `if total` 保护（`:664,1189,1302,1396,1635,1665`）
- **异常链丢失**：全部包装均用 `raise ... from exc`，无丢失
- **bare except**：全仓 0 处
- **资源泄漏**：DB session（7 处均 `finally: close()` 或 `session_scope`）、openpyxl workbook（`finally: close()`）、串口句柄（`close()` 幂等 + `__exit__` 无条件执行）三处均已逐路径核对
- **无限重试**：`notification.py` 有指数退避 + 有限次数（1+max_retries）
- **cascade 误删**：`models.py` 未定义任何 relationship
- **`__repr__` 泄露**：所有 `__repr__` 均不含 content/error_message/密码
- **SMTP 密码泄露**：未出现在任何日志/异常/返回体/DB 中
- **SSRF**：webhook URL 仅来自 env 配置，非外部请求参数
- **硬编码密钥**：JS/HTML/Python 中均未发现
- **敏感 `__repr__`/`_to_dict`**：死信 `_to_dict` 带 content 属设计选择（已记为 P2-29 性能项）
- **XSS 汇点**：`cases.js` 全部 innerHTML 汇点均正确转义；`error_message` 在 `templates/` 中无渲染点
- **已测试锁定的有意设计**：pass_rate 分母含 skipped（`test_case_notify_integration_demo.py:157` 显式断言）；comports 前置校验（`test_serial_client.py:308-314`）

---

## 八、覆盖率基线明细（Phase 3 输入）

| 文件 | 当前 | 目标 | 未覆盖语句数 |
|---|---|---|---|
| `src/common/http_client.py` | 76% | ≥90% | 25 |
| `src/core/task_queue.py` | 82% | ≥90% | 33 |
| `src/web/routes/executions.py` | 84% | ≥90% | 28 |
| `src/db/db_session.py` | 81% | ≥90% | 19 |
| `src/core/data_driver.py` | 86% | ≥92% | 22 |
| `src/common/env_manager.py` | 85% | ≥90% | 9 |
| `src/core/executors.py` | 85% | ≥90% | 11 |
| `src/web/exceptions.py` | 84% | ≥90% | 8 |
| **合计** | **92% (3606 stmts / 280 miss)** | **≥95%** | **155** |

**可达性测算**：全仓 3606 条语句，当前覆盖 3326（92.24%）。达到 95% 需覆盖 ≥3426 条，即需新增覆盖 **100 条**。8 个目标文件共 155 条未覆盖，**覆盖其中 ~65% 即可达标**，目标可达且有余量。
