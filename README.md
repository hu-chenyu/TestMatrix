# TestMatrix 通用自动化测试效能平台

> 面向互联网接口自动化测试的全链路效能平台：用例管理 → 调度执行 → 报告分析 →
> Web 可视化 → 通知推送 → CI/CD，覆盖自动化框架设计、数据驱动、测试平台化与
> DevOps 工程化全链路；芯片嵌入式板卡测试扩展能力已预留（架构支持双赛道）。

## 核心特性

- **数据驱动引擎**：YAML / Excel（规划中支持 CSV）外部数据参数化，用例与数据彻底分离；
  三维筛选（模块/优先级/标签）+ 接口依赖编排（规划中：token 跨用例传递、场景化编排）
- **用例调度管理**：批次管理、P0-P3 分级执行、dry-run 零副作用预览、CLI/Web 双触发，
  执行结果与批次汇总全链路入库可追溯
- **多协议统一封装**：HTTP（Requests）/ 串口（pyserial）/ Telnet 三协议客户端统一封装，
  芯片板卡测试仅需低成本适配即可接入
- **报告分析引擎**：Allure 结果解析、通过率/耗时 P95/失败明细统计、模块与优先级分布、
  趋势数据入库；质量度量（覆盖率趋势/缺陷密度/执行效率，规划中）
- **Web 可视化平台（开发中）**：Dashboard（统计卡片/趋势折线/模块饼图/失败 Top）、
  用例管理、执行记录（批次列表/失败堆栈/SSE 实时日志）三页面
- **多渠道通知推送**：邮件 HTML 报告（内联 CSS）+ 企微 markdown 摘要、失败@负责人、
  分级通知策略、失败重试（指数退避）+ 死信记录
- **真实 pytest 执行（规划中）**：subprocess 封装、pytest 钩子与自定义插件、
  pytest-xdist 并发执行、模拟/真实双模式切换
- **进阶工程能力（规划中）**：Redis 缓存层+任务队列、MySQL 深度优化（EXPLAIN/索引）、
  AST 用例代码静态检查、k6 性能压测基线
- **全链路日志**：Loguru 三通道输出（控制台/全量/错误独立），按天切割、trace_id 用例级追踪
- **数据持久化**：SQLAlchemy 2.0 ORM，用例/执行记录/缺陷统计三张核心表，
  SQLite（本地）与 MySQL 8.0（团队共用）一键切换

## 技术栈

| 分层 | 技术选型 |
| --- | --- |
| 核心语言 | Python 3.11 |
| 测试框架 | pytest 7.4 + allure-pytest + pytest-rerunfailures + pytest-cov（规划：pytest-xdist） |
| 协议层 | Requests / pyserial / telnetlib |
| 日志报告 | Loguru / Allure 2.x |
| 数据驱动 | PyYAML / openpyxl |
| 数据层 | SQLAlchemy 2.0（SQLite 3 / MySQL 8.0） |
| 缓存与队列 | Redis（规划中） |
| Web平台 | Flask 2.3 + Jinja2 + Bootstrap 5 + ECharts 5 |
| 代码分析 | AST 静态检查（规划中） |
| 工程化 | Git / Jenkins / Docker / Docker Compose / k6 |

## 快速开始

### 环境要求

- Python 3.11.x
- （可选）Allure命令行工具（生成HTML报告用）：[安装指引](https://allurereport.org/docs/install-for-windows/)

### 3 步运行

```bash
# 1. 安装依赖
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 运行测试（Demo用例内置本地模拟服务，零外部依赖、可离线执行）
pytest                  # 全量用例
pytest -m smoke         # 仅冒烟用例

# 3. 启动Web平台（Web后端交付后生效）
python -m src.web.app   # 访问 http://localhost:5000
```

### 更多体验命令

```bash
# 用例调度链路CLI（入库→批次创建→筛选执行→汇总统计）
python -m src.core.case_manager -f testdata/yaml/api_user_query_matrix.yaml           # 全流程模拟执行
python -m src.core.case_manager -f testdata/yaml/api_user_query_matrix.yaml --dry-run # 仅预览待执行用例
python -m src.core.case_manager -f testdata/yaml/api_user_query_matrix.yaml -p P0     # 按优先级筛选执行

# 生成并打开Allure HTML报告（需已安装allure命令行）
allure generate output/allure_results -o output/reports/allure-report --clean
allure open output/reports/allure-report
```

### Docker方式运行

```bash
# 构建镜像并运行测试容器（产物挂载到宿主机output/目录）
docker compose -f docker/docker-compose.yml up test-runner

# MySQL模式联调（可选）
docker compose -f docker/docker-compose.yml --profile mysql up -d
```

## 目录结构

```
TestMatrix/
├── src/                    # 核心源码
│   ├── common/             # 公共底层封装（HTTP/串口/Telnet/日志/断言/环境配置）
│   ├── db/                 # 数据持久层（ORM模型 + 会话管理，SQLite/MySQL双模式）
│   ├── core/               # 平台核心逻辑（数据驱动/用例调度/报告解析/通知推送）
│   └── web/                # Flask Web可视化平台（开发中）
├── tests/                  # pytest测试用例
│   ├── api_demo/           # HTTP接口测试Demo（登录/用户查询/健康检查）
│   └── chip_demo/          # 芯片板卡测试Demo（预留）
├── testdata/               # 数据驱动测试数据（yaml/ excel/）
├── output/                 # 运行产物（日志/Allure结果/报告，不入Git）
├── examples/               # 扩展Demo（k6性能脚本）
├── docker/                 # 容器化配置（Dockerfile、docker-compose.yml）
├── docs/                   # 项目文档（core架构设计文档）
├── pytest.ini              # pytest核心配置
├── .env.example            # 环境变量模板
├── requirements.txt        # Python依赖清单
└── PROJECT_SPEC.md         # 项目规格说明书（开发基准）
```

## 阶段规划

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 第一阶段 | 架构基座：目录骨架、common封装层、数据持久层、pytest体系、Demo验证 | ✅ 已完成 |
| 第二阶段 | 核心能力：数据驱动引擎、用例调度、报告解析、通知推送、Flask+Redis后端、Web三页面、真实pytest执行、依赖编排 | 🔄 开发中 |
| 第三阶段 | 工程化交付：Jenkins CI/CD流水线、Docker三服务编排、MySQL深度优化、AST静态检查、k6压测、稳定性验证 | ⏳ 规划中 |

详细需求基准见 [PROJECT_SPEC.md](PROJECT_SPEC.md)，core 层架构设计见 [docs/core_architecture.md](docs/core_architecture.md)。
