# Day16 讲述材料：通知模块（旁路能力的可靠性设计）

> 用途：Day16 阶段复盘讲述大纲。配套文档：[notification_architecture.md](../notification_architecture.md)

## 0. 一句话定位

通知是测试执行主流程的**旁路能力**：批次执行与报告生成永远不因通知渠道故障而受影响。
在此前提下做到——渠道可插拔、策略可配置、失败可重试、耗尽有死信、事后可回溯。

---

## 1. 六天演进路径（讲模块怎么长出来的）

| 天 | 交付 | 核心类/方法 |
| --- | --- | --- |
| Day10 | 抽象基座 + 邮件通知器 | `Notification` / `BaseNotifier` / `EmailNotifier` |
| Day11 | HTML 邮件报告模板 | `EmailReportTemplate`（内联 CSS 六区块） |
| Day12 | 企业微信机器人 | `WeChatNotifier`（markdown + URL 脱敏） |
| Day13 | 分级策略 + @负责人 + 路由器 | `NotificationRouter` |
| Day14 | 指数退避重试 + 死信 | `_send_with_retry` / `NotificationDeadLetterRepository` |
| Day15 | 执行链路集成 | `build_notification_statistics` 适配器 / CLI `--notify` |
| Day41 | 发送历史留痕 + 查询 API | `NotificationHistory` / `/api/notifications/*` |

## 2. 类关系图（文字版）

```
Notification（统一消息结构：title/content/level/pass_rate/extra…）
      ▲
BaseNotifier（ABC）：channel_name / send()* / is_enabled() / build_notification()
   ├── EmailNotifier   smtplib；465→SMTP_SSL，587→STARTTLS，其他→明文；finally quit()
   └── WeChatNotifier  webhook markdown；HTML→markdown 降级；URL 日志脱敏
EmailReportTemplate（render 静态入口，内联 CSS 六区块，通过率阈值着色）

NotificationRouter（策略与重试都在这里，渠道只做单次尝试）
   ├── should_notify(stat, strategy)   all 恒发 / failed_only 有失败才发
   ├── collect_owners(stat)            owner 标签去重 + 手机号分流 + @all
   ├── notify(stat, execution_id)      构造分发 → 逐渠道 _send_with_retry
   ├── _send_with_retry(notifier, n)   1/2/4/8s 退避 + 可选 jitter
   ├── _save_dead_letter(...)          重试耗尽 → notification_dead_letters
   └── _save_history(...)              Day41：成功/进死信 → notification_history

NotificationDeadLetterRepository  ←→ ORM: notification_dead_letters（全文留痕）
NotificationHistoryRepository     ←→ ORM: notification_history（一行一条结果）
```

## 3. 核心数据流（批次完成 → 送达/死信/历史）

```
run_batch 汇总完成（notify=True）
  → CaseManager.notify_execution_result        第一层 try 兜底
  → build_notification_statistics 适配器
       test_executions 明细 → AllureResult（error→broken，秒→毫秒）
       → ReportStatistics.aggregate（唯一统计入口）
  → NotificationRouter.notify                  第二层防护
       strategy 判定（all / failed_only）
       → collect_owners → _build_channel_notifications（邮件 HTML / 企微 markdown）
       → 每个渠道 _send_with_retry：
            成功 → _save_history(status=success) → results[channel]=True
            失败 → 退避重试 → 耗尽 → _save_dead_letter（全文）
                                  + _save_history(status=dead_letter)
            渠道未启用（配置性跳过）→ 不写历史（它不是一次真实发送尝试）
  → 返回结果字典；不影响 run_batch 返回值
```

第三层兜底：死信/历史写入自身失败（DB 挂）只记 error 日志——旁路的旁路也不炸主流程。

## 4. 关键设计决策

1. **send 契约：单次尝试、吞异常、返回 bool、绝不外抛**（ABC 强制）。
2. **重试归 Router 不归渠道**：send 保持纯语义，N 个渠道零重复重试代码。
3. **指数退避 1/2/4/8s + 可选 jitter**：给故障方喘息；jitter 防多实例雪崩；
   默认关 jitter 保证测试确定性；最后一次失败不空等。
4. **SMTP 端口自适应**：一套代码覆盖生产 SSL / 云厂商 TLS / 本地 MailHog 明文。
5. **邮件 HTML 全内联 CSS + 600px**：Outlook/Gmail 过滤 `<style>`，内联是事实标准。
6. **企微 @人走 payload 注入**：`mentioned_list`/`mentioned_mobile_list` 顶层字段；
   markdown 正文里写 `<@xxx>` 不会触发提醒。
7. **死信表存全文 Text，历史表存摘要**：死信供人工重发（content 不截断），
   历史供列表回溯（subject 截断 256）；两表互补不重复建设。
8. **适配器复用 aggregate**：通知数字与 Web 看板、汇总表同源。
9. **sleeper/repo 构造注入**：测试零真实 sleep、零真实网络；坏 repo 验证兜底。

## 5. 可观测性（Day41 补齐）

- `GET /api/notifications/history`：分页 + channel/status/execution_id 筛选，
  按 created_at 倒序；空库返回空列表。
- `GET /api/notifications/dead-letters`：分页 + channel 筛选，最新在前，
  content 完整返回。
- 两接口均只读：重放是扩展位（status=resent），不提前实现，避免误触发重复通知。

## 6. 踩坑与教训

- 企微不支持 HTML：早期直接把邮件 HTML 塞进 markdown，标签原样展示；
  `_convert_to_markdown` 去标签 + 还原实体 + 压缩空行才可用。
- webhook key 进日志等于泄密：日志只打前 30 字符，脱敏从第一天就做。
- SMTP 连接泄漏：长跑进程异常路径不 quit() 会耗尽连接，finally 强制释放。
- "成功也不留痕"的盲区：只有死信可查时无法回答"哪天发过什么"，
  Day41 补 notification_history 才闭环。
- 重试 sleep 让测试变慢且抖动：sleeper 注入后测试秒级、确定性。

## 7. Q&A 预设

- **Q：通知失败为什么不抛异常让调用方知道？**
  A：抛异常会让一个已成功的执行批次整体报错；失败信号经返回值 + 死信表 +
  历史表三处留痕，可观测而不反噬主流程。
- **Q：为什么重试不放进每个 Notifier？**
  A：重试是发送策略不是渠道能力；归 Router 后策略（次数/退避/jitter）单点可调。
- **Q：jitter 为什么默认关闭？**
  A：生产建议开（防雪崩），测试必须关（防随机抖动导致断言不稳定）。
- **Q：死信为什么不做自动重放？**
  A：自动重放可能在故障恢复瞬间形成通知风暴；先留痕，重放作为后续显式操作。
- **Q：新增钉钉渠道要改几处？**
  A：继承 BaseNotifier 实现 send/is_enabled，注册进 Router notifiers 列表即可；
  策略、重试、死信、历史全部复用。
- **Q：failed_only 策略下全部通过会发生什么？**
  A：should_notify 返回 False，整路跳过，不写历史（没有发送尝试发生）。

## 8. 收尾数字锚点

- 2 个真实渠道 + 1 个模板；重试序列 1/2/4/8（默认 3 次重试，总尝试 4 次）。
- 死信 fail_reason / 历史 subject 截断：1000 / 256 字符。
- 企微请求 10s 超时；Day41 新增 10 条 API+落库测试，mock notifier 全程不触网。
