# Day9 讲述材料：core 层架构（平台业务核心）

> 用途：Day9 阶段复盘讲述大纲。只写要点与数字锚点，讲述时按节展开，每节 3-5 分钟。
> 配套文档：[core_architecture.md](../core_architecture.md)

## 0. 一句话定位

core 层是平台的业务核心，承接 common（协议/日志/配置）与 db（持久化），
对上被 CLI 与 Web 调用，内部收敛为四大能力：**数据驱动、调度执行、报告分析、通知推送**。

---

## 1. 模块职责图（文字版）

```
common 层（基础设施）
  env_manager 配置注入 ｜ LogManager 统一日志 ｜ HttpClient/SerialClient/TelnetClient
  ｜ Assertion 断言库
  ▼
core 层（业务核心，四大引擎）
  data_driver   用例加载与内存三维筛选（YAML/Excel 双格式 → 统一 dict 结构）
  case_manager  入库 upsert → 批次创建 → 分级调度 → 结果记录 → 汇总统计（含 CLI）
  report_analyzer  Allure JSON 解析 → 统计聚合 → 持久化 → 趋势查询（三层分工）
  notification  BaseNotifier 抽象 + 邮件/企微 + 分级策略 + 重试退避 + 死信
  ▲
  ｜ 函数内延迟导入（规避 core ↔ db 循环依赖）
db 层
  ORM 模型（SQLAlchemy 2.0 声明式） + DatabaseSession 会话管理（SQLite/MySQL 双模式）
```

- data_driver 与 case_manager 顶部直接导入 db；report_analyzer / notification
  在函数内部延迟导入——导入图必须无环。
- 跨模块传递只用 dict / dataclass / pydantic 结构，不传 ORM 实体给上层。

## 2. 核心数据流（一条主线讲完整）

```
YAML/Excel
  → DataDriver.load_cases（解析+校验+规范化，报错带行号/字段名）
  → sync_cases_from_file（upsert 入 test_cases）
  → select_cases_for_execution（priority/case_id/tags 三维筛选，"排序即调度"）
  → 执行器（simulated 模拟 / 规划中 PytestRunner subprocess）
  → record_execution 落 test_executions（逐条，含 start/end/duration/error_message）
  → finish_execution 落 defect_statistics（批次汇总，唯一约束保幂等）
  → report_analyzer 解析 Allure 结果 → ReportStatistics.aggregate（统计唯一入口）
  → notification 旁路推送（失败绝不影响主流程）
```

关键口径：**全项目统计数字只有一个来源——aggregate**。Web 看板、批次汇总表、
通知模板三处数字同源，避免口径分叉。

## 3. 各模块讲述要点

### 3.1 env_manager / LogManager（common 横切）

- env_manager：python-dotenv 加载项目根 .env；优先级 系统环境变量 > .env > 默认值；
  get/get_int/get_bool/get_float 类型安全取值。
- LogManager：Loguru 单例，全模块统一 `get_logger()`，日志带 trace_id。
- 讲述点：横切关注点（配置/日志）不允许各模块自造，统一入口才能全局替换。

### 3.2 data_driver

- 双格式动机：测开要 YAML（可 review、版本友好），业务测试要 Excel（零门槛）。
- 三维筛选放内存：筛选发生在入库前，且不依赖 DB 连接，文件/DB 两数据源语义一致。
- 性能锚点：50 条混合数据，单条加载+校验+规范化约 0.55ms（约 1800 条/秒）。
- 异常体验：文件不存在/后缀不支持 → DataDriverError；YAML 错带行号；
  Excel 空行跳过、空表头报错。

### 3.3 case_manager

- 批次号 `RUN-YYYYMMDD-HHMMSS-xxxx`：可读 + 不依赖 DB 生成 + 进程内防碰撞。
- 分级执行：priority 升序天然对齐风险等级；tags 圈定 smoke/regression。
- dry-run：零副作用预览执行计划，CI 中可作冒烟前置。
- CLI 用 argparse（标准库、零供应链成本），`-p` 可 append 多优先级。
- 失败语义：failed=断言失败（缺陷疑似），error=环境/代码异常；failed/error
  必须带 error_message；单用例失败不中断整批。

### 3.4 report_analyzer（三层分工）

- 解析层 ReportAnalyzer：扫描 result.json，单文件损坏跳过不中断整批。
- 计算层 ReportStatistics：计数/分组/P95；labels 扁平数组转 Dict 一次命中。
- 持久层 ReportRepository：字段映射落库，execution_id 唯一约束保幂等。
- P95：≥20 条用百分位；<20 条取 max 近似——样本不足时保守高估。
- 模块名四级 fallback：feature→suite→parentSuite→full_name，unknown 兜底，
  分组总量守恒由测试锁定。

## 4. 关键设计决策（讲述时给"备选方案 + 放弃理由"）

1. **通知 send 吞异常返回 bool**：旁路能力不能把成功批次拖成失败。
2. **延迟导入解循环依赖**：core 与 db 互导必死；推迟到调用瞬间，加载图无环。
3. **执行器单点抽象**：`_simulate_execute` 与未来 PytestRunner 同构可切换。
4. **汇总唯一约束**：execution_id 唯一，重复 finish 走 upsert/幂等而非报错。
5. **标准库优先**：smtplib/argparse/sqlite3，内部工具不引可引可不引的第三方。

## 5. 踩坑与教训

- 统计口径分叉：早期通知模板自己算通过率，与看板数字不一致；
  收口为 aggregate 唯一入口后由测试锁同源。
- error→broken 映射双向自洽：入库时 failed=合计-broken，通知适配时
  error→broken，互为逆过程，漏一个方向数字就对不上。
- 损坏 JSON 中断整批：Allure 并发写产物偶发坏文件，后置消费必须容错跳过。
- 依赖方向靠纪律维持：新增 import 先问"会不会成环"，code review 必查。

## 6. Q&A 预设

- **Q：三维筛选为什么不下推 SQL？**
  A：筛选先于入库存在（参数化直用场景），内存筛选保证两种数据源同语义；
  万级用例时再引入生成器分片，当前量级无需。
- **Q：批次号为什么不用自增 ID？**
  A：可读性、不依赖 DB、CI/分布式节点本地可生成；同秒碰撞进程内重试消解。
- **Q：P95 小样本取 max 是否不严谨？**
  A：小样本百分位本身无统计意义，max 是保守估计，性能结论宁可高估风险。
- **Q：为什么不用 Celery 做调度？**
  A：当前秒级模拟执行 + SQLite 串行写锁，裸线程/队列足够；引入 broker 是过度运维。
- **Q：新增一种用例格式（CSV）怎么做？**
  A：后缀分发的解析器注册位，实现统一 load 接口即可，筛选与调度零改动。
- **Q：core 层怎么保证可测？**
  A：执行器/时钟/sleeper/仓储均可构造注入；179 条 core 相关测试锁定关键决策。

## 7. 收尾数字锚点

- 四大引擎；ORM 表从 Day9 的 3 张核心表演进到当前 6 张
  （新增 test_execution_batches、notification_dead_letters、notification_history）。
- 数据驱动约 1800 条/秒；每条模拟用例约 0.01s。
- core/notification 相关测试 179 条（Day9 基线），关键决策全部有测试锁定。
