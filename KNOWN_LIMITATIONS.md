# KNOWN_LIMITATIONS（已知能力边界）

> 本文件随里程碑沉淀更新。每个能力边界如实标注，不做无证据承诺。

## 1. 任务队列默认单 worker 串行

刻意取舍（个人项目 + SQLite 写锁），队列消费层不做多 worker 并发。
执行并发由 pytest-xdist（`-n`，并发执行规划中）承担，队列层再做消费多 worker 是第二套并发，
两套并发 + SQLite 写锁会制造复杂度。详见 `task_queue.py` 注释。

## 2. 串口 / Telnet 为模拟验证（无真机）

串口用 pyserial `loop://` 伪串口、Telnet 用本地 socket 模拟验证；
暂无真机设备验证条件，真机适配需在具备设备后按实测补验。

**Python 版本兼容性**：`telnetlib` 已在 Python 3.13 移除（PEP 594），
当前 Telnet 客户端在 3.13+ 下建立连接时（`connect()`）抛 `RuntimeError` 明确指引迁移（非裸崩），
主链路（执行/解析/报告/Web/队列）不受影响。该降级分支当前未被 CI 覆盖（`pragma: no cover`，
当前锁定 Python 3.11），D74 迁移时需一并补用例。迁移至 `telnetlib3` 或 socket 自实现
排 v3.6 Day74（与 Python 升级同批）。

## 3. 不迁 FastAPI / asyncio

见 ADR-0001。当前 Flask 同步模型已满足单机工具定位，
不引入 asyncio 复杂度（并发由 xdist 承担，不需要应用层异步）。

## 4. AST 精准回归为脚本级实验

不做平台功能（pytest-testmon / Develocity 已产品化），
仅做覆盖映射 + import 拓扑 + 影响面反查脚本，召回率上限随实验公开。

## 5. 本地开发库 schema 迁移

SQLite 本地库的 schema 迁移当前通过 `src/db/migration.py` 重建库方案执行，
全新 clone 用户首次运行 `init_db()` 自动建表。
后续如引入迁移工具（Alembic）再改用轻量 ALTER 迁移（当前未排期）。

## 6. Windows 本地全量 pytest 偶发 allure 目录竞态（时序敏感 flake）

**现象**：Windows 本机全量运行时，`tests/test_day44_bugfix_demo.py::test_real_subprocess_failed`
偶发判定为 `error` 而非 `failed`（嵌套 pytest 与外层 allure 写盘竞争固定目录
`output/allure_results`，触发 `--clean-alluredir` 互删/互占 → INTERNALERROR，退出码 3 被归为 error）。

**复现特征**：非确定性时序竞争——两次普通串行全量（无 `-n` 并行）实测 1 败（170.73s）
1 绿（231.52s），与耗时无单调关系、与并行无关；CI（ubuntu-24.04）不受影响（文件系统
时序差异）。嵌套 pytest 实际写入外层默认目录，per-run alluredir 覆盖疑似未生效
（D53 首查项）。`pytest.ini` L36-37 已记录 WinError 145 已知问题与手动处置
（删目录后重跑）。

**修复排期**：v3.6 Day53（conftest per-run 唯一 alluredir，消除嵌套 pytest 与外层
会话的目录竞争；验收必须包含"嵌套 pytest 生效独立 alluredir、不清理外层目录 +
Windows 本机连续 3 次全量 1473 全绿"）。在 D53 修复前，Windows 本地全量以串行
执行为准，偶发失败重跑即可；不得因此调整 CI 基线或测试数。

## 7. Web 层无鉴权（单机 / 受信网络定位）

本条**不是新披露**：无鉴权这一边界此前已声明四处，但分散在 API 文档首屏、运行时告警、
上传上限注释和 SSE 并发审查记录里——只读本页的人看不到。本节做**集中汇总**，并把
威胁模型的准确边界写清：它既不是"没处理"，也不是远程代码执行。

**已声明无鉴权的四处（均为既有声明，本节不新增口径）**：

1. `docs/API.md:6` 首屏：「**鉴权说明**：当前版本无认证，仅内网/本机使用，禁止暴露公网。」
2. `run.py:132` 非回环绑定时打印运行时告警：「本服务无任何鉴权，仅限可信内网部署，
  禁止暴露公网！」（`_warn_non_loopback_binding()`，**只告警、不阻断启动**；
  回环判定见 `run.py:127` `loopback_hosts = {"127.0.0.1", "localhost"}`）
3. `src/web/config.py:54-56` 上传大小硬上限处注释：「平台当前无鉴权，风险直接可利用」
4. `src/web/routes/executions.py:972-974`（审查编号 **P1-02**）记录了一条**无凭证拒绝
  服务链**：触发一个长批次 + N 条 EventSource 占满工作线程；已加两道闸
  （`SSE_MAX_LIFETIME_SECONDS` 单流存活上限、`SSE_MAX_CONNECTIONS_PER_IP` 单 IP
  并发订阅超限返 429）

**代码层现状**：`src/web` 全层无鉴权守卫——`app.py:134` 的 `before_request` 只记请求
日志与开始时间，未做身份校验；全层搜 `login_required` / `api_key` / `Authorization` /
`check_token` 零命中。多租户鉴权是**刻意砍掉的 scope**，不是遗漏。

**威胁模型的准确边界**（容易被高估为 RCE，也容易被低估为"没处理"）：
`POST /api/executions/trigger` 是**异步入队**接口（`executions.py:327` docstring 明写
"异步"，`:345` 落 pending 批次行后入队），真正拉起子进程的是 worker 侧
`executors.py:1196` 的 `subprocess.Popen`，且全程**无 `shell=True`**
（`data_driver.py:464` 已自注"不是 RCE——无 shell=True，且 case_id 正则本就为防选项注入"）。

因此未授权访问的实际后果是 **DELETE 用例（数据破坏）+ 批次创建与资源消耗（含上述拒绝
服务链）+ 触发已配置的真实通知渠道外发**，**不是任意命令执行**。注入面已收敛，
缺的是**未授权访问**这一层。

**修复点**：若要把服务暴露到内网或多用户复用，第一步是在 `trigger` / `import` / `delete`
三个接口前加单 token 网关——统一在 `before_request` 里校验，成本远低于逐接口加装饰器。
已排为 P0。
