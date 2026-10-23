"""
Day62 真实执行全链路集成测试（触发 → 执行 → 解析 → 入库 → 看板）

覆盖范围（10 条，逐节点验证数据一致性）:
  1. 通过场景全链路: CaseManager 建用例 → Web 触发真实 pytest 子进程 →
     轮询终态 → 明细入库 → 看板趋势接口可见
  2. 失败场景全链路: 真实断言失败 → 入库 failed → 真实 traceback 摘要 →
     看板通过率反映失败
  3. 重试机制端到端（Day61 新增能力）: 首次尝试伪超时 → 重试后通过 →
     ExecutionResult.retries_used 记账 → 最终入库 passed
  4. 写库幂等端到端（Day60 新增能力）: 重复 record_execution 返回 False →
     明细条数与批次总数不膨胀
  5. 批量执行端到端: 一次触发多条用例 → 入库条数 = 触发条数 →
     批次 passed/failed/total 与明细逐条对得上
  6. 输出解析端到端: 真实子进程输出 → PytestOutputParser → 计数与用例级
     入库口径的差异被显式钉死
  7. 看板趋势端到端: 连续两次执行 → 趋势接口返回两条历史 → 通过率互不覆盖
  8. 执行详情查询端到端: GET /api/executions/<id> 返回 summary + items 完整字段
  9. 并发执行端到端: 两个独立批次同时触发 → 互不干扰 → 看板不混淆
 10. 异常路径端到端: 非法参数 400 / 不存在批次 404 → 零脏数据

与 Day53 的分工（刻意不重复）:
  Day53 验证的是"输出实时通道 + SSE 流 + 双模式切换 + Dashboard clamp"，
  只覆盖了 2 条真实执行链路用例。本文件不再触碰 SSE 与前端静态断言，
  专注**数据链路一致性**（每一跳的计数、字段、幂等与隔离），
  并补上 Day60 写库幂等与 Day61 重试机制的端到端验证。

数据隔离: 每条用例独立临时 SQLite 文件库；teardown 复位数据库引擎、
事件通道、输出通道，并还原 TM_EXECUTOR 进程级环境变量。

时序约定（项目铁律）: 批次终态一律走**有界轮询**
（POLL_MAX_ATTEMPTS / POLL_INTERVAL_SECONDS），禁止固定 sleep 赌时序；
真实子进程用例只用 `assert True` / `assert False` 级别的小文件，
绝不触发全量嵌套回归。

一条已知的设计边界（不是缺陷，Day61 决策）:
  `retries_used` 只存在于 ExecutionResult（数据类字段），**没有进数据库
  schema**——Day61 明确"不触 schema"。因此详情接口不返回该字段，
  本文件用记录型执行器替换器在进程内取证，见第 3 条用例。
"""

import contextlib
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import allure
import pytest
from flask.testing import FlaskClient
from src.core import case_manager as case_manager_mod
from src.core import event_bus
from src.core.case_manager import CaseManager
from src.core.executors import (
  EXIT_REASON_TIMEOUT,
  ExecutionResult,
  PytestRunner,
  reset_output_buses,
)
from src.core.output_parser import PytestOutputParser
from src.db import models
from src.db.db_session import DatabaseSession
from src.web import create_app
from src.web.routes.executions import EXECUTOR_MODE_ENV_KEY

from tests.db_cleanup import safe_unlink

# 项目根（本文件位于 tests/ 下，向上一级为项目根）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 真实子进程用例的临时测试文件目录（output/ 已被 .gitignore 忽略）。
# 目录名**会话级唯一**（同 Day53/Day54 口径）: 微秒时间戳区分同机先后
# 启动的会话、pid 区分同一时钟节拍内的两次启动（Windows 时钟粒度约
# 15.6ms，同节拍内微秒位可能相同）。固定名会被并行会话的 teardown
# rmtree 删掉在途探针文件，表现为随机 1 failed。
SCRATCH_DIRNAME = f"day62_scratch-{int(time.time() * 1e6)}-{os.getpid()}"

# 轮询参数: 真实单条用例（含 pytest 启动开销约 1.5s）实测 2-4s，
# 3 条用例的批次留足预算；120 × 0.25s = 30s 上界，离实测值一个量级。
POLL_MAX_ATTEMPTS = 120
POLL_INTERVAL_SECONDS = 0.25

# 各用例的模块名（模块筛选是隔离两个并发布次的唯一手段，故显式命名）
MODULE_MAIN = "Day62全链路"
MODULE_BATCH = "Day62批量"
MODULE_PARSER = "Day62解析"
MODULE_TREND = "Day62趋势"
MODULE_DETAIL = "Day62详情"
MODULE_CONCURRENT_PASS = "Day62并发通过"
MODULE_CONCURRENT_FAIL = "Day62并发失败"

# 详情接口单条明细必须携带的字段集（Day22 契约，Day62 全链路回归面）
EXPECTED_ITEM_FIELDS = frozenset({
  "id",
  "execution_id",
  "case_id",
  "case_name",
  "result",
  "start_time",
  "end_time",
  "duration",
  "error_message",
  "environment",
  "executor",
  "created_at",
})

# 详情接口批次汇总必须携带的字段集
EXPECTED_SUMMARY_FIELDS = frozenset({
  "execution_id",
  "total_cases",
  "passed",
  "failed",
  "error",
  "skipped",
  "pass_rate",
  "created_at",
})


# ===========================================================================
# 夹具与工具
# ===========================================================================
@pytest.fixture
def day62_client(
  monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[FlaskClient]:
  """
  Day62 全链路客户端夹具（function 级临时 SQLite 文件库）

  每条用例独立建库，保证"入库条数/批次计数"这类聚合断言精确无残留；
  teardown 依次复位数据库引擎、事件通道注册表、输出通道注册表，
  并**还原 TM_EXECUTOR**（该变量被双模式切换等路径写进进程级
  os.environ，monkeypatch 不会自动回收）。

  参数:
    monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具
    tmp_path (Path): pytest 临时目录

  返回:
    Iterator[FlaskClient]: yield 测试客户端
  """
  original_executor = os.environ.get(EXECUTOR_MODE_ENV_KEY)
  db_path = tmp_path / "day62_integration.db"
  monkeypatch.setenv("TM_DB_TYPE", "sqlite")
  monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
  monkeypatch.delenv(EXECUTOR_MODE_ENV_KEY, raising=False)
  DatabaseSession.reset()
  DatabaseSession.init_db()
  yield create_app("test").test_client()
  DatabaseSession.reset()
  event_bus.reset_channels()
  reset_output_buses()
  if original_executor is None:
    os.environ.pop(EXECUTOR_MODE_ENV_KEY, None)
  else:
    os.environ[EXECUTOR_MODE_ENV_KEY] = original_executor
  # Windows 下后台执行线程可能仍持有句柄: 删除失败只告警，不升级 teardown ERROR
  safe_unlink(db_path)


@pytest.fixture
def make_case_file() -> Iterator[Callable[[str, str], str]]:
  """
  造一个真实可执行的 pytest 探针文件，返回其**项目相对** source_ref

  为什么不用 tmp_path: source_ref 必须过形态校验
  （`^[\\w./-]+\\.py(::\\w+)*$`，禁盘符/反斜杠/".."），且 pytest 子进程的
  工作目录是项目根，只有项目内的相对路径才能被收集到。

  参数:
    无

  返回:
    Iterator[Callable[[str, str], str]]: 入参（模块名, 文件内容），
      返回形如 `output/day62_scratch-<时间戳>-<pid>/x.py` 的相对路径
  """
  scratch_dir = PROJECT_ROOT / "output" / SCRATCH_DIRNAME
  scratch_dir.mkdir(parents=True, exist_ok=True)

  def _factory(module_name: str, body: str) -> str:
    (scratch_dir / f"{module_name}.py").write_text(body, encoding="utf-8")
    relative_dir = scratch_dir.relative_to(PROJECT_ROOT).as_posix()
    return f"{relative_dir}/{module_name}.py"

  yield _factory
  # 只删本次会话自己创建的唯一目录，并行运行的另一会话不受影响
  shutil.rmtree(scratch_dir, ignore_errors=True)


def _make_pass_fail_probe_refs(
  factory: Callable[[str, str], str],
  tag: str,
  fail_msg: str = "day62 probe boom",
) -> tuple[str, str]:
  """
  构造成对的通过/失败探针用例 source_ref（内部方法）

  为什么抽成一处: "一次通过 + 一次失败" 是看板趋势、执行详情、并发隔离
  三类场景的共同前置，三处各写一遍造文件代码时，任何一处改探针内容
  （比如统一 failure 文案或改用 `assert 1 == 2`）都要改三遍，漏一处就
  会出现"同一个测试文件里三条用例的探针语义不一致"。

  与任务书签名的偏差（有意）: 造文件能力来自 `make_case_file` fixture，
  模块级函数取不到 fixture，故把工厂作为**第一个参数**传入；其余参数
  （tag / fail_msg）与约定一致。

  参数:
    factory (Callable[[str, str], str]): 造探针文件的工厂（make_case_file）
    tag (str): 模块名标记，决定两个探针文件名
          `test_day62_<tag>_pass.py` / `test_day62_<tag>_fail.py`，
          保证不同测试的探针互不撞名
    fail_msg (str): 失败探针的断言消息（默认 "day62 probe boom"），
            调用方需要断言 error_message 时据它取值

  返回:
    tuple[str, str]: (通过探针 source_ref, 失败探针 source_ref)

  异常:
    无（文件写入失败照常上抛 OSError）
  """
  pass_ref = factory(
    f"test_day62_{tag}_pass", f"def test_{tag}_pass():\n  assert True\n"
  )
  fail_body = (
    f"def test_{tag}_fail():\n  assert 1 == 2, '{fail_msg}'\n"
  )
  fail_ref = factory(f"test_day62_{tag}_fail", fail_body)
  return pass_ref, fail_ref


def _create_case(
  case_id: str,
  name: str,
  module: str = MODULE_MAIN,
  source_ref: str | None = None,
) -> dict:
  """
  经 CaseManager 创建一条用例（链路起点）

  刻意走核心层而不是直接 INSERT：创建立即回读，能顺带验证
  "source_ref 归一化后落库"这一跳（Day47 契约）。

  参数:
    case_id (str): 业务用例编号
    name (str): 用例名称
    module (str): 所属模块（同时是模块筛选隔离的键）
    source_ref (str | None): 真实执行目标（项目相对路径），None 表示不可执行

  返回:
    dict: 新建用例的完整字段字典

  异常:
    CaseManagerError: 编号重复/字段非法/数据库异常时抛出
  """
  return CaseManager.create_case({
    "case_id": case_id,
    "name": name,
    "module": module,
    "priority": "P1",
    "case_type": "api",
    "status": "active",
    "description": "Day62 全链路集成测试用例",
    "source_ref": source_ref,
  })


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
  assert response.status_code == 202, response.get_json()
  payload = response.get_json()["data"]
  # 受理层的 total_cases 必须等于后续入库明细条数（第 5 条用例的对账基准）
  assert payload["total_cases"] >= 1
  return str(payload["execution_id"])


def _wait_terminal(client: FlaskClient, execution_id: str) -> dict:
  """
  有界轮询批次状态至终态 finished/failed（内部方法）

  集成测试铁律: 终态轮询走 GET /api/executions/<id>/status 接口层，
  不直调 CaseManager——本文件验证的是 API 全链路。同时防 teardown
  竞态: 触发了后台线程的用例必须等批次进终态再结束。

  参数:
    client (FlaskClient): Flask 测试客户端
    execution_id (str): 批次号

  返回:
    dict: 终态状态字典

  异常:
    AssertionError: 轮询期间状态查询非 200
    pytest.fail: 轮询预算内未达终态
  """
  status_data: dict = {}
  for _ in range(POLL_MAX_ATTEMPTS):
    response = client.get(f"/api/executions/{execution_id}/status")
    assert response.status_code == 200, "轮询期间状态查询应始终 200"
    status_data = response.get_json()["data"]
    if status_data["status"] in ("finished", "failed"):
      return status_data
    time.sleep(POLL_INTERVAL_SECONDS)
  pytest.fail(
    f"批次在轮询预算内未达终态: {execution_id}, "
    f"最后状态: {status_data.get('status', '无响应')}"
  )
  raise AssertionError("不可达: pytest.fail 后代码不会执行到此处")


def _batch_detail(client: FlaskClient, execution_id: str) -> dict:
  """
  查询批次详情并返回 data（内部方法）

  参数:
    client (FlaskClient): Flask 测试客户端
    execution_id (str): 批次号

  返回:
    dict: {"summary": 汇总字典, "items": 明细列表}

  异常:
    AssertionError: 详情接口非 200
  """
  response = client.get(f"/api/executions/{execution_id}")
  assert response.status_code == 200, response.get_json()
  return response.get_json()["data"]


def _trend_rows(client: FlaskClient, limit: int = 20) -> list[dict]:
  """
  查询看板趋势数据（内部方法）

  参数:
    client (FlaskClient): Flask 测试客户端
    limit (int): 返回最近 N 个批次

  返回:
    list[dict]: 时间升序的趋势行

  异常:
    AssertionError: 趋势接口非 200
  """
  response = client.get(f"/api/reports/trend?limit={limit}")
  assert response.status_code == 200, response.get_json()
  return response.get_json()["data"]


def _trend_row_of(rows: list[dict], execution_id: str) -> dict:
  """
  从趋势数据里取出指定批次的行（内部方法）

  参数:
    rows (list[dict]): 趋势行列表
    execution_id (str): 批次号

  返回:
    dict: 命中行

  异常:
    AssertionError: 看板看不到该批次（数据链路断裂的判据）
  """
  matched = [row for row in rows if row["execution_id"] == execution_id]
  assert matched, f"看板趋势未见批次 {execution_id}，实得: {rows}"
  return matched[0]


def _execution_rows(execution_id: str) -> list[dict]:
  """
  直查 test_executions 明细行（内部方法，入库侧的取证面）

  为什么不走详情接口: 详情接口要求批次已有 defect_statistics 汇总行
  （未完成批次会被判 404），而"明细是否落库"恰恰要在批次完成前后
  都能验证。会话关闭后 ORM 实例即脱离，故就地取成普通字典。

  参数:
    execution_id (str): 批次号

  返回:
    list[dict]: 按 id 升序的明细字典（只含断言需要的字段）

  异常:
    无（数据库异常照常上抛）
  """
  with DatabaseSession.session_scope() as session:
    rows = (
      session.query(models.TestExecution)
      .filter_by(execution_id=execution_id)
      .order_by(models.TestExecution.id.asc())
      .all()
    )
    return [
      {
        "case_id": row.case_id,
        "case_name": row.case_name,
        "result": row.result,
        "duration": row.duration,
        "error_message": row.error_message,
        "environment": row.environment,
        "executor": row.executor,
      }
      for row in rows
    ]


class _EmptyFirstQuery:
  """
  "永远查不到行"的查询替身（模拟写前查重在 TOCTOU 窗口里未命中）

  只实现 `record_execution` 第一道防线用到的那一条链
  （`query(...).filter_by(...).first()`），其余方法一概不提供——
  一旦被测代码换了查重写法，这里会以 AttributeError 立即暴露，
  而不是静默地"恰好也返回 None"。
  """

  def filter_by(self, *args: Any, **kwargs: Any) -> "_EmptyFirstQuery":
    """
    模拟 filter_by 链式调用

    参数:
      *args (Any): 透传占位（不使用）
      **kwargs (Any): 透传占位（不使用）

    返回:
      _EmptyFirstQuery: 自身，保持链式调用
    """
    return self

  def first(self) -> None:
    """
    模拟 `.first()` 查不到行

    返回:
      None: 永远"未命中"，把调用推进到写库分支
    """
    return None


class _MissFirstQuerySession:
  """
  首个 query() 返回空查询替身的会话包装（其余能力全部委托真实会话）

  为什么包装会话而不是打补丁到 SQLAlchemy: `record_execution` 的第一道
  防线是**内联** SELECT（`session.query(TestExecution.id)...`），没有
  可 monkeypatch 的方法入口；而 `_execution_detail_exists` 属于第三道
  防线自己的回查，patch 它反而会把"重复键"误判成"其它完整性异常"。
  只包住本次调用拿到的会话，作用域就是这一条路径。
  """

  def __init__(self, session: Any, state: dict[str, bool]) -> None:
    """
    包装真实会话

    参数:
      session (Any): DatabaseSession.session_scope 产出的真实 Session
      state (dict[str, bool]): 共享状态，`state["missed"]` 表示空查询
                   替身已被消耗（即第一道防线确实被放行）

    返回:
      无
    """
    self._session = session
    self._state = state

  def query(self, *args: Any, **kwargs: Any) -> Any:
    """
    第一次 query 返回空查询替身，之后一律委托真实会话

    参数:
      *args (Any): 透传给真实会话的 query
      **kwargs (Any): 透传给真实会话的 query

    返回:
      Any: 首次为 _EmptyFirstQuery，之后为真实 Query 对象
    """
    if not self._state["missed"]:
      self._state["missed"] = True
      return _EmptyFirstQuery()
    return self._session.query(*args, **kwargs)

  def __getattr__(self, name: str) -> Any:
    """
    未显式覆盖的属性一律委托真实会话（如 add / commit / close）

    参数:
      name (str): 属性名

    返回:
      Any: 真实会话上的同名属性

    异常:
      AttributeError: 真实会话也没有该属性时照常抛出
    """
    return getattr(self._session, name)


def _open_toctou_window(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
  """
  打开一次"查重未命中"的 TOCTOU 窗口（内部方法，第三道防线取证用）

  效果: monkeypatch `DatabaseSession.session_scope`，本次调用拿到的会话
  在**第一次** query 时返回空查询替身（等价于"并发写方通过查重的那一刻"），
  之后恢复真实实现——第三道防线捕获 IntegrityError 后的回查必须拿到
  真实结果，否则它会把重复键当成其它完整性异常原样上抛。

  参数:
    monkeypatch (pytest.MonkeyPatch): 补丁工具（用例结束自动还原）

  返回:
    dict[str, bool]: 共享状态；"missed" 为 True 表示空查询替身确已被
      消耗（第一道防线被放行），用例据此取证"窗口真的开了"

  异常:
    无
  """
  real_scope = DatabaseSession.session_scope
  state: dict[str, bool] = {"missed": False}

  @contextlib.contextmanager
  def _miss_first_query_scope() -> Iterator[Any]:
    """
    包装真实会话作用域（内部函数）

    返回:
      Iterator[Any]: yield 包装后的会话
    """
    with real_scope() as session:
      yield _MissFirstQuerySession(session, state)

  monkeypatch.setattr(
    DatabaseSession, "session_scope", _miss_first_query_scope
  )
  return state


class _RecordingPytestRunner(PytestRunner):
  """
  记录型真实执行器（测试替身，行为与 PytestRunner 完全一致）

  只多一件事: 把 run_one 的**最终** ExecutionResult 留在进程内，
  供断言读取 `parsed_result` / `retries_used` 这两个不落库的字段。
  不改变任何执行语义——所有真实子进程调用都走父类实现。
  """

  def __init__(self, **kwargs: Any) -> None:
    """
    构造记录型执行器

    参数:
      **kwargs (Any): 原样透传给 PytestRunner（timeout/cwd/env/
               max_retries/retry_delay）

    返回:
      无
    """
    super().__init__(**kwargs)
    self.results: list[ExecutionResult] = []

  def run_one(self, case: dict[str, Any]) -> ExecutionResult:
    """
    执行单条用例并留存结果

    参数:
      case (dict): 待执行用例字典

    返回:
      ExecutionResult: 父类返回的最终结果（原样返回，不加工）
    """
    result = super().run_one(case)
    self.results.append(result)
    return result


class _RetryAfterTimeoutRunner(PytestRunner):
  """
  首次尝试伪超时、其余尝试真实执行的执行器（测试替身，Day61 重试验证）

  为什么要伪超时而不是让真实子进程跑超时: 真实超时要在"pytest 启动开销
  （实测约 1.5s）"与"用例执行时长"之间卡一个极窄的 timeout 窗口，
  同机负载波动就会让首次尝试变成"跑完并通过"——那是把断言建在
  时序运气上。这里首次尝试直接返回一份 `exit_reason=timeout` 的结果
  （与真实超时分支同形状: result=error、exit_code=None），
  重试循环、第二次真实执行、入库与看板全部仍是真实链路。
  """

  def __init__(self, **kwargs: Any) -> None:
    """
    构造伪超时执行器

    参数:
      **kwargs (Any): 原样透传给 PytestRunner

    返回:
      无
    """
    super().__init__(**kwargs)
    self.attempts = 0
    self.results: list[ExecutionResult] = []

  def _execute_once(self, case: dict[str, Any]) -> ExecutionResult:
    """
    首次尝试返回伪超时结果，之后委托真实实现

    参数:
      case (dict): 待执行用例字典

    返回:
      ExecutionResult: 首次为 timeout 形状的结果，之后为真实执行结果
    """
    self.attempts += 1
    if self.attempts == 1:
      timeout_result = ExecutionResult(
        result="error",
        error_message="首次尝试伪超时（测试替身: 真实超时窗口过窄会引入时序抖动）",
        duration=0.01,
        exit_code=None,
        exit_reason=EXIT_REASON_TIMEOUT,
      )
      self.results.append(timeout_result)
      return timeout_result
    real_result = super()._execute_once(case)
    self.results.append(real_result)
    return real_result


# ===========================================================================
# 1~4. 单批次全链路: 通过 / 失败 / 重试 / 幂等
# ===========================================================================
@allure.feature("Day62 全链路集成")
@allure.story("触发→执行→解析→入库→看板")
class TestSingleBatchChain:
  """单批次全链路: 通过、失败、重试、写库幂等"""

  @allure.title("通过场景: 建用例→触发→执行→入库→看板全链路走通")
  def test_full_chain_passed_case_visible_on_dashboard(
    self,
    day62_client: FlaskClient,
    make_case_file: Callable[[str, str], str],
  ) -> None:
    """
    任务一的通过场景（链路五跳逐跳取证）:
      ①CaseManager 建用例，source_ref 归一化后落库；
      ②POST /api/executions/trigger 受理并返回 202；
      ③真实 pytest 子进程执行该探针文件，退出码 0；
      ④test_executions 落**恰好一条** passed 明细（duration>0、
        executor=web、environment=dev，均取自批次行）；
      ⑤看板趋势接口立刻能查到该批次，pass_rate 为落库原值 1.0。
    """
    source_ref = make_case_file(
      "test_day62_chain_pass", "def test_chain_ok():\n  assert True\n"
    )
    created = _create_case("TM-D62-0001", "全链路-执行通过", source_ref=source_ref)
    assert created["source_ref"] == source_ref  # 链路第①跳: 执行目标落库

    execution_id = _trigger(
      day62_client, {"executor": "pytest", "module": MODULE_MAIN}
    )
    status_data = _wait_terminal(day62_client, execution_id)
    assert status_data["status"] == "finished"
    assert status_data["total_cases"] == 1
    assert status_data["passed"] == 1
    assert status_data["failed"] == 0
    assert status_data["error"] == 0
    assert status_data["pass_rate"] == 1.0

    rows = _execution_rows(execution_id)
    assert len(rows) == 1  # 链路第④跳: 明细条数 = 用例数
    assert rows[0]["case_id"] == "TM-D62-0001"
    assert rows[0]["result"] == "passed"
    assert rows[0]["error_message"] is None
    assert rows[0]["duration"] > 0  # 真实子进程耗时，非模拟器常量
    assert rows[0]["executor"] == "web"  # 批次行透传，不是模型默认值 local
    assert rows[0]["environment"] == "dev"

    detail = _batch_detail(day62_client, execution_id)
    assert detail["summary"]["passed"] == 1
    assert detail["items"][0]["result"] == "passed"

    row = _trend_row_of(_trend_rows(day62_client), execution_id)
    assert row["total_cases"] == 1
    assert row["passed"] == 1
    assert row["failed"] == 0
    assert row["pass_rate"] == 1.0  # 落库原值 0~1，clamp 只在展示层

  @allure.title("失败场景: 真实断言失败落 failed 并带回真实 traceback")
  def test_failed_case_persists_real_traceback_and_pass_rate(
    self,
    day62_client: FlaskClient,
    make_case_file: Callable[[str, str], str],
  ) -> None:
    """
    任务一的失败场景:
      真实 `assert 1 == 2` 让 pytest 退出码为 1，链路必须把结论
      落成 failed（不是 error——error 是"这次没跑成"），
      并把子进程真输出的断言文本透传进 error_message（不截断），
      看板通过率随之变为 0.0（失败必须被看板看见）。
    """
    source_ref = make_case_file(
      "test_day62_chain_fail",
      "def test_chain_fail():\n  assert 1 == 2, 'day62 chain boom'\n",
    )
    _create_case("TM-D62-0011", "全链路-执行失败", source_ref=source_ref)

    execution_id = _trigger(
      day62_client, {"executor": "pytest", "module": MODULE_MAIN}
    )
    status_data = _wait_terminal(day62_client, execution_id)
    assert status_data["status"] == "finished"
    assert status_data["total_cases"] == 1  # 与通过场景同口径: 1 条用例 1 条明细
    assert status_data["failed"] == 1
    assert status_data["error"] == 0
    assert status_data["pass_rate"] == 0.0

    rows = _execution_rows(execution_id)
    assert len(rows) == 1
    assert rows[0]["result"] == "failed"
    # 真实 traceback 摘要（证明落库的是子进程真输出，不是模拟器字符串）
    assert "day62 chain boom" in (rows[0]["error_message"] or "")

    row = _trend_row_of(_trend_rows(day62_client), execution_id)
    assert row["failed"] == 1
    assert row["pass_rate"] == 0.0

  @allure.title("重试机制: 首次超时后重试通过，retries_used 记账且最终 passed")
  def test_retry_after_timeout_marks_retries_used_and_passes(
    self,
    day62_client: FlaskClient,
    make_case_file: Callable[[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
  ) -> None:
    """
    Day61 重试机制的端到端验证（任务三）:
      ①执行器配置 max_retries=1、retry_delay=0（不引入等待）；
      ②首次尝试返回 timeout 形状结果 → run_one 判为可重试；
      ③第二次尝试真实起子进程并通过 → 批次结论 passed；
      ④ExecutionResult.retries_used == 1（在同一对象上记账）；
      ⑤入库与看板都只看最终结论（一条 passed 明细、通过率 1.0）——
        "第一次超时"不会污染批次通过率。
    """
    source_ref = make_case_file(
      "test_day62_retry_pass", "def test_retry_ok():\n  assert True\n"
    )
    _create_case("TM-D62-0021", "全链路-首次超时重试通过", source_ref=source_ref)

    runner = _RetryAfterTimeoutRunner(max_retries=1, retry_delay=0.0)
    # 只替换编排层取执行器的入口，执行器内部（重试循环/子进程/桥接）全真
    monkeypatch.setattr(
      case_manager_mod, "get_executor", lambda kind=None: runner
    )

    execution_id = _trigger(
      day62_client, {"executor": "pytest", "module": MODULE_MAIN}
    )
    status_data = _wait_terminal(day62_client, execution_id)
    assert status_data["status"] == "finished"
    assert status_data["total_cases"] == 1  # 重试不产生额外明细（1 条用例 1 行）
    assert status_data["passed"] == 1
    assert status_data["failed"] == 0

    assert runner.attempts == 2  # max_retries=1 → 最多执行 2 次
    assert len(runner.results) == 2
    assert runner.results[0].exit_reason == EXIT_REASON_TIMEOUT
    assert runner.results[0].retries_used == 0  # 首次尝试不带重试记账
    assert runner.results[-1].retries_used == 1  # 重试次数由 run_one 记账
    assert runner.results[-1].result == "passed"

    rows = _execution_rows(execution_id)
    assert len(rows) == 1
    assert rows[0]["result"] == "passed"
    row = _trend_row_of(_trend_rows(day62_client), execution_id)
    assert row["pass_rate"] == 1.0

  @allure.title("写库幂等: 重复 record_execution 返回 False 且批次明细不膨胀")
  def test_duplicate_record_execution_keeps_detail_count_stable(
    self,
    day62_client: FlaskClient,
    make_case_file: Callable[[str, str], str],
  ) -> None:
    """
    Day60 写库幂等的端到端验证（任务四）:
      真实执行完成后，对同一 (execution_id, case_id) 再写两次:
        ①两次都返回 False（幂等，不是抛异常）；
        ②test_executions 行数仍为 1；
        ③重新聚合（finish_execution）后批次 total 仍为 1、
          passed 仍为 1——明细不膨胀，聚合口径不被污染。
    """
    source_ref = make_case_file(
      "test_day62_idempotent", "def test_idem_ok():\n  assert True\n"
    )
    _create_case("TM-D62-0031", "全链路-写库幂等", source_ref=source_ref)

    execution_id = _trigger(
      day62_client, {"executor": "pytest", "module": MODULE_MAIN}
    )
    status_data = _wait_terminal(day62_client, execution_id)
    assert status_data["total_cases"] == 1
    assert len(_execution_rows(execution_id)) == 1

    now = datetime.now()
    for _ in range(2):
      written = CaseManager.record_execution(
        execution_id=execution_id,
        case_id="TM-D62-0031",
        case_name="全链路-写库幂等",
        result="passed",
        start_time=now,
        end_time=now,
        duration=0.1,
      )
      assert written is False  # 幂等跳过，而不是再插一行

    # 幂等写入不得改变行数与聚合结果（批次统计不被重复写撑大）
    assert len(_execution_rows(execution_id)) == 1
    summary = CaseManager.finish_execution(execution_id)
    assert summary["total"] == 1
    assert summary["passed"] == 1
    status_after = _wait_terminal(day62_client, execution_id)
    assert status_after["total_cases"] == 1
    assert status_after["passed"] == 1

  @allure.title("写库幂等: 查重未命中的窗口下唯一约束仍吸收重复写")
  def test_duplicate_record_absorbed_by_unique_constraint(
    self,
    day62_client: FlaskClient,
    make_case_file: Callable[[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
  ) -> None:
    """
    Day60 第三道防线的独立验证（复核实测 P2-01：原用例只触达第一道防线）:

    背景: `record_execution` 是三层防御——①写前 SELECT 查重（内联
    `session.query`）→ ②`session.add` + INSERT → ③撞唯一约束后回查、
    命中即按重复吸收并返回 False。串行重复写在第①层就 return 了，
    所以"删掉第③层"的变异不会让上一条用例变红（变异存活）。

    本用例故意制造"查重未命中"的 TOCTOU 窗口，把调用推到第③层:
      ①真实执行完成后记录明细行数（1 行）；
      ②屏蔽第①层（首次 query 返回空查询替身＝并发写方通过查重那一刻）；
      ③同键再写一次 ⇒ 返回 False（被唯一约束吸收，不是抛异常）；
      ④第③层的回查**确实被调用过**（spy 取证），证明走的不是第①层；
      ⑤明细行数仍为 1，`finish_execution` 聚合 total/passed 仍为 1。

    与任务书的偏差（有意，已按真实代码处置）: 任务书让 monkeypatch
      `_execution_detail_exists` 来"禁用第一道防线"，但该方法实际是
      **第三道防线自己的回查**（`record_execution` 的第①层是内联 SELECT，
      没有独立方法入口）。patch 它会让第③层把重复键误判成"其它完整性
      异常"并上抛 `CaseManagerError`——验证面从"吸收能力"变成"失败路径"。
      故改为包装会话作用域、只让首个 query 查不到行，作用域限定本次调用。
    """
    source_ref = make_case_file(
      "test_day62_defense3", "def test_defense3_ok():\n  assert True\n"
    )
    _create_case("TM-D62-0032", "全链路-第三道防线", source_ref=source_ref)

    execution_id = _trigger(
      day62_client, {"executor": "pytest", "module": MODULE_MAIN}
    )
    status_data = _wait_terminal(day62_client, execution_id)
    assert status_data["total_cases"] == 1
    rows_before = _execution_rows(execution_id)
    assert len(rows_before) == 1
    assert rows_before[0]["result"] == "passed"

    # 第③层取证的 spy: 只记录调用并委托真实实现，不改行为
    recheck_calls: list[tuple[str, str]] = []
    original_recheck = CaseManager._execution_detail_exists

    def _spy_recheck(execution_id_arg: str, case_id_arg: str) -> bool:
      """
      记录第三道防线回查的入参后委托真实实现

      参数:
        execution_id_arg (str): 批次号
        case_id_arg (str): 用例编号

      返回:
        bool: 真实回查结果（库中是否已有该明细行）
      """
      recheck_calls.append((execution_id_arg, case_id_arg))
      return original_recheck(execution_id_arg, case_id_arg)

    monkeypatch.setattr(
      CaseManager, "_execution_detail_exists", _spy_recheck
    )
    window_state = _open_toctou_window(monkeypatch)

    written = CaseManager.record_execution(
      execution_id=execution_id,
      case_id="TM-D62-0032",
      case_name="全链路-第三道防线",
      result="passed",
      start_time=datetime.now(),
      end_time=datetime.now(),
      duration=0.1,
      environment="dev",
      executor="web",
    )
    assert written is False  # 撞唯一约束后被吸收，而不是抛异常
    assert window_state["missed"] is True  # 第①层确实被放行（窗口真的开了）
    assert recheck_calls == [(execution_id, "TM-D62-0032")]  # 第③层回查发生

    # 吸收之后明细不膨胀、聚合口径不变
    assert len(_execution_rows(execution_id)) == 1
    summary = CaseManager.finish_execution(execution_id)
    assert summary["total"] == 1
    assert summary["passed"] == 1
    status_after = _wait_terminal(day62_client, execution_id)
    assert status_after["total_cases"] == 1
    assert status_after["passed"] == 1


# ===========================================================================
# 5~6. 批量执行与输出解析口径
# ===========================================================================
@allure.feature("Day62 全链路集成")
@allure.story("批量执行与解析口径")
class TestBatchAndParserChain:
  """批量执行一致性、真实输出解析口径"""

  @allure.title("批量执行: 入库条数=触发条数，批次 passed/failed/total 逐条对得上")
  def test_batch_execution_counts_match_triggered_cases(
    self,
    day62_client: FlaskClient,
    make_case_file: Callable[[str, str], str],
  ) -> None:
    """
    任务五（批量执行端到端）:
      一次触发 3 条用例（2 通过 + 1 断言失败）→ 全部完成后:
        ①受理层 total_cases 与入库明细条数均为 3；
        ②批次 passed=2、failed=1、error=0、pass_rate=round(2/3, 4)；
        ③明细逐条 case_id 与预期结果一一对应（没有串行/串位）；
        ④看板趋势行与批次汇总同口径（total/ passed/ failed 三处一致）。
    """
    pass_ref = make_case_file(
      "test_day62_batch_pass_a", "def test_batch_a():\n  assert True\n"
    )
    pass_ref_b = make_case_file(
      "test_day62_batch_pass_b", "def test_batch_b():\n  assert 2 + 2 == 4\n"
    )
    fail_ref = make_case_file(
      "test_day62_batch_fail",
      "def test_batch_c():\n  assert 'day62' == 'batch-boom'\n",
    )
    _create_case("TM-D62-0101", "批量-用例A通过", MODULE_BATCH, pass_ref)
    _create_case("TM-D62-0102", "批量-用例B通过", MODULE_BATCH, pass_ref_b)
    _create_case("TM-D62-0103", "批量-用例C失败", MODULE_BATCH, fail_ref)
    # 前置对账: 筛选结果必须"恰好这 3 条"——只校验非空时，筛选条件写错
    # （比如 module 漏传成全量 4 条）也能通过，后面的"入库 3 条"才会红，
    # 定位成本高且容易误判成入库侧问题
    selected = CaseManager.select_cases_for_execution(module=MODULE_BATCH)
    assert len(selected) == 3
    assert {case["case_id"] for case in selected} == {
      "TM-D62-0101",
      "TM-D62-0102",
      "TM-D62-0103",
    }

    execution_id = _trigger(
      day62_client, {"executor": "pytest", "module": MODULE_BATCH}
    )
    status_data = _wait_terminal(day62_client, execution_id)
    assert status_data["status"] == "finished"
    assert status_data["total_cases"] == 3
    assert status_data["passed"] == 2
    assert status_data["failed"] == 1
    assert status_data["error"] == 0
    assert status_data["skipped"] == 0
    assert status_data["pass_rate"] == round(2 / 3, 4)

    rows = _execution_rows(execution_id)
    assert len(rows) == 3  # 链路第④跳: 入库条数 = 触发条数
    by_case = {row["case_id"]: row["result"] for row in rows}
    assert by_case == {
      "TM-D62-0101": "passed",
      "TM-D62-0102": "passed",
      "TM-D62-0103": "failed",
    }

    detail = _batch_detail(day62_client, execution_id)
    assert detail["summary"]["total_cases"] == 3
    assert len(detail["items"]) == 3
    row = _trend_row_of(_trend_rows(day62_client), execution_id)
    assert (row["total_cases"], row["passed"], row["failed"]) == (3, 2, 1)

  @allure.title("输出解析: 真实 pytest 输出解析计数与用例级入库口径均被钉死")
  def test_real_output_parsed_counts_are_consistent(
    self,
    day62_client: FlaskClient,
    make_case_file: Callable[[str, str], str],
    monkeypatch: pytest.MonkeyPatch,
  ) -> None:
    """
    任务六（输出解析端到端）:
      一个探针文件里同时有 4 个 pytest 用例项（1 通过 / 1 断言失败 /
      1 跳过 / 1 fixture 异常），验证两套口径各自成立且差异被显式记录:
        ①解析口径（pytest 用例项级）: parse_ok=True，
          passed=1、failed=1、skipped=1、error=1，total=4；
        ②解析结果可复现: 对 ExecutionResult.output 再跑一次
          PytestOutputParser.parse，四项计数完全相同；
        ③入库口径（平台用例级）: 本批次 1 条用例 → total_cases=1、
          failed=1、error=0、skipped=0（平台的一个 source_ref 就是
          "一条用例"，pytest 内部的用例项数不直接进库）；
        ④真实失败文本落进 error_message（解析→入库这一跳的取证）。
    """
    mixed_body = '''import pytest


@pytest.fixture
def boom():
  raise RuntimeError("day62 parser fixture boom")


def test_parse_pass():
  assert True


def test_parse_fail():
  assert 1 == 2, "day62 parse boom"


def test_parse_skip():
  pytest.skip("day62 parse skip")


def test_parse_error(boom):
  assert True
'''
    source_ref = make_case_file("test_day62_parser_mixed", mixed_body)
    _create_case("TM-D62-0201", "解析-混合结果", MODULE_PARSER, source_ref)

    runner = _RecordingPytestRunner()
    monkeypatch.setattr(
      case_manager_mod, "get_executor", lambda kind=None: runner
    )

    execution_id = _trigger(
      day62_client, {"executor": "pytest", "module": MODULE_PARSER}
    )
    status_data = _wait_terminal(day62_client, execution_id)
    assert status_data["status"] == "finished"

    assert len(runner.results) == 1
    result = runner.results[0]
    parsed = result.parsed_result
    assert parsed is not None, "真实 pytest 输出必须能解析出汇总行"
    assert parsed.parse_ok is True
    assert (
      parsed.passed_count,
      parsed.failed_count,
      parsed.skipped_count,
      parsed.error_count,
    ) == (1, 1, 1, 1)
    assert parsed.total_count == 4  # 四项之和，xfail/xpass 不计入

    # 同一份输出再解析一次必须得到同样的计数（解析无状态、口径可复现）
    reparsed = PytestOutputParser.parse(result.output)
    assert (
      reparsed.passed_count,
      reparsed.failed_count,
      reparsed.skipped_count,
      reparsed.error_count,
    ) == (1, 1, 1, 1)

    # 入库口径: 平台按"一条用例一个结论"落库（退出码 1 → failed）
    assert status_data["total_cases"] == 1
    assert status_data["failed"] == 1
    assert status_data["error"] == 0
    assert status_data["skipped"] == 0

    rows = _execution_rows(execution_id)
    assert len(rows) == 1
    assert rows[0]["result"] == "failed"
    assert "day62 parse boom" in (rows[0]["error_message"] or "")


# ===========================================================================
# 7~8. 看板趋势与执行详情
# ===========================================================================
@allure.feature("Day62 全链路集成")
@allure.story("看板与详情查询")
class TestDashboardAndDetailChain:
  """看板趋势多批次可见性、执行详情完整字段"""

  @allure.title("看板趋势: 连续两次执行的通过率各自可见且历史顺序正确")
  def test_two_executions_both_visible_in_trend(
    self,
    day62_client: FlaskClient,
    make_case_file: Callable[[str, str], str],
  ) -> None:
    """
    任务七（看板趋势端到端）:
      连续触发两次独立执行（先全部通过、后全部失败）→:
        ①趋势接口两条都在（历史数据不互相覆盖）；
        ②通过率分别为 1.0 与 0.0（不是同一个值被复用）；
        ③两条 created_at 升序排列，先跑的批次在前（折线图时间轴）；
        ④两次执行的耗时都能从详情接口读到（>0，真实子进程耗时）。
    """
    pass_ref, fail_ref = _make_pass_fail_probe_refs(
      make_case_file, "trend", "day62 trend boom"
    )

    _create_case("TM-D62-0301", "趋势-第一次通过", MODULE_TREND, pass_ref)
    first_id = _trigger(
      day62_client, {"executor": "pytest", "module": MODULE_TREND}
    )
    first_status = _wait_terminal(day62_client, first_id)
    assert first_status["passed"] == 1

    # 第二次换成失败用例（同一模块，覆盖同一批次的通过率口径）
    CaseManager.update_case("TM-D62-0301", {"source_ref": fail_ref})
    second_id = _trigger(
      day62_client, {"executor": "pytest", "module": MODULE_TREND}
    )
    second_status = _wait_terminal(day62_client, second_id)
    assert second_status["failed"] == 1
    assert first_id != second_id

    rows = _trend_rows(day62_client)
    first_row = _trend_row_of(rows, first_id)
    second_row = _trend_row_of(rows, second_id)
    assert first_row["pass_rate"] == 1.0
    assert second_row["pass_rate"] == 0.0
    assert first_row["total_cases"] == 1
    assert second_row["total_cases"] == 1
    # 时间升序（同秒完成时 created_at 相同也满足非降序，不赌时钟精度）
    assert first_row["created_at"] <= second_row["created_at"]
    assert rows.index(first_row) < rows.index(second_row)

    for execution_id in (first_id, second_id):
      items = _batch_detail(day62_client, execution_id)["items"]
      assert len(items) == 1
      assert items[0]["duration"] > 0  # 耗时可见

  @allure.title("执行详情: 完成后返回 summary + 完整字段明细且按执行顺序排列")
  def test_execution_detail_after_completion(
    self,
    day62_client: FlaskClient,
    make_case_file: Callable[[str, str], str],
  ) -> None:
    """
    任务八（详情查询端到端）:
      一条通过 + 一条失败的批次完成后 GET /api/executions/<id>:
        ①summary 携带 8 个契约字段，计数与批次状态接口一致；
        ②items 携带 12 个契约字段（含 duration / environment /
          executor / error_message）；
        ③items 按 id 升序（与 record_execution 写入顺序一致，即执行先后）；
        ④通过项 error_message 为 null，失败项带回真实断言文本。

    已知边界（记录而非断言缺陷）: 详情接口**不含 retries_used**——
      该字段只存在于 ExecutionResult，Day61 明确不改数据库 schema，
      故详情层无从读取。重试次数由第 3 条用例在进程内取证。
    """
    pass_ref, fail_ref = _make_pass_fail_probe_refs(
      make_case_file, "detail", "day62 detail boom"
    )
    _create_case("TM-D62-0401", "详情-通过", MODULE_DETAIL, pass_ref)
    _create_case("TM-D62-0402", "详情-失败", MODULE_DETAIL, fail_ref)

    execution_id = _trigger(
      day62_client, {"executor": "pytest", "module": MODULE_DETAIL}
    )
    status_data = _wait_terminal(day62_client, execution_id)
    assert status_data["total_cases"] == 2

    detail = _batch_detail(day62_client, execution_id)
    summary = detail["summary"]
    assert set(summary) == EXPECTED_SUMMARY_FIELDS
    assert summary["execution_id"] == execution_id
    assert summary["total_cases"] == status_data["total_cases"]
    assert summary["passed"] == status_data["passed"] == 1
    assert summary["failed"] == status_data["failed"] == 1
    assert summary["pass_rate"] == status_data["pass_rate"]

    items = detail["items"]
    assert len(items) == 2
    for item in items:
      assert set(item) == EXPECTED_ITEM_FIELDS
      assert item["execution_id"] == execution_id
      assert item["duration"] > 0
      assert item["executor"] == "web"
      assert item["environment"] == "dev"
    # 按 id 升序 = 按执行先后（写入顺序）
    assert items[0]["id"] < items[1]["id"]

    by_case = {item["case_id"]: item for item in items}
    assert by_case["TM-D62-0401"]["result"] == "passed"
    assert by_case["TM-D62-0401"]["error_message"] is None
    assert by_case["TM-D62-0402"]["result"] == "failed"
    assert "day62 detail boom" in by_case["TM-D62-0402"]["error_message"]


# ===========================================================================
# 9~10. 并发执行与异常路径
# ===========================================================================
@allure.feature("Day62 全链路集成")
@allure.story("并发与异常路径")
class TestConcurrencyAndErrorPath:
  """并发批次隔离、异常路径零脏数据"""

  @allure.title("并发执行: 两个批次同时触发互不干扰，看板数据不混淆")
  def test_concurrent_executions_keep_batches_isolated(
    self,
    day62_client: FlaskClient,
    make_case_file: Callable[[str, str], str],
  ) -> None:
    """
    任务九（并发执行端到端）:
      两个用例分属不同模块，两个线程用**各自的** test client 同时
      触发（Barrier 对齐后同时发请求）→ 两个后台批次并发执行:
        ①两个批次都进终态，各自 total_cases=1；
        ②各自明细只有自己那条用例（没有把对方的用例写进来）；
        ③看板趋势两条独立成行，通过率 1.0 与 0.0 没有互相串味。
    """
    pass_ref, fail_ref = _make_pass_fail_probe_refs(
      make_case_file, "conc", "day62 conc boom"
    )
    _create_case("TM-D62-0501", "并发-通过", MODULE_CONCURRENT_PASS, pass_ref)
    _create_case("TM-D62-0502", "并发-失败", MODULE_CONCURRENT_FAIL, fail_ref)

    second_client = day62_client.application.test_client()
    barrier = threading.Barrier(2)
    triggered: dict[str, str] = {}
    thread_errors: list[BaseException] = []

    def _submit(label: str, client: FlaskClient, module: str) -> None:
      """
      线程体: 对齐后触发一次执行

      参数:
        label (str): 结果字典的键（pass/fail）
        client (FlaskClient): 本线程独占的测试客户端
        module (str): 模块筛选值

      返回:
        无
      """
      try:
        barrier.wait(timeout=30)
        triggered[label] = _trigger(
          client, {"executor": "pytest", "module": module}
        )
      except BaseException as exc:  # 线程内异常不能静默丢弃，交回主线程断言
        thread_errors.append(exc)

    threads = [
      threading.Thread(
        target=_submit,
        args=("pass", day62_client, MODULE_CONCURRENT_PASS),
        daemon=True,
      ),
      threading.Thread(
        target=_submit,
        args=("fail", second_client, MODULE_CONCURRENT_FAIL),
        daemon=True,
      ),
    ]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join(timeout=60)

    assert not thread_errors, thread_errors
    assert set(triggered) == {"pass", "fail"}
    pass_id = triggered["pass"]
    fail_id = triggered["fail"]
    assert pass_id != fail_id

    pass_status = _wait_terminal(day62_client, pass_id)
    fail_status = _wait_terminal(day62_client, fail_id)
    assert pass_status["total_cases"] == 1
    assert pass_status["passed"] == 1
    assert fail_status["total_cases"] == 1
    assert fail_status["failed"] == 1

    pass_rows = _execution_rows(pass_id)
    fail_rows = _execution_rows(fail_id)
    assert [row["case_id"] for row in pass_rows] == ["TM-D62-0501"]
    assert [row["case_id"] for row in fail_rows] == ["TM-D62-0502"]
    assert pass_rows[0]["result"] == "passed"
    assert fail_rows[0]["result"] == "failed"

    rows = _trend_rows(day62_client)
    assert _trend_row_of(rows, pass_id)["pass_rate"] == 1.0
    assert _trend_row_of(rows, fail_id)["pass_rate"] == 0.0

  @allure.title("异常路径: 非法触发返 400、不存在批次返 404，且零脏数据")
  def test_invalid_trigger_leaves_no_dirty_data(
    self, day62_client: FlaskClient
  ) -> None:
    """
    任务十（异常路径端到端）:
      ①执行器枚举非法 → 400（参数层拦截，不建批次行）；
      ②筛选条件命中零用例 → 400（业务语义，不建批次行）；
      ③查询不存在的批次状态/详情 → 404；
      ④以上全部走完后 test_execution_batches / test_executions /
        defect_statistics 三张表均为空——失败的请求不产脏数据，
        也不会让看板出现空批次。
    """
    _create_case("TM-D62-0601", "异常路径-正常用例", MODULE_MAIN)

    bad_executor = day62_client.post(
      "/api/executions/trigger",
      json={"executor": "not-a-real-executor"},
    )
    assert bad_executor.status_code == 400
    assert "executor" in bad_executor.get_json()["message"]

    no_cases = day62_client.post(
      "/api/executions/trigger",
      json={"executor": "pytest", "module": "Day62不存在的模块"},
    )
    assert no_cases.status_code == 400

    missing = "RUN-DAY62-NOT-EXIST-0001"
    status_response = day62_client.get(f"/api/executions/{missing}/status")
    assert status_response.status_code == 404
    detail_response = day62_client.get(f"/api/executions/{missing}")
    assert detail_response.status_code == 404

    with DatabaseSession.session_scope() as session:
      assert session.query(models.TestExecutionBatch).count() == 0
      assert session.query(models.TestExecution).count() == 0
      assert session.query(models.DefectStatistic).count() == 0

    # 看板在空库下必须是空列表，而不是 500 或伪造数据
    assert _trend_rows(day62_client) == []
