"""
Redis任务队列模块（第三阶段Day32交付）

定位:
  - 生产者-消费者模型替代trigger接口"一批次一裸线程":
   trigger路由只负责筛选用例、落pending批次行并把执行任务
   LPUSH到Redis队列（生产者）；应用内单worker线程BRPOP串行
   取任务执行（消费者），复用CaseManager._execute_batch_async
   既有编排（执行明细/事件/通知/缓存失效逻辑零改动）
  - 默认关闭（TM_TASK_QUEUE_ENABLED=false）: 关闭时trigger仍走
   Day24原裸线程链路，370条既有测试零回归；灰度可随时切回
  - enqueue失败必须fallback裸线程: Redis故障时enqueue返回False，
   调用方据此起裸线程兜底，任务绝不丢、接口绝不500
  - 单worker串行: 个人项目 + SQLite单文件写锁，串行消费即可；
   多worker并发是后续扩展，本版本不做
  - 任务状态走Redis hash快查（tm:task:{execution_id}）:
   pending/queued_at/running/started_at/finished/failed/
   finished_at/error，供队列视角快速观测；批次的权威状态与
   执行明细仍落SQLite（test_execution_batches等表），Redis
   hash只是调度层镜像，不作为业务事实来源

复用基础设施（Day31）:
  - 连接配置复用TM_REDIS_URL（redis://或fake://测试内存实例）
   与同一套懒连接/故障静默/reset_backend测试模式，不新增依赖

payload约定（与_execute_batch_async真实签名严格对齐）:
  {"execution_id": 批次号,
   "cases": start_execution筛选返回的纯字典用例列表,
   "executor_kind": simulated/pytest或None(读TM_EXECUTOR)}
 ——筛选与批次行落库已在trigger请求内由start_execution完成，
   worker不再重复筛选/落批次行，只消费执行编排。

使用示例:
  from src.core.task_queue import task_queue_client, start_worker

  queued = task_queue_client.enqueue(execution_id, payload)
  if not queued:
    # Redis不可用/未启用: 调用方自行fallback裸线程
    ...
"""

import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

import redis

from src.common.env_manager import env_manager
from src.common.logger import LogManager

logger = LogManager.get_logger()

# redis.from_url 本身无类型标注（redis 客户端未提供 py.typed），在 typed
# 上下文直接调用会被 mypy 判为 no-untyped-call。与 src/core/cache.py:61
# 同一处置：先收进一个标注为 Callable[..., Any] 的别名再调用。
_REDIS_FROM_URL: Callable[..., Any] = redis.from_url

# fake://协议标记: 与Day31缓存层同一约定，测试内存实例
FAKE_SCHEME = "fake://"

# 任务状态hash字段名固定值（调度层状态机，仅这四态）
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"

# worker停止时join的最长等待秒数（一批次模拟执行约0.01s/条，
# 3秒足够在途任务收尾；真实长任务由daemon属性保证不阻进程退出）
WORKER_JOIN_TIMEOUT_SECONDS = 3.0

# 任务状态hash的存活秒数（Day44 P3-03）：状态hash只是调度层镜像，
# 权威状态在 SQLite 批次行，批次终态后无需长期保留。取 24 小时是
# 为了覆盖"跨夜未消费的积压任务仍可查到调度状态"这一运维排查场景。
TASK_STATUS_TTL_SECONDS = 24 * 3600

# 后端构建失败后的冷却秒数（Day44 热修 P1-1）
# 取 5 秒的依据：足够把"构建失败"从"每微秒一次"降到"每 5 秒一次"
# （实测热转时为 8871 次/秒，冷却后为 0.2 次/秒，约 4.4 万倍降幅），
# 又足够短，使运维改完 TM_REDIS_URL 后最多等 5 秒 worker 就自动恢复，
# 无需重启进程。同时它与 WORKER_IDLE_BACKOFF_SECONDS 构成两道独立的闸：
# 冷却管"构建重试频率"，退避管"主循环空转频率"，两者缺一仍会出问题
# （只冷却不退避 -> 不刷屏但空转；只退避不冷却 -> 不空转但仍刷屏）。
BACKEND_FAILURE_COOLDOWN_SECONDS = 5.0

# worker 主循环在后端不可用时的退避秒数（Day44 热修 P1-1）
# 取 1 秒的依据：与 BRPOP 自身的 brpop_timeout(默认1s) 同量级，
# 保证"后端正常时每 1 秒醒一次"与"后端不可用时每 1 秒醒一次"的节奏一致，
# 不会因为加了退避而让正常情况下的消费变慢（正常路径不进入该分支）。
WORKER_IDLE_BACKOFF_SECONDS = 1.0

# BRPOP 阻塞超时的**最小有效值**（Day45 全量审查 问题3）。
#
# 事实校准：redis-py 5.0.8 的 `Redis.brpop(keys, timeout: int = 0)`，
# 其 docstring 原文为 "If timeout is 0, then block indefinitely."——
# **0 的语义是永久阻塞，不是"不阻塞立即返回"**（修复前本方法的 docstring
# 写的恰好相反）。于是 `TM_TASK_BRPOP_TIMEOUT=0` 会让 worker 永久卡在
# brpop 上，`stop_event` 始终得不到检查，优雅停止失效。
#
# 处置：把 <= 0 的取值一律夹到本常量（0.1s），既保住"近似立即返回"的原意，
# 又让 stop 能在最迟 0.1s 粒度内被响应。不抛 ValueError 是因为
# 配置文件写错不应让 worker 线程直接死掉。
MIN_BRPOP_TIMEOUT_SECONDS = 0.1

# 消费侧故障的指数退避（Day45 全量审查 问题1）。
#
# Day44 热修只给"后端构建失败"加了冷却与退避，漏掉了"后端已构建成功、
# 但运行期连接断开"这条路径：此时 `_backend` 非 None，worker 的退避判据
# `if self.queue_client._backend is None` 为假，直接 continue 形成零延时
# 热转（实测 2 秒 21124 次空转，每轮一条 WARNING）。本组常量把退避依据
# 从"后端是否为 None"换成"本轮 dequeue 是否发生故障"，并按连续失败次数
# 指数递增，单轮上限 30 秒。
WORKER_FAILURE_BACKOFF_BASE_SECONDS = 1.0
WORKER_FAILURE_BACKOFF_CEILING_SECONDS = 30.0

# 幂等抢占 claim 的存活秒数（Day45 全量审查 问题2）。
#
# 场景：enqueue 的 lpush 已在 Redis 生效，但应答因 socket_timeout 超时
# 抛错 → enqueue 返回 False → 路由用同一 execution_id 起裸线程；此时
# 消息仍在队列里等 worker 消费 → 同一批次被执行两次，明细双写、
# 计数翻倍、通过率被污染。以 execution_id 做 SETNX 抢占，后到者直接放弃。
# 取 3600 秒的依据：覆盖最长的单批次执行时长（Day45 设计文档 L1 批次
# 超时同为 3600s），既不误伤同批次的正常重试，又能让崩溃后的批次在
# 一小时后可被重新抢占。
CLAIM_TTL_SECONDS = 3600

# 在途任务（processing list）的最大停留秒数（Day45 全量审查 问题4）。
#
# 无 ack 的后果：BRPOP 弹出即从队列删除，进程被 kill（OOM/部署重启）时
# 批次永久停在 pending/running，无任何补偿路径。改用 BRPOPLPUSH 后任务
# 同时落在 processing list，超过本时长的在途任务会被重新投回任务队列。
# 取 300 秒的依据：模拟执行 0.01s/条，真实执行（Day48 起）单批分钟级，
# 300 秒足以覆盖绝大多数正常批次，同时让崩溃恢复足够快。
STALE_PROCESSING_SECONDS = 300

# ===========================================================================
# 在途归属与心跳（Day51 任务二 a/d）
# ===========================================================================
#
# 为什么心跳判据要独立于 STALE_PROCESSING_SECONDS：
#  后者衡量的是"任务被取出后多久还没结束"，用的是 started_at；
#  而崩溃补偿真正要回答的问题是"**持任务的 worker 还活着吗**"。
#  一个批次可能正常跑十几分钟（started_at 早就超过阈值），只要 worker
#  一直在刷新心跳，它就绝不该被当成崩溃残留重投——否则每次长批次都会
#  在自己跑到一半时被复制一份并发执行。两者混用会把"慢"误判成"死"。

# 心跳判定陈旧的 TTL（秒）。与心跳间隔的关系是 **至少 3 倍余量**：
# 只要 worker 还活着，心跳就会每 HEARTBEAT_INTERVAL_SECONDS 刷新一次，
# 于是最坏情况下（在刷新瞬间被调度挂起）也有 2 个间隔的容差，
# 不会因为一次 GC 停顿或一次 Redis 抖动就误判崩溃。
# 取 60 秒的依据：小于它则真实执行的大批次更容易被误判（宁可重投也不
# 丢任务），大于它则崩溃后要等更久才能恢复；60 秒与 CLAIM_TTL_SECONDS
# 的量级关系是"心跳比抢占短一个数量级"，保证补偿窗口先于幂等窗口到期。
HEARTBEAT_TTL_SECONDS = 60.0

# 心跳刷新间隔（秒）。worker 执行任务期间由独立心跳线程按此周期刷新。
# 必须显著小于 HEARTBEAT_TTL_SECONDS（取其 1/4），留足容忍抖动余量。
HEARTBEAT_INTERVAL_SECONDS = 15.0

# 在途状态 hash 中表示归属与心跳的字段名（单一事实来源）
FIELD_WORKER_ID = "worker_id"
FIELD_HEARTBEAT_AT = "heartbeat_at"
FIELD_REQUEUE_COUNT = "requeue_count"

# 单个任务的最大重入队次数（Day51 任务二 c）
#
# 为什么必须有上界：崩溃补偿是"至少一次"语义的兜底，若某条任务因任何
# 原因永远无法真正跑完（载荷里带了一个必炸的执行目标、批次行已被删除、
# DB 持续不可达），没有上界就会形成"重投→崩溃→重投→崩溃"的永动循环，
# 把 worker 的空闲轮次全部吃掉并持续刷 WARNING。
# 取 3 的依据：崩溃补偿真正要救的是"进程被杀"这类一次性的偶发故障，
# 3 次重投足以覆盖"连续重启 3 次"的极端运维场景；仍失败即视为需要人工
# 介入，进入死信而不是继续自动重试。
MAX_REQUEUE_ATTEMPTS = 3

# 死信状态（写入调度层状态 hash，仅用于标记需人工干预）
#
# 刻意**不进入** STATUS_PENDING/RUNNING/FINISHED/FAILED 四态词表：
# 那是"任务真实执行结果"的取值域，而死信是"补偿机制放弃了"的标记，
# 两者语义不同。权威的业务状态仍以 SQLite 批次行为准。
STATUS_DEAD_LETTER = "dead_letter"

# 重入队幂等守卫 key 的存活秒数
#
# 守卫防的是"同一条在途任务被重复投进队列"（两个 worker 同时扫描到它，
# 或上一轮重投尚未完成时下一轮又扫到）——那会让队列里出现两条同一批次号
# 的消息，被先后消费成两次执行。TTL 取 STALE_PROCESSING_SECONDS 是为了
# 与陈旧判定同节奏：守卫过期后任务若仍在途（说明上一轮重投后仍没跑完）
# 仍可被再次补偿，**不会因为守卫而永久丢任务**（守卫未抢到时在途条目
# 原样留在 list 中，下一轮扫描还会再看到它）。
REQUEUE_GUARD_TTL_SECONDS = STALE_PROCESSING_SECONDS


# ===========================================================================
# claim 属主校验（Day52 P2 修复）
# ===========================================================================
#
# Day51 收口时复核发现的问题：`release_claim` 是
# **无条件 DEL**，而 worker 主循环每轮收尾都调用它——**包括"入口被 claim
# 挡下、零用例执行"的那一轮**。后果是：被挡的消费者会 DEL 掉自己并不持有
# 的旧 worker claim，使 claim 层对"进程冻结但未死透"的防护只持续一个重投
# 周期（守卫 TTL 300s 过后第二次重投即可真实双跑）。
#
# 但它**不能被简单删掉**：真 kill -9 场景下，旧 worker 带着未释放的 claim
# 消失，claim 的 TTL 是 CLAIM_TTL_SECONDS(3600s)。若无条件释放被移除，
# 重投回来的任务会被这道 3600s 的闸挡住，3 次重投全部落空、先转死信——
# 崩溃补偿能力反而没了。于是统一方案是三件事一起做：
#
#  1. claim 占用时 `SET value=<worker_id> NX EX <ttl>`：把"谁持有"变成
#   可比对的事实，而不是固定字符串 "1"（那让属主无从判断）；
#  2. 释放时属主比对删除（compare-and-del）：只删自己持有的 claim，
#   被挡轮次释放不动别人的保护；
#  3. requeue_stale 判定属主死亡后**显式接管**那条无人认领的 claim，
#   让重投回来的任务能重新抢占——这是崩溃恢复能在 claim TTL 内发生的
#   必要条件。
#
# 为什么 compare-and-del 用 WATCH/MULTI/EXEC 而不是 Lua 脚本：
#  项目测试后端是 fakeredis，本机 **未安装 lupa**（实测 `EVAL` 直接返回
#  "unknown command `eval`"），Lua 方案会让全部队列用例在 CI 集体变红，
#  且补 lupa 属于新增测试依赖（requirements.txt 不在当日白名单）。
#  WATCH/MULTI/EXEC 是 Redis 原生 CAS 语义：WATCH 之后若该 key 被任何
#  人改动，EXEC 直接失败并抛 WatchError，天然堵住"GET 与 DEL 之间属主
#  被改"的竞态（已实测：跨连接抢占后 EXEC 抛错，新属主的 claim 存活）。


def task_status_key(execution_id: str) -> str:
  """
  构造任务状态hash的Redis key（纯函数）

  参数:
    execution_id (str): 执行批次号

  返回:
    str: "tm:task:{execution_id}"

  异常:
    无
  """
  return f"tm:task:{execution_id}"


def task_claim_key(execution_id: str) -> str:
  """
  构造幂等抢占的Redis key（纯函数）

  参数:
    execution_id (str): 执行批次号

  返回:
    str: "tm:claim:{execution_id}"

  异常:
    无
  """
  return f"tm:claim:{execution_id}"


def task_requeue_guard_key(execution_id: str) -> str:
  """
  构造重入队幂等守卫的Redis key（纯函数，Day51）

  **刻意与 `task_claim_key` 分开**，两者防的是完全不同的故障：

    tm:claim:{id} ——防"同一批次被两条链路同时执行"（Day45 问题2）。
              由 _execute_batch_async 在执行入口抢占，
              批次终态时释放，TTL 覆盖整个批次时长。
    tm:requeue:{id}——防"同一条在途任务被重复投进队列"（Day51）。
              由 requeue_stale 在重投前抢占，短 TTL。

  为什么绝不能复用 claim key：requeue_stale 若去 SET claim key，
  要么永远抢不到（旧 worker 正持有，重投被自己挡住、白丢补偿机会），
  要么覆盖写入（旧 worker 的执行期保护被抹掉，新消费者直接双跑）。
  两个 key 的语义与生命周期都不同，复用会把"防双跑"变成"制造双跑"。

  参数:
    execution_id (str): 执行批次号

  返回:
    str: "tm:requeue:{execution_id}"

  异常:
    无
  """
  return f"tm:requeue:{execution_id}"


def generate_worker_id() -> str:
  """
  生成 worker 唯一标识（纯函数，Day51）

  组成 "主机名-pid-短随机串" 三段：
    - 主机名：跨进程区分（同一台机器上可能同时跑着 web 进程内 worker
     与 scripts/start_worker.py 起的独立 worker 进程）
    - pid：同一主机内的进程区分
    - 随机短串：同一进程内**反复重建 worker**（Day44 起 stop_worker
     后可再次 start_worker）时的区分

  只取 8 位随机后缀不是为了美观：它只需让"同机同 pid 的先后两个
  worker 实例"区分开即可，长度越长只增加状态 hash 的体积而无收益。

  参数:
    无

  返回:
    str: 形如 "host-12345-a1b2c3d4" 的 worker 标识

  异常:
    无（gethostname 失败时回落空串，标识退化为 "pid-rand" 仍可用）
  """
  try:
    hostname = socket.gethostname()
  except OSError:
    # 拿不到主机名不该让 worker 起不来：标识退化为 "pid-随机串"
    # 仍然满足"同一进程内唯一"这一最低要求
    hostname = ""
  return f"{hostname}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _parse_int_field(value: object, default: int = 0) -> int:
  """
  把状态 hash 字段解析为整数（脏值安全，纯函数）

  Redis hash 里的一切都是 str，但**不保证是我们自己写的**——队列是
  跨进程共享的持久存储，外部写入/历史版本残留都可能留下空串、空格、
  甚至非数字文本。直接 int(...) 会在补偿路径上抛 ValueError，而这条
  路径的失败后果是"崩溃任务再也捞不回来"（补偿机制自己崩了）。

  故解析失败一律回落默认值（重投次数 0），让扫描继续。

  参数:
    value (object): 待解析的字段值
    default (int): 解析失败时的回落值，默认 0

  返回:
    int: 解析成功返回整数；失败返回 default

  异常:
    无
  """
  if isinstance(value, bool):
    # bool 是 int 子类，isinstance(True, int) 为真但语义不是计数
    return default
  try:
    return int(str(value).strip())
  except (TypeError, ValueError):
    return default


def failure_backoff_seconds(consecutive_failures: int) -> float:
  """
  按连续失败次数计算退避秒数（纯函数，指数递增 + 封顶）

  1 次→1s，2 次→2s，3 次→4s，…… 上限
  WORKER_FAILURE_BACKOFF_CEILING_SECONDS（30s）。
  封顶的理由：退避过久会让 Redis 短暂抖动后恢复时消费延迟过大，
  30 秒与 Day44 的 BACKEND_FAILURE_COOLDOWN_SECONDS 同量级，
  既止住热转又不显著拖慢自愈。

  参数:
    consecutive_failures (int): 已连续失败的轮数（<=0 时返回 0）

  返回:
    float: 本轮应退避的秒数（0 表示无需退避）
  """
  if consecutive_failures <= 0:
    return 0.0
  exponent = min(consecutive_failures - 1, 10)
  # 显式 float()：mypy 把 `int ** int` 推断为 Any（int.__pow__ 的
  # 返回类型是 Any），不显式转换会让整个乘法退化为 Any，
  # 在 strict 下触发 no-any-return 判红。
  backoff = WORKER_FAILURE_BACKOFF_BASE_SECONDS * float(2**exponent)
  return min(backoff, WORKER_FAILURE_BACKOFF_CEILING_SECONDS)


class TaskQueueClient:
  """
  Redis任务队列客户端（懒连接 + 默认关闭 + 故障静默降级）

  与Day31 CacheClient同一构建模式:
    - enabled=false时全部命令no-op（enqueue返回False，
     调用方据此fallback裸线程）
    - fake://URL构建fakeredis内存实例（decode_responses=True，
     全链路str读写避免bytes/str混用）
    - 真实URL走redis.from_url（socket_timeout=2防挂死，
     from_url本身不建连，首命令才真正连通）
    - enqueue/dequeue/set_status/get_status全部吞
     redis.RedisError，只记warning不冒泡

  属性:
    _backend (redis.Redis | fakeredis.FakeRedis | None):
      懒加载后端实例，None表示尚未构建
  """

  def __init__(self) -> None:
    """
    初始化队列客户端（只置空后端，不建立连接）

    参数:
      无

    返回:
      无

    异常:
      无
    """
    self._backend: Any | None = None
    # 后端构建失败的冷却截止时间戳（time.monotonic 基准）。
    # 0.0 表示"从未失败过"。由 _get_backend 维护：失败时置为
    # now + BACKEND_FAILURE_COOLDOWN_SECONDS，成功时清零。
    # 单独一个属性而非复用 _backend，是为了与"后端实例缓存"解耦——
    # 冷却期结束后仍必须能正常重建并自愈。
    self._failed_until: float = 0.0
    # 连续故障轮数（Day45 问题1）：dequeue 因 Redis 异常失败时 +1，
    # 成功（含"队列空"这种正常无任务）时归零。worker 据此退避。
    self._consecutive_failures: int = 0
    # 最近一次 dequeue 是否因故障失败（Day45 问题1 的退避判据）。
    # 与 `_backend is None` 的区别：后端已构建但连接断开时后者为假、
    # 前者为真，正是热转的漏网路径。
    self._last_dequeue_failed: bool = False
    # 最近一次成功 dequeue 到的原始报文（Day45 问题4 的 ack 用）：
    # BRPOPLPUSH 把任务移入 processing list 后，需在执行完成后用
    # 同一报文串 LREM 掉。保留 dequeue 的 dict | None 签名不变。
    self._last_dequeued_raw: str | None = None
    # 本进程内执行者的身份（Day52 P2）：claim/release_claim 未显式
    # 传 worker_id 时用它做属主比对。
    #
    # 为什么挂在客户端上而不是只作为方法参数：真正调用 claim() 的是
    # `case_manager._execute_batch_async`（Day45 埋点），那处**不在
    # Day52 的改动白名单内**，不能为了传 worker_id 去改它。于是身份
    # 由持有客户端的 worker 在构造时登记一次——一个进程一个客户端、
    # 一个 worker，登记与读取同属一方，不存在错配。
    #
    # 缺省空串表示"无登记身份"：裸线程链路（无 worker）走的就是这条
    # 路径，此时 claim 与 release 的属主都是空串、仍然自洽。
    self.worker_id: str = ""

  # ------------------------------------------------------------------
  # 消费侧故障状态（worker 退避判据，Day45 问题1）
  # ------------------------------------------------------------------
  def has_recent_failure(self) -> bool:
    """
    最近一次 dequeue 是否因故障失败（而非"队列空"）

    用途：worker 主循环的退避判据。**不能**用 `_backend is None`
    代替——后端已构建成功但运行期连接断开时该判断为假，worker 会
    零延时 continue 形成热转（Day45 实测 2 秒 21124 次空转）。

    参数:
      无

    返回:
      bool: 最近一次 dequeue 因 Redis/传输故障失败时为 True；
         "队列空"（BRPOP 正常超时）返回 False
    """
    return self._last_dequeue_failed

  def get_failure_backoff(self) -> float:
    """
    本轮应退避的秒数（按连续失败次数指数递增）

    参数:
      无

    返回:
      float: 退避秒数；无连续失败时返回 0.0
    """
    return failure_backoff_seconds(self._consecutive_failures)

  @property
  def consecutive_failures(self) -> int:
    """
    当前连续故障轮数（测试与日志观测用）

    返回:
      int: 连续失败次数；成功一次即归零
    """
    return self._consecutive_failures

  def _record_dequeue_failure(self) -> None:
    """
    记录一次 dequeue 故障（内部方法）

    参数:
      无

    返回:
      None
    """
    self._consecutive_failures += 1
    self._last_dequeue_failed = True

  def _clear_dequeue_failure(self) -> None:
    """
    清除故障标记（dequeue 恢复正常时调用，内部方法）

    参数:
      无

    返回:
      None
    """
    self._consecutive_failures = 0
    self._last_dequeue_failed = False

  # ------------------------------------------------------------------
  # 配置属性（实时读env_manager，monkeypatch可热替换）
  # ------------------------------------------------------------------
  @property
  def enabled(self) -> bool:
    """
    任务队列总开关（TM_TASK_QUEUE_ENABLED，默认false关闭）

    参数:
      无

    返回:
      bool: 是否启用任务队列
    """
    return env_manager.get_bool("TM_TASK_QUEUE_ENABLED", False)

  @property
  def worker_enabled(self) -> bool:
    """
    是否允许在应用内启动worker线程

    TM_TASK_WORKER_ENABLED缺省时跟随TM_TASK_QUEUE_ENABLED
    （队列启用则默认起worker）；显式配置可单独关闭worker
    （如测试只想验证入队、不想真正消费的场景）。

    参数:
      无

    返回:
      bool: 是否启用应用内worker
    """
    return env_manager.get_bool(
      "TM_TASK_WORKER_ENABLED", self.enabled
    )

  @property
  def queue_key(self) -> str:
    """
    任务队列Redis list的key（TM_TASK_QUEUE_KEY）

    参数:
      无

    返回:
      str: 队列key，默认"tm:queue:tasks"
    """
    return env_manager.get("TM_TASK_QUEUE_KEY", "tm:queue:tasks")

  @property
  def processing_key(self) -> str:
    """
    在途任务list的key（BRPOPLPUSH 的目标，Day45 问题4）

    可靠性机制：任务被取出时同时落入本 list，执行成功后由 ack()
    移除。worker 崩溃时任务仍在，requeue_stale() 按停留时长重新投回
    任务队列——这正是旧 BRPOP"弹出即删"缺失的确认语义。

    参数:
      无

    返回:
      str: 在途list key，默认"{queue_key}:processing"
    """
    return f"{self.queue_key}:processing"

  @property
  def brpop_timeout(self) -> float:
    """
    worker循环BRPOP阻塞超时秒数（TM_TASK_BRPOP_TIMEOUT）

    超时必须是有限值: worker靠超时返回None后重新检查
    stop_event，永久阻塞会导致stop无法及时响应。

    参数:
      无

    返回:
      float: 超时秒数，默认1.0
    """
    return env_manager.get_float("TM_TASK_BRPOP_TIMEOUT", 1.0)

  @property
  def redis_url(self) -> str:
    """
    Redis连接URL（复用Day31的TM_REDIS_URL配置）

    参数:
      无

    返回:
      str: 连接URL；fake://开头为测试内存实例约定
    """
    return env_manager.get(
      "TM_REDIS_URL", "redis://127.0.0.1:6379/0"
    )

  # ------------------------------------------------------------------
  # 后端构建（与CacheClient同模式）
  # ------------------------------------------------------------------
  def _get_backend(self) -> Any | None:
    """
    获取后端实例（懒加载并缓存复用）

    构建规则:
      - enabled=false: 恒返回None（全命令no-op）
      - fake://开头: fakeredis.FakeRedis(decode_responses=True)
      - 其余: redis.from_url(socket_timeout=2)

    参数:
      无

    返回:
      Any | None: 后端实例；未启用或fakeredis依赖缺失时为None

    异常:
      无（ImportError降级None，连接异常由命令处RedisError兜底）
    """
    if not self.enabled:
      return None
    if self._backend is not None:
      return self._backend
    # 失败冷却闸门（Day44 热修 P1-1）：修复前"构建失败不缓存异常以便自愈"
    # 与 worker 循环 `payload is None -> continue`（无退避）叠加成热转——
    # 实测 1 秒内 _get_backend 被调 8871 次、打 8871 条完全相同的 WARNING
    # （外推 1 小时 3193 万条），CPU 占满一核 + 磁盘写满。
    # 自愈是想要的，但"过一会儿再试一次"就够，不需要"每微秒再试一次"。
    now = time.monotonic()
    if now < self._failed_until:
      # 冷却期内：静默返回，既不重试也不打日志
      return None
    recovering = self._failed_until > 0.0
    url = self.redis_url
    if url.startswith(FAKE_SCHEME):
      try:
        import fakeredis

        self._backend = fakeredis.FakeRedis(decode_responses=True)
        self._failed_until = 0.0
        if recovering:
          logger.info("任务队列后端已恢复 | 类型: fakeredis内存实例")
        else:
          logger.debug("任务队列后端已构建 | 类型: fakeredis内存实例")
        return self._backend
      except ImportError as exc:
        logger.warning(
          f"任务队列已启用但fakeredis未安装，降级no-op | {exc}"
        )
        return None
    # 真实Redis客户端。构建期异常必须在此兜底（Day44 P2-04）：
    # 三个公开方法（enqueue/dequeue/set_status）都是"先取后端再进 try"，
    # 而 redis.from_url 对非法 URL 抛的是 ValueError（端口非数字 /
    # scheme非法 / 含中文冒号），**不是 RedisError**，完全没有被方法体内
    # 的 except redis.RedisError 覆盖。未兜底时：① trigger 路由的
    # enqueue() 冒泡到全局处理器 → 接口 500，绕过"入队失败 fallback
    # 裸线程、接口不 500"的项目铁律；② TaskWorker.run() 的 dequeue()
    # 在 while 循环内、任何 try 之外，异常逃出线程目标函数把 worker
    # 打死且业务日志无痕。与 src/core/cache.py 同一套判据。
    try:
      self._backend = _REDIS_FROM_URL(
        url,
        decode_responses=True,
        socket_timeout=2,
      )
    except (ValueError, TypeError, redis.RedisError) as exc:
      # 关键改动：进入时间冷却，而不是"每次调用都重试并打一条日志"。
      # 冷却期内本函数开头直接 return None，热转（CPU）与日志洪水
      # （磁盘）两个后果同时消除。
      # 自愈仍然成立：冷却是"时间到即重试"，不是"失败一次永久禁用"；
      # 冷却期满后的第一次调用会真正重新尝试构建，成功则清空标记。
      self._failed_until = time.monotonic() + BACKEND_FAILURE_COOLDOWN_SECONDS
      logger.warning(
        f"任务队列后端构建失败，入队降级并交由调用方 fallback | "
        f"URL: {url} | 异常: {type(exc).__name__}: {exc} | "
        f"将在 {BACKEND_FAILURE_COOLDOWN_SECONDS}s 后重试"
      )
      return None
    self._failed_until = 0.0
    if recovering:
      logger.info(f"任务队列 Redis 连接已恢复 | URL: {url}")
    else:
      logger.debug(f"任务队列后端已构建 | 类型: redis | URL: {url}")
    return self._backend

  def reset_backend(self) -> None:
    """
    重置后端实例（仅测试场景使用）

    丢弃已缓存后端，下次访问按最新环境变量重建；
    function级fixture在用例间调用保证fake数据隔离。

    参数:
      无

    返回:
      无

    异常:
      无（close异常静默忽略）
    """
    if self._backend is not None:
      try:
        self._backend.close()
      except Exception as exc:  # noqa: BLE001 测试重置不关心关闭异常
        logger.debug(f"任务队列后端关闭异常已忽略 | {exc}")
    self._backend = None
    # 一并清空失败冷却（Day44 热修 P1-1）：否则用例内先触发一次构建失败
    # 就会让后续 5 秒内的所有调用静默返回 None，测试之间互相污染。
    self._failed_until = 0.0

  # ------------------------------------------------------------------
  # 队列命令
  # ------------------------------------------------------------------
  def enqueue(self, execution_id: str, payload: dict) -> bool:
    """
    任务入队（生产者调用，LPUSH到队列list头部）

    payload序列化为JSON后LPUSH；成功返回True。
    未启用、后端缺失或任何Redis异常时返回False——调用方
    必须据此fallback裸线程，保证任务不丢。

    参数:
      execution_id (str): 执行批次号（用于日志定位）
      payload (dict): 任务载荷，须含execution_id/cases/
              executor_kind字段（JSON可序列化）

    返回:
      bool: 入队成功True；未启用/异常False（调用方fallback）

    异常:
      无（redis.RedisError与序列化异常均吞掉返回False）
    """
    backend = self._get_backend()
    if backend is None:
      return False
    try:
      # ensure_ascii=False: cases含中文用例名时原样存储
      raw_payload = json.dumps(payload, ensure_ascii=False)
      backend.lpush(self.queue_key, raw_payload)
      logger.debug(
        f"任务已入队 | execution_id={execution_id} | "
        f"队列key={self.queue_key}"
      )
      return True
    except (redis.RedisError, TypeError, ValueError) as exc:
      # 故障信号: warning打一次并返回False，交由路由fallback
      logger.warning(
        f"任务入队失败，将fallback裸线程 | "
        f"execution_id={execution_id} | {exc}"
      )
      return False

  def dequeue(self, timeout: float | None = None) -> dict | None:
    """
    阻塞式取出任务（消费者调用，BRPOPLPUSH 队列尾→在途，FIFO）

    LPUSH头部 + BRPOPLPUSH尾部构成先入先出；超时无任务返回None。
    任何Redis/反序列化异常返回None（worker循环继续，
    不因单条坏消息或瞬时故障退出）。

    **与修复前的三处语义变化**（Day45 全量审查 问题3/问题1/问题4）：

    1. **timeout=0 不再是"永久阻塞"**：redis-py 5.0.8 的 brpop 文档
      明确 "If timeout is 0, then block indefinitely."，而本方法
      旧 docstring 写的是"0表示不阻塞立即返回"——语义正好写反。
      现把 <=0 的取值夹到 MIN_BRPOP_TIMEOUT_SECONDS(0.1s)。
    2. **BRPOP → BRPOPLPUSH**：弹出后同时落入在途 list
      (processing_key)，执行完成后由 ack() 移除。worker 崩溃时
      任务仍在在途 list，可由 requeue_stale() 重新投回。
    3. **故障可观测**：Redis 异常时记 `_last_dequeue_failed=True`
      并累加连续失败计数，供 worker 指数退避（has_recent_failure /
      get_failure_backoff）；"队列空"不算故障，立即清零。

    参数:
      timeout (float | None): 阻塞超时秒数；None 时用
        TM_TASK_BRPOP_TIMEOUT 配置值；<=0 会被夹到
        MIN_BRPOP_TIMEOUT_SECONDS（不永久阻塞）

    返回:
      dict | None: 任务payload字典；超时/未启用/反序列化失败/
            载荷非JSON对象时为None（保证worker主循环永不死于
            单条畸形消息）

    异常:
      无
    """
    backend = self._get_backend()
    if backend is None:
      # 后端不可用属"故障"，与"队列空"区分开，才能触发 worker 退避
      self._record_dequeue_failure()
      return None
    effective_timeout = (
      timeout if isinstance(timeout, (int, float)) else self.brpop_timeout
    )
    # 夹逼：redis-py 的 0 语义是永久阻塞，会让 stop_event 失效
    if effective_timeout is None or effective_timeout <= 0:
      effective_timeout = MIN_BRPOP_TIMEOUT_SECONDS
    try:
      # BRPOPLPUSH：原子地把任务从队列尾移入在途 list，返回报文串
      # （超时返回 None）。在途 list 的存在使 worker 崩溃后任务可恢复。
      raw_payload = backend.brpoplpush(
        self.queue_key, self.processing_key, timeout=effective_timeout
      )
    except redis.RedisError as exc:
      logger.warning(f"任务出队异常，本轮跳过 | {exc}")
      self._record_dequeue_failure()
      return None
    # 通信成功（含"队列空"）即恢复：清零故障标记，避免一次抖动
    # 之后的正常消费仍背着退避
    self._clear_dequeue_failure()
    if raw_payload is None:
      return None
    self._last_dequeued_raw = raw_payload
    try:
      payload = json.loads(raw_payload)
    except (ValueError, TypeError) as exc:
      # 坏消息不卡死worker: 记warning后跳过。
      # 此时它已在在途 list 里，必须就地 LREM，否则会被
      # requeue_stale 当作"在途超时"无限重投（永动循环）。
      logger.warning(f"任务载荷反序列化失败，消息已丢弃 | {exc}")
      self._discard_from_processing(raw_payload)
      return None
    # 结构校验: json.loads 对合法JSON数组/标量也成功，但返回的不是
    # 载荷字典。TaskWorker.run 在 try 块**之前**执行
    # `payload.get("execution_id")`，非dict会抛 AttributeError
    # 逃出while循环、令worker线程静默死亡，后续任务永久堆积
    # （队列是跨进程共享的持久存储，混入非对象载荷完全可能）。
    # 归入与坏消息相同的跳过路径，保持worker存活。
    if not isinstance(payload, dict):
      logger.warning(
        f"任务载荷结构非法（期望JSON对象），消息已丢弃 | "
        f"实际类型: {type(payload).__name__}"
      )
      self._discard_from_processing(raw_payload)
      return None
    return payload

  def ack(self) -> bool:
    """
    确认最近一次 dequeue 的任务已完成（从在途 list 移除）

    与 dequeue 配套：任务成功执行（或终态收尾）后调用，使在途 list
    不再持有它。执行崩溃时不会调用，任务留在在途 list 等
    requeue_stale 回收。

    **调用顺序铁律（Day51）**：终态必须先落库再 ack。
    先 ack 会让任务同时从"在途 list"和"可被 requeue_stale 扫到"
    两个维度消失，而此时业务终态若还没写成功（或写失败了），
    批次就永久停在 running——没有任何补偿路径能把它捞回来。
    正常收尾请直接用 `complete_task()`，它把"写终态 → ack"封成
    一条调用链，调用方无法写错顺序。

    参数:
      无

    返回:
      bool: 成功从在途 list 移除返回 True；无待确认任务/后端不可用/
         Redis 异常返回 False（不影响执行结果）
    """
    raw = self._last_dequeued_raw
    if raw is None:
      return False
    removed = self._discard_from_processing(raw)
    if removed:
      self._last_dequeued_raw = None
    return removed

  def mark_in_flight(self, execution_id: str, worker_id: str) -> bool:
    """
    标记任务进入在途执行态：running + worker_id 归属 + 初始心跳

    Day51 任务二 (a)：worker 把任务从队列取出后**立即**写这一笔，
    让"谁正在执行哪条任务"成为可查询的事实，而不是只能靠
    processing list 里那条匿名报文去猜。

    为什么归属信息落在状态 hash 而不塞进 processing list 的报文：
    processing list 的元素是原始 JSON 报文，ack 依赖 `LREM` 精确
    匹配这条报文串；往里塞 worker_id 就得重写报文，等于把"确认语义"
    和"执行元数据"耦合在一起。状态 hash 本身就是按批次号组织的
    在途记录，worker_id / 心跳挂在它上面，processing list 只负责
    "成员资格"，两者职责不重叠。

    参数:
      execution_id (str): 执行批次号
      worker_id (str): 执行该任务的 worker 标识

    返回:
      bool: 写入成功 True；批次号为空/后端不可用/Redis 异常 False
         （False 不阻断执行：归属信息缺失只会让补偿机制更保守）

    异常:
      无
    """
    if not execution_id:
      return False
    now = datetime.now().isoformat()
    backend = self._get_backend()
    if backend is None:
      return False
    try:
      backend.hset(
        task_status_key(execution_id),
        mapping={
          "status": STATUS_RUNNING,
          "started_at": now,
          FIELD_WORKER_ID: worker_id,
          FIELD_HEARTBEAT_AT: now,
        },
      )
      backend.expire(task_status_key(execution_id), TASK_STATUS_TTL_SECONDS)
      return True
    except redis.RedisError as exc:
      logger.warning(
        f"在途归属标记写入失败（不影响任务执行）| "
        f"execution_id={execution_id} | worker={worker_id} | {exc}"
      )
      return False

  def heartbeat(self, execution_id: str) -> bool:
    """
    刷新在途任务的心跳时间（worker 执行期间周期调用，Day51 任务二 d）

    心跳是"worker 还活着"的唯一可观测证据：任务被取出时只写一次
    初始心跳，此后只要 worker 仍在执行批次，就由独立心跳线程按
    HEARTBEAT_INTERVAL_SECONDS 周期刷新。requeue_stale 判陈旧时
    **优先看心跳**，心跳新鲜就不重投——这样一批正常跑十几分钟的大
    批次不会因为 started_at 超阈值而被复制成并发双跑。

    参数:
      execution_id (str): 执行批次号

    返回:
      bool: 刷新成功 True；批次号为空/无在途记录/后端不可用/
         Redis 异常 False

    异常:
      无
    """
    if not execution_id:
      return False
    backend = self._get_backend()
    if backend is None:
      return False
    try:
      backend.hset(
        task_status_key(execution_id),
        key=FIELD_HEARTBEAT_AT,
        value=datetime.now().isoformat(),
      )
      return True
    except redis.RedisError as exc:
      # 心跳刷新失败只记 debug：连续失败会让最后一次心跳变陈旧、
      # 任务被当成崩溃残留重投（宁可重投也不丢任务），但单次失败
      # 远没到需要人工介入的程度，刷 warning 只会污染日志
      logger.debug(
        f"心跳刷新失败 | execution_id={execution_id} | {exc}"
      )
      return False

  def acquire_requeue_guard(self, execution_id: str) -> bool:
    """
    抢占重入队幂等守卫（SETNX + TTL，Day51 任务二 c）

    重投前必须先抢到守卫，否则同一条在途任务会被重复投进队列：
    两个 worker 同时扫到它、或一轮扫描未完成时下一轮又扫到，
    都会各自 LPUSH 一次，队列里就出现两条同批次号的消息，
    被先后消费成两次执行（明细去重只能挡住重复落库，挡不住
    两次真实执行 + 两次 Allure 桥接）。

    参数:
      execution_id (str): 执行批次号

    返回:
      bool: 抢到守卫（此前无人持有/上次守卫已过期）True；
         已被他人持有 False；后端不可用时**返回 True**
         （守卫只是防重复的优化，不该在 Redis 故障时把
          补偿机制整个变成不可用，见 claim 的同款理由）

    异常:
      无
    """
    if not execution_id:
      return False
    backend = self._get_backend()
    if backend is None:
      return True
    try:
      acquired = backend.set(
        task_requeue_guard_key(execution_id),
        "1",
        nx=True,
        ex=REQUEUE_GUARD_TTL_SECONDS,
      )
    except redis.RedisError as exc:
      logger.warning(
        f"重入队守卫抢占异常，按放行处理 | "
        f"execution_id={execution_id} | {exc}"
      )
      return True
    return bool(acquired)

  def complete_task(
    self,
    execution_id: str,
    terminal_status: str,
    error: str | None = None,
    state_confirmed: bool = True,
  ) -> bool:
    """
    写终态 + ack 在同一条调用链内完成（Day51 任务二 b）

    这是"ack 与状态回写联动"的唯一入口，把**顺序**变成调用方
    无法写错的结构约束：先 set_status 落终态，再 ack 移除在途条目。

    不允许反过来做的原因：ack 一旦完成，任务就从在途 list 消失，
    requeue_stale 再也扫不到它；若此刻业务终态还没写成功，
    批次会永久停在 running，没有任何补偿路径能捞回来——
    而"状态没写成"恰恰是数据库异常时最可能发生的事。

    state_confirmed=False 表示**没能确认业务终态已落库**
    （批次行查不到、状态查询抛错、或查询到的不是终态）。
    此时仍然写调度层的终态（让任务不至于在状态 hash 上永远停在
    running），但**拒绝 ack**：让在途条目留在 list 里，由
    requeue_stale 按心跳 TTL 重新投回，走"至少一次"而不是
    "至多一次"。代价是可能重跑一次，收益是绝不静默丢任务。

    参数:
      execution_id (str): 执行批次号
      terminal_status (str): 调度层终态 finished/failed/dead_letter
      error (str | None): 终态错误原因，None 表示不写该字段
      state_confirmed (bool): 业务终态是否已确认落库

    返回:
      bool: 终态写入且 ack 完成 True；拒绝 ack 或写入失败 False

    异常:
      无（内部全部异常自兜底，失败只反映在返回值与日志上）
    """
    self.set_status(
      execution_id,
      terminal_status,
      finished_at=datetime.now().isoformat(),
      error=error,
    )
    if not state_confirmed:
      logger.error(
        f"业务终态未确认落库，拒绝ack（在途条目保留待崩溃补偿重投）| "
        f"execution_id={execution_id} | 调度层终态={terminal_status} | "
        f"原因={error or '未提供'}"
      )
      return False
    return self.ack()

  def _discard_from_processing(self, raw_payload: str) -> bool:
    """
    从在途 list 移除指定报文（内部方法）

    参数:
      raw_payload (str): dequeue 返回过的原始报文串

    返回:
      bool: 移除成功 True；后端不可用/异常 False
    """
    backend = self._get_backend()
    if backend is None:
      return False
    try:
      backend.lrem(self.processing_key, 1, raw_payload)
      return True
    except redis.RedisError as exc:
      logger.warning(f"在途任务移除失败 | {exc}")
      return False

  def requeue_stale(
    self,
    max_age_seconds: float = STALE_PROCESSING_SECONDS,
    heartbeat_ttl_seconds: float = HEARTBEAT_TTL_SECONDS,
  ) -> int:
    """
    把在途 list 中心跳超时的任务重新投回任务队列（崩溃补偿，Day51 任务二 c）

    无 ack 的后果（Day45 问题4）：旧的 BRPOP 弹出即删，进程被 kill
    时批次永久停在 pending/running，状态 hash 24h 后过期，无任何
    补偿路径。改 BRPOPLPUSH 后任务同时留在在途 list，本方法在
    worker 每轮空闲时调用，把心跳超时的在途任务重投。

    **陈旧判据改为"优先看心跳"**（Day51）：
      1) 有心跳记录 -> 只看最后一次心跳距今是否超过
        heartbeat_ttl_seconds；worker 只要还在执行批次就由心跳
        线程持续刷新，心跳新鲜即判为"仍有人在管"，绝不重投；
      2) 无心跳字段（老数据/状态 hash 已过期）-> 回落到 started_at /
        queued_at 的停留时长判据（Day45 口径）；
      3) 状态 hash 整体缺失 / 时间戳不可解析 -> 视为陈旧（宁可重投
        一次也不要永久丢任务，重复执行由 execution_id 幂等抢占与
        明细去重兜住）。

    **四道闸：重投是双刃剑，既能救回崩溃任务，也能制造双跑与永动重投**
      ① 幂等守卫：重投前 SETNX tm:requeue:{id}，抢不到就跳过，防同
        一条在途任务被重复 LPUSH（两个 worker 同时扫到它）。守卫
        **用独立 key、绝不复用 tm:claim:**——复用会抹掉旧 worker
        的执行期保护，双跑反而被这次"补偿"制造出来。
      ② 次数上界：超过 MAX_REQUEUE_ATTEMPTS 次转入死信标记，不再
        自动重投，防永动重投刷屏与空转。
      ③ 先移出在途再入队：顺序反了会在本轮 requeue_stale 里再次扫
        到自己，形成同轮自环。
      ④ 坏消息就地 LREM：无人能真正执行的报文重投只会形成永动循环。

    参数:
      max_age_seconds (float): 心跳缺失时的兜底停留时长阈值（秒），
        默认 STALE_PROCESSING_SECONDS
      heartbeat_ttl_seconds (float): 心跳陈旧阈值（秒），默认
        HEARTBEAT_TTL_SECONDS

    返回:
      int: 成功重新投回的任务条数（死信与守卫未抢到者不计入）

    异常:
      无（Redis 异常一律降级为 0 条重投，不向上抛）
    """
    backend = self._get_backend()
    if backend is None:
      return 0
    try:
      in_flight = backend.lrange(self.processing_key, 0, -1) or []
    except redis.RedisError as exc:
      logger.warning(f"在途任务扫描失败 | {exc}")
      return 0
    if not in_flight:
      return 0

    requeued = 0
    for raw in in_flight:
      try:
        payload = json.loads(raw)
      except (ValueError, TypeError):
        # 坏消息无人认领，直接丢弃以免永动重投
        self._discard_from_processing(raw)
        continue
      if not isinstance(payload, dict):
        self._discard_from_processing(raw)
        continue
      execution_id = str(payload.get("execution_id") or "").strip()
      if not execution_id:
        self._discard_from_processing(raw)
        continue
      status = self.get_status(execution_id) or {}
      if not self._is_stale_in_flight(
        execution_id,
        max_age_seconds,
        heartbeat_ttl_seconds,
        status=status,
      ):
        continue
      # 闸②：重投次数上界。超过即转死信等待人工干预——某条任务反复
      # 崩溃必有根因（载荷带必炸的执行目标、批次行已删、DB 持续不可
      # 达），自动重试只会掩盖它并持续刷日志
      attempts = _parse_int_field(status.get(FIELD_REQUEUE_COUNT))
      if attempts >= MAX_REQUEUE_ATTEMPTS:
        self._discard_from_processing(raw)
        self.set_status(
          execution_id,
          STATUS_DEAD_LETTER,
          error=f"重入队次数已达上限 {MAX_REQUEUE_ATTEMPTS}，判定为死信",
          **{FIELD_REQUEUE_COUNT: attempts},
        )
        logger.error(
          f"在途任务重入队次数超上限，转入死信（不再自动补偿）| "
          f"execution_id={execution_id} | 已重投: {attempts} 次"
        )
        continue
      # 闸①：幂等守卫。抢不到说明别的 worker 或上一轮已处理过这条
      # 任务，跳过；此时**不动在途条目**，下轮扫描仍能看到它，
      # 守卫过期后仍可补偿，不会因守卫把任务永久钉死
      if not self.acquire_requeue_guard(execution_id):
        continue
      # 闸⑤（Day52 P2 修复）：死亡属主的 claim 显式接管。
      #
      # 为什么必须在重投**之前**完成：重投回来的任务会先走
      # `claim()` 才进入执行体，若此时旧 worker 遗留的 claim 还在
      # （TTL 3600s），它会被当场挡下——3 次重投全部落空、先进死信，
      # 崩溃补偿等于没有。上一行那句"守卫过期后仍可补偿"在这里
      # 同样不成立：挡住它的是 claim 而不是守卫。
      #
      # 为什么由 requeue_stale 释放、而不是让每个 worker 各自为政：
      # 本方法已经用**心跳陈旧**判定过属主死亡，它是在替系统做
      # 死亡认定，据此接管那条无人认领的 claim 是合法且必要的；
      # 与之相对，worker 收尾时释放自己并不持有的 claim（被挡轮次）
      # 没有任何认定依据，必须被属主校验挡下。
      dead_owner = str(status.get(FIELD_WORKER_ID) or "")
      # 接管恒放行（Day52-fix P2-2 起）：属主失配也继续接管，调用方
      # 不再需要"接管失败就跳过"的分支。保留该分支只会留下一段永远
      # 走不到的死代码——`_takeover_stale_claim` 的每条出口都返回 True
      # （后端不可用/读不到属主/删除异常均按 fail-open 放行），留着它
      # 既无法被测试覆盖，也让人误以为"接管仍可能失败并中止重投"。
      self._takeover_stale_claim(execution_id, dead_owner)
      # 闸③：先移出在途再入队，顺序反了会在本轮扫到自己形成自环
      self._discard_from_processing(raw)
      try:
        backend.lpush(self.queue_key, raw)
      except redis.RedisError as exc:
        logger.warning(f"陈旧任务重新入队失败 | {exc}")
        continue
      attempts += 1
      self.set_status(
        execution_id,
        STATUS_PENDING,
        error=f"心跳超时已重新入队（第 {attempts} 次）",
        **{FIELD_REQUEUE_COUNT: attempts},
      )
      requeued += 1
      logger.warning(
        f"在途任务心跳超时已重新入队（worker 崩溃补偿）| "
        f"execution_id={execution_id} | 阈值: {heartbeat_ttl_seconds}s | "
        f"第 {attempts} 次重投"
      )
    return requeued

  def _is_stale_in_flight(
    self,
    execution_id: str,
    max_age_seconds: float,
    heartbeat_ttl_seconds: float = HEARTBEAT_TTL_SECONDS,
    status: dict[str, Any] | None = None,
  ) -> bool:
    """
    判定在途任务是否已心跳超时（内部方法）

    参数:
      execution_id (str): 执行批次号
      max_age_seconds (float): 心跳缺失时的兜底停留时长阈值（秒）
      heartbeat_ttl_seconds (float): 心跳陈旧阈值（秒）
      status (dict | None): 已读入的任务状态 hash 快照；None 时本
        方法自行读取（便于调用方在一轮扫描里复用同一份快照）

    返回:
      bool: 心跳超时返回 True；状态 hash 缺失/时间戳不可解析也返回
         True（宁可重投，重复执行由幂等抢占兜住）
    """
    status = self.get_status(execution_id) if status is None else status
    if not status:
      return True
    # 心跳优先：有心跳记录就只看心跳，不受 started_at 影响。这是
    # "跑得慢 != 死了"的判据——一批正常执行十几分钟的批次，
    # started_at 早已超过 max_age_seconds，但心跳持续刷新，必须判为
    # 未陈旧，否则每次长批次都会被复制一份并发起双跑。
    heartbeat_value = status.get(FIELD_HEARTBEAT_AT)
    stamp_value = heartbeat_value or status.get("started_at") or status.get("queued_at")
    if not stamp_value:
      return True
    threshold = heartbeat_ttl_seconds if heartbeat_value else max_age_seconds
    try:
      stamp = datetime.fromisoformat(str(stamp_value))
    except (ValueError, TypeError):
      return True
    # 时间戳可能带时区标识（tz-aware），而 datetime.now() 是 naive：
    # 两者直接相减会抛 TypeError（不是 ValueError），故先归一到 naive。
    # 队列状态 hash 由本模块写入（naive isoformat），带时区只可能是
    # 外部写入的异常数据；按"无法判定即视为陈旧"处理，避免反复重投。
    if stamp.tzinfo is not None:
      return True
    elapsed = (datetime.now() - stamp).total_seconds()
    return elapsed > threshold

  # ------------------------------------------------------------------
  # 任务状态hash（调度层快查，权威状态仍在SQLite批次表）
  # ------------------------------------------------------------------
  def set_status(
    self, execution_id: str, status: str, **fields: Any
  ) -> None:
    """
    写入任务状态hash（HMSET语义，走HSET mapping避免弃用警告）

    参数:
      execution_id (str): 执行批次号
      status (str): 调度状态pending/running/finished/failed
      **fields (Any): 附加时间/错误字段，如
        queued_at/started_at/finished_at/error（值统一
        转str存储，None值跳过，HGETALL只回str）

    返回:
      无

    异常:
      无（未启用或Redis异常时静默no-op）
    """
    backend = self._get_backend()
    if backend is None:
      return
    # HSET mapping: status必写；None字段剔除（Redis hash无null语义）
    mapping: dict = {"status": str(status)}
    for field_name, field_value in fields.items():
      if field_value is not None:
        mapping[field_name] = str(field_value)
    try:
      backend.hset(task_status_key(execution_id), mapping=mapping)
      # 状态 hash 加 TTL（Day44 P3-03）：原先只 hset 不设过期，
      # 每个执行批次会在 Redis 里留一个永久 key（error 字段失败时
      # 可达数百字符），与 cache.py 全量 setex 的策略不一致，长期
      # 运行时无界累积且无任何清理路径。批次终态后该 hash 只是
      # 调度层镜像，权威状态在 SQLite 批次行，过期不影响正确性。
      backend.expire(task_status_key(execution_id), TASK_STATUS_TTL_SECONDS)
    except redis.RedisError as exc:
      logger.warning(
        f"任务状态写入异常，已跳过 | execution_id={execution_id} | {exc}"
      )

  def get_status(self, execution_id: str) -> dict | None:
    """
    读取任务状态hash（HGETALL）

    参数:
      execution_id (str): 执行批次号

    返回:
      dict | None: 状态字段字典（无记录返回空dict）；
             未启用/异常时返回None

    异常:
      无
    """
    backend = self._get_backend()
    if backend is None:
      return None
    try:
      return backend.hgetall(task_status_key(execution_id))
    except redis.RedisError as exc:
      logger.warning(
        f"任务状态读取异常，返回None | execution_id={execution_id} | {exc}"
      )
      return None

  # ------------------------------------------------------------------
  # 幂等抢占（Day45 全量审查 问题2）
  # ------------------------------------------------------------------
  def claim(
    self,
    execution_id: str,
    ttl_seconds: int = CLAIM_TTL_SECONDS,
    worker_id: str | None = None,
  ) -> bool:
    """
    以批次号做幂等抢占（SET NX + TTL），value 存属主 worker_id（Day52 P2）

    要解决的场景：enqueue 的 lpush 已在 Redis 生效，但应答因
    socket_timeout 超时抛错 → enqueue 返回 False → 路由用**同一
    execution_id** 起裸线程；此时消息仍在队列里等 worker 消费 →
    同一批次被执行两次，明细双写、计数翻倍、通过率被污染。
    两条链路谁先抢到 claim 谁执行，后到者直接放弃。

    **value 为什么必须存属主而不是固定 "1"（Day52）**：属主是
    release_claim 做 compare-and-del 的唯一判据。存固定串时任何
    worker 读到的都是同一个值，"这条 claim 是不是我的"根本无从判断，
    属主校验形同虚设。

    队列未启用（后端不可用）时**一律返回 True**：此时只有路由的裸线程
    一条路径，不存在双跑；用"抢不到就不执行"去阻断默认链路会
    直接让平台的核心功能失效——幂等保护不能反过来变成可用性风险。

    参数:
      execution_id (str): 执行批次号
      ttl_seconds (int): claim 存活秒数，默认 CLAIM_TTL_SECONDS
      worker_id (str | None): 属主标识；None 时取客户端登记的
        `self.worker_id`（裸线程链路为空串，两侧仍自洽）

    返回:
      bool: 抢到（此前无人持有）返回 True；已被他人持有返回 False；
         后端不可用时返回 True（不阻断执行）
    """
    if not execution_id:
      return False
    backend = self._get_backend()
    if backend is None:
      return True
    owner = self._resolve_owner(worker_id)
    try:
      acquired = backend.set(
        task_claim_key(execution_id),
        owner,
        nx=True,
        ex=ttl_seconds,
      )
    except redis.RedisError as exc:
      # 抢占本身故障：按"放行"处理并在日志留痕。理由同上——
      # 保护机制不可用时宁可短暂失去幂等，也不能让执行链路停摆
      # （明细去重是第二道防线，不依赖本方法）
      logger.warning(
        f"任务幂等抢占异常，按放行处理（明细去重为第二道防线） | "
        f"execution_id={execution_id} | {exc}"
      )
      return True
    return bool(acquired)

  def _resolve_owner(self, worker_id: str | None) -> str:
    """
    解析属主标识：显式参数优先，其次客户端登记身份（内部方法）

    参数:
      worker_id (str | None): 显式传入的属主；None 表示未显式指定

    返回:
      str: 属主标识串（可能是空串，表示"无登记身份"）
    """
    if worker_id is not None:
      return str(worker_id)
    return str(self.worker_id)

  def _compare_and_del_claim(
    self, backend: Any, execution_id: str, expected_owner: str
  ) -> bool | None:
    """
    属主比对删除（compare-and-del，WATCH/MULTI/EXEC 原子提交）

    为什么必须是事务而不是"GET 之后 DEL"：两次命令之间 claim 可能
    已被 requeue_stale 接管为新属主，此时那个迟到的 DEL 会删掉**别人
    的**保护，等于把刚建立的闸又拆了。WATCH 让"读到的属主"与"被删的
    属主"之间不存在窗口——期间 key 被改动，EXEC 直接抛 WatchError。

    三态返回而非 bool，是为了把"读不到属主"与"属主不符"分开：两者
    的安全方向相反（见 release_claim 与 _takeover_stale_claim 的处置）。

    参数:
      backend (Any): 已构建的 Redis 后端
      execution_id (str): 执行批次号
      expected_owner (str): 期望的属主标识

    返回:
      bool | None: True=确认删除（含"本无 claim，已处于可重执行状态"）；
        False=属主不符或事务被抢占，未删除；
        None=无法读取属主（后端未提供事务管线或 Redis 异常），
        调用方须按各自的安全方向处置

    异常:
      无（Redis 异常一律降级为 None，不向上抛）
    """
    key = task_claim_key(execution_id)
    # 能力探测：精简替身/受限实现可能只提供部分命令。缺事务管线时
    # 无法做"读属主 + 条件删"，返回 None 让调用方 fail-closed，
    # 绝不退化成无条件 DEL（那正是本方法要修的缺陷）
    pipeline_factory = getattr(backend, "pipeline", None)
    if pipeline_factory is None:
      return None
    try:
      with pipeline_factory() as pipe:
        try:
          pipe.watch(key)
          # watch 模式下 pipeline 立即执行：此刻读到的值就是
          # EXEC 将要保护的那个值
          current = pipe.get(key)
          if current is None:
            pipe.unwatch()
            return True
          if str(current) != expected_owner:
            pipe.unwatch()
            return False
          pipe.multi()
          pipe.delete(key)
          deleted = pipe.execute()
        except redis.WatchError:
          # WATCH 期间 key 被他人改动：属主已易主，本轮放弃删除
          return False
    except redis.RedisError as exc:
      logger.warning(
        f"claim 属主比对异常，无法判定 | "
        f"execution_id={execution_id} | {exc}"
      )
      return None
    return bool(deleted)

  def release_claim(self, execution_id: str, worker_id: str | None = None) -> bool:
    """
    释放幂等抢占（属主比对删除，批次进入终态后调用）

    只删**自己持有**的 claim：worker 主循环每轮收尾都会调用本方法，
    其中包括"入口被别的 worker 的 claim 挡下、零用例执行"的那一轮。
    修复前的无条件 DEL 会让那一轮把别人的保护拆掉，使 claim 层对
    "进程冻结但未死透"的防护只剩一个重投周期（Day52 P2）。

    读不到属主时**拒绝释放**（fail-closed）：无法确认归属就不动手，
    最坏结果是这条 claim 留到 TTL 自然过期（3600s）后自动放行，
    而误删的后果是双跑防线被当场拆除。

    参数:
      execution_id (str): 执行批次号
      worker_id (str | None): 期望属主；None 时取客户端登记身份

    返回:
      bool: 删除成功或本就无 claim True；属主不符/读不到属主/
      后端不可用/批次号为空 False

    异常:
      无（内部全部降级为 False，不向上抛）
    """
    if not execution_id:
      return False
    backend = self._get_backend()
    if backend is None:
      return False
    expected_owner = self._resolve_owner(worker_id)
    outcome = self._compare_and_del_claim(backend, execution_id, expected_owner)
    if outcome is None:
      logger.warning(
        f"claim 属主不可读，拒绝释放（保守：宁可等 TTL 过期也不误删他人保护）| "
        f"execution_id={execution_id} | 期望属主={expected_owner or '(空)'}"
      )
      return False
    if outcome is False:
      logger.warning(
        f"拒绝释放非自有 claim（被挡轮次不得拆掉他人幂等保护）| "
        f"execution_id={execution_id} | 期望属主={expected_owner or '(空)'}"
      )
      return False
    return True

  def _takeover_stale_claim(self, execution_id: str, dead_owner: str) -> bool:
    """
    接管"已判定属主死亡"的 claim（重投前置，Day52 P2）

    只允许在调用方**已经用心跳陈旧判定过属主死亡**之后调用——这是
    它与 `release_claim` 的根本区别：release_claim 是 worker 清理
    自己的东西，只能凭属主比对动手；本方法是替系统执行死亡认定后
    回收无主资源，两者的授权来源根本不同。

    属主比对仍要读，但**失配时按 fail-open 继续接管**（Day52-fix P2-2）：
    比对失配最常见的成因不是"有活人抢走了 claim"，而是**被挡轮次覆写**——
    `mark_in_flight` 在 `_execute_batch_async`（内部 claim）**之前**执行，
    于是被 A 的 claim 挡下的 B 早已把状态 hash 的 worker_id 写成自己。
    此时 `dead_owner` 是 B（被挡者），claim 实际属主才是真正死掉的 A。

    为什么失配仍可安全接管：调用方已用心跳判过超时（>60s），而**活着的**
    worker 其心跳线程会持续刷新 heartbeat_at，根本走不到这一步。故失配时
    claim 的实际属主几乎必然就是死亡者，按它接管才对；若坚持失配即跳过，
    崩溃恢复会从 60s 退化到 claim TTL 3600s，且 3 次重投全被挡、先转死信。

    读不到属主时同样按"无活属主"继续（与 release_claim 的 fail-closed
    方向相反）：这里的对手方是"崩溃后无人回收的任务"，读不到属主若
    就不动，等于把唯一能救回任务的路径掐断；而认错代价由
    MAX_REQUEUE_ATTEMPTS 与明细分桶去重兜住。

    参数:
      execution_id (str): 执行批次号
      dead_owner (str): 本方法上游判定为死亡的属主标识；
        空串表示状态 hash 里没有 worker_id 记录（老数据/
        状态 hash 已过期），无法比对属主

    返回:
      bool: 接管是否放行。本方法**恒返回 True**——失配也接管，
        只有"后端不可用/读不到属主"等情形才会在更早的分支提前返回，
        但那些分支同样返回 True；保留 bool 仅为向后兼容签名与
        降级测试断言；调用方已不分支（恒返回 True，接管恒放行）。

    异常:
      无（Redis 异常降级为"继续"，不阻断重投）
    """
    backend = self._get_backend()
    if backend is None:
      return True
    key = task_claim_key(execution_id)
    # 能力探测：精简替身/受限实现可能不提供 GET，读不到属主即按
    # "无活属主"继续（方向说明见上方 docstring）
    getter = getattr(backend, "get", None)
    if getter is None:
      return True
    try:
      current = getter(key)
    except redis.RedisError as exc:
      logger.warning(
        f"接管前读取 claim 属主失败，按无活属主继续重投 | "
        f"execution_id={execution_id} | {exc}"
      )
      return True
    if current is None:
      return True
    if dead_owner and str(current) != dead_owner:
      # 失配 → fail-open 继续接管（Day52-fix P2-2）。
      #
      # 失配的主因是"被挡轮次覆写属主"：mark_in_flight 在 claim 之前
      # 执行，被 A 的 claim 挡下的 B 早已把状态 hash 的 worker_id 写成
      # 自己，于是 dead_owner=B 而 claim 实际属主=真正死掉的 A。
      # 若在这里 return False，崩溃恢复就从心跳 TTL 的 60s 退化到
      # claim TTL 的 3600s，且 3 次重投全被这道闸挡下、先转死信。
      #
      # 之所以敢 fail-open：调用方已判过心跳超时（>60s），而**活着**的
      # worker 其心跳线程会持续刷新 heartbeat_at，根本走不到这一步。
      # 即"claim 实际属主"比"状态 hash 属主"更接近真正的死亡者。
      logger.warning(
        f"接管时 claim 属主与状态 hash 属主不一致（多为被挡轮次覆写所致），"
        f"按心跳超时判定 fail-open 接管 | execution_id={execution_id} | "
        f"claim属主={current} | 状态hash属主={dead_owner}"
      )
      # 不 return：继续向下执行 DELETE
    try:
      backend.delete(key)
    except redis.RedisError as exc:
      logger.warning(
        f"死亡属主 claim 接管失败（重投后可能被该 claim 挡住）| "
        f"execution_id={execution_id} | 判定死亡的属主="
        f"{dead_owner or '(未知)'} | {exc}"
      )
      return True
    logger.warning(
      f"已接管判定死亡属主的 claim，使重投任务可重新抢占 | "
      f"execution_id={execution_id} | 原属主={current}"
    )
    return True


class TaskWorker:
  """
  任务队列消费者（单worker串行循环）

  循环语义:
    while not stop_event.is_set():
      payload = dequeue(BRPOP有限超时)
      payload为None（超时/异常）→ continue重新检查停止信号
      取到任务 → set_status(running) →
      CaseManager._execute_batch_async执行编排 →
      finally按批次DB终态set_status(finished/failed)

  健壮性:
    - BRPOPLPUSH必须有限超时，stop_event才能在超时粒度内被响应，
     禁止永久阻塞（redis-py 的 timeout=0 是"永久阻塞"而非"不阻塞"，
     故 <=0 一律夹到 MIN_BRPOP_TIMEOUT_SECONDS）
    - 单个任务的任何异常都被捕获，worker线程绝不因任务失败
     而退出循环（_execute_batch_async内部已把批次异常兜成
     failed状态，外层再做双保险）
    - 故障退避（Day45 问题1）：dequeue 因故障失败时按连续次数指数
     退避（1/2/4/8/16/30 秒封顶），不再出现"后端已构建但连接断开"
     的零延时热转
    - 崩溃补偿（Day45 问题4）：任务执行完 ack() 移出在途 list；
     worker 崩溃时任务留在在途 list，空闲轮次由 requeue_stale()
     按停留时长重新投回

  属性:
    queue_client (TaskQueueClient): 队列客户端
    stop_event (threading.Event): 停止信号事件
  """

  def __init__(
    self,
    queue_client: TaskQueueClient,
    stop_event: threading.Event,
    worker_id: str | None = None,
  ) -> None:
    """
    初始化worker

    参数:
      queue_client (TaskQueueClient): 队列客户端实例
      stop_event (threading.Event): 停止信号（set后循环在
                      下一个BRPOP超时边界退出）
      worker_id (str | None): worker 唯一标识；None 时按
                  "主机名-pid-随机串" 自动生成
                  （Day51：任务一被谁执行必须是可查事实）

    返回:
      None

    异常:
      无
    """
    self.queue_client = queue_client
    self.stop_event = stop_event
    # Day51 任务二 (d)：worker 身份。任务一被取出即写入在途状态
    # hash 的 worker_id 字段，排障时能直接回答"这条任务卡在哪个
    # worker 上"，而不是面对一个匿名报文猜。
    self.worker_id = worker_id if worker_id else generate_worker_id()
    # Day52 P2：把身份登记到队列客户端上，供 claim/release_claim 在
    # 未显式传参时做属主比对。真正的 claim() 调用点在
    # `case_manager._execute_batch_async`（不在当日白名单），只能靠
    # 客户端持有身份把这条链路接起来——一个进程一个客户端一个 worker，
    # 登记方与读取方同属一方，不存在把 A 的身份用到 B 上的可能
    self.queue_client.worker_id = self.worker_id
    # 当前正在执行的批次号（None 表示空闲）。心跳线程只刷新它，
    # 避免在空闲期给无关批次写心跳。
    self._current_execution_id: str | None = None
    # 心跳线程与其独立停止信号（按任务起停，见 _heartbeat_loop）
    self._heartbeat_thread: threading.Thread | None = None
    self._heartbeat_stop: threading.Event | None = None

  @property
  def heartbeat_interval(self) -> float:
    """
    心跳刷新间隔秒数（TM_TASK_HEARTBEAT_INTERVAL，默认15s）

    取值必须显著小于 HEARTBEAT_TTL_SECONDS（默认60s），否则
    "刷新间隔 >= 陈旧阈值"会让每个正常执行的批次都在两次刷新之间
    被 requeue_stale 误判为崩溃。这里做一次夹逼：配置值若大于
    TTL 的一半则夹到 TTL/2，宁可刷新勤快也不误判崩溃。

    参数:
      无

    返回:
      float: 心跳间隔秒数
    """
    configured = env_manager.get_float(
      "TM_TASK_HEARTBEAT_INTERVAL", HEARTBEAT_INTERVAL_SECONDS
    )
    if configured <= 0:
      return HEARTBEAT_INTERVAL_SECONDS
    return min(configured, HEARTBEAT_TTL_SECONDS / 2)

  def _heartbeat_loop(self, stop_signal: threading.Event) -> None:
    """
    心跳刷新线程主体（Day51 任务二 d）

    为什么必须是独立线程而不是"每处理完一条用例刷新一次"：
    _execute_batch_async 是**同步阻塞**的一次性调用——worker 主循环
    在它返回之前根本拿不到控制权，而真实执行的一条用例可能跑几十秒
    到几分钟。只在用例之间刷新的话，一批里只要有单条慢用例，
    心跳就会在它执行期间完全停摆，requeue_stale 会在它跑到一半时
    把任务重投出去——**补偿机制自己制造了它本该防止的双跑**。

    用 Event.wait 而非 time.sleep：停止信号一置位就立即唤醒，
    不会让收尾多等一个心跳周期。

    **线程按任务起停（而不是随 worker 常驻）**：心跳只在"确实有任务
    在执行"时才有意义，空闲期常驻一个空转线程纯属浪费；更实际的原因
    是它必须有自己的停止信号——复用 worker 的 stop_event 会让心跳
    的等待与主循环的**故障退避**混在同一个事件上（既有 Day44 回归
    用例正是按"退避恰好触发 1 次"断言的，两者共用会让判据失效）。

    参数:
      stop_signal (threading.Event): 本次任务的停止信号（执行收尾时置位）

    返回:
      None

    异常:
      无（循环体逐轮兜底，任何异常都不得让心跳线程静默死亡）
    """
    while not stop_signal.is_set():
      # wait 返回 True 表示收到停止信号，立即退出，不多等一个周期
      if stop_signal.wait(self.heartbeat_interval):
        break
      execution_id = self._current_execution_id
      if not execution_id:
        # 空闲期没有在途任务，无需心跳
        continue
      try:
        self.queue_client.heartbeat(execution_id)
      except Exception as exc:  # noqa: BLE001 心跳线程绝不能因单轮异常退出
        logger.debug(
          f"心跳线程单轮刷新异常（继续下一轮）| "
          f"execution_id={execution_id} | {exc}"
        )

  def _start_heartbeat(self) -> None:
    """
    任务开始执行时拉起心跳线程（内部方法）

    参数:
      无

    返回:
      None
    """
    self._stop_heartbeat()
    self._heartbeat_stop = threading.Event()
    self._heartbeat_thread = threading.Thread(
      target=self._heartbeat_loop,
      args=(self._heartbeat_stop,),
      name="tm-task-heartbeat",
      daemon=True,
    )
    self._heartbeat_thread.start()

  def _stop_heartbeat(self) -> None:
    """
    任务收尾时停掉心跳线程并等待其退出（内部方法）

    参数:
      无

    返回:
      None
    """
    thread = self._heartbeat_thread
    self._heartbeat_thread = None
    if self._heartbeat_stop is not None:
      self._heartbeat_stop.set()
      self._heartbeat_stop = None
    if thread is not None and thread.is_alive():
      thread.join(timeout=self.heartbeat_interval + 1.0)
      if thread.is_alive():
        logger.warning("心跳线程未在预算内退出，已按daemon随进程退出")

  def run(self) -> None:
    """
    worker线程主循环（串行消费直到收到停止信号）

    参数:
      无

    返回:
      无

    异常:
      无（循环内全部异常捕获并记录，保证线程不崩）
    """
    # 延迟导入避免模块加载期的无谓依赖（实际无环，保持与
    # case_manager引用方向单一: 队列层依赖核心层而非反之）
    from src.core.case_manager import CaseManager

    logger.info(
      f"任务队列worker已启动 | 队列key={self.queue_client.queue_key} | "
      f"BRPOP超时={self.queue_client.brpop_timeout}s | "
      f"worker_id={self.worker_id} | "
      f"心跳间隔={self.heartbeat_interval}s"
    )
    self._consume_loop(CaseManager)

  def _consume_loop(self, case_manager_cls: Any) -> None:
    """
    worker 消费主循环（Day51：与心跳启动解耦，便于统一收尾）

    参数:
      case_manager_cls (Any): CaseManager 类对象（延迟导入后传入，
        显式传参而非循环内重复 import）

    返回:
      None

    异常:
      无（循环内全部异常捕获并记录，保证线程不崩）
    """
    CaseManager = case_manager_cls
    while not self.stop_event.is_set():
      # 结构性兜底（Day44 P3-01）：本 docstring 承诺"循环内全部异常
      # 捕获并记录，保证线程不崩"，但修复前只有 _execute_batch_async
      # 调用点包了 try，dequeue() 与 set_status() 都在任何 try 之外——
      # 任何未预料的异常都会逃出线程目标函数、直接杀死消费线程，
      # 且业务日志无痕（只有 stderr 栈），后续任务永久堆积。
      # 这里给整个循环体加最外层 except，兑现契约。
      try:
        # 有限超时阻塞取任务: 超时/异常均返回None，循环回头
        # 检查stop_event，保证停止信号最迟一个超时周期生效
        payload = self.queue_client.dequeue()
        if payload is None:
          # 退避闸（Day44 热修 P1-1 + Day45 问题1 重写）：
          #
          # 修复前判据是 `self.queue_client._backend is None`，
          # 只覆盖"后端未构建"；Day45 全量审查实测发现
          # **后端已构建成功、运行期连接断开**时该判据为假，
          # dequeue 立即返回 None（且不阻塞），于是直接 continue
          # 形成零延时热转——实测 2 秒 21124 次空转、每轮一条
          # WARNING（外推 1 小时约 3800 万条，CPU 占满一核 +
          # 磁盘写满），与 Day44 修掉的症状完全同类。
          #
          # 现改用 dequeue 自己记录的故障状态：它区分
          # "队列空"（BRPOP 正常超时，通信成功）与"故障"
          # （Redis 异常或后端不可用），并按连续失败次数指数
          # 退避（1/2/4/8/16/30 秒封顶），恢复后计数归零。
          # wait() 而非 sleep()：它同时监听 stop_event，
          # 因此退避不会让 stop_worker() 响应变慢。
          if self.queue_client.has_recent_failure():
            backoff = self.queue_client.get_failure_backoff()
            if backoff > 0:
              self.stop_event.wait(backoff)
            continue
          # 队列空：正常后端下 dequeue 已在 BRPOPLPUSH 上阻塞了
          # brpop_timeout 秒，再多等 WORKER_IDLE_BACKOFF_SECONDS
          # 属于无谓延迟，仅在后端确实不可用时才退避。
          if self.queue_client._backend is None:
            self.stop_event.wait(WORKER_IDLE_BACKOFF_SECONDS)
          # 崩溃补偿（Day45 问题4）：队列空闲时把在途 list 中停留
          # 过久的任务重新投回队列。只在"队列空且无故障"时做——
          # 故障期扫在途 list 只会徒增 Redis 压力。
          else:
            self.queue_client.requeue_stale()
          continue

        # execution_id 显式为 null 时 .get(key, "") 的默认值取不到，
        # str(None) 会得到字面串 "None" 并让 not 判断为假，绕过
        # 畸形消息防御（Day44 P3-04）。这里先取原值再判空。
        raw_execution_id = payload.get("execution_id")
        execution_id = (
          "" if raw_execution_id is None else str(raw_execution_id).strip()
        )
        if not execution_id:
          # 防御: 畸形消息（缺字段 / null / 纯空白）直接跳过
          # （dequeue已验JSON）。必须在此 ack：任务此刻躺在
          # 在途 list 里，不确认移除会被 requeue_stale 当作
          # "崩溃残留"反复重投（永动循环）。
          logger.warning(
            f"任务缺少有效execution_id字段，已跳过 | payload={payload}"
          )
          self.queue_client.ack()
          continue

        # 在途归属 + 初始心跳（Day51 任务二 a/d）：
        # 任务一被取出就立刻记录"谁在执行"与"最后一次心跳"，
        # 而不是等执行结束才写状态——worker 在此之后任何时刻
        # 崩溃，requeue_stale 都已有一条带归属的陈旧记录可判
        self._current_execution_id = execution_id
        self.queue_client.mark_in_flight(execution_id, self.worker_id)
        # 心跳线程按任务起停（Day51）：只在真正执行批次期间存在，
        # 空闲期不空转，也不会把心跳等待与主循环的故障退避混在
        # 同一个 stop_event 上
        self._start_heartbeat()
        logger.info(
          f"worker开始执行任务 | execution_id={execution_id} | "
          f"worker_id={self.worker_id}"
        )
      except Exception as exc:  # noqa: BLE001 线程绝不能因单轮异常退出
        logger.error(
          f"worker单轮处理异常（已跳过本轮，循环继续）| "
          f"异常: {type(exc).__name__}: {exc}"
        )
        continue

      task_error: str | None = None
      try:
        # 复用既有批次编排（执行体零改动: 明细落库/event_bus
        # 埋点/通知旁路/缓存失效全部在_execute_batch_async内）
        CaseManager._execute_batch_async(
          execution_id=execution_id,
          cases=payload.get("cases", []),
          executor_kind=payload.get("executor_kind"),
        )
      except Exception as exc:  # noqa: BLE001 双保险: 理论上执行体已全兜底
        # worker绝不能因任务异常退出循环
        task_error = f"{type(exc).__name__}: {exc}"
        logger.error(
          f"worker执行任务抛异常（执行体应已兜底置failed）| "
          f"execution_id={execution_id} | {task_error}"
        )
      finally:
        # 执行结束即停止心跳：再往后刷新就成了给一个已经收尾的
        # 批次续命，会把"worker 还活着"这个信号错误延长到收尾之后
        self._current_execution_id = None
        self._stop_heartbeat()

      # 终态以SQLite批次表权威状态为准（执行体保证落终态），
      # Redis hash只是调度层镜像；查询失败按failed兜底记录
      terminal_status = STATUS_FINISHED
      terminal_error: str | None = task_error
      # state_confirmed：业务终态是否**已确认落库**。这是 Day51
      # ack 联动守卫的判据——ack 之后任务会同时从在途 list 与
      # requeue_stale 的视野里消失，此时若终态其实没写成功
      # （批次行查不到 / 状态查询抛错 / 查到的不是终态），
      # 批次就永久停在 running 且无任何补偿路径能捞回来。
      state_confirmed = False
      try:
        batch_status = CaseManager.get_execution_status(execution_id)
        if batch_status is not None:
          batch_state = batch_status.get("status")
          state_confirmed = batch_state in ("finished", "failed")
          if batch_state == "failed":
            terminal_status = STATUS_FAILED
            # 优先记录批次表中的批次级error_message
            terminal_error = (
              task_error or batch_status.get("error_message")
            )
          elif not state_confirmed:
            # 批次行存在却仍是非终态（pending/running）：
            # 执行体承诺会落终态，此刻没落即状态写入失效
            terminal_status = STATUS_FAILED
            terminal_error = (
              task_error or f"批次状态未达终态: {batch_state}"
            )
        else:
          terminal_status = STATUS_FAILED
          terminal_error = task_error or "批次状态查询为空"
      except Exception as exc:  # noqa: BLE001 状态查询失败不阻断收尾
        terminal_status = STATUS_FAILED
        terminal_error = task_error or f"终态查询异常: {exc}"

      # 终态写入与 ack 收成一条调用链（Day51 任务二 b）：
      # complete_task 内部固定"先落终态、再移出在途"的顺序，
      # 调用方无法把两者写反；state_confirmed=False 时它拒绝 ack，
      # 把在途条目留给 requeue_stale 走"至少一次"而非"至多一次"
      acked = self.queue_client.complete_task(
        execution_id,
        terminal_status,
        error=terminal_error,
        state_confirmed=state_confirmed,
      )
      # 释放幂等抢占（Day45 问题2）：批次已终态，允许同批次被
      # 重新执行（如失败后人工重跑），否则 claim 会把重试也挡住。
      # 刻意在 complete_task 之后释放：claim 是"正在执行"的保护，
      # 终态未确认前就释放会让重投回来的任务立刻二次抢到 claim
      # 而与本次执行并发。
      #
      # Day52 P2：显式带上 worker_id 做属主比对。这一轮若是被别的
      # worker 的 claim 挡下（零用例执行），比对会失败并原样保留
      # 那道保护——这正是修复前无条件 DEL 造成的双跑缺口。
      released = self.queue_client.release_claim(execution_id, self.worker_id)
      logger.info(
        f"worker任务结束 | execution_id={execution_id} | "
        f"终态={terminal_status} | 终态已确认={state_confirmed} | "
        f"已ack={acked} | claim已释放={released}"
      )

    logger.info("任务队列worker已停止")


# ===========================================================================
# 模块级单例与启停管理（start/stop幂等）
# ===========================================================================
# 队列客户端单例: 生产者(路由)与消费者(worker)共用同一连接
task_queue_client = TaskQueueClient()

# worker线程/停止事件/启停锁（模块级持有，保证只起一个worker）
_worker_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
_state_lock = threading.Lock()


def start_worker() -> threading.Thread | None:
  """
  启动应用内worker线程（幂等）

  仅TM_TASK_WORKER_ENABLED=true（缺省跟随队列开关）时实际
  创建daemon线程；已有存活worker时直接返回该线程不重复创建。
  daemon=True保证进程退出时worker不阻塞；正常停止走
  stop_worker的stop_event+join。

  参数:
    无

  返回:
    threading.Thread | None: worker线程对象；未启用时返回None

  异常:
    无（线程创建异常记error后返回None，调用方应用启动不受阻）
  """
  global _worker_thread, _stop_event
  with _state_lock:
    if not task_queue_client.worker_enabled:
      return None
    # 已有存活worker: 幂等返回，不重复起线程
    if _worker_thread is not None and _worker_thread.is_alive():
      return _worker_thread
    try:
      _stop_event = threading.Event()
      worker = TaskWorker(task_queue_client, _stop_event)
      _worker_thread = threading.Thread(
        target=worker.run,
        name="tm-task-worker",
        daemon=True,
      )
      _worker_thread.start()
      return _worker_thread
    except Exception as exc:  # noqa: BLE001 启动失败不阻断应用
      logger.error(f"任务队列worker启动异常 | {exc}")
      _worker_thread = None
      _stop_event = None
      return None


def stop_worker() -> None:
  """
  停止worker线程（幂等）

  置stop_event后join最多WORKER_JOIN_TIMEOUT_SECONDS秒，
  随后清空模块级引用（允许后续start_worker重建）。
  worker未启动时直接返回，不报错。

  参数:
    无

  返回:
    无

  异常:
    无（join异常静默忽略，daemon线程最终随进程退出）
  """
  global _worker_thread, _stop_event
  with _state_lock:
    thread = _worker_thread
    event = _stop_event
  if event is not None:
    # 通知worker在当前BRPOP超时边界退出循环
    event.set()
  if thread is not None and thread.is_alive():
    try:
      thread.join(timeout=WORKER_JOIN_TIMEOUT_SECONDS)
      if thread.is_alive():
        # join超时（在途长任务未跑完）: daemon不阻退出，仅记warning。
        # **保留模块级引用不置空**——置空会让 start_worker 的
        # 存活检查失效、下次调用再起一个 worker，两个 worker 并发
        # 消费同一队列，破坏"单worker串行"不变量。
        # 引用保留后，start_worker 靠 is_alive() 判定会返回本线程
        # （幂等），调用方可稍后重试 stop。
        logger.warning(
          f"worker在{WORKER_JOIN_TIMEOUT_SECONDS}s内未结束，"
          "daemon线程将随进程退出；模块级引用保留，"
          "start_worker的存活检查据此不会重复起worker"
        )
        return
    except RuntimeError as exc:
      logger.debug(f"worker join异常已忽略 | {exc}")
      return
  # 线程已结束（或本就没有存活worker）: 清空引用，允许后续start重建
  #
  # 必须条件清空（v3 修复 V2-P1-3）: 上面取 thread 后已释放 _state_lock，
  # join 期间（join 释放 GIL、可能长达 WORKER_JOIN_TIMEOUT_SECONDS）另一
  # 线程可调 start_worker——若原 worker 此时已退出，is_alive() 为 False，
  # start_worker 会建新 worker 并把 _worker_thread 指向它。无条件置 None
  # 会把**新 worker 的引用一并抹掉**，后果是: 它仍在跑却无人能停，且
  # 下次 start_worker 见 None 又会起一个，两个 worker 并发 BRPOP 同一
  # 队列，"单 worker 串行"不变量被破坏。
  with _state_lock:
    if _worker_thread is thread:
      _worker_thread = None
      _stop_event = None
      logger.debug("任务队列worker引用已清空")
