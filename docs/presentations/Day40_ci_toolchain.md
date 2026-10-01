# Day40 讲述材料：CI 与工具链（五层门禁 + fail-closed 思想）

> 用途：Day40 阶段复盘讲述大纲。配套事实来源：[.github/workflows/ci.yml](../../.github/workflows/ci.yml)

## 0. 一句话定位

Day38 建流水线、Day39 清存量、Day40 拿到首次全绿并做了三轮 fail-closed 加固：
核心思想是**门禁可以分层宽严，但任何工具链异常都不能被读作"0 问题"的假绿**。

---

## 1. 流水线全貌（一张文字图）

```
push/PR to main
 └─ job: test（ubuntu-24.04 钉死镜像，timeout 15min）
     matrix: 3.11 主版本（任何一步失败即红）
             3.12 探测版本（job 级 continue-on-error，步骤级保留真实红叉）
     steps:
       1. checkout@v5（fetch-depth=0，mypy 增量步骤要完整 git 历史做 diff）
       2. setup-python@v6（pip 缓存，cache-dependency-path=requirements.txt）
       3. 装依赖 + ruff/mypy/pip-audit（工具链不进 requirements.txt）
       4. ruff 全量检查（src + tests，规则不 ignore）
       5. mypy 增量（只硬阻断本次 diff 新增行上的错误）
       6. mypy 全量基线（存量豁免，::warning:: 追踪趋势只减不增）
       7. pytest 全量 + 覆盖率门禁 --cov-fail-under=85
       8. pip-audit 完整报告（展示用，固定不阻断）
       9. pip-audit 分级门禁（未豁免 HIGH/CRITICAL/UNKNOWN 阻断）
      10. 探测版本结果汇总（仅 3.12，always() 收敛四步骤 outcome 为 warning）
```

## 2. 五层门禁的宽严分层

| 层 | 命令/口径 | 阻断策略 |
| --- | --- | --- |
| 风格 | `ruff check src tests`，select E4/E7/E9/F/UP/B/I，ignore 为空 | 全量硬阻断，0 errors 才过 |
| 类型（增量） | mypy strict，只对 CHANGED_SRC 跑，错误行命中 diff 新增行集合才计数 | 新增行 1 个错误即红；存量行豁免 |
| 类型（基线） | `mypy src` 全量 | 不阻断，warning 输出存量总数（196 个），只减不增 |
| 测试 | `pytest -q --cov=src --cov-fail-under=85` | 失败即红；exit 5（零收集）同样红，不设容忍 |
| 安全 | pip-audit + OSV 定级 + allowlist | 未豁免高危/无法定级阻断；中低危仅告警 |

分层逻辑：**新增代码享受零容忍，存量债务清单化、趋势化**，不被一次性卡死，
也不允许借"存量很多"让新增烂代码混进来。

## 3. mypy 行级阻断的实现（本阶段技术含量最高的一段 bash）

1. PR 用 `origin/main` 做基线，push 用 `github.event.before`；
2. **BASE 可达性 fail-closed 前置**：`git cat-file -e BASE^{commit}` 不通过直接红——
   否则 BASE 不可达时 CHANGED_SRC 为空会误走"无 src 变更跳过"分支（假绿）；
3. `git diff --unified=0` + awk 解析 hunk 头 `@@ -a,b +c,d @@`，
   产出本次全部新增行的 `path:line` 集合；
4. mypy 输出路径分隔符 `tr '\\' '/'` 归一化，正则兼容带/不带列号；
5. 错误行命中新增行集合才计入 new_err_count，>0 即红；
6. **崩溃守卫**：mypy 退出码非 0 但错误行数为 0 → 工具链崩溃/格式变异，判红。

## 4. pip-audit 分级门禁

- pip-audit 自身 JSON 的 vulns 不含 severity：向 OSV API 查
  `database_specific.severity`，本 ID 查不到再按 GHSA 别名查，均失败归 UNKNOWN。
- `UNKNOWN` 与 HIGH/CRITICAL 同级阻断（无法定级 = fail-closed）。
- PYSEC/GHSA 互为镜像：`{id}∪aliases` 去重 + 定级缓存，避免重复计数。
- allowlist 条目必须带**未过期的过期日**：缺日、过期均按未豁免处理。
- 网络/5xx 重试 1 次，4xx 视为确定性结果不重试。
- 只审计 `-r requirements.txt` 依赖闭包，不把 ruff/mypy 自身算进来。

## 5. 连续全绿历程（数字锚点）

- Day38：工作流落地（ruff/mypy/pytest/pip-audit 四件套 + 覆盖率门禁 80）。
- Day39：ruff --fix 机械修复 348 → 12 个手动项；serial/telnet 补测
  41 条，覆盖率 82% → 87%（后续 88%）；全量 463 → 484 条。
- Day40：ruff 12 → 0；覆盖率门禁 80 收紧到 85；pytest 484 → 487；
  CI 首次全绿。
- Day40-fix/2/3：`from None` 异常链断言修复、actions 升 v5/v6、
  钉 ubuntu-24.04（避开 2026-10-19 runner 滚动迁 26.04）、
  mypy fail-closed 守卫前置到 CHANGED_SRC 计算之前。

## 6. 关键设计决策

1. **job 级而非 step 级 continue-on-error**：3.12 失败不拖红整条流水线，
   但失败步骤保留红叉与 annotation，末尾 always() 汇总，信号不丢失。
2. **零收集 exit 5 不设容忍**：testpaths 固定 tests/，0 收集只可能是配置损坏，
   容忍就是给静默通过留门。
3. **工具链只在 CI 安装**：requirements.txt 是运行期依赖唯一事实来源，
   不被开发工具污染。
4. **mypy 双口径**：增量硬门禁保新增质量，全量基线保债务可观测。
5. **钉镜像版本**：全绿初期避开 runner 大版本迁移这一不可控外部变量。

## 7. 踩坑与教训

- **setuptools 过老导致 pip-audit 失败**：CI 环境先
  `python -m pip install --upgrade pip`，本地正常不代表 runner 干净。
- **fail-closed 守卫的顺序陷阱**：守卫必须放在"空变更跳过"分支之前，
  否则异常路径恰好落进跳过分支 exit 0——加固时要逐行走查异常会落到哪。
- **退出码非 0 ≠ 有错误**：mypy 崩溃也返回非 0，必须同时看错误行数，
  两个信号组合判定。
- **PR 事件 before 可能为空**：用 origin/main 兜底基线，否则增量计算直接失效。
- **GitHub 默认跳过失败后步骤**：汇总步骤必须 always()，否则永远 skipped。

## 8. Q&A 预设

- **Q：196 个存量 mypy 错误为什么不一次性修完？**
  A：独立专项处理，避免在业务提交里夹带大规模注解变更；基线 warning 保证
  数字只减不增，新增行零容忍保证不恶化。
- **Q：3.12 失败流水线却绿，算不算放水？**
  A：不算。job 级不阻断是版本兼容窗口策略，步骤红叉、annotation、聚合 warning
  三处信号都在；3.11 主版本任何一步失败照常红。
- **Q：allowlist 为什么强制过期日？**
  A：没有过期日的豁免等于永久豁免；强制日期让每笔技术债都有到期复议点。
- **Q：覆盖率门禁为什么从 80 提到 85？**
  A：Day39 补测后实际已到 88%，门禁设在略低于实测值的位置，既防回落又不误伤。
- **Q：为什么不用复用 action 而自己写 bash 门禁？**
  A：行级命中、fail-closed、OSV 定级都是项目特有口径，自有脚本可测可读可审计。

## 9. 收尾数字锚点

- 5 层门禁；3.11 主版本 + 3.12 探测位；15 分钟整体超时。
- 487 条测试全绿；覆盖率门禁 85%（实测 88%）；ruff 0；mypy 新增行 0、存量 196 跟踪中。
- 提交规范 Conventional Commits；README 徽章与 CI 数字每次变动同步。
