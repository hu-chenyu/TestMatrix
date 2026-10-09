# KNOWN_LIMITATIONS（已知能力边界）

> 本文件随里程碑沉淀更新。每个能力边界如实标注，不做无证据承诺。

## 1. 任务队列默认单 worker 串行

刻意取舍（个人项目 + SQLite 写锁），队列消费层不做多 worker 并发。
执行并发由 pytest-xdist（`-n`，Day58 接入）承担，队列层再做消费多 worker 是第二套并发，
两套并发 + SQLite 写锁会制造复杂度。详见 `task_queue.py` 注释。

## 2. 串口 / Telnet 为伪串口 + socket 模拟

当前无真机验证：串口用 pyserial `loop://` 伪串口、Telnet 用本地 socket 模拟。
三协议统一用例模型已落地，但真机板卡测试能力为架构预留，待有真机时验证。

## 3. 不迁 FastAPI / asyncio

见 ADR-0001。当前 Flask 同步模型已满足单机工具定位，
不引入 asyncio 复杂度（并发由 xdist 承担，不需要应用层异步）。

## 4. AST 精准回归为脚本级实验（Day94-95）

不做平台功能（pytest-testmon / Develocity 已产品化），
仅做覆盖映射 + import 拓扑 + 影响面反查脚本，召回率上限随实验公开。

## 5. 本地开发库 schema 迁移

SQLite 本地库的 schema 迁移当前通过 `src/db/migration.py` 重建库方案执行，
全新 clone 用户首次运行 `init_db()` 自动建表。
后续如引入迁移工具（Alembic）再改用轻量 ALTER 迁移（当前 119 版计划未排期）。
