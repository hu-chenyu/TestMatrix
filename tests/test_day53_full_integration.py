"""
Day53 全链路联调测试（Web 触发 → 真实执行 → 入库 → 看板刷新 + SSE 原始输出流 + 双模式切换）

覆盖范围:
  1. 执行输出实时通道（ExecutionOutputBus）单元行为:
    序号分配/行截断/有界环淘汰/历史回放+实时跟随/关闭 drain/空闲回收
  2. 真实执行器 subprocess 逐行输出捕获（PytestRunner + 真 pytest 子进程）
  3. 原始输出 SSE 流接口 /api/executions/<id>/output 的分支与帧格式
  4. 执行器双模式切换 API（GET/POST /api/executor/mode）与执行行为变化
  5. 全链路: Web 触发真实执行 → 结果入库 → 看板趋势接口可见
  6. Dashboard 通过率 clamp 防御（前端展示层）

数据隔离: 每条用例独立临时 SQLite 文件库；teardown 额外复位事件通道与
输出通道，并**还原 TM_EXECUTOR 环境变量**——双模式切换是进程级写入，
不还原会污染同会话后续用例（它们断言"未显式指定执行器时走 simulated"）。

时序约定（项目铁律）: 批次终态一律走**有界轮询**，禁止固定 sleep 赌时序；
真实子进程用例只用 `assert True` 级别的小文件，绝不触发全量嵌套回归。
"""

import math
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import allure
import pytest
from flask.testing import FlaskClient
from src.core import event_bus
from src.core import executors as executors_mod
from src.core.case_manager import CaseManager
from src.core.executors import (
  OUTPUT_BUS_IDLE_SECONDS,
  OUTPUT_RING_MAXLEN,
  OUTPUT_SOURCE_STDERR,
  OUTPUT_SOURCE_STDOUT,
  ExecutionOutputBus,
  PytestRunner,
  arm_output_capture,
  close_output_bus,
  get_output_bus,
  prune_idle_output_buses,
  reset_output_buses,
)
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app
from src.web.routes import executions as executions_mod
from src.web.routes.executions import EXECUTOR_MODE_ENV_KEY

# 项目根（本文件位于 tests/ 下，向上一级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 真实子进程用例的临时测试文件目录（output/ 已被 .gitignore 忽略）。
# Day54：目录名改为**会话级唯一**（同 Day53 conftest 的 per-run alluredir 思路），
# 唯一性由 <微秒时间戳> + <进程号> 双要素保证：时间戳区分同机先后启动的会话，
# pid 区分同一微秒节拍内的两次启动（Windows 时钟粒度约 15.6ms，同节拍内微秒位
# 可能完全相同）。修复前固定名 output/day53_scratch 会被并行会话的 teardown
# rmtree 删掉在途探针文件，表现为随机的 1 failed。
SCRATCH_DIRNAME = f"day53_scratch-{int(time.time() * 1e6)}-{os.getpid()}"

# 看板脚本路径（clamp 防御的静态断言对象）
DASHBOARD_JS = PROJECT_ROOT / "src" / "web" / "static" / "js" / "dashboard.js"

# 轮询参数: 模拟执行约 0.01s/条、真实执行单条约 1-2s，预算留足防 flaky
POLL_MAX_ATTEMPTS = 60
POLL_INTERVAL_SECONDS = 0.3

# 原始输出通道的批次号（真实子进程用例专用，避免与 Web 触发的批次号撞车）
STREAM_BATCH_ID = "RUN-DAY53-OUTPUT-STREAM-0001"

# 高频输出用例的耗时预算（秒，Day53-fix P2-a 回归）
# 150 行在修复前要 150 × 0.05s = 7.5s（/events 的帧间隔被复用），修复后
# 只受一次 0.5s 节拍支配；取 3.0s 是"离两边都远"的判据，不是经验值。
HIGH_FREQ_TIME_BUDGET_SECONDS = 3.0


# ===========================================================================
# 夹具与工具
# ===========================================================================
def seed_cases(source_ref: str | None = None) -> None:
  """
  造用例种子数据并入库（4条active）

  模块与末位奇偶分布（模拟执行结果可手算）:
    - 用户中心: TM-D53-0001（奇→failed）、TM-D53-0002（偶→passed）
    - 订单中心: TM-D53-0003（奇→failed）、TM-D53-0004（偶→passed）
  全量4条=2过2挂（通过率0.5），供"执行行为随模式变化"与看板断言共用。

  参数:
    source_ref (str | None): 可选的真实执行目标（仅第一条用例挂上，
                 用于全链路真实执行用例；None 表示全部
                 不挂 source_ref——pytest 模式下会按
                 "执行目标缺失"逐条降级 error，不起子进程）

  返回:
    无
  """
  seeds = [
    ("TM-D53-0001", "联调用例-用户登录成功", "用户中心"),
    ("TM-D53-0002", "联调用例-用户登录密码错误", "用户中心"),
    ("TM-D53-0003", "联调用例-订单创建成功", "订单中心"),
    ("TM-D53-0004", "联调用例-订单状态流转", "订单中心"),
  ]
  with DatabaseSession.session_scope() as session:
    for index, (case_id, name, module) in enumerate(seeds):
      session.add(
        models.TestCase(
          case_id=case_id,
          name=name,
          module=module,
          priority="P1",
          case_type="api",
          status="active",
          description="Day53 全链路联调种子用例",
          source_ref=source_ref if index == 0 else None,
        )
      )


@pytest.fixture
def day53_client(
  monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
  """
  Day53 联调客户端夹具（function级临时 SQLite 文件库）

  每条用例独立建库+种子造数，保证聚合断言精确无跨用例残留；
  teardown 依次复位 DatabaseSession 引擎、事件通道注册表、输出通道
  注册表，并**还原 TM_EXECUTOR**（见模块 docstring 的隔离说明）。

  参数:
    monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
    tmp_path (Path): pytest 临时目录

  返回:
    Iterator[FlaskClient]: yield 测试客户端
  """
  original_executor = os.environ.get(EXECUTOR_MODE_ENV_KEY)
  db_path = tmp_path / "day53_integration.db"
  monkeypatch.setenv("TM_DB_TYPE", "sqlite")
  monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
  monkeypatch.delenv(EXECUTOR_MODE_ENV_KEY, raising=False)
  DatabaseSession.reset()
  DatabaseSession.init_db()
  seed_cases()
  yield create_app("test").test_client()
  DatabaseSession.reset()
  event_bus.reset_channels()
  reset_output_buses()
  # 双模式切换写入的是进程级 os.environ，monkeypatch 不会自动回收它，
  # 必须显式还原——否则同会话后续用例会看到"默认执行器变成 pytest"
  if original_executor is None:
    os.environ.pop(EXECUTOR_MODE_ENV_KEY, None)
  else:
    os.environ[EXECUTOR_MODE_ENV_KEY] = original_executor
  db_path.unlink(missing_ok=True)


@pytest.fixture
def scratch_dir() -> Iterator[Path]:
  """
  在项目根 output/ 下准备可写的临时测试文件目录（用例结束后清理）

  为什么不用 tmp_path: 真实执行器的 source_ref 必须过形态校验
  （``^[\\w./-]+\\.py(::\\w+)*$``，禁盘符/反斜杠/".."），且 pytest 子进程
  的工作目录是项目根，只有项目内的相对路径才能被收集到。

  teardown 只删**本次会话自己**创建的唯一目录
  （output/day53_scratch-<时间戳>-<pid>），并行运行的另一会话不受影响。

  参数:
    无

  返回:
    Iterator[Path]: yield 本次会话独有的临时目录
  """
  target_dir = PROJECT_ROOT / "output" / SCRATCH_DIRNAME
  target_dir.mkdir(parents=True, exist_ok=True)
  yield target_dir
  shutil.rmtree(target_dir, ignore_errors=True)


@pytest.fixture
def make_source_ref(scratch_dir: Path) -> Callable[[str, str], str]:
  """
  造一个真实可执行的测试文件，返回其**项目相对** source_ref

  参数:
    scratch_dir (Path): scratch_dir fixture 提供的临时目录

  返回:
    Callable[[str, str], str]: 入参（模块名, 文件内容），返回形如
                  ``output/day53_scratch-<时间戳>-<pid>/x.py``
                  的相对路径
  """

  def _factory(module_name: str, body: str) -> str:
    (scratch_dir / f"{module_name}.py").write_text(body, encoding="utf-8")
    relative_dir = scratch_dir.relative_to(PROJECT_ROOT).as_posix()
    return f"{relative_dir}/{module_name}.py"

  return _factory


def _wait_batch_terminal(client: FlaskClient, execution_id: str) -> dict:
  """
  轮询批次状态至终态 finished/failed（内部方法，走真实 HTTP）

  集成测试铁律: 终态轮询必须走 GET /api/executions/<id>/status 接口层，
  禁止直调 CaseManager——本文件验证的是 API 全链路。同时防 teardown
  竞态: 触发了后台线程的用例必须等批次进终态再结束。

  参数:
    client (FlaskClient): Flask 测试客户端
    execution_id (str): 执行批次号

  返回:
    dict: 终态状态字典

  异常:
    AssertionError: 轮询预算内未达终态/状态查询非 200 时断言失败
  """
  status_data: dict | None = None
  for _ in range(POLL_MAX_ATTEMPTS):
    response = client.get(f"/api/executions/{execution_id}/status")
    assert response.status_code == 200, "轮询期间状态查询应始终200"
    status_data = response.get_json()["data"]
    if status_data["status"] in ("finished", "failed"):
      return status_data
    time.sleep(POLL_INTERVAL_SECONDS)
  pytest.fail(
    f"批次在轮询预算内未达终态: {execution_id}, "
    f"最后状态: {status_data['status'] if status_data else '无响应'}"
  )
  raise AssertionError("不可达: pytest.fail后代码不会执行到此处")


def _trigger(client: FlaskClient, body: dict | None = None) -> str:
  """
  走真实接口触发一次执行并返回批次号（内部方法）

  参数:
    client (FlaskClient): Flask 测试客户端
    body (dict | None): 触发请求体，None 表示无请求体（全量回归）

  返回:
    str: 批次号

  异常:
    AssertionError: 触发未返回 202 时断言失败
  """
  response = client.post("/api/executions/trigger", json=body or {})
  assert response.status_code == 202, "触发执行应返回202"
  return str(response.get_json()["data"]["execution_id"])


def _parse_sse_frames(text: str) -> list[tuple[str, dict]]:
  """
  把 SSE 响应文本解析成 (事件类型, 载荷) 列表（内部方法）

  只解析本项目自产帧（event/data 两行 + 空行），注释心跳帧（以 ":" 开头）
  直接跳过；data 行按 JSON 反序列化。

  参数:
    text (str): SSE 响应全文

  返回:
    list[tuple[str, dict]]: 帧序列；无法解析载荷的帧被跳过

  异常:
    无
  """
  import json as _json

  frames: list[tuple[str, dict]] = []
  event_type: str | None = None
  for raw_line in text.splitlines():
    line = raw_line.rstrip("\r")
    if line.startswith(":"):
      continue
    if line.startswith("event: "):
      event_type = line[len("event: ") :].strip()
      continue
    if line.startswith("data: ") and event_type is not None:
      try:
        frames.append((event_type, _json.loads(line[len("data: ") :])))
      except ValueError:
        continue
      event_type = None
  return frames


# ===========================================================================
# 1. 执行输出实时通道单元行为
# ===========================================================================
@allure.feature("Day53 全链路联调")
@allure.story("执行输出实时通道")
class TestExecutionOutputBus:
  """输出通道：序号/截断/有界环/回放+实时/关闭 drain"""

  @allure.title("发布行分配单调序号并剥离行尾换行")
  def test_publish_assigns_monotonic_seq_and_strips_newline(self) -> None:
    """
    行序号必须从 1 起单调递增（订阅游标的唯一依据），
    行尾的 \\r\\n / \\n 必须剥离（否则 SSE 帧里会出现空行，客户端断帧）。
    """
    bus = ExecutionOutputBus()
    bus.publish(OUTPUT_SOURCE_STDOUT, "第一行\n")
    bus.publish(OUTPUT_SOURCE_STDERR, "第二行\r\n")

    lines = bus.snapshot()
    assert [item.seq for item in lines] == [1, 2]
    assert lines[0].line == "第一行"
    assert lines[1].line == "第二行"
    assert lines[1].source == OUTPUT_SOURCE_STDERR

  @allure.title("超长行被截断且有界环淘汰最旧行")
  def test_long_line_truncated_and_ring_bounded(self) -> None:
    """
    单行超长必须截断（防 SSE 帧与内存被撑爆）；行数超容量上限时
    淘汰最旧行（实时展示是 best-effort 窗口，完整输出仍走落库路径）。
    """
    from src.core.executors import OUTPUT_LINE_MAX_LENGTH

    bus = ExecutionOutputBus()
    bus.publish(OUTPUT_SOURCE_STDOUT, "x" * (OUTPUT_LINE_MAX_LENGTH * 3))
    assert len(bus.snapshot()[0].line) <= OUTPUT_LINE_MAX_LENGTH + 16

    for index in range(OUTPUT_RING_MAXLEN + 20):
      bus.publish(OUTPUT_SOURCE_STDOUT, f"line-{index}")
    snapshot = bus.snapshot()
    assert len(snapshot) == OUTPUT_RING_MAXLEN
    # 最旧行已被淘汰，最新行仍在
    assert snapshot[-1].line == f"line-{OUTPUT_RING_MAXLEN + 19}"
    assert snapshot[0].line == f"line-{20}"

  @allure.title("订阅先回放历史再跟随实时行，关闭后正常结束")
  def test_subscribe_replays_then_follows_live(self) -> None:
    """
    订阅语义必须同时满足两件事: ①进入时把历史环里的行全量回放；
    ②之后新发布的行能被同一条迭代器读到；③通道关闭且游标追平后
    迭代正常结束（drain 语义，不永久挂死）。

    本用例刻意**不起线程**: 先拿到回放行，再在主线程 publish，
    下一次 next() 必然读到新行——用状态推进替代 sleep 赌时序。
    """
    bus = ExecutionOutputBus()
    bus.publish(OUTPUT_SOURCE_STDOUT, "历史行")

    stream: Iterator = bus.subscribe(tick=True)
    first = next(stream)
    assert first is not None and first.line == "历史行"

    bus.publish(OUTPUT_SOURCE_STDOUT, "实时行")
    second = next(stream)
    assert second is not None and second.line == "实时行"

    # 无新行时按节拍 yield None（调用方据此做心跳/终态判定）
    assert next(stream) is None

    bus.close(reason="finished")
    with pytest.raises(StopIteration):
      next(stream)

  @allure.title("关闭后拒绝新行且可读残余，空闲回收只收陈旧通道")
  def test_close_and_prune_semantics(self) -> None:
    """
    ①已关闭通道不再接受 publish（迟到输出无消费意义）；
    ②空闲回收只回收超过阈值的通道，活跃通道不受影响（防误收正在
    写入的批次）；③回收后注册表查询返回 None。
    """
    fresh_id = "RUN-DAY53-BUS-FRESH"
    stale_id = "RUN-DAY53-BUS-STALE"
    try:
      fresh_bus = arm_output_capture(fresh_id)
      stale_bus = arm_output_capture(stale_id)
      assert fresh_bus is not None and stale_bus is not None

      # 关闭后 publish 被丢弃
      stale_bus.close(reason="closed_for_test")
      before = len(stale_bus.snapshot())
      stale_bus.publish(OUTPUT_SOURCE_STDOUT, "迟到行")
      assert len(stale_bus.snapshot()) == before

      # 把一条通道伪造成"早于空闲阈值"（不动阈值本身，避免牵连他测）
      stale_bus.last_activity -= OUTPUT_BUS_IDLE_SECONDS + 60
      removed = prune_idle_output_buses()
      assert stale_id in removed
      assert get_output_bus(stale_id) is None
      assert get_output_bus(fresh_id) is not None
    finally:
      close_output_bus(fresh_id)
      close_output_bus(stale_id)

  @allure.title("空批次号不建通道（输出无处可归）")
  def test_empty_execution_id_creates_no_bus(self) -> None:
    """批次号为空/空白串时一律不建通道，防注册表里出现无名通道。"""
    assert arm_output_capture("") is None
    assert get_output_bus("  ") is None


# ===========================================================================
# 2. 真实执行器 subprocess 逐行输出捕获
# ===========================================================================
@allure.feature("Day53 全链路联调")
@allure.story("真实执行器输出捕获")
class TestRealExecutorOutputStreaming:
  """PytestRunner 在捕获开启时逐行读子进程输出"""

  @allure.title("捕获开启时真实子进程输出逐行进入输出通道")
  def test_run_one_streams_real_subprocess_output(
    self, scratch_dir: Path, make_source_ref: Callable[[str, str], str]
  ) -> None:
    """
    真起一个 pytest 子进程跑 `assert True`，断言:
      ①执行结果仍为 passed（逐行读不得改变退出码映射结果）；
      ②输出通道收到了子进程 stdout 行，且含 pytest 的汇总行
       （"1 passed"）——这正是"真实输出→SSE"链路的原料；
      ③ExecutionResult.output 仍按原口径带完整输出（落库路径不变）。
    """
    source_ref = make_source_ref(
      "test_day53_stream_ok", "def test_ok():\n  assert True\n"
    )
    try:
      arm_output_capture(STREAM_BATCH_ID)
      result = PytestRunner().run_one(
        {
          "case_id": "TM-D53-STREAM-0001",
          "source_ref": source_ref,
          "execution_id": STREAM_BATCH_ID,
        }
      )
      assert result.result == "passed", result.error_message
      assert result.output, "执行结果必须保留完整输出（落库口径不变）"

      bus = get_output_bus(STREAM_BATCH_ID)
      assert bus is not None
      streamed = [item.line for item in bus.snapshot()]
      assert streamed, "捕获开启时输出通道必须有行"
      assert any("passed" in line for line in streamed), streamed
    finally:
      close_output_bus(STREAM_BATCH_ID)

  @allure.title("未开启捕获时走既有一次性读取路径（零行为变更）")
  def test_run_one_without_capture_keeps_legacy_path(
    self, scratch_dir: Path, make_source_ref: Callable[[str, str], str]
  ) -> None:
    """
    未 arm 的批次不得建通道、不得改走逐行读：既有 59 个执行器用例的
    Popen 替身只有 communicate（stdout/stderr 是字符串），一旦默认
    走逐行读它们会集体变红——这条用例是那个"默认行为不变"的守卫。
    """
    source_ref = make_source_ref(
      "test_day53_legacy_ok", "def test_ok():\n  assert True\n"
    )
    unarmed_id = "RUN-DAY53-NOT-ARMED"
    assert get_output_bus(unarmed_id) is None

    result = PytestRunner().run_one(
      {
        "case_id": "TM-D53-LEGACY-0001",
        "source_ref": source_ref,
        "execution_id": unarmed_id,
      }
    )
    assert result.result == "passed", result.error_message
    assert get_output_bus(unarmed_id) is None

  @allure.title("子进程输出读取不丢行（两流合并且顺序稳定）")
  def test_stream_reads_both_stdout_and_stderr(
    self, scratch_dir: Path, make_source_ref: Callable[[str, str], str]
  ) -> None:
    """
    子进程同时往 stdout/stderr 写时两条管道都要被读到，不能因
    "只读 stdout" 让 stderr 写满缓冲区把进程卡死（本用例是双读线程
    的回归守卫）；真实 pytest 的 stderr 会带上插件/告警类输出。
    """
    source_ref = make_source_ref(
      "test_day53_fail_case",
      "def test_fail():\n  assert 1 == 2, 'day53 boom'\n",
    )
    try:
      arm_output_capture(STREAM_BATCH_ID)
      result = PytestRunner().run_one(
        {
          "case_id": "TM-D53-STREAM-FAIL",
          "source_ref": source_ref,
          "execution_id": STREAM_BATCH_ID,
        }
      )
      assert result.result == "failed", result.error_message
      bus = get_output_bus(STREAM_BATCH_ID)
      assert bus is not None
      streamed = "\n".join(item.line for item in bus.snapshot())
      # 真实失败堆栈必须出现在流里（前端"原始输出"区的核心价值）
      assert "day53 boom" in streamed, streamed
    finally:
      close_output_bus(STREAM_BATCH_ID)


# ===========================================================================
# 3. 原始输出 SSE 流接口
# ===========================================================================
@allure.feature("Day53 全链路联调")
@allure.story("原始输出SSE流")
class TestOutputSseEndpoint:
  """GET /api/executions/<id>/output"""

  @allure.title("输出流返回 text/event-stream 且含输出行与终帧")
  def test_output_stream_frames_for_simulated_batch(
    self, day53_client: FlaskClient
  ) -> None:
    """
    模拟批次同样应有原始输出可视反馈（每条用例一行 [simulated]），
    并在批次终态后收到 output_end 终帧收流（终帧是客户端"可以停下来"
    的唯一信号，没有它浏览器会按规范无限重连）。
    """
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    status_data = _wait_batch_terminal(day53_client, execution_id)
    assert status_data["passed"] == 2 and status_data["failed"] == 2

    response = day53_client.get(
      f"/api/executions/{execution_id}/output",
      headers={"Accept": "text/event-stream"},
    )
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/event-stream")

    raw = response.get_data(as_text=True)
    frames = _parse_sse_frames(raw)
    output_frames = [data for name, data in frames if name == "output"]
    end_frames = [data for name, data in frames if name == "output_end"]
    assert len(output_frames) == 4, output_frames
    # 帧 id 必须是**裸整数行序号**（Day53-fix P1）：发出去什么形状，
    # 重连时浏览器就回传什么形状，解析端只认裸整数
    assert "id: 1\n" in raw
    assert "id: live:" not in raw
    assert "id: db:" not in raw
    assert all("[simulated]" in frame["line"] for frame in output_frames)
    assert output_frames[0]["seq"] >= 1
    assert end_frames and end_frames[-1]["reason"] == "batch_terminal"
    assert end_frames[-1]["lines"] == 4

  @allure.title("输出流按 Last-Event-ID 断点续传（不重复补发已收行）")
  def test_output_stream_resumes_from_last_event_id(
    self, day53_client: FlaskClient
  ) -> None:
    """
    重连时携带 Last-Event-ID 只回放其后的行（断点续传）；序号锚点
    是**裸整数行号**（与事件流的 namespace:seq 是两套体系，不得混用）。
    """
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    _wait_batch_terminal(day53_client, execution_id)

    response = day53_client.get(
      f"/api/executions/{execution_id}/output",
      headers={"Last-Event-ID": "2"},
    )
    frames = _parse_sse_frames(response.get_data(as_text=True))
    output_frames = [data for name, data in frames if name == "output"]
    assert [frame["seq"] for frame in output_frames] == [3, 4]
    # 终帧必须仍然送达（否则客户端永远等不到"输出结束"）
    assert any(name == "output_end" for name, _ in frames)

  @allure.title("通道已被回收的终态批次如实告知无捕获且立即收流")
  def test_output_stream_reports_no_capture_when_channel_gone(
    self, day53_client: FlaskClient
  ) -> None:
    """
    批次已终态且输出通道已被回收/从未建立时，接口必须发一帧
    no_capture 终帧后立刻收流——绝不挂死等待（那会长期钉住一个
    Flask 工作线程，且前端永远停在"连接中"）。
    """
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    _wait_batch_terminal(day53_client, execution_id)
    close_output_bus(execution_id, reason="test_reclaim")

    response = day53_client.get(f"/api/executions/{execution_id}/output")
    assert response.status_code == 200
    frames = _parse_sse_frames(response.get_data(as_text=True))
    assert [name for name, _ in frames] == ["output_end"]
    assert frames[0][1]["reason"] == "no_capture"

  @allure.title("批次不存在时输出流返回404")
  def test_output_stream_404_for_unknown_batch(
    self, day53_client: FlaskClient
  ) -> None:
    """不存在的批次统一 404（与其他接口口径一致），不返回半截空流。"""
    response = day53_client.get("/api/executions/RUN-NOT-EXIST-0001/output")
    assert response.status_code == 404
    assert response.get_json()["message"] == "执行批次不存在"

  @allure.title("畸形 Last-Event-ID 一律按全量回放（不拒掉重连）")
  @pytest.mark.parametrize("header_value", ["abc", "1x", "-5"])
  def test_output_stream_malformed_last_event_id_falls_back(
    self, day53_client: FlaskClient, header_value: str
  ) -> None:
    """
    非数字/非正整数的 Last-Event-ID 一律当 0 处理（全量回放）:
    断线重连是自动行为，客户端头部畸形时拒掉请求等于把用户的日志
    永久锁死；宁可重发也不可断供。
    """
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    _wait_batch_terminal(day53_client, execution_id)

    response = day53_client.get(
      f"/api/executions/{execution_id}/output",
      headers={"Last-Event-ID": header_value},
    )
    assert response.status_code == 200
    frames = _parse_sse_frames(response.get_data(as_text=True))
    output_frames = [data for name, data in frames if name == "output"]
    assert [frame["seq"] for frame in output_frames] == [1, 2, 3, 4]

  @allure.title("无通道的未终态批次现场补开捕获并正常收流")
  def test_output_stream_arms_capture_for_unarmed_batch(
    self, day53_client: FlaskClient, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """
    分支 3: 批次行存在但输出通道不在（触发时未接入捕获 / 已被回收）
    且批次未终态时，接口必须**现场补开捕获**再跟流——否则这些批次的
    输出区永远空白。用直接建批次（不经触发接口，故无捕获）+ 桩状态
    收敛，保证用例确定性结束且不依赖真实执行。
    """
    execution_id = str(
      CaseManager.start_execution(
        trigger="manual", executor_name="web", environment="dev"
      )["execution_id"]
    )
    assert get_output_bus(execution_id) is None

    monkeypatch.setattr(
      executions_mod, "_safe_batch_status", lambda _eid: "finished"
    )
    response = day53_client.get(f"/api/executions/{execution_id}/output")
    assert response.status_code == 200
    frames = _parse_sse_frames(response.get_data(as_text=True))
    assert [name for name, _ in frames] == ["output_end"]
    assert get_output_bus(execution_id) is not None, "应已现场补开捕获"

  @allure.title("单IP并发订阅超限时输出流返回429")
  def test_output_stream_429_when_quota_exceeded(
    self, day53_client: FlaskClient, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """
    输出流与事件流共用同一份单 IP 订阅配额（无鉴权服务下的拒绝服务
    防线）: 超限必须 429 而不进入流式响应，防"触发长批次 + N 条流
    占满工作线程"这条链。
    """
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    _wait_batch_terminal(day53_client, execution_id)
    monkeypatch.setitem(
      executions_mod._SSE_ACTIVE_CONNECTIONS, "127.0.0.1", 999
    )
    response = day53_client.get(f"/api/executions/{execution_id}/output")
    assert response.status_code == 429

  @allure.title("HEAD 请求即时归还订阅名额（防名额被永久占死）")
  def test_output_stream_head_releases_slot(
    self, day53_client: FlaskClient
  ) -> None:
    """
    Werkzeug 对 HEAD 丢弃响应体，流生成器永不推进、其 finally 也永不
    执行——若不显式归还名额，HEAD 连发即可把某 IP 的名额占死。
    """
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    _wait_batch_terminal(day53_client, execution_id)
    before = executions_mod._SSE_ACTIVE_CONNECTIONS.get("127.0.0.1", 0)

    response = day53_client.head(f"/api/executions/{execution_id}/output")
    assert response.status_code == 200
    after = executions_mod._SSE_ACTIVE_CONNECTIONS.get("127.0.0.1", 0)
    assert after == before, "HEAD 必须即时归还名额"

  @allure.title("无输出的长挂批次到达存活上界后主动收流")
  def test_output_stream_lifetime_cap_without_output(
    self, day53_client: FlaskClient, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """
    节拍分支的存活上界: 批次状态始终非终态且一直没有输出行时，
    流必须在上界处发终帧收流，而不是永久钉住一个 Flask 工作线程。
    """
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    _wait_batch_terminal(day53_client, execution_id)
    # 换一个"空通道"（原通道里已有 4 行，会走"先回放"分支）
    close_output_bus(execution_id, reason="test_empty")
    arm_output_capture(execution_id)
    monkeypatch.setattr(executions_mod, "SSE_MAX_LIFETIME_SECONDS", 0.0)
    monkeypatch.setattr(
      executions_mod, "_safe_batch_status", lambda _eid: "running"
    )

    response = day53_client.get(f"/api/executions/{execution_id}/output")
    frames = _parse_sse_frames(response.get_data(as_text=True))
    assert [name for name, _ in frames] == ["output_end"]
    assert frames[0][1]["reason"] == "lifetime_exceeded"

  @allure.title("有输出的批次到达存活上界后主动收流（事件持续到达分支）")
  def test_output_stream_lifetime_cap_after_output(
    self, day53_client: FlaskClient, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """
    存活上界必须**每轮都判**：只在节拍分支判时，输出持续到达的长批次
    永远走不到那里，上界形同虚设。
    """
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    _wait_batch_terminal(day53_client, execution_id)
    close_output_bus(execution_id, reason="test_empty")
    bus = arm_output_capture(execution_id)
    assert bus is not None
    bus.publish(OUTPUT_SOURCE_STDOUT, "上界前的最后一行")
    monkeypatch.setattr(executions_mod, "SSE_MAX_LIFETIME_SECONDS", 0.0)
    monkeypatch.setattr(
      executions_mod, "_safe_batch_status", lambda _eid: "running"
    )

    response = day53_client.get(f"/api/executions/{execution_id}/output")
    frames = _parse_sse_frames(response.get_data(as_text=True))
    assert [name for name, _ in frames] == ["output", "output_end"]
    assert frames[0][1]["line"] == "上界前的最后一行"
    assert frames[1][1]["reason"] == "lifetime_exceeded"

  @allure.title("真实浏览器格式的 Last-Event-ID（live:N）同样能断点续传")
  def test_output_stream_resumes_from_browser_style_last_event_id(
    self, day53_client: FlaskClient
  ) -> None:
    """
    P1 回归（Day53-fix）: 修复前输出帧发的是 `id: live:{seq}`——因为
    `_format_sse_frame` 的 `namespace` 默认值就是 live，调用方没传。
    浏览器会原样回传 `Last-Event-ID: live:2`，而解析端只做 `int()` 直转
    → 抛错 → 返回 0 → **每次重连都全量回放（最多 2000 行）**，断点续传
    在真实浏览器下从不生效。本用例按真实浏览器回传的形态发头（而不是
    手工构造一个客户端永远发不出的裸整数头），断言只回放锚点之后的行。

    修复前必红（返回 [1,2,3,4]），修复后必绿（返回 [3,4]）。
    """
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    _wait_batch_terminal(day53_client, execution_id)

    response = day53_client.get(
      f"/api/executions/{execution_id}/output",
      headers={"Last-Event-ID": "live:2"},
    )
    assert response.status_code == 200
    frames = _parse_sse_frames(response.get_data(as_text=True))
    output_frames = [data for name, data in frames if name == "output"]
    assert [frame["seq"] for frame in output_frames] == [3, 4]
    # 终帧仍要送达（否则客户端永远等不到"输出结束"）
    assert any(name == "output_end" for name, _ in frames)

  @allure.title("高频输出不被帧间隔节流（150 行预算内推完）")
  def test_output_stream_high_frequency_not_throttled(
    self, day53_client: FlaskClient, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """
    P2-a 回归（Day53-fix）: 修复前逐行输出复用了 /events 的
    `FRAME_INTERVAL_SECONDS=0.05`，等于给每条流加 **20 行/s 硬上限**——
    回放满环 2000 行要 100s，实时高频输出时消费游标持续滞后、环静默
    淘汰丢行。本用例推 150 行并断言端到端耗时 < 3s：修复前需要
    150 × 0.05 = 7.5s（必红），修复后只受一次 0.5s 节拍支配（约 0.6s）。
    """
    line_count = 150
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    _wait_batch_terminal(day53_client, execution_id)
    close_output_bus(execution_id, reason="test_empty")
    bus = arm_output_capture(execution_id)
    assert bus is not None
    for index in range(line_count):
      bus.publish(OUTPUT_SOURCE_STDOUT, f"高频行-{index}")
    monkeypatch.setattr(
      executions_mod, "_safe_batch_status", lambda _eid: "finished"
    )

    # 计时窗口必须**包住 body 消费**：Flask 测试客户端默认 buffered=False，
    # 流生成器是在读取 response 数据时才被迭代的；只计 client.get() 的
    # 耗时会把整个节流过程漏在窗口外（这条断言就永远不会红）
    started = time.monotonic()
    response = day53_client.get(f"/api/executions/{execution_id}/output")
    raw = response.get_data(as_text=True)
    elapsed = time.monotonic() - started

    frames = _parse_sse_frames(raw)
    pushed = [data for name, data in frames if name == "output"]
    assert len(pushed) == line_count, "高频输出不得丢行（环容量内）"
    assert [frame["seq"] for frame in pushed] == list(range(1, line_count + 1))
    assert elapsed < HIGH_FREQ_TIME_BUDGET_SECONDS, (
      f"150 行推送耗时 {elapsed:.2f}s，超过预算 "
      f"{HIGH_FREQ_TIME_BUDGET_SECONDS}s（疑似仍在按 0.05s 节流）"
    )

  @allure.title("空闲超心跳间隔后发 : heartbeat 注释帧（且不推进游标）")
  def test_output_stream_emits_heartbeat_when_idle(
    self, day53_client: FlaskClient, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """
    P2-b 回归（Day53-fix）: 修复前节拍分支只判终态/上界、**不产出任何
    字节**，于是客户端断开要等存活上界（30 分钟）才发现，死连接长期钉住
    单 IP 的 5 个名额 → 合法重连被 429；中间代理也可能掐断空闲连接。

    本用例把心跳间隔压到小于两个节拍（0.6s < 2 × 0.5s），状态序列为
    "两次运行中 → 终态"，既保证空闲窗口足够长、又保证用例在有限时间内
    自然收流（不靠 sleep 赌时序，靠节拍次数推进）。
    """
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    _wait_batch_terminal(day53_client, execution_id)
    close_output_bus(execution_id, reason="test_empty")
    arm_output_capture(execution_id)
    monkeypatch.setattr(executions_mod, "HEARTBEAT_INTERVAL_SECONDS", 0.6)

    calls = {"count": 0}

    def _status_sequence(_execution_id: str) -> str:
      """前两次节拍报运行中（留出空闲窗口），其后转终态收流"""
      calls["count"] += 1
      return "running" if calls["count"] <= 2 else "finished"

    monkeypatch.setattr(
      executions_mod, "_safe_batch_status", _status_sequence
    )

    response = day53_client.get(f"/api/executions/{execution_id}/output")
    raw = response.get_data(as_text=True)

    assert ": heartbeat\n\n" in raw, raw
    # 注释帧不得带 id 行——带了就会推进客户端的 Last-Event-ID 游标，
    # 让下一次重连的锚点指向一条不存在的"行"
    assert "id: " not in raw, raw


# ===========================================================================
# 4. 执行器双模式切换
# ===========================================================================
@allure.feature("Day53 全链路联调")
@allure.story("执行器双模式切换")
class TestExecutorModeApi:
  """GET/POST /api/executor/mode 与切换后的执行行为"""

  @allure.title("未配置时读取到默认模式 simulated")
  def test_get_mode_returns_default_simulated(
    self, day53_client: FlaskClient
  ) -> None:
    """未设置 TM_EXECUTOR 时当前模式必须是 simulated（工厂兜底口径一致）。"""
    response = day53_client.get("/api/executor/mode")
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["mode"] == "simulated"
    assert data["available"] == ["simulated", "pytest"]
    assert data["default"] == "simulated"

  @allure.title("切换模式后新触发的批次执行行为随之变化")
  def test_mode_switch_changes_executor_behaviour(
    self, day53_client: FlaskClient
  ) -> None:
    """
    全链路验证"模式切换真的换执行器"：
      ①模拟模式触发 → 4 条种子用例按末位奇偶得 2 过 2 挂；
      ②切到真实模式再触发 → 种子用例都没有 source_ref，真实执行器
       按"执行目标缺失"逐条降级 error（4 条 error，0 passed）。
    两步的差异只可能来自执行器不同——这正是双模式切换要证明的事，
    且第二条不起子进程（build_command 阶段即失败），不拖慢全量。
    """
    first_id = _trigger(day53_client, {})
    first_status = _wait_batch_terminal(day53_client, first_id)
    assert first_status["passed"] == 2 and first_status["failed"] == 2

    switch = day53_client.post("/api/executor/mode", json={"mode": "pytest"})
    assert switch.status_code == 200
    assert switch.get_json()["data"]["mode"] == "pytest"
    assert day53_client.get("/api/executor/mode").get_json()["data"]["mode"] == (
      "pytest"
    )

    second_id = _trigger(day53_client, {})
    second_status = _wait_batch_terminal(day53_client, second_id)
    assert second_status["error"] == 4
    assert second_status["passed"] == 0
    # 已有批次的结果不受切换影响（模式只作用于之后触发的批次）
    after = day53_client.get(f"/api/executions/{first_id}/status")
    assert after.get_json()["data"]["passed"] == 2

  @allure.title("非法/缺失模式一律400（不让脏值静默生效）")
  @pytest.mark.parametrize(
    "payload",
    [
      {},
      {"mode": ""},
      {"mode": "ghost"},
      {"mode": 123},
    ],
  )
  def test_post_mode_invalid_returns_400(
    self, day53_client: FlaskClient, payload: dict
  ) -> None:
    """
    缺失、空白、非法枚举、非字符串类型四种入参一律 400：模式写的是
    进程级执行开关，静默接受脏值会让"界面显示 A、实际跑 B"。
    """
    response = day53_client.post("/api/executor/mode", json=payload)
    assert response.status_code == 400
    assert "mode" in response.get_json()["message"]
    # 失败不得改变当前模式
    assert (
      day53_client.get("/api/executor/mode").get_json()["data"]["mode"]
      == "simulated"
    )

  @allure.title("无请求体等价于空对象（按 mode 缺失处理，不500）")
  def test_post_mode_without_body_returns_400(
    self, day53_client: FlaskClient
  ) -> None:
    """
    完全没有请求体时 `get_json(silent=True)` 返回 None，接口必须
    归一到空对象走参数校验（400），而不是抛 NoneType 相关异常变 500。
    """
    response = day53_client.post("/api/executor/mode")
    assert response.status_code == 400
    assert "mode" in response.get_json()["message"]

  @allure.title("请求体非JSON对象时返回400")
  def test_post_mode_with_non_object_body_returns_400(
    self, day53_client: FlaskClient
  ) -> None:
    """顶层是数组/字符串等非对象结构时直接 400，不进入字段解析。"""
    response = day53_client.post("/api/executor/mode", json=["pytest"])
    assert response.status_code == 400
    assert "JSON对象" in response.get_json()["message"]


# ===========================================================================
# 5. 全链路：Web 触发 → 真实执行 → 入库 → 看板刷新
# ===========================================================================
@allure.feature("Day53 全链路联调")
@allure.story("Web触发真实执行全链路")
class TestFullChainRealExecution:
  """触发 → 真实 subprocess → 明细入库 → 看板趋势可查"""

  @allure.title("Web 触发真实执行并落库，看板趋势接口能查到本次批次")
  def test_web_trigger_real_executor_visible_on_dashboard(
    self,
    day53_client: FlaskClient,
    scratch_dir: Path,
    make_source_ref: Callable[[str, str], str],
  ) -> None:
    """
    全链路一条龙（任务一的验收面）:
      ①通过 POST /api/executions/trigger 以真实执行器触发；
      ②真实执行器拉起 pytest 子进程跑 output/day53_scratch-<时间戳>-<pid>
       下的真实用例并落明细；
      ③批次终态计数与明细接口一致（passed=1、error=0）；
      ④看板数据源 GET /api/reports/trend 立刻能查到该批次，
        pass_rate 为**原始比值**（展示层才做百分比与 clamp）。
    """
    source_ref = make_source_ref(
      "test_day53_chain_ok", "def test_chain_ok():\n  assert True\n"
    )
    # 重建种子：仅第一条用例挂真实执行目标，其余三条无 source_ref
    with DatabaseSession.session_scope() as session:
      session.query(models.TestCase).delete()
    seed_cases(source_ref=source_ref)

    execution_id = _trigger(day53_client, {"executor": "pytest"})
    status_data = _wait_batch_terminal(day53_client, execution_id)
    assert status_data["status"] == "finished"
    assert status_data["passed"] == 1
    assert status_data["error"] == 3

    detail = day53_client.get(f"/api/executions/{execution_id}")
    assert detail.status_code == 200
    items = detail.get_json()["data"]["items"]
    by_result = {item["case_id"]: item["result"] for item in items}
    assert by_result["TM-D53-0001"] == "passed"

    trend = day53_client.get("/api/reports/trend?limit=20")
    assert trend.status_code == 200
    rows = trend.get_json()["data"]
    matched = [row for row in rows if row["execution_id"] == execution_id]
    assert matched, rows
    row = matched[0]
    assert row["total_cases"] == 4
    assert row["passed"] == 1
    # 看板拿到的仍是落库原值（0~1 比值），clamp 只发生在展示层
    assert 0.0 <= row["pass_rate"] <= 1.0

  @allure.title("真实执行失败的用例能带回真实 traceback 摘要")
  def test_web_trigger_real_executor_reports_real_failure(
    self,
    day53_client: FlaskClient,
    scratch_dir: Path,
    make_source_ref: Callable[[str, str], str],
  ) -> None:
    """
    "真实 assert False 全链路"的延伸验证（Day52 门禁的展示面）:
    真实失败的用例必须落 failed（而非 error），且明细里的
    error_message 带回真实断言文本——证明落库的是子进程真输出，
    不是模拟器造的字符串。
    """
    source_ref = make_source_ref(
      "test_day53_chain_fail",
      "def test_chain_fail():\n  assert 1 == 2, 'day53 chain boom'\n",
    )
    with DatabaseSession.session_scope() as session:
      session.query(models.TestCase).delete()
    seed_cases(source_ref=source_ref)

    execution_id = _trigger(day53_client, {"executor": "pytest"})
    status_data = _wait_batch_terminal(day53_client, execution_id)
    assert status_data["failed"] == 1

    detail = day53_client.get(f"/api/executions/{execution_id}")
    items = detail.get_json()["data"]["items"]
    target = [item for item in items if item["case_id"] == "TM-D53-0001"][0]
    assert target["result"] == "failed"
    assert "day53 chain boom" in (target["error_message"] or "")


# ===========================================================================
# 6. Dashboard 通过率 clamp 防御（展示层）
# ===========================================================================
@allure.feature("Day53 全链路联调")
@allure.story("看板通过率clamp防御")
class TestDashboardPassRateClamp:
  """
  通过率展示层 clamp 防御。

  验证口径说明（本机无 JS 运行时，项目前端用例一律走静态断言）:
    ①dashboard.js 必须定义 clampPassRatePercent 且收敛区间为 [0,100]；
    ②所有通过率展示点（统计卡片/趋势曲线/趋势 tooltip/饼图 tooltip）
     都必须经过该函数，不得残留裸乘 100 的拼接；
    ③按 JS 语义做等价映射核对越界取值（4033% → 100、-50% → 0）；
    ④后端数据不被修改（趋势接口返回的仍是落库原值）。
  """

  @allure.title("clamp 助手函数存在且上界为 [0,100]")
  def test_clamp_helper_is_bounded(self) -> None:
    """clamp 实现必须是 Math.min(100, Math.max(0, n)) 且过滤非有限数。"""
    source = DASHBOARD_JS.read_text(encoding="utf-8")
    assert "function clampPassRatePercent(" in source
    assert "Math.min(100, Math.max(0, numeric))" in source
    assert "Number.isFinite(numeric)" in source

  @allure.title("全部通过率展示点都经过 clamp，无裸乘100残留")
  def test_all_pass_rate_display_points_use_clamp(self) -> None:
    """
    四处展示点（统计卡片、趋势曲线数据、趋势 tooltip、饼图 tooltip）
    都要走 clamp；原先的裸 `.toFixed(1)` 拼接必须被替换干净，
    否则会出现"曲线被收敛、悬浮显示 4033%"的自相矛盾。
    """
    source = DASHBOARD_JS.read_text(encoding="utf-8")
    # 1 处定义 + 4 处调用（统计卡片 / 趋势曲线 / 趋势 tooltip / 饼图 tooltip）
    assert source.count("clampPassRatePercent(") >= 5

    # 逐个 `.toFixed(1)`（通过率百分比的唯一展示出口）核对取值来源:
    # 要么上游就是 clampPassRatePercent（三处直接展示点），要么是已被
    # clamp 过的曲线数据（series label 的 p.value）。任何"裸乘 100 直接
    # toFixed"的残留都会在此被抓住——多行写法故取前 120 字符做上下文。
    for match in re.finditer(r"\.toFixed\(1\)", source):
      context = " ".join(source[max(0, match.start() - 120) : match.start()].split())
      assert "clampPassRatePercent" in context or context.endswith(
        "return p.value"
      ), context

  @allure.title("越界取值按 JS 语义等价映射收敛到 [0,100]")
  @pytest.mark.parametrize(
    ("raw", "expected"),
    [
      (4033.0, 100.0),
      (-50.0, 0.0),
      (float("nan"), 0.0),
      (float("inf"), 0.0),
      (78.5714, 78.5714),
      (0.0, 0.0),
      (100.0, 100.0),
    ],
  )
  def test_clamp_mapping_matches_frontend_semantics(
    self, raw: float, expected: float
  ) -> None:
    """
    按 dashboard.js 里 clampPassRatePercent 的 JS 语义做等价核对:
    非有限数归 0（JS 的 .toFixed 遇 NaN 会输出字面量 "NaN%"），
    其余收敛到 [0,100]。本机无 JS 运行时，故用等价实现核对边界取值。
    """

    def _clamp(value: float) -> float:
      if not math.isfinite(value):
        return 0.0
      return min(100.0, max(0.0, value))

    assert _clamp(raw) == expected

  @allure.title("展示层防御不修改后端数据（趋势接口返回落库原值）")
  def test_backend_trend_keeps_raw_values(self, day53_client: FlaskClient) -> None:
    """
    clamp 只在展示层：趋势接口必须原样返回 0~1 的比值（0.5 而不是 50），
    脏值仍可被查到来源——修数属于数据治理，不该由前端偷偷改。
    """
    execution_id = _trigger(day53_client, {"executor": "simulated"})
    _wait_batch_terminal(day53_client, execution_id)
    rows = day53_client.get("/api/reports/trend?limit=20").get_json()["data"]
    matched = [row for row in rows if row["execution_id"] == execution_id]
    assert matched
    assert matched[0]["pass_rate"] == 0.5
    assert matched[0]["passed"] == 2 and matched[0]["failed"] == 2


# ===========================================================================
# 7. 原始输出流的并发订阅（真线程 + 有界轮询，防 flaky）
# ===========================================================================
@allure.feature("Day53 全链路联调")
@allure.story("输出通道并发订阅")
class TestOutputBusConcurrency:
  """多订阅者各自独立游标（SSE 重连/多窗口场景）"""

  @allure.title("两个订阅者都能看到完整输出序列（互不分流）")
  def test_multiple_subscribers_see_full_sequence(self) -> None:
    """
    历史环是非消费式的：先后订阅的两个游标都能读到全量行。
    用真线程并发订阅 + 有界轮询收集结果（禁止 sleep 赌时序）。
    """
    bus = ExecutionOutputBus()
    for index in range(20):
      bus.publish(OUTPUT_SOURCE_STDOUT, f"row-{index}")
    bus.close(reason="done")

    collected: dict[str, list[int]] = {"a": [], "b": []}

    def _consume(name: str) -> None:
      for item in bus.subscribe(tick=False):
        if item is not None:
          collected[name].append(item.seq)

    threads = [
      threading.Thread(target=_consume, args=(name,), daemon=True)
      for name in ("a", "b")
    ]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join(timeout=5.0)

    assert not any(thread.is_alive() for thread in threads)
    assert collected["a"] == list(range(1, 21))
    assert collected["b"] == list(range(1, 21))


# ===========================================================================
# 8. 防御分支：旁路能力的异常必须只记日志、绝不升级
# ===========================================================================
@allure.feature("Day53 全链路联调")
@allure.story("输出通道防御分支")
class TestDay53DefensiveBranches:
  """
  异常路径逐条覆盖。

  为什么这些分支值得专门写用例: 输出通道与输出流都是**旁路展示能力**，
  它们的异常一旦上抛，代价是"真实执行被打成失败"或"整轮 pytest 起不来"
 ——这正是项目 6.1 通知旁路铁律的同族问题。这些分支平时不会走到，
  没有用例就等于没有防线。
  """

  @allure.title("投递与关闭的内部异常只记日志（不抛出）")
  def test_publish_and_close_swallow_internal_errors(self) -> None:
    """
    ①非法入参（非字符串行）让内部 rstrip 抛 AttributeError；
    ②临界区被破坏（with 一个非上下文管理器）让 close 抛 TypeError。
    两者都必须被内部消化——投递/关闭发生在执行线程里，上抛会把
    一条本来跑得好好的用例打成 failed。
    """
    bus = ExecutionOutputBus()
    bus.publish(OUTPUT_SOURCE_STDOUT, None)  # type: ignore[arg-type]
    assert bus.snapshot() == []

    bus._condition = None  # type: ignore[assignment]
    bus.close(reason="broken_condition")
    # 临界区已被破坏，close 内部在加锁处即失败 → 关闭标记不得被置上，
    # 但异常必须被吞掉（本用例"不抛出"即通过；is_closed 只读不加锁）
    assert bus.is_closed is False

  @allure.title("通道属性读取与空闲回收异常降级")
  def test_properties_and_prune_failure(
    self, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """属性读取语义 + 回收路径异常时必须返回空列表而不是上抛。"""
    bus = ExecutionOutputBus()
    assert bus.is_closed is False
    assert bus.close_reason == ""
    bus.close(reason="manual")
    assert bus.is_closed is True
    assert bus.close_reason == "manual"

    # 破坏注册表锁: with None → TypeError → 回收必须降级为空列表
    monkeypatch.setattr(executors_mod, "_OUTPUT_BUSES_LOCK", None)
    assert prune_idle_output_buses() == []

  @allure.title("逐行读线程异常与关流异常被就地消化")
  def test_consume_stream_reader_and_close_failures(self) -> None:
    """
    读线程抛错（管道被破坏）只结束本流读线程，另一流照常收完；
    关流失败同样只记日志。返回值语义必须与 communicate 一致。
    """

    class _BrokenStream:
      """可控的假流: readline 抛错 / 正常出一行后 EOF，close 一律抛错"""

      def __init__(self, fail_on_read: bool) -> None:
        self._fail_on_read = fail_on_read
        self._first = True

      def readline(self) -> str:
        """按配置抛错或返回一行后 EOF"""
        if self._fail_on_read:
          raise OSError("pipe broken")
        if self._first:
          self._first = False
          return "一行输出\n"
        return ""

      def close(self) -> None:
        """模拟关流失败"""
        raise OSError("close failed")

    class _StubProcess:
      """只提供 _consume_stream 需要的三个接口的假进程"""

      def __init__(self, stdout: object, stderr: object) -> None:
        self.stdout = stdout
        self.stderr = stderr

      def wait(self, timeout: float | None = None) -> int:
        """立即"退出"""
        return 0

    bus = ExecutionOutputBus()
    capture = executors_mod._StreamedOutput()
    process = _StubProcess(_BrokenStream(True), _BrokenStream(False))
    stdout, stderr = PytestRunner()._consume_stream(
      process, bus, capture  # type: ignore[arg-type]
    )
    assert stdout == ""
    assert stderr == "一行输出\n"
    assert capture.text.strip() == "一行输出"

  @allure.title("逐行读超时先清进程树再抛出，kill 前输出不丢")
  def test_consume_stream_timeout_kills_tree_then_raises(self) -> None:
    """
    逐行读路径的超时由 `process.wait(timeout)` 抛出（该异常不带
    stdout/stderr 流），故必须在抛出前①清进程树（否则读线程读不到
    EOF、join 白白等满预算）、②把已捕获的行留在暂存区——超时恰恰
    是最需要现场的那一次。
    """

    class _OnceThenEofStream:
      """先出一行再 EOF，防读线程空转"""

      def __init__(self) -> None:
        self._first = True

      def readline(self) -> str:
        """第一行有内容，其后 EOF"""
        if self._first:
          self._first = False
          return "超时前已输出\n"
        return ""

      def close(self) -> None:
        """正常关流"""
        return None

    class _TimeoutProcess:
      """wait 抛 TimeoutExpired 的假进程（pid=0 让树清理走守卫分支）"""

      pid = 0

      def __init__(self) -> None:
        self.stdout = _OnceThenEofStream()
        self.stderr = _OnceThenEofStream()

      def wait(self, timeout: float | None = None) -> int:
        """模拟超时"""
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    bus = ExecutionOutputBus()
    capture = executors_mod._StreamedOutput()
    with pytest.raises(subprocess.TimeoutExpired):
      PytestRunner()._consume_stream(
        _TimeoutProcess(), bus, capture  # type: ignore[arg-type]
      )
    assert capture.cleanup_note, "超时必须在抛出前留下进程树清理描述"
    assert "超时前已输出" in capture.text
    assert "超时前已输出" in "\n".join(
      item.line for item in bus.snapshot()
    )

  @allure.title("输出流状态查询的两种降级（异常与空结果）")
  def test_safe_batch_status_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `_safe_batch_status` 是"流已开始后"的状态查询入口，绝不能上抛:
      ①查询异常 → 记警告并返回空串（按"状态未知"继续跟流）；
      ②批次不存在（返回 None）→ 返回空串。
    两者都必须与"批次仍在跑"同样处理，否则一个正常的输出流会被
    中途打成 500。
    """

    def _boom(_execution_id: str) -> dict:
      raise RuntimeError("db down")

    monkeypatch.setattr(CaseManager, "get_execution_status", _boom)
    assert executions_mod._safe_batch_status("RUN-DAY53-DOWN") == ""

    monkeypatch.setattr(
      CaseManager, "get_execution_status", lambda _eid: None
    )
    assert executions_mod._safe_batch_status("RUN-DAY53-ABSENT") == ""
