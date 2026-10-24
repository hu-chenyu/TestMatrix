"""
任务队列 enqueue→dequeue→ack 端到端契约用例（Day60）

验证背景（PROJECT_PLAN Day60"QueueBackend 窄协议收口"的验收要求）:
  收口要看的是**行为契约**，不是类结构。当前实现把"Redis 后端"与
  "fakeredis 内存后端"统一在 `TaskQueueClient` 内部（`_get_backend()`
  按 URL scheme 选择），后端差异对调用方完全透明——契约只要在
  `TaskQueueClient` 这一层成立，换不换后端都成立。

  本文件钉住收口真正要保的三件事:
    1. 入队→出队→ack 全链路走通，且 ack 之后任务同时从队列与在途
       list 消失（否则 requeue_stale 会把它当成崩溃残留重投）；
    2. 状态 hash 随链路流转 pending → running → finished；
    3. 没有"待确认任务"时 ack 返回 False 而不是抛异常（worker 收尾
       路径上 ack 可能被重复调用，抛异常会打死消费循环）。

隔离方式:
  每个用例独立 fakeredis 内存实例（TM_REDIS_URL=fake://）与独立队列
  key，零外部依赖、零跨用例残留。
"""

import allure
import pytest
from src.core.task_queue import TaskQueueClient


@pytest.fixture()
def queue_client(monkeypatch: pytest.MonkeyPatch) -> TaskQueueClient:
  """
  启用 fake 后端的独立队列客户端 fixture

  参数:
    monkeypatch (pytest.MonkeyPatch): 环境变量覆写工具

  返回:
    TaskQueueClient: 指向 fakeredis 内存实例的新客户端
  """
  monkeypatch.setenv("TM_TASK_QUEUE_ENABLED", "true")
  monkeypatch.setenv("TM_REDIS_URL", "fake://0")
  # 独立队列 key：fakeredis 实例虽为进程内共享，但 key 隔离更彻底
  monkeypatch.setenv("TM_TASK_QUEUE_KEY", "tm:queue:day60-contract")
  return TaskQueueClient()


@allure.feature("Redis任务队列")
@allure.story("enqueue→dequeue→ack 契约")
@pytest.mark.api
@pytest.mark.regression
class TestQueueAckChain:
  """队列的三个方法的端到端契约（后端在实现内部，对外只有行为）"""

  def test_enqueue_dequeue_ack_chain(self, queue_client: TaskQueueClient) -> None:
    """
    全链路: 入队 → 出队（同时进入在途 list）→ 标记在途 → 写终态并 ack，
    结束时队列与在途 list 均为空、状态 hash 为 finished

    参数:
      queue_client (TaskQueueClient): fake 后端客户端 fixture

    返回:
      无
    """
    execution_id = "RUN-DAY60-QUEUE"
    payload = {
      "execution_id": execution_id,
      "executor_kind": "simulated",
      "cases": [{"case_id": "TM-Q-0001", "name": "队列契约用例"}],
    }
    backend = queue_client._get_backend()

    # 1) 入队：队列 1 条，在途 0 条
    assert queue_client.enqueue(execution_id, payload) is True
    assert backend.llen(queue_client.queue_key) == 1
    assert backend.llen(queue_client.processing_key) == 0

    # 2) 出队：报文出队即移入在途 list（BRPOPLPUSH 语义，不丢任务）
    assert queue_client.dequeue(timeout=0.5) == payload
    assert backend.llen(queue_client.queue_key) == 0
    assert backend.llen(queue_client.processing_key) == 1

    # 3) 在途归属与状态：running
    assert queue_client.mark_in_flight(execution_id, "worker-day60") is True
    running_status = queue_client.get_status(execution_id)
    assert running_status["status"] == "running"
    assert running_status["worker_id"] == "worker-day60"

    # 4) 终态 + ack 一条链：状态 finished，在途 list 清空
    assert queue_client.complete_task(execution_id, "finished") is True
    assert queue_client.get_status(execution_id)["status"] == "finished"
    assert backend.llen(queue_client.processing_key) == 0

  def test_ack_without_pending_task_returns_false(
    self, queue_client: TaskQueueClient
  ) -> None:
    """
    没有待确认任务时 ack 返回 False（不抛异常）

    为什么必须容忍: worker 收尾路径上 ack 可能被重复调用（终态已确认、
    任务已被别的路径清走），抛异常会把整个消费循环打死。

    参数:
      queue_client (TaskQueueClient): fake 后端客户端 fixture

    返回:
      无
    """
    assert queue_client.ack() is False

  def test_ack_twice_second_returns_false(self, queue_client: TaskQueueClient) -> None:
    """
    ack 幂等: 同一任务连续 ack 两次，第二次返回 False 而非抛错

    参数:
      queue_client (TaskQueueClient): fake 后端客户端 fixture

    返回:
      无
    """
    execution_id = "RUN-DAY60-ACK2"
    assert queue_client.enqueue(execution_id, {"execution_id": execution_id}) is True
    assert queue_client.dequeue(timeout=0.5) is not None
    assert queue_client.ack() is True
    assert queue_client.ack() is False

  def test_unconfirmed_state_rejects_ack(
    self, queue_client: TaskQueueClient
  ) -> None:
    """
    业务终态未确认时 complete_task 只写调度层终态、**拒绝 ack**（在途保留）

    这条是把"至多一次"降级为"至少一次"的那条路径：ack 完成后任务就从
    requeue_stale 视野里消失，若业务终态没落库，批次会永久停在 running。

    参数:
      queue_client (TaskQueueClient): fake 后端客户端 fixture

    返回:
      无
    """
    execution_id = "RUN-DAY60-UNCONFIRMED"
    assert queue_client.enqueue(execution_id, {"execution_id": execution_id}) is True
    assert queue_client.dequeue(timeout=0.5) is not None

    assert queue_client.complete_task(execution_id, "finished", state_confirmed=False) is False
    assert queue_client.get_status(execution_id)["status"] == "finished"
    # 在途条目仍在，等待崩溃补偿重投
    assert queue_client._get_backend().llen(queue_client.processing_key) == 1
