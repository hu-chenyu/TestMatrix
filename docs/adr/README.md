# 架构决策记录（ADR）索引

> 格式参考 [MADR](https://adr.github.io/madr/)（Markdown Any Decision Records）精简版。
> 铁律（PROJECT_PLAN 约束 6）：**ADR 即时写**——架构决策发生的当天新建文件，不事后统一补写；
> ADR 的价值是"当时的判断依据与备选方案取舍"与文件时间戳。
> 编号一旦分配不复用；被推翻的决策不删除，新增 ADR 并在旧文件头部标注「已被 ADR-xxxx 取代」。

## 索引

| 编号 | 标题 | 决策日 | 状态 |
| --- | --- | --- | --- |
| [0001](0001-flask-vs-fastapi.md) | Web 框架选型：Flask 2.3 而非 FastAPI | 2026-09-07（Day17，追溯补录于 Day37） | 已接受；Day101 升级 Flask 3.x |
| [0002](0002-redis-queue-design.md) | 任务调度用 LPUSH/BRPOP list 队列而非 pub/sub；event_bus 与 task_queue 并存 | 2026-09-15/09-22（Day25/Day32，追溯补录于 Day37） | 已接受 |

## 模板

```markdown
# ADR-XXXX: 标题

- 状态：提议 / 已接受 / 已废弃（被 ADR-YYYY 取代）
- 日期：YYYY-MM-DD（DayN）

## 背景与问题
## 决策
## 备选方案与取舍
## 后果（正向 / 负向 / 后续动作）
```

## 后续待写（按排期触发，当日建档）

- Day52：真实执行器为何用 subprocess 而非内嵌 pytest API
- Day72：pytest 插件 vs 钩子的选型取舍
- Day91：Jenkins 与 GitHub Actions 的分工
- AST 量化实验后：为何覆盖映射为主而非纯 AST
