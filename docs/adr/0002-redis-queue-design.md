# ADR-0002: 任务调度用 LPUSH/BRPOP list 队列而非 pub/sub；event_bus 与 task_queue 并存

- 状态：已接受
- 日期：2026-09-15（Day25，event_bus）/ 2026-09-22（Day32，task_queue）；追溯补录于 2026-09-27（Day37）

## 背景与问题

平台有两类不同的实时性需求，早期设计一度设想"Day32 用 Redis pub/sub 统一替换内存事件通道"：

1. **任务调度**：trigger 接口产生"执行某批次"的任务，需要可靠地被 worker 取走执行，不能丢。
2. **执行事件**：批次执行过程中向前端 SSE 推送 batch_start/case_finished/batch_finished，
   订阅者可以多个（多标签页），允许在通道终态后走 DB 快照补发。

## 决策

- **任务调度**：Redis **list**（LPUSH 入队 + BRPOP 阻塞出队，FIFO），见 `src/core/task_queue.py`。
- **执行事件**：保留**进程内事件通道** `src/core/event_bus.py`（有界历史环 + Condition 多订阅广播），
  不替换为 pub/sub。两者并存、职责正交。
- 队列与缓存均**默认关闭、故障静默降级**：Redis 不可用时队列 enqueue 返回 False，
  路由回退 Day24 裸线程链路，任务不丢、接口不 500。

## 备选方案与取舍

| 方案 | 问题 |
| --- | --- |
| Redis pub/sub 做任务调度 | **消息不持久**：订阅者不在线（worker 重启/BRPOP 间隙）消息直接丢失，fire-and-forget，不满足"任务绝不丢" |
| Redis Stream（XADD/XREADGROUP） | 持久、可 ACK、支持消费组，但引入 pending entries list/PEL/CLAIM 运维概念；当前单 worker + 个人项目规模用不到，属过度设计，留作未来多机扩展位 |
| Celery/RQ 等成熟队列 | 重型依赖（broker + 后端 + 独立进程模型），与本项目"默认关闭、零外部依赖可离线跑"定位冲突；学习成本高于自研收益 |
| event_bus 也改 Redis pub/sub | 单进程部署下 SSE 响应与后台线程同进程，内存通道零网络开销；pub/sub 不保留历史，Last-Event-ID 回放仍要自己实现（现在由有界环+DB 重建承担），替换无收益 |

选择 list 队列的关键理由：LPUSH/BRPOP 天然 FIFO、消息被 BRPOP 取走前持久在 list 中、
实现量小且语义刚好够用；Redis 故障有裸线程兜底。

## 后果

- 正向：任务不丢（持久取走）、事件不重（DB 为权威态，event_bus 只做实时扇出）；
  默认关闭保证 422 条既有测试与离线体验零回归；单 worker 串行契合 SQLite 单文件写锁。
- 负向：单 worker 无水平扩展能力；进程重启时 list 中的任务 payload 仍在但 worker 需自行恢复
  （当前 payload 含 cases 快照，重启后可重新消费，批次状态以 DB 为准）。
- 后续动作：worker 多机部署成为真实需求时，演进路径为 list → Stream（消费组）或切换 Celery，
  届时新建 ADR；event_bus 的替换触发条件是"worker 与 web 不在同一进程"。
