# TestMatrix 大扫除 v4 · 第三轮代码审查报告（Hy4-Preview）

| 项 | 值 |
|---|---|
| 审查日期 | 2026-10-02 |
| 审查人 | Hy4-Preview（独立审查，未参考 MiniMax 报告） |
| 审查范围 | `git diff 080f967..HEAD`（v3 改动）+ 全仓 src/ tests/ static/js/ templates/ |
| 规模 | 21 个文件，1921 insertions / 127 deletions |
| 审查重点 | v3 修复有效性验证 + 前两轮遗漏 bug + v3 新代码质量 |
| 发现总数 | **8 条**（P0×0 / P1×1 / P2×3 / P3×4） |
| 实证方式 | 源码级推演 + 独立脚本实证（loguru 真实渲染实验、Flask test_client 真跑 SSE、`make_url` 解析、正则直接 exec）+ 变异验证（源码级文本变异） |
| 基线核对 | `git log --oneline -5` 与预期 5 个 commit 一致；`git rev-list --count HEAD`=107；`git status --short` 仅 `M PROJECT_PLAN.md` —— **基线符合，审查继续** |
| 亲跑基线 | `py -m pytest -q` → **1013 passed / 0 failed / 0 rerun / 0 warning**（99.61s） |

---

## 一、v3 修复有效性验证（17 条逐条复核）

| 编号 | 修复内容 | 验证结果 | 实证证据 |
|---|---|---|---|
| V2-P0-1 | 控制台 sink 守卫 | ⚠️ **部分生效** | 源码级守卫本身有效（见下），但配套的**行为测试对该变异无牙齿**，且**源码注释两条安全结论实证为假** → 升级为新发现 V3-P1-1 |
| V2-P1-1 | params 脱敏 | ✅ 真正生效 | `http_client.py:323` 已传 `SENSITIVE_QUERY_FIELDS`；测试 parametrize 含 `key`，撤销修复必红。残留非 dict 形态未覆盖 → 新发现 V3-P3-1 |
| V2-P1-2 | 路径脱敏正则 | ✅ 真正生效 | 9 个样本实测全部收敛（含 `C:\Program Files\...`、`/data/...`、`/etc/...`），详见第四节 |
| V2-P1-3 | stop_worker 条件清空 | ✅ 真正生效 | `task_queue.py:671-675` 条件清空 + 对照组 `test_reference_cleared_when_no_replacement`；桩生命周期真实（join 前 alive、join 后 dead），非假通过 |
| V2-P2-1 | MySQL quote 替换 | ✅ 真正生效 | `db_session.py:146-147` 改用 `quote(..., safe='')`，空间→`%20` 而非 `+` |
| V2-P2-2 | db 降级帧不占序列位置 | ✅ 真正生效 | **端到端实证**：降级帧 `ids: []`；无头重连 → 全量重发 `db:1/2/3`。但"核心回归测试"无牙齿 → 新发现 V3-P2-1 |
| V2-P2-3 | tags 拼接 | ✅ 真正生效 | 四种组合 + 端到端往返（`select_cases_for_execution(tags="smoke")` 命中） |
| V2-P2-4 | IntegrityError 收窄 | ✅ 真正生效 | 含 `assert not isinstance(excinfo.value, CaseConflictError)` 反向断言（关键：否则 `CaseConflictError` 是 `CaseManagerError` 子类，`pytest.raises(CaseManagerError)` 无法区分） |
| V2-P2-5 | UnicodeError 包装 | ✅ 真正生效 | `data_driver.py:267` 在 `YAMLError` 与 `OSError` 之间插入 `UnicodeError`；对照组验证语法错误仍走 YAMLError 分支 |
| V2-P2-6 | SECRET_KEY 部分修 | ⚠️ **部分生效（已知）** | 正向判定已落地；"漏配 TM_ENV → dev → 随机密钥 + DEBUG=True" 缺口**仍在**，注释已诚实标注。补充低成本闭合方案见第三节 |
| V2-P2-7 | 三条假验证 | ✅ 真正生效 | 三条均已补"可观测证据"：读回 `is None` + `_backend is None` / `scan_calls == ["tm:cache:cases:*"]` + warning 留痕 / `append_calls == 1` + error 留痕 |
| V2-P2-8 | 前端正则锚定 | ✅ 真正生效 | 新增 `_extract_function_body`（花括号配对），匹配范围 30937 → 1696 字符 |
| V2-P3-2 | 串口 do-while | ✅ 真正生效 | `serial_client.py:322-332` 先读后判 deadline；三条用例覆盖 0 超时有数据 / 无数据 / 正超时轮询 |
| V2-P3-6 | Unix 前缀补全 | ✅ 真正生效 | `/etc`、`/srv`、`/data`、`/mnt`、`/www` 实测均收敛 |
| V2-P3-7 | IPv6 方括号 | ⚠️ **半修** | IPv6 `::1`→`[::1]` 正确；但 `host:port` 形态被误加方括号 → 新发现 V3-P2-3 |
| V2-P3-8 | 缓存隔离 | ✅ 真正生效 | `_safe_invalidate` 收口 4 处裸调；`test_create_succeeds_when_cache_invalidate_raises` 验证创建仍成功且真落库 |
| V2-P3-10 | file_name 脱敏 | ✅ 真正生效 | `cases.py:680` 与日志同走 `_sanitize_log_field` |

**汇总**：17 条中 **13 条真正生效**、**3 条部分生效**（V2-P0-1 / V2-P2-6 / V2-P3-7）、**1 条（V2-P2-2）修复生效但配套核心回归测试无牙齿**。

### 1.1 V2-P0-1 源码级守卫的变异验证（回答"能否挡住改回 diagnose=True"）

守卫逻辑：`inspect.getsource(LogManager.setup)` → `split("if console_output:")[1].split("# 文件输出通道")[0]` → 剥注释行 → 断言 `diagnose=False in code` 且 `diagnose=True not in code`。

先确认切片确实是控制台 sink（实测输出）：

```
logger.add(
    sys.stderr,
    level=log_level,
    format=log_format,
    filter=lambda record: record["extra"].setdefault("trace_id", "-") or True,
    enqueue=True,
    diagnose=False,
    backtrace=False,
)
```

再对四种变异跑守卫逻辑（`(含diagnose=False?, 含diagnose=True?)`）：

| 变异 | 结果 | 是否变红 |
|---|---|---|
| 当前代码 | `(True, False)` | 绿 |
| 翻转 `diagnose=False`→`True` | `(False, True)` | **红**（两条断言双杀） |
| 改写成 `diagnose = True`（带空格） | `(False, False)` | **红**（第一条断言兜住） |
| 整行删除 | `(False, False)` | **红** |

**结论：守卫位置正确、逻辑有效**，能挡住"把 diagnose=False 改回 True / 改写格式 / 删掉"三类改动。

但守卫挡不住的是**行为层**：见 V3-P1-1。

---

## 二、新发现的 bug

### P0（0 条）

无。

### P1（1 条）

#### V3-P1-1 `logger.py` 与测试 docstring 的安全结论实证为假：loguru 0.7.2 的 `diagnose=True` **确实会输出局部变量值**，且 `log_format` 不含 `{exception}` **不阻止 traceback 渲染**

- **位置**：
  - `src/common/logger.py:109-124`（源码注释"口径更正"）
  - `tests/test_security_hardening_demo.py:1274-1285`（`test_local_variables_not_leaked_into_console` docstring）
  - `tests/test_security_hardening_demo.py:1335-1348`（`test_log_format_does_not_render_exception` docstring）
- **问题**：v3 在源码注释里写下两条"实测更正"，经本人独立复现，**两条均不成立**：

  **结论①（假）**：注释称"0.7.2 的 diagnose 只在 traceback 帧上渲染源码行与被调用表达式的求值结果（形如 `-> <function boom at 0x...>`），**不输出 f_locals 里的变量值**；8 种组合实测敏感局部变量值一次都没出现"。

  实测（loguru 0.7.2，真实 `.py` 源文件——linecache 能读到源码行时）：

  ```
  === diagnose=True ===
  捕获到数据库异常
  Traceback (most recent call last):

  > File "<string>", line 20, in <module>

    File "...\leakdemo.py", line 6, in boom
      return connect(db_password)
             |       -> 'Sup3rSecretDbPass'
             -> <function connect at 0x000001AAA3530C20>

    File "...\leakdemo.py", line 2, in connect
      raise RuntimeError('connect failed')

  RuntimeError: connect failed

  SECRET LEAKED: True
  ```

  `diagnose=False` 同一场景 `SECRET LEAKED: False`。即 **diagnose=True 会打印局部变量值**，条件是该变量出现在 traceback 中某一帧的**源码行**上（真实业务代码里 `connect(password=db_password)` / `login(pwd=secret)` 正是这种形态）。v3 的 8 组合之所以都没复现，是因为其敏感变量定义在**独立一行**（`db_password = "..."`），而异常发生行是另一行——`_raise_with_secret` 的 raise 行不含该变量。

  **结论②（假）**：注释称"真正的'变量值不进日志'由 log_format 不含 `{exception}` 保证……**任何 sink 上都不会输出 traceback**，diagnose 的取值在当前格式下没有可观测影响"。

  实测（4 种 diagnose × backtrace 组合 + 项目真实格式，格式串均**不含** `{exception}`）：

  ```
  backtrace=False diagnose=False -> traceback_rendered=True
  backtrace=False diagnose=True  -> traceback_rendered=True
  backtrace=True  diagnose=False -> traceback_rendered=True
  backtrace=True  diagnose=True  -> traceback_rendered=True

  项目真实 log_format + diagnose=False 的实际输出:
  '2026-10-02T21:53:32.347985+0800 | ERROR | __main__:14 | 捕获到数据库异常\n
   Traceback (most recent call last):\n  File "<string>", line 12, in run\n
     File "<string>", line 6, in boom\nRuntimeError: 数据库认证失败\n'
  ```

  loguru 0.7.2 **无论格式是否含 `{exception}` 都会渲染 traceback**。

- **后果**：
  1. 源码注释是**公开仓库里的安全结论**。它告诉后续维护者"diagnose=True 对变量值无害""traceback 根本不会进日志"。若有人据此为了排查方便把 `diagnose` 放开，凭据会**真实泄露**到控制台与日志文件（控制台输出常被 CI 日志采集器原样收走）。
  2. `test_local_variables_not_leaked_into_console` 名义上是"端到端行为守"，但它的敏感变量不在异常行上，**把 diagnose 改回 True 它仍然全绿**——v3 自己也承认这点并把责任推给源码级守卫。结果是：真正能挡住泄露的只有一条**文本断言**，没有任何运行时测试能证明"变量值不进日志"这条安全属性。
  3. `test_log_format_does_not_render_exception` 断言的是 `{exception} not in fmt`，但它声称的"这是当前真正生效的那道屏障"是错的；该断言实际守住的东西（格式串不含 `{exception}`）与变量值泄露**无因果关系**。
- **当前风险**：三个 sink 均为 `diagnose=False`，**无实际泄露**。本条是"安全结论错误 + 测试无牙齿"，非可利用漏洞，故不判 P0。
- **修复建议**：
  1. 改写 `logger.py:109-124` 的注释：删除"不输出 f_locals 变量值"与"任何 sink 都不会输出 traceback"两句，改为"实测 0.7.2 在变量出现在异常行时会渲染 `name -> value`，traceback 无论格式如何都会输出；diagnose=False 是**当前唯一**挡住变量值打印的屏障，不得放开"。
  2. 把 `test_local_variables_not_leaked_into_console` 的构造改成敏感变量**出现在异常发生行**（如 `raise ValueError(f"数据库认证失败: {db_password}")` 或 `connect(db_password)`），使其对 `diagnose=True` 变异真正变红——这才是"端到端行为守"应有的牙齿。
  3. `test_log_format_does_not_render_exception` 的 docstring 删除"真正生效的那道屏障"表述，改为"格式不含 {exception} 避免 traceback 被渲染**两次**；真正的变量值屏障是 diagnose=False"。

---

### P2（3 条）

#### V3-P2-1 `test_degraded_frame_does_not_break_reconnect` 是假验证：硬编码 `live:0`，撤销修复仍全绿

- **位置**：`tests/test_v2_executions_coverage.py:317-397`（v3 新增，约 80 行）
- **问题**：该用例 docstring 自称"V2-P2-2 核心回归"，复现"请求1 降级帧被标成 db:1 → 请求2 带 `db:1` 重连 → batch_start 被永久过滤"。但代码里请求2 的头是**硬编码** `headers={"Last-Event-ID": "live:0"}`（第 383 行），而 `live` 是**另一套编号体系**——按 `_parse_last_event_id_header` + 分支逻辑，`resume_db` 恒为 0，与请求1 到底有没有输出 id **完全无关**。
- **实证**（Flask test_client 真跑 `/api/executions/<id>/events`，先让明细查询失败再恢复）：

  ```
  req1 ids: []                      # 降级帧确实无 id（修复生效）
  req1 batch_finished: True

  hdr= {'Last-Event-ID': 'live:0'} -> batch_start: True  | ids: ['id: db:1', 'id: db:2', 'id: db:3']
  hdr= {'Last-Event-ID': 'db:1'}   -> batch_start: False | ids: ['id: db:2', 'id: db:3']
  hdr= None                        -> batch_start: True  | ids: ['id: db:1', 'id: db:2', 'id: db:3']
  ```

  关键：`db:1` 能让 `batch_start` 被过滤（证明原 bug 场景真实存在），而 `live:0` 恒等全量重发——**若把修复撤销（降级帧改回带 `db:1`），本用例请求2 仍用 `live:0` → 仍全量重发 → 断言仍绿**。
- **后果**：这条最花篇幅的"核心回归"测试无法区分修复前后。同组的三条简单断言（`_frame_ids(raw) == []`，见该文件 311-315 / 417-421 / 450-454）才有牙齿，修复实际是靠它们兜住的——但报告与用例 docstring 都把功劳记在了这条无牙齿的用例上，后续维护者会误以为重连场景已被端到端覆盖。
- **修复建议**：请求2 的 `Last-Event-ID` 由请求1 实际收到的 id 推导（`first_ids[-1] if first_ids else None`），而不是写死 `live:0`；并补一条"带 `db:1` 重连时 batch_start 被过滤"的对照断言，把两套编号体系的差异显式钉死。

#### V3-P2-2 v3 新增的 `test_safe_invalidate_swallows_and_logs` 是全函数零断言（"不抛异常即为通过"）

- **位置**：`tests/test_v3_robustness_demo.py:683-693`
- **问题**：函数体只有 `_safe_invalidate("测试操作", _boom)  # 不抛异常即为通过`，无一条 `assert`。docstring 写的是"`_safe_invalidate` 吞掉异常（不向上抛）"，未验证"**吞掉且记 warning 留痕**"这一半。这正是 v2 审查 V2-P2-7 批评过的同一形态，而 v3 修复 V2-P2-7 时给另外三条都补了"桩被调用过 + warning/error 留痕"的真断言，唯独这条自己新写的遗漏了。
- **实证**：AST 全仓扫描（`tests/**/test_*.py`，排除 `pytest.raises` 与 `assert_*` 助手函数）共 5 条零断言，其中 4 条是 `tests/api_demo/` 的旧用例（断言在 `_execute_query_case` 助手内，有牙齿），**真零断言仅此 1 条**。
- **说明（避免误判）**：本条**不是完全的空转**——`_boom` 必然抛异常，`_safe_invalidate` 若不再吞异常则用例会 error 变红，所以"吞掉"这一半有牙齿；缺口只在"warning 留痕"这一半。
- **后果**：若有人把 `logger.warning` 删掉（异常仍被吞），用例照样全绿，"吞掉但无痕迹"这种静默降级不会被发现——而"吞掉必须留痕"正是 V2-P3-8 的设计要点。
- **修复建议**：照抄同文件 `test_stage_c_support_coverage_demo.py` 的 `_LogCapture` 模式，断言 warning 中含操作名与异常类型名。

#### V3-P2-3 `_bracket_ipv6_host` 对"host:port"形态误加方括号，V2-P3-7 只修了一半，且把响亮失败变成静默错误

- **位置**：`src/db/db_session.py:65-67`（v3 新增函数）、`:148`（调用点）
- **问题**：判定条件 `if ":" in host and not host.startswith("[")` 把"含冒号"直接等同于 IPv6。而 v2 报告 V2-P3-7 列出的**第二个**症状是 `TM_DB_MYSQL_HOST="127.0.0.1:3307"`（host 带端口，不是 IPv6），该形态被同样加上方括号。
- **实证**：

  ```
  _bracket_ipv6_host('127.0.0.1:3307') -> '[127.0.0.1:3307]'
  _bracket_ipv6_host('::1')            -> '[::1]'
  _bracket_ipv6_host('[::1]')          -> '[::1]'
  _bracket_ipv6_host('localhost')      -> 'localhost'

  make_url('mysql+pymysql://root:pw@[127.0.0.1:3307]:3306/db') -> host='127.0.0.1:3307' port=3306
  make_url('mysql+pymysql://root:pw@[::1]:3306/db')            -> host='::1'           port=3306
  make_url('mysql+pymysql://root:pw@127.0.0.1:3307:3306/db')   -> ValueError: invalid literal for int() with base 10: '3307:3306'
  ```

  IPv6 分支正确（V2-P3-7 一半已修）；host:port 分支得到 `host='127.0.0.1:3307'`——**主机名里嵌着端口**，PyMySQL 会去解析这个不存在的主机名。
- **后果**：修复前是 `ValueError`（构建 DSN 阶段响亮崩溃，一眼看出配置错）；修复后是构建成功、连不上，报错退化为 DNS/连接超时类信息，**排障方向被带偏**。虽然两种情况下都连不上（不会连错库），但可观测性变差了。
- **修复建议**：区分两种形态——`host.count(":") > 1`（IPv6 多冒号）才加方括号；单冒号形态要么按 `host:port` 拆分并把端口并进 `port`，要么显式 `raise ValueError` 提示"端口请用 TM_DB_MYSQL_PORT 配置"。

---

### P3（4 条）

#### V3-P3-1 `_mask_data` 对非 dict 形态的 `params`（元组列表 / 字符串 / 嵌套 dict）不脱敏

- **位置**：`src/common/http_client.py:417-443`
- **问题**：`_mask_data` 只处理 `dict`（逐键比对字段表）与"list 里的 dict"。`requests` 的 `params` 还支持**二元组列表**与**查询字符串**；`request()` 走 `**kwargs` 时也不受 `get(params: dict | None)` 的类型约束。嵌套 dict 中的凭据同样漏网。
- **实证**：

  ```
  tuple-list : [('key', 'SECRET1'), ('token', 'SECRET2')]   # 原样，未打码
  dict       : {'key': '***'}
  nested     : {'payload': {'password': 'SECRET3'}}         # 内层未打码
  str-params : key=SECRET4&token=SECRET5                    # 原样
  url        : https://h/p?key=%2A%2A%2A&token=%2A%2A%2A    # URL 侧正常
  ```

- **后果**：与 V2-P1-1 同类——"同一份凭据写进 URL 被打码、换个位置就明文"的并行路径未完全闭合。当前内部调用方全部传 dict，实际暴露面为零，故判 P3。
- **修复建议**：`_mask_data` 增加 tuple 分支（`isinstance(item, (list, tuple)) and len(item)==2` 时按 key 比对）与嵌套 dict 递归；或在 `request()` 里对 `params` 为 `str/bytes` 时复用 `_safe_url` 的查询串逻辑。补 2~3 条 parametrize 用例。

#### V3-P3-2 `close_channel` 先摘注册表后关通道，窗口内 publish 会新建孤儿通道（理论推演，未实证复现）

- **位置**：`src/core/event_bus.py:417-420`
- **问题**：`close_channel` 先 `_CHANNELS.pop()` 再 `channel.close()`。两步之间若另一线程调 `_publish_execution_event`（内部是 `get_channel(create=True)`），注册表已无该 key → **新建一个通道**并塞进注册表，而随后 `close()` 关掉的是旧通道。新通道无人关闭 → 内存泄漏；且持有旧通道引用的 SSE 订阅者永远看不到这个事件。
- **边界**：`_publish_execution_event` 与 `close_channel` 在 `_execute_batch_async` 中由**同一个线程**串行走，正常路径不可达；只有将来多 worker / 并发发布同一 execution_id 才会触发。
- **标注**：**待验证（静态推断）**，未构造出可复现时序，按"宁可少报"原则仅列 P3。
- **修复建议**：改为"先 `channel.close()` 再 `pop`"，或在 `_REGISTRY_LOCK` 内一次性完成两步；注意调换顺序会让竞态窗口内的 `get_channel(create=False)` 拿到已关闭通道（SSE 走分支二补发，语义可接受），需同步验证既有测试。

#### V3-P3-3 `_extract_function_body` 的 docstring 声称跳过注释，实现未处理 `//` 与 `/* */`

- **位置**：`tests/test_frontend_race_demo.py:31-91`（v3 新增）
- **问题**：docstring 明写"跳过字符串与注释"，实现只处理了引号状态机。JS 里 `// ... }` 或 `/* } */` 中的花括号会参与配对。
- **后果**：`//` 注释里出现 `{` → 深度不归零 → `AssertionError`（响亮，安全）；`//` 注释里出现 `}` → 深度提前归零 → **静默返回被截断的函数体**，后续 `re.search` 在一个错误的切片上做断言，可能退化为"任意位置存在目标赋值即通过"——正是 V2-P2-8 要消除的漏报。当前 `loadCases` 无此形态，故未触发。
- **修复建议**：状态机增加 `//` 行注释与 `/* */` 块注释两种跳过状态；或把 docstring 改成"跳过字符串（注释未处理）"，避免误导。

#### V3-P3-4 Unix 路径脱敏是固定前缀白名单，白名单外的绝对路径零匹配，且可能"中段匹配"产出畸形串

- **位置**：`src/web/routes/cases.py:521-524`（`_UNIX_ABS_PATH`）
- **实证**：

  ```
  '/app/config/cases.yaml'        => '/app/config/cases.yaml'    # 容器部署常见 /app，完全未脱敏
  '/build/ci/cases.yaml'          => '/build/ci/cases.yaml'      # 同上
  '/workspace/data/cases.yaml'    => '/workspacecases.yaml'      # 中段匹配 /data/... 被替换，输出畸形
  '/home/u/v a/x.yaml'            => 'x.yaml'                    # 正常
  ```

- **后果**：`/workspace/data/cases.yaml` 这类"上层目录不在白名单、下层恰好命中"的路径会被替换掉中间一段，得到 `/workspacecases.yaml`——既不完整脱敏，又输出了不可读的畸形文本。HTTP 导入路径实际只产生 `tmp/` 前缀（已被覆盖），故暴露面限于 CLI / 其它调用方，判 P3。
- **修复建议**：把"Unix 绝对路径"的判定从"前缀白名单"改为"以 `/` 开头 + 至少含一个分隔符"，白名单仅作为**额外**的启发式；或至少要求匹配起点位于字符串开头或空白/引号之后，避免中段匹配。

---

## 三、v2 暂缓的 5 条 P3 重新评估

| 编号 | 内容 | 当前状态（实证） | 建议 | 理由 |
|---|---|---|---|---|
| V2-P3-1 | `handle_500` 近乎死代码 | `src/web/exceptions.py:155-167` 仍在；`tests/test_stage_c_support_coverage_demo.py::_after_request_failing_app` 仍在构造 `after_request` 异常来触达它 | **降级 P4 + 修注释（1 行）** | 它**不是纯死代码**——`after_request` 阶段异常确实可达且有测试依赖。删除要连带删测试，收益为负。真正要修的是 docstring：补一句"仅响应收尾（after_request）阶段可达；视图内异常由 Exception 兜底处理器先命中"。 |
| V2-P3-3 | `health_check` 死代码 | `src/db/db_session.py:308` 仍在，`src/` 内零调用点；`db_session.py:9` 模块 docstring 仍写"Web平台健康探活"（**注释漂移未修**）；3 条测试覆盖三态 | **降级 P4 + 修注释（2 行）** | 作为部署自检/外部调用的公开入口有存在价值，且已被测试覆盖，删除要删 3 条测试。只需把模块 docstring 的"Web平台健康探活"改掉，方法 docstring 标注"无 src/ 调用点，仅部署自检与外部调用"。 |
| V2-P3-4 | `escapeHtml` 两份定义 | 仍在：`main.js:47`（挂 `window.escapeHtml`，`main.js:159`）与 `cases.js:108`（顶层函数声明，浏览器上会覆盖 `window.escapeHtml`）；`cases.js:1006` 还把它导出 | **现在修（低风险）** | 两份当前行为等价（实测字符类一致、`&` 先替换无双重编码），但安全基元两个事实来源是明确隐患。实测全仓测试**只有** `test_security_hardening_demo.py` 引用 `escapeHtml`，且全部指向 `main.js`/`dashboard.js`，**无测试断言 cases.js 的本地定义** → 删掉 `cases.js:108-118`、改用 `const escapeHtml = window.escapeHtml;` 不会影响任何既有断言。工作量约 10 行。 |
| V2-P3-5 | SSE 零帧无限重连 | 仍在：`executions.py:725-745` 分支二中 `resume_live ≥ 全部事件 id` → 0 帧且 `saw_terminal=True`；`tests/test_v2_executions_coverage.py:438-465` 显式断言该行为为有意设计 | **现在修（P2 级，需同步改 1 条断言）** | 服务端返回即断流 → 浏览器按 SSE 规范约 3s 自动重连并携带同一 `live:N` → 每次都 0 帧 → 热循环，日志与连接资源持续消耗。推荐方案 A（不改状态码）：**对终态帧豁免断点过滤**（终态幂等重发），保证每次重连至少 1 帧，前端收到终态后主动 `close()`；方案 B（改契约）：零帧时返回 HTTP 204（EventSource 遇 204 永久停止重连），但需改 API 契约与响应断言。选 A 风险更低，只需同步更新那条"断言当前行为"的测试。 |
| V2-P3-9 | `.gitattributes` 重检出 | `git ls-files --eol` 实证：索引侧 `i/lf` 152 个；工作区 `w/crlf` **31** 个、`w/mixed` 1 个（v2 时是 24 个，**在恶化**）；本地 `core.autocrlf=true`，`.gitattributes` 为 `* text=auto eol=lf` | **现在修（低风险，独立一次操作）** | 因索引 blob 本身已是 LF，`git add --renormalize .` **不产生 blob 变更**（不会有内容 diff），实质只是把 31 个工作区文件按 `eol=lf` 重写成 LF；`.gitattributes` 的 `eol=lf` 覆盖本地 `autocrlf=true`，重检出后不会再回退成 CRLF。风险可控，但：① 建议单独一次操作，不与其他改动混在一个 commit；② 先处理 `w/mixed` 那个文件与 `.gitignore`；③ 操作后跑全量 pytest + 观察一次 CI，确认零影响。 |

### 3.1 V2-P2-6 残留缺口的低成本闭合方案（补充）

v3 已诚实标注"生产部署漏配 `TM_ENV` → dev → 随机密钥 + `DEBUG=True`"未覆盖，并正确地指出"完全闭合需要新增配置契约"。这里给一个**不需要新契约**的低成本补法：在 `get_config()` 里，当 `env_name` 来自缺省（`os.getenv("TM_ENV")` 为 None）时打一条 **WARNING**："TM_ENV 未显式配置，已按 dev 缺省：DEBUG 开启且 SECRET_KEY 退化为进程级随机密钥，生产部署请显式设置 TM_ENV=prod"。3 行改动，不破坏"本地零配置可跑通"，把静默降级变成有痕迹的降级。

---

## 四、经实证确认无问题的部分

以下为重点验证后确认**没有问题**的项，附判据，避免后续重复排查：

| 项 | 判据 |
|---|---|
| `stop_worker` 条件清空逻辑正确 | 实测源码 `if _worker_thread is thread`；三种路径（join 超时保留 / join 异常保留 / 正常条件清空）均有断言；对照组 `test_reference_cleared_when_no_replacement` 证明不是"永远不清空" |
| `start_worker` 无同类竞态 | 全程持有 `_state_lock`（含 `thread.start()`），存活检查与赋值在同一临界区 |
| `event_bus` 锁内校验位置正确 | `publish` 的 `_closed` 校验与 `event_id` 分配、append 全在 `Condition` 临界区内；`_next_event_id` 单调递增；`snapshot()` 持锁拷贝 |
| `event_bus.subscribe` 游标定位正确 | `start_index = max(0, cursor + 1 - oldest_id)`；`yield` 严格在锁外；`close()` 后 drain 语义完整 |
| `IntegrityError` 收窄不会误伤真冲突 | `test_unique_violation_still_translated_to_conflict` 关闭串行预检、强制走 DB 唯一约束，仍得 `CaseConflictError`；三种方言消息（SQLite/PG/MySQL 形态）判定正确，NOT NULL 消息判定为 False |
| NOT NULL 违例当前不可达 | `models.py` 中 `test_cases` 8 个业务列均 `nullable=False` 且**均带 Python 侧 `default=`**（module="default"/priority="P2"/case_type="api"/status="active"/description=""/creator="admin"），显式传 None 时 SQLAlchemy 套用 default，不产生违例——v3 的"前提更正"**这一条是对的** |
| 路径脱敏（Windows 含空格）确实收敛 | 实测 9 个样本：`C:\Users\John Smith\AppData\...\cases.yaml` → `cases.yaml`；`C:\Program Files\TestMatrix\data\cases.yaml` → `cases.yaml`；`C:\a.yaml` → `a.yaml` |
| Unix 前缀补全确实生效 | `/data/uploads/secret/cases.yaml`、`/etc/testmatrix/upload/cases.yaml`、`/srv/www/x.yml`、`/mnt/d/f.yaml` 均 → basename |
| 多路径/散文场景未被贪婪匹配吞掉 | `/tmp/a.yaml then /var/b.yaml` → `a.yaml then b.yaml`（两条都收敛，正确）；`YAML语法解析失败: C:\Users\ci\AppData\x.yaml 第3行` → 保留行号定位信息 |
| `_build_description` 拼接与反解往返一致 | 四种组合（仅描述/仅标签/两者/都空）构建与 `_parse_tags_from_description` 反解全部匹配；端到端 `select_cases_for_execution(tags="smoke")` 命中带描述的用例 |
| 串口 do-while 三条语义正确 | 0 超时有数据→返回；0 超时无数据→立即抛超时（不空等一轮）；正超时→轮询 ≥3 次后取到数据 |
| 上传路径无遍历 | `suffix` 白名单前置校验 + `secure_filename(Path(name).stem)` + 白名单 suffix 重组；无用户可控路径进入 `Path(tmp_dir)/safe_name` |
| 响应体 `file_name` 与日志口径一致 | 两处均走 `_sanitize_log_field`（折叠控制字符 + 200 字符截断） |
| 全仓无 SQL 注入 | 全部走 ORM；仅 `text("SELECT 1")` 两处字面量；无 f-string / `%` 拼接 SQL |
| 全仓 JS 无 XSS 汇点 | `src/web/static/js`（排除第三方 `echarts.min.js`/`bootstrap.bundle.min.js`）内 `innerHTML` / `outerHTML` / `insertAdjacentHTML` / `eval(` / `new Function` / `document.write` **零命中** |
| 无裸 `except` / 调试残留 | `src/` 内无 `except:`；无 `breakpoint()` / `pdb` / `console.log` / `debugger` / `alert(` |
| 测试质量整体健康 | AST 全仓扫描：真零断言测试**仅 1 条**（V3-P2-2）；`tests/api_demo/` 的 4 条零断言用例断言在 `_execute_query_case` 助手内，有牙齿 |
| v3 新增两个大测试文件断言充分 | `test_http_client_demo.py`（268 行）含阳性对照（非敏感字段必须保留）与结构不变量（body ⊆ query）；`test_v3_robustness_demo.py`（693 行）A/C/D/E/F/G/H 各组均配对照组成对验证 |
| V2-P2-7 三条假验证已真修复 | 三条均补"桩被调用过 + 日志留痕"双证据，非空转通过 |
| V2-P2-8 前端正则已按函数作用域锚定 | `_extract_function_body` 花括号配对，匹配范围从 30937 收窄到 1696 字符；`assert match is not None` 避免无匹配时空转 |
| 全量回归零影响 | `py -m pytest -q` → 1013 passed / 0 failed / 0 rerun |

---

## 五、建议修复顺序

| 优先级 | 条目 | 理由 |
|---|---|---|
| **第一批** | V3-P1-1（loguru 安全结论实证为假） | 唯一 P1。当前配置安全，但**公开仓库源码注释里写着错误的安全结论**，会直接诱导后续维护者放开 diagnose 造成真实凭据泄露；同时补上行为测试的牙齿。工作量：注释改写 + 1 条测试构造调整 |
| **第二批** | V3-P2-1（SSE 重连测试假验证）、V3-P2-2（`_safe_invalidate` 测试零断言）、V3-P2-3（`host:port` 半修） | 前两条是"v3 修 V2 假验证"这一主题下的**同类遗漏**，不改的话下一轮还会再报；第三条是 v3 已宣称修完但只修了一半，且把响亮失败改成了静默错误 host |
| **第三批** | V2-P3-4（escapeHtml 收敛）、V2-P3-5（SSE 零帧重连）、V2-P3-9（`.gitattributes` 重检出） | 三条暂缓项经重新评估均可现在修且风险可控：escapeHtml 无测试依赖、零帧重连需同步 1 条断言、renormalize 不产生 blob 变更 |
| **第四批** | V3-P3-1（非 dict params 脱敏）、V3-P3-3（`_extract_function_body` 注释状态机）、V3-P3-4（Unix 前缀白名单） | 健壮性与代码质量；V3-P3-1 当前暴露面为零，可随下次 http_client 改动一并处理 |
| **降级/仅改注释** | V2-P3-1（handle_500）、V2-P3-3（health_check） | 实测均**非纯死代码**（handle_500 被 after_request 异常测试触达，health_check 有 3 条测试），删除收益为负；建议降级 P4 并只修正注释漂移 |
| **待验证** | V3-P3-2（`close_channel` 顺序） | 静态推断、未构造出可复现时序；建议先补一条"并发 publish 与 close_channel"的确定性竞态测试确认可达性，再决定是否改顺序 |

---

## 六、审查过程中遇到的问题与不确定项

1. **硬边界与变异验证的冲突**：任务要求"对安全类修复做变异验证（临时撤销修复后测试变红）"，但硬边界要求"不修改任何源码"。本人的处理是**不落盘修改仓库**，改为：① 对源码级文本断言类守卫，用 `inspect.getsource` + 内存字符串变异跑守卫逻辑（等价于撤销修复）；② 对运行时行为类，用独立脚本构造等价场景（loguru 真实渲染实验、Flask test_client 真跑 SSE）。仓库全程零改动，`git status` 复查仍为仅 `M PROJECT_PLAN.md`。
2. **实证脚本的落盘**：全部 `py -c` 单行执行；仅两处需要真实文件/linecache 的实验在系统 `%TEMP%`（仓库外）创建临时脚本，用完立即删除。
3. **不确定项**：V3-P3-2（`close_channel` pop-then-close）为静态推断，单 worker 线程模型下未找到可复现路径，已明确标注"待验证"，未计入确定结论。
4. **未深入的区域**（受篇幅与优先级限制，非结论）：`src/core/notification.py` 的重试/死信并发安全性、`src/web/routes/reports.py` 的参数边界、`src/web/routes/notifications.py`——本轮仅做了高风险模式 grep（无 SQL 拼接、无裸 except、无调试残留），未逐行审查。

---

*报告人：Hy4-Preview | 独立审查完成 | 报告路径：`docs/bug_audit_report_v3_hy4_20261002.md`*
