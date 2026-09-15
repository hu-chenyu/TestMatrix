"""
内存事件总线（SSE实时事件推送的事件通道基础设施）

功能（第三阶段Day25交付，Day26增强为多订阅者广播模型）:
    - ExecutionEvent   执行事件数据类（event_type/data/timestamp/
                       event_id四字段统一传输单元，event_id是
                       游标定位与Last-Event-ID断点回放的唯一依据）
    - EventChannel     单批次事件通道（有界历史环 + 条件变量实现
                       多订阅者广播：发布方只追加不消费，各订阅者
                       维护独立游标，互不干扰都能看完整事件序列）
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
      publish事件、HTTP流式响应subscribe消费；进程重启通道即丢
      （已终态批次走数据库快照直发，不依赖通道存活）；
      Day32换Redis pub/sub时保持本模块对外函数签名不变，
      仅替换内部实现即可
    - 多订阅者广播模型（Day26）: 历史环是非消费式的（只追加
      不弹出），多个订阅者各自维护独立游标先后订阅同一通道，
      都能读到完整事件序列互不分流；这是对Day25单消费者
      （竞争消费deque）的**有意行为变更**
    - 历史环有界（maxlen=1000）超容量自动淘汰最旧事件
      （best-effort回放窗口）: 断线客户端Last-Event-ID早于最旧
      存活事件时从最旧开始回放不报错；超出回放窗口的客户端由
      路由层走数据库重建分支兜底，不能指望历史环无限保留
    - 事件通道故障绝不影响真实执行: publish/close内部
      try/except兜底只记error日志（日志通道不能搞挂真实
      执行主流程）

线程安全:
    - EventChannel内部用threading.Condition（deque本身非线程
      安全，Condition内建锁保护追加/游标推进/关闭标记三处临界区）
    - 全局注册表用threading.RLock保护通道字典读写
    - subscribe的yield严格在锁外执行（消费方处理慢时绝不
      阻塞生产方publish）

使用示例:
    from src.core.event_bus import ExecutionEvent, get_channel

    # 生产方（后台执行线程）
    channel = get_channel("RUN-20260916-120000-ab12", create=True)
    channel.publish(ExecutionEvent(event_type="batch_start",
                                  data={"total_cases": 4}))
    # publish在锁内为事件分配单调递增event_id（从1起）

    # 消费方（SSE流式响应生成器，多订阅者广播）
    channel = get_channel("RUN-20260916-120000-ab12")
    for event in channel.subscribe(last_event_id=None, tick=False):
        ...  # 阻塞迭代，直到通道关闭且游标追平最新事件
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

# 订阅限时等待秒数: 历史环无新事件且未关闭时wait(0.5)后重查，
# 防notify丢失场景下订阅者永久挂死；tick=True时该超时同时是
# 心跳节拍（yield None交由路由层决定是否发注释帧）
SUBSCRIBE_WAIT_TIMEOUT_SECONDS = 0.5

# 历史环容量上限: 单批次最多保留的近期事件数，超容量自动淘汰
# 最旧事件（best-effort回放窗口，超出窗口的断线客户端由路由层
# 走数据库重建分支兜底）
HISTORY_RING_MAXLEN = 1000


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
        event_id (int | None): 事件序号（publish时在锁内分配的
                              单调递增整数，从1起；是订阅游标定位
                              与Last-Event-ID断点回放的唯一依据，
                              手工构造时缺省None、入通道即被覆写）
    """

    event_type: str
    data: dict
    timestamp: float = field(default_factory=time.time)
    event_id: Optional[int] = None


class EventChannel:
    """
    单批次事件通道（一个execution_id一条通道，多订阅者广播）

    后台执行线程publish向有界历史环追加事件，SSE流式响应
    subscribe按各自独立游标读取；collections.deque(maxlen) +
    threading.Condition实现线程安全。

    广播语义（Day26，与Day25单消费者模型的区别）:
        - 历史环只追加不消费（不再popleft），多个订阅者先后
          订阅都能看完整事件序列，互不干扰
        - publish在锁内为事件分配单调递增event_id（从1起，
          通道级计数器，跨订阅者唯一）
        - 订阅者各自维护独立游标（已读到的最后一条event_id），
          只推进自己的游标

    关闭语义（drain语义，防订阅者丢已发布事件）:
        - close后不再接受publish（丢弃 + 警告日志）
        - close前已入环的残余事件仍可被订阅者读完（游标追平
          最新事件后迭代正常结束，生成器return）
        - closed且游标已追平时subscribe迭代正常结束

    回放窗口（best-effort边界）:
        - 历史环超容量自动淘汰最旧事件，last_event_id早于最旧
          存活事件时从最旧开始回放，不报错
        - 被淘汰的事件无法找回，超出窗口的客户端由路由层走
          数据库重建分支兜底
    """

    def __init__(self) -> None:
        """
        初始化事件通道

        参数:
            无

        返回:
            无
        """
        # 有界历史环: 只追加不消费，超容量自动淘汰最旧事件
        self._history: deque = deque(maxlen=HISTORY_RING_MAXLEN)
        self._condition = threading.Condition()
        self._closed = False
        self._close_reason = ""
        # 事件id单调递增分配器（从1起，publish在锁内自增）
        self._next_event_id = 1

    def publish(self, event: ExecutionEvent) -> None:
        """
        发布事件（锁内分配单调递增event_id + 追加历史环 +
        唤醒等待中的订阅者）

        铁律: 本方法绝不抛异常——日志通道故障不能搞挂真实执行
        主流程，任何内部异常仅记error日志后吞掉；已关闭通道的
        事件直接丢弃（批次终态后的迟到事件无消费意义）。

        参数:
            event (ExecutionEvent): 待发布事件（event_id由本方法
                                  在锁内覆写分配，调用方无需关心）

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
                # 锁内分配单调递增event_id（游标定位与Last-Event-ID
                # 回放的唯一依据），随后追加历史环（超容量自动淘汰）
                event.event_id = self._next_event_id
                self._next_event_id += 1
                self._history.append(event)
                self._condition.notify_all()
        except Exception as exc:
            # 兜底铁律: 事件发布任何故障只记日志，不影响执行主流程
            logger.error(
                f"事件发布异常（已吞掉，不影响执行主流程）| "
                f"类型: {event.event_type} | {exc}"
            )

    def subscribe(
        self,
        last_event_id: Optional[int] = None,
        tick: bool = False,
    ) -> Iterator[Optional[ExecutionEvent]]:
        """
        订阅事件（阻塞迭代器，多订阅者广播，各自独立游标）

        迭代语义:
            - 进入时从历史环筛 event_id > last_event_id 的全部
              事件批量回放（游标推进到最后一条）；last_event_id
              为None时从最旧存活事件全量回放；last_event_id早于
              最旧存活事件（超容量淘汰）时从最旧开始回放不报错
            - 游标落后于最新事件: 立即取出下一条并yield（锁外）
            - 无新事件且未关闭: wait(0.5s)限时等待（超时后重查，
              防notify丢失时永久挂死）；tick=True时超时yield None
              （心跳节拍，交由路由层决定是否发注释帧），tick=False
              时超时静默continue（Day25行为）
            - 已关闭且游标已追平最新事件: 迭代正常结束（生成器
              return，drain语义不变）

        多订阅者: 本方法不消费历史环（只推进自己的游标），多个
        订阅者先后/并发订阅同一通道互不干扰，都能看完整事件序列。

        参数:
            last_event_id (int | None): 断点回放锚点（客户端
                                       Last-Event-ID），None表示
                                       全量回放，缺省None
            tick (bool): 无新事件限时等待超时时是否yield None
                        心跳节拍，缺省False（保持静默continue）

        返回:
            Iterator[ExecutionEvent | None]: 阻塞事件迭代器；
            tick=True时可能yield None（心跳节拍），调用方须判空

        异常:
            无
        """
        # 本订阅者的独立游标: 已读到的最后一条event_id（None按0
        # 处理，从最旧存活事件开始全量回放）
        cursor = last_event_id if last_event_id is not None else 0
        while True:
            event: Optional[ExecutionEvent] = None
            wait_timed_out = False
            with self._condition:
                if self._history:
                    newest_id = self._history[-1].event_id
                    if cursor < newest_id:
                        # 历史环内event_id连续，按游标直接定位下一条:
                        # 最旧存活事件id为_history[0].event_id，游标
                        # 早于最旧存活事件时从头回放（best-effort）
                        oldest_id = self._history[0].event_id
                        start_index = max(0, cursor + 1 - oldest_id)
                        event = self._history[start_index]
                        cursor = event.event_id
                if event is None:
                    if self._closed:
                        # closed且游标已追平: 迭代正常结束
                        return
                    # 限时等待防永久挂死: notify唤醒或超时后重查；
                    # wait返回False表示超时（tick=True时的心跳节拍）
                    wait_timed_out = not self._condition.wait(
                        timeout=SUBSCRIBE_WAIT_TIMEOUT_SECONDS
                    )
            # yield严格在锁外: 消费方处理慢不阻塞生产方
            if event is not None:
                yield event
            elif tick and wait_timed_out:
                # 心跳节拍: 超时无新事件，交由路由层决定发不发注释帧
                yield None

    def close(self, reason: str = "") -> None:
        """
        关闭通道（标记closed + 唤醒全部等待中的订阅者）

        关闭后残余事件仍可被订阅者读完（drain语义，游标追平后
        迭代结束），只是不再接受新publish；本方法绝不抛异常
        （批次终态清理不能因日志通道故障失败）。

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
        历史环当前快照（不消费）

        返回历史环内全部存活事件的浅拷贝列表，供已终态批次补发
        积压事件使用（如SSE接口对刚终态通道的竞态窗口补发）；
        不影响后续subscribe回放（历史环是非消费式的）。

        参数:
            无

        返回:
            list[ExecutionEvent]: 当前历史环快照（浅拷贝）

        异常:
            无
        """
        with self._condition:
            return list(self._history)


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
