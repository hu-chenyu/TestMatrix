# Day34 讲述材料：Web 后端 + Redis（从裸线程到队列的工程化）

> 用途：Day34 阶段复盘讲述大纲。配套文档：[web_backend_redis_architecture.md](../web_backend_redis_architecture.md)

## 0. 一句话定位

Web 阶段把 core 能力 HTTP 化，并完成一次调度模型升级：
**Flask 工厂 + 蓝图分层提供 JSON API/SSE/页面；Redis 以"默认关闭、故障静默"的方式
接入缓存与任务队列，trigger 从一批次一裸线程升级为 LPUSH/BRPOP 生产者-消费者。**

---

## 1. 分层职责图（文字版）

```
浏览器（Bootstrap5 页面 + ECharts）
  │ JSON（统一响应 code/message/data）   │ SSE（text/event-stream）
  ▼                                      ▼
Flask create_app（应用工厂）
  ├── 配置三环境（dev/test/prod，TM_ENV 切换）
  ├── 蓝图：base / cases / executions / reports / pages
  │        (+ Day41 notifications：/api/notifications/history|dead-letters)
  ├── 全局异常处理（APIError/404/405/500，TESTING 双模式错误信息）
  ├── 请求钩子（before/after 日志配对，perf_counter 毫秒耗时，安全头）
  └── start_worker + atexit（队列消费线程生命周期）
  ▼
core 层（case_manager / report_analyzer / notification）
  │
  ├── SQLite（权威业务事实：批次表/明细表/汇总表/死信/历史）
  └── Redis（可选，默认关）
        ├── cache.py    缓存层：懒连接/fake:// 测试协议/TTL/SCAN 失效/静默降级
        └── task_queue.py 队列层：LPUSH 入队/BRPOP 单 worker 串行消费/hash 状态镜像
```

## 2. 接口全貌

- Day34 交付时 4 蓝图 19 个 HTTP 接口 + SSE；Day41 起为 5 蓝图 21 接口。
- 分页全接口统一：page/page_size（1-100），响应
  `{items,total,page,page_size,total_pages}`。
- 页面路由（pages_bp，无 /api 前缀）与 JSON 接口严格分离；根 / 返回 JSON 版本信息。

## 3. 核心数据流：trigger 双通道

```
POST /api/executions/trigger
  → start_execution：筛选用例 + 落 pending 批次行（权威状态先行持久化）
  → 队列开关开？
      是 → task_queue_client.enqueue（LPUSH tm:queue:tasks）
             成功 → 立即 202
             失败（Redis 故障）→ fallback 起裸线程，任务不丢，接口不 500
      否 → 直接起裸线程（Day24 原链路，370+ 既有测试零回归）
  → 单 worker 线程 BRPOP 串行取任务
      置 running（Redis hash 镜像）→ 复用 CaseManager._execute_batch_async
      （明细落库 / SSE 埋点 / 通知旁路 / 缓存失效 全部零改动）
      → 以 SQLite 批次表状态裁定终态 → hash 置 finished/failed
客户端：status 轮询 与 /events SSE 双通道取结果
```

payload 与真实签名严格对齐：`{execution_id, cases, executor_kind}`，
worker 不重复筛选/落批次行，只消费编排。

## 4. SSE 实时推送（重点节）

- 选型：单向推送 + 原生 HTTP + 浏览器自动重连，不需要 WebSocket 的双向能力。
- `EventChannel`：一个批次一条通道；`deque(maxlen=1000)` **有界历史环**
  （非消费式，只追加不弹出）+ Condition；publish 在锁内分配通道级
  单调递增 event_id，各订阅者独立游标，多标签页互不分流。
- 四分支收敛保证流一定关得上：
  1. 批次不存在 → 404（普通 JSON，不是 SSE 帧）；
  2. 通道不在注册表（pending 极早期/CLI 批次/已清理）→ finished 从 DB
     重建完整序列，failed/pending 单发快照；
  3. 通道在但已终态（竞态窗口）→ snapshot 补发积压 + 缺终态补帧；
  4. 运行中 → 实时转发，0.5s 等待节拍、15s 心跳注释帧保活。
- Last-Event-ID 断点回放；心跳注释帧不推进游标。
- 线程安全：历史环/event_id/游标全在锁内；subscribe 的 yield 在锁外，
  慢消费者不阻塞生产方；跨线程只传纯 dict，不借请求线程 Session。

## 5. Redis 缓存层

- 默认关闭：接入时已有 360+ 测试，开关一切即回退，CI/开发机零 Redis 依赖。
- 懒连接 + `fake://` 测试协议（fakeredis，decode_responses 全 str）。
- 七维参数 sha1 造 key；空结果也缓存（防穿透）；SETEX TTL 300s 兜底一致性。
- 写后主动失效：SCAN 游标匹配前缀逐个 DEL（幂等），**禁止 KEYS**
  （O(n) 阻塞单线程）。
- RedisError 全部静默降级直查 DB，接口不 500。

## 6. 量化收益（真实 Redis 5.0.14.1，1000 用例 + 100 批次，预热 10 + 正式 200 次）

| 场景 | 平均延迟 | QPS 变化 |
| --- | --- | --- |
| 用例列表 | 3.085ms → 1.359ms（-55.9%） | 324 → 736（+127%） |
| 报告 summary | -58.4% | +140.4% |
| Dashboard 五接口轮询 P95 | 14.552ms → 1.801ms（-87.6%） | 148 → 860（+482.8%） |
| 10 线程并发列表 | — | 258 → 523（+102.6%） |

正确性三项全过：空结果防穿透、写后主动失效、TTL 到期兜底。

## 7. 关键设计决策

1. **trigger 返回 202 而不是同步等**：分钟级任务占 HTTP 连接必被代理超时切断；
   pending 批次行先行落库，结果走轮询/SSE。
2. **单 worker 串行**：SQLite 单文件写锁，多 worker 只是把锁竞争推给 DB；
   串行还天然保证同批次明细写入顺序。多 worker 留给 MySQL 阶段。
3. **队列与缓存共享 URL、两个独立单例**：生命周期/开关/命名空间互不污染。
4. **任务状态 hash 只是镜像**：调度层快查，权威状态永远在 SQLite 批次表。
5. **触发来源服务端裁定为 web**：不信请求体，防伪造 trigger 来源。
6. **缓存默认关、队列故障 fallback**：Redis 永远不是可用性单点。

## 8. 踩坑与教训

- SSE 500 假象：探针未 init_db 表不存在导致 500；测试环境建表后正确返回 404——
  排查问题先区分"代码错"还是"环境没初始化"。
- close 通道先摘注册表再标记 closed：drain 语义让持有旧引用的订阅者读完残余事件。
- worker 异常不能杀死消费循环：任务级 try 全兜底，循环只认 stop_event。
- BRPOP 必须有限超时（1s）：永久阻塞会让停止信号永远无法响应。

## 9. Q&A 预设

- **Q：为什么不上 Celery？**
  A：单文件 SQLite + 秒级任务，单 worker 串行足够；broker 的运维成本大于收益，
  扩展点预留在多 worker + MySQL 阶段。
- **Q：Redis 挂了平台还能用吗？**
  A：能。缓存降级直查 DB；队列 enqueue 失败 trigger 回退裸线程，任务不丢。
- **Q：进程重启队列里的任务怎么办？**
  A：LPUSH 的 payload 仍在 Redis 可被新 worker 消费；pending 批次行已在 SQLite，
  状态可查（重启不丢状态是对裸线程模式的核心改进）。
- **Q：SSE 为什么心跳用注释帧？**
  A：`: heartbeat` 不携带 event/id，客户端静默忽略且不污染断点回放游标。
- **Q：SCAN 弱一致会不会删不干净？**
  A：迭代开始前存在的匹配 key 保证可见；迭代期间新增 key 由 TTL 兜底，DEL 幂等。

## 10. 收尾数字锚点

- 5 蓝图 21 接口（Day41 后）；SSE 心跳 15s、等待节拍 0.5s、历史环 1000 事件。
- 缓存 TTL 300s；key 前缀 `tm:`；队列默认 key `tm:queue:tasks`，BRPOP 超时 1s。
- Dashboard 轮询 P95 降 87.6%、QPS +482.8% 是本阶段最硬的量化结果。
