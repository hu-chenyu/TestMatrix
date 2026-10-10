# TestMatrix 通用自动化测试效能平台

[![CI](https://github.com/hu-chenyu/TestMatrix/actions/workflows/ci.yml/badge.svg)](https://github.com/hu-chenyu/TestMatrix/actions/workflows/ci.yml) [![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/) [![tests 1473](https://img.shields.io/badge/tests-1473%20passed-brightgreen)](https://github.com/hu-chenyu/TestMatrix/actions/workflows/ci.yml) [![coverage 99.05%](https://img.shields.io/badge/coverage-99.05%25-brightgreen)](https://github.com/hu-chenyu/TestMatrix/actions/workflows/ci.yml)

> **你的平台绿了，你确定是真绿吗？**
>
> 面向互联网接口自动化测试的全链路效能平台：用例管理 → 调度执行 → 报告分析 →
> Web 可视化 → 通知推送 → CI/CD，Python 全栈自建、单机可跑、零外部依赖起步。
>
> **全项目方法论：约束下量化质量 → 门禁决策**——每个优化阶段产出可复现的前后对比数据，每个能力边界写进 KNOWN_LIMITATIONS。

**它怎么证明自己的数字可信（全部可验证）：**

- **79 条 bug 逐轮清零 + 变异验证打穿假覆盖**：五轮质量专项（不占开发工期），
  安全类修复逐条做变异验证（把修复回退测试必须变红）；第一次跑变异验证就打穿了
  多条"常量断言冒充行为覆盖"的用例——把常量改成 1e-9 等效关闭，测试照样绿，
  分支实测 0 覆盖。没被变异验证校准过的覆盖率，只是行覆盖率，不是行为覆盖率；
  过程中两次推翻自己的审查结论并留痕，不悄悄改写；
- **CI 会拦住"删测试换绿"**：测试收集总数下限断言（基线 1473，只增不减），
  少一条即红；零收集不容忍（exit 5 也判红）；mypy 崩溃守卫——类型检查器自身
  报错时 fail-closed 判红，杜绝"工具没跑=检查通过"（源于 CI 级联跳过真实事故）；
- **1473 条测试 / 99.05% 覆盖 / 0 warning / 0 failed**：CI 双 job 全绿
  （Python 3.11 主 + 3.12 探测位）；提交不间断（唯一空档为 2026-09-06 周日）。
- **测试文件命名约定**：`tests/*_demo.py` 为每日修复对应的回归用例（真实断言，非示例脚本），
  命名源于"修复即回归"的开发纪律；以 mock 为主（含少量真启本机子进程的端到端用例
  与本地回环服务端验证），无真实外部网络/设备/服务连接；核心验收点坚持真实执行，
  不用 mock 掩盖缺陷。

## 快速开始

### 环境要求

- Python 3.11+
- （可选）Allure 命令行工具（生成 HTML 报告用）：[安装指引](https://allurereport.org/docs/install-for-windows/)

### PyPI 包

项目已在 PyPI 预留包名 **`testopshub`**（当前 v0.0.4 为占位包，用于包名预留；正式功能将随项目里程碑逐步发布）。

> 命名对应：PyPI 分发名 `testopshub`、Python 导入名 `testopshub`，均对应本项目 **TestMatrix**（项目仓库名与对外品牌名）。

```bash
# 占位包（当前无可调用功能，仅用于包名预留）
pip install testopshub
```

> 当前推荐通过 `git clone` + `pip install -r requirements.txt` 方式运行（见下方 3 步运行）；正式功能发布后将支持 `pip install testopshub` 直接安装使用。

### 3 步运行（30 秒看到结果）

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 初始化本地库（纯 SQLite，零外部依赖；幂等，已建表则跳过）
python -c "from src.db.db_session import DatabaseSession; DatabaseSession.init_db()"

# 3. 30 秒看到结果：平台自己的用例调度链路（不是 pytest）
python -m src.core.case_manager -f testdata/yaml/api_user_query_matrix.yaml --dry-run
```

> 零外部依赖：纯 SQLite 即可跑通，不用装 Redis / MySQL / Docker。
> 第 3 步 `--dry-run` 为零副作用预览，输出"待执行用例列表（共 4 条，全新库口径）TM-API-0201…0204"，exit=0。
>
> **你会看到**：用例被解析入库 → 批次创建 → 筛选执行 → 汇总统计的完整链路输出；
> 启动 Web 平台后可在 Dashboard 查看统计卡片 / 通过率趋势折线 / 模块分布环图 / 失败 Top 榜。

```bash
# 可选：启动 Web 平台（纯 SQLite 零外部依赖）
python run.py             # 启动后浏览器访问 http://localhost:5000/dashboard
# 可选参数：--port 8080 指定端口 / --host 0.0.0.0 开放局域网 / --debug 调试模式
```

### 更多体验命令

```bash
# 用例调度链路 CLI（入库→批次创建→筛选执行→汇总统计）
python -m src.core.case_manager -f testdata/yaml/api_user_query_matrix.yaml           # 全流程模拟执行
python -m src.core.case_manager -f testdata/yaml/api_user_query_matrix.yaml --dry-run # 仅预览待执行用例
python -m src.core.case_manager -f testdata/yaml/api_user_query_matrix.yaml -p P0     # 按优先级筛选执行

# 生成并打开 Allure HTML 报告（需已安装 allure 命令行）
allure generate output/allure_results -o output/reports/allure-report --clean
allure open output/reports/allure-report
```

### 启用 Redis 任务队列（可选）

默认纯 SQLite 即可运行，批次执行走进程内线程。需要任务跨进程排队、串行消费时
可启用 Redis 队列（trigger 路由 LPUSH 入队，独立 worker 进程 BRPOP 消费；
Redis 故障时自动回退裸线程，任务不丢）：

```bash
# 1. 安装并启动 Redis（本机默认 redis://127.0.0.1:6379/0）
#    Windows 可用 Memurai / WSL；macOS: brew install redis && redis-server

# 2. 在 .env 中开启开关（首次运行 python run.py 会自动从 .env.example 生成 .env）
#    TM_REDIS_ENABLED=true
#    TM_TASK_QUEUE_ENABLED=true
#    TM_TASK_WORKER_ENABLED 保持 false 即可：Web 进程不起 in-app worker，
#    全局由下方独立脚本单进程消费（串行），脚本会在自身进程内自动置位该开关

# 3. 启动 Web（终端 1）
python run.py

# 4. 另开一个终端启动常驻 worker（Ctrl+C 优雅停止，内置建表与 Redis PING 自检）
python scripts/start_worker.py
```

### Docker 方式运行

```bash
# 构建镜像并运行测试容器（产物挂载到宿主机 output/ 目录）
docker compose -f docker/docker-compose.yml up test-runner

# MySQL 模式联调（可选）
docker compose -f docker/docker-compose.yml --profile mysql up -d
```

## 为什么用 TestMatrix

市面上的测试平台大多回答"怎么把用例跑起来"。TestMatrix 回答的是
"跑出来的结论凭什么可信"：

1. **CI 不允许靠删测试变绿。**
   多数仓库的覆盖率门禁只卡百分比——删掉 10 条测试，覆盖率从 88% 升到 89%，绿灯反而更亮。TestMatrix 的 CI 里有一条测试总数下限断言：1473 是下限，删一条就红。

2. **覆盖率数字是被变异验证校准过的。**
   变异验证会主动改坏生产代码、确认用例真的会红。我们第一次跑它就打穿了多条"常量断言冒充行为覆盖"的用例——把常量改成 1e-9 等效关闭，测试照样绿，分支实测 0 覆盖。没被变异验证校准过的覆盖率，只是行覆盖率，不是行为覆盖率。

3. **任务不丢、不重复、不静默死亡。**
   队列幂等抢占（同批次不双跑）+ BRPOPLPUSH ack/requeue（worker 崩溃自动补偿）+ 空队列指数退避（防 2 秒 2 万次热转）；Redis 故障自动回退裸线程。

4. **零外部依赖起步，单机就能跑。**
   纯 SQLite 即可完整跑通「入库 → 批次 → 执行 → 汇总 → 看板」全链路；Redis / MySQL / Docker 均为可选增强，不需要先搭基础设施。

## 核心能力

- **数据驱动引擎**：YAML / Excel 外部数据参数化，用例与数据彻底分离；三维筛选（模块/优先级/标签）
- **用例调度管理**：批次管理、P0-P3 分级执行、dry-run 零副作用预览、CLI/Web 双触发，执行结果与批次汇总全链路入库可追溯
- **HTTP 客户端与安全脱敏**：Requests 统一封装（超时 / 重试 / 错误分类）；日志与报告中的凭据、查询参数、文件路径统一脱敏（含 Windows 路径边界）
- **报告分析引擎**：Allure 结果解析、通过率/耗时 P95/失败明细统计、模块与优先级分布、趋势数据入库；质量度量（覆盖率趋势/缺陷密度/执行效率）
- **Web 可视化平台（三页面已交付）**：Dashboard（统计卡片/趋势折线/模块环图/优先级柱图/失败 Top）、用例管理（列表/筛选/CRUD/批量导入）、执行记录（批次列表/详情/触发/SSE 实时日志）
- **多渠道通知推送**：邮件 HTML 报告（内联 CSS）+ 企微 markdown 摘要、失败@负责人、分级通知策略、失败重试（指数退避）+ 死信记录
- **真实 pytest 执行（开发中）**：subprocess 同步执行已落地——超时控制 / cwd·env 隔离 / 退出码映射 / cp936 解码 / source_ref 追踪；规划中 pytest 钩子与自定义插件、xdist 并发、模拟/真实双模式切换、结果归一
- **进阶工程能力**：Redis 缓存层+任务队列（已交付）、MySQL 深度优化（EXPLAIN/索引，规划中）、AST 精准回归（脚本级：覆盖映射+import 拓扑+影响面反查，规划中）、k6 性能压测基线（规划中）
- **全链路日志**：Loguru 三通道输出（控制台/全量/错误独立），按天切割、trace_id 用例级追踪
- **数据持久化**：SQLAlchemy 2.0 ORM，用例/执行记录/缺陷统计三张核心表，SQLite（本地）与 MySQL 8.0（团队共用）一键切换

## 技术栈

| 分层 | 技术选型 |
| --- | --- |
| 核心语言 | Python 3.11（CI 多版本矩阵） |
| 测试框架 | pytest 7.4 →当日最新稳定（预期 9.x，按插件兼容矩阵确认）+ allure-pytest + pytest-rerunfailures + pytest-cov + pytest-xdist（依赖已随 requirements 集成，并发执行规划中） |
| 协议层 | Requests |
| 日志报告 | Loguru / Allure 2.x |
| 数据驱动 | PyYAML / openpyxl |
| 数据层 | SQLAlchemy 2.0（SQLite 3 / MySQL 8.0） |
| 缓存与队列 | Redis（已交付） |
| Web平台 | Flask 2.3（大版本冻结，D75 冻结期 CVE 核查，不升级 3.x）+ Jinja2 + Bootstrap 5 + ECharts 5 |
| 代码分析 | AST 精准回归（脚本级：覆盖映射+import 拓扑） |
| 工程化 | Git / Jenkins / Docker / Docker Compose / k6 |

## 工程质量与纪律

- **CI 五层门禁**：ruff 不豁免 → mypy 新增行硬阻断（崩溃即红）→ 覆盖率 85% 硬门禁 + 测试收集总数下限断言 → pip-audit 高危 fail-closed（豁免单必须带未过期的过期日）→ 探测版本不阻断
- **五轮质量大扫除**：79 条 bug 逐轮清零，安全类修复逐条变异验证，覆盖率 93%→99.05%，测试 497→1473
- **能力边界公开**：每个能力边界写进 KNOWN_LIMITATIONS，不做无证据承诺
- **ADR 即时写**：架构决策发生当天写进 docs/adr/，不事后统一补写

## 目录结构

```
TestMatrix/
├── src/                    # 核心源码
│   ├── common/             # 公共底层封装（HTTP/日志/断言/环境配置）
│   ├── db/                 # 数据持久层（ORM模型 + 会话管理，SQLite/MySQL双模式）
│   ├── core/               # 平台核心逻辑（数据驱动/用例调度/报告解析/通知推送）
│   └── web/                # Flask Web可视化平台（三页面已交付）
├── tests/                  # pytest测试用例
│   └── api_demo/           # HTTP接口测试Demo（登录/用户查询/健康检查）
├── testdata/               # 数据驱动测试数据（yaml/ excel/）
├── output/                 # 运行产物（日志/Allure结果/报告，不入Git）
├── examples/               # 扩展Demo（k6性能脚本）
├── docker/                 # 容器化配置（Dockerfile、docker-compose.yml）
├── docs/                   # 项目文档（core架构设计文档、ADR）
├── pytest.ini              # pytest核心配置
├── .env.example            # 环境变量模板
└── requirements.txt        # Python依赖清单
```

## 阶段规划

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 第一阶段 | 架构基座：目录骨架、common封装层、数据持久层、pytest体系、Demo验证 | ✅ 已完成 |
| 第二阶段 | 核心能力：数据驱动引擎、用例调度、报告解析、通知推送、Flask+Redis后端、Web三页面骨架 | ✅ 已完成 |
| 第三阶段 | 前端交付：Dashboard四图表、用例管理、执行记录三页面 | ✅ 已完成 |
| 第四阶段 | 深度能力：真实pytest执行引擎、Flaky用例治理、AST精准回归脚本 | 🔄 开发中 |
| 第五阶段 | 工程化交付：Docker三服务编排+Jenkins+k6、MySQL深度优化、质量打磨+重构、演示+博客+交付 | ⏳ 规划中 |

> 阶段表说明：CI/CD 已交付（`.github/workflows/ci.yml` 五层质量门禁，双 job：3.11 主 + 3.12 探测），Web 三页面已交付。
> 详细路线图与里程碑说明见 [docs/ROADMAP.md](docs/ROADMAP.md)。

core 层架构设计见 [docs/core_architecture.md](docs/core_architecture.md)。

---

**License**: MIT — Copyright (c) 2026 chenyu.hu
