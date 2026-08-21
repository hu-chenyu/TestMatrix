# TestMatrix 通用自动化测试效能平台 - 项目规格说明书

> 本文档为全程开发基准（Baseline），任何阶段的功能范围、技术选型、目录结构变更
> 均须以本文档为准；确需变更时先更新本文档再实施。

## 1. 项目定位

本项目为通用技术能力补充项目，与职场正式业务项目形成互补：公司项目负责体现芯片测试
的业务深度，本项目负责体现自动化框架设计、平台化建设、DevOps工程化等通用技术能力。

- **目标岗位**：3-5年社招测试开发工程师
- **核心目标**：简历ATS关键词高命中、面试具备完整技术深度
- **双赛道**：互联网接口测试（核心主线）+ 芯片嵌入式板卡测试（低成本扩展加分项）
- **规模控制**：单人6个月可完成的中型规模，不做大型平台
- **芯片适配边界**：仅完成基础通信封装层（串口/Telnet）并预留扩展能力，不做核心主线
- **质量标准**：工业级工程规范，超出普通练手项目水平，匹配中级测开岗位招聘标准

## 2. 技术栈（严格遵循，不随意增减核心组件）

| 分类 | 技术 |
| --- | --- |
| 核心语言 | Python 3.11 |
| 测试框架 | pytest 7.4.x、allure-pytest、pytest-rerunfailures、pytest-cov |
| 协议层 | Requests（HTTP）、pyserial（串口）、telnetlib（Telnet网口） |
| 日志报告 | Loguru、Allure 2.x |
| 数据驱动 | PyYAML、openpyxl |
| 数据层 | SQLAlchemy 2.0 ORM，默认SQLite 3本地开发，兼容MySQL 8.0一键切换 |
| Web平台 | Flask 2.3后端、Jinja2模板、原生HTML5/CSS3/JS、Bootstrap 5、ECharts 5 |
| 工程化 | Git、Jenkins、Docker、Docker Compose |
| 扩展Demo（非核心） | Kubernetes部署YAML、k6性能测试脚本 |
| 辅助工具 | python-dotenv、smtplib邮件推送、marshmallow数据校验 |

## 3. 标准目录结构

```
TestMatrix/
├── src/                            # 核心源码目录，平台全部业务逻辑
│   ├── common/                     # 公共底层封装层
│   │   ├── http_client.py          # HTTP统一封装（超时/重试/脱敏日志/异常包装）
│   │   ├── serial_client.py        # 串口通信封装（芯片板卡适配层）
│   │   ├── telnet_client.py        # Telnet网口封装（芯片板卡适配层）
│   │   ├── logger.py               # Loguru统一封装（双输出/按天切割/分级/trace_id）
│   │   ├── assertion.py            # 通用增强断言库
│   │   └── env_manager.py          # 多环境配置管理（dev/test/prod切换）
│   ├── db/                         # 数据持久层
│   │   ├── models.py               # ORM模型（用例/执行记录/缺陷统计核心表）
│   │   └── db_session.py           # 会话管理（SQLite/MySQL双模式切换）
│   ├── core/                       # 平台核心逻辑层
│   │   ├── data_driver.py          # YAML/Excel数据驱动引擎
│   │   ├── case_manager.py         # 用例调度与管理
│   │   └── report_analyzer.py      # 测试报告解析与统计
│   └── web/                        # Flask Web后台
│       ├── app.py                  # Web服务入口
│       ├── routes/                 # API路由模块
│       ├── static/                 # 静态资源（JS/CSS/ECharts）
│       └── templates/              # Jinja2页面模板
├── tests/                          # pytest测试用例
│   ├── conftest.py                 # 全局配置（fixture/日志钩子/失败自动记录）
│   ├── api_demo/                   # HTTP接口测试Demo
│   └── chip_demo/                  # 芯片硬件测试Demo（预留）
├── testdata/                       # 数据驱动测试数据
│   ├── yaml/                       # YAML格式测试数据
│   └── excel/                      # Excel格式测试数据（第二阶段启用）
├── output/                         # 运行产物（不入Git）
│   ├── logs/                       # 运行日志
│   ├── allure_results/             # Allure原始结果
│   └── reports/                    # HTML测试报告
├── examples/                       # 扩展Demo（非核心，简历加分项）
│   ├── k8s/                        # K8s部署示例YAML
│   └── performance/                # k6性能测试脚本
├── docker/                         # 容器化配置
│   ├── Dockerfile                  # 平台镜像构建文件
│   └── docker-compose.yml          # 一键编排启动
├── docs/                           # 项目文档
│   └── images/                     # 文档配图
├── pytest.ini                      # pytest核心配置
├── .env.example                    # 环境变量模板（不含真实密钥）
├── requirements.txt                # Python依赖清单（锁定稳定版本）
├── .gitignore                      # Git忽略配置
├── PROJECT_SPEC.md                 # 本规格说明书
└── README.md                       # 精简版对外介绍
```

## 4. 阶段规划

### 第一阶段：架构基座搭建期（本文档基准范围）

优先完成项目骨架与底层核心能力，保证核心链路可运行：

1. 按标准目录结构创建全部文件夹与初始文件
2. 数据持久层：测试用例、测试执行记录、缺陷统计3张核心表SQLAlchemy ORM模型；
   数据库会话封装，支持配置切换SQLite/MySQL
3. common层全部基础封装，每模块职责独立、异常处理完备、日志记录完整
4. pytest基础体系：pytest.ini（用例规则/标记/报告路径）；conftest.py
   （全局fixture/日志钩子/失败自动日志记录）
5. 标准requirements.txt（标注稳定版本）与标准.gitignore
6. 最小可运行HTTP接口Demo用例，可直接执行并生成Allure报告
7. .env.example环境变量模板

### 第二阶段：核心能力建设期（规划）

- data_driver：YAML/Excel数据驱动引擎，marshmallow数据格式校验
- case_manager：用例加载、分级执行（smoke/regression/P0-P3）、批量调度
- report_analyzer：Allure结果解析、指标计算、缺陷统计入库
- web平台：Flask + Bootstrap 5 + ECharts 5可视化看板（用例管理/执行记录/趋势统计）
- 执行结果回写数据库，邮件推送（smtplib）

### 第三阶段：工程化交付期（规划）

- Jenkins流水线（定时冒烟/回归、失败告警）
- Docker镜像构建与Compose编排落地
- K8s部署示例验证、k6性能基线脚本完善
- 覆盖率统计（pytest-cov）接入质量卡点

## 5. 开发规范要求

1. 所有代码严格遵循PEP8规范，模块职责单一，具备良好的可扩展性与可维护性
2. 每个类、核心方法必须添加清晰的中文注释，说明功能、参数、返回值与异常处理逻辑
3. 代码具备生产级容错能力：完善的异常捕获、全链路日志追踪、入参校验，
   禁止无容错的玩具式写法
4. 优先保证核心链路跑通，再细化边缘功能；先完成整体骨架，再填充细节内容
5. 每阶段仅完成当期交付内容，后续阶段功能预留扩展位即可，不超前开发

## 6. 验收标准（第一阶段）

- [x] 目录结构与规格说明书一致
- [x] 3张核心表ORM模型与会话管理可用，SQLite/MySQL可切换
- [x] common层6个模块封装完成，异常处理与日志完备
- [x] pytest体系可运行，失败用例自动记录日志并附加Allure详情
- [x] `pytest`命令可直接执行全部Demo用例并生成Allure结果
- [x] requirements.txt版本锁定、.gitignore、.env.example齐备
