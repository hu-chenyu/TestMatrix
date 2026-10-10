# Day51 · 批次口径盘点（登记① Day1）

> 范围：**只产出分歧清单，不写改造代码**。定口径与全链路改造归 Day56。
> 依据：`PROJECT_CONTEXT.md` §5 Day50 登记①、§6.3 统计口径、§7 跨天踩坑记录。
> 方法：以**函数名**定位，不使用登记里的行号（见 §四 第 3 条）。

---

## 一、结论摘要

批次口径的根因是**一条**（伪批次），其余三条是它的派生或已对齐。

- **根因**：`pytest-{case_id}` 这个确定性回落值，把「单条用例的一次执行」写成了「一个批次」，污染 `defect_statistics` 的批次数、通过率趋势、模块分布三个口径。
- **派生 1**：同一用例重复执行 → 命中唯一约束 → 桥接失败 → 但失败对用户不可见（P3②）。
- **派生 2**：看板批次列表的数据源是 `defect_statistics`，而明细在 `test_executions`，fallback 批次只有汇总没有明细 → 出现「无明细批次」。
- **已对齐**：`failed = failed + broken` 的口径在 `save_statistics` 里已正确实现，**盘点结论是「不要动」**。

---

## 二、根因：伪批次是怎么产生的

### 2.1 链路

```
CaseManager._execute_batch_async
  → executors.run_one(case)                      # case 字典来自 _to_dict
      → resolve_bridge_execution_id(case)         # executors.py L497-530
          ① case["execution_id"] 非空 → 直接复用
          ② 否则 → f"pytest-{case_id}"            # ← 伪批次在此产生
      → bridge_results_dir(allure_dir, 该批次号)  # report_analyzer.py L1760
          → ReportStatistics.aggregate(results)
          → ReportRepository.save_statistics(stat, execution_id, remark)
              # defect_statistics.execution_id 有 unique 约束，刻意不静默更新
```

### 2.2 三条代码注释已经把问题写明了（Day50 作者已知并显式声明）

`executors.py` L505-514 的注释原文：

> 「为什么回落值是确定性的而不是"每次生成新批次号"：`defect_statistics.execution_id` 有唯一约束，且 `save_statistics` 刻意不做静默更新……若此处每次生成全新批次号，那么**每条用例每跑一次**就会往 defect_statistics 插一行——而这张表是 Web 报告统计的数据源，插进来的单条用例统计会把"批次数""通过率趋势""模块分布"的全部口径撑坏（一���用例被当成一个批次）。……**真正的批次号关联留给执行编排层（Day51 执行状态回写时接入）。**」

**即：这不是未知缺陷，是 Day50 主动留下的显式债务，且 Day51 已被指定为接管日。**

---

## 三、已核对为「正确、不要动」的口径

| 口径 | 实现位置 | 事实 |
|---|---|---|
| DB 层 `error` ↔ 统计层 `broken` | `report_analyzer.py` L986 `error=stat.broken` | ✅ 与 §6.3 一致 |
| 纯 failed 剔除 broken | L985 `failed=stat.failed - stat.broken` | ✅ 与 §6.3 一致 |
| 失败合计 = failed + broken | 读侧 `get_failed_results`（"failed+broken均视为失败"，L12） | ✅ 读写两侧口径闭合 |

**盘点结论：§6.3 的统计口径已实现完整，D56 改造时不要动这三个字段的映射关系。**

---

## 四、分歧清单

| # | 分歧 | 代码事实 | 影响面 | 处置 |
|---|---|---|---|---|
| **1** | **伪批次**：单条用例被写成一个批次 | `resolve_bridge_execution_id` 无 `execution_id` 时回落 `pytest-{case_id}`，该值是 `defect_statistics` 的唯一键 | 批次数 / 通过率趋势 / 模块分布 三个口径 | **D56 定口径** |
| **2** | **重复执行 = 桥接失败** | unique 约束 + 刻意不静默更新 → 第二次跑同一用例必抛 IntegrityError → 被 `bridge_results_dir` 捕获为 `BridgeResult.error` | `bridge_error` 被置位但**落库只取 result/duration/error_message，用户看不到** | **D52**（P3②） |
| **3** | ⚠️ **登记里的消费点行号已漂移** | §5 登记 `case_manager L1692/1755/L1822`；实测：`list_executions_paged` **L1687**、`get_execution_detail` **L1817**、**`get_execution_status` L2429（L1755 偏差 674 行）** | 若照行号改会改错位置 | **D51 即刻生效**：一律以函数名定位 |
| **4** | 批次级与用例级双维数据源不同 | 批次列表读 `defect_statistics`（§6.11），明细读 `test_executions`；fallback 批次只写汇总 → 「无明细批次」 | 看板批次列表 | **D56** |

> **第 3 条本身就是一条踩坑记录**：`PROJECT_CONTEXT` §7.38 写过「白名单式判据与源码形状断言：修对了反而红」、§7.55「改字段名时只做字符串替换，测试会**假通过**」。**写死的行号和写死的源码文本会随文件演进而腐烂**——D51 盘点阶段就应当把这类硬编码判据从清单里剔除。

---

## 五、消费点重新定位（按函数名，不按行号）

| 函数 | 文件 | 对批次口径的依赖 |
|---|---|---|
| `list_executions_paged` | `src/core/case_manager.py` L1687 | 批次列表数据源（`defect_statistics`） |
| `get_execution_detail` | `src/core/case_manager.py` L1817 | 汇总（`defect_statistics`）+ 明细（`test_executions`）双表关联 |
| `get_execution_status` | `src/core/case_manager.py` L2429 | 批次状态查询 |
| 报告统计路由 | `src/web/routes/reports.py`（批次列表/趋势/概览） | 趋势与概览直接读 `defect_statistics` |
| 执行记录前端 | `src/web/static/js/*.js`（批次列表渲染） | 展示层，不改 |

**消费点总数：核心层 3 处 + 路由 1 处 + 前端 1 处。** 任一口径改动都需同步这 5 处中的相关部分。

---

## 六、D56 定口径的三个候选方案（供决策，本日不选）

### 方案 C（推荐作为主方案）：Day51 执行状态回写时接入真实 execution_id

- **做法**：让 `case["execution_id"]` 在批次编排层带上真实批次号，`resolve_bridge_execution_id` 的优先级 ① 自然命中，回落路径不再触发
- **依据**：`executors.py` L514 注释原话「真正的批次号关联留给执行编排层（Day51 执行状态回写时接入）」
- **性质**：**根因治理**，不是症状过滤
- **仍需兜底**：人工单跑用例时确实没有批次号，回落路径不能删

### 方案 A（推荐作为兜底）：回落时不写批次级表

- **做法**：`resolve_bridge_execution_id` 在无真实 `execution_id` 时返回空，`bridge_results_dir` 跳过入库（如实记为「未入库」而非写脏行）
- **优点**：零污染，改动面最小
- **代价**：单条执行后看板无该次批次记录（明细仍在 `test_executions`，未丢失）
- **风险**：必须保证「跳过」有日志，不能静默

### 方案 B（不推荐）：fallback 行加来源标记 + 消费点过滤

- **做法**：`pytest-{case_id}` 行加来源标记，5 个消费点过滤
- **不推荐理由**：改动面最大（写入点 1 + 消费点 5），且**新增消费点极易漏过滤**（§7.45 同类问题的教训：三处各写一套判据 = 三处都会漏）

**建议组合：C（主）+ A（兜底）。**

---

## 七、本日不做的事（防止范围蔓延）

- ❌ 不改 `save_statistics` 的 `failed`/`error` 字段映射（§6.3 已对齐）
- ❌ 不改 `resolve_bridge_execution_id` 的优先级顺序（真实批次号优先是正确设计）
- ❌ 不碰 `test_executions` 用例级表
- ❌ 不改前端展示层
- ❌ 不做 P3②（bridge_error 可见性，属 D52）

---

## 八、Day51 其余三项的边界（本盘点顺带确认）

| 登记项 | 与本盘点的关系 | 归属 |
|---|---|---|
| ② P3 bridge_error 无下游消费 | 与分歧 #2 同源（重复执行→桥接失败→不可见），但**不合并**：③要动落库字段、②要动消费点，两者在不同层 | D52 |
| ③ P3 本地手动全量 allure 竞态 | **与批次口径无关**（conftest 层目录隔离），独立 | D53 |
| ④ 实时进度 + 真实批次号关联 | **正是方案 C 的执行载体**——「真实批次号关联」就是让 `case` 字典带 `execution_id` | D55 |

> **关键耦合**：D55 的「真实批次号关联」做完后，分歧 #1（伪批次）在批次编排路径上自然消失；剩下的只有人工单跑的兜底需求。所以 **D56 的工作量取决于 D55 是否真正接上了 execution_id**。