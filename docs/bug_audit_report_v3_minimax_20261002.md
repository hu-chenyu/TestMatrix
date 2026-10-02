# TestMatrix 大扫除 v4 · 第三轮代码审查报告（MiniMax）

| 项 | 值 |
|---|---|
| 审查日期 | 2026-10-02 |
| 审查人 | MiniMax（独立审查，未参考其他 AI 报告） |
| 审查范围 | `git diff 080f967 HEAD`（v3 改动 21 文件 +1921/-127）+ 全仓 src/ + static/js/ + templates/ |
| 起点基线 | commit `37f8eee`、107 次提交、1013 passed、覆盖率 99%、CI Run #44 全绿 |
| 审查重点 | ①v3 修复有效性验证 ②前两轮遗漏 bug ③v2 暂缓 5 条 P3 重评估 |
| 发现总数 | **20 条**（P0×0 / P1×1 / P2×4 / P3×15） |
| 实证方式 | 6 条修复的**变异验证**（仓库外沙箱执行）+ loguru 内部结构探针 + v2/v3 脱敏行为逐字对比 + 静态 AST/正则推演 |

> **本轮审查的自我纠错**：三条发现直接指向 v3 我自己引入的问题（P1 回归、P2-1 空测、P3-1 注释失实），已全部实证确认并写入下方，不做淡化处理。

---

## 一、v3 修复有效性验证（17 条逐条复核）

### 1.1 变异验证结果（在仓库外 TEMP 沙箱执行，真实仓库零改动）

对 6 条修复逐条做「撤销修复 → 跑测试 → 恢复」：

| 编号 | 修复 | 变异前 | 变异后 | 恢复后 | 结论 |
|---|---|---|---|---|---|
| V2-P0-1 | 控制台 sink `diagnose=False` | 4 passed | **1 failed**, 3 passed | 4 passed | 守卫有效 |
| V2-P1-1 | params 用查询串字段表脱敏 | 13 passed | **3 failed**, 10 passed | 13 passed | 有效 |
| V2-P1-2 | 路径段允许空格 | 32 passed | **5 failed**, 27 passed | 32 passed | 有效 |
| V2-P1-3 | `stop_worker` 条件清空 | 2 passed | **1 failed**, 1 passed | 2 passed | 有效 |
| V2-P2-4 | IntegrityError 只对唯一约束收窄 | 3 passed | **1 failed**, 2 passed | 3 passed | 有效 |
| V2-P2-5 | YAML `UnicodeError` 分支 | 9 passed | **3 failed**, 6 passed | 9 passed | 有效 |

**6/6 全部对变异敏感**，即这些测试能真正区分修复前/修复后，不是空转。

> 变异验证的执行方式：硬边界禁止修改仓库，故先把项目复制到 `%TEMP%\tm_audit_sandbox`（排除 .venv/.git/output），用原 .venv 的解释器在沙箱内跑。第一次批量脚本曾把「恢复后」检查写在恢复动作之前（脚本顺序 bug），已修正后重跑。

### 1.2 逐条结论

| 编号 | 修复内容 | 验证结果 | 实证依据 |
|---|---|---|---|
| V2-P0-1 | 控制台 sink 诊断守卫 | ⚠️ **部分生效** | 源码级守卫确能挡住 `diagnose=True`（变异验证红）；但配套的 `test_log_format_does_not_render_exception` 是空测，且其前提与事实相反（见 V3-P2-1 / V3-P3-1） |
| V2-P1-1 | params 按查询串字段表脱敏 | ✅ 真正生效 | 变异 3 红；实测 body `key` 未误伤、`body ⊆ query` 不变量成立 |
| V2-P1-2 | Windows 含空格路径脱敏 | ✅ 真正生效（但引入了新回归，见 V3-P1-1） | 变异 5 红；单空格与 `Program Files` 均正确 |
| V2-P1-3 | stop_worker 条件清空 | ✅ 真正生效 | 变异 1 红；`thread` 在两条早退路径前均已绑定，无 UnboundLocalError |
| V2-P2-1 | MySQL userinfo 改 `quote` | ✅ 真正生效 | SQLAlchemy 往返实测：`'test user'`→`%20`→解析回 `'test user'` |
| V2-P2-2 | SSE 降级帧不输出 id 行 | ✅ 真正生效 | 序号语义逐行比对未变（batch_start=1、终态=N+2）；重连场景有端到端两条断言 |
| V2-P2-3 | 描述与标签拼接共存 | ✅ 真正生效 | 4 组组合实测全部正确；但逐行扫描引入幽灵标签风险（V3-P3-12） |
| V2-P2-4 | IntegrityError 收窄 | ✅ 真正生效（含脆弱性） | 变异 1 红；真实 SQLite 撞编号仍 409。但 `orig` 兜底分支会误判（V3-P3-2） |
| V2-P2-5 | 非 UTF-8 YAML 包装 | ✅ 真正生效 | 变异 3 红；`UnicodeError` 分支位置正确（在 YAMLError 之后、OSError 之前） |
| V2-P2-6 | SECRET_KEY 正向判定 | ⚠️ **部分生效**（作者已披露，确认属实） | 白名单判定优于原写法；但 `get_config` 缺省 `"dev"`，"生产漏配 TM_ENV"仍漏 |
| V2-P2-7 | 三条假验证补真断言 | ✅ 真正生效 | 三条均以「桩被调用过」或「读回为 None」作阳性对照，无残留零断言 |
| V2-P2-8 | 前端正则函数作用域锚定 | ✅ 真正生效 | 匹配范围从全文 30937 字符收窄到 loadCases 的 1696；docstring 与实现不符（V3-P3-3） |
| V2-P3-2 | read_until do-while | ✅ 真正生效 | 读一次→判 deadline→才 sleep，timeout=0 不多睡一轮 |
| V2-P3-6 | Unix 前缀补全 | ✅ 真正生效 | `/etc/a.yaml` → `a.yaml` 实测 |
| V2-P3-7 | IPv6 方括号 | ✅ 真正生效 | `'[::1]'` 原样、`'::1'`→`'[::1]'`，`make_url` 解析正确 |
| V2-P3-8 | 缓存失效统一兜底 | ✅ 真正生效 | 全仓 grep 确认 4 处调用点无残留裸调；剩余 2 处 `invalidate_reports()` 本就有 try |
| V2-P3-10 | file_name 脱敏 | ✅ 真正生效 | 统一走 `_sanitize_log_field`；但新行为零测试（V3-P3-4） |

**汇总：15 条真正生效，2 条部分生效（V2-P0-1、V2-P2-6），0 条假修复。**

---

## 二、新发现的 bug

### P1

#### V3-P1-1 name 类字符类漏排除空格 → 同消息第二条 Windows 路径整体泄露（相对 v2 是能力回退）

- **位置**：`src/web/routes/cases.py:515`（`_PATH_NAME_CHAR`）、`:516`（`_PATH_NAME_CHAR_POSIX`），用于 `:519-523` 的两个正则
- **问题**：`_PATH_CHAR`（`:513`）的普通分支写作 `[^{_PATH_STOP} ]`——**正确排除了空格**；但两个 name 类的普通分支写成 `[^{_PATH_STOP}\\]` 与 `[^{_PATH_STOP}/]`，**漏了空格**。而 `:510-512` 的注释恰好写明「空格必须从普通分支里排除——否则第一个分支就会直接吃掉空格，前瞻分支永远轮不到」——**这条要求只做在了 `_PATH_CHAR` 上，没同步到 name 类**。
- **实证证据**（v2/v3 脱敏行为逐字对比，正则原文取自 `git show 080f967`）：

  ```
  IN  : see C:\x\a.yaml in D:\y\b.ini
  v2  : see a.yaml in b.ini          ← 两条都脱敏
  v3  : see a.yaml in D:\y\b.ini     ← 第二条原样泄露
  泄露判定: v3 输出含 'D:' 盘符前缀 = True；v2 = False
  ```

  机制：basename 组（name 类）允许空格，于是贪婪吞下 `a.yaml in D:`（含空格与下一个盘符前缀）；`re.sub` 非重叠扫描从匹配末尾（`D:` 之后）继续，剩余 `\y\b.ini` 不再以 `[A-Za-z]:\` 开头 → 零匹配 → 原样留在输出里。
  POSIX 变体因普通分支排除了 `/` 而侥幸无此问题——**所以现有两条「多处路径」用例全用 POSIX 路径，恰好绕开了这个洞**。
- **后果**：服务端绝对路径明文进 400 响应体（`cases.py:656` 唯一调用点）→ 前端 toast 展示给上传者。属**能力回退**：v2 挡得住的场景 v3 挡不住。
- **测试覆盖缺口**：`tests/test_cases_import_sanitize.py:128`、`:251` 两条「多处路径」用例均为 POSIX，无一条 Windows 双路径。
- **修复建议**：两个 name 类普通分支加空格 —— `[^{_PATH_STOP}\ ]` / `[^{_PATH_STOP}/ ]`；并补一条 Windows 双路径回归用例。**修复前请确认**：`a.yaml in D:` 这类散文吞并是否会在放宽后产生新的跨路径误吞（子 agent实测该改法使泄露用例变为 `see a.yaml in b.ini`，其余 6 条既有用例输出逐字不变）。

### P2

#### V3-P2-1 `test_log_format_does_not_render_exception` 是恒真空测，且其前提与事实相反

- **位置**：`tests/test_security_hardening_demo.py`（v3 新增）
- **问题（两层）**：
  1. **空测**：`handler._precolorized_formats` 在 loguru 0.7.2 中是 **dict**，且仅在有 ANSI 输出时才被 `update_format` 填充。跑完 `LogManager.setup(console_output=False)` 后两个 handler 均为 `{}`（已实证：`type=dict len=0`）。测试 `"".join(getattr(handler, "_precolorized_formats", []))` 恒得空串，`"{exception}" not in ""` **恒真**。对照实验：把 `{exception}` 加进格式串，该断言**仍然 True**。
  2. **前提事实错误**：实测 `handler._decolorized_format` 为 `'{time:...} | {message}\n{exception}'` —— **格式串确实含 `{exception}`，traceback 一直在渲染**。该测试 docstring 声称「直接加回 {exception} 时立刻变红」做不到。
- **后果**：`{exception}` 这一半屏障**实际无人看守**。与 P0-1 同批引入，属「消灭空测」目标自身引入的空测。
- **修复建议**：改为对 `LogManager.setup` 源码断言 `"{exception}" not in log_format` 段（与 diagnose 守卫同法）；或断言 `handler._decolorized_format`。

#### V3-P2-2 `test_safe_invalidate_swallows_and_logs` 零断言

- **位置**：`tests/test_v3_robustness_demo.py:683-693`（文件末尾，注释自陈「不抛异常即为通过」）
- **问题**：全函数无 `assert`、无 `pytest.raises`（AST 扫描确认是该文件 20 条中唯一零断言者）。函数名承诺 `and_logs`，但对 `logger.warning` 零断言。
- **后果**：`_safe_invalidate` 唯一可观测产物就是那条含 `operation` 的 warning（运维判断「是创建还是更新出的缓存故障」的唯一线索），删掉它测试仍全绿。属 `4ed4f6d`「三条假验证补真断言」的漏网之鱼。
- **修复建议**：用 loguru 临时 sink 断言 warning 文本同时含 `"测试操作"` 与 `RuntimeError`。

#### V3-P2-3 多空格目录名泄露操作系统账户名（v3 注释低估了后果）

- **位置**：`src/web/routes/cases.py:503`（`_PATH_CHAR` 的空格前瞻）
- **问题**：前瞻只支持**单个**空格。目录名含两个连续空格时前瞻失败，匹配在此终止，`\1` 只替换掉 `C:\Users\`，**账户名与剩余全路径原样回显**。
- **实证证据**（直接 import 真实函数）：

  ```
  IN : open C:\Users\John  Smith\secret.yaml failed
  OUT: open John  Smith\secret.yaml failed          ← 账户名 + 文件名全泄露
  IN : cannot open /home/bob/My  Docs/cases.yaml
  OUT: cannot open My  Docs/cases.yaml              ← 同上
  对照（单空格，修复有效）:
  IN : in C:\Users\John Smith\AppData\Local\Temp\tm_x\a.yaml, line 3
  OUT: in a.yaml, line 3
  ```

- **后果**：该函数唯一调用点把 `CaseDataLoadError` 转 400 并经 toast 展示。触发条件取决于**服务端**路径（Windows 账户目录名、Linux 含多空格家目录均合法），攻击者无法主动构造，故非可利用注入；但部署路径一旦命中即稳定泄露服务端目录结构与账户名——正是该函数 docstring 声明要防的威胁。
- **与 v3 注释的冲突**：`cases.py:501-505` 把该限制记为「路径在此终止」，与实际后果（账户名泄露）不符，**记录偏轻**。
- **修复建议**：前瞻从「恰好一个空格」放宽为「一个或多个空格」` {1,}(?=...)`，同时保留散文断开能力。若担心多空格与散文不可区分，**宁可过度脱敏**（多脱敏只损诊断值，少脱敏造成泄露）。

#### V3-P2-4 四条 params 脱敏测试缺阳性对照

- **位置**：`tests/test_http_client_demo.py:124`、`:160`、`:181`、`:211`
- **问题**：断言只有 `PLACEHOLDER not in joined`。`http_client.py` 的请求前置日志是唯一日志来源，若它被删除或被 `logger.disable`/级别过滤，`capture.messages` 为空 → 4 条全绿。`:141 test_params_business_fields_preserved` 已有阳性对照，写法可直接复制。
- **修复建议**：每条补 `assert "HTTP请求 >>>" in joined`。
- **备注**：另 `:181` 只覆盖 list 逐项递归；`_mask_data` 对 **dict 内部嵌套 dict 不递归**，`params={"data": {"key": ...}}` 仍明文——此为**静态推断，未运行验证**。

### P3

#### V3-P3-1 logger.py 关于 `diagnose` 的注释与实测不符（v1 与 v3 均不准确）

> **⚠️ 本条结论已被后续定向复现部分推翻，见下方「结论更正」。原始探针不完整。**

- **位置**：`src/common/logger.py`（v3 更正后的注释）
- **问题**：v3 把注释从「diagnose=True 会追加 `行号: 变量名 = 变量值`」改为「只渲染源码行与表达式求值」。**两者都不准确**。真实文件下实测 `diagnose=True` 相对 `False` 的**全部差异**是：

  ```
  +     -> <function _boom at 0x000001BE67A2E7A0>
  ```

  仅此一行。即：
  - ❌ 不输出局部变量值（`db_password` 未出现）
  - ❌ 不输出局部变量的容器内容（dict 里的 DSN 密码未出现）
  - ❌ 不输出字符串拼接结果（`endpoint` 未出现）
  - ❌ **连源码行都不显示**（`connect_db()` 在两种设置下均未出现）
  - ✅ 也不泄露**函数以外**的任何东西；只多出函数对象的 repr
- **准确的表述应为**：格式串**始终**渲染 `{exception}`（traceback + 文件路径 + 行号在两种设置下都进日志，这是既有暴露面、与 diagnose 无关）；`diagnose=False` 额外挡住的只是「函数对象 repr」这一类信息，**不是变量值**。因此 v1 的「防变量值泄露」与 v3 的「防源码行泄露」两处注释都需要更正。
- **修复建议**：注释改为「diagnose 额外渲染 traceback 帧的函数对象 repr（`-> <function f at 0x...>`），不渲染局部变量值；此处关闭是为减少内部实现细节外泄，与密码/token 无关」。

##### 结论更正（2026-10-02 大扫除 v5 定向复现，本条原结论过强）

原探针把「变量赋值」与「`raise` 语句」**分行**放置，且 `raise` 行内无任何变量引用，因此漏掉了关键场景。定向复现四个场景后：

| 场景 | diagnose=False | diagnose=True | 判定 |
|---|---|---|---|
| A 变量与 `raise` 分行 | 不含密钥 | 不含密钥 | 无泄露 |
| **C 变量作为异常行函数实参**（`connect(db_password)`） | 不含密钥 | **含密钥**，新增 `\| -> 'Sup3rSecretDbPass'` | **真泄露** |
| B/D 变量在异常消息 f-string 里 | 含密钥 | 含密钥 | 泄露来自**应用自己写的异常消息**，与 diagnose 无关 |

**修正后的准确结论**：
1. loguru 0.7.2 的 `diagnose=True` **确实会输出局部变量的值**——条件是变量作为**表达式实参出现在异常发生的那一行源码上**，loguru 会对该行求值并渲染 `| -> value`。
2. 渲染格式是 `|   -> '值'`（对行内表达式求值），**不是** v1 注释所写的 `行号: 变量名 = 变量值` 形式——v1 对**机制**的描述不准。
3. 但 v1 的**修复方向是对的**：`diagnose=False` 是当前唯一挡住这类变量值打印的屏障。我原报告称「diagnose 只是多出函数 repr、与密码无关」属于**过强结论**，据此把 v1 的修复说成「理由错误」也不准确——理由的**措辞**不准，**意图与效果**都成立。
4. 直接影响：`tests/test_security_hardening_demo.py::test_local_variables_not_leaked_into_console` 之所以对 `diagnose=True` 变异不敏感，正是因为它用的是场景 A 的形态。改写该测试必须采用场景 C 的形态（敏感变量作为异常行上的函数实参），否则重写后仍会是空转。

#### V3-P3-2 `_is_unique_case_id_violation` 的 `orig` 兜底分支必误判

- **位置**：`src/core/case_manager.py:222`
- **问题**：`str(exc.orig) if getattr(exc, "orig", None) else str(exc)`。`orig` 缺失时走 `str(exc)`，而 SQLAlchemy 的 `str(exc)` 形如 `(builtins.X) msg\n[SQL: INSERT INTO test_cases (case_id, ...)]` —— **必然含 `case_id`** → 任何 `IntegrityError` 都被判成「编号冲突」→ 409，真因再次被掩盖，正是本次要修的问题。
- **实证**：构造 `IntegrityError("INSERT INTO test_cases (case_id,...)", {...}, None)` → 判定 `True`。
- **可达性**：SQLAlchemy 包装 DBAPI 错误时必设 `orig`，生产不可达，属**防御性缺口**。
- **另两处脆性**（同一函数）：①将来改成 `UniqueConstraint("case_id", name="uq_case_biz_no")`，PG 消息变成 `... unique constraint "uq_case_biz_no"` 不含 `case_id` → 真冲突被误报 500；②新增 `CheckConstraint(..., name="chk_case_id_format")` 时，SQLite 消息 `CHECK constraint failed: chk_case_id_format` 命中 `case_id` → 又被误翻译。当前 `models.py` 全文无 `CheckConstraint`（仅 `Index`），故 ②当前不可达。
- **修复建议**：删掉 else 兜底（缺失时返回 `False`）；判据改为约束名白名单集合或方言错误码。

#### V3-P3-3 `_extract_function_body` docstring 承诺「跳过注释」但实现没有

- **位置**：`tests/test_frontend_race_demo.py:41-45`（docstring）vs `:57-90`（实现）
- **问题**：实现只跟踪 `'`/`"`/`` ` `` 三种引号态，**无注释分支**。`cases.js` 中任何 `//` 注释里的撇号（如 `// don't reset`）会误开字符串态，把后续配对全打乱 → 抛「花括号不配平」或截错函数体。
- **当前安全**：实测 `loadCases` 函数体 1696 字符、1 catch/1 finally、0 行注释花括号/0 块注释/0 正则字面量/0 模板串，函数声明能被匹配上。属**潜在脆弱 + 注释失真**，不是当前假通过。
- **修复建议**：加 `//` 到行尾、`/* */` 跳过；或把 docstring 改成只承诺「跳过字符串」。

#### V3-P3-4 `file_name` 改了响应契约但零测试

- **位置**：`src/web/routes/cases.py:680`
- **问题**：改走 `_sanitize_log_field` 后，控制字符折叠与 >200 截断是**新的对外行为**，但现有 5 处 `file_name` 断言（`test_web_cases_import_demo.py:242,262`、`test_security_hardening_demo.py:781,808`、`test_cases_import_sanitize.py:331`）全是不含控制字符、<200 字符的名字，全部照旧通过——新行为无任何断言护栏。
- **修复建议**：补一条含 `\n\t` 的文件名用例与一条 >200 字符的用例。

#### V3-P3-5 三个新增函数缺「异常」段，违反项目 docstring 铁律

- **位置**：`case_manager.py:222`（`_is_unique_case_id_violation`）、`:246`（`_safe_invalidate`）、`db_session.py:49`（`_bracket_ipv6_host`）
- **问题**：`PROJECT_CONTEXT.md` 第 8 节铁律要求「所有函数/类必须有完整 docstring，参数、返回值、异常都写清楚」。三者均只有 参数/返回；同批改动的 `http_client.py:417`（`_mask_data`）反而保留了 异常 段，口径不一致。
- **修复建议**：三处补 异常 段。

#### V3-P3-6 TESTING 模式下 500 响应体回显完整 SQL 与列值

- **位置**：`src/core/case_manager.py:851-856`（v3 新增的「非唯一约束」分支）
- **问题**：`raise CaseManagerError(f"用例创建完整性违例（检查字段是否为空/超长）: {exc}")` 把 `IntegrityError` 原文拼进 message → 路由只翻译 `CaseConflictError` → 全局 `Exception` 处理器在 `TESTING` 下 `return error(str(exc), 500)`，而 `IntegrityError` 原文含完整 INSERT 语句与全部绑定列值。
- **性质澄清**：生产（`TESTING=False`）返回「服务器内部错误」，无泄露。v3 前 `IntegrityError` 一律转 `CaseConflictError`（不含 `{exc}`），同函数的 `except SQLAlchemyError` 本就如此——**v3 未创造新泄露类别，只增加一处实例**。
- **修复建议**：该分支只把 `{exc}` 记进日志，message 用固定文案。

#### V3-P3-7 `_generated_execution_ids` 无锁 check-then-act

- **位置**：`src/core/case_manager.py:151`（模块级 `set`）、`:323-324`
- **问题**：`if execution_id not in ...` 与 `.add()` 之间无锁。GIL 保证单条操作原子，不保证这对操作原子；两线程可同时通过检查并返回**同一批次号** → `get_channel` 注册表键碰撞（两批次事件互串）与批次行歧义。
- **缓解事实**：需同时命中「同秒 + uuid4 前 4 位 hex 撞车」，概率约 1/65536；且 uuid4 才是主防线，集合只是去重兜底。
- **性质**：v1/v2 遗留，v3 未触碰。
- **修复建议**：加一把模块级 `threading.Lock`（生成路径非热点，开销可忽略）。

#### V3-P3-8 LIKE 通配符未转义

- **位置**：`src/core/case_manager.py:657`：`pattern = f"%{str(keyword).strip().lower()}%"`
- **问题**：未转义 `%` / `_`，用户传 `keyword=%` 即匹配全表。
- **性质澄清**：参数化查询，**无 SQL 注入**；影响限于只读列表接口返回行数偏多（已分页，≤100）。v1/v2 遗留。

#### V3-P3-9 全仓唯一一处无 `try/finally` 的全局状态改写

- **位置**：`tests/test_p2_b3_hardening_demo.py:292-327`
- **问题**：`:315-316` 直接赋值 `tq._worker_thread` / `tq._stop_event`，`:326-327` 的复原写在函数末尾。若 `:320` 或 `:321` 断言失败 → 脏全局泄漏给后续所有用例；且复原是「置 None」而非「还原原值」，会抹掉真实 worker 引用。
- **对照**：同文件 `:355-363` 的同类测试写法正确（try/finally），两处不一致。
- **修复建议**：改用 `monkeypatch.setattr(tq, "_worker_thread", stuck, raising=False)`。

#### V3-P3-10 `test_http_client_demo.py` parametrize 缺 `access_key`

- **位置**：`tests/test_http_client_demo.py:121`
- **问题**：v3 往 `SENSITIVE_QUERY_FIELDS` 新增的正是 `access_key`，但参数化列表是 `["key","token","access_token","api_key","apikey","password"]`，无直接行为断言；目前仅由 `:257` 的结构不变量间接锁住表成员关系。
- **修复建议**：把 `"access_key"` 加进列表。

#### V3-P3-11 `read_until` 零超时「立即」未测时间

- **位置**：`tests/test_v3_robustness_demo.py:568-588`
- **问题**：docstring 承诺「立即抛超时（不空等）」，实际只断言抛 `SerialClientError`。若把 `time.sleep(0.05)` 挪到 deadline 判定**之前**（本次 do-while 重构最易踩的坑），测试照样绿。
- **修复建议**：断言 `time.sleep` 调用数为 0，或断言耗时 `< 0.04s`。

#### V3-P3-12 `_parse_tags_from_description` 逐行扫描 → 描述正文含「标签:」行产生幽灵标签

- **位置**：`src/core/case_manager.py:2402-2413`
- **问题**：由「整串以标签开头」改为「逐行查找标签行」后，描述正文里恰好有一行以 `标签:` 开头的文本会被误当标签。实证：`{"description": "步骤1\n标签: 伪造标签\n步骤2", "tags": []}` → 解析出 `['伪造标签']`；v2 对同样输入返回 `[]`。
- **性质**：需描述含行首「标签:」，影响小，可不阻塞。
- **修复建议**：拼接时给标签行加一个不易被正文复现的标记前缀，解析时按该标记定位。

#### V3-P3-13 注释编号笔误

- **位置**：`src/core/case_manager.py:832-834`，注释写作「v3 修复 V2-P2-2/P2-4」，而 P2-2 实为 SSE 降级帧。
- **性质**：纯注释笔误，不影响行为。

#### V3-P3-14 `executors.py` 遗留 TODO

- **位置**：`src/core/executors.py:167` — `TODO: 真实测试集执行时按用例数据解析可执行测试文件路径（当前用例暂无 script_path 字段，先用 case_id 占位）`
- **问题**：`path` 退化为 `case_id`，`py -m pytest <case_id>` 必然找不到文件。仅 `pytest` 执行器受影响（非默认路径）。
- **性质**：v1 立项时即知的占位，非回归。

---

## 三、v2 暂缓的 5 条 P3 重新评估

| 编号 | 内容 | 当前真实状态 | 建议 | 理由 | 工作量 |
|---|---|---|---|---|---|
| **V2-P3-1** | `handle_500` 近乎死代码 | 仍在 `exceptions.py:155-167`，**v3 未动** | **降级为 P4，只加注释不删代码** | 删除牵动 `test_security_hardening_demo.py` 与 `test_stage_c_support_coverage_demo.py` 两条测试；且它是**唯一能让 500 响应体在测试环境显示真因**的出口，对排障有实际价值，代价只是一处「看似不可达」的分支 | 5 分钟（加 2 行 docstring 说明「仅响应收尾阶段可达」） |
| **V2-P3-3** | `health_check` 死代码 | 仍在 `db_session.py:308`，全仓零 src 调用点；`db_session.py:9` 注释漂移**仍在** | **修注释，不删代码**（把该条拆两半） | 删代码必然删 3 条契约测试，与「测试只增不减」直接冲突；而那 3 条锁的是「引擎构建失败必须降级 False」这一通用契约，有独立价值。真正该修的只是 `:9` 的错误描述 | 10 分钟（改 1 行模块 docstring + 方法 docstring 标注「当前无生产调用方，保留供自检/未来编排」） |
| **V2-P3-4** | `escapeHtml` 两份定义 | 两份都在（`main.js:47` + `:159`；`cases.js:108` + 导出）。加载序已实证：`base.html` 先载 `main.js`→`api.js`，`cases.html` 后载 `cases.js` | **继续暂缓** | 两实现已实证等价，收敛无行为变化；但 `cases.js` 有 12 处调用点 + 1 处 window 导出，删副本要改 13 处；更关键是加载顺序是**隐式契约**——将来 `cases.js` 被提前加载或 `main.js` 改 `defer`，删副本会直接 `escapeHtml is not defined`。「改对了没收益、改错了炸页面」 | 收敛 30 分钟 + 加载序回归测试 20 分钟 |
| **V2-P3-5** | SSE 零帧无限重连 | **零帧场景仍然存在**：v3 只改了 DB 分支降级帧，live 分支（`executions.py:723-745`）未变——`event.event_id <= resume_live` 且含终态事件时 `saw_terminal=True` → 兜底不触发 → `return` → 响应体 0 帧。心跳只在订阅路径存在，0 帧路径不走 | **不改代码，改注释**（保持 P3） | 这是**产品级取舍**：0 帧后浏览器约 3s 自动重连形成热循环，但「客户端已见过终态」这个事实服务端无法从 `Last-Event-ID` 之外得知；补终止帧会与 v2 锁定的「不重复补发」契约冲突。正确解法是前端收到终态事件后主动 `es.close()`，属前端改动 | 10 分钟（补一条源码注释说明该热循环是已知取舍，避免下一轮重复提） |
| **V2-P3-9** | `.gitattributes` 重检出 | 工作区 `w/crlf` = **32 个文件**（v2 记的 24 已漂移），`w/mixed` 已归零（`.gitignore` 混行已修），索引侧 `i/lf` 全绿，`core.autocrlf=true` | **撤回 v2 的处方，降级为 P4** | 关键纠正：`git add --renormalize .` 在这里是**空操作**——clean filter 会把工作区 CRLF 归一为 LF，索引已是 LF，入库结果与现状逐字节相同，工作区文件仍是 CRLF。且它会把 `PROJECT_PLAN.md` 的未提交改动一并 stage。真要修必须 `git rm --cached -r . && git reset --hard`，属破坏性操作 | 降 P4 0 成本 + 在 `.gitattributes` 注明「新克隆自动生效，历史工作区需重检出」 |

---

## 四、经实证确认无问题的部分

| 项 | 判据 |
|---|---|
| **XSS 汇点** | 8 处 `innerHTML` 逐一核对：4 处纯静态字面量；1 处分页控件只拼数值与 `data-page`；`cases.js:382` 模块下拉的 `name`/`value` 属性**两侧都过 escapeHtml**；`renderTableRows` 的 case_id/name/module/case_type/creator 与 `data-case-id` 全部转义；`priorityBadge`/`statusBadge` 走固定枚举 class 映射 |
| **两份 escapeHtml 等价** | `main.js:47` 单次 `replace(/[&<>"']/g)` 与 `cases.js:108` 五次链式 `replace` 覆盖字符集完全相同。**且全仓无 `href=`/`src=` 由用户数据拼装的汇点**（唯一 `href="#"` 是分页占位），故不做 URL 编码不构成 `javascript:` 缺口 |
| **CSP 不与模板冲突** | `base.html:21` 的 `script-src 'self'` 可正常放行：逐页核对 `extra_js` 块全是 `url_for` 外链，**模板内无任何内联 `<script>`、无 tojson、无 `{{ }}` 插值进 JS**；`style-src 'unsafe-inline'` 对应 dashboard.html 行内 style |
| **SSE 帧无注入** | `json.dumps(ensure_ascii=False)` 转义换行，`error_message`/`case_name` 无法伪造 `data:` 行或断帧 |
| **SQL 注入** | 全仓仅 2 处 `text()`（`db_session.py` 与 `routes/base.py`），均为常量 `"SELECT 1"`；无 f-string/`%`/`.format()` 拼 SQL，无 `raw()` |
| **路径遍历** | 唯一 HTTP 可达文件入口 `import_cases`：`tempfile.mkdtemp` 隔离 + `secure_filename` + 后缀白名单 + `finally` 中 `os.remove` + `rmtree(ignore_errors=True)`，无 TOCTOU。中文名被压空已由 `safe_stem or f"import{suffix}"` 兜住。其余 `open()` 路径全部来自 env 或内部常量 |
| **凭据脱敏** | `SENSITIVE_BODY_FIELDS ⊆ SENSITIVE_QUERY_FIELDS` 不变量成立（差集只有裸 key，符合设计）；v3 已把 params 切到查询串表且 list 递归传了 `fields`。企微 4 处日志点全覆盖 `_mask_url`；`requests` 异常文本自带 URL 这条泄露面，3 处均已改为只打类型名。邮件 `self.password` 仅进 `client.login()`。`test_http_client_demo.py` 的占位符字面即自我否定（`PLACEHOLDER-NOT-A-REAL-KEY`），不会被误当真密钥 |
| **event_bus 竞态** | 类型校验在锁外但只读入参；closed 检查、event_id 分配、append、notify_all **全在同一把锁内**；`close()` 先 pop 注册表再 close，持旧引用的订阅者仍能 drain，无复活窗口；注册表全程 `_REGISTRY_LOCK` |
| **task_queue 竞态** | v3 条件清空正确消除了「抹掉新 worker 引用」的双 worker 竞态；join 超时仍保留引用；dequeue 对非 dict 载荷做结构校验，不会静默死亡 |
| **cache / notification 并发** | cache 无进程内读-改-写，`_backend` 重复构建最坏是丢弃一个未连接的客户端对象；notification 死信/历史各自独立 session，无模块级可变状态 |
| **异常处理** | 全仓**无裸 `except:`、无 `except Exception: pass`、无 `contextlib.suppress`、无 `except BaseException`**；`finally` 中清理均不抛；`data_driver.py:267` 新增的 `except UnicodeError` 位置正确 |
| **调试残留** | 自研 JS 零 `console.log`/`debugger`/`alert(`（命中全在第三方 bootstrap）；`print(` 命中全在 `case_manager.main()` 的 `if __name__ == "__main__"` 保护内；无 `breakpoint`/`pdb`/`FIXME`/`XXX`/`HACK` |
| **正则 ReDoS** | `_PATH_CHAR` 是「外层 `*` 套内层前瞻 `*`」的嵌套量词形态，理论上可灾难回溯。实测 n 从 200→3200 翻 16 倍，耗时 0.01ms→0.57ms，**线性**——两分支在空格字符上互斥（第一分支显式排除空格），无歧义回溯 |
| **正则捕获组** | 探针确认两个正则 `groups == 1`，`sub(r"\1")` 确为 basename，无组编号错位 |
| **`quote(x, safe="")` 边界** | sqlalchemy 2.0.25 实测往返：`'test user'`、`'test+user'`、空用户名/空密码、`'u@host/x'`、`'p?q#f'` 全部一致 |
| **`_bracket_ipv6_host` 边界** | `'[::1]'` 原样、`''` 原样、`'::1'`→`'[::1]'`；`make_url` 解析 `host='::1' port=3306` 正确；含幂等守卫测试 |
| **v3 工程卫生** | 新增行中 `print(`/`TODO`/`FIXME`/`breakpoint` 零命中；无 >100 码点超宽行 |

---

## 五、建议修复顺序

| 优先级 | 条目 | 理由 |
|---|---|---|
| **第一批** | V3-P1-1（name 类漏排除空格） | 唯一的能力回退，且是安全类；改动 2 字符 + 补 1 条 Windows 双路径用例 |
| **第一批** | V3-P2-1（空测 + 前提错误） | 本轮「消灭空测」目标自身引入的空测；且它看守的 `{exception}` 屏障当前无人看守 |
| **第二批** | V3-P2-2、V3-P2-4 | 假验证与弱断言，改动小、收益直接 |
| **第二批** | V3-P2-3（多空格泄露） | 安全类但需在「过度脱敏」与「散文断开」间权衡，建议同时补 Windows 双路径用例再定 |
| **第三批** | V3-P3-1（logger 注释）、V3-P3-2（`orig` 兜底）、V3-P3-3（docstring 失实）、V3-P3-5（docstring 铁律）、V3-P3-13（注释笔误） | 纯注释/文档/防御性缺口，零行为风险，可合并为一个「文档与注释对齐」commit |
| **第三批** | V3-P3-4、V3-P3-10、V3-P3-11、V3-P3-9 | 补测试断言 / 补 monkeypatch 隔离 |
| **第四批** | V3-P3-6、P3-7、P3-8、P3-12、P3-14 | 均为前轮遗留或影响面小，择机处理 |

---

## 六、审查过程中的不确定项与声明

1. **未运行 pytest**（按硬边界，且会与主会话争用 `output/allure_results`）。所有「测试是否敏感」的结论来自**沙箱内的变异验证**（真实仓库零改动），不是运行真实仓库得出的。
2. **V3-P2-4 的嵌套 dict 盲区**为静态推断，未运行验证。
3. **V3-P3-12 的幽灵标签**为实证（已构造输入验证解析结果），但「实际是否有用户会在描述里写行首『标签:』」属使用面推断。
4. **V3-P3-2 的 `orig` 兜底**已构造实例证明会误判，但 SQLAlchemy 在本项目三条路径下是否真会产出 `orig=None` 的 IntegrityError 未实测——标注为**防御性缺口**而非生产缺陷。
5. **V2-P3-9 的 `w/crlf` 32 个文件**为本次快照，工作区变动会使其漂移。
6. 本轮未对「第三方库内部」与 `testdata/`（只读）做审查。

---

*本报告为 MiniMax 独立审查产出，未参考任何其他 AI 的审查报告。*
