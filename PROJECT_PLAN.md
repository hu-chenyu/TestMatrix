# TestMatrix 项目开发计划（PROJECT_PLAN）

> 本文件是 TestMatrix 项目进度的**唯一追踪依据**，覆盖 2026-08-22 至 2026-12-18 共 119 天（B 方案·119版）。
> 每天完成任务后，将对应行的 `[ ]` 改为 `[✓]`；进行中改为 `[~]`。
> 状态约定：`[ ]` 待开始 / `[~]` 进行中 / `[✓]` 已完成

**修订历史**

| 版本 | 日期 | 说明 |
| --- | --- | --- |
| 初版 | 2026-08-27（Day6） | 计划结构成型，补充深度重构、技术博客与开源准备阶段 |
| 修订① | 2026-09-03（Day12） | 精简技术栈（砍K8s/芯片Mock），加强真实pytest执行/性能优化/真实场景验证/技术博客，总天数 181→150 |
| 修订② | 2026-09-03（Day12） | 深度目标强化，150→180 天（+30 天按分配方案落位），新增 Redis(3天)/AST(2天)/AI扩展(1天)/依赖编排(3天)，量化成果强制化 |
| 修订③ | 2026-09-27（Day37） | 排期重排：执行器前移拆两段/AI 扩12天/AST 升级精准回归9天/CI 插入/Flaky 治理5天，总工期180天 |
| **修订④（现行·B方案·119版）** | **2026-09-28（Day37）** | **基于 evolving 二次评估 + 六个 AI 独立复核核定 B 方案 119 版：总工期 180→119 天（后续 82 天 = 71 模块日 + 11 天缓冲）。核心变更：删除 AI 扩展12天、Web前端12→4天、独立联调并入执行器、全量回归13天降级、AST 9天→2天脚本+博客、依赖编排取消、开源准备11→2天、博客7→5篇、MySQL 6→4天、重构6→5天、演示3→2天、CI 2→3天。Flask 不迁 FastAPI。真实执行器 Phase-1/2（20天）与 Flaky 治理（5天）为核心深度保留。Flaky 从 Day96-100 错峰至 Day115-119（避开 AquaMind M4 S1-S8），原位置改机动缓冲，总缓冲 5→11 天。演示删录屏/视频改操作手册+截图。详见第六节。** |
| 修订⑤ | 2026-10-02（大扫除 v1+v2） | **质量专项（不占 119 天工期，独立进行）**：v1 完成 79 条 bug 全清零（P0×1/P1×6/P2×42 全清、P3 清理 8 条；分级分解为当时记录，与总数不符，仅总数可信），覆盖率 93%→96%，测试 497→828；v2 完成覆盖率 96%→98.75%（测试 828→935→951）、第二轮代码审查产出 22 条发现（报告 docs/bug_audit_report_v2_20261002.md）、backlog 低风险清理 3 条。当前基线：**951 passed / 0 failed / 0 rerun，覆盖率 98.75%（3754 语句 / 47 未覆盖，24 文件 100%），ruff 0 错误，mypy 新增行 0 错误，CI 3.11+3.12 双 job 全绿**。待决项见 docs/bug_audit_report_v2_20261002.md 第七节修复顺序 |
| 修订六 | 2026-10-02（大扫除 v3） | **质量专项续（修复 v2 审查发现的 22 条）**：安全类 3 条（控制台 sink 诊断断言重写并变异验证、params 字典按查询串字段表脱敏、Windows 含空格路径脱敏）、健壮性 8 条（worker 引用竞态条件清空、MySQL userinfo 改用 quote、IPv6 方括号、SSE 降级帧不占序列位置、YAML 编码错误包装、SECRET_KEY 判定改正向、IntegrityError 收窄、描述与标签共存、缓存失效统一兜底）、测试质量 4 条（三条「不抛异常」式假验证补真断言、前端语义断言正则按函数作用域锚定）、P3 三条 + 禁改词 2 处。过程中推翻 v2 报告两处不可达前提（loguru 0.7.2 的 diagnose 不输出变量值、NOT NULL 违例经 create_case 不可达）。当前基线：**1013 passed / 0 failed / 0 rerun，覆盖率 99%（3788 语句 / 46 未覆盖），ruff 0 错误，mypy 新增行 0 错误**。暂缓 5 条 P3 需产品确认或高风险，详见 docs/bug_audit_report_v2_20261002.md |
| 修订七 | 2026-10-02（大扫除 v4 + v5） | **质量专项续（第三轮双 AI 交叉审查 + 修复 13 条）**：v4 产出两份独立审查报告（MiniMax 20 条 + 豆包 Hy4-Preview 8 条，含交集/独有/分歧对比）。v5 分 3 个 commit 修复 13 条——Commit A 安全类 4 条（**修复 v3 引入的路径脱敏能力回退**：name 段漏排除空格致同消息第二条 Windows 路径整体泄露；禁止字符集拆三个具名常量；500 响应体不回显 SQL）、Commit B 测试质量 4 条（纯 tests/：缓存失效补 warning 真断言、SSE 重连游标改由请求1 实际 id 推导、params 脱敏补阳性对照）、Commit C 健壮性 5 条（IPv6 按冒号数量区分 + host:port 误配显式拒绝、TM_ENV 缺省告警、SSE 终态事件豁免断点过滤消除重连热循环、cases.js escapeHtml 收敛到单一来源并保留幂等导出、docstring 口径更正）。**过程中推翻 v4 自己的结论**：豆包对 loguru `diagnose=True` 的判断正确（敏感变量作为实参出现在异常帧那一行时确实会输出值），v4 报告末尾追加「结论更正」小节留痕，未悄悄改写。4 项安全/健壮性修复**逐条变异验证全部如期变红**。当前基线：**1040 passed / 0 failed / 0 rerun，覆盖率 98.76%（3803 语句 / 47 未覆盖），ruff 0 错误，mypy 新增行 0 错误，CI Run #45 3.11 六步 + 3.12 探测位全绿** |

---

## 一、项目概览

| 项目 | 内容 |
| --- | --- |
| 项目名称 | TestMatrix 通用自动化测试效能平台 |
| 项目定位 | 面向互联网接口自动化测试的全链路效能平台（用例管理→调度执行→报告分析→Web可视化→通知推送→CI/CD→性能优化）；芯片扩展能力预留（common 层 serial/telnet 封装，架构支持双赛道） |
| 定位声明 | **Python 技术栈的个人测试工程化全链路实践**——为了吃透测试工程化各环节而自建，用真实 pytest 项目验证平台自身；不是开源产品、不追求用户量、不与 MeterSphere 等商业平台做功能竞争 |
| 深度目标 | 每个技术栈达中级偏上~高级深度，每个优化阶段有量化对比数据，每个能力边界有明确声明 |
| 起止日期 | 2026-08-22 ~ 2026-12-18（总跨度 119 天，B 方案·119版） |
| 总天数 | 119 天（Day1 ~ Day119；Day119 为项目最终交付日） |
| 当前进度 | Day48-fix2（真实执行器 Phase-1 Day4 第二次热修：env/cwd 异常逃逸修复——`subprocess.run` 启动参数非法抛的 `TypeError`/`ValueError` 原本不在 `run_one` 的 except 链内会直接上抛，补构造期 env 内容校验 + 兜底层捕获）/ Day119 已完成（40%）；1434 条 pytest 全过（0 failed / 0 warning）；覆盖率 98.97%；**CI Run #71 双 job 全绿**；48 天里 47 天有提交（唯一空档 2026-09-06 周日），全部推送 GitHub；实际开工日 2026-09-29（计划日+1，由 Day109-114 缓冲吸收 1 天偏移） |
| 技术栈 | Python 3.11→当日最新稳定（预期 3.14/3.15，下限不低于3.14，Day74 升级，含 telnetlib 替代；CI 多版本矩阵） / pytest 7.4→当日最新稳定（预期 9.x，Day74 按插件兼容矩阵确认） / SQLAlchemy 2.0（SQLite/MySQL 双模式）/ Redis / Flask 2.3→3.x / Jinja2 / Bootstrap 5 / ECharts 5 / Loguru / Allure 2 / Docker / Jenkins / GitHub Actions / k6 / AST（脚本级精准回归） |
| 面向对象 | ① 测试工程师（fork 二开/参考架构）② 开源社区贡献者 ③ 项目维护者（长期技术积累与迭代） |
| 可运行性目标 | git clone 后按 README 三步内启动 Web 平台 → 看到 Dashboard → 触发执行 → 看到报告 |
| 预估代码量 | 源码约 1.8-2.2 万行；含测试/配置/文档总量约 3.2-3.8 万行；测试用例终态 ≥450 条（只增不减，当前 1434 条） |

### 与 AquaMind 的关系（互补，非竞争）

| 维度 | TestMatrix | AquaMind | 互补关系 |
| --- | --- | --- | --- |
| 测试对象 | 传统 HTTP API + 芯片板卡 | LLM/AI 应用 | 对象互补，覆盖传统+AI 两大赛道 |
| 测试维度 | 功能测试+工程化全链路+平台化 | 非功能测试（负载下质量退化）+统计严谨性 | 维度互补，功能+非功能全覆盖 |
| 核心能力 | 用例管理→调度→报告→Web→通知→CI/CD 一条龙 | 并发阶梯实验设计+退化统计判定(S1-S8)+公开退化数据集 | 工程广度 vs 技术深度 |
| 职业叙事 | "能从零搭完整测试平台"（工程广度+全链路闭环） | "能做 AI 原生测试创新"（前沿性+统计严谨性） | 一个证明能干活，一个证明有想法 |

> **执行协调原则（119版最终事实）**：① 两项目最深任务错峰——TestMatrix 执行器深水（Day45-64，10/5-10/24）落在 AquaMind M1/M2 基础建设期；Flaky 治理（Day115-119，12/14-18）落在 AquaMind M4 完全结束后。② AquaMind 旗舰窗口（M3 10/25~11/15、M4 11/16~12/13）期间，TestMatrix 不排"研究型深水"（执行器/Flaky），仅排中强度任务（DevOps/质量打磨/MySQL/AST）与轻量任务（机动/演示/博客）；重构仅在 Day85 Go/No-Go 通过后执行 Day86 低风险拆分，Day87-88 高风险重写按约束 #8 ② 顺延规则处理。③ 冲突优先级：本职工作 > AquaMind 旗舰 > TestMatrix；同一天不做两个项目的"深度任务"。④ 倦怠触发器（可测量）：连续 5 个工作日完成率 <80% → 立即启用砍件序，下一工作日 TestMatrix 只做 CI 维护。⑤ 11 天缓冲（Day96-100 机动 + Day109-114 缓冲）不得预支，仅在触发砍件或工作突发时动用；缓冲日允许无提交。⑥ 重构降级预案见阶段七。⑦ AquaMind 3 个休息日（11/5、11/19、12/10）= 双项目同休。⑧ 12/13 复盘点：剩余 >25 模块日则 Flaky 降级最小可交付并顺延 2027。

### 质量基准线（Day37 首测 · 滚动更新至最新）

| 指标 | 实测值 | 口径 |
| --- | --- | --- |
| 源码行数（common+db+core+web 后端+前端自研） | **14,555 行**（44 个入库 .py/.html/.css/.js 文件，逐文件行数求和） | 已达成 B 方案终态目标 18k-22k 的 **66%~81%**（首测口径） |
| tests 行数 | 15,182 行 | 终态不设上限，随用例只增不减（首测口径） |
| 用例条数 | 1434 条（实跑 1434 passed） | 已达 ≥450 条下限的 318%；终态口径为 ≥450 条只增不减 |
| 语句覆盖率 | **98.97%**（4764 语句 / 49 未覆盖，pytest --cov=src 实测） | 已达成 CI 门禁 ≥85%；剩余未覆盖集中在异常分支与降级路径，随质量打磨滚动补齐 |

> 结论：产量健康、工期宽裕（B 方案后续 82 天 = 71 模块日 + 11 天缓冲），剩余重点是**把心脏（真实执行器）做透、把 Flaky 做深**，而非继续堆量。

---

## 二、开发铁律（全程有效）

1. 每天一个明确目标，每天提交代码，commit 遵循 Conventional Commits：`<type>(<scope>): <概括>` + 正文关键点——**例外：缓冲日（Day96-100 / Day109-114）允许无提交、无目标，该例外由约束 #8 直接授权，不视为违反铁律**。缓冲的价值取决于它是否真的空着。
2. 新模块只改占位文件 + 新增测试文件，不修改已有代码
3. 日志统一用 `LogManager`；PEP8、中文注释、类型标注
4. 核心功能日任务量基准：300-500 行代码 + 8-15 条测试用例（行数是参考不是 KPI，风险覆盖和可演示场景优先）
5. 写完跑 pytest 全量通过后再提交（当前基线：**1434 条**）
6. 每做完一个核心模块留复盘日：回顾设计决策 + 更新讲述材料 + 质量补漏 + 模块文档
7. **量化成果强制**：性能优化/MySQL 优化/Redis 引入/真实执行接入/Flaky 治理，**必须产出优化前后对比数据与对照组数据**，并固化测量协议（同机/同数据集/重复 3 次取中位/脚本进 `scripts/`）
8. 范围控制：做精不做全，每个模块交付"能演示的完整功能"；不凑技术栈，每个保留的技术都要有充分理由
9. **ADR 即时写**：架构决策发生的当天就写进 `docs/adr/`，不事后统一补写。ADR 的价值是"当时的判断依据与备选方案取舍"，不是文档数量
10. **模块讲述材料按模块复盘时生成**，确保不看代码也能讲清每个设计决策
11. **覆盖率增量门禁**：各里程碑验收时覆盖率必须达标——M5(CI) ≥80% → M9(执行器深水区) ≥82% → M11(质量打磨) ≥85% → M13(MySQL深优) ≥85%。不达标则停下补测，不得进入下一阶段
12. **Flask 不迁 FastAPI**：本项目是 Jinja2 服务端渲染 + 同步 CRUD + SSE，Flask 完全胜任；迁移零功能改进且引入回归风险。被问到时回答"服务端渲染无异步收益，迁移是成本不是改进"

### 复盘日标准动作

> 目标：把"写出来的代码"变成"自己讲得出来的代码"。五个动作全部完成才算该复盘日结束。

| 动作 | 内容 | 完成标准 |
| --- | --- | --- |
| **动作1 代码消化** | 通读本阶段模块全部源码；**合上代码**能凭记忆画出模块架构图、讲清完整数据流、说出每个类和关键方法的职责 | 卡壳的位置标记后重读源码，直到能凭记忆复述 |
| **动作2 更新讲述材料** | 写入仓库外的讲述材料文件。按四要素追加本阶段深度点：① 踩过的坑 ② 选型对比（至少2个备选，讲清 trade-off）③ 设计决策 ④ 改进空间 | 每个深度点 300-500 字、口语化、能直接讲 3-5 分钟 |
| **动作3 质量补漏** | 本模块测试覆盖率自查；补边界值/异常路径测试；修掉遗留小 bug | **当天跑全量 pytest，测试条数只增不减** |
| **动作4 模块文档** | 更新 `docs/` 下对应模块文档（架构图/设计决策/扩展点） | 文档与代码当前状态一致 |
| **动作5 独立自讲验收** | 不看代码、对照讲述材料自讲一遍 | 每个深度点 3-5 分钟讲得下来；讲不下来的点回源码重读后再讲 |

---

## 三、119 天排期总览（B 方案·119版，阶段视图，Day1 ~ Day119）

| 阶段 | 天序 | 天数 | 起止日期 | 内容 |
| --- | --- | --- | --- | --- |
| 已完成 | Day1-37 | 37 | 08-22 ~ 09-27 | 架构基座/data_driver/case_manager/report_analyzer/通知模块/Web后端/Redis/Web前端骨架 |
| 阶段一 CI 与工具链 | Day38-40 | 3 | 09-28 ~ 09-30 | CI 工作流 + pyproject.toml + ruff/mypy + 覆盖率门禁 + serial/telnet 0% 覆盖修复 + 依赖安全 |
| 阶段二 Web 前端收尾 | Day41-44 | 4 | 10-01 ~ 10-04 | Dashboard 图表 + 用例管理页 + 执行记录页（三页面，轻量快速） |
| 阶段三 真实执行器 Phase-1（含联调） | Day45-54 | 10 | 10-05 ~ 10-14 | subprocess 真实执行 + source_ref + 结果桥接 + Web 触发 + 任务队列 ACK/requeue + **Day52 验收门禁** + 全链路联调 |
| 阶段四 真实执行器 Phase-2 深水区 | Day55-64 | 10 | 10-15 ~ 10-24 | pytest 钩子 + 自研可打包插件 + xdist 并发 + 写库竞态处理 + 全量基线→并发优化量化对比 |
| 阶段五 DevOps | Day65-78 | 14 | 10-25 ~ 11-07 | Docker 多阶段+compose 三服务+Jenkins 流水线+k6 压测+Python 升级（3.11→当日最新稳定，含 telnetlib 替代）/pytest 当日最新稳定（预期 9.x）/Flask 3.x 升级 |
| 阶段六 质量打磨 | Day79-84 | 6 | 11-08 ~ 11-13 | 性能优化+代码质量+安全加固（含 subprocess 注入防护）+边界异常补测+真实场景验证 |
| 阶段七 深度重构 | Day85-89 | 5 | 11-14 ~ 11-18 | 三文件差异化重构：notification 物理拆分 + report_analyzer 抽象消重 + case_manager 逻辑重构（marshmallow 序列化改造） |
| 阶段八 MySQL 深优 | Day90-93 | 4 | 11-19 ~ 11-22 | EXPLAIN 慢查询+索引优化+SQLAlchemy 调优+量化对比+Redis 缓存深化 |
| 阶段九 AST 脚本+博客 | Day94-95 | 2 | 11-23 ~ 11-24 | AST 精准回归脚本（覆盖映射+import 拓扑+影响面反查，基于 Day45 POC 工程化）+ 节省率/漏检率双指标量化 + 用例依赖清单导出 + AST 博客 |
| 阶段十 机动缓冲（AquaMind M4 S1-S8 期间） | Day96-100 | 5 | 11-25 ~ 11-29 | 延期征用/靶向补强/UX 打磨/长时运行稳定性/MySQL 切换回归；AquaMind S1-S8 期间 TestMatrix 仅做轻量任务 |
| 阶段十一 演示打磨 | Day101-102 | 2 | 11-30 ~ 12-01 | Demo 操作手册（命令序列+预期输出+截图）+ 真实感数据集 + 一页纸架构图 + 彩排（**不录视频**） |
| 阶段十二 技术博客 3 篇 | Day103-106 | 4 | 12-02 ~ 12-05 | 执行器设计/xdist 写锁竞态治理/架构复盘 三篇（Flaky 篇移至 Day119），统一发布归档 |
| 阶段十三 交付准备 | Day107-108 | 2 | 12-06 ~ 12-07 | README 初稿+CHANGELOG+LICENSE 核查+全量回归预跑 |
| 阶段十四 缓冲（AquaMind M4 收尾期） | Day109-114 | 6 | 12-08 ~ 12-13 | 延期吸收/观测前缓冲/AquaMind M4 收尾期间 TestMatrix 轻量待命 |
| 阶段十五 Flaky 用例治理（核心深度） | Day115-119 | 5 | 12-14 ~ 12-18 | 重复执行→方差计算→自动标注→隔离队列→Dashboard 展示；**误标率指标**（结论成立的条件）；Day119 含 Flaky 博客发布+最终 freeze |

**合计：37+3+4+10+10+14+6+5+4+2+5+2+4+2+6+5 = 119 天，末日 Day119 = 2026-12-18**（后续 82 天 = 71 模块日 + 11 天缓冲）

---

## 四、119 天开发计划总表（逐日）

### 阶段A：核心基座 + report_analyzer（Day1-9）✓ 已完成

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day1 | 08/22（六） | W1 | 架构基座：分层目录/env_manager/LogManager/ORM模型/双模式DB/HttpClient/断言库 | 架构基座 | 全部 common 层+db 层 ~1500 行 | [✓] |
| Day2 | 08/23（日） | W1 | data_driver 引擎：YAML/Excel 统一加载/字段校验/三维筛选+50 条大数据量验证 | data_driver | 引擎+Demo 用例 53 条测试 | [✓] |
| Day3 | 08/24（一） | W2 | case_manager Day1：异常类/批次号生成/用例加载入库/多维度查询 | case_manager | 4 个方法+9 条测试 | [✓] |
| Day4 | 08/25（二） | W2 | case_manager Day2：批次创建/待执行筛选/执行结果记录/批次汇总统计 | case_manager | 4 个方法+14 条测试 | [✓] |
| Day5 | 08/26（三） | W2 | case_manager Day3：run_batch 批量执行/CLI 入口/**模拟执行器（临时占位，Day52 起被真实执行取代）** | case_manager | CLI+批量链路+10 条测试 | [✓] |
| Day6 | 08/27（四） | W2 | report_analyzer Day1：Allure 结果目录扫描/result JSON 解析/用例级数据提取 | report_analyzer | 解析器~400 行+12 条测试 | [✓] |
| Day7 | 08/28（五） | W2 | report_analyzer Day2：统计聚合（通过率/耗时/失败明细，按模块/优先级分组） | report_analyzer | 聚合引擎~400 行+12 条测试 | [✓] |
| Day8 | 08/29（六） | W2 | report_analyzer Day3：defect_statistics 入库+趋势数据生成+模块集成测试 | report_analyzer | 入库+趋势~350 行+10 条测试 | [✓] |
| Day9 | 08/30（日） | W2 | 复盘日：core 层测试补全+架构文档+讲述材料第 1 次 | 复盘/文档 | 架构文档+补测 16 条 | [✓] |

### 阶段B：通知模块收官（Day10-16，7天）✓ 已完成

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day10 | 08/31（一） | W3 | 通知模块 Day1：架构设计+BaseNotifier+smtplib 邮件通知器 | 通知模块 | 通知基座~350 行+10 条测试 | [✓] |
| Day11 | 09/01（二） | W3 | 通知模块 Day2：HTML 邮件报告模板（内联 CSS 汇总表格） | 通知模块 | 邮件模板~300 行+8 条测试 | [✓] |
| Day12 | 09/02（三） | W3 | 通知模块 Day3：企业微信机器人 webhook 通知（markdown 消息） | 通知模块 | 企微通知器~300 行+10 条测试 | [✓] |
| Day13 | 09/03（四） | W3 | 通知模块 Day4：分级通知策略（全量/仅失败）+失败用例@负责人 | 通知模块 | 通知路由~350 行+10 条测试 | [✓] |
| Day14 | 09/04（五） | W3 | 通知模块 Day5：重试机制（指数退避 1/2/4/8s+最大重试次数+死信记录入库） | 通知模块 | 重试+死信~300 行+10 条测试 | [✓] |
| Day15 | 09/05（六） | W3 | 通知模块 Day6：与 case_manager 集成（批次完成自动推送）+mock 单测全覆盖+.env 完善 | 通知模块 | 集成~300 行+12 条测试 | [✓] |
| Day16 | 09/06（日） | W3 | 复盘日：通知模块回顾+模块文档+讲述材料第 2 次 | 复盘 | 讲述材料+模块文档+补测+独立自讲 | [✓] |

### 阶段C：Web后端 + Redis（Day17-34，18天）✓ 已完成

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day17 | 09/07（一） | W4 | Flask 应用工厂 create_app+蓝图架构+env 配置对接+web 目录骨架 | Web后端 | 应用工厂~350 行+10 条测试 | [✓] |
| Day18 | 09/08（二） | W4 | 统一响应封装+全局异常处理+健康检查 API（/health+数据库连通性） | Web后端 | 基座~300 行+10 条测试 | [✓] |
| Day19 | 09/09（三） | W4 | 用例列表 API（分页/模块/优先级/类型/关键字搜索） | Web后端 | 列表 API~350 行+10 测试 | [✓] |
| Day20 | 09/10（四） | W4 | 用例 CRUD API（新增/编辑/删除+marshmallow 入参校验） | Web后端 | CRUD API~400 行+12 条测试 | [✓] |
| Day21 | 09/11（五） | W4 | 用例批量导入 API（YAML/Excel 上传→DataDriver→入库） | Web后端 | 导入 API~300 行+10 条测试 | [✓] |
| Day22 | 09/12（六） | W4 | 执行记录 API（批次列表/单用例详情/失败堆栈） | Web后端 | 查询 API~350 行+10 条测试 | [✓] |
| Day23 | 09/13（日） | W4 | 报告统计 API（汇总/趋势/模块分布/失败 Top）+质量度量 API（覆盖率/缺陷密度/执行效率） | Web后端 | 统计 API~400 行+12 条测试 | [✓] |
| Day24 | 09/14（一） | W5 | 用例执行触发 API（异步执行+run_batch 调用+批次状态查询）+执行器策略抽象（BaseExecutor/**模拟**/pytest 骨架/工厂） | Web后端 | 触发 API~400 行+10 条测试 | [✓] |
| Day25 | 09/15（二） | W5 | SSE 实时日志推送框架（日志通道+流式响应） | Web后端 | SSE 框架~350 行+8 条测试 | [✓] |
| Day26 | 09/16（三） | W5 | SSE 执行状态实时更新+断线重连处理 | Web后端 | SSE 增强~300 行+8 条测试 | [✓] |
| Day27 | 09/17（四） | W5 | 通知集成（Web 触发执行完成后自动邮件/企微推送） | Web后端 | 集成~250 行+8 测试 | [✓] |
| Day28 | 09/18（五） | W5 | Web 后端集成测试（Flask test client 全 API 覆盖） | Web后端 | 集成测试~15 条 | [✓] |
| Day29 | 09/19（六） | W5 | API 打磨（边界参数/错误码统一/日志埋点） | Web后端 | 优化~300 行 | [✓] |
| Day30 | 09/20（日） | W5 | API 文档 v1（全接口清单+请求/响应示例） | Web后端 | API 文档 1 份 | [✓] |
| Day31 | 09/21（一） | W6 | Redis 接入+缓存层（用例列表/统计结果缓存+TTL 失效策略） | Redis | 缓存层~350 行+10 条测试 | [✓] |
| Day32 | 09/22（二） | W6 | Redis 任务队列（异步执行任务队列+任务状态管理，替代裸线程） | Redis | 任务队列~350 行+10 条测试 | [✓] |
| Day33 | 09/23（三） | W6 | Redis 量化对比：缓存命中前后响应时间/吞吐量数据+缓存穿透防护 | Redis | 量化对比报告 1 份 | [✓] |
| Day34 | 09/24（四） | W6 | 复盘日：Web后端+Redis 阶段回顾+讲述材料第 3 次 | 复盘 | 讲述材料+模块文档+补测+独立自讲 | [✓] |

### 阶段D（已开始）：Web前端骨架与 Dashboard（Day35-37，3天）✓ 已完成

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day35 | 09/25（五） | W6 | 前端骨架：Bootstrap5+Jinja2 模板继承+导航栏+静态资源结构 | Web前端 | 骨架~400 行 | [✓] |
| Day36 | 09/26（六） | W6 | Dashboard 页布局+ECharts5 集成+统计 API 对接 | Web前端 | Dashboard 框架~350 行 | [✓] |
| Day37 | 09/27（日） | W6 | Dashboard 统计卡片（用例总数/通过率/执行批次/失败数） | Web前端 | 卡片组件~300 行 | [✓] |

---

### 阶段一：CI 与工具链（Day38-40，3天）

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day38 | 09/28（一） | W7 | GitHub Actions 工作流（单 Python 版本 + 全量 422 + 构建徽章上 README）+ `pyproject.toml`（**ruff 全量强制 / mypy 仅新增文件强制**）；CI 矩阵含 Python 3.11（主）+ 3.12（探测，仅告警不阻断）；**依赖安全**：(a) 显式 pin 传递依赖 Werkzeug/itsdangerous/blinker；(b) 升级含已知 CVE 的直接依赖 Jinja2≥3.1.6、requests≥2.32.x、cryptography≥42，升级后跑 422 回归；(c) CI 加 **pip-audit** 步骤（高危项修复或写带过期日的 allowlist） | 工程化 | CI 配置 + pyproject.toml + 徽章；pip-audit 无未豁免高危项；422 测试零回归【热修3项：①3.12 探测位 step 级 continue-on-error 改 job 级保信号+结果汇总告警步 ②results_dir fixture 竞态消除 ③allowlist 缺过期日 fail-closed】 | [✓] |
| Day39 | 09/29（二） | W7 | 覆盖率接入（`--cov=src --cov-report=term-missing`）数字上 README + **serial/telnet 0% 覆盖修复**（pyserial `loop://` 伪串口 + 本地 socket 模拟） | 工程化 | 覆盖率徽章 + 串口/telnet 测试补测 | [✓] |
| Day40 | 09/30（三） | W7 | CI 矩阵全绿验证 + 覆盖率门禁确认（≥80%）+ 复盘日：CI 阶段回顾+讲述材料第 4 次 | 工程化/复盘 | CI 绿灯；覆盖率≥80%；讲述材料 | [✓]（fix 8089487 行级门禁、fix2 经 amend 改署名→afded06 fail-closed+异常链、fix3 34a59a6 守卫前置+徽章） |

> **为什么先把 CI 做起来**：阶段七要拆分 2438 行的 `case_manager.py`，属于高风险逻辑重构。**没有 CI + 422 条回归护体，这一步必翻车。**

### 阶段二：Web 前端收尾（Day41-44，4天）

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day41 | 10/01（四） | W7 | Dashboard 通过率趋势折线图（近 N 次批次）+ 模块分布饼图 + 优先级柱状图 + 失败 Top 榜 | Web前端 | 四图表~400 行 | [x] |
| Day42 | 10/02（五） | W7 | 用例管理页：列表表格+分页+筛选搜索联动（模块/优先级/类型/关键字）+ 新增/编辑表单（弹窗+字段校验） | Web前端 | 管理页~500 行 | [x] |
| Day43 | 10/03（六） | W7 | 执行记录页：批次列表（批次号/触发方式/统计徽章）+ 单用例详情（结果/耗时/失败堆栈）+ 执行触发（文件选择/筛选/dry-run）+ SSE 实时日志展示 | Web前端 | 记录页~500 行 | [✓]（commit 6b44500：executions.html 20→301 行 + executions.js 新建 1067 行 + 骨架断言更新，+1362/-12；Run #46 全绿。**缺陷修复 commit 1dc7ede**：双 AI 交叉审查共 8 条（3 P1+5 P2）全部修复，executions.js +192/-19，Run #47 全绿，详见 docs/bug_audit_report_day43_mavis_20261003.md 与 docs/bug_audit_report_day43_CodeBuddy_20261003.md。**Day43 收尾批量修复 commit 9e50de2**：双审查交叉确认的 10 项（PytestRunner 骨架命令/输出截断取尾部、case_id 正则、SSE onerror 三分支、分页 pendingPage 补刷、图表错误态、priorityBadge 原型链、favicon 404、compose 口令强制显式配置等）全部落地，10 文件 +617/-45，新增 tests/test_day43_closing_demo.py 27 条，测试 1054→**1081**，覆盖率 98.77%，**Run #52 双 job 全绿**） |
| Day44 | 10/04（日） | W7 | 三页面整体走查+边界 UX（空态/加载/异常）+ 删除确认+toast 反馈+复盘日：前端阶段回顾+讲述材料第 5 次 | Web前端/复盘 | 走查清单+截图+讲述材料 | [✓]（**P3 清单 5 项清理**：D15 importCases 业务码白名单统一为区间判据、D16 chart-helper 新增 disposeChart/disposeAllCharts 并挂 beforeunload、M3 defect_statistics 加 idx_ds_created_at 索引、D1 run.py 非回环绑定安全告警、D9 日志区 MAX_LOG_LINES=2000 上界防御；**边界 UX**：三页面空态文案、toast 图标+分级时长（成功/信息 3s、警告/失败 5s）、删除确认弹窗、加载态 spinner 与按钮禁用全部复核通过；**可选优化**：趋势图 tooltip 补时间行；**三页面全量浏览器走查**：控制台 0 error，131 请求 0 个 4xx/5xx，201/202/204 状态码各自印证新增/触发/删除路径，favicon 0 请求；**走查新发现并修复 1 条**：趋势图 20 点数据标签重叠成"100100100100100.0%"，加 labelLayout.hideOverlap 修复；新增 tests/test_models_index_demo.py 7 条，测试 1081→**1088**，覆盖率 98.77%，ruff 0 错误，mypy 增量 0 错误；复盘文档 docs/frontend_phase_review_day44.md（157 行，untracked）**【Day44 收尾：全量审查 30 bug 批量修复，commit 25711f3，31 文件 +1996/-161】**P1-01 **PytestRunner 的 `--` 终止符位置错误**（Day43 修复引入的回归：argparse 在 `--` 之后停止解析选项，`-q`/`--tb=short` 被当成路径，pytest 报 `file or directory not found: -q` 退出码 4，`TM_EXECUTOR=pytest` 下每条用例必落 error，真实执行链路功能性不可用）→ 选项移到 `--` 之前 + **补真实子进程集成测试**（不 mock subprocess.run：通过用例实测 `result=passed`、失败用例 `result=failed`——当初纯 mock 正是掩盖该缺陷的原因）。P2 组 15 项：compose 强制 MySQL 口令反噬 sqlite 默认路径、`.env.example` 占位口令击穿同一道 fail-closed（改留空+run.py 哨兵+3306 绑回环）、page 无上界致 SQLite OverflowError→**500**（改 MAX_PAGE 单一来源，越界返 **400**）、队列构建期 ValueError 致路由 500+worker 静默死亡、通知退避无上界致队列阻塞、LIKE 通配符未转义、导入 N+1、SSE 无存活与并发上限（30 分钟+单 IP 5 条，超限 **429**）、前端无 AbortController 超时、标签缺失 WARNING 刷屏、排序复合索引、导入通道 case_id 缺字符集、看板缺 ECharts 守卫、CI reruns 掩盖 flaky+测试数无下限断言。P3 组 14 项：worker 循环结构兜底、状态 hash TTL、重试/退避上界、死信分页上限、死代码清理、ID strip 归一、cache 序列化异常兜底、db_session 冗余分支、三页 noscript、aria-label、.dockerignore、Dockerfile 非 root+镜像源可配置、API.md/README 漂移修正。测试 1088→**1165**（新增 `tests/test_day44_bugfix_demo.py` 77 条，三连跑零 flaky），覆盖率 98.77%→**98.55%**（门禁 85），ruff 0，mypy 增量门禁 0（133 条存量按增量口径豁免），三页面浏览器复验 0 error。**Run #54 双 job 全绿**：3.11 四步 ruff/mypy增量/pytest1165-cov85/pip-audit 分级门禁均 success，3.12 探测位亦 success） |

> **B 方案说明**：原计划 12 天压缩为 4 天。测开岗对前端要求是"能看、数据真"，三页面核心功能 4 天足够；UX 润色后置到机动缓冲阶段。

### 阶段三：真实执行器 Phase-1（含联调）（Day45-54，10天）

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day45 | 10/05（一） | W8 | 接入方案设计（subprocess 封装/结果桥接/source_ref 数据模型设计）+ ADR 即时写；**AST 精准回归最小 POC**：(a) 解析 `.coverage` 建立"用例↔源码文件"映射表；(b) AST 解析 import 关系建立反向影响面；(c) 人为修改 5 个 src 文件输出选中比例/漏检率；(d) 记录实验数据（为 Day94 工程化打地基） | 真实执行 | 设计文档 + ADR；AST POC 实验记录文档 | [✓] |
| └ Day45 热修 | — | — | **fix（7818df7，第 123 次）**：CI Run #56 pytest 步红——POC 库外判据写死 Windows 盘符形态（Linux `split("/")[0]` 为空串恒为假），改 `PureWindowsPath`/`PurePosixPath` 双形态判定 + 跨平台守卫测试 → Run #57 全绿 | | |
| └ Day45-fix2 | — | — | **fix（f2e0649，第 124 次）**：DeepSeek 独立验收 7 项全修——**P1** 脚本含 GBK 不可编码字符（U+2194）致中文控制台 `--help` 退出码 1、报告不可用（capsys 走 UTF-8 捕获故单测测不出，已补源码级 GBK 守卫测试）；**P2** 设计文档 `cwd` 两处取值相反（3.4 代码块写批次目录、3.6 表格写项目根，照抄会让 `pythonpath=.` 失效）；**P3×5** 报告数字未随热修同步 / scripts 6 处 bare 泛型不符 mypy strict / 零覆盖统计未过滤库外文件（口径与库外诊断项不一致）/ `--tests-dir` 死参数 / `script_path` 误写为 KeyError（源码用 `.get()` 不抛异常）→ Run #58 双 job 全绿 | | |
| └ Day45 全量审查第 1 批 | — | — | **fix（第 125 次，5 项并发与队列核心）**：①**问题1 P1 worker 零延时热转**——Day44 只修了"后端构建失败"，漏了"后端已构建但连接断开"（判据 `_backend is None` 为假 → 实测 2 秒 21124 次空转 + 每轮一条 WARNING，外推 1 小时约 3800 万条，CPU 占满一核 + 磁盘写满）。改 `dequeue` 区分"队列空/故障"并按连续失败**指数退避 1/2/4/8/16/30 封顶**。②**问题2 P1 enqueue 超时双跑**——`lpush` 已生效但应答超时 → 路由起裸线程且消息仍在队列 → 同一批次跑两遍、明细双写计数翻倍。改 `SETNX tm:claim:{execution_id}` **幂等抢占**（后端不可用恒放行，不让保护机制变成可用性风险）+ `record_execution` **明细去重**作第二道防线。③**问题3 P2 BRPOP timeout 语义写反了**——实测 redis-py 5.0.8 文档 `If timeout is 0, then block indefinitely.`，原 docstring 写的"0=不阻塞立即返回"恰好相反，配置 0 会让 worker 永久卡住、`stop_event` 失效；`<=0` 一律夹到 `MIN_BRPOP_TIMEOUT_SECONDS=0.1`。④**问题4 P2 Redis 无 ack**——BRPOP 弹出即删，进程被 kill 时批次永久停在 pending；改 **BRPOPLPUSH** 落入在途 list + `ack()` 确认 + `requeue_stale()` 崩溃补偿（坏消息就地 LREM 防永动重投）。⑤**问题5 P1 benchmark flushdb 清共享库**——脚本默认 DB 与应用同为 0 号库且每场景 `flushdb()`，按默认参数跑基准会清掉应用缓存**与任务队列 list**（已入队任务静默丢失）；改默认切 **DB 5** + 共享库安全闸（`--allow-shared-db` 才放行），不采用"按前缀删"因为基准测的正是应用的真实缓存行为、key 前缀完全相同无法按前缀隔离。**另修 Day44 两条空转测试**：原实现先 `stop_event.set()` 再 `worker.run()`，循环体一次不进、dequeue 实测 0 次调用且无任何断言，改回前两天修复可静默复发；现改为真实驱动循环 + 对 `_execute_batch_async` 的负向断言。**关键设计决策**：幂等抢占放在 `_execute_batch_async` 入口（worker 消费与路由兜底线程的**共同收口点**）而非话书指定的路由层——放路由只覆盖一条路径、漏掉 worker。**12 项变异验证 12/12 全部杀死**（每项修复回退即变红），新增 38 条配套测试含 11 条容错分支覆盖，`task_queue.py` 覆盖率 99%→**满覆盖**、总体 98.64%→**98.76%** | | |
| └ Day45 全量审查第 2 批 | — | — | **fix（ba277b6，第 127 次，7 文件 +1046/-118，6 项安全与脱敏）**：**问题1 P1 生产日志配置从未生效**——全仓 `LogManager.setup(` 调用点仅 logger.py 自身 docstring、tests/conftest.py 与测试文件，**src/ 内零调用、run.py 零调用、start_worker.py 零调用**；实测 loguru 0.7.2 默认 `LOGURU_LEVEL=DEBUG`/`BACKTRACE=True`/`DIAGNOSE=True` 且导入即预置 stderr handler，于是生产**不产生任何日志文件**、TM_LOG_LEVEL 完全惰性、「diagnose=False 拦住敏感变量值打印」的屏障在生产不存在。改 `get_logger()` 惰性触发 setup()，由 `_initialized` 守卫保证只做一次；**按话书要求最后修**（脱敏先就位，否则日志一落盘就扩大泄露面）。**问题2 P1 嵌套凭据明文进日志**——`_mask_data` 的 list 分支递归、dict 分支却只看顶层键，实测 `{"user":{"password":"x"}}` 与 `{"items":[{"access_token":"y"}]}` 原样输出；改 dict/list 同等递归，带 `MAX_MASK_DEPTH=10` 深度上限与 `id()` 循环引用保护（共享子对象不误判为环）。**问题3 P1 响应体完全不过脱敏**——模块 docstring 明写「请求与响应自动脱敏记录」，但 `_safe_body` 只做 `response.text` 原样返回，而 API 返回的凭据恰恰都在响应里；改 JSON 解析后走同一套递归脱敏，**非 JSON 原样返回**（不做正则硬解，官方公告明确正则脱敏「标准用法不受影响」即误伤与漏伤并存）。**问题4 P2 TESTING 档回显 SQL 参数值**——实测 `[parameters: ('TM-SECRET-0001',1,0)]` 完整进 500 响应体，而 `test` 档常被当预发用；改只抹 `[parameters: ...]` 保留 `[SQL: ...]` 骨架，完整信息仍留服务端日志；**handle_500 与 handle_unexpected_error 两处都要改**（后者端到端可达、前者不可达需单测）。**问题5 P2 Redis URL 内嵌口令原样进日志**——口令在 userinfo 段，query 脱敏覆盖不到；改复用 `mask_url` 的 userinfo 脱敏，host/port/db 仍可见。**问题6 P3 断言失败信息直出敏感 URL 与全部响应头**——断言失败信息会进 pytest stdout 与 Allure 报告（常被归档、团队间共享，泄露寿命远长于日志文件）；改 URL 走 mask_url、响应头走 mask_headers、被断言的敏感头**实际值与期望值双侧打码**。**关键设计决策**：①新增 `src/common/security.py` 收拢三张字段表与四类脱敏原语（纯标准库、任一层可导入），三处各写一套打码逻辑正是本次漏检的根源；②**保留 `HttpClient._safe_url`/`._mask_data` 的签名与默认值作兼容入口**（既有测试用 `inspect.signature` 校验默认值并直接引用私有名，收敛实现单点性不动模块 API 面，6.33 决策）；③`get_logger()` 的惰性初始化吞 setup 异常并降级——日志是旁路能力，不可用不得让业务链路崩（同通知旁路铁律）。**14 项变异验证 14/14 全部杀死**。测试 1248→**1284**，覆盖率 98.76%→**98.83%**，6 个文件新增行覆盖满覆盖，mypy 新增行 0 错误，敏感词本批次净增 0，ruff 0 | | |
| └ Day45 全量审查第 3 批 | — | — | **fix（7e0df4c，第 128 次，5 文件 +898/-26，5 项 Web 与 SSE）**：**问题1 P1 SSE 名额可被 5 个 HEAD 请求永久占满**——名额原先**只在流式生成器的 finally 里归还**，而 Werkzeug 对 HEAD 请求直接丢弃响应体，`stream_with_context` 包装出的外层生成器停在哨兵 `yield None` 上永不推进，内层 `event_generator` 的函数体从未执行，finally 永不运行；5 个 HEAD 即可把某 IP 的 5 个名额占死，此后同一 NAT 出口的整个团队所有 `GET .../events` 一律 429 且无自愈路径。**第一版方案（登记 flask.g + `teardown_request` 兜底）实测 teardown 一次都没触发**：`stream_with_context` 为保住上下文会在视图执行期间急切 `next()` 包装生成器，对**同一个 RequestContext 二次 push**，`_cv_tokens` 长度为 2 使 `pop` 里 `clear_request = len(self._cv_tokens) == 1` 为假，钩子被整体跳过（`after_request` 虽触发但发生在响应体开始消费之前，归还即过早、配额退化成摆设）。**改法**：幂等的 `_SseSlotLease` 凭证 + 三条归还路径（生成器 finally / `response.call_on_close` / HEAD 即时归还），幂等判据从 flask.g 换成凭证自身的 `_released` 标志——**不把幂等性挂在框架提供的请求级存储上**。**问题2 P2 单流存活上界在「事件持续到达」时永不判定**——判定只写在心跳分支内，大批次下事件间隔 < 0.5s，tick 根本不触发，1800s 护闸形同虚设，而原注释恰好声称「不会漏判」与实现相反；提取 `_lifetime_exceeded()` 闭包，主循环每轮统一调用（心跳分支与真实事件分支各一处）。**问题3 P2 trigger 筛选值类型不校验**——module/priority/tags 原值透传，核心层 `_normalize_values` 对非 str/list 返 `[]` 并 warning，**筛选条件被静默忽略、实际跑全量回归**，接口仍回 202；新增 `_parse_filter_value` + `VALID_CASE_PRIORITIES`，类型与枚举不合即 400；**不做 upper() 归一**（核心层查询侧本就归一，路由层再归一会改变合法输入行为，且受理埋点会记到与调用方原值不同的字符串）。**问题4 P1 用例写操作不失效报告缓存**——报告类统计硬依赖 test_cases 表的 module/priority/status，而三者全在可更新白名单内，改用例或删用例后三类统计在 TTL（300s）内持续返回与库不一致的聚合值（聚合指标出错比明细出错更难察觉）；新增 `_invalidate_case_related_caches` 同时清两个前缀 + CLI 批次执行路径补失效。**问题5 P2 日志注入**——`request.path` 按 PEP 3333 已被 URL 解码，`%0d%0a` 变成真实换行符，可凭空伪造一整行审计记录；新增 `src/web/utils.py` 的 `sanitize_log_field`（控制字符折叠 + 200 字截断标记），话书建议的 `src/common/security.py` 本批禁改故落在 `src/web/utils.py`；`cases.py` 不在白名单，其私有 `_sanitize_log_field` 保持原样，收敛留待后续批次。**关键决策与意外收获**：①**测试判据加强时当场逮到一个真漏网点**——只单行化了 `before_request` 的「请求:」日志，`after_request` 的「响应:」配对日志仍在写原始 path，凭空多出一行伪造的「响应:」记录（两处配对要求 method+path 完全一致，只改一侧比不改更隐蔽），补上后才有 M5c 这条变异；②`_parse_filter_value` 一度带 `allow_multi` 参数但三个调用点全为 True，该分支是**永不可达的死代码**，连同覆盖率一起清掉（fix2 已因死参数修过一次）；③`tags="smoke"` 这类裸字符串**不是非法输入**（核心层 str/list 通吃），判它非法等于凭空收紧合法输入，测试判据同步纠正；④`@stream_with_context` 装饰函数时 Flask 类型桩把返回值一律标成 Iterator，导致 `event_generator()` 报 `"Iterator[str]" not callable`，改用官方文档的另一形态 `stream_with_context(event_generator())` 后类型与运行时都诚实。**12 项变异验证 12/12 全部杀死**（M2b 靠挂死/超时杀死——心跳护闸一失效，空闲流永不收流，正是该 bug 的本质）。测试 1284→**1318**，覆盖率 98.83%→**99.03%**，`executions.py` 与新建 `utils.py` 满覆盖，mypy 新增行 0 错误（全量 134→132），敏感词本批次净增 0，ruff 0 | | |
| └ Day45 全量审查第 4 批 | — | — | **fix（2cdc19b，第 129 次，5 文件 +1004/-58，4 项测试与数据）**：**问题1 P1 常量断言冒充行为覆盖**——`tests/test_day44_bugfix_demo.py` 里 `assert SSE_MAX_LIFETIME_SECONDS > 0` 与 `assert notif.MAX_SEND_BUDGET_SECONDS == 30.0` 把常量改成 1e-9（等效关闭）测试照样过，而 `notification.py` 的预算耗尽提前收尾分支**实测 0 覆盖**。本批补齐真实行为测试：通知预算耗尽→第 2 次尝试即收尾且退避被夹到剩余预算、预算充足→走满 1+max_retries、SSE 存活上限到期后**并发名额被归还**（第 3 批没覆盖的角度：finally 走 `return` 提前退出而非正常收流）；**SSE 半边其实已由第 3 批 P2-2 的两条行为测试覆盖，本批不重复**。**问题2 P1 事件总线历史环淘汰后静默丢事件**——docstring 承诺"超出回放窗口的客户端由路由层走数据库重建分支兜底"，而该分支以 `status in ("finished","failed")` 为前提，**运行中批次根本走不到**；批次超 999 条用例时重连静默丢约 950 条 `case_finished`，且 event_id 连续递增看起来完全正常，排障会误判为"用例没被执行"。改法：新增 `EventChannel.detect_gap()` 判定缺口，`subscribe()` 首帧 yield 一帧 `stream_reset`（携带 `missing_from`/`missing_to`/`missing_count`），路由层成帧并打运维告警。**`stream_reset` 刻意不进 `VALID_EVENT_TYPES`**——那是 `publish` 的入参白名单，而该事件由 subscribe 合成、不经过 publish 校验，放进去等于允许外部伪造不占 event_id 的伪事件。**未实现"运行中批次也做 DB 重建"**（话术标为可选）：运行中批次的 DB 快照没有终态事件、且帧 id 属 `db:` 体系，与实时流的 `live:` 体系混用会破坏断点过滤，改为让客户端拿显式信号自行决策。**问题3 P2 failed 分支只改状态不聚合**——第 k 条用例抛异常时前 k-1 条明细已落库、`defect_statistics` 却是零行，而 `list_executions_paged` 只读该表，**批次从列表彻底消失**、`get_execution_status` 却显示 failed+全 0。提取 `_aggregate_execution_results`（无记录返回 None 由调用方裁决），finished 与 failed 两条路径共用；failed 分支另把真实计数回写批次行（该接口直查批次行、不聚合），首条即崩溃时正常降级不打断收尾。**问题4 P3 批次号 strip 口径不统一**——实测**只有 `record_execution` 做了归一**，`finish_execution`/`get_execution_detail`/`get_execution_status` 只校验非空却仍用带空白的原值查询，`_update_batch_status`/`build_notification_statistics` 连校验都没有：带空白批次号能让状态更新静默丢失（warning + None）、明细按干净号落库而汇总查不到，最终 `finish_execution` 抛"批次不存在"、整批判 failed。提取 `_normalize_batch_id` 并在 **6 个落库入口**统一调用；`build_notification_statistics` 只 strip 不抛（其契约是"异常: 无"，空白号与"无记录"同义返回 None）。**关键设计决策**：①同一口径散落多处即多处漂移，与 7.45 同源，故一律提取为单一函数而非逐处补 strip；②`_aggregate_execution_results` 用"返回 None"而非抛错区分两种调用方，避免 finished 路径的"批次不存在"契约被 failed 路径的降级需求污染。**11 项变异验证 11/11 全部杀死**（首轮 M2c 存活暴露一个真发现：`stream_reset` 的 `event_id` 为 None，通用渲染路径本就会正确省略 id 行，路由层特判分支对"帧长什么样"毫无影响、只负责打 WARNING——故把该分支的判据改为断言运维告警日志，让它名副其实）。测试 1318→**1337**，覆盖率 99.03%→**99.09%**，`event_bus.py`/`notification.py`/`executions.py` 均达满覆盖，mypy 新增行 0 错误（全量 132→131），敏感词本批次净增 0，密钥扫描 0 命中，ruff 0 | | |
| Day46 | 10/06（二） | W8 | **source_ref 字段改造 Day1**：ORM 模型新增字段 + 迁移脚本 | 数据模型 | 模型+迁移~200 行 | [✓] |
| └ Day46-fix | — | — | **fix（fe78c9b，第 131 次，4 文件 +339/-13）**：**P1-1 备份被跳过时迁移失败无回滚（数据丢失路径）**——原 `except` 分支的回滚条件只有 `if backup_path is not None`，而幂等窗口内重复迁移会跳过备份（`backup_path=None`），此时写回失败则**整库已被 DROP 重建为空且不回滚**，更糟的是异常消息仍写"已保持或恢复迁移前状态"，排障会被直接误导。修法：新增 `_find_latest_backup()` 回退到备份目录中最新一份**可用**备份（按文件名时间戳倒序 + `_is_usable_backup()` 只读打开校验 SQLite 文件头**且至少含一张表**，最新的损坏则自动退到次新的），恢复源优先级改为 本次备份 > 最近历史备份 > 无（明说需人工恢复），并把消息按三种真实结果分支措辞；`_restore_outcome()` 连"恢复动作本身失败"也如实报出，不再让上层以为已回滚而放过空库。**补 7 条回归测试**（跳过备份后失败从历史备份恢复且数据回到备份时刻的 N 条而非 0、当前备份消息措辞、无可用备份消息措辞、损坏文件跳过、空库/损坏库判不可用、恢复失败如实上报、文件名不可解析返回 None）。**P2-1 字段类型口径定稿 String(512)**——`src/db/models.py` 用的是 String(512)，而 ADR-001 与设计文档写的是 Text，三处漂移；本批按 Day46 话术定稿为 String(512)（512 对正常路径绰绰有余、MySQL 严格模式有长度校验可提前拦截异常超长输入、录入侧 Day47 加长度校验兜底），ADR-001 与设计文档同步改写并补论证表，同时注明 **SQLite 不实现 VARCHAR 长度约束、该列在 SQLite 上不做拦截**这一实测边界。**`src/db/models.py` 零改动**（代码已是 String(512)）。测试 1363→**1370**，覆盖率 99.04%→**99.11%**，`migration.py` 99%，ruff 0，mypy 新增行 0 错误。**Run #66 双 job 全绿**（3.11 六步 + 3.12 探测位均 success）。新增 7.51 坑 | | |
| Day47 | 10/07（三） | W8 | **source_ref 改造 Day2**：存量回填 + 用例录入方式改造（YAML/Excel 新增列） | 数据模型 | 回填脚本~250 行 | [✓] |
| └ Day47 | — | — | **feat（cfa3aac，第 132 次，10 文件 +1918/-42）**：新建 `src/scripts/backfill_source_ref.py`（490 行，三档 A 自动提取/B 人工补录/C 模拟执行专用，**判定顺序 A→C→B**、幂等靠 `UPDATE ... WHERE source_ref IS NULL`、**只写 source_ref 一列**、`--dry-run`/`--limit`/`--project-root`、缺列提示先跑迁移并退 1）；`case_manager.py` 新增 **core 层单一校验 `validate_source_ref`**（形态 `^[\w./-]+\.py(::\w+)*$` + ≤512 + 禁 `..`）并接入 YAML 整批预校验/Excel 列表头/创建/更新，`UPDATABLE_CASE_FIELDS` 加该字段；`routes/cases.py` 两 Schema 加可选 `source_ref`（不传即 None，非法 400）；`executors.py` 的 `build_command` 改为 **source_ref 优先 → script_path 过渡兼容（打 WARNING）→ 皆空抛 ValueError，绝不回落 case_id**，`run_one` 捕获降级为该条 error 不上抛。**因行为变更适配 13 条既有断言（不删测试），其中 6 条原本假通过**（`assert result == "error"` 被新的异常降级路径满足而真实分支未执行，已补消息子串校验）。**新增 40 条测试**，基线 1370→**1410**（0 failed/0 warning），覆盖率 99.11%→**98.94%**（新文件 97%），ruff 0，mypy 新增行 0 错误，**Run #67 双 job 全绿**（一次通过零热修）。**留 Day48 前置缺口**：`_to_dict` 未暴露 source_ref，真实执行前须先补 | | |
| └ Day47-fix | — | — | **fix（b253a90，第 133 次，9 文件 +592/-87）**：**P2-1 测试写脏真实库（根因比"缺隔离 fixture"深两层）**——任务指令指向"补隔离 fixture"，实测发现补了也拦不住，逐层挖出两个真根因：①**后台线程活过用例边界**：本项目执行编排用 daemon 线程跑批次，用例一结束 `monkeypatch` 就把 `TM_DB_SQLITE_PATH` 还原成 `.env` 里的真实库路径，而线程此时若调 `get_engine()`（已被 reset 清空）会**按还原后的环境变量重建引擎**写进真实库——该文件单独连跑两次两次都漏（test_executions +1、+3），不是偶发；②**`patch.object(env_manager, "get")` 整体替换配置访问器**：替身对表外的键返回 `default`，而 `TM_DB_SQLITE_PATH` 的 default 恰是 `output/testmatrix.db`，于是隔离环境变量在替身生效期间**完全失效**（`NotificationRouter()` 未注入仓储时回落真实 `NotificationHistoryRepository`，实测 notification_history 1222→1228、notification_dead_letters 173→174）。**修法**：①`tests/conftest.py` 加**会话级** autouse fixture 把默认路径钉到 `tmp_path_factory` 临时目录、**整场会话不还原**——从"逐用例隔离"升级为"整场会话绝不触碰真实库"，前者防不住线程泄漏、后者防得住（各文件自己的 fixture 仍可覆盖）；②router 测试的 `_mock_get` 对 `TM_DB_TYPE`/`TM_DB_SQLITE_PATH` 放行真实实现（这类"全局替换配置访问器"的替身会连无关子系统的配置一起屏蔽）。**P3-1** C 档关键词补 `description`（原注释写 module/name/description、实现只有前两个；存量用例常把接口性质写描述里，实测 dev 库大量 `标签: smoke, api`，漏扫会被误报成"待人工补录"）。**P3-2** 候选提取后 `replace("\\","/")` 归一——候选正则含 `\` 但复验正则不含，Windows 风格路径原本**静默跳过**（比报错更难发现）。**P3-3 执行侧纵深防御**（方案 A）：`validate_source_ref` 下沉到 `src/common/source_ref.py`（`case_manager` 反向依赖 `executors`，就地定义会循环导入；用 `X as X` 幂等再导出保持对外 API 面零变化），`build_command` 对非空 `source_ref` 也校验，`../secret/x.py` 与含空格值**在进命令前被拒**。**P3-4** Excel `source_ref` 支持 `__CLEAR__` 显式清空（DataDriver 只收非 None 单元格，空单元格与"没这列"不可区分，此前 Excel 侧无法清空而 YAML/API 可以）。**P3-5** 校验失败消息补用例 name：`用例 {case_id}（{name}）: ...`，name 空则退化为仅编号。**连带适配 2 条旧断言**：Day43/44 的 `--version` 用例原断言"落在 `--` 之后"，加格式校验后它**在拼命令前就被拒**——已改为断言"拼不出命令"（终止符只是不让它被当选项、值仍会当路径传；格式校验才是真正挡在门外，防护层次前移）。**测试 1410→1424**（新增 14 条），覆盖率 98.94%→**98.96%**（新文件 `source_ref.py` 100%、`executors.py` 100%），ruff 0，mypy 新增行 0 错误。**P2-1 实测验收**：全量 pytest 前后 `output/testmatrix.db` 的 size / mtime_ns / 各表行数**完全不变**（mtime_ns 逐纳秒一致） | | |
| Day48 | 10/08（四） | W8 | PytestRunner 执行器封装（subprocess+超时控制+输出捕获） | 真实执行 | 执行器~400 行+8 条测试 | [✓]（**feat（6a9a4e4，第 134 次，10 文件 +650/-98）**：**任务一 补 Day47 前置缺口**——`case_manager._to_dict` 返回字典加入 `"source_ref": row.source_ref`（位置在 `description` 之后 `creator` 之前），该方法是"模型行→用例字典"的**唯一**出口（列表/详情/创建响应 + `select_cases_for_execution` → `_execute_batch_async` 逐条 `run_one` 全部经它），键缺失比值为 None 更危险，故**无条件输出该键**（未配置时序列化为 JSON `null`，不裁剪）；连带同步 2 处精确字段集断言（`test_web_cases_crud_demo` / `test_web_cases_list_demo`）与 `docs/API.md`（`source_ref` 此前在文档中**完全缺失**，已补 POST/PUT 请求表 2 行 + 4 处响应示例）。**任务二 PytestRunner 完善 + 移除 script_path 过渡**：`__init__` 新增 `timeout`/`cwd`/`env` 三项可配置项——timeout 三级优先级（构造参数 > `TM_PYTEST_TIMEOUT` > 模块常量），**非法值 0/-1/`True`/非数字一律显式抛错而非静默回落**（静默回落会让"我明明把预算调到 5 秒"却仍按 30 秒跑，而超时消息里的阈值看起来又是合法的，排查时完全看不出配置没生效）；`cwd` 默认取 `PROJECT_ROOT`（复用 `src/common/env_manager.py` 既有常量，**非可选装饰项**：Web 触发与队列 worker 的 cwd 与项目根无关，不显式指定则 source_ref 里的相对路径全部找不到、每条以退出码 4 落 error）；`env` 默认传 `None` 让 subprocess 直接继承，注入时合并 `{**os.environ, **env}`。移除 `build_command` 的 `script_path` 过渡分支，只读 `source_ref`、为空即抛 ValueError。**未新增 `ExecutionResult` 字段**（避免动数据库 schema），未动 `SimulatedExecutor`。**任务三 新建 `tests/test_pytest_runner.py` 8 条**，全部 monkeypatch `subprocess.run` 不真启子进程（真跑子进程是 Day44 用例的职责，两者互补非重复）。**白名单冲突（事前上报并获批准扩容 6→10）**：移除过渡分支会打挂 **4 个白名单外历史文件的 7 条既有测试**（`test_day44_bugfix_demo` 4 条含 3 条真实子进程、`test_day43_closing_demo`、`test_day47_source_ref_backfill_demo`、`test_stage_c_support_coverage_demo`），按铁律只能改断言不能删除；其中 3 条真实子进程用例有个隐蔽坑——原用 `str(tmp_path)`，而 `source_ref` 须过 `validate_source_ref` 形态校验（禁盘符/反斜杠/`..`），**Windows 绝对临时路径过不了**，只把 `script_path=` 换成 `source_ref=` 会让用例在拼装阶段降级为 error 而"通过"、真实子进程根本没跑，恰好丢掉它们唯一要证明的事；已改为在 `output/`（已 gitignore）下建临时文件、用项目相对路径，fixture try/finally 清理。**测试 1424→1432**，覆盖率 98.96%→**98.97%**（4741 语句/49 未覆盖），`executors.py` **100%**、`case_manager.py` 98%，ruff 0，mypy 新增 165 行 0 错误（101 errors 全为存量，已用脚本把报错行与 diff 新增行求交验证为 0），三连跑零 flaky（新文件 8 passed ×3、适配文件 181 passed ×3），敏感词与密钥扫描零命中。**Run #69 双 job 全绿**（3.11 四步 + 3.12 探测位均 success，一次通过零热修） | | |
| └ Day48-fix | — | — | **fix（beec41d，第 135 次，3 文件 +101/-3）**：DeepSeek 独立验收 6 个 P3 全修。**P3-1** `test_web_cases_crud_demo` docstring "_to_dict全量11字段" 改 12（source_ref 已 Day48 纳入）。**P3-2** `test_run_one_passed` 耗时断言消息与实际校验口径不符（消息写"非硬编码 0"而断言是 `>= 0.0`）——按方案 A 只改消息为"耗时为非负测量值"、不动断言逻辑：mock 的 subprocess.run 极快，写死 `> 0.0` 会引入时钟分辨率相关的 flaky 风险，"确实执行过而非短路返回"由 Day44 三条**真实子进程**用例以 `duration > 0.0` 兜底，两者互补非重复。**P3-3** `_build_env` 的"未注入返回 None"无断言钉住——改成"总是返回环境拷贝"是**等价变异**（子进程所见环境相同、不报错），只能靠断言守，在 `test_run_one_failed` 追加 `kwargs["env"] is None`。**P3-4** 超时消息/日志含 venv 解释器绝对路径，**决策保留不脱敏**（本地自托管工具的解释器路径既非凭据也不含业务数据，而超时恰是必须复现才能定位的故障，裁掉命令行保全收益为零），仅在超时分支加注释说明理由与"将来改多租户需重评"的边界。**P3-5** `_resolve_timeout` docstring 明示空串/纯空白视为"未配置"回落默认常量不抛错（容器与 CI 常把未设置变量渲染成空串，报错是噪声而非信息）。**P3-6** `__init__` 补 `cwd`/`env` 显式类型校验（此前只有 timeout 有业务级校验，报错风格两种；`Path(cwd)`/`dict(env)` 的标准库消息读不出"是我把 cwd 传成了 int"），**额外补两条合法类型正向断言**（`cwd="sub/dir"` 归一为 `Path`、`env={}` 与 `None` 的区别）——只测非法分支时校验条件写反成"合法即拒"测试照样绿。测试 1432 不变，**1432 passed / 0 failed / 0 warning**，覆盖率 98.96%→**98.97%**（`executors.py` **100%**），ruff 0，mypy 新增 51 行 0 错误，串行三连跑 8 passed ×3，**Run #70 双 job 全绿**（一次通过零热修） | | |
| └ Day48-fix2 | — | — | **fix（aa4fe88，第 136 次，2 文件 +196/-3）**：**P2 异常逃逸修复**——`PytestRunner.run_one` 只捕 `TimeoutExpired` 与 `OSError`，而 `subprocess.run(env=...)` 要求环境块**全为 str**，非 str 抛 `TypeError`、env 键含 `=` 抛 `ValueError`、键值含 NUL 抛 `ValueError`、cwd 含 NUL 抛 `ValueError`，**这些都在真正创建进程之前抛出、不属既有 except 链，直接上抛穿透 run_one**，违反 docstring"异常: 无"与"单条用例失败不升级为整批 failed"（一条用例环境变量配错会让整批判 failed 且丢掉真因）。**本机实测复现 7 种逃逸面并修正任务书一处描述偏差**：空串 env 键在 Windows 上抛的是 `OSError`（WinError 87）而非 `ValueError`（原本就已被 `except OSError` 捕获、不构成逃逸），但该行为跨平台不一致（Linux 判为非法变量名），故仍在构造期统一拒绝。**两层修复**：①构造期新增 `_validate_env_content` 逐键值校验（键/值非 str 抛 `TypeError`、键空串或含 `=` 抛 `ValueError`，消息带变量名与实际类型；**bytes 同样拒**——Windows CreateProcess 环境块只接受 str，放行只是把失败从构造期推迟到调用期）；②`run_one` 第二个 try 块追加 `except (TypeError, ValueError)` 降级为该条 `error` 并保留标准库原文。**明确否决方案 C（自动 `str()` 转换）**：`None → "None"`、`True → "True"` 是静默语义篡改，配置错误被伪装成正常值，与 Day48 确立的"非法配置显式抛错"冲突。**两层都留的理由**：构造期把"自己写的"配置错误挡在最早处且报错带变量名；兜底层则因为 `self.env` 构造后可变（Day49 接入批次级 env 注入时极可能绕过构造函数），而"单条用例失败绝不升级为整批 failed"是 run_one 的硬契约。**测试**：既有校验用例追加 6 组内容校验（值 int/None/True/float/bytes/list、键非 str、键含 `=`、键空串、合法边界含空格/中文变量名）+ 新增 `test_run_one_type_error_degraded`（3 种异常变体）与 `test_run_one_degrades_when_env_mutated_after_construction`（绕过构造期直接改 `self.env`，证明兜底层独立生效）共 **2 个新测试函数**。**除 mock 外另用真实 subprocess 实测验证**：4 种坏 env 全部返回 `result=error` 且零逃逸。测试 1432→**1434**，覆盖率 **98.97%** 持平（4764 语句/49 未覆盖），`executors.py` **100%**（126→145 语句，0 未覆盖），ruff 0，mypy 新增 115 行 0 错误，**全程严格串行跑 pytest**（遵守 7.52 铁律），三连跑 10 passed ×3，**Run #71 双 job 全绿**（一次通过零热修） | | |
| Day49 | 10/09（五） | W8 | 执行器稳定性（进程超时杀死/僵尸进程清理/退出码解析）+ pytest 参数化执行（-m/-k/目录选择透传） | 真实执行 | 稳定性~300 行+8 条测试 | [ ] |
| Day50 | 10/10（六） | W8 | 输出解析（pytest 终端输出→结构化结果+实时进度解析）+ Allure 结果桥接（结果目录隔离→ReportAnalyzer→入库） | 真实执行 | 解析器+桥接~400 行+8 条测试 | [ ] |
| Day51 | 10/11（日） | W8 | 执行状态回写（运行中/完成/失败状态机+批次进度）+ **任务队列 ACK/requeue 健壮性**：(a) BRPOP 后写 pending 标记"执行中"；(b) 正常完成后删 pending 写终态；(c) worker 崩溃时扫描超时任务重新入队；(d) pending 带 worker_id+心跳，防止单任务被两 worker 重复执行 | 真实执行 | 状态机+ACK机制~400 行+8 条测试 | [ ] |
| Day52 | 10/12（一） | W9 | **验收门禁（硬性）**：`TM_EXECUTOR=pytest` 真跑一条 `assert True` 通过 + 结果入库 + Web 可见；**追加验收**：kill -9 模拟 worker 崩溃，pending 任务回 pending 并重新消费，同一任务 DB 中只产生一条记录（唯一键幂等，键粒度为 (batch_id, case_id)）；并发场景下单任务不被两 worker 同时消费（pending+心跳机制）；**真 assert False 全链路**：验证真实 traceback 能从 pytest→报告解析→DB→Web（37 天来所有"失败"都是模拟器造的字符串，报告解析器从未处理过真实 traceback） | 真实执行 | **门禁通过才进联调** | [ ] |
| Day53 | 10/13（二） | W9 | 全链路联调（Web 触发→**真实执行**→入库→看板刷新）+ SSE 真实输出流式展示（subprocess 输出→SSE 推送）+ 真实/模拟双模式切换 UI+API；**顺手：Dashboard 通过率趋势图加 clamp 防御（`Math.min(100, Math.max(0, rate))`），防历史脏数据或异常值产生越界点（如 4033%），展示层不出现物理不可能的值** | 联调 | 全链路跑通+流式推送~350 行 | [ ] |
| Day54 | 10/14（三） | W9 | 联调 bug 修复 + 表单校验与错误提示打磨 + 加载/空/异常三类状态 UX + 浏览器控制台零报错 + 复盘日：执行器 Phase-1 回顾+讲述材料第 6 次 | 联调/复盘 | 零控制台报错+联调报告+讲述材料 | [ ] |

> **Day52 验收门禁（硬性）**：未通过则停下联调，继续补执行器，直到门禁通过为止。此门禁用于杜绝"联调的其实是模拟链路"。

> **B 方案说明**：原计划独立联调 8 天（Day62-69）并入 Phase-1，压缩为 2 天（Day53-54）。执行器已在手上，联调不需要反复对齐假数据。

### 阶段四：真实执行器 Phase-2 深水区（Day55-64，10天）

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day55 | 10/15（四） | W9 | pytest 钩子进阶（`pytest_collection_modifyitems` 定制 + conftest 增强） | 真实执行 | 钩子应用~300 行 | [ ] |
| Day56 | 10/16（五） | W9 | 自定义 pytest 插件开发（平台专用：结果上报/日志钩子，pytest11 entry point） | 真实执行 | 插件~400 行+8 条测试 | [ ] |
| Day57 | 10/17（六） | W9 | 插件本地打包验证 + 补测 + ADR（插件 vs 钩子的选型取舍） | 真实执行 | 验证通过 + ADR | [ ] |
| Day58 | 10/18（日） | W9 | xdist 并发执行接入（-n 参数 + 结果合并） | 真实执行 | 并发接入~300 行 | [ ] |
| Day59 | 10/19（一） | W10 | 并发结果合并正确性验证（乱序结果按用例归位）+ **写库冲突处理 Day1**：多 worker 写 `record_execution` 的 SQLite 锁/MySQL 竞态排查 | 真实执行 | 验证~10 条测试+竞态分析报告 | [ ] |
| Day60 | 10/20（二） | W10 | **写库冲突处理 Day2** + 幂等设计与并发批次调度（资源竞争/锁/队列）+ **QueueBackend 窄协议收口**（只收口 enqueue/dequeue/ack/health 4 个多态方法，定义 QueueBackend Protocol，三实现：RedisQueueBackend / InMemoryBackend 用现有 fake:// / ThreadFallback 把调用方裸线程兜底收进来；Redis 专属能力 claim 幂等抢占/requeue_stale/TTL 保留在实现类不进 Protocol；L866 claim() 后端不可用时恒返回 True 的 fail-open 边界原样保留，由契约测试覆盖）+ **xdist 多 worker 幂等对账**（结果按用例归位、无重复、无丢失、计数一致，1/2/4/8 曲线在 Day64 报告产出）。验收（行为断言非结构断言）：①调用方不再出现自己 if redis挂了:起裸线程 的分支，改由统一接口+health 路由；②三后端契约测试按操作分组——enqueue/dequeue/ack 三后端行为一致，claim 单独成类只断言 Redis 双跑防护有效+后端不可用时 claim 必须返回 True 且任务仍被执行（fail-open 回归护栏，必须新增测试）；③现有 1434 测试零回归、claim 双跑防护测试仍绿；④不新增第三方依赖。溢出降级：若当天主任务吃紧，收口可降级为文档注记，但一致性测试不可省；真做不完溢出进 Day96-100 缓冲，不占 Day61 | 真实执行 | 幂等+调度~400 行+8 条测试 | [ ] |
| Day61 | 10/21（三） | W10 | 执行稳定性（超时重试/异常恢复/中断恢复） | 真实执行 | 稳定性~300 行+8 条测试 | [ ] |
| Day62 | 10/22（四） | W10 | 真实执行全链路集成测试（触发→执行→解析→入库→看板） | 真实执行 | 集成测试~10 条 | [ ] |
| Day63 | 10/23（五） | W10 | **全量基线测量**（测量协议：同机/同数据集/重复 3 次取中位/脚本固化进 `scripts/`）+ **绝对规模北极星测量**：实验 A（1434 真实集成用例集）护栏——4 worker 吞吐 ≥300 用例/分钟、平台开销 ≤裸 pytest 直跑 ×1.15（防"拿测试自身速度邀功"）、核心写接口 P95 ≤200ms（SQLite 单机，测量时注明系统状态：空载/批次执行中）；实验 B（1 万条合成轻量用例集）护栏——端到端 ≤12 分钟，必须注明扩增集构成（mock 占比、不含真实网络），禁止对外暗示"1 万真实业务用例"；两个实验独立测量、独立报告，不互相推导（数据集构成写进测量协议正文）。改进规则：先测真实基线 B，靶值 = max(护栏, ⌈B×1.25⌉)；回归报警线 B×0.8（低于即异常）。不达标处理：Day64 报告给根因+排 Day96-100 缓冲优化，禁止下调护栏或改扩增集凑数 | 真实执行 | 基线数据+测量脚本+北极星数据 | [ ] |
| Day64 | 10/24（六） | W10 | **并发优化量化对比报告**（全量基线→并发优化对比，含写库竞态处理 + **xdist 并发写锁曲线**：并发数 1/2/4/8 下的吞吐量与锁等待时间对比）+ **绝对规模表**（护栏/实测/提升倍数三列）+ **多 worker 吞吐曲线**（1/2/4/8，与既有 xdist 写锁曲线并列，沉淀为博客 #2 图表素材）+ 接入文档 + 复盘日：执行器 Phase-2 回顾+讲述材料第 7 次 | 真实执行 | 对比报告（含写锁曲线+绝对规模表）+文档+讲述材料 | [ ] |

### 阶段五：DevOps（Day65-78，14天）

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day65 | 10/25（日） | W10 | **SQLite 并发加固**：(a) 连接加 `busy_timeout=30000ms`；(b) 启用 WAL（`PRAGMA journal_mode=WAL`）；(c) 评估模块级写锁 + Dockerfile 多阶段构建 + 镜像瘦身 + .dockerignore | Docker | 5 批次并发无 database is locked；优化 Dockerfile | [ ] |
| Day66 | 10/26（一） | W11 | docker-compose 编排（Web+MySQL+**Redis**）+healthcheck+依赖顺序 | Docker | compose 三服务配置 | [ ] |
| Day67 | 10/27（二） | W11 | MySQL 容器配置（持久化卷+字符集+初始化 SQL）+ start.bat/start.sh 一键启动 + 预置示例数据 | Docker | MySQL 配置+启动脚本 | [ ] |
| Day68 | 10/28（三） | W11 | 容器全链路验证（Docker 内跑通**真实执行**→报告→看板）+ **容器内复测对账**（可选，零成本：容器内 xdist -n4 对账数字与宿主 Day60 结论一致，不建新任务，有余量则做；叫"环境复验"——同一 xdist 能力的稳健性检查，不是新增多 worker 对账）+ 部署文档 | Docker | 验证记录+部署文档 | [ ] |
| Day69 | 10/29（四） | W11 | Jenkinsfile 流水线（拉代码→装依赖→跑用例）+ Allure 报告生成归档 + 构建结果通知集成 | Jenkins | Jenkinsfile+报告归档 | [ ] |
| Day70 | 10/30（五） | W11 | 定时构建 + 参数化构建（筛选参数透传 run_batch）+ 流水线本地模拟验证 + 问题修复 | Jenkins | 参数化配置+验证通过 | [ ] |
| Day71 | 10/31（六） | W11 | **半天流水线可靠性加固**（上午）：①失败重试策略（防 CI 抖动误报红）②构建超时（防挂死占节点）③磁盘治理（工作区清理+产物保留轮转，合并为一项）④凭据管理（配置检查，约 15-20 分钟：Jenkinsfile 禁硬编码长期 token，用 credentials binding；不展开成模块）+ **半天 ADR/文档收口**（下午）：Jenkins vs GitHub Actions 分工 ADR（保留，高价值选型文档）+ 流水线架构图 + 使用指南收口。明确砍掉：节点标签调度（多节点是分布式暗示，与单机定位冲突，且无多节点可测） | Jenkins | 加固配置+ADR+文档 | [ ] |
| Day72 | 11/01（日） | W11 | k6 核心接口压测脚本 + p95 阈值断言 | k6 | k6 脚本 | [ ] |
| Day73 | 11/02（一） | W12 | k6 性能基线数据（量化）+ 压测报告 | k6 | 基线数据+报告 | [ ] |
| Day74 | 11/03（二） | W12 | **Python 升级：3.11 → 当日最新稳定（预期 3.14/3.15，按当日实际发布与关键依赖（SQLAlchemy/Redis/PyMySQL）wheel可用性确认；下限不低于3.14）**——同步处理 telnetlib（3.13 已移除标准库 telnetlib：telnet_client 改用 telnetlib3 或 socket 自实现；现有代码已内置 3.13+ 降级指引）；**CI 矩阵扩至 ≥3 版本（主版本 + 两个探测版本）**。另：**pytest 升级到当日最新稳定（预期 9.x；按 allure-pytest/xdist/cov/rerunfailures 兼容矩阵确认，个别插件滞后则先 8.4 并记 backlog）**（含 pytest-cov 升到当日最新稳定、pytest-rerunfailures 升到当日最新稳定）+ CI 矩阵分支验证，**绿了才合**；**升级当天必须复跑 `pytest --collect-only` 并同步 ci.yml 的 `TEST_COUNT_BASELINE`（大版本升级可能改变收集语义，零余量基线需同步校准）；若收集计数变化，优先回滚 pytest 版本排查根因，不得直接调大 baseline 凑绿——除非确认是收集语义正向变化且有代码改动同步支撑** | 版本升级 | Python/pytest 升级通过+回归绿 | [ ] |
| Day75 | 11/04（三） | W12 | **Flask 3.x 升级** + 同链升 Werkzeug≥3.1、Jinja2、itsdangerous、blinker + CI 矩阵验证，**绿了才合** | 版本升级 | 升级通过+回归绿 | [ ] |
| Day76 | 11/05（四） | W12 | 【共同休息日，任务按协调#8⑦吸收】**三项升级（Python/pytest/Flask）联合回归测试** + 升级对比记录（性能/兼容性） | 版本升级 | 回归绿+对比记录 | [ ] |
| Day77 | 11/06（五） | W12 | DevOps 阶段问题修复 + 双流水线（功能回归+性能基线）确认 | DevOps | 修复+双流水线确认 | [ ] |
| Day78 | 11/07（六） | W12 | 复盘日：DevOps 阶段回顾 + 量化数据整理 + 讲述材料第 8 次 | 复盘 | 讲述材料+量化数据+独立自讲 | [ ] |

> **升级窗口说明（Day74-76）**：三项升级同窗完成——Day74 Python（含 telnetlib 替代，CI 矩阵扩至 ≥3 版本）+ pytest；Day75 Flask；Day76 联合回归+对比记录。**排不下时优先级：Python（含 telnetlib；3.11 于 2027-10 EOL，不可砍）＞ pytest ＞ Flask（可顺延至缓冲日 Day109-114 并记 backlog）**。升级后同步 README/PROJECT_SPEC 技术栈行为支持范围格式（如「支持 Python 3.11–3.1x」）+ CI 徽章。

### 阶段六：质量打磨（Day79-84，6天）

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day79 | 11/08（日） | W12 | 后端性能优化（慢查询/N+1 排查/索引核对+对比数据）+ 前端性能优化（静态资源/接口响应/图表按需加载） | 质量打磨 | 优化记录+对比数据 | [ ] |
| Day80 | 11/09（一） | W13 | 代码质量（类型标注补全+PEP8 全量（ruff）+复杂函数拆分）+ **覆盖率缺口补齐（口径见 §一 质量基准线）** | 质量打磨 | 质量整改 | [ ] |
| Day81 | 11/10（二） | W13 | 安全加固 Day1：secrets 不落库、不落日志（日志脱敏）+ Web API SQL 注入与越权防护 | 安全加固 | 脱敏改造+防护改造+用例 | [ ] |
| Day82 | 11/11（三） | W13 | 安全加固 Day2：**subprocess 命令注入防护**（命令白名单）+ 依赖漏洞扫描（pip-audit 复跑） | 安全加固 | 注入防护+扫描报告 | [ ] |
| Day83 | 11/12（四） | W13 | 边界/异常/并发场景补测（空数据/非法参数/超长字段/DB 异常/网络超时/Redis 断连降级/多批次并发）+ 回归 bug 修复 | 全量回归 | 补测~20 条+修复 | [ ] |
| Day84 | 11/13（五） | W13 | 真实场景验证：拿平台跑 TestMatrix 自身测试套件（真实数据入库）+ Dashboard 数据真实化+截图+质量报告（覆盖率/复杂度/安全扫描汇总）+ 复盘日：质量打磨回顾+讲述材料第 9 次 | 真实场景/复盘 | 真实执行记录+质量报告+讲述材料 | [ ] |

> **B 方案说明**：原计划质量打磨 8 天+全量回归 13 天=21 天，压缩为 6 天。边界/异常/并发补测并入质量打磨，纯完善性的"全量回归"降级为机动缓冲阶段的靶向补强。

### 阶段七：深度重构（Day85-89，5天）

> **高风险阶段**：拆分 2438 行的 `case_manager.py`，必须在 CI + 全量回归（以当日 CI 收集基线为准，当前 1434 条）绿的前提下开工，**每拆一层一个 commit**，保留重构痕迹。
>
> **Day85 Go/No-Go 检查点**：Day85 开工前先跑一次"拆分后签名不变的机械拆分"（只搬函数不改逻辑），如果这一步就红，说明全量回归测试对内部结构耦合很深，**立刻启用降级预案，不要试 3 天再降**。
>
> **重构降级预案**：Day87 结束若全量回归不绿 → 冻结 case_manager 逻辑重写（回退当日 commit），只保留 notification 搬迁 / report_analyzer 消重两个低风险拆分；batch_runner 拆段与 marshmallow 改造进 backlog，不占用 Day88-89。重构不可中断——降级预案在开工前定好，不现场决定。

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day85 | 11/14（六） | W13 | 重构方案 + 性能基线建立（全接口压测数据留档）+ ADR（三文件差异化重构策略） | 深度重构 | 基线数据+ADR | [ ] |
| Day86 | 11/15（日） | W13 | **notification.py 物理拆分**（7 文件，纯搬迁，逻辑零改动，半天）+ **report_analyzer.py 抽象消重**（抽 MetricsQueryBuilder 基类 + 拆 4 文件，半天） | 深度重构 | `notification/` 包 + `report_analyzer/` 包 | [ ] |
| Day87 | 11/16（一） | W14 | **case_manager.py 重构 Day1**：提取 `crud.py`（分批 commit，全量回归保持绿）+ `execution.py` + `notify_bridge.py` + `cli.py` | 深度重构 | 4 个新模块+回归绿 | [ ] |
| Day88 | 11/17（二） | W14 | **case_manager.py 重构 Day2**：`batch_runner.py` 拆三段（执行调度/事件发布/进度更新）+ `serializer.py` 改用 marshmallow Schema | 深度重构 | 高风险改造完成 | [ ] |
| Day89 | 11/18（三） | W14 | 重构后全量回归保绿 + 重构对比数据（重构前后性能/可维护性）+ 复盘日：重构阶段回顾+讲述材料第 10 次 | 深度重构/复盘 | 回归绿+对比数据+讲述材料 | [ ] |

> **B 方案说明**：原计划重构 6 天，压缩为 5 天。notification 物理拆分（半天）与 report_analyzer 抽象消重（半天）合并为 Day86 一天；覆盖率缺口补齐已并入质量打磨阶段（Day79-84），不占重构时间。

### 阶段八：MySQL 深优（Day90-93，4天）

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day90 | 11/19（四） | W14 | 【共同休息日，任务按协调#8⑦吸收】MySQL EXPLAIN 慢查询分析 + 索引优化实战 | MySQL深优 | 慢查询分析+索引 | [ ] |
| Day91 | 11/20（五） | W14 | SQLAlchemy 查询优化（selectinload/懒加载调优）+ report_analyzer 大结果集流式解析 + 增量解析 + 内存占用对比 | MySQL深优 | 查询优化~300 行+流式解析+对比 | [ ] |
| Day92 | 11/21（六） | W14 | MySQL 优化量化对比报告（优化前后响应时间/执行计划对比）+ data_driver 增强：CSV 格式支持 + 数据分片加载 | MySQL深优 | 量化对比报告+CSV支持~300 行 | [ ] |
| Day93 | 11/22（日） | W14 | Redis 缓存深化（命中率优化+穿透/雪崩防护+量化数据）+ 连接池调优 + 复盘日：MySQL 深优回顾+讲述材料第 11 次 | MySQL深优/复盘 | 命中率数据+讲述材料 | [ ] |

> **B 方案说明**：原计划 MySQL 深优 6 天，压缩为 4 天。索引/EXPLAIN 两天足够出量化结果；慢查询治理在无生产流量的个人项目里没有真实对象，不单独排期。

### 阶段九：AST 脚本+博客（Day94-95，2天）

> **B 方案定位调整**：AST 精准回归从原计划 9 天平台功能降级为 **2 天脚本+博客**。理由：pytest-testmon 已实现覆盖选测（节省率以 Day95 实测值为准），Develocity Predictive Test Selection 已把"漏检"模型化处理；商业侧双指标也已覆盖。本项目可辩护差异点仅剩"开源 Python 栈里把节省率+漏检率同时公开测量"，1-2 天脚本+博客足够且最优。

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day94 | 11/23（一） | W15 | AST 精准回归脚本工程化（基于 Day45 POC）：覆盖映射+import 拓扑+影响面反查+CLI 入口；量化实验：构造已知答案集（10 组变更），产出**节省率 + 漏检率**双指标（同机/同数据集/重复3次取中位）；用例→fixture/数据依赖清单导出（替代原依赖编排阶段） | AST脚本 | 脚本+CLI+双指标量化数据+依赖清单 | [ ] |
| Day95 | 11/24（二） | W15 | AST 博客撰写《AST 精准回归选型：如何用覆盖率+AST 把执行时间砍下来》（含节省率/漏检率双指标数据+与 pytest-testmon 对比+边界声明）+ KNOWN_LIMITATIONS 补充（AST 召回率上限：动态调用/反射/装饰器重度使用场景失效）+ 复盘日 | AST脚本/博客 | 博客 1 篇+边界声明+讲述材料第 12 次 | [ ] |

### 阶段十：机动缓冲（Day96-100，5天）

> **AquaMind M4 S1-S8 期间（11/26-12/3）的 TestMatrix 轻量窗口**。此阶段 AquaMind 正在做统计严谨性深活（S1-S8），TestMatrix 不排深度任务，仅做机动/补测/文档。

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day96 | 11/25（三） | W15 | **机动**：前期延期则征用；无延期则做全局走查问题修复 + 交互体验细节打磨 | 机动缓冲 | 视情况 | [ ] |
| Day97 | 11/26（四） | W15 | **机动**：无延期则做全局异常兜底（404/500 页面+接口降级）+ 文案/图标/排版统一 | 机动缓冲 | 兜底页+细节清单 | [ ] |
| Day98 | 11/27（五） | W15 | **机动**：无延期则做 Dashboard 自动刷新 + 长时运行稳定性验证 | 机动缓冲 | 稳定性数据 | [ ] |
| Day99 | 11/28（六） | W15 | **机动**：无延期则做千级数据验证 + MySQL 模式全量回归（SQLite→MySQL 切换验证） | 机动缓冲 | 切换回归通过 | [ ] |
| Day100 | 11/29（日） | W15 | **机动**：无延期则做 ADR 归档整理 + KNOWN_LIMITATIONS 补充 + 复盘日（轻量） | 机动缓冲/复盘 | 文档整理+讲述材料 | [ ] |

### 阶段十一：演示打磨（Day101-102，2天）

> **不录视频**。产出可复制的 Demo 操作手册 + 截图组，满足"陌生人 clone 后按手册可复现"。

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day101 | 11/30（一） | W15 | Demo 操作手册编写（命令序列+每步预期输出+核心链路截图：真实执行→报告→通知→看板）+ 演示数据准备（真实感 Demo 数据集，非 mock） | 演示打磨 | 操作手册+数据集+截图组 | [ ] |
| Day102 | 12/01（二） | W16 | 一页纸架构图 + 演示彩排（含异常场景应对）+ 手册走查验证（按手册从零复现）+ 复盘日 | 演示打磨/复盘 | 架构图+彩排记录+手册验证通过 | [ ] |

### 阶段十二：技术博客 3 篇（Day103-106，4天）

> **Flaky 篇移至 Day119**（Flaky 模块完成后发布，确保误标率数据真实）。本阶段发布执行器/xdist 写锁/架构三篇。

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day103 | 12/02（三） | W16 | 博客1《**真实 pytest 执行引擎设计**：subprocess/钩子/插件/xdist 并发实践》（含量化对比数据） | 技术博客 | 博客 1 篇 | [ ] |
| Day104 | 12/03（四） | W16 | 博客2《**pytest-xdist 写锁竞态治理**：并发 1/2/4/8 的吞吐与锁等待曲线》（含量化对比数据） | 技术博客 | 博客 1 篇 | [ ] |
| Day105 | 12/04（五） | W16 | 博客3《**TestMatrix 架构设计复盘**：从 0 到 1 搭建测试效能平台》 | 技术博客 | 博客 1 篇 | [ ] |
| Day106 | 12/05（六） | W16 | **3 篇统一发布归档**（掘金/知乎）+ 发布链接归档 + 复盘日 | 技术博客/复盘 | 3 篇发布+链接归档+讲述材料第 14 次 | [ ] |

> **B 方案说明**：原计划博客 7 篇→5 篇，119 版进一步将 Flaky 篇从 Day104 移至 Day119（Flaky 模块完成后），本阶段实际发布 3 篇。博客是放大器不是必需品，高质量 > 数量。

### 阶段十三：交付准备（Day107-108，2天）

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day107 | 12/06（日） | W16 | README 初稿（badge：CI 状态/覆盖率/开源协议+三步体验流程）+ CHANGELOG 初稿 + LICENSE 核查 | 交付准备 | README 初稿+CHANGELOG | [ ] |
| Day108 | 12/07（一） | W17 | 全量回归预跑（以当日 CI 收集基线为准，当前 1434 条全绿）+ 已知问题清单 + 最终 freeze 前检查项 | 交付准备 | 回归绿+检查清单 | [ ] |

### 阶段十四：缓冲（Day109-114，6天）

> **AquaMind M4 收尾期（12/8-12/13）的 TestMatrix 缓冲窗口**。用于吸收前期延期、Flaky 开工前的准备、或 AquaMind 观测前的轻量待命。不得预支，仅在触发砍件或工作突发时动用。

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day109 | 12/08（二） | W17 | **缓冲**：延期吸收或 Flaky 预研（重复执行框架设计/误标率协议预读） | 缓冲 | 视情况 | [ ] |
| Day110 | 12/09（三） | W17 | **缓冲**：延期吸收或靶向补强 | 缓冲 | 视情况 | [ ] |
| Day111 | 12/10（四） | W17 | 【共同休息日，任务按协调#8⑦吸收】**缓冲**：延期吸收或文档完善 | 缓冲 | 视情况 | [ ] |
| Day112 | 12/11（五） | W17 | **缓冲**：延期吸收或 Flaky 人工标注基准集准备 | 缓冲 | 视情况 | [ ] |
| Day113 | 12/12（六） | W17 | **缓冲**：延期吸收或全量回归复跑 | 缓冲 | 视情况 | [ ] |
| Day114 | 12/13（日） | W17 | **缓冲**：AquaMind M4 最终验收日，TestMatrix 轻量待命 + Flaky 开工准备 | 缓冲 | Flaky 开工就绪 | [ ] |

### 阶段十五：Flaky 用例治理（Day115-119，5天）—— 核心深度模块

> **AquaMind M4 完全结束后开工**（12/14 起）。Flaky 治理是 pytest 生态公认痛点；BuildPulse（$99-499/月）/Develocity 面向海外商业 CI 栈，Python 开源侧有 pytest-flakehunter（重复执行+报告+历史+AI归因）、pytest-flakefighters（DeFlaker 差分覆盖分类+可选抑制）、pytest-flakiness（Flakiness.io 托管 reporter）、pytest-flakemark（AST 插桩行级根因定位）、pytest-xflaky（简称 xflaky；标记自动化 + 隔离提交流程：自动加 xfail(strict=False) 标记并自动提交隔离 PR）。**"隔离"本身已是业界标准模式（marker+CI 分道即可）；xflaky 为标记自动化 + 隔离提交流程，无持续隔离工作流（队列/趋势/回归决策）；本项目可辩护差异点收窄为：误标率自证 + 平台内策略化隔离闭环（跳过/重试/单独报告+队列/看板）+ 标注数据集**。"误标率"是结论成立的条件——商业产品靠海量跨构建数据免此问题，个人项目必须显式测量误标率才能自证判定可信。
>
> **Flaky 最小可交付定义**：重复执行框架 + 自动标注 + 误标率测量协议必须有；Dashboard 看板可砍（砍件序最后一位）。误标率基准集采用种子注入法（故意注入已知 flaky + 已知稳定用例），规模 ≥100 用例 × ≥20 次重复，按根因分类报误标率。种子按 flake rate p∈{5%,10%,20%,30%} 分层注入，按层分别报 FPR/FNR；N=20 时 p=5% 检出率仅 64.2%（1−(1−0.05)^20），该检出下限写入 KNOWN_LIMITATIONS；总误标率为各层加权汇总。

| 天数 | 日期 | 周次 | 当天任务 | 所属模块 | 预计产出 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| Day115 | 12/14（一） | W18 | Flaky 检测：重复 N 次执行框架 + 结果采集模型设计（注意：现有 `reruns=2` 是**掩盖**不是治理） | Flaky治理 | 检测框架~350 行+8 条测试 | [ ] |
| Day116 | 12/15（二） | W18 | 方差计算 + flaky 自动标注算法（结果一致性判定阈值）+ **误标率测量协议**（种子注入法基准集 ≥100 用例 × ≥20 次重复，按 p∈{5%,10%,20%,30%} 分层注入、分报 FPR/FNR，总误标率加权汇总；p=5% 检出下限 64.2% 写入 KNOWN_LIMITATIONS） | Flaky治理 | 标注算法~300 行+8 条测试+误标率数据 | [ ] |
| Day117 | 12/16（三） | W18 | 隔离队列 + 执行策略（跳过/重试/单独报告） | Flaky治理 | 隔离策略~300 行+8 条测试 | [ ] |
| Day118 | 12/17（四） | W18 | Dashboard Flaky 趋势看板 + TOP 榜展示 + 补测 + **产出结构化 Flaky 标注数据集**（方差/标注/隔离数据本身就是资产，可复现）+ **Flaky 博客起笔**（误标率数据已就绪，先写框架+数据图表）+ 讲述材料第 15 次（全项目汇总）+ PLAN 全部勾选验收 | Flaky治理 | 看板~300 行+标注数据集+博客初稿+讲述材料 | [ ] |
| Day119 | 12/18（五） | W18 | Flaky 博客终稿发布《Flaky 用例治理：从掩盖到根治的工程实践》（含误标率数据+与 flakehunter/flakefighters/flakiness/flakemark/xflaky 五家对比）+ **最终 freeze**（README 终版+全量回归+项目交付总结） | Flaky治理/复盘/交付 | 博客 1 篇+代码冻结+交付总结 | [ ] |

---

## 五、执行约束（B 方案，8 条，全程硬性）

1. **Day52 验收门禁（硬性）**
   `TM_EXECUTOR=pytest` 真跑一条 `assert True` 用例，必须做到「通过 + 结果入库 + Web 可见」+「kill -9 崩溃场景 pending 任务回 pending 并重新消费，同一任务 DB 中只产生一条记录（唯一键幂等，键粒度 (batch_id, case_id)；同用例跨批次可再次执行）」+「并发场景下单任务不被两 worker 同时消费（pending+心跳）」+「真 assert False 全链路：真实 traceback 从 pytest→报告解析→DB→Web」。**未通过门禁不得进入联调**，继续补执行器直到通过。

2. **降级规则（超期时的砍件顺序）**
   任一阶段超期时，按 **机动缓冲 5 天（Day96-100）→ 缓冲 6 天（Day109-114）→ 演示 2 天 → 博客 4 天 → DevOps 尾部 D77-78（问题修复/双流水线确认）+ D76 对比记录部分** 的顺序砍；D76 的升级后全量回归不可砍（正确性底线，可并入缓冲执行）。
   **真实执行器 Phase-1/2 / Flaky 治理 三条线永不砍**——它们是项目的深度来源，砍掉等于放弃定位。
   **D96-100 缓冲使用规则**：只吸收"非深度型"延期（文档/UX/补测）；深度型延期（执行器/Flaky/重构）走砍件序，不挪用缓冲。

3. **覆盖率增量门禁（硬性）**
   各里程碑验收时覆盖率必须达标：M5(CI, Day40) ≥80% → M9(执行器深水区, Day64) ≥82% → M11(质量打磨, Day84) ≥85% → M13(MySQL深优, Day93) ≥85%。不达标则停下补测，不得进入下一阶段。

4. **Flask 不迁 FastAPI（硬性）**
   本项目是 Jinja2 服务端渲染 + 同步 CRUD + SSE，Flask 完全胜任；迁移零功能改进且引入回归风险。被问到时回答"服务端渲染无异步收益，迁移是成本不是改进"。

5. **AST 定位为脚本+博客，不做平台功能（硬性）**
   pytest-testmon/Develocity 已产品化覆盖选测+漏检双指标，做平台功能是重复造轮子。仅产出脚本+博客+双指标量化数据，不做 Web 集成、不做 CI 门禁集成。

6. **不做 AI 扩展（硬性）**
   AI 叙事由 AquaMind 独家承载（LLM 应用非功能测试旗舰方向）。TestMatrix 不做失败归因/用例生成/报告摘要等 AI 功能，避免模糊定位且与 AquaMind 重复。Flaky 标注数据本身就是资产，不需要 AI 消费方。

7. **ADR 即时写，不事后补**
   决策发生的当天写进 `docs/adr/`。ADR 的价值是**当时的判断依据与备选方案取舍**，不是文档数量；事后统一补写会丢失时间戳这个唯一资产。

8. **与 AquaMind 执行协调（硬性，119版最终事实）**
   ① 两项目最深任务错峰：TestMatrix 执行器深水（Day45-64，10/5-10/24）落在 AquaMind M1/M2 基础建设期；Flaky 治理（Day115-119，12/14-18）落在 AquaMind M4 完全结束后。② AquaMind 旗舰窗口（M3 10/25~11/15、M4 11/16~12/13）期间，TestMatrix 不排"研究型深水"（执行器/Flaky），仅排中强度任务（DevOps/质量打磨/重构/MySQL/AST）与轻量任务（机动/演示/博客）；**旗舰窗口内 TestMatrix 深度任务（重构 Day87-88 高风险重写）遇 AM 旗舰日则顺延至缓冲，不在 AM 旗舰日做高风险重写**。③ 冲突优先级：本职工作 > AquaMind 旗舰 > TestMatrix；同一天不做两个项目的"深度任务"。④ 倦怠触发器（可测量口径）：**连续 5 个工作日当天目标完成率 <80%（完成率=当天实际产出行数/计划行数，或当天勾选状态 [✓] 占比）→ 立即启用砍件序，下一工作日 TestMatrix 只做 CI 绿灯维护，不新增模块**。⑤ 11 天缓冲（Day96-100 + Day109-114）不得预支，仅在触发砍件或工作突发时动用；缓冲日允许无提交（铁律 #1 豁免）。⑥ 重构降级预案见阶段七（Day85 Go/No-Go + Day87 不绿则冻结 case_manager 重写）。⑦ AquaMind 计划中的 3 个休息日（11/5、11/19、12/10）= 双项目共同休息日，TestMatrix 当天也不排任务（11/5 落在 DevOps Day76 由阶段五内压缩吸收；11/19 落在 MySQL Day90 由阶段八内顺延吸收；12/10 落在缓冲 Day111 消耗 1 天缓冲；总欠账含开工 +1 偏移 ≤4 天，12/13 复盘点按此口径计算剩余模块日）。⑧ **12/13 复盘点**：Day114（缓冲末日）做一次正式复盘——若剩余未完成模块日 >25 天，则 Flaky 降级为"重复执行+标注+误标率"最小可交付，看板与博客进 2027 维护期，项目顺延至 2027-01；若 ≤25 天则按计划 Day115 开工 Flaky。

---

## 六、排期重排对照表（180 天原计划 → 119 天 B 方案·119版）

| 变更项 | 原计划（180天） | B 方案·119版 | 增量 | 理由（含 evolving 二次评估搜索证据 + 六个 AI 复核） |
| --- | --- | --- | --- | --- |
| **AI 扩展** | Day153-164（12天，三能力闭环） | **删除** | -12 | AI 叙事由 AquaMind 独家承载；平台塞 AI 模糊定位且与 AquaMind 重复；对照组数据在平台语境下无人核验 |
| **Web 前端收尾** | Day40-51（12天） | Day41-44（4天） | -8 | 测开岗前端要求"能看、数据真"，三页面核心功能 4 天足够；UX 润色后置机动缓冲 |
| **前后端联调（独立）** | Day62-69（8天） | 并入 Phase-1（Day53-54，2天） | -6 | 执行器已在手上，不需要反复对齐假数据；联调必须落在真实执行之后才有意义 |
| **全量回归+真实场景+文档v1** | Day94-106（13天） | 边界补测并入质量打磨（Day83）；真实场景验证并入 Day84；文档终版并入 Day112 | -10 | 纯完善性边际收益最低；不需要单独 13 天 |
| **AST 精准回归** | Day127-135（9天，平台功能） | Day94-95（2天，脚本+博客） | -7 | pytest-testmon 已实现覆盖选测（节省率以 Day95 实测值为准）；Develocity Predictive Test Selection 已把"漏检"模型化；商业侧双指标也已覆盖。做平台功能是重复造轮子 |
| **依赖编排** | Day136-137（2天） | 取消（用例依赖清单导出并入 Day94 半天） | -2 | pytest fixture 依赖图天然承载用例间依赖；平台侧只需清单导出，不需要独立编排引擎 |
| **开源准备与发布** | Day170-180（11天） | Day107-108（2天，交付准备）+ Day119（最终 freeze） | -8 | 不排期完整开源治理（CONTRIBUTING/Issue模板/code review 流程）；公开仓库+README 终版+CHANGELOG 即满足本项目交付形态，如有进一步开源需求再另行安排 |
| **技术博客** | Day141-147（7篇7天） | Day103-106（3篇3天+1天发布归档）+ Day95（AST博客1天）+ Day119（Flaky博客1天） | -2 | 聚焦高价值选题（执行器/xdist 写锁/架构/AST/Flaky）；Flaky 篇必须在 Flaky 模块完成后发布（Day119），确保误标率数据真实；博客是放大器不是必需品 |
| **MySQL 深优** | Day121-126（6天） | Day90-93（4天） | -2 | 索引/EXPLAIN 两天足够出量化结果；慢查询治理在无生产流量的个人项目里没有真实对象 |
| **深度重构** | Day115-120（6天） | Day85-89（5天） | -1 | notification 物理拆分（半天）与 report_analyzer 消重（半天）合并为一天；assertion 补覆盖已在质量打磨阶段完成 |
| **演示打磨** | Day138-140（3天） | Day101-102（2天） | -1 | 5 分钟 Demo 不需要 3 天；核心链路演示已足够 |
| **质量打磨** | Day107-114（8天） | Day79-84（6天） | -2 | 部分内容（assertion 补覆盖）并入；安全加固 2 天保留；性能优化 1 天保留 |
| **CI 与工具链** | Day38-39（2天） | Day38-40（3天） | +1 | serial/telnet 两个协议客户端从 0 补测，2 天偏紧；加 1 天复盘 |
| **真实执行器 Phase-1/2** | Day52-61 + Day70-79（20天） | Day45-54 + Day55-64（20天） | 0 | 全项目最该花时间的地方，不压缩；位置前移 |
| **Flaky 治理** | Day148-152（5天） | Day115-119（5天） | 0 | 核心深度模块，不压缩；从 Day96-100 错峰至 Day115-119（避开 AquaMind M4 S1-S8 统计深活同周并行） |
| **DevOps** | Day80-93（14天） | Day65-78（14天） | 0 | 不压缩；位置前移 |
| **机动缓冲** | Day165-169（5天） | Day96-100（5天机动）+ Day109-114（6天缓冲） | +6 | 缓冲从 5 天增至 11 天；Flaky 错峰后原位置改机动，新增 6 天缓冲用于 AquaMind M4 收尾期待命 |
| **已完成（Day1-37）** | 37天 | 37天 | 0 | 不变 |
| **合计** | **180** | **119** | **-61** | 总工期减少 61 天，Day119 = 2026-12-18；后续 82 天 = 71 模块日 + 11 天缓冲 |

---

## 七、深度重构专项：三文件差异化重构方案（Day85-89）

三个文件的**病因完全不同**，不能用同一套拆法：搬家、动手术、消重复。

### 7.1 `notification.py`（1894 行）—— 物理拆分，半天，风险极低

**诊断**：7 个类、46 个方法，**>60 行方法仅占 8.7%（4/46）**。它不是上帝类，而是**多个职责清晰的类挤在一个文件里**。

```
src/core/notification/
├── base.py                      BaseNotifier + Notification 数据类
├── channels/
│   ├── email.py                 EmailNotifier
│   └── wechat.py                WeChatNotifier
├── templates/
│   └── email_report.py          EmailReportTemplate（14 个 _render_* 方法，天然内聚）
├── retry.py                     指数退避 + 重试（_send_with_retry）
├── dead_letter.py               NotificationDeadLetterRepository
└── router.py                    NotificationRouter
```

- **做法**：纯搬迁，**逻辑零改动**，半天完成
- **额外收益**：拆完后 `router.py` 的 `__init__` 配置化后，正好支撑"新增一个通知渠道改 1 个文件"

### 7.2 `case_manager.py`（2438 行）—— 逻辑重构，高风险，需 CI 护体

**诊断**：2 个类承载 30 个方法，**>60 行方法占 46.7%（14/30）**；最长方法 `_execute_batch_async` 209 行、`list_cases_paged` 162 行。这不是拆文件能解决的——多个百行级方法说明在手写巨型业务逻辑段，**而项目已经装了 marshmallow 3.20.1 却仅在 web 层使用，core 层序列化仍手写**。

```
src/core/case_manager/
├── crud.py                      sync_cases_from_file(101) / list_cases(81) / list_cases_paged(162) / create / update / delete
├── execution.py                 create_execution / select_cases_for_execution / record_execution(105) / finish_execution(101)
├── batch_runner.py              _execute_batch_async(209) → 拆成「执行调度 / 事件发布 / 进度更新」三段
├── serializer.py                序列化方法系列 → **改用 marshmallow Schema**（已装，core 层未使用）
├── notify_bridge.py             build_notification_statistics(87) / notify_execution_result
└── cli.py                       命令行输出部分
```

- **风险等级：高**。`batch_runner` 与 `serializer` 属于重写，不是搬迁
- **强制要求**：① 必须在 CI + 全量回归（以当日 CI 收集基线为准，当前 1434 条）绿的前提下开工；② **每拆一层一个 commit**，保留重构痕迹，严禁一次性大 commit 掩盖

### 7.3 `report_analyzer.py`（1568 行）—— 抽象消重，不是单纯拆文件

**诊断**：`get_overview_summary`(105) / `get_module_distribution`(92) / `get_failed_top`(106) / `get_quality_metrics`(146) 是**同一套 SQL 组装模式的四个变体**。单纯按文件切开，重复一点没减少。

```
src/core/report_analyzer/
├── parser.py                    AllureResult + ReportAnalyzer（解析层）
├── metrics.py                   ModuleStat / PriorityStat / FailedCaseDetail / StatisticsResult（值对象）
├── aggregator.py                ReportStatistics（聚合层）
└── repository.py                ReportRepository + **MetricsQueryBuilder 基类**（消灭四个方法的重复 SQL 组装）
```

- **关键不是拆成几个文件，而是 MetricsQueryBuilder**
- 验收：拆完后四个查询方法的行数应显著下降，且重复 SQL 片段归零

---

## 八、AST 精准回归技术路线（脚本级，Day94-95）

### 8.1 目标

> 代码变更 → 只跑受影响的用例子集 → 产出「全量基线 vs 精准 X 秒，**节省 Y%**」+「**漏检率 Z%**」的量化对比。基线测量协议：同机/同数据集/重复 3 次取中位/脚本固化进 scripts/。
> **差异化边界（诚实声明）**：覆盖映射选测并非原创——pytest-testmon 等工具已实现（节省率以本项目 Day95 实测值为准）；Develocity Predictive Test Selection（2026.2）已把"漏检"作为模型化处理。
> 本项目的可辩护差异点：① **同时产出节省率和漏检率双指标**（业界普遍只报节省率）；② 开源 Python 栈里公开测量双指标的脚本可复现；③ 用例→fixture/数据依赖清单导出。

### 8.2 技术路线：覆盖映射为主 + AST diff 为辅

| 通道 | 作用 | 精度 | 覆盖场景 |
| --- | --- | --- | --- |
| **主：行级覆盖映射** | 覆盖率产物 →（用例 ↔ 源码行）映射表 → 变更行命中即选中 | 高 | 有测试覆盖到的代码 |
| **辅：import 拓扑图** | 静态解析引用关系，做反向可达性分析 | 中 | 未被覆盖的新增代码（兜底选中，宁可多跑） |

### 8.3 量化实验设计（关键，决定这个能力的可信度）

1. **构造已知答案集**：人工构造 10 个变更提交，**先验标注**每个提交实际影响的用例列表
2. 跑精准选型的推荐子集，与先验答案比对
3. 产出两个数字：
   - **节省率** =（全量耗时 − 精准耗时）/ 全量耗时
   - **漏检率** =（先验受影响但未被选中）/ 先验受影响总数
4. **两个数字必须同时给出**。只报"节省 70%"不报漏检率，会被立刻识破

> **必须同步在 KNOWN_LIMITATIONS 中写清召回率上限**（动态调用、反射、装饰器重度使用的场景会失效）。

---

## 九、技术深度对照表（B 方案）

| 技术栈 | 当前深度 | B 方案终态深度 | 深度体现 |
| --- | --- | --- | --- |
| pytest | 中级（会用 fixture/mark） | 高级 | 钩子函数（`collection_modifyitems`）/自定义**可打包插件**（pytest11 entry point）/xdist 并发 + 写库幂等/subprocess 封装真实执行 |
| SQLAlchemy/MySQL | 中级（ORM CRUD） | 中级偏上→高级 | EXPLAIN 慢查询分析/索引优化实战/selectinload 调优/执行计划对比/量化数据 |
| Redis | 中级偏上（已交付） | 中级偏上 | 缓存层（TTL/穿透/雪崩防护）/任务队列/命中率量化/故障降级/ACK/requeue 健壮性 |
| Flask/Web后端 | 中级偏上（已交付） | 中级偏上 | 应用工厂/蓝图/SSE/异步任务队列/接口缓存层/安全加固（注入与越权） |
| 数据驱动 | 中级 | 高级 | 双格式+CSV+分片+校验配置化 |
| 报告分析 | 中级偏上（已交付） | 中级偏上 | 流式解析/增量解析/质量度量体系+MetricsQueryBuilder 抽象消重 |
| **代码分析** | 无（Day45 POC） | **中级偏上（脚本级）** | **AST 精准回归脚本**（覆盖映射 + import 拓扑 + 影响面反查），产出节省率与漏检率双指标；不做平台功能（pytest-testmon/Develocity 已产品化） |
| 通知推送 | 中级偏上（已交付） | 中级偏上 | 多渠道+分级路由+指数退避重试+死信记录+**可扩展到"改 1 个文件新增渠道"** |
| DevOps | 中级 | 中级偏上 | 多阶段构建/compose 三服务（含 Redis）/Jenkinsfile 全流水线/GitHub Actions CI/k6 量化基线/Python 升级（3.11→当日最新稳定，含 telnetlib 替代）+pytest 当日最新稳定（预期 9.x）+Flask 3.x 升级 |
| **不稳定性治理** | 无（reruns=2 掩盖） | **高级** | Flaky 重复执行/方差计算/自动标注/隔离队列/趋势看板/**误标率指标**（结论成立的条件） |
| 深度重构 | 无 | 高级 | 三文件差异化重构（物理搬迁/逻辑重写/抽象消重三种策略），marshmallow 序列化改造，每拆一层一个 commit |

> **B 方案删除**：AI 方向（失败归因/用例生成/报告摘要）——由 AquaMind 独家承载，TestMatrix 不做 AI 功能。

---

## 十、里程碑节点与验收标准（B 方案）

| 里程碑 | 节点日 | 关键交付物 | 验收标准 |
| --- | --- | --- | --- |
| M1 核心基座完成 ✓ | Day5（08/26） | common/db 层+data_driver+case_manager 全闭环+CLI | 107 条 pytest 全通过 |
| M2 report_analyzer 完成 ✓ | Day9（08/30） | Allure 解析器+统计聚合+趋势+入库 | 真实数据解析无误 |
| M3 通知模块完成 ✓ | Day16（09/06） | 邮件 HTML+企微+分级策略+重试死信+集成 | 真实邮箱收到报告 |
| M4 Web后端+Redis 完成 ✓ | Day34（09/24） | 应用工厂+全 API+SSE+缓存层+任务队列 | test client 全 API 通过 |
| **M5 CI 与工具链就绪** | **Day40（09/30）** | GitHub Actions + pyproject + ruff/mypy + 覆盖率徽章 + serial/telnet 补测 | **CI 绿灯；覆盖率≥80%；serial/telnet 不再是 0%；pip-audit 无未豁免高危** |
| **M6 前端收尾** | **Day44（10/04）** | Dashboard 图表 + 用例管理 + 执行记录三页面 | 三页面数据正常渲染；无控制台报错 |
| **M7 真实执行闭环（门禁）** | **Day52（10/12）** | subprocess 执行器 + source_ref + Web 触发 + ACK/requeue | **`TM_EXECUTOR=pytest` 真跑 assert True → 入库 → Web 可见；kill -9 崩溃 pending 回 pending 可重投；唯一键幂等 (batch_id, case_id) 同用例跨批次可再执行；并发单任务不被两 worker 同时消费；真 assert False 全链路 traceback pytest→解析→DB→Web** |
| M8 执行器 Phase-1 联调完成 | Day54（10/14） | 全链路真实演示能力 | Web 触发→真实执行→入库→看板刷新完整跑通 |
| **M9 执行器深水区完成** | **Day64（10/24）** | 钩子 + 可打包插件 + xdist + 量化报告 | **全量基线→并发优化对比报告**（含写库竞态处理）；覆盖率≥82% |
| M10 DevOps 完成 | Day78（11/07） | Docker + Jenkins + Actions + k6 + Python 升级（3.11→当日最新稳定，含 telnetlib 替代）/pytest 当日最新稳定（预期 9.x）/Flask 3.x | compose 三服务一条命令启动；流水线绿灯；k6 基线归档；版本升级回归绿 |
| **M11 质量打磨完成** | **Day84（11/13）** | 性能/代码质量/安全（含注入防护）+边界补测+真实场景 | 质量报告归档；无高危漏洞；assertion.py 覆盖率维持 100%；覆盖率≥85% |
| **M12 重构完成** | **Day89（11/18）** | 三文件差异化重构 + marshmallow 序列化改造 | N+1 消除；重构后回归绿；重构前后对比数据 |
| **M13 MySQL 深优完成** | **Day93（11/22）** | EXPLAIN+索引+SQLAlchemy 调优+Redis 深化 | MySQL 有 EXPLAIN 对比数据；Redis 命中率数据；覆盖率≥85% |
| **M14 AST 脚本+博客完成** | **Day95（11/24）** | AST 精准回归脚本 + 双指标量化 + 依赖清单 + AST 博客 | **AST 产出节省率 + 漏检率**；博客发布；KNOWN_LIMITATIONS 补充 |
| **M15 Flaky 治理完成** | **Day119（12/18）** | 检测 + 标注 + 隔离 + 看板 + 误标率 + Flaky 博客 | Flaky 用例可被自动识别隔离；**误标率数据（种子注入法基准集 ≥100 用例 × ≥20 次，按 p∈{5%,10%,20%,30%} 分层注入、分报 FPR/FNR，总误标率加权汇总）**；标注数据集产出；Flaky 博客发布（含与 flakehunter/flakefighters/flakiness/flakemark/xflaky 五家对比） |
| M16 演示打磨完成 | Day102（12/01） | Demo 操作手册 + 截图组 + 一页纸架构图 + 彩排（**不录视频**） | 按手册可从零复现核心链路；彩排无失误 |
| M17 技术博客完成 | Day119（12/18） | 3 篇技术博客（执行器/xdist 写锁/架构，Day103-106）+ AST 博客（Day95）+ Flaky 博客（Day119） | 5 篇全部发布（掘金/知乎）并归档 |
| M18 项目交付 | Day119（12/18） | README 终版 + CHANGELOG + 代码 freeze + 全项目讲述材料 | PLAN 全部勾选；陌生人 clone 三步内见 Dashboard |

---

## 十一、达标判据（B 方案，8 条）

| # | 判据 | 量化口径 / 交付物 | 当前状态 |
| --- | --- | --- | --- |
| 1 | **工程治理** | CI 徽章 + 覆盖率 ≥85% + ruff/mypy 零错误 + 提交不断档（休息日除外） | 覆盖率 **98.97%**、CI 五层门禁运行中（Run #71 全绿） |
| 2 | **核心能力（心脏）** | 真实 pytest 执行引擎（subprocess + 钩子 + 可打包插件 + xdist）；全量基线 → 并发优化量化对比 | 待 Day45-64；**测量协议：同机/同数据集/重复 3 次取中位/脚本固化进 `scripts/`** |
| 3 | **Flaky 治理（差异化）** | 重复执行 → 方差 → 自动标注 → 隔离 → 趋势看板 + **误标率指标** | 待 Day115-119；现有 `reruns=2` 只是掩盖；与 flakehunter/flakefighters/flakiness/flakemark/xflaky 五家对比（xflaky 为标记自动化 + 隔离提交流程、无持续隔离工作流）；本项目差异点 = 误标率自证（种子注入法）+ 平台内策略化隔离闭环 + 标注数据集 |
| 4 | **AST 精准回归（脚本级）** | 覆盖映射+import 拓扑+影响面反查脚本；**节省率+漏检率双指标**；用例依赖清单导出 | 待 Day94-95；Day45 POC 已完成；不做平台功能（pytest-testmon/Develocity 已产品化） |
| 5 | **软硬混合** | 串口/Telnet + HTTP 统一用例模型 | Day39 用 **pyserial `loop://` 伪串口 + 本地 socket 模拟**补测试证据；若无真机验证则降级为架构预留 |
| 6 | **工程深度** | 通知退避/死信/分级路由 + Redis 缓存穿透防护+ACK/requeue + MySQL EXPLAIN 优化 + **subprocess 命令注入防护** + 三文件差异化重构 | 前三项已完成，注入防护待 Day82，重构待 Day85-89 |
| 7 | **可复现** | Docker compose 一键启动 + 陌生人 clone 三步内见 Dashboard + Demo 操作手册（不录视频） | 待 Day65-78 / Day101-102 |
| 8 | **边界清晰** | KNOWN_LIMITATIONS（AST 召回率上限 / xdist 收益拐点 / Flask 不迁 FastAPI 的理由）+ ADR 决策记录（**即时写**） | AST 边界待 Day95；ADR 持续即时写 |

> **第 8 条是全部判据里最稀缺的一条**：敢量化自己的边界，本身就是区分度的筛子。

> **B 方案删除**：原第 4 条"AI 闭环"判据——由 AquaMind 独家承载，TestMatrix 不做 AI 功能。

---

## 十二、进度统计

| 指标 | 数值 |
| --- | --- |
| 已完成天数 | 48 / 119（40%） |
| 已完成里程碑 | M1（核心基座）、M2（report_analyzer）、M3（通知模块）、M4（Web后端+Redis）、**M5（CI与工具链，Day40）**、**M6（前端收尾，Day44）** |
| 当前测试基线 | **1434 passed / 0 failed / 0 warning / 0 skipped**（v1 487→828、v2 828→951、v3 951→1013、v5 1013→1040、Day43 收尾 →1081、Day44 前端 →1088、Day44 收尾 →1165、Day45 →1210、全量审查第1批 →1248、**第2批 →1284、第3批 →1318、第4批 →1337、Day46 →1363、Day46-fix →1370、Day47 →1410、Day47-fix →1424、Day48 →1432、Day48-fix2 →1434**；Day48 新增 `tests/test_pytest_runner.py` 8 条、Day48-fix2 再加 2 条，共 **10 条**，全部 monkeypatch `subprocess.run` 不真启子进程、串行三连跑零 flaky） |
| 当前覆盖率 | **98.97%**（4764 语句/49 未覆盖；`src/core/executors.py` **100%**（145 语句/0 未覆盖）、`src/core/case_manager.py` 98%）；CI 门禁 **--cov-fail-under=85**（零收集 exit5 不容忍） |
| CI 覆盖率门禁 | pytest 步骤 `--cov=src --cov-fail-under=85`（Day40 由 80 收紧，不足退出码1判红、零收集 exit5 也判红）；Run #9 起真实生效 |
| CI 状态 | **Run #71 双 job 全绿**（3.11 四步全 success：ruff / mypy增量 / pytest1434-cov85 / pip-audit 分级门禁；3.12 探测位亦全 success），commit aa4fe88（Day48-fix2：env/cwd 异常逃逸修复，一次通过零热修） |
| ruff 存量 | **0 errors（Day40 清零，All checks passed）**，规则不 ignore，保持零增量；Day48-fix2 实测 `ruff check src tests` All checks passed |
| 当前源码量 | src 44 个入库自研文件 + Day47 新增 1 个（`src/scripts/backfill_source_ref.py` 490 行）；tests 47 个入库 .py 文件 + Day47/Day48 各新增 1 个（`tests/test_pytest_runner.py` 356 行）；scripts 新增 1 个 POC 脚本（1288 行）；前端 JS 3198 行 + 模板 795 行（不含 vendor） |
| mypy 存量 | **双层门禁**：增量步骤硬阻断"本次 diff 新增行"类型错误；崩溃/输出异常判红。**Day48 实测：2 个变更 src 文件（case_manager / executors）新增 165 行 0 错误**——本地增量 mypy 会吐全部存量 101 errors / 10 files，**已用脚本把报错行号与 `git diff -U0` 新增行号求交验证为 0**，而非口头断言"没问题"；涉及文件全部为既有未改动文件 |
| mypy 存量（Day39 摸底原始口径，存档） | 202 errors/21 files——Day40 修 F821×6 后注解可解析降至 196（全量基线实测），双层门禁行级化后不再按文件阻断 |
| 累计提交 | **136 次**（48 天里 47 天有提交，唯一空档 2026-09-06 周日；全推 origin/main；Day48 三次提交：交付 `6a9a4e4`（第 134 次）+ 热修 `beec41d`（第 135 次，P3 全修）+ 热修 `aa4fe88`（第 136 次，env/cwd 异常逃逸修复），两次热修均一次通过零追加热修） |
| **下一任务** | **Day49 真实执行器 Phase-1 Day5：执行器稳定性（进程超时杀死/僵尸进程清理/退出码解析）+ pytest 参数化执行（-m/-k/目录选择透传）**（119 版排期表内）。Day48 已把 PytestRunner 从"骨架"变成"可配置的真执行器"，但**超时只杀不洁**——`subprocess.run` 超时后 `subprocess.run` 会 `kill()` 掉直接子进程，而 pytest 自己 spawn 的 xdist worker / 被测代码再开的子进程会变僵尸留在后台，Day49 需处理进程树清理；pytest 参数化执行则要让 `-m`/`-k`/目录选择从用例维度透传到命令，Day48 尚未做任何选择器支持。**遗留人工项**：Day47 回填后剩余的 B 档待补录清单（描述文本无法自动推断路径的用例）需人工补录 source_ref，否则这些用例在 `TM_EXECUTOR=pytest` 下会按"source_ref 为空"降级为 error |

---

*文件生成于 2026-08-27（Day6）。*
*2026-09-03 修订①②：精简技术栈+加强真实执行/性能优化，150→180 天。*
*2026-09-27 修订③：排期重排——执行器前移拆两段/AI 扩12天/AST 升级精准回归9天/CI 插入/Flaky 治理5天，总工期180天。*
***2026-09-28 修订④（B 方案·119版·现行）：基于 evolving 二次评估 + 六个 AI 独立复核核定 B 方案 119 版：总工期 180→119 天（后续 82 天 = 71 模块日 + 11 天缓冲）。删除 AI 扩展12天、Web前端12→4天、独立联调并入执行器、全量回归13天降级、AST 9天→2天脚本+博客、依赖编排取消、开源准备11→2天、博客7→5篇（Flaky篇移至Day119）、MySQL 6→4天、重构6→5天、演示3→2天（删录屏改操作手册+截图）、CI 2→3天。Flask 不迁 FastAPI。真实执行器 Phase-1/2（20天）与 Flaky 治理（5天）为核心深度保留。Flaky 从 Day96-100 错峰至 Day115-119（避开 AquaMind M4 S1-S8），原位置改机动缓冲，总缓冲 5→11 天。Day52 门禁改幂等口径（唯一键 (batch_id, case_id) + 真 assert False 全链路）。公开文档删 CVE-2026-48710 引用。新增覆盖率增量门禁、Flask 不迁约束、AST 脚本级定位、不做 AI 扩展、与 AquaMind 执行协调（最深任务错峰+冲突优先级+倦怠触发器+缓冲保护）8 条执行约束。Day119 = 2026-12-18。六个 AI 最终复核（第二轮）后补改：铁律#1缓冲豁免、pytest 8.x→9.x、76→71模块日算术修正、重构降级预案补写、Day119拆分、M7验收幂等同步、删无出处"节省率70-90%"、协调#8扩至8条（含旗舰窗口深度顺延/倦怠可测量口径/休息日/12-13复盘点）、Flaky竞品加flakemark四家对比、误标率基准集种子注入法。***
