# ADR-0001: Web 框架选型——Flask 2.3 而非 FastAPI

- 状态：已接受（维持 Flask 2.3 冻结；2026-10 v3.6 共识取消 3.x 升级，D75 改冻结期 CVE 核查）
- 日期：2026-09-07（Day17）；追溯补录于 2026-09-27（Day37）

## 背景与问题

Web 后端（Day17 启动）需要一个 Python Web 框架承载：多页面后台（Jinja2 服务端渲染）
+ JSON API（19 个接口）+ SSE 实时日志流 + 后台异步任务触发。候选为 Flask 与 FastAPI。

## 决策

选用 **Flask 2.3**（应用工厂 + 蓝图组织，Jinja2 模板渲染，SSE 走 stream_with_context），
不引入 FastAPI。

## 备选方案与取舍

| 维度 | Flask 2.3（选用） | FastAPI（放弃） |
| --- | --- | --- |
| 场景匹配 | 同步 CRUD + SSE + Jinja2 多页面后台，全部命中其舒适区 | 强项在 async 高并发与 OpenAPI 自动文档，本项目均非刚需 |
| 模板渲染 | Jinja2 原生一体 | 需另配模板方案，多页面后台不是其设计重心 |
| SSE | generator + stream_with_context 直接实现 | 需 StreamingResponse，async 栈反而增加心智负担 |
| 学习/维护成本 | 生态成熟、资料多、同步模型简单 | async/await 全链路传染，团队收益不抵成本 |
| 数据校验 | marshmallow（已在 web 层使用） | pydantic 开箱即用——但本项目 core 层数据模型以 SQLAlchemy/marshmallow 为主，再引一套校验体系增加分裂 |

## 后果

- 正向：同步栈与后台线程模型（`_execute_batch_async`/event_bus/task_queue worker）天然契合；
  SSE 三分支（实时订阅/终态补发/DB 重建）实现直接；依赖面小。
- 负向：Flask 2.3 自 3.0（2023-09）起无安全 backport；无自动 OpenAPI（API.md 人工维护）。
- 后续动作：经 v3.6 共识调整，Flask 大版本冻结，D75 仅做 Werkzeug/Jinja2/itsdangerous/blinker CVE 核查，不升级 3.x（升级收益低于回归风险）；3.13 兼容性在 Day74 Python 升级窗口统一验证。
