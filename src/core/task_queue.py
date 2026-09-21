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
from datetime import datetime
from typing import Any, Optional

import redis

from src.common.env_manager import env_manager
from src.common.logger import LogManager

logger = LogManager.get_logger()

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
        self._backend: Optional[Any] = None

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
    def _get_backend(self) -> Optional[Any]:
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
        if self._backend is None:
            url = self.redis_url
            if url.startswith(FAKE_SCHEME):
                try:
                    import fakeredis

                    self._backend = fakeredis.FakeRedis(
                        decode_responses=True
                    )
                    logger.debug("任务队列后端已构建 | 类型: fakeredis内存实例")
                except ImportError as exc:
                    logger.warning(
                        f"任务队列已启用但fakeredis未安装，降级no-op | {exc}"
                    )
                    return None
            else:
                self._backend = redis.from_url(
                    url,
                    decode_responses=True,
                    socket_timeout=2,
                )
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

    def dequeue(self, timeout: Optional[float] = None) -> Optional[dict]:
        """
        阻塞式取出任务（消费者调用，BRPOP队列list尾部，FIFO）

        LPUSH头部 + BRPOP尾部构成先入先出；超时无任务返回None。
        任何Redis/反序列化异常返回None（worker循环继续，
        不因单条坏消息或瞬时故障退出）。

        参数:
            timeout (float | None): BRPOP阻塞超时秒数；None时用
                                    TM_TASK_BRPOP_TIMEOUT配置值；
                                    0表示不阻塞立即返回

        返回:
            dict | None: 任务payload字典；超时/未启用/异常时为None

        异常:
            无
        """
        backend = self._get_backend()
        if backend is None:
            return None
        effective_timeout = (
            timeout if isinstance(timeout, (int, float)) else self.brpop_timeout
        )
        try:
            # BRPOP返回(key, value)元组；超时返回None
            result = backend.brpop(self.queue_key, timeout=effective_timeout)
        except redis.RedisError as exc:
            logger.warning(f"任务出队异常，本轮跳过 | {exc}")
            return None
        if result is None:
            return None
        try:
            _, raw_payload = result
            return json.loads(raw_payload)
        except (ValueError, TypeError) as exc:
            # 坏消息不卡死worker: 记warning后跳过（消息已被BRPOP移除）
            logger.warning(f"任务载荷反序列化失败，消息已丢弃 | {exc}")
            return None

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
        except redis.RedisError as exc:
            logger.warning(
                f"任务状态写入异常，已跳过 | execution_id={execution_id} | {exc}"
            )

    def get_status(self, execution_id: str) -> Optional[dict]:
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
        - BRPOP必须有限超时，stop_event才能在超时粒度内被响应，
          禁止永久阻塞
        - 单个任务的任何异常都被捕获，worker线程绝不因任务失败
          而退出循环（_execute_batch_async内部已把批次异常兜成
          failed状态，外层再做双保险）

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
            # 有限超时阻塞取任务: 超时/异常均返回None，循环回头
            # 检查stop_event，保证停止信号最迟一个超时周期生效
            payload = self.queue_client.dequeue()
            if payload is None:
                continue

            execution_id = str(payload.get("execution_id", ""))
            if not execution_id:
                # 防御: 畸形消息无批次号直接跳过（dequeue已验JSON）
                logger.warning(f"任务缺少execution_id字段，已跳过 | payload={payload}")
                continue

            # 调度状态置running并记开始时间
            self.queue_client.set_status(
                execution_id,
                STATUS_RUNNING,
                started_at=datetime.now().isoformat(),
            )
            logger.info(f"worker开始执行任务 | execution_id={execution_id}")

            task_error: Optional[str] = None
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
            terminal_error: Optional[str] = task_error
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
_worker_thread: Optional[threading.Thread] = None
_stop_event: Optional[threading.Event] = None
_state_lock = threading.Lock()


def start_worker() -> Optional[threading.Thread]:
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
                # join超时（在途长任务未跑完）: daemon不阻退出，
                # 仅记warning；测试fixture场景下批次都是秒级模拟执行
                logger.warning(
                    f"worker在{WORKER_JOIN_TIMEOUT_SECONDS}s内未结束，"
                    "daemon线程将随进程退出"
                )
        except RuntimeError as exc:
            logger.debug(f"worker join异常已忽略 | {exc}")
    with _state_lock:
        _worker_thread = None
        _stop_event = None
    logger.debug("任务队列worker引用已清空")
