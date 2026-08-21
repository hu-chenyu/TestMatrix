# TestMatrix 通用自动化测试效能平台

> 个人中型测试效能平台项目，面向互联网接口测试与芯片嵌入式板卡测试双赛道，
> 覆盖自动化框架设计、数据驱动、测试报告、数据持久化与DevOps工程化全链路。

## 核心特性

- **多协议统一封装**：HTTP（Requests）/ 串口（pyserial）/ Telnet 三协议客户端统一封装，
  芯片板卡测试仅需低成本适配即可接入
- **数据驱动**：YAML / Excel 外部数据文件参数化，用例与数据彻底分离
- **增强断言库**：状态码、JSON点号路径取值、字段子集、响应耗时、包含关系等
  10+ 类开箱即用断言，失败信息自带完整上下文
- **全链路日志**：Loguru三通道输出（控制台/全量/错误独立），按天切割、trace_id用例级追踪
- **数据持久化**：SQLAlchemy 2.0 ORM，用例、执行记录、缺陷统计三张核心表，
  SQLite（本地）与MySQL（团队共用）一键切换
- **企业级报告**：Allure 2.x 报告（分级/步骤/附件/环境信息），失败详情自动附加
- **工程化交付**：Docker镜像构建、Docker Compose编排、K8s部署示例、k6性能测试示例

## 技术栈

| 分层 | 技术选型 |
| --- | --- |
| 核心语言 | Python 3.11 |
| 测试框架 | pytest 7.4 + allure-pytest + pytest-rerunfailures + pytest-cov |
| 协议层 | Requests / pyserial / telnetlib |
| 日志报告 | Loguru / Allure 2.x |
| 数据驱动 | PyYAML / openpyxl |
| 数据层 | SQLAlchemy 2.0（SQLite 3 / MySQL 8.0） |
| Web平台 | Flask 2.3 + Jinja2 + Bootstrap 5 + ECharts 5 |
| 工程化 | Git / Jenkins / Docker / Docker Compose |

## 快速开始

### 环境要求

- Python 3.11.x
- （可选）Allure命令行工具（生成HTML报告用）：[安装指引](https://allurereport.org/docs/install-for-windows/)

### 安装与运行

```bash
# 1. 安装依赖
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 配置环境变量（本地开发可直接使用默认值，跳过本步）
copy .env.example .env   # Windows
cp .env.example .env     # Linux/Mac

# 3. 运行测试（Demo用例内置本地模拟服务，零外部依赖、可离线执行）
pytest                  # 全量用例
pytest -m smoke         # 仅冒烟用例

# 4. 生成并打开Allure HTML报告（需已安装allure命令行）
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
│   ├── db/                 # 数据持久层（ORM模型 + 会话管理）
│   ├── core/               # 平台核心逻辑（数据驱动/用例调度/报告解析）
│   └── web/                # Flask Web可视化平台
├── tests/                  # pytest测试用例
│   ├── api_demo/           # HTTP接口测试Demo（登录/用户查询/健康检查）
│   └── chip_demo/          # 芯片板卡测试Demo（预留）
├── testdata/               # 数据驱动测试数据（yaml/ excel/）
├── output/                 # 运行产物（日志/Allure结果/报告，不入Git）
├── examples/               # 扩展Demo（K8s部署YAML、k6性能脚本）
├── docker/                 # 容器化配置（Dockerfile、docker-compose.yml）
├── docs/                   # 项目文档
├── pytest.ini              # pytest核心配置
├── .env.example            # 环境变量模板
├── requirements.txt        # Python依赖清单
└── PROJECT_SPEC.md         # 项目规格说明书（开发基准）
```

## 阶段规划

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 第一阶段 | 架构基座：目录骨架、common封装层、数据持久层、pytest体系、Demo验证 | ✅ 已完成 |
| 第二阶段 | 核心能力：数据驱动引擎、用例调度、报告解析、Flask可视化平台 | 开发中 |
| 第三阶段 | 工程化：Jenkins CI/CD流水线、邮件通知、K8s部署落地 | 规划中 |

详细需求基准见 [PROJECT_SPEC.md](PROJECT_SPEC.md)。
