"""
内存事件总线（SSE实时日志推送的事件通道基础设施）

功能（第三阶段Day25交付）:
    - ExecutionEvent   执行事件数据类（event_type/data/timestamp
                       三字段统一传输单元）
    - EventChannel     单批次事件通道（线程安全的阻塞队列 +
                       条件变量，后台线程publish、SSE流式响应
                       subscribe消费）
    - VALID_EVENT_TYPES 合法事件类型枚举（单一事实来源，埋点方
                       与消费方共用）
    - get_channel      通道注册表查询（可选惰性建通道）
    - close_channel    通道关闭 + 注册表移除（批次终态后清理，
                       防内存泄漏）
    - reset_channels   注册表清空（测试fixture teardown专用，
                       防跨测试通道污染）

设计定位（重要，防误读）:
    - 本模块是 Day32 Redis pub/sub 队列化改造前的**过渡方案**:
      纯内存实现（零新第三方依赖），单进程内后台执行线程
      publish事件、HTTP流式响应subscribe消费；进程重启通道
      即丢（已终态批次走数据库快照直发，不依赖通道存活）；
      Day32换Redis pub/sub时保持本模块对外函数签名不变，
      仅替换内部实现即可
    - 单消费者模型: 同一通道同一时刻只支持一个订阅者完整
      消费（SSE流式接口独占）；多订阅者会竞争消费同一队列
      导致事件被分流——断线重连/多订阅者/历史回放是Day26
      SSE增强的范畴
    - 事件通道故障绝不影响真实执行: publish/close内部
      try/except兜底只记error日志（日志通道不能搞挂真实
      执行主流程）

线程安全:
    - EventChannel内部用threading.Condition（deque本身非线程
      安全，Condition内建锁保护入队/出队/关闭标记三处临界区）
    - 全局注册表用threading.RLock保护通道字典读写
    - subscribe的yield严格在锁外执行（消费方处理慢时绝不
      阻塞生产方publish）

使用示例:
    from src.core.event_bus import ExecutionEvent, get_channel

    # 生产方（后台执行线程）
    channel = get_channel("RUN-20260915-120000-ab12", create=True)
    channel.publish(ExecutionEvent(event_type="batch_start",
                                  data={"total_cases": 4}))

    # 消费方（SSE流式响应生成器）
    channel = get_channel("RUN-20260915-120000-ab12")
    for event in channel.subscribe():
        ...  # 阻塞迭代，直到通道关闭且队列排空
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterator, Optional

from src.common.logger import LogManager

logger = LogManager.get_logger()

# 合法事件类型枚举（单一事实来源: 埋点方与消费方共用本元组；
# publish收到非法类型直接丢弃并记警告，绝不抛异常）
VALID_EVENT_TYPES = (
    "batch_start",     # 批次开始执行（置running后发布）
    "case_finished",  # 单条用例执行完成（record_execution成功后发布）
    "batch_finished",  # 批次正常完成（置finished后发布）
    "batch_failed",    # 批次异常失败（置failed后发布）
)

# 订阅限时等待秒数: 队列空且未关闭时wait(0.5)后重查，
# 防notify丢失场景下订阅者永久挂死
SUBSCRIBE_WAIT_TIMEOUT_SECONDS = 0.5


@dataclass
class ExecutionEvent:
    """
    执行事件数据类

    SSE事件通道的统一传输单元，后台执行线程发布、SSE流式
    响应消费；timestamp由服务端时间戳生成，不信任客户端。

    属性:
        event_type (str): 事件类型，合法取值见VALID_EVENT_TYPES
        data (dict): 事件载荷（各类型字段约定:
                     batch_start={total_cases, executor_kind} /
                     case_finished={case_id, case_name, result,
                                    duration, error_message} /
                     batch_finished={total, passed, failed, error,
                                    skipped, pass_rate} /
                     batch_failed={error_message}）
        timestamp (float): 事件产生时间戳（time.time()秒级浮点，
                           缺省自动取当前时间）
    """

    event_type: str
    data: dict
    timestamp: float = field(default_factory=time.time)


class EventChannel:
    """
    单批次事件通道（一个execution_id一条通道）

    后台执行线程publish入队尾，SSE流式响应subscribe阻塞消费；
    collections.deque + threading.Condition实现线程安全。

    关闭语义（drain语义，防订阅者丢已发布事件）:
        - close后不再接受publish（丢弃 + 警告日志）
        - close前已入队的残余事件仍可被订阅者读完
        - closed且队列排空后subscribe迭代正常结束（生成器return）
    """

    def __init__(self) -> None:
        """
        初始化事件通道

        参数:
            无

        返回:
            无
        """
        self._queue: deque = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._close_reason = ""

    def publish(self, event: ExecutionEvent) -> None:
        """
        发布事件（入队尾 + 唤醒等待中的订阅者）

        铁律: 本方法绝不抛异常——日志通道故障不能搞挂真实执行
        主流程，任何内部异常仅记error日志后吞掉；已关闭通道的
        事件直接丢弃（批次终态后的迟到事件无消费意义）。

        参数:
            event (ExecutionEvent): 待发布事件

        返回:
            无

        异常:
            无（全部内部消化，只记日志）
        """
        try:
            if self._closed:
                logger.warning(
                    f"事件通道已关闭，事件被丢弃 | 类型: {event.event_type}"
                )
                return
            if event.event_type not in VALID_EVENT_TYPES:
                logger.warning(
                    f"非法事件类型，事件被丢弃 | 类型: {event.event_type} | "
                    f"合法取值: {list(VALID_EVENT_TYPES)}"
                )
                return
            with self._condition:
                self._queue.append(event)
                self._condition.notify_all()
        except Exception as exc:
            # 兜底铁律: 事件发布任何故障只记日志，不影响执行主流程
            logger.error(
                f"事件发布异常（已吞掉，不影响执行主流程）| "
                f"类型: {event.event_type} | {exc}"
            )

    def subscribe(self) -> Iterator[ExecutionEvent]:
        """
        订阅事件（阻塞迭代器）

        迭代语义:
            - 队列有事件: 立即弹出下一条并yield
            - 队列空且未关闭: wait(0.5s)限时等待（超时后重查，
              防notify丢失时永久挂死），被publish/close唤醒后重查
            - 已关闭且队列排空: 迭代正常结束（生成器return）

        注意: 单消费者模型——多个订阅者同时消费同一通道会竞争
        分流事件（Day26 SSE增强再解决多订阅者与回放）。

        参数:
            无

        返回:
            Iterator[ExecutionEvent]: 阻塞事件迭代器

        异常:
            无
        """
        while True:
            event: Optional[ExecutionEvent] = None
            with self._condition:
                if self._queue:
                    event = self._queue.popleft()
                elif self._closed:
                    # closed且队列排空: 迭代正常结束
                    return
                else:
                    # 限时等待防永久挂死: notify唤醒或超时后重查
                    self._condition.wait(
                        timeout=SUBSCRIBE_WAIT_TIMEOUT_SECONDS
                    )
            # yield严格在锁外: 消费方处理慢不阻塞生产方
            if event is not None:
                yield event

    def close(self, reason: str = "") -> None:
        """
        关闭通道（标记closed + 唤醒全部等待中的订阅者）

        关闭后残余事件仍可被订阅者读完（drain语义），只是不再
        接受新publish；本方法绝不抛异常（批次终态清理不能因
        日志通道故障失败）。

        参数:
            reason (str): 关闭原因（如"finished"/"failed"/"reset"，
                           供诊断与测试断言）

        返回:
            无

        异常:
            无
        """
        try:
            with self._condition:
                self._closed = True
                self._close_reason = reason
                self._condition.notify_all()
        except Exception as exc:
            logger.error(f"事件通道关闭异常 | 原因: {reason} | {exc}")

    @property
    def is_closed(self) -> bool:
        """
        通道关闭标记（属性）

        参数:
            无

        返回:
            bool: 已关闭返回True（bool读写原子性由GIL保证）
        """
        return self._closed

    @property
    def close_reason(self) -> str:
        """
        通道关闭原因（属性）

        参数:
            无

        返回:
            str: close时传入的reason，未关闭时为空串
        """
        return self._close_reason

    def snapshot(self) -> list:
        """
        队列当前快照（不消费）

        返回队列剩余事件的浅拷贝列表，供已终态批次补发积压
        事件使用（如SSE接口对刚终态通道的竞态窗口补发）；
        不弹出元素、不影响后续subscribe消费。

        参数:
            无

        返回:
            list[ExecutionEvent]: 当前队列快照（浅拷贝）

        异常:
            无
        """
        with self._condition:
            return list(self._queue)


# ===========================================================================
# 全局通道注册表（模块级单例）: execution_id → EventChannel
# ===========================================================================
_CHANNELS: dict = {}
_REGISTRY_LOCK = threading.RLock()


def get_channel(execution_id: str, create: bool = False) -> Optional[EventChannel]:
    """
    查询（可选创建）批次事件通道

    create=True时不存在则新建并注册（后台执行线程埋点用）；
    create=False时不存在返回None（路由层据此走降级分支）。
    RLock保证并发下"查询+创建"原子性，重复调用返回同一通道。

    参数:
        execution_id (str): 执行批次号
        create (bool): 不存在时是否新建通道，默认False

    返回:
        EventChannel | None: 对应通道；create=False且不存在
                            时返回None

    异常:
        无
    """
    with _REGISTRY_LOCK:
        channel = _CHANNELS.get(execution_id)
        if channel is None and create:
            channel = EventChannel()
            _CHANNELS[execution_id] = channel
        return channel


def close_channel(execution_id: str, reason: str = "") -> None:
    """
    关闭并移除批次事件通道（批次终态后清理，防内存泄漏）

    先从注册表移除（此后get_channel查不到），再标记通道closed
    （持有旧通道引用的订阅者仍能读完残余事件，drain语义）。

    参数:
        execution_id (str): 执行批次号
        reason (str): 关闭原因（"finished"/"failed"）

    返回:
        无

    异常:
        无
    """
    with _REGISTRY_LOCK:
        channel = _CHANNELS.pop(execution_id, None)
    if channel is not None:
        channel.close(reason)


def reset_channels() -> None:
    """
    清空通道注册表（测试fixture teardown专用）

    关闭全部存活通道后清空注册表，防跨测试通道污染（事件注册表
    是全局单例，残留通道会串测试导致flaky）；生产代码不调用。

    参数:
        无

    返回:
        无

    异常:
        无
    """
    with _REGISTRY_LOCK:
        channels = list(_CHANNELS.values())
        _CHANNELS.clear()
    for channel in channels:
        channel.close(reason="reset")
