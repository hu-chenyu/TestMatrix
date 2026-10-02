# TestMatrix 大扫除 v2 · 第二轮代码审查报告

| 项 | 值 |
|---|---|
| 审查日期 | 2026-10-02 |
| 审查范围 | `git diff 9984d72 HEAD`（v1 起点 → 当前 HEAD `96af3cd`） |
| 规模 | 39 个文件，+7281 / −219 行 |
| 审查重点 | v1 新改的代码有没有引入**新** bug、修复有没有**真正生效**、测试有没有**假通过** |
| 发现总数 | **22 条**（P0×1 / P1×3 / P2×8 / P3×10） |
| 实证方式 | 源码级推演 + 独立脚本实证（正则直接 exec 源码定义、SQLAlchemy `make_url` 解析、`git ls-files --eol`），**关键结论均已由审查者本人复验** |

## 审查方法与分工

v1 改动 39 个文件、7000 余行，单线程通读会遗漏。按类别分四路并行审查，每路都要求"只读 + 必须实证 + 不确定不报"：

1. **安全类**（http_client / exceptions / base / app / config / base.html / main.js / dashboard.js / notification）—— 10 文件
2. **竞态与健壮性**（executions / task_queue / event_bus / cache / case_manager / serial_client / env_manager / db_session）—— 8 文件
3. **新增测试质量**（9 个 v1 新测试文件 + 4 个被改断言的既有文件）—— 13 文件
4. **数据层与工具层**（data_driver / report_analyzer / db_session / cases / reports / notifications / logger / serial_client / .gitattributes）—— 9 文件

**范围校正**：审查中发现 `src/core/event_bus.py`、`src/web/routes/reports.py`、`src/web/routes/notifications.py` 在 `9984d72..HEAD` 区间**零改动**（`git diff --numstat` 为空）。v1 简报把它们列为"新改"是错误的，实际改动文件为 34 个。

---

## 一、P0（1 条）

### V2-P0-1 安全断言假通过：控制台 sink 的 `diagnose=False` 修复完全未被验证

- **位置**：`tests/test_security_hardening_demo.py:1256-1295`（测试）、`src/common/logger.py:112`（被测）
- **问题**：该测试 docstring 明确写"回归点: 修复前**控制台 sink** 未设 diagnose=False（两个文件 sink 都设了）"。但测试第 1276 行调用 `LogManager.setup(..., console_output=False)`，而 `logger.py:102` 的控制台 sink 被 `if console_output:` 守卫 —— **该 sink 根本没有被创建**。断言（1290 行）只读 `log_dir.glob("*.log")`，即两个**修复前就已经是 `diagnose=False`** 的文件 sink（`logger.py:131/145`）。
- **后果（实证推演）**：把 `logger.py:112` 的 `diagnose=False` 改回 `True`（即撤销本次修复本身），落盘文件内容**逐字节不变**，`assert "Sup3rSecretDbPass" not in content` **仍然通过**。这是全仓唯一一条对安全修复零感知的假通过。
- **修复建议**：改用 `console_output=True` + loguru 测试 sink 捕获 stderr（`logger.add(sink_list.append, ...)`）后再断言；或直接断言 `loguru_logger._core.handlers` 中 console sink 的 `diagnose is False`。

---

## 二、P1（3 条）

### V2-P1-1 查询串脱敏存在未闭合的并行路径：`params` 字典明文进日志

- **位置**：`src/common/http_client.py:292-297`（前置日志）、`:388-410`（`_mask_data`）
- **问题**：v1 新增的 `SENSITIVE_QUERY_FIELDS`（含 `key`）只作用于 URL 字符串（`_safe_url`）。而 requests 生态传查询参数的正规做法是 `params=` 字典，该路径走 `_mask_data`，用的是**不含 `key` 的** `SENSITIVE_BODY_FIELDS`，且是精确匹配。模块自己的用法示例（`http_client.py:16`）就是 `client.get("/get", params={"key": "value"})`。
- **后果**：`client.get("/cgi-bin/webhook/send", params={"key": "<真实企微 key>"})` → 前置日志输出 `params: {'key': '<真实 key>'}` 明文。**同一份凭据写进 URL 会被打码、走 `params` 不会** —— 正是 v1 声称要堵的那类缺口，只补了 URL 一侧。
- **注意**：这与 v1 之后 D 阶段"刻意不把裸 `key` 收进 `SENSITIVE_BODY_FIELDS`"的决策直接相关。那个决策对 **JSON body** 是对的（body 里叫 `key` 的通常是业务数据），但 `params` 字典**语义上就是查询串**，应当按查询串口径脱敏。
- **修复建议**：让 `_mask_data` 接受字段表参数，前置日志对 `params` 传 `SENSITIVE_QUERY_FIELDS`，对 `json`/`data` 传 `SENSITIVE_BODY_FIELDS`。

### V2-P1-2 Windows 含空格路径的脱敏不仅失效，还会"打码安全前缀、留下敏感后缀"

- **位置**：`src/web/routes/cases.py:492`（`_WIN_ABS_PATH`）
- **问题**：字符类 `[^\s\]\)'\"，、]+` 排除空白。`C:\` 之后一旦遇到空格，整条正则在任何起点都匹配失败。
- **后果（审查者已用 exec 源码正则实证）**：

  | 输入 | 实际输出 | 应为 |
  |---|---|---|
  | `C:\Users\John Smith\AppData\Local\Temp\t\cases.yaml` | `<path> Smith\AppData\Local\Temp\t\cases.yaml` | `<path>` |
  | `C:\Program Files\TestMatrix\data\cases.yaml` | **原样回显，零匹配** | `<path>` |

  第二行是完全泄露；第一行更隐蔽——安全的 `C:\Users\` 前缀被打码，**操作系统账户名与其后全部路径反而留下**，看起来像生效了。Windows 用户名含空格是常见形态，且 `tempfile.mkdtemp` 落在 `%TEMP%` 下，消息经 `cases.py:628` 抛 `ValidationError` → 前端 toast 展示给终端用户。
- **为何没被测出**：`tests/test_cases_import_sanitize.py:38-41` 的样本用 `ci_user`（无空格），恰好绕开。
- **同文件不一致**：v1 恰在这段代码正下方新增了 `_sanitize_log_field`（`cases.py:508`）却没动正则，形成"同文件两个脱敏器、一个有洞"。
- **修复建议**：路径段字符类允许空格，改用终止符匹配（如 `r"[A-Za-z]:\\[^\r\n\"']*?\\([^\r\n\"']+)"`），并补一条含空格用例的回归测试。

### V2-P1-3 `stop_worker` 无条件清空模块级引用，可被 `start_worker` 插队导致第二个 worker

- **位置**：`src/core/task_queue.py:663-666`
- **问题**：v1 在 join 超时（`:658`）与 join 异常（`:661`）两条路径上正确地 `return` 并保留引用。但**正常结束路径**的 `with _state_lock: _worker_thread = None` 没有校验"我 join 的那个 thread 仍是当前 `_worker_thread`"。
- **可复现时序**：
  1. T1 `stop_worker()`：`:637` 取到 `thread=A` 后**释放锁**，`:645` join 最长 3.0s（join 期间释放 GIL）
  2. A 在 join 期间正常退出
  3. T2 `create_app()` → `start_worker()`：`is_alive()==False` → 建 B，`_worker_thread=B`
  4. T1 join 返回 → `:663` 把 `_worker_thread` 置 `None`，**B 正在跑但引用被抹掉**
  5. 下次 `start_worker()` 见 `None` → 建 C。B 与 C 并发 BRPOP 同一队列，"单 worker 串行"被破坏，且 `stop_worker` 再也停不掉 B
- **注意**：`:648-656` 的注释断言"start_worker 的存活检查据此不会重复起 worker"——该断言在此路径上不成立。
- **修复建议**：`:663` 改为条件清空 `if _worker_thread is thread: _worker_thread = None`（3 行改动）。

---

## 三、P2（8 条）

### V2-P2-1 MySQL 连接串在 userinfo 段用错编码函数，含空格的凭据拼出错误 DSN

- **位置**：`src/db/db_session.py:115`（`quote_plus(user)` / `quote_plus(password)`）
- **问题**：`quote_plus` 把空格编码成 `+`，而 `+` 只在 `application/x-www-form-urlencoded` 语境下代表空格。SQLAlchemy 解析 userinfo 用的是 `unquote`（非 `unquote_plus`）。
- **后果（审查者已用 `SQLAlchemy.make_url` 实证）**：
  ```
  mysql+pymysql://test+user:pw@...  => username='test+user'    ← 连不上
  mysql+pymysql://test%20user:pw@... => username='test user'   ← 正确
  ```
  `TM_DB_MYSQL_USER="test user"` 会以字面用户名 `test+user` 认证失败，密码同理。password 侧是 v1 之前既有问题，v1 把 `user` 纳入编码方向正确但选错了函数，**扩散了同一缺陷**。
- **修复建议**：user/password 两段改用 `urllib.parse.quote(..., safe="")`（userinfo 段空格的正确转义是 `%20`），并更新 `tests/test_stage_c_infra_coverage_demo.py` 中锁死 `p%40ss+w%2Frd%231` 的断言。

### V2-P2-2 `db:1` 在三条降级路径上被复用为三种语义，重连会永久丢失 `batch_start`

- **位置**：`src/web/routes/executions.py:687-692`、`:699`、`:711`
- **问题**：`db:1` 在正常 DB 重建里是 `batch_start`，在明细查询失败降级（`detail=None`，`next_frame_id` 停在 1）里变成 `batch_finished`，在 failed/running 单帧直发里又是 `batch_finished`/`batch_start`。
- **后果**：请求 #1 走 finished 分支但 `get_execution_detail` 瞬时失败（`:651` 吞掉）→ 客户端只收到 `db:1`(batch_finished)；请求 #2 重连带 `Last-Event-ID: db:1`，此时 DB 已恢复 → `resume_db=1` → `:660` 的 `1 > 1` 为假，**`batch_start` 被永久过滤**，客户端永远拿不到 `total_cases`。
- **修复建议**：降级帧改用固定保留序号（如 `db:0`）或不参与断点过滤。

### V2-P2-3 `description` 非空时 tags 被整体丢弃，数字标签归一化扩大了触发面

- **位置**：`src/core/case_manager.py:2283-2287`（`_build_description`）
- **问题**：`test_cases` 表**没有 tags 列**（`src/db/models.py`），tags 靠序列化进 `description` 暂存、再由 `_parse_tags_from_description` 反解。`_build_description` 在 `custom_desc` 非空时直接 `return custom_desc`，**tags 分支永不执行**。
- **后果**：v1 之前 `description=2024`（Excel 数值）会因非 str 被拒；v1 引入 `_normalize_scalar` 后它变成合法字符串 `"2024"`，于是命中 `custom_desc` 分支 —— **一整行用例的 tags 被静默丢弃**，随后 `cases.py:224` 的交集筛选恒不命中。
- **注**：该方法的 docstring 写明"数据自带 description 字段时优先使用原描述"，属已记录的设计取舍。但"取描述"与"丢标签"不应是同一个决定。
- **修复建议**：改为拼接（`custom_desc` + `\n标签: ...`），或让 `_parse_tags_from_description` 支持行内提取。
- **关联**：v1 阶段 C 的 `test_numeric_tags_normalized_to_string` 断言的归一化行为本身正确，但其适用边界（tags 能否最终落库）未在该测试中体现。

### V2-P2-4 `except IntegrityError` 覆盖面过宽，NOT NULL 违例被误报为 409

- **位置**：`src/core/case_manager.py:767-779`
- **问题**：`IntegrityError` 覆盖全部完整性违例。`test_cases` 全部 8 个业务列均 `nullable=False`，而 `create_case` 用 `payload.get("module", "default")` 取值 —— **键存在但值为 `None` 时 `.get` 的默认值不生效**，返回 `None` → flush 抛 NOT NULL `IntegrityError` → 被翻译成 `CaseConflictError("用例编号已存在")` → 409，真因被掩盖。
- **暴露面**：HTTP 路径上 marshmallow 会先拒 `None`，故仅内部调用方可达。
- **修复建议**：只对唯一约束判定（如检查约束名，或在 except 内判断 `case_id` 是否已存在），否则回落 `except SQLAlchemyError`。

### V2-P2-5 非 UTF-8 的 YAML 让 `UnicodeDecodeError` 原样逃逸，未被包装为 `DataDriverError`

- **位置**：`src/core/data_driver.py:267-269`
- **问题**：`_load_yaml` 只 catch `(yaml.YAMLError, OSError)`，而 `UnicodeDecodeError` 继承自 `UnicodeError → ValueError`，两条 except 都接不住；且解码发生在 `safe_load(file_handle)` **读取句柄**阶段，不在 `open()` 阶段，前一处也覆盖不到。
- **后果（审查者已实证）**：用例导入遇到 GBK 编码的 YAML 返回 500，响应体是裸的 codec 错误文本，而不是 400 + "文件编码错误，请用 UTF-8"。
- **修复建议**：`except` 增加 `UnicodeError`（或改捕获 `ValueError` 并按 `yaml.YAMLError` 先后分流），错误文案明确指向编码。
- **注**：v2 任务一的测试已如实记录当前行为（`tests/test_v2_data_driver_coverage.py::test_non_utf8_yaml_escapes_as_unicode_decode_error`），修复时改为断言 `DataDriverError` 即可。

### V2-P2-6 `SECRET_KEY` fail-fast 的生效条件依赖另一个更易遗漏的变量

- **位置**：`src/web/config.py`（`get_config` 默认 `TM_ENV` → `"dev"`；`resolve_secret_key` 仅在 `config_class is ProductionConfig` 时抛错）
- **后果**：生产部署漏配 `TM_ENV` → 拿到 `DevelopmentConfig` → **fail-fast 不触发**，退回进程级随机 key（session 跨进程/重启必然失效），且 `DevelopmentConfig.DEBUG=True` 还会一并开启 Werkzeug 调试器。fail-fast 只在 `TM_ENV=prod` 这一条正路上成立。
- **性质**：非 v1 引入的回归（v1 前行为相同），而是 **v1 修复的覆盖缺口** —— "生产"由配置类身份判定，而配置类身份又由一个未校验的环境变量决定。
- **修复建议**：改为正向判定 —— 只要 `TM_ENV` 不是 `dev`/`test` 就要求 `TM_SECRET_KEY`；或在 `DEBUG=True` 时对缺失 `TM_SECRET_KEY` 也告警一次。

### V2-P2-7 三条测试是"不抛异常"式假验证，无法区分"正确吞掉"与"压根没发生"

- **位置**：
  - `tests/test_p1_hardening_demo.py:237-253` —— **全函数零断言**，注释写"再读回确认确实未写入"但代码里没有任何读回
  - `tests/test_stage_c_support_coverage_demo.py:491-512` —— 注释直书"不抛异常即为通过"，零断言
  - `tests/test_stage_c_support_coverage_demo.py:563-585` —— 同型
- **修复建议**：改为状态断言，例如 `assert cache_client.get_json("tm:test:key") is None`；`publish`/`close` 两条应断言通道状态符合预期而非仅不抛异常。

### V2-P2-8 前端语义断言的正则未按函数作用域锚定，存在跨函数误捕获

- **位置**：`tests/test_frontend_race_demo.py:191`（`r"catch\s*\(error\)\s*\{([\s\S]*?)finally\s*\{"`）
- **问题**：`cases.js` 中有 **5 处** `catch (error)` 与 5 处 `finally`，`re.search` 取的是**全文件第一个** `catch (error)` 到最近 `finally` 之间的整段。一旦更靠前的函数新增一个 `catch (error) {`，捕获区会跨越函数边界膨胀，只要任意位置存在 `state.total = 0` 就通过 —— **"代码存在但逻辑仍错"会漏报**。
- **修复建议**：先用 `async function loadCases` 定位函数体切片，再在切片内做 catch 匹配。

---

## 四、P3（10 条）

| # | 位置 | 问题 | 修复建议 |
|---|---|---|---|
| V2-P3-1 | `src/web/exceptions.py:155-167` | `handle_500` 近乎死代码：Flask 2.3 对非 HTTPException 只沿 MRO 查类处理器，必然先命中 `Exception` 兜底；实测仅 `after_request` 阶段异常可达。其 TESTING 分支 `error(str(original), 500)` 实际永不执行 | 删除，或注释为"仅响应收尾阶段可达"，避免维护者误判 |
| V2-P3-2 | `src/common/serial_client.py:305-311` | `read_until(timeout=0)` 恒抛超时：`while time.monotonic() < deadline` 在**首次读之前**判定，循环体一次都不执行。v1 把 `timeout or self.timeout` 改成"0 即零等待"，实际变成"必然失败" | 改 do-while（先读一次再判 deadline），使 0 表示"非阻塞排空缓冲区" |
| V2-P3-3 | `src/db/db_session.py:276-297` | `health_check` 是死代码：全仓无 `src/` 调用点，`/health` 走 `base.py:114` 的 `_check_database()`。连带**注释漂移**：`db_session.py:9` 仍写"Web平台健康探活" | 删除该方法，或修正模块 docstring 并在方法 docstring 标注"仅自检用" |
| V2-P3-4 | `src/web/static/js/main.js:47-59` 与 `cases.js:108-118` | `escapeHtml` 两份定义，行为当前等价但 `cases.js` 顶层函数声明会覆盖 `main.js:159` 设置的 `window.escapeHtml`。安全基元有两个事实来源 | `cases.js` 复用 `window.escapeHtml`，删除本地副本 |
| V2-P3-5 | `src/web/routes/executions.py:723-737` | `resume_live ≥ 终态事件 id` 时全部事件被跳过、`saw_terminal=True`，兜底不触发 → **响应体 0 帧**；服务端返回即断流，浏览器按 SSE 规范约 3s 后自动重连并携带同一 `live:N` → 无限热循环 | 零帧时至少输出一条注释帧，或补一个可判定的终止标记。**注**：`tests/test_v2_executions_coverage.py:438-465` 显式断言该行为为有意设计 |
| V2-P3-6 | `src/web/routes/cases.py:493-496` | Unix 前缀元组缺 `etc`/`srv`/`data`/`mnt`/`www`。审查者已实证 `/data/uploads/secret/cases.yaml`、`/etc/testmatrix/upload/cases.yaml` 均零匹配 | 补全前缀元组（当前 tmp 场景够用，但函数语义是通用脱敏器） |
| V2-P3-7 | `src/db/db_session.py:116` | IPv6 / 含端口 host 拼出畸形 DSN（不加方括号）。已实证 `mysql+pymysql://root:pw@::1:3306/db` → `ValueError: invalid literal for int()`；`TM_DB_MYSQL_HOST="127.0.0.1:3307"` 同样 ValueError | host 含 `:` 且无 `[` 时补方括号，或校验拒绝 |
| V2-P3-8 | `src/core/case_manager.py:794` | `invalidate_cases_list()` 裸调未做 try 隔离（v1 为 `invalidate_reports()` 在 `:1788/1852` 加了独立 try）。缓存层若抛异常，用例已创建成功却返回 500。属口径不一致 | 抽出统一的 `_safe_invalidate(fn, ctx)` 包装 |
| V2-P3-9 | `.gitattributes:17-21` | 落地未完成：`git ls-files --eol` 实证索引侧全部 `i/lf`（属实），但工作区侧 `w/crlf` 遍布 24 个文件，`.gitignore` 为 `w/mixed`。`eol=lf` 只在**下次 checkout** 生效，新增文件不改写已存在的工作区文件 | 执行一次 `git add --renormalize .` 后重检出；并统一 `.gitignore` 混行 |
| V2-P3-10 | `src/web/routes/cases.py:647` | 响应体 `file_name: original_name` 未脱敏，与 `cases.py:642` 的日志口径不一致。**已确认非 XSS**（toast 走 `textContent`，浏览器自动转义），仅口径与观感问题 | 统一脱敏口径 |

---

## 五、复核推翻的 v1 旧结论（3 条）

审查过程中发现 v1 阶段 D 报告里的三条结论本身有问题，此处更正，避免后续照单执行：

1. **"MySQL 分支 user/host/database 未做 URL 编码"——部分错误**。host 是主机名/IP、database 是路径段名，两者按字面量解析，**编码后反解析会失败**。正确结论是只补 `user`，且用 `quote` 而非 `quote_plus`（见 V2-P2-1）。
2. **"`serial_client.py` 为 CRLF，其余文件为 LF"——不准确**。仓库内全部 blob 本身就是 LF，CRLF 只是本机 `core.autocrlf=true` 的工作区产物（残留问题见 V2-P3-9）。
3. **"数字型 tags 被归一为字符串是设计"——只对了一半**。归一化行为本身正确，但 tags 的落地通道（序列化进 `description`）在描述非空时会被整体丢弃（见 V2-P2-3）。

---

## 六、经实证确认无问题的部分

以下为审查中重点验证、确认**没有问题**的项，附判据，避免后续重复排查：

| 项 | 判据 |
|---|---|
| 前端转义函数无漏字符 | `main.js:48` 的 `/[&<>"']/g` 对文本节点与双引号属性上下文完整；`cases.js` 链式 replace 等价且 `&` 先替换无双重编码。**但它不是 URL 上下文消毒器**（不处理 `javascript:`），当前无 `href` 注入汇点，暂不构成漏洞 |
| CSP 未过严也未形同虚设 | 实测全部模板仅通过 `url_for('static')` 加载脚本，零内联 `<script>`、零 `onclick=`/`onload=`、零外链 CDN，故 `script-src 'self'` 可正常放行；`style-src 'unsafe-inline'` 确有行内 style 需求 |
| 脱敏未误杀业务数据 | v1 扩表只增 `access_token`/`api_key`/`apikey` 等凭据名，`status`/`name`/`module` 不在表内 |
| `/health` 生产态不泄露 | `base.py:92` 对外摘要仅 `type(exc).__name__`，完整异常（含连接串）只进 `logger.warning`；`base.py:139-141` 的 TESTING 门控方向正确 |
| `notification.py` 凭据脱敏到位 | `_mask_webhook_url` 剥离 query/fragment，异常路径只打类型名不打 `str(exc)`；`_esc` 用 `html.escape(quote=True)` 覆盖全部 5 字符 |
| SSE 双编号体系真隔离 | `resume_live`/`resume_db` 严格按 `last_namespace` 二选一，跨体系恒为 0 → 全量重发 |
| 旧格式裸整数按 live 解释不丢帧 | 错位前提是"客户端从 DB 分支拿到裸 id≥2 且通道仍存活"；而 DB 分支产出裸 id≥2 要求 finished 批次，此时通道已清理，重连仍走 DB 分支 → `resume_db=0` 全量重发 |
| 6 个异常抛出点类型化无遗漏 | `grep` 确认 `cases.py`/`executions.py` 全部改按类型捕获，无残留子串匹配；核心层 6 个语义点均已换成具名异常子类 |
| `dequeue` 载荷结构校验位置正确 | `isinstance(dict)` 判定置于 `json.loads` 之后、`return` 之前，畸形消息走跳过路径保 worker 存活 |
| 缓存构建期降级边界正确 | 异常类型窄（`ValueError`/`TypeError`/`RedisError`），`_backend` 保持 `None` 允许自愈；三处调用点均已是"先取后端再 try" |
| `_execute_batch_async` 缓存隔离边界正确 | finished 路径在业务成功之后、failed 路径在无嵌套 try 处，两处 `except Exception` 语义均不篡改批次结果 |
| 被修改的 4 个既有断言未被削弱 | `test_serial_client.py` 由 1 条子串变 2 条（覆盖面扩大）；`test_web_app_factory_demo.py` 仅增强；`test_web_response_exception_demo.py` 纯环境变量补齐 |
| v1 时序测试合规 | 9 个新文件中仅 3 处 `time.sleep`，全部位于轮询循环内，无固定等待赌时序 |
| v1 测试隔离性合规 | 模块级全局状态（`tq._worker_thread`、loguru handlers、事件注册表）均有 fixture 前后还原；`import_client` fixture 显式 `init_db()`，已规避"依赖本地残留表" |

---

## 七、建议修复顺序

| 优先级 | 条目 | 理由 |
|---|---|---|
| 第一批 | V2-P0-1、V2-P1-1、V2-P1-2 | 均为安全类，且 V2-P0-1 是"安全断言本身失效"，不修则后续安全修复仍无验证能力 |
| 第二批 | V2-P1-3、V2-P2-1、V2-P2-2 | 各为 1~3 行改动，但都破坏了 v1 注释/测试已宣称解决的不变量 |
| 第三批 | V2-P2-5、V2-P2-6、V2-P2-7、V2-P2-8 | 功能缺口与测试强度 |
| 第四批 | V2-P2-3、V2-P2-4 | 需产品确认期望形态后再动 |
| 待定 | V2-P3 全部 | 含 3 条需产品口径确认（escapeHtml 收敛、零帧流终止标记、.gitattributes 重检出） |
