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
import threading
import time
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

    def requeue_stale(self, max_age_seconds: float = STALE_PROCESSING_SECONDS) -> int:
        """
        把在途 list 中停留过久的任务重新投回任务队列（崩溃补偿）

        无 ack 的后果（Day45 问题4）：旧的 BRPOP 弹出即删，进程被 kill
        时批次永久停在 pending/running，状态 hash 24h 后过期，无任何
        补偿路径。改 BRPOPLPUSH 后任务同时留在在途 list，本方法在
        worker 每轮空闲时调用，把超过 max_age_seconds 的在途任务重投。

        判定"停留过久"用调度状态 hash 的 started_at/queued_at 时间戳，
        缺失则视为陈旧（宁可重投一次，也不要永久丢任务——重复执行由
        execution_id 幂等抢占与明细去重兜住）。

        参数:
            max_age_seconds (float): 在途最大停留秒数，默认
                                      STALE_PROCESSING_SECONDS

        返回:
            int: 成功重新投回的任务条数
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
            if not self._is_stale_in_flight(execution_id, max_age_seconds):
                continue
            # 先移出在途再入队：顺序反了会在本轮 requeue_stale 里
            # 再次扫到自己，形成同轮自环
            self._discard_from_processing(raw)
            try:
                backend.lpush(self.queue_key, raw)
            except redis.RedisError as exc:
                logger.warning(f"陈旧任务重新入队失败 | {exc}")
                continue
            requeued += 1
            logger.warning(
                f"在途任务超时已重新入队（worker 崩溃补偿） | "
                f"execution_id={execution_id} | 阈值: {max_age_seconds}s"
            )
        return requeued

    def _is_stale_in_flight(self, execution_id: str, max_age_seconds: float) -> bool:
        """
        判定在途任务是否已停留过久（内部方法）

        参数:
            execution_id (str): 执行批次号
            max_age_seconds (float): 阈值秒数

        返回:
            bool: 超过阈值返回 True；状态 hash 缺失/时间不可解析也返回
                  True（宁可重投，重复执行由幂等抢占兜住）
        """
        status = self.get_status(execution_id)
        if not status:
            return True
        started = status.get("started_at") or status.get("queued_at")
        if not started:
            return True
        try:
            stamp = datetime.fromisoformat(str(started))
        except (ValueError, TypeError):
            return True
        # 时间戳可能带时区标识（tz-aware），而 datetime.now() 是 naive：
        # 两者直接相减会抛 TypeError（不是 ValueError），故先归一到 naive。
        # 队列状态 hash 由本模块写入（naive isoformat），带时区只可能是
        # 外部写入的异常数据；按"无法判定即视为陈旧"处理，避免反复重投。
        if stamp.tzinfo is not None:
            return True
        elapsed = (datetime.now() - stamp).total_seconds()
        return elapsed > max_age_seconds

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
    def claim(self, execution_id: str, ttl_seconds: int = CLAIM_TTL_SECONDS) -> bool:
        """
        以批次号做幂等抢占（SETNX + TTL），防同一批次被双跑

        要解决的场景：enqueue 的 lpush 已在 Redis 生效，但应答因
        socket_timeout 超时抛错 → enqueue 返回 False → 路由用**同一
        execution_id** 起裸线程；此时消息仍在队列里等 worker 消费 →
        同一批次被执行两次，明细双写、计数翻倍、通过率被污染。
        两条链路谁先抢到 claim 谁执行，后到者直接放弃。

        队列未启用（后端不可用）时**一律返回 True**：此时只有路由的裸线程
        一条路径，不存在双跑；用"抢不到就不执行"去阻断默认链路会
        直接让平台的核心功能失效——幂等保护不能反过来变成可用性风险。

        参数:
            execution_id (str): 执行批次号
            ttl_seconds (int): claim 存活秒数，默认 CLAIM_TTL_SECONDS

        返回:
            bool: 抢到（此前无人持有）返回 True；已被他人持有返回 False；
                  后端不可用时返回 True（不阻断执行）
        """
        if not execution_id:
            return False
        backend = self._get_backend()
        if backend is None:
            return True
        try:
            acquired = backend.set(
                task_claim_key(execution_id), "1", nx=True, ex=ttl_seconds
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

    def release_claim(self, execution_id: str) -> bool:
        """
        释放幂等抢占（批次进入终态后调用，允许同批次被重新执行）

        参数:
            execution_id (str): 执行批次号

        返回:
            bool: 释放成功 True；后端不可用/异常 False
        """
        if not execution_id:
            return False
        backend = self._get_backend()
        if backend is None:
            return False
        try:
            backend.delete(task_claim_key(execution_id))
            return True
        except redis.RedisError as exc:
            logger.warning(f"释放幂等抢占失败 | execution_id={execution_id} | {exc}")
            return False


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
        self, queue_client: TaskQueueClient, stop_event: threading.Event
    ) -> None:
        """
        初始化worker

        参数:
            queue_client (TaskQueueClient): 队列客户端实例
            stop_event (threading.Event): 停止信号（set后循环在
                                           下一个BRPOP超时边界退出）

        返回:
            无

        异常:
            无
        """
        self.queue_client = queue_client
        self.stop_event = stop_event

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
            f"BRPOP超时={self.queue_client.brpop_timeout}s"
        )
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

                # 调度状态置running并记开始时间
                self.queue_client.set_status(
                    execution_id,
                    STATUS_RUNNING,
                    started_at=datetime.now().isoformat(),
                )
                logger.info(f"worker开始执行任务 | execution_id={execution_id}")
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

            # 终态以SQLite批次表权威状态为准（执行体保证落终态），
            # Redis hash只是调度层镜像；查询失败按failed兜底记录
            terminal_status = STATUS_FINISHED
            terminal_error: str | None = task_error
            try:
                batch_status = CaseManager.get_execution_status(execution_id)
                if batch_status is not None:
                    if batch_status.get("status") == "failed":
                        terminal_status = STATUS_FAILED
                        # 优先记录批次表中的批次级error_message
                        terminal_error = (
                            task_error or batch_status.get("error_message")
                        )
                else:
                    terminal_status = STATUS_FAILED
                    terminal_error = task_error or "批次状态查询为空"
            except Exception as exc:  # noqa: BLE001 状态查询失败不阻断收尾
                terminal_status = STATUS_FAILED
                terminal_error = task_error or f"终态查询异常: {exc}"

            self.queue_client.set_status(
                execution_id,
                terminal_status,
                finished_at=datetime.now().isoformat(),
                error=terminal_error,
            )
            # 确认在途任务（Day45 问题4）：执行已收尾（无论终态是
            # finished 还是 failed，任务都不会再被重投），把它从在途 list
            # 移除。不 ack 的话它会一直留在那里，直到超过
            # STALE_PROCESSING_SECONDS 被 requeue_stale 当成"崩溃残留"
            # 重投一次——等于每批任务都白跑第二遍。
            self.queue_client.ack()
            # 释放幂等抢占（Day45 问题2）：批次已终态，允许同批次被
            # 重新执行（如失败后人工重跑），否则 claim 会把重试也挡住。
            self.queue_client.release_claim(execution_id)
            logger.info(
                f"worker任务结束 | execution_id={execution_id} | "
                f"终态={terminal_status}"
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
