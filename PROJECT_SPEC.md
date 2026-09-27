# TestMatrix 通用自动化测试效能平台 — 项目规格说明书

> 本文档是了解 TestMatrix 的第一入口：做出来有什么效果、怎么操作、怎么扩展。
> 开发基准（Baseline）：功能范围、技术选型、目录结构以本文档为准；确需变更先更新本文档再实施。

## 1. 项目概述

**TestMatrix 是什么**：一个面向互联网接口自动化测试的全链路效能平台，覆盖「用例管理 → 调度执行 → 报告分析 → Web 可视化 → 通知推送 → CI/CD」完整闭环。芯片嵌入式板卡测试的扩展能力已在 common 层预留（串口/Telnet 封装），架构支持双赛道。

**解决什么痛点**：
- 用例与数据耦合：参数一改动十几个文件 → YAML/Excel 外置数据驱动
- 裸 pytest 只有命令行：无批次管理、无历史趋势、无失败归因 → 批次化调度 + 三表持久化 + Dashboard
- 执行结果靠人盯：跑完不知道、失败没人跟 → 邮件 HTML 报告 + 企微推送 + 失败@负责人 + 分级通知
- 环境搭建成本高：clone 后装一堆东西才能跑 → 三步启动 + Docker 一键编排

**目标用户**：测试工程师（个人/小团队）、开源二开者、测试效能建设参考者。

**为什么不用 pytest 直接跑**：pytest 解决"执行"，不解决"管理"——用例资产化（入库/检索/分级）、执行批次化（历史/趋势/对比）、结果可视化（Dashboard/质量度量）、失败闭环（通知/@人/死信）是平台层的价值；且平台通过 subprocess 反向驱动真实 pytest 执行（**Day52-61 + Day70-79 交付**，分两段前移），两者是包含关系不是替代关系。

## 2. 核心功能与效果

| 模块 | 做什么 | 做完的直观效果 |
| --- | --- | --- |
| 用例管理 | YAML/Excel 批量导入、页面单条增删改、三维筛选（模块/优先级/标签）、分页列表 | 测试资产入库可检索，改参数不改代码 |
| 调度执行 | 批次管理（RUN-时间戳-随机位）、P0-P3 分级执行、dry-run 预览、CLI/Web 双触发 | 一条命令圈定 P0 冒烟先跑，执行顺序与风险对齐 |
| 报告分析 | Allure 结果解析、通过率/耗时 P95/失败明细统计、模块与优先级分布、趋势数据入库 | 每个批次自动产出统计，Dashboard 直接消费 |
| Web 可视化 | Dashboard（统计卡片/趋势折线/模块饼图/失败 Top/质量度量）、用例管理页、执行记录页（批次列表/单用例详情/失败堆栈/SSE 实时日志） | 浏览器里看板式操作，触发执行实时看日志滚动 |
| 通知推送 | 执行完成自动发邮件（HTML 报告）和企微（markdown 摘要）、失败用例@负责人、分级通知（全量/仅失败）、失败重试（指数退避）+死信记录 | 跑完手机/邮箱收到带颜色（绿/橙/红）的报告，失败有人跟 |
| 真实执行 | subprocess 封装真实 pytest、钩子/自定义插件、pytest-xdist 并发、模拟/真实双模式切换 | 平台可跑任意 pytest 项目并回传结果（**Day52-61 + Day70-79 交付**） |
| 进阶能力 | Redis 缓存层+任务队列、MySQL 深度优化（EXPLAIN/索引）、AST 精准回归选型、接口依赖编排（token 传递/场景编排）、Flaky 用例治理、k6 性能压测 | 中级偏上~高级技术深度的载体，全部有量化对比数据 |

## 3. 快速开始（3 步跑起来）

**环境要求**：Python 3.11.x；可选 Allure 命令行（HTML 报告）、Docker（容器方式）。

```bash
# 第1步：安装依赖（国内镜像加速）
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 第2步：运行测试验证环境（内置本地模拟服务，零外部依赖、可离线执行）
pytest

# 第3步：启动 Web 平台（Day17-30 Web后端交付后生效）
python -m src.web.app
# 浏览器访问 http://localhost:5000 → 看到 Dashboard 与预置示例数据
```

> 当前阶段（Day37）Web 后端已交付，第3步可启动后用浏览器访问验证；CLI 仍可用于体验完整调度链路：
> `python -m src.core.case_manager -f testdata/yaml/api_user_query_matrix.yaml --dry-run`
> （入库→批次创建→筛选→执行→汇总统计，`--dry-run` 仅预览不落库；`-p P0` 按优先级筛选）

**预期效果**：`pytest` 全绿（**当前基线 422 条通过，覆盖率 82%**）；CLI 输出待执行用例列表与批次汇总报告（总数/通过/失败/通过率）。

## 4. 使用指南

**怎么导入用例**：Web 用例管理页「批量导入」上传 YAML/Excel（模板字段：case_id/name/module/priority/tags，可从页面下载模板）；或 CLI：`python -m src.core.case_manager -f testdata/yaml/你的文件.yaml`（自动 upsert，case_id 存在则更新）。

**怎么触发执行**：① Web 执行记录页点「触发执行」（选文件/优先级/模块/标签/dry-run 开关）；② CLI 命令（同上，支持 `-p P0 -m 用户管理 -t smoke` 组合筛选）；③ Jenkins 定时/参数化构建自动触发。

**怎么看报告**：Dashboard 看通过率趋势/模块分布/失败 Top/质量度量趋势；执行记录页点批次号看单用例详情与失败堆栈（error_message + trace 完整展示）；Allure 原始报告：`allure generate output/allure_results -o output/reports/allure-report --clean && allure open output/reports/allure-report`。

**怎么配置通知**：`copy .env.example .env` 后填写 `TM_EMAIL_*`（SMTP 授权码）与 `TM_WECHAT_WEBHOOK_URL`（企微群机器人），开关置 true；未配置时通知自动跳过不影响主流程。

**怎么用 Docker 启动**：`docker compose -f docker/docker-compose.yml up -d`（Web+MySQL+Redis 三服务编排，含 healthcheck 与预置数据）。

## 5. 架构说明

**分层架构**：common（公共能力：HTTP/串口/Telnet 客户端、日志、配置、断言）→ db（三表 ORM + 会话管理，SQLite/MySQL 双模式）→ core（data_driver 数据驱动 / case_manager 调度 / report_analyzer 分析 / notification 通知）→ web（Flask 应用工厂 + 蓝图 + Redis 缓存与任务队列）。数据流：用例文件 → data_driver → case_manager 入库调度 → Allure 结果 → report_analyzer 统计入库 → Web 看板/通知消费。

**关键选型理由**：
- Flask 而非 FastAPI：平台以同步 CRUD + SSE 为主，Flask 生态成熟、模板渲染（Jinja2）与蓝图组织契合多页面后台；不需要 async 高并发场景，避免为用不到的特性引入学习与维护成本。
- SQLAlchemy 2.0 ORM 而非原生 SQL：双数据库切换只改 URL；类型标注（Mapped/mapped_column）配合 IDE 补全；N+1 用 selectinload 治理而非手拼 JOIN。
- SSE 而非 WebSocket：日志推送是单向服务端→客户端，SSE 基于普通 HTTP、无需额外协议栈与端口，断线重连浏览器原生支持。
- Redis：缓存层（统计结果/用例列表，TTL+穿透防护）与任务队列（异步执行），是执行触发从"裸线程"到"可观测队列"的升级路径。
- 通知基类抽象（BaseNotifier）：邮件/企微/未来钉钉统一 send 接口，新增渠道零侵入。

## 6. 配置说明（环境变量）

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| TM_ENV | 运行环境（dev/test/prod） | dev |
| TM_BASE_URL / TM_HTTP_TIMEOUT / TM_HTTP_RETRIES | 被测服务地址/超时/重试 | httpbin.org / 10 / 2 |
| TM_LOG_LEVEL / TM_LOG_DIR | 日志级别/目录 | INFO / output/logs |
| TM_DB_TYPE | 数据库类型（sqlite/mysql） | sqlite |
| TM_DB_SQLITE_PATH | SQLite 文件路径 | output/testmatrix.db |
| TM_DB_MYSQL_HOST/PORT/USER/PASSWORD/DATABASE | MySQL 连接配置 | 127.0.0.1/3306/root/空/testmatrix |
| TM_EMAIL_ENABLED / SMTP_HOST / SMTP_PORT / SENDER / PASSWORD / RECEIVERS | 邮件通知（465=SSL，587=STARTTLS） | false / 示例占位 |
| TM_WECHAT_ENABLED / WEBHOOK_URL | 企微机器人通知 | false / 空 |
| TM_REDIS_ENABLED / HOST / PORT / PASSWORD / DB | Redis 缓存与队列（**Day31-33 已接入**） | false / 127.0.0.1 / 6379 |

## 7. 演示效果

核心功能截图（Dashboard / 用例管理 / 执行记录 / SSE 日志）与 5 分钟 Demo 视频、1 分钟高光视频在项目完成演示打磨阶段（**Day138-140**）后归档至 docs/ 与 README；当前可在 `output/reports/` 查看 Allure 报告效果、在 README 查看架构与阶段进度。

## 8. 常见问题 FAQ

- **Q：怎么切换 SQLite/MySQL？** `.env` 中 `TM_DB_TYPE=mysql` 并填写 `TM_DB_MYSQL_*`，重启即生效（表结构 `DatabaseSession.init_db()` 自动建）。
- **Q：怎么添加新的通知渠道？** 继承 `src/core/notification.py` 的 `BaseNotifier`，实现 `send(notification)`（失败内部捕获返回 False），配置加 `TM_XXX_ENABLED` 开关即可接入统一路由。
- **Q：怎么扩展新的执行器？** case_manager 的 `_simulate_execute` 是单点替换位；真实 pytest 执行走 `PytestRunner`（subprocess）；新执行器实现同构接口即可被批次调度复用。
- **Q：邮件/企微发送失败会影响执行吗？** 不会。通知是旁路能力：send 内部捕获全部异常返回 False，失败自动指数退避重试，重试耗尽记入死信，主流程零感知。
- **Q：Redis 挂了平台还能用吗？** 能。缓存层带故障降级：Redis 不可用时自动回退直查数据库并记录告警日志。

## 9. 技术栈（严格遵循，不随意增减核心组件）

| 分类 | 技术 |
| --- | --- |
| 核心语言 | Python 3.11 |
| 测试框架 | pytest 7.4.x → 8.x（Day100 升级）、allure-pytest、pytest-rerunfailures、pytest-cov、pytest-xdist（Day52-79 真实执行阶段） |
| 协议层 | Requests（HTTP）、pyserial（串口）、telnetlib（Telnet） |
| 日志报告 | Loguru、Allure 2.x |
| 数据驱动 | PyYAML、openpyxl |
| 数据层 | SQLAlchemy 2.0 ORM（SQLite 3 / MySQL 8.0 双模式） |
| 缓存与队列 | Redis（Day31-33 已接入） |
| Web平台 | Flask 2.3 → 3.x（Day101 升级）、Jinja2、Bootstrap 5、ECharts 5 |
| 代码分析 | AST 精准回归选型（覆盖映射为主 + import 拓扑为辅，Day127-135） |
| AI 能力 | LLM 统一适配层（Day153-164）：失败归因 / 用例生成 / 报告摘要，带离线 Mock 与降级；机动缓冲 Day165-169 |
| 工程化 | Git、Jenkins、GitHub Actions（Day38-39 接入 CI）、Docker、Docker Compose、k6 |
| 辅助 | python-dotenv、smtplib、marshmallow |

## 10. 阶段规划（对应 PROJECT_PLAN 180 天）

阶段编号与排期以 `PROJECT_PLAN.md` 第三节为准（Day1 ~ Day180，末日 Day180 = 2027-02-17）。

| 阶段 | 天序 | 内容 | 状态 |
| --- | --- | --- | --- |
| 阶段 A-D | Day1-37 | 基座与核心：目录骨架、common 封装层、数据持久层、pytest 体系、数据驱动、用例调度、报告解析、通知推送、Flask 后端 + Redis、Web 骨架与 Dashboard | 已完成 |
| 阶段一~二 | Day38-51 | CI 与工具链 + Web 前端收尾 | 下一任务（Day38） |
| 阶段三~五 | Day52-79 | 真实 pytest 执行器 Phase-1 → 前后端联调 → Phase-2 深水区（钩子 + 自研插件 + xdist 并发） | 规划中 |
| 阶段六~八 | Day80-114 | DevOps（Docker / Jenkins / k6）、全量回归 + pytest 8.x 与 Flask 3.x 升级、质量打磨 | 规划中 |
| 阶段九~十 | Day115-137 | 三文件差异化重构 + MySQL 深优 + AST 精准回归选型 + 依赖编排 | 规划中 |
| 阶段十一~十五 | Day138-169 | 演示打磨 + 技术博客 7 篇 + Flaky 用例治理 + AI 扩展（Day153-164）+ 机动缓冲（Day165-169） | 规划中 |
| 阶段十六 | Day170-180 | 开源准备与 v1.0.0 发布 | 规划中 |

## 11. 验收标准

**已完成（Day1-37，详细逐日记录见 PROJECT_PLAN.md）**：
- [x] 目录结构一致 [x] 三表 ORM+双模式切换 [x] common 六模块 [x] pytest 体系+失败自动记录 [x] pytest 直跑生成 Allure [x] 依赖锁定/.gitignore/.env.example
- [x] data_driver：双格式加载、三维筛选、50 条大数据量验证
- [x] case_manager：入库/批次/分级执行/结果记录/汇总统计全链路 + CLI
- [x] report_analyzer：解析准确、统计与人工核对一致、入库正常、趋势可查
- [x] 通知模块三渠道 + 重试退避 + 死信记录（真实送达待凭据）
- [x] Web 后端：三页面 API + SSE 实时日志；Redis 缓存层与任务队列（Day17-34）
- [x] 全量用例 422 条通过，覆盖率 82%（3442 stmts / 629 miss）

**Day38-51（当前）**：
- [ ] CI：GitHub Actions 绿灯、pyproject + ruff/mypy 落地、覆盖率徽章上 README
- [ ] 覆盖率修复：serial_client / telnet_client 脱离 0%
- [ ] Web 前端：Dashboard 图表与三页面数据渲染正常，无控制台报错

**Day52-180（待办）**：
- [ ] **Day61 门禁**：`TM_EXECUTOR=pytest` 真跑一条 assert True 通过 + 结果入库 + Web 可见
- [ ] 真实执行深水区：pytest 钩子 + 可打包插件 + xdist 并发结果合并正确 + 396 秒量化对比
- [ ] 通知真实送达：真实邮箱收 HTML 报告、企微推送成功（需 SMTP/企微凭据）
- [ ] Redis：缓存命中前后量化对比、任务队列可观测
- [ ] 依赖编排：token 跨用例传递、失败跳过依赖用例
- [ ] Docker 三服务一条命令启动，容器内跑通真实执行 → 报告 → 看板
- [ ] Jenkins 流水线绿灯
- [ ] MySQL 优化 EXPLAIN 前后对比数据
- [ ] **AST 精准回归选型 CLI 可运行，产出节省率与漏检率**
- [ ] **Flaky 治理**：用例可被自动识别隔离，产出标注数据供 AI 消费
- [ ] **AI 三项**：失败归因 / 用例生成 / 报告摘要，每项有对照组与成本数据
- [ ] 24 小时稳定性、陌生人三步启动
- [ ] 7 篇博客发布、5 分钟 Demo + 1 分钟高光视频

## 12. 开发规范要求

1. PEP8、模块职责单一、中文注释、完整类型标注
2. 生产级容错：异常捕获、全链路日志、入参校验，禁止玩具式写法
3. 优化类工作必须产出量化前后对比数据
4. 每阶段仅完成当期交付，不超前开发
