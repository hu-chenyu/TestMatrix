# TestMatrix 路线图

> 本文件为公开路线图，只记录里程碑与能力状态，不包含逐日排期与内部管理细节。
> 项目纪律：每个里程碑交付可复现的前后对比数据，每个能力边界写进 [KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md)。

## 已交付里程碑

### 第一阶段：架构基座 ✅

- 目录骨架与模块分层（src / tests / docs / testdata / output）
- common 封装层：HTTP 客户端、日志、断言、环境配置
- 数据持久层：SQLAlchemy 2.0 ORM，SQLite / MySQL 双模式一键切换
- pytest 体系：标记注册、临时库 fixture、allure 集成
- Demo 验证：HTTP 接口测试用例全流程跑通

### 第二阶段：核心能力 ✅

- 数据驱动引擎：YAML / Excel 外部数据参数化，用例与数据彻底分离
- 用例调度管理：批次管理、P0-P3 分级执行、dry-run 零副作用预览、CLI/Web 双触发
- 报告分析引擎：Allure 结果解析、通过率/耗时 P95/失败明细统计、趋势数据入库
- 通知推送：邮件 HTML 报告 + 企微 markdown 摘要、失败@负责人、分级通知策略
- Flask + Redis 后端：Web 三页面骨架、Redis 任务队列（故障自动回退裸线程）

### 第三阶段：前端交付 ✅

- Dashboard：统计卡片 / 通过率趋势折线 / 模块分布环图 / 优先级柱图 / 失败 Top 榜
- 用例管理：列表 / 筛选 / CRUD / 批量导入
- 执行记录：批次列表 / 详情 / 触发 / SSE 实时日志
- CI/CD：GitHub Actions 五层质量门禁（ruff → mypy → 覆盖率+测试数下限 → pip-audit → 探测版本），双 job 矩阵

## 进行中里程碑

### 第四阶段：深度能力 🔄

- **真实 pytest 执行引擎**：subprocess 同步执行已落地（超时控制 / cwd·env 隔离 / 退出码映射 / cp936 解码 / source_ref 追踪）；规划中 pytest 钩子与自定义插件、xdist 并发、模拟/真实双模式切换
- **Flaky 用例治理**：检测 / 标注 / 隔离专项，不依赖 reruns 藏问题
- **AST 精准回归脚本**：覆盖映射 + import 拓扑 + 影响面反查，脚本级实验

## 规划中里程碑

### 第五阶段：工程化交付 ⏳

- Docker 三服务编排（Web / Worker / MySQL）+ Jenkins + k6 性能压测基线
- MySQL 深度优化（EXPLAIN / 索引）
- 质量打磨与重构：类型标注补全、PEP8 全量、复杂函数拆分
- 演示与博客交付：项目复盘、技术文章、开源治理经验

## 质量基线（当前）

| 指标 | 数值 |
| --- | --- |
| 测试总数 | 1473（CI 下限断言，只增不减） |
| 覆盖率 | 99.05% |
| CI 状态 | 双 job 全绿（Python 3.11 主 + 3.12 探测位） |
| 提交纪律 | 提交不间断（唯一空档为 2026-09-06 周日） |
| 缺陷清零 | 79 条 bug 逐轮清零，安全类修复逐条变异验证 |

## 能力边界

每个能力边界如实标注，不做无证据承诺。详见 [KNOWN_LIMITATIONS.md](../KNOWN_LIMITATIONS.md)。

---

*本路线图随里程碑更新，不记录逐日排期与内部管理细节。*
