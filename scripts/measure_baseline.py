"""
TestMatrix 全量基线测量与绝对规模北极星测量脚本（Day63 基线 / Day64 并发曲线）

定位:
  测量日专用只读工具。不修改 src/ 任何一行、不新增测试、不改 CI 配置，
  只把"同机 / 同数据集 / 重复取中位"的测量方法固化为可复现脚本，
  产出 raw JSON + 控制台摘要，供 docs/baseline_day63.md 与
  docs/baseline_day64.md 引用。

=========================== 测量协议（正文，唯一来源） ===========================

0. 系统状态
   - 测量机为空载单机（无其它构建/压测进程），Windows + PowerShell，
     Python 3.11.9（项目 .venv），pytest 7.4.4，SQLite 3.45.1（文件库）
   - 数据库使用**开发库副本** output/baseline_day63/measure_day63.db：
     从 output/testmatrix.db 复制而来，测量全程不碰开发库，测后可整库删除
   - 队列关闭（TM_TASK_QUEUE_ENABLED=false）、缓存关闭（TM_REDIS_ENABLED=false）、
     通知渠道关闭（TM_EMAIL_ENABLED/TM_WECHAT_ENABLED=false）——
     外部网络抖动不得进入计时口径，通知与缓存是旁路能力，与执行链路耗时无关
   - 执行器显式指定 pytest（.env 默认 TM_EXECUTOR=simulated，模拟执行器
     不产生真实子进程，用它测吞吐毫无意义）

1. 实验 A —— 当日全量真实集成用例集（1639 条 pytest 用例）
   A1 裸 pytest 基线 B:
      .venv\\Scripts\\python.exe -m pytest -q -n 4 --tb=no
      每轮前删除 output/allure_results（Windows --clean-alluredir 抢目录会
      报 WinError 145），重复 --runs 次取中位
   A2 平台批量执行:
      触发 POST /api/executions/trigger（Flask test_client 走真实路由 +
      真实后台编排，排除网络栈噪声但保留全部平台逻辑），筛选 module=
      baseline_day63_real、executor=pytest，有界轮询
      GET /api/executions/<批次>/status 至 finished/failed，
      计时口径 = POST 发出到批次终态的墙钟秒数
      每轮前清空 test_executions / defect_statistics / test_execution_batches
      （记录清理前行数），保证轮次间零污染
   A3 核心写接口 P95:
      create_case / record_execution / finish_execution 各 100 次，
      空载系统状态下逐次计时，取 P50/P95/P99（毫秒）
   A4 护栏:
      4 worker 吞吐 ≥ 300 用例/分钟（A1 口径：1639 / 耗时分钟）
      平台开销比 ≤ 1.15（A2 / A1）
      create_case / record_execution / finish_execution P95 ≤ 200ms

2. 实验 B —— 1 万条合成轻量用例集
   扩增集构成（必须随报告公开，禁止暗示"1 万真实业务用例"）:
     100% mock：每条用例的 source_ref 全部指向同一个只含
     `assert True` 的轻量文件（无真实网络、无文件 IO、无数据库依赖），
     module 统一为 synthetic_day63，case_id 前缀 TM-SYN-DAY63-，
     name/description 显式标注"合成轻量用例-D63测量用"
   测量:
     导入 10000 条 → 触发 module=synthetic_day63 的批次（可只跑前 N 条
     样本，样本用 priority=P0 标记，其余 P3，二者用例形状完全一致）→
     有界轮询终态 → 计时 → 清理（删用例 + 清执行记录 + 校验行数恢复）
   护栏: 1 万条端到端 ≤ 720 秒（12 分钟）

3. 统计口径
   - 所有计时用 time.perf_counter()（毫秒精度），不用 time.time()
   - 重复 --runs 次取中位数（statistics.median）消除单次抖动
   - 百分位用最近秩法：第 p 百分位 = 升序第 ceil(p/100*N) 个
   - 轮询一律有上界（平台 1800s / 合成 1800s），禁止无限等待；
     轮询间隔用固定 sleep 是等待下一次查询，不是"用 sleep 赌时序"

4. 不达标处理
   只记录根因与后续优化排期（Day64 根因 + Day96-100 缓冲优化），
   绝不下调护栏、绝不缩小扩增集凑数。

5. 命令行用法（PowerShell）
   .venv\\Scripts\\python.exe scripts/measure_baseline.py --experiment A --runs 1
   .venv\\Scripts\\python.exe scripts/measure_baseline.py --experiment A --runs 3
   .venv\\Scripts\\python.exe scripts/measure_baseline.py --experiment B --runs 3
   .venv\\Scripts\\python.exe scripts/measure_baseline.py --experiment all --runs 3

6. 平台执行样本量说明（口径透明）
   平台对**每条用例起一个 pytest 子进程**且串行编排（executors.PytestRunner
   + case_manager._execute_batch_async 逐条 run_one），单条真实用例的进程
   启动开销在 2 秒量级——1639 条全量平台执行约 1 小时/轮，1 万条合成集约
   1.4 小时/轮。故本脚本对"平台侧"采用**同数据集前缀样本 + 线性外推**：
   样本与全量数据集形状完全一致（同一批真实 nodeid、同一份合成文件），
   并额外用 --linear-check 在 2 倍样本量上复测，验证单条成本恒定，
   外推结论与逐条实测成本一致。裸 pytest 与写接口 P95 均为全量/定量实测，
   不做外推。

7. Day64 并发曲线（--concurrency-curve，与上面两个实验并列的独立测量模式）
   目的: 回答"多 worker 下到底是并发不够、还是写锁在挡路"，给出 1/2/4/8
         四档的耗时、吞吐、加速比与写锁等待，作为 docs/baseline_day64.md 数据源。
   7.1 xdist 并发档位:
      .venv\\Scripts\\python.exe -m pytest -q --tb=no -n <N>   N ∈ {1,2,4,8}
      与实验 A1 同口径（不含 --cov：覆盖率插桩会放大锁竞争并显著拖慢，
      Day60 已实测 -n 8 --cov 的稳定性问题，测量耗时不混入插桩成本），
      每档重复 --runs 次取中位；每轮前删除 output/allure_results（同 A1 理由）。
      说明: -n 1 走的是 xdist 的单 worker 形态，不是"完全串行"，
      与实验 A1 的 -n 4 同族可比；本模式下不另测纯串行，避免口径混用。
   7.2 吞吐与加速比:
      吞吐(用例/分) = 本轮通过条数 / 中位耗时(s) × 60
      加速比 = 1 档中位耗时 / N 档中位耗时；效率 = 加速比 / N
      通过条数取实测值（pytest 尾部摘要 "N passed"），不用写死常量，
      避免用例集增长后吞吐被固定分母拉偏。
   7.3 写锁等待（间接探针，标注清楚不是业务链路）:
      pytest 侧各 worker 的会话库按 pytest-xdist 的 basetemp 分片
      （xdist workermanage 给每个 worker 下发 <basetemp>/popen-gwN），
      即**跨 worker 不共享同一个 SQLite 文件**，因此并发曲线的差异
      主要来自进程/CPU 侧，而不是跨 worker 的写锁排队。
      为了给"如果共享一个文件库会怎样"一个可复现数字，另起一个
      **合成写锁探针**：N 个线程对同一个临时 SQLite 文件各写 M 条
      （BEGIN IMMEDIATE + INSERT + COMMIT，busy_timeout=30000，
      与 Day60 的测试环境取值一致），逐条计时；
      单轮抖动大（OS 调度/GC 会让个别插入等几十毫秒），故每档重复
      --runs 轮、p50/p95/p99 取轮间中位；
      写锁等待(p50) = 该档中位 p50 − 1 档中位 p50（差值口径，不是内核计数）。
      探针库在每轮结束即删除，不进入业务库、不参与实验 A/B 口径。
   7.4 结果合并:
      每次调用只测 --concurrency-levels 指定的档位，并与既有 JSON 合并
      （便于分段执行：1 档单独跑、2/4/8 一起跑），派生字段在合并后统一重算。
      口径调整后重测探针用 --lock-probe-only：只刷新写锁探针并合并，
      不因探针口径变化重跑分钟级的 pytest 耗时。
   7.5 护栏:
      吞吐 ≥ 300 用例/分钟（与实验 A 同一条护栏，4 worker 档位为判定锚点）。

运行方式兼容:
  同时支持 `py -m scripts.measure_baseline` 与 `py scripts/measure_baseline.py`；
  后者 sys.path 默认不含项目根，故先把项目根插入 sys.path 再导入 src.*
"""

import argparse
import json
import math
import os
import platform
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

# 项目根目录（scripts/ 的上一级）必须在 import src.* 之前确定并加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
  sys.path.insert(0, str(PROJECT_ROOT))

from src.common.source_ref import validate_source_ref  # noqa: E402
from src.core.case_manager import CaseManager  # noqa: E402
from src.db.db_session import DatabaseSession  # noqa: E402
from src.db.models import (  # noqa: E402
  DefectStatistic,
  TestCase,
  TestExecution,
  TestExecutionBatch,
)
from src.web import create_app  # noqa: E402

# ---------------------------------------------------------------------------
# 常量区（护栏 / 数据集标识 / 轮询预算）
# ---------------------------------------------------------------------------
OUTPUT_DIR_DEFAULT = PROJECT_ROOT / "output" / "baseline_day63"
DEV_DB_PATH = PROJECT_ROOT / "output" / "testmatrix.db"
MEASURE_DB_NAME = "measure_day63.db"

# 当日全量真实用例数（pytest --collect-only 口径，Day63 实测 1639）
TOTAL_PYTEST_TESTS = 1639

# 三项实验 A 护栏
THROUGHPUT_GUARDRAIL_PER_MIN = 300.0
OVERHEAD_RATIO_GUARDRAIL = 1.15
WRITE_P95_GUARDRAIL_MS = 200.0
# 实验 B 护栏（秒）：12 分钟
SYNTH_E2E_GUARDRAIL_SECONDS = 720.0

# 裸 pytest 直跑参数（与验收命令同口径：4 worker、静默、无 traceback）
BARE_PYTEST_ARGS = ("-q", "-n", "4", "--tb=no")

# 有界轮询：预算 + 间隔（间隔只决定查询频率，不参与判定）
PLATFORM_POLL_BUDGET_SECONDS = 1800.0
SYNTH_POLL_BUDGET_SECONDS = 1800.0
POLL_INTERVAL_SECONDS = 2.0

# 批次终态
TERMINAL_STATUSES = ("finished", "failed")

# 合成轻型用例集标识（module 是清理的唯一锚点）
SYNTH_MODULE = "synthetic_day63"
SYNTH_CASE_PREFIX = "TM-SYN-DAY63-"
SYNTH_SOURCE_REL = "output/baseline_day63/synthetic_day63_light.py"
SYNTH_SAMPLE_PRIORITY = "P0"
SYNTH_BULK_PRIORITY = "P3"

# 真实用例导入标识（实验 A 平台侧数据集）
REAL_MODULE = "baseline_day63_real"
REAL_MODULE_LINEAR = "baseline_day63_linear"

# 写接口 P95 测量标识
P95_MODULE = "baseline_day63_p95"
P95_CASE_PREFIX = "TM-P95-DAY63-"

# 写接口 P95 逐接口调用次数（任务书固定 100 次）
P95_ITERATIONS = 100

# 空载状态说明（写进报告，避免"批次执行中"与"空载"混谈）
WRITE_P95_SYSTEM_STATE = "空载（无任何批次在执行，队列/通知/缓存均关闭）"

# ---------------------------------------------------------------------------
# 并发曲线（Day64）
# ---------------------------------------------------------------------------
CURVE_OUTPUT_DIR_DEFAULT = PROJECT_ROOT / "output" / "baseline_day64"
CURVE_JSON_NAME = "concurrency_curve.json"
# 任务书固定的四个并发档位（逗号分隔命令行参数解析后仍按此顺序输出）
CURVE_LEVELS = (1, 2, 4, 8)
# 与实验 A1 同口径：静默 + 不带 traceback；-n 由档位拼接
CURVE_PYTEST_ARGS = ("-q", "--tb=no")
# 合成写锁探针：每线程写多少条、锁等待上限（与 Day60 测试环境取值一致）
LOCK_PROBE_WRITES_PER_THREAD = 25
LOCK_PROBE_BUSY_TIMEOUT_MS = 30000


# ---------------------------------------------------------------------------
# 通用工具：计时 / 百分位 / JSON 落盘
# ---------------------------------------------------------------------------
class Stopwatch:
  """毫秒级计时上下文管理器（perf_counter，单调时钟不受系统校时影响）"""

  def __init__(self) -> None:
    """初始化计时器（秒数在使用前恒为 0.0）"""
    self.seconds = 0.0
    self._started = 0.0

  def __enter__(self) -> "Stopwatch":
    """进入 with 块：记录起点"""
    self._started = time.perf_counter()
    return self

  def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
    """退出 with 块：结算耗时（异常路径同样结算，便于记录失败轮次）"""
    self.seconds = time.perf_counter() - self._started


def percentile(values: list[float], p: float) -> float:
  """
  计算最近秩法百分位

  参数:
    values (list[float]): 原始样本（不要求有序，内部排序）
    p (float): 百分位，0 < p <= 100

  返回:
    float: 第 p 百分位值；样本为空时返回 0.0

  异常:
    无
  """
  if not values:
    return 0.0
  ordered = sorted(values)
  rank = max(1, math.ceil(p / 100.0 * len(ordered)))
  return ordered[min(rank, len(ordered)) - 1]


def default_median(values: list[float]) -> float:
  """
  中位数（空样本返回 0.0，避免调用方各自判空）

  参数:
    values (list[float]): 原始样本

  返回:
    float: 中位数
  """
  return statistics.median(values) if values else 0.0


def now_iso() -> str:
  """当前本地时间的 ISO 字符串（秒精度，供 JSON 时间戳字段使用）"""
  return datetime.now().isoformat(timespec="seconds")


def dump_json(path: Path, payload: dict[str, Any]) -> None:
  """
  写 JSON 原始数据（含 datetime 兜底序列化）

  参数:
    path (Path): 目标文件路径（父目录自动创建）
    payload (dict[str, Any]): 待序列化数据

  返回:
    无

  异常:
    OSError: 磁盘不可写时抛出（测量数据是当日唯一产出，不允许静默丢弃）
  """
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, default=str),
    encoding="utf-8",
  )


def print_table(title: str, rows: list[tuple[str, str, str, str]]) -> None:
  """
  打印控制台摘要表（指标 / 护栏 / 实测 / 判定）

  参数:
    title (str): 表标题
    rows (list[tuple[str, str, str, str]]): 四列文本行

  返回:
    无
  """
  print(f"\n===== {title} =====")
  header = ("指标", "护栏", "实测", "判定")
  widths = [max(len(header[i]), *(len(row[i]) for row in rows)) for i in range(4)]
  line = " | ".join(header[i].ljust(widths[i]) for i in range(4))
  print(line)
  print("-" * len(line))
  for row in rows:
    print(" | ".join(row[i].ljust(widths[i]) for i in range(4)))


def verdict(passed: bool) -> str:
  """达标判定文本（统一口径，控制台只用 ASCII 方括号，避免 GBK 控制台编码崩溃）"""
  return "[达标]" if passed else "[未达标]"


# ---------------------------------------------------------------------------
# 数据库准备与清理
# ---------------------------------------------------------------------------
def prepare_measure_db(db_path: Path, rebuild: bool = True) -> dict[str, int]:
  """
  准备测量用数据库副本并切换环境变量

  为什么用副本而不是开发库: 测量要清空 test_executions / test_execution_batches
  / defect_statistics 三张表并批量造 1 万条用例，直接对开发库操作会把
  历史执行记录（看板演示数据、Day62 集成证据）一并抹掉，且一旦清理失败
  就是不可逆污染。副本方案下开发库全程只读。

  参数:
    db_path (Path): 测量库路径
    rebuild (bool): True 时从开发库重新复制（每轮完整测量独立起点）

  返回:
    dict[str, int]: 初始行数快照（test_cases/batches/executions/defects）

  异常:
    FileNotFoundError: 开发库不存在（未初始化）时抛出
  """
  if not DEV_DB_PATH.exists():
    raise FileNotFoundError(f"开发库不存在，无法准备测量副本: {DEV_DB_PATH}")
  db_path.parent.mkdir(parents=True, exist_ok=True)
  if rebuild and db_path.exists():
    db_path.unlink()
  if rebuild:
    shutil.copy2(DEV_DB_PATH, db_path)

  os.environ["TM_DB_TYPE"] = "sqlite"
  os.environ["TM_DB_SQLITE_PATH"] = str(db_path)
  # 大批量串行写入 + 计时口径：锁等待抬到 30s，避免偶发 5s 超时污染样本
  os.environ["TM_SQLITE_BUSY_TIMEOUT_MS"] = "30000"
  # 旁路能力一律关闭（外部网络/后台消费线程不得进入计时口径）
  os.environ["TM_REDIS_ENABLED"] = "false"
  os.environ["TM_TASK_QUEUE_ENABLED"] = "false"
  os.environ["TM_TASK_WORKER_ENABLED"] = "false"
  os.environ["TM_EMAIL_ENABLED"] = "false"
  os.environ["TM_WECHAT_ENABLED"] = "false"
  # 执行器默认值（.env 为 simulated；平台测量必须真实起 pytest 子进程）
  os.environ["TM_EXECUTOR"] = "pytest"

  DatabaseSession.reset()
  return snapshot_counts()


def snapshot_counts() -> dict[str, int]:
  """
  统计四张关键表行数（测量前后对齐用）

  参数:
    无

  返回:
    dict[str, int]: 表名到行数的映射
  """
  with DatabaseSession.session_scope() as session:
    return {
      "test_cases": session.query(TestCase).count(),
      "test_execution_batches": session.query(TestExecutionBatch).count(),
      "test_executions": session.query(TestExecution).count(),
      "defect_statistics": session.query(DefectStatistic).count(),
    }


def reset_execution_tables() -> dict[str, int]:
  """
  清空执行记录三表（test_executions / defect_statistics / test_execution_batches）

  参数:
    无

  返回:
    dict[str, int]: 清理前各表行数（写进 JSON，作为"本轮起点干净"的证据）

  异常:
    SQLAlchemyError: 数据库异常时上抛（清理失败即测量无效，不能静默继续）
  """
  with DatabaseSession.session_scope() as session:
    before = {
      "test_executions": session.query(TestExecution).count(),
      "defect_statistics": session.query(DefectStatistic).count(),
      "test_execution_batches": session.query(TestExecutionBatch).count(),
    }
    session.query(TestExecution).delete()
    session.query(DefectStatistic).delete()
    session.query(TestExecutionBatch).delete()
  return before


def delete_cases_by_module(module: str) -> int:
  """
  删除指定 module 的全部用例（测量造数的唯一清理锚点）

  参数:
    module (str): 用例 module 标识

  返回:
    int: 实际删除行数
  """
  with DatabaseSession.session_scope() as session:
    deleted = (
      session.query(TestCase)
      .filter(TestCase.module == module)
      .delete(synchronize_session=False)
    )
  return int(deleted or 0)


def create_batch_row(execution_id: str, total_cases: int) -> None:
  """
  直接写入批次元信息行（仅用于写接口 P95 测量的准备阶段，不计时）

  为什么不用 CaseManager.start_execution: 它带用例筛选，会把 module 下
  全部用例塞进同一批次；P95 需要的是 100 个各自只含 1 条明细的独立批次。

  参数:
    execution_id (str): 批次号
    total_cases (int): 批次用例数

  返回:
    无
  """
  with DatabaseSession.session_scope() as session:
    session.add(
      TestExecutionBatch(
        execution_id=execution_id,
        trigger="cli",
        executor="day63-measure",
        environment="measure-d63",
        remark="Day63 写接口 P95 测量用批次",
        status="pending",
        total_cases=total_cases,
      )
    )


# ---------------------------------------------------------------------------
# 实验 A-1：裸 pytest 直跑基线
# ---------------------------------------------------------------------------
def _bare_pytest_env() -> dict[str, str]:
  """
  构造裸 pytest 子进程环境（剔除测量库相关变量）

  参数:
    无

  返回:
    dict[str, str]: 环境变量副本

  说明:
    tests/conftest.py 会自行注入会话级临时库，子进程不得继承测量库路径，
    否则一旦用例泄漏写库就可能落进测量副本，反过来污染计时数据。
  """
  child_env = dict(os.environ)
  for key in ("TM_DB_SQLITE_PATH", "TM_DB_TYPE", "TM_SQLITE_BUSY_TIMEOUT_MS"):
    child_env.pop(key, None)
  return child_env


def measure_bare_pytest(runs: int) -> dict[str, Any]:
  """
  测量裸 pytest 全量直跑耗时（实验 A 基线 B）

  参数:
    runs (int): 重复次数（取中位）

  返回:
    dict[str, Any]: {"runs": [...], "median_seconds": float,
             "passed": int, "collected": int}

  异常:
    无（子进程失败只记录在返回值里，由调用方判定可信度）
  """
  runs_data: list[dict[str, Any]] = []
  counts: list[int] = []
  for index in range(runs):
    # --clean-alluredir 在 Windows 上遇历史残留目录会 INTERNALERROR（WinError 145）
    shutil.rmtree(PROJECT_ROOT / "output" / "allure_results", ignore_errors=True)
    command = [sys.executable, "-m", "pytest", *BARE_PYTEST_ARGS]
    with Stopwatch() as sw:
      completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_bare_pytest_env(),
        check=False,
      )
    passed = _parse_pytest_passed(completed.stdout)
    counts.append(passed)
    runs_data.append(
      {
        "run": index + 1,
        "seconds": round(sw.seconds, 3),
        "returncode": completed.returncode,
        "passed": passed,
      }
    )
    print(
      f"  [A1] 第 {index + 1}/{runs} 轮裸 pytest: {sw.seconds:.2f}s | "
      f"passed={passed} | 退出码={completed.returncode}"
    )
  return {
    "runs": runs_data,
    "median_seconds": round(default_median([r["seconds"] for r in runs_data]), 3),
    "passed": int(default_median([float(c) for c in counts])),
    "collected": TOTAL_PYTEST_TESTS,
  }


def _parse_pytest_passed(stdout: str) -> int:
  """
  从 pytest 尾部摘要解析通过条数

  参数:
    stdout (str): pytest 标准输出

  返回:
    int: 通过条数；解析不到时返回 0
  """
  matches = re.findall(r"(\d+) passed", stdout)
  return int(matches[-1]) if matches else 0


# ---------------------------------------------------------------------------
# 实验 A-2：平台批量执行（真实路由 + 真实后台编排）
# ---------------------------------------------------------------------------
def collect_real_nodeids(limit: int) -> list[str]:
  """
  收集真实用例 nodeid（作为平台侧"同数据集"来源）

  参数:
    limit (int): 需要的条数上限

  返回:
    list[str]: 可作为 source_ref 的 nodeid 列表（等步长抽取，跨模块均匀分布）

  异常:
    RuntimeError: 收集失败或可用 nodeid 不足时抛出（数据集不足会让
          测量失去意义，宁可失败也不缩水凑数）

  说明:
    参数化用例的 nodeid 形如 `...test_x[a-b]`，含 `[` 与 `-`，
    过不了 source_ref 校验（只允许 \\w 段），故按同一把尺子过滤，
    与平台"可执行用例"的口径一致（Day63 实测 1639 → 1201 可执行形态）。

    为什么等步长抽取而不是取前 N 条: pytest 收集顺序按目录/文件排列，
    前 N 条会全落在 `tests/api_demo/` 一个目录里，样本代表性被目录绑定；
    等步长抽样让样本覆盖全部模块，且抽样规则固定可复现（无随机种子）。
  """
  command = [
    sys.executable,
    "-m",
    "pytest",
    "--collect-only",
    "-q",
    "-o",
    "addopts=",
  ]
  completed = subprocess.run(
    command,
    cwd=str(PROJECT_ROOT),
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="replace",
    check=False,
  )
  candidates: list[str] = []
  for raw in completed.stdout.splitlines():
    candidate = raw.strip()
    if "::" not in candidate:
      continue
    try:
      normalized = validate_source_ref(candidate)
    except ValueError:
      continue
    if normalized and normalized not in candidates:
      candidates.append(normalized)
  if len(candidates) < limit:
    raise RuntimeError(
      f"可执行 nodeid 不足: 需要 {limit} 条，实际收集到 {len(candidates)} 条"
    )
  if len(candidates) == limit:
    return candidates
  # 等步长抽样：步长向下取整，确保抽出的下表严格递增且首条必取
  stride = len(candidates) // limit
  return [candidates[index * stride] for index in range(limit)]


def import_real_cases(nodeids: list[str], module: str, case_prefix: str) -> dict[str, Any]:
  """
  把真实 nodeid 导入平台用例表（平台侧"同数据集"）

  参数:
    nodeids (list[str]): 真实用例 nodeid 列表
    module (str): 承载模块名（便于按 module 筛选触发与清理）
    case_prefix (str): 用例编号前缀（样本组与线性复核组必须互不冲突）

  返回:
    dict[str, Any]: {"count", "seconds", "per_case_ms"}
  """
  with Stopwatch() as sw:
    for index, nodeid in enumerate(nodeids, start=1):
      CaseManager.create_case(
        {
          "case_id": f"{case_prefix}{index:04d}",
          "name": f"真实用例-D63测量用-{index:04d}",
          "module": module,
          "priority": "P2",
          "case_type": "api",
          "status": "active",
          "description": "Day63 实验A 平台侧数据集（真实 pytest nodeid）",
          "source_ref": nodeid,
          "creator": "day63-measure",
        }
      )
  return {
    "count": len(nodeids),
    "seconds": round(sw.seconds, 3),
    "per_case_ms": round(sw.seconds / max(len(nodeids), 1) * 1000.0, 3),
  }


def _wait_batch_terminal(
  client: Any, execution_id: str, budget_seconds: float
) -> tuple[str, dict[str, Any], bool]:
  """
  有界轮询批次终态

  参数:
    client (Any): Flask test_client（真实路由，不绕过平台层）
    execution_id (str): 批次号
    budget_seconds (float): 等待预算上限（超时即返回，不无限等）

  返回:
    tuple[str, dict[str, Any], bool]: (最后状态, 状态载荷, 是否在预算内到达终态)
  """
  deadline = time.perf_counter() + budget_seconds
  status = "pending"
  payload: dict[str, Any] = {}
  while time.perf_counter() < deadline:
    response = client.get(f"/api/executions/{execution_id}/status")
    body = response.get_json() or {}
    payload = body.get("data") or {}
    status = str(payload.get("status") or "unknown")
    if status in TERMINAL_STATUSES:
      return status, payload, True
    time.sleep(POLL_INTERVAL_SECONDS)
  return status, payload, False


def trigger_and_wait(
  client: Any, body: dict[str, Any], budget_seconds: float
) -> dict[str, Any]:
  """
  触发一次批次执行并计时到终态

  参数:
    client (Any): Flask test_client
    body (dict[str, Any]): 触发请求体（筛选条件 + executor）
    budget_seconds (float): 轮询预算上限

  返回:
    dict[str, Any]: {"execution_id", "seconds", "status", "finished_in_budget",
             "total_cases", "passed", "failed", "error", "skipped"}

  异常:
    RuntimeError: 触发接口未返回 202/批次号时抛出（测量前置条件不成立）
  """
  with Stopwatch() as sw:
    response = client.post("/api/executions/trigger", json=body)
    if response.status_code != 202:
      raise RuntimeError(
        f"触发失败: HTTP {response.status_code} | {response.get_data(as_text=True)[:300]}"
      )
    data = (response.get_json() or {}).get("data") or {}
    execution_id = str(data.get("execution_id") or "")
    if not execution_id:
      raise RuntimeError("触发接口未返回批次号")
    status, payload, in_budget = _wait_batch_terminal(client, execution_id, budget_seconds)
  return {
    "execution_id": execution_id,
    "seconds": round(sw.seconds, 3),
    "status": status,
    "finished_in_budget": in_budget,
    "total_cases": int(payload.get("total_cases") or 0),
    "passed": int(payload.get("passed") or 0),
    "failed": int(payload.get("failed") or 0),
    "error": int(payload.get("error") or 0),
    "skipped": int(payload.get("skipped") or 0),
  }


def batch_case_stats(execution_id: str) -> dict[str, Any]:
  """
  汇总某批次的逐条明细耗时（用于验证"单条成本恒定"这一外推前提）

  参数:
    execution_id (str): 批次号

  返回:
    dict[str, Any]: {"rows", "duration_median", "duration_p95",
             "duration_min", "duration_max"}
  """
  with DatabaseSession.session_scope() as session:
    rows = (
      session.query(TestExecution.duration)
      .filter(TestExecution.execution_id == execution_id)
      .all()
    )
  durations = [float(row[0] or 0.0) for row in rows]
  return {
    "rows": len(durations),
    "duration_median": round(default_median(durations), 3),
    "duration_p95": round(percentile(durations, 95), 3),
    "duration_min": round(min(durations), 3) if durations else 0.0,
    "duration_max": round(max(durations), 3) if durations else 0.0,
  }


def measure_platform_execution(
  client: Any,
  module: str,
  expected_cases: int,
  runs: int,
) -> dict[str, Any]:
  """
  实验 A 平台侧计时（每轮前清空执行记录，保证轮次独立）

  参数:
    client (Any): Flask test_client
    module (str): 数据集的 module 锚点
    expected_cases (int): 该 module 下的用例数（校验批次 total 是否一致）
    runs (int): 重复次数

  返回:
    dict[str, Any]: {"runs": [...], "median_seconds", "cases_per_run",
             "per_case_seconds", "case_stats"}

  异常:
    RuntimeError: 批次用例数与预期不一致 / 未在预算内到达终态时抛出
  """
  body = {
    "module": module,
    "case_type": "api",
    "executor": "pytest",
    "environment": "measure-d63",
    "remark": "Day63 实验A 平台批量执行测量",
  }
  runs_data: list[dict[str, Any]] = []
  for index in range(runs):
    cleared = reset_execution_tables()
    result = trigger_and_wait(client, body, PLATFORM_POLL_BUDGET_SECONDS)
    stats = batch_case_stats(result["execution_id"])
    runs_data.append(
      {
        "run": index + 1,
        "seconds": result["seconds"],
        "cases": result["total_cases"],
        "status": result["status"],
        "finished_in_budget": result["finished_in_budget"],
        "cleared_before": cleared,
        "case_stats": stats,
        "result_counts": {
          "passed": result["passed"],
          "failed": result["failed"],
          "error": result["error"],
          "skipped": result["skipped"],
        },
      }
    )
    print(
      f"  [A2] 第 {index + 1}/{runs} 轮平台执行: {result['seconds']:.2f}s | "
      f"用例={result['total_cases']} | 状态={result['status']} | "
      f"单条中位={stats['duration_median']}s"
    )
    if result["total_cases"] != expected_cases:
      raise RuntimeError(
        f"批次用例数与数据集不一致: 预期 {expected_cases}，实际 {result['total_cases']}"
      )
    if not result["finished_in_budget"]:
      raise RuntimeError(
        f"批次未在 {PLATFORM_POLL_BUDGET_SECONDS}s 内到达终态: {result['execution_id']}"
      )
  seconds = [float(row["seconds"]) for row in runs_data]
  median_seconds = default_median(seconds)
  return {
    "runs": runs_data,
    "median_seconds": round(median_seconds, 3),
    "cases_per_run": expected_cases,
    "per_case_seconds": round(median_seconds / max(expected_cases, 1), 4),
    "case_stats": runs_data[-1]["case_stats"] if runs_data else {},
  }


# ---------------------------------------------------------------------------
# 实验 A-3：核心写接口 P95
# ---------------------------------------------------------------------------
def measure_write_p95(iterations: int = P95_ITERATIONS) -> dict[str, Any]:
  """
  测量三个核心写接口的单次延迟分布（空载状态）

  参数:
    iterations (int): 每个接口的调用次数，默认 100

  返回:
    dict[str, Any]: {"system_state", "iterations", "interfaces": {
             name: {"samples_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms"}}}

  异常:
    CaseManagerError: 底层写库异常（测量环境异常时必须响亮失败）
  """
  create_samples: list[float] = []
  record_samples: list[float] = []
  finish_samples: list[float] = []

  # 1) create_case：独立用例，测后统一按 module 清理
  case_ids: list[str] = []
  for index in range(iterations):
    case_id = f"{P95_CASE_PREFIX}{index:04d}"
    case_ids.append(case_id)
    with Stopwatch() as sw:
      CaseManager.create_case(
        {
          "case_id": case_id,
          "name": f"P95测量用例-{index:04d}",
          "module": P95_MODULE,
          "priority": "P2",
          "case_type": "api",
          "status": "active",
          "description": "Day63 写接口 P95 测量用",
          "creator": "day63-measure",
        }
      )
    create_samples.append(sw.seconds * 1000.0)

  # 2) record_execution + finish_execution：每个批次一条明细
  for index in range(iterations):
    execution_id = f"D63-P95-{index:04d}"
    create_batch_row(execution_id, 1)
    started = datetime.now()
    with Stopwatch() as sw:
      CaseManager.record_execution(
        execution_id=execution_id,
        case_id=case_ids[index],
        case_name=f"P95测量用例-{index:04d}",
        result="passed",
        start_time=started,
        end_time=datetime.now(),
        duration=0.01,
      )
    record_samples.append(sw.seconds * 1000.0)
    with Stopwatch() as sw:
      CaseManager.finish_execution(execution_id)
    finish_samples.append(sw.seconds * 1000.0)

  interfaces = {
    "create_case": create_samples,
    "record_execution": record_samples,
    "finish_execution": finish_samples,
  }
  summary: dict[str, Any] = {}
  for name, samples in interfaces.items():
    summary[name] = {
      "samples_ms": [round(value, 3) for value in samples],
      "p50_ms": round(percentile(samples, 50), 3),
      "p95_ms": round(percentile(samples, 95), 3),
      "p99_ms": round(percentile(samples, 99), 3),
      "max_ms": round(max(samples), 3),
    }
    print(
      f"  [A3] {name}: P50={summary[name]['p50_ms']}ms | "
      f"P95={summary[name]['p95_ms']}ms | P99={summary[name]['p99_ms']}ms"
    )
  return {
    "system_state": WRITE_P95_SYSTEM_STATE,
    "iterations": iterations,
    "interfaces": summary,
  }


# ---------------------------------------------------------------------------
# 实验 A 编排
# ---------------------------------------------------------------------------
def run_experiment_a(app: Any, args: argparse.Namespace) -> dict[str, Any]:
  """
  执行实验 A 全流程（裸 pytest → 平台执行 → 写接口 P95 → 护栏判定）

  参数:
    app (Any): Flask 应用实例
    args (argparse.Namespace): 命令行参数

  返回:
    dict[str, Any]: 原始数据 + 护栏判定（直接写入 JSON）

  异常:
    RuntimeError: 数据集准备或批次执行异常时抛出（失败轮询等）
  """
  print("\n[实验 A] 当日全量真实集成用例集北极星测量")
  nodeids = collect_real_nodeids(args.platform_sample)
  print(f"  [A0] 可执行真实 nodeid 采集完成: {len(nodeids)} 条")

  payload: dict[str, Any] = {
    "experiment": "A",
    "started_at": now_iso(),
    "dataset": {
      "total_pytest_tests": TOTAL_PYTEST_TESTS,
      "platform_sample_cases": len(nodeids),
      "platform_sample_source": "真实 pytest nodeid（非参数化，可作 source_ref）",
      "bare_pytest_command": " ".join([".venv/Scripts/python.exe", "-m", "pytest",
                                       *BARE_PYTEST_ARGS]),
      "protocol_note": "平台侧为同数据集前缀样本 + 线性外推，理由见脚本 docstring 第 6 节",
    },
  }

  try:
    print("  [A1] 裸 pytest 直跑基线 …")
    payload["bare_pytest"] = measure_bare_pytest(args.runs)

    print("  [A2] 平台批量执行（真实路由 + 真实后台编排）…")
    payload["platform_import"] = import_real_cases(nodeids, REAL_MODULE, "TM-D63A-")
    with app.test_client() as client:
      platform = measure_platform_execution(client, REAL_MODULE, len(nodeids), args.runs)
    payload["platform"] = platform

    if args.linear_check:
      print(f"  [A2b] 线性度复核（{len(nodeids) * 2} 条样本，单轮）…")
      linear_ids = collect_real_nodeids(len(nodeids) * 2)
      payload["linear_check_import"] = import_real_cases(
        linear_ids, REAL_MODULE_LINEAR, "TM-D63L-"
      )
      with app.test_client() as client:
        payload["linear_check"] = measure_platform_execution(
          client, REAL_MODULE_LINEAR, len(linear_ids), 1
        )

    print("  [A3] 核心写接口 P95（空载，各 100 次）…")
    payload["write_p95"] = measure_write_p95()

    payload["guards"] = _judge_experiment_a(payload, args.platform_sample)
    payload["finished_at"] = now_iso()
    return payload
  finally:
    # 造数一律清理：用例 + 执行记录，行数快照落进 JSON 便于复核
    deleted = {
      REAL_MODULE: delete_cases_by_module(REAL_MODULE),
      REAL_MODULE_LINEAR: delete_cases_by_module(REAL_MODULE_LINEAR),
      P95_MODULE: delete_cases_by_module(P95_MODULE),
    }
    reset_execution_tables()
    payload["cleanup"] = {"deleted_cases": deleted, "after": snapshot_counts()}


def _judge_experiment_a(payload: dict[str, Any], sample_cases: int) -> dict[str, Any]:
  """
  计算实验 A 三项护栏、靶值与回归报警线

  参数:
    payload (dict[str, Any]): 已采集的原始数据
    sample_cases (int): 平台侧样本用例数

  返回:
    dict[str, Any]: 每项指标的实测值、护栏、判定与外推说明
  """
  bare_median = float(payload["bare_pytest"]["median_seconds"])
  platform_median = float(payload["platform"]["median_seconds"])
  per_case = float(payload["platform"]["per_case_seconds"])

  # 护栏一：4 worker 吞吐（裸 pytest 口径，全量实测，无外推）
  bare_throughput = TOTAL_PYTEST_TESTS / (bare_median / 60.0) if bare_median else 0.0
  # 平台侧：实测样本吞吐 + 按单条成本外推的全量吞吐
  sample_throughput = sample_cases / (platform_median / 60.0) if platform_median else 0.0
  projected_seconds = per_case * TOTAL_PYTEST_TESTS
  projected_throughput = (
    TOTAL_PYTEST_TESTS / (projected_seconds / 60.0) if projected_seconds else 0.0
  )
  # 护栏二：平台开销比（平台全量外推 / 裸 pytest 实测中位）
  overhead_ratio = projected_seconds / bare_median if bare_median else 0.0
  # 靶值 / 回归报警线（改进规则：靶值 = max(护栏, ⌈B×1.25⌉)，报警线 = B×0.8）
  target_seconds = max(OVERHEAD_RATIO_GUARDRAIL * bare_median, math.ceil(bare_median * 1.25))
  alarm_seconds = bare_median * 0.8

  write_p95 = payload["write_p95"]["interfaces"]
  return {
    "bare_pytest_median_seconds": round(bare_median, 3),
    "throughput_bare_per_min": {
      "value": round(bare_throughput, 1),
      "guardrail": THROUGHPUT_GUARDRAIL_PER_MIN,
      "passed": bare_throughput >= THROUGHPUT_GUARDRAIL_PER_MIN,
    },
    "throughput_platform_sample_per_min": {
      "value": round(sample_throughput, 1),
      "sample_cases": sample_cases,
      "guardrail": THROUGHPUT_GUARDRAIL_PER_MIN,
      "passed": sample_throughput >= THROUGHPUT_GUARDRAIL_PER_MIN,
    },
    "throughput_platform_projected_per_min": {
      "value": round(projected_throughput, 1),
      "projected_seconds_for_full_set": round(projected_seconds, 1),
      "guardrail": THROUGHPUT_GUARDRAIL_PER_MIN,
      "passed": projected_throughput >= THROUGHPUT_GUARDRAIL_PER_MIN,
    },
    "platform_overhead_ratio": {
      "value": round(overhead_ratio, 3),
      "guardrail": OVERHEAD_RATIO_GUARDRAIL,
      "passed": overhead_ratio <= OVERHEAD_RATIO_GUARDRAIL,
      "per_case_seconds": per_case,
    },
    "write_p95": {
      name: {
        "value_ms": write_p95[name]["p95_ms"],
        "guardrail_ms": WRITE_P95_GUARDRAIL_MS,
        "passed": write_p95[name]["p95_ms"] <= WRITE_P95_GUARDRAIL_MS,
      }
      for name in ("create_case", "record_execution", "finish_execution")
    },
    "improvement_rule": {
      "baseline_b_seconds": round(bare_median, 3),
      "target_seconds": round(target_seconds, 3),
      "alarm_seconds": round(alarm_seconds, 3),
      "note": "靶值 = max(护栏比×B, ceil(B×1.25))；报警线 = B×0.8，低于即异常",
    },
  }


# ---------------------------------------------------------------------------
# 实验 B：1 万条合成轻量用例集
# ---------------------------------------------------------------------------
def write_synthetic_source() -> Path:
  """
  生成合成用例共用的轻量执行目标文件（assert True，无网络/无 IO）

  参数:
    无

  返回:
    Path: 相对项目根的路径（与 source_ref 一致）
  """
  target = PROJECT_ROOT / SYNTH_SOURCE_REL
  target.parent.mkdir(parents=True, exist_ok=True)
  target.write_text(
    '"""Day63 实验B 合成轻量用例共用执行目标（assert True，无外部依赖）。"""\n'
    "\n"
    "\n"
    "def test_synthetic_light():\n"
    "  \"\"\"合成轻量用例：仅断言 True，不触网、不落盘、不连库\"\"\"\n"
    "  assert True\n",
    encoding="utf-8",
  )
  return Path(SYNTH_SOURCE_REL)


def generate_synthetic_cases(count: int, sample_count: int) -> list[dict[str, Any]]:
  """
  生成合成轻量用例数据集（纯数据，不落库）

  参数:
    count (int): 总条数（实验 B 固定 10000）
    sample_count (int): 其中打 P0 优先级的样本条数（0 表示全量同优先级）

  返回:
    list[dict[str, Any]]: 用例字典列表，供 import_synthetic_cases 落库
  """
  cases: list[dict[str, Any]] = []
  for index in range(1, count + 1):
    priority = (
      SYNTH_SAMPLE_PRIORITY if 0 < sample_count and index <= sample_count
      else SYNTH_BULK_PRIORITY
    )
    cases.append(
      {
        "case_id": f"{SYNTH_CASE_PREFIX}{index:05d}",
        "name": f"合成轻量用例-D63测量用-{index:05d}",
        "module": SYNTH_MODULE,
        "priority": priority,
        "case_type": "api",
        "status": "active",
        "description": "合成轻量用例-D63测量用（assert True，无网络/无文件IO/无数据库依赖）",
        "source_ref": SYNTH_SOURCE_REL,
        "creator": "day63-measure",
      }
    )
  return cases


def import_synthetic_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
  """
  批量导入合成用例（走 CaseManager.create_case 真实入库路径）

  参数:
    cases (list[dict[str, Any]]): generate_synthetic_cases 的产物

  返回:
    dict[str, Any]: {"count", "seconds", "per_case_ms", "cases_before",
             "cases_after"}

  异常:
    CaseManagerError: 入库异常时上抛（造数失败即测量无效）
  """
  before = snapshot_counts()["test_cases"]
  with Stopwatch() as sw:
    for case in cases:
      CaseManager.create_case(case)
  after = snapshot_counts()["test_cases"]
  return {
    "count": len(cases),
    "seconds": round(sw.seconds, 3),
    "per_case_ms": round(sw.seconds / max(len(cases), 1) * 1000.0, 3),
    "cases_before": before,
    "cases_after": after,
    "rows_added": after - before,
  }


def measure_synthetic_end_to_end(
  client: Any, total_cases: int, sample_cases: int, runs: int
) -> dict[str, Any]:
  """
  实验 B 端到端计时（触发 → 有界轮询终态）

  参数:
    client (Any): Flask test_client
    total_cases (int): 数据集总条数（10000）
    sample_cases (int): 实测样本条数（0 表示跑全量）
    runs (int): 重复次数

  返回:
    dict[str, Any]: {"runs": [...], "median_seconds", "per_case_seconds",
             "projected_full_seconds", "case_stats"}

  异常:
    RuntimeError: 未在预算内到达终态时抛出
  """
  body: dict[str, Any] = {
    "module": SYNTH_MODULE,
    "case_type": "api",
    "executor": "pytest",
    "environment": "measure-d63",
    "remark": "Day63 实验B 合成轻量用例集测量",
  }
  if sample_cases > 0:
    body["priority"] = SYNTH_SAMPLE_PRIORITY
  expected = sample_cases if sample_cases > 0 else total_cases

  runs_data: list[dict[str, Any]] = []
  for index in range(runs):
    cleared = reset_execution_tables()
    result = trigger_and_wait(client, body, SYNTH_POLL_BUDGET_SECONDS)
    stats = batch_case_stats(result["execution_id"])
    runs_data.append(
      {
        "run": index + 1,
        "seconds": result["seconds"],
        "cases": result["total_cases"],
        "status": result["status"],
        "finished_in_budget": result["finished_in_budget"],
        "cleared_before": cleared,
        "case_stats": stats,
      }
    )
    print(
      f"  [B2] 第 {index + 1}/{runs} 轮端到端: {result['seconds']:.2f}s | "
      f"用例={result['total_cases']} | 状态={result['status']} | "
      f"单条中位={stats['duration_median']}s"
    )
    if result["total_cases"] != expected:
      raise RuntimeError(
        f"批次用例数与样本不一致: 预期 {expected}，实际 {result['total_cases']}"
      )
    if not result["finished_in_budget"]:
      raise RuntimeError(
        f"批次未在 {SYNTH_POLL_BUDGET_SECONDS}s 内到达终态: {result['execution_id']}"
      )

  seconds = [float(row["seconds"]) for row in runs_data]
  median_seconds = default_median(seconds)
  measured_cases = expected
  per_case = median_seconds / max(measured_cases, 1)
  return {
    "runs": runs_data,
    "median_seconds": round(median_seconds, 3),
    "measured_cases": measured_cases,
    "per_case_seconds": round(per_case, 4),
    "projected_full_seconds": round(per_case * total_cases, 1),
    "case_stats": runs_data[-1]["case_stats"] if runs_data else {},
  }


def cleanup_synthetic_cases(baseline_cases: int) -> dict[str, Any]:
  """
  清理合成用例及其执行记录，并校验行数恢复

  参数:
    baseline_cases (int): 测量前 test_cases 行数（恢复目标）

  返回:
    dict[str, Any]: {"deleted_cases", "remaining_synth", "cases_after",
             "restored"}
  """
  deleted = delete_cases_by_module(SYNTH_MODULE)
  reset_execution_tables()
  with DatabaseSession.session_scope() as session:
    remaining = (
      session.query(TestCase).filter(TestCase.module == SYNTH_MODULE).count()
    )
  after = snapshot_counts()["test_cases"]
  return {
    "deleted_cases": deleted,
    "remaining_synth": remaining,
    "cases_after": after,
    "baseline_cases": baseline_cases,
    "restored": remaining == 0 and after == baseline_cases,
  }


def run_experiment_b(app: Any, args: argparse.Namespace) -> dict[str, Any]:
  """
  执行实验 B 全流程（生成 → 导入 → 端到端 → 清理 → 护栏判定）

  参数:
    app (Any): Flask 应用实例
    args (argparse.Namespace): 命令行参数

  返回:
    dict[str, Any]: 原始数据 + 护栏判定

  异常:
    RuntimeError: 造数行数不符或批次执行异常时抛出
  """
  print("\n[实验 B] 1 万条合成轻量用例集北极星测量")
  baseline = snapshot_counts()
  source_rel = write_synthetic_source()
  cases = generate_synthetic_cases(args.synth_total, args.synth_sample)

  payload: dict[str, Any] = {
    "experiment": "B",
    "started_at": now_iso(),
    "dataset": {
      "total_cases": args.synth_total,
      "module": SYNTH_MODULE,
      "case_id_prefix": SYNTH_CASE_PREFIX,
      "source_ref": str(source_rel),
      "composition": "mock 100%（assert True 轻量用例，无真实网络/无文件IO/无数据库依赖）",
      "sample_cases": args.synth_sample,
      "cases_before": baseline["test_cases"],
      "protocol_note": "样本与全量形状一致（同一执行目标、同一 module），样本走 P0 优先级筛选",
    },
  }

  payload["cleanup"] = {
    "deleted_cases": 0,
    "remaining_synth": 0,
    "cases_after": baseline["test_cases"],
    "baseline_cases": baseline["test_cases"],
    "restored": True,
  }
  try:
    print(f"  [B1] 导入 {args.synth_total} 条合成用例 …")
    payload["import"] = import_synthetic_cases(cases)
    if payload["import"]["rows_added"] != args.synth_total:
      raise RuntimeError(
        f"导入行数不符: 预期 +{args.synth_total}，实际 +{payload['import']['rows_added']}"
      )

    print("  [B2] 端到端执行（真实路由 + 真实 pytest 子进程）…")
    with app.test_client() as client:
      payload["end_to_end"] = measure_synthetic_end_to_end(
        client, args.synth_total, args.synth_sample, args.runs
      )

    payload["guards"] = _judge_experiment_b(payload)
    payload["finished_at"] = now_iso()
    return payload
  finally:
    cleanup = cleanup_synthetic_cases(baseline["test_cases"])
    if "cleanup" in payload and payload["cleanup"]["deleted_cases"] == 0:
      payload["cleanup"] = cleanup
    print(
      f"  [B3] 清理: 删除 {cleanup['deleted_cases']} 条 | "
      f"残留 {cleanup['remaining_synth']} 条 | "
      f"test_cases 恢复到 {cleanup['cases_after']} 行（原 {cleanup['baseline_cases']}）"
    )


def _judge_experiment_b(payload: dict[str, Any]) -> dict[str, Any]:
  """
  计算实验 B 护栏判定（实测样本 + 全量外推）

  参数:
    payload (dict[str, Any]): 已采集的原始数据

  返回:
    dict[str, Any]: 护栏对比结果
  """
  end_to_end = payload["end_to_end"]
  measured = float(end_to_end["median_seconds"])
  projected = float(end_to_end["projected_full_seconds"])
  return {
    "e2e_measured_sample_seconds": {
      "value": round(measured, 3),
      "cases": end_to_end["measured_cases"],
      "guardrail_seconds": SYNTH_E2E_GUARDRAIL_SECONDS,
      "passed": measured <= SYNTH_E2E_GUARDRAIL_SECONDS,
    },
    "e2e_projected_full_seconds": {
      "value": projected,
      "cases": payload["dataset"]["total_cases"],
      "guardrail_seconds": SYNTH_E2E_GUARDRAIL_SECONDS,
      "passed": projected <= SYNTH_E2E_GUARDRAIL_SECONDS,
    },
    "composition_declared": payload["dataset"]["composition"],
    "case_shape_constant": end_to_end["case_stats"],
  }


# ---------------------------------------------------------------------------
# Day64 并发曲线：xdist worker 档位 × 耗时 / 吞吐 / 加速比 + 合成写锁探针
# ---------------------------------------------------------------------------
def _parse_concurrency_levels(raw: str) -> list[int]:
  """
  解析逗号分隔的并发档位

  参数:
    raw (str): 形如 "1,2,4,8" 的文本

  返回:
    list[int]: 去重并升序排列的正整数档位

  异常:
    ValueError: 存在非整数或小于 1 的档位（口径错误直接失败，不静默跳过）
  """
  levels: list[int] = []
  for chunk in raw.split(","):
    text = chunk.strip()
    if not text:
      continue
    try:
      value = int(text)
    except ValueError as exc:
      raise ValueError(f"并发档位必须是整数，收到 {text!r}") from exc
    if value < 1:
      raise ValueError(f"并发档位必须 >= 1，收到 {value}")
    if value not in levels:
      levels.append(value)
  if not levels:
    raise ValueError("并发档位不能为空")
  return sorted(levels)


def _parse_pytest_warnings(stdout: str) -> int:
  """
  从 pytest 尾部摘要解析告警条数

  参数:
    stdout (str): pytest 标准输出

  返回:
    int: 告警条数；摘要不含告警时返回 0
  """
  matches = re.findall(r"(\d+) warnings?", stdout)
  return int(matches[-1]) if matches else 0


def measure_pytest_concurrency(workers: int, runs: int) -> dict[str, Any]:
  """
  测量指定 xdist 并发档位下的全量 pytest 耗时

  参数:
    workers (int): xdist worker 数（-n 参数）
    runs (int): 重复次数（取中位）

  返回:
    dict[str, Any]: {"workers", "runs", "median_seconds", "passed", "warnings"}

  异常:
    无（子进程失败只记录在返回值里，由调用方判定可信度）
  """
  command = [sys.executable, "-m", "pytest", *CURVE_PYTEST_ARGS, "-n", str(workers)]
  runs_data: list[dict[str, Any]] = []
  for index in range(runs):
    # 与实验 A1 同因：--clean-alluredir 在 Windows 遇残留目录会 INTERNALERROR
    shutil.rmtree(PROJECT_ROOT / "output" / "allure_results", ignore_errors=True)
    with Stopwatch() as sw:
      completed = subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_bare_pytest_env(),
        check=False,
      )
    passed = _parse_pytest_passed(completed.stdout)
    warnings = _parse_pytest_warnings(completed.stdout)
    runs_data.append(
      {
        "run": index + 1,
        "seconds": round(sw.seconds, 3),
        "returncode": completed.returncode,
        "passed": passed,
        "warnings": warnings,
      }
    )
    print(
      f"  [曲线] n={workers} 第 {index + 1}/{runs} 轮: {sw.seconds:.2f}s | "
      f"passed={passed} | warnings={warnings} | 退出码={completed.returncode}"
    )
  return {
    "workers": workers,
    "runs": runs_data,
    "median_seconds": round(default_median([r["seconds"] for r in runs_data]), 3),
    "passed": int(default_median([float(r["passed"]) for r in runs_data])),
    "warnings": max((r["warnings"] for r in runs_data), default=0),
  }


def _prepare_lock_probe_db(db_path: Path) -> None:
  """
  创建合成写锁探针库（单表 + (execution_id, case_id) 唯一索引）

  参数:
    db_path (Path): 探针库路径（存在即先删除，保证每档从零开始）

  返回:
    无

  异常:
    OSError: 目录不可写时抛出
    sqlite3.Error: 建表失败时抛出
  """
  if db_path.exists():
    db_path.unlink()
  connection = sqlite3.connect(str(db_path))
  try:
    connection.execute(f"PRAGMA busy_timeout = {LOCK_PROBE_BUSY_TIMEOUT_MS}")
    connection.execute(
      "CREATE TABLE probe ("
      "id INTEGER PRIMARY KEY AUTOINCREMENT, "
      "execution_id TEXT NOT NULL, "
      "case_id TEXT NOT NULL)"
    )
    connection.execute("CREATE UNIQUE INDEX uq_probe ON probe (execution_id, case_id)")
    connection.commit()
  finally:
    connection.close()


def _lock_probe_writer(
  db_path: Path,
  workers: int,
  thread_index: int,
  barrier: threading.Barrier,
  latencies: list[float],
  errors: list[str],
) -> None:
  """
  单个探针线程：对齐屏障后连写 N 条，逐条记录"取锁+写入+提交"耗时

  参数:
    db_path (Path): 探针库路径（各线程各持一条独立连接）
    workers (int): 档位（用于构造 execution_id，保证同档内唯一键不冲突）
    thread_index (int): 线程序号（case_id 前缀）
    barrier (threading.Barrier): 起始对齐屏障（禁止用 sleep 赌同时开跑）
    latencies (list[float]): 输出——每条写入的毫秒耗时
    errors (list[str]): 输出——SQLite 错误文本（超时/锁失败）

  返回:
    无
  """
  connection = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
  connection.execute(f"PRAGMA busy_timeout = {LOCK_PROBE_BUSY_TIMEOUT_MS}")
  try:
    barrier.wait()
    for index in range(LOCK_PROBE_WRITES_PER_THREAD):
      started = time.perf_counter()
      try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
          "INSERT INTO probe (execution_id, case_id) VALUES (?, ?)",
          (f"curve-n{workers}", f"{thread_index}-{index}"),
        )
        connection.execute("COMMIT")
      except sqlite3.Error as exc:
        # 失败不重试：探针要测的是"锁等待与失败形态"，重试会把结论洗平
        errors.append(f"{type(exc).__name__}: {exc}")
        try:
          connection.execute("ROLLBACK")
        except sqlite3.Error:
          pass
      finally:
        latencies.append((time.perf_counter() - started) * 1000.0)
  finally:
    connection.close()


def probe_sqlite_write_lock(workers: int, output_dir: Path) -> dict[str, Any]:
  """
  合成写锁探针单轮：N 线程并发写同一个 SQLite 文件，量化锁等待

  参数:
    workers (int): 线程数（与并发档位同值）
    output_dir (Path): 探针库所在目录（测完即删除）

  返回:
    dict[str, Any]: {"threads", "writes_per_thread", "total_writes",
             "p50_ms", "p95_ms", "p99_ms", "max_ms", "error_count"}

  异常:
    无（探针失败只体现在 error_count 与空样本上）
  """
  db_path = output_dir / f"lock_probe_n{workers}.db"
  _prepare_lock_probe_db(db_path)
  latencies: list[float] = []
  errors: list[str] = []
  barrier = threading.Barrier(workers)
  threads = [
    threading.Thread(
      target=_lock_probe_writer,
      args=(db_path, workers, index, barrier, latencies, errors),
      name=f"lock-probe-{workers}-{index}",
      daemon=True,
    )
    for index in range(workers)
  ]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join(timeout=120.0)
  payload = {
    "threads": workers,
    "writes_per_thread": LOCK_PROBE_WRITES_PER_THREAD,
    "total_writes": workers * LOCK_PROBE_WRITES_PER_THREAD,
    "p50_ms": round(percentile(latencies, 50), 3),
    "p95_ms": round(percentile(latencies, 95), 3),
    "p99_ms": round(percentile(latencies, 99), 3),
    "max_ms": round(max(latencies), 3) if latencies else 0.0,
    "error_count": len(errors),
    "errors": errors[:3],
  }
  if db_path.exists():
    db_path.unlink()
  print(
    f"  [写锁探针] n={workers}: p50={payload['p50_ms']:.3f}ms | "
    f"p95={payload['p95_ms']:.3f}ms | 错误={payload['error_count']}"
  )
  return payload


def probe_sqlite_write_lock_repeated(
  workers: int,
  output_dir: Path,
  repeats: int,
) -> dict[str, Any]:
  """
  合成写锁探针重复测量：单轮抖动大（OS 调度/GC 一次插入等几十毫秒），取中位

  参数:
    workers (int): 线程数（与并发档位同值）
    output_dir (Path): 探针库所在目录
    repeats (int): 重复轮数（>=1，通常取 3）

  返回:
    dict[str, Any]: 单轮结构 + "repeats": [每轮 p50/p95] + 中位口径的
             p50_ms / p95_ms / p99_ms / max_ms / error_count

  异常:
    无
  """
  rounds = [
    probe_sqlite_write_lock(workers, output_dir) for _ in range(max(1, repeats))
  ]
  payload: dict[str, Any] = dict(rounds[-1])
  payload["p50_ms"] = round(default_median([float(row["p50_ms"]) for row in rounds]), 3)
  payload["p95_ms"] = round(default_median([float(row["p95_ms"]) for row in rounds]), 3)
  payload["p99_ms"] = round(default_median([float(row["p99_ms"]) for row in rounds]), 3)
  payload["max_ms"] = round(default_median([float(row["max_ms"]) for row in rounds]), 3)
  payload["error_count"] = sum(int(row["error_count"]) for row in rounds)
  payload["repeats"] = [
    {"p50_ms": row["p50_ms"], "p95_ms": row["p95_ms"], "error_count": row["error_count"]}
    for row in rounds
  ]
  print(
    f"  [写锁探针·中位] n={workers}（{len(rounds)} 轮）: "
    f"p50={payload['p50_ms']:.3f}ms | p95={payload['p95_ms']:.3f}ms"
  )
  return payload


def _load_curve_payload(path: Path) -> dict[str, Any]:
  """
  读取既有并发曲线 JSON（分段测量时用于合并）

  参数:
    path (Path): JSON 路径

  返回:
    dict[str, Any]: 既有数据；文件不存在或不可解析时返回空字典
  """
  if not path.exists():
    return {}
  try:
    loaded = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError):
    return {}
  return loaded if isinstance(loaded, dict) else {}


def _finalize_curve(payload: dict[str, Any]) -> dict[str, Any]:
  """
  重算派生字段：吞吐、加速比、效率、扣护栏、锁等待增量

  参数:
    payload (dict[str, Any]): 含 "levels" 的曲线数据（就地更新）

  返回:
    dict[str, Any]: 同一个 payload（便于链式调用）
  """
  levels: dict[str, Any] = payload.setdefault("levels", {})
  ordered = sorted(levels.values(), key=lambda item: int(item["workers"]))
  base = next((item for item in ordered if int(item["workers"]) == 1), None)
  base_seconds = float(base["median_seconds"]) if base else 0.0
  base_lock = float((base.get("lock_probe") or {}).get("p50_ms", 0.0)) if base else 0.0
  for item in ordered:
    seconds = float(item["median_seconds"])
    workers = int(item["workers"])
    throughput = (int(item["passed"]) / seconds * 60.0) if seconds > 0 else 0.0
    speedup = (base_seconds / seconds) if seconds > 0 and base_seconds > 0 else 0.0
    lock = item.get("lock_probe") or {}
    item["throughput_per_min"] = round(throughput, 1)
    item["speedup_vs_1"] = round(speedup, 3)
    item["efficiency"] = round(speedup / workers, 3) if workers else 0.0
    item["throughput_guardrail_per_min"] = THROUGHPUT_GUARDRAIL_PER_MIN
    item["throughput_passed"] = throughput >= THROUGHPUT_GUARDRAIL_PER_MIN
    item["lock_wait_p50_ms"] = (
      round(max(0.0, float(lock.get("p50_ms", 0.0)) - base_lock), 3) if base_lock else 0.0
    )
  return payload


def run_concurrency_curve(args: argparse.Namespace) -> int:
  """
  执行并发曲线测量：逐档跑全量 pytest + 写锁探针，合并后落 JSON 并打印摘要

  参数:
    args (argparse.Namespace): 含 runs / concurrency_levels / curve_output_dir

  返回:
    int: 进程退出码（0 正常；1 参数或测量异常）
  """
  output_dir: Path = args.curve_output_dir
  json_path = output_dir / CURVE_JSON_NAME
  try:
    levels = _parse_concurrency_levels(args.concurrency_levels)
  except ValueError as exc:
    print(f"[失败] {exc}")
    return 1
  if args.runs < 1:
    print("--runs 必须 >= 1")
    return 1

  output_dir.mkdir(parents=True, exist_ok=True)
  payload = _load_curve_payload(json_path)
  payload["mode"] = "concurrency_curve"
  payload["environment"] = environment_info()
  payload["protocol"] = {
    "pytest_command": f"python -m pytest {' '.join(CURVE_PYTEST_ARGS)} -n <N>",
    "levels_raw": args.concurrency_levels,
    "runs_per_level": args.runs,
    "levels": levels,
    "throughput_guardrail_per_min": THROUGHPUT_GUARDRAIL_PER_MIN,
    "throughput_formula": "通过条数 / 中位耗时(s) × 60",
    "lock_probe": (
      f"{LOCK_PROBE_WRITES_PER_THREAD} 条/线程的 BEGIN IMMEDIATE + INSERT + COMMIT，"
      f"busy_timeout={LOCK_PROBE_BUSY_TIMEOUT_MS}ms，库为临时文件、测完即删"
    ),
    "lock_wait_formula": "该档 p50 − 1 档 p50（差值口径，非内核计数器）",
    "dataset": "当日全量 pytest 用例集（-q --collect-only 实收条数）",
    "excluded": "不含 --cov（插桩会放大锁竞争并拖慢耗时，与实验 A1 同口径）",
  }
  payload["levels"] = payload.get("levels", {})

  if args.lock_probe_only:
    # 只重测写锁探针（pytest 耗时是分钟级成本，不因探针口径调整而重跑）
    for workers in levels:
      existing = payload["levels"].get(str(workers))
      if not existing:
        print(f"[跳过] 档位 n={workers} 尚无 pytest 数据，不单独补探针")
        continue
      existing["lock_probe"] = probe_sqlite_write_lock_repeated(
        workers, output_dir, args.runs
      )
    _finalize_curve(payload)
    payload["updated_at"] = now_iso()
    dump_json(json_path, payload)
    _print_concurrency_curve(payload)
    print(f"\n[JSON] 并发曲线数据（仅刷新写锁探针）: {json_path}")
    return 0

  for workers in levels:
    print(f"\n[并发曲线] 档位 n={workers}，共 {args.runs} 轮")
    measured = measure_pytest_concurrency(workers, args.runs)
    measured["lock_probe"] = probe_sqlite_write_lock_repeated(workers, output_dir, args.runs)
    payload["levels"][str(workers)] = measured
    _finalize_curve(payload)
    dump_json(json_path, payload)
    print(f"  [落盘] {json_path}（档位已合并，防中途中断丢数据）")

  _finalize_curve(payload)
  payload["updated_at"] = now_iso()
  dump_json(json_path, payload)
  _print_concurrency_curve(payload)
  print(f"\n[JSON] 并发曲线数据: {json_path}")
  return 0


def _print_concurrency_curve(payload: dict[str, Any]) -> None:
  """
  打印并发曲线摘要（曲线表 + 吞吐护栏判定）

  参数:
    payload (dict[str, Any]): run_concurrency_curve 的产物

  返回:
    无
  """
  levels = payload.get("levels", {})
  if not levels:
    print("\n[并发曲线] 无数据")
    return
  ordered = sorted(levels.values(), key=lambda item: int(item["workers"]))
  print("\n===== 并发曲线（xdist worker 档位） =====")
  header = ("并发数", "中位耗时(s)", "吞吐(用例/分)", "加速比", "效率", "锁等待p50(ms)")
  rows = [
    (
      str(int(item["workers"])),
      f"{float(item['median_seconds']):.2f}",
      f"{float(item['throughput_per_min']):.1f}",
      f"{float(item['speedup_vs_1']):.2f}",
      f"{float(item['efficiency']):.2f}",
      f"{float(item['lock_wait_p50_ms']):.3f}",
    )
    for item in ordered
  ]
  widths = [max(len(header[i]), *(len(row[i]) for row in rows)) for i in range(len(header))]
  line = " | ".join(header[i].ljust(widths[i]) for i in range(len(header)))
  print(line)
  print("-" * len(line))
  for row in rows:
    print(" | ".join(row[i].ljust(widths[i]) for i in range(len(header))))

  guard_rows = [
    (
      f"n={int(item['workers'])} 吞吐(用例/分)",
      f"≥{THROUGHPUT_GUARDRAIL_PER_MIN:.0f}",
      f"{float(item['throughput_per_min']):.1f}",
      verdict(bool(item["throughput_passed"])),
    )
    for item in ordered
  ]
  print_table("并发曲线吞吐护栏", guard_rows)


# ---------------------------------------------------------------------------
# 环境信息与命令行入口
# ---------------------------------------------------------------------------
def environment_info() -> dict[str, Any]:
  """
  采集测量环境信息（写进 JSON 与报告）

  参数:
    无

  返回:
    dict[str, Any]: 机器/解释器/pytest/pytest-xdist/SQLite 版本
  """
  return {
    "os": f"{platform.system()} {platform.release()} {platform.version()}",
    "machine": platform.machine(),
    "processor": platform.processor(),
    "cpu_count": os.cpu_count(),
    "python": sys.version.split()[0],
    "pytest": pytest.__version__,
    "pytest_xdist": _distribution_version("pytest-xdist"),
    "sqlite": sqlite3.sqlite_version,
    "measured_at": now_iso(),
  }


def _distribution_version(distribution: str) -> str:
  """
  读取已安装发行包的版本号（测量环境留痕用）

  参数:
    distribution (str): 发行包名（如 "pytest-xdist"）

  返回:
    str: 版本号；未安装或元数据不可读时返回 "unavailable"

  异常:
    无（版本留痕失败不得让测量脚本中断）
  """
  try:
    return metadata.version(distribution)
  except (metadata.PackageNotFoundError, OSError, ValueError):
    return "unavailable"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  """
  解析命令行参数

  参数:
    argv (list[str] | None): 参数列表，None 表示取 sys.argv

  返回:
    argparse.Namespace: 解析结果
  """
  parser = argparse.ArgumentParser(
    description="TestMatrix Day63 全量基线测量与绝对规模北极星测量",
  )
  parser.add_argument(
    "--experiment",
    choices=("A", "B", "all"),
    default="all",
    help="选择实验：A=当日全量真实集，B=1 万条合成集，all=两个都跑",
  )
  parser.add_argument("--runs", type=int, default=3, help="每项指标的重复次数（取中位）")
  parser.add_argument(
    "--output-dir",
    type=Path,
    default=OUTPUT_DIR_DEFAULT,
    help="JSON 原始数据输出目录",
  )
  parser.add_argument(
    "--platform-sample",
    type=int,
    default=150,
    help="实验 A 平台侧同数据集样本条数（全量平台执行成本约 1 小时/轮）",
  )
  parser.add_argument(
    "--linear-check",
    action="store_true",
    help="是否额外在 2 倍样本量上复测一次，验证单条成本恒定",
  )
  parser.add_argument(
    "--synth-total",
    type=int,
    default=10000,
    help="实验 B 合成用例总条数（护栏针对该规模）",
  )
  parser.add_argument(
    "--synth-sample",
    type=int,
    default=1000,
    help="实验 B 实测样本条数，0 表示跑全量（全量约 80 分钟/轮）",
  )
  parser.add_argument(
    "--concurrency-curve",
    action="store_true",
    help="只跑 Day64 并发曲线（1/2/4/8 档 + 合成写锁探针），不跑实验 A/B",
  )
  parser.add_argument(
    "--concurrency-levels",
    default="1,2,4,8",
    help="并发档位，逗号分隔（默认 1,2,4,8；可分段执行后合并 JSON）",
  )
  parser.add_argument(
    "--curve-output-dir",
    type=Path,
    default=CURVE_OUTPUT_DIR_DEFAULT,
    help="并发曲线 JSON 输出目录（默认 output/baseline_day64）",
  )
  parser.add_argument(
    "--lock-probe-only",
    action="store_true",
    help="只重测写锁探针并合并进既有 JSON（不重跑分钟级 pytest 耗时）",
  )
  return parser.parse_args(argv)


def _configure_console() -> None:
  """
  把标准输出/错误切到 UTF-8（Windows 默认 GBK 控制台遇中文表格会崩）

  参数:
    无

  返回:
    无

  异常:
    无（流不支持 reconfigure 时静默跳过，例如被替换成字符串缓冲）
  """
  for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
      reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
  """
  脚本入口：准备环境 → 按参数执行实验 → 落 JSON → 打印摘要

  参数:
    argv (list[str] | None): 参数列表，None 表示取 sys.argv

  返回:
    int: 进程退出码（0 成功；1 环境/测量异常）
  """
  _configure_console()
  args = parse_args(argv)
  if args.concurrency_curve:
    # 曲线模式不碰数据库副本：它只跑裸 pytest 与临时库写锁探针
    return run_concurrency_curve(args)
  if args.runs < 1:
    print("--runs 必须 >= 1")
    return 1

  output_dir: Path = args.output_dir
  db_path = output_dir / MEASURE_DB_NAME
  print(f"[准备] 测量库副本: {db_path}")
  initial = prepare_measure_db(db_path)
  app = create_app("test")
  print(
    f"[准备] 初始行数: test_cases={initial['test_cases']} | "
    f"batches={initial['test_execution_batches']} | "
    f"executions={initial['test_executions']} | "
    f"defects={initial['defect_statistics']}"
  )

  env = environment_info()
  print(
    f"[准备] 环境: {env['os']} | CPU={env['cpu_count']} | "
    f"Python={env['python']} | pytest={env['pytest']} | SQLite={env['sqlite']}"
  )

  exit_code = 0
  try:
    if args.experiment in ("A", "all"):
      payload_a = run_experiment_a(app, args)
      payload_a["environment"] = env
      payload_a["initial_counts"] = initial
      dump_json(output_dir / "experiment_A_raw.json", payload_a)
      _print_experiment_a(payload_a)
    if args.experiment in ("B", "all"):
      payload_b = run_experiment_b(app, args)
      payload_b["environment"] = env
      payload_b["initial_counts"] = initial
      dump_json(output_dir / "experiment_B_raw.json", payload_b)
      _print_experiment_b(payload_b)
  except Exception as exc:  # noqa: BLE001 测量脚本边界：任何异常都要打全并返回非 0
    print(f"[失败] {type(exc).__name__}: {exc}")
    exit_code = 1
  finally:
    DatabaseSession.reset()

  print(f"\n[JSON] 原始数据目录: {output_dir}")
  return exit_code


def _print_experiment_a(payload: dict[str, Any]) -> None:
  """
  打印实验 A 摘要表

  参数:
    payload (dict[str, Any]): run_experiment_a 的产物

  返回:
    无
  """
  guards = payload["guards"]
  bare = guards["bare_pytest_median_seconds"]
  rows = [
    (
      "裸pytest耗时(s)",
      "—",
      f"{bare:.2f}",
      "基线 B",
    ),
    (
      "平台执行耗时(s/样本)",
      "—",
      f"{payload['platform']['median_seconds']:.2f}"
      f"（{payload['platform']['cases_per_run']} 条）",
      "实测",
    ),
    (
      "平台全量外推(s)",
      f"≤{guards['improvement_rule']['target_seconds']:.0f}",
      f"{guards['throughput_platform_projected_per_min']['projected_seconds_for_full_set']:.0f}",
      "外推",
    ),
    (
      "4worker吞吐(用例/分)",
      f"≥{THROUGHPUT_GUARDRAIL_PER_MIN:.0f}",
      f"{guards['throughput_bare_per_min']['value']:.0f}",
      verdict(guards["throughput_bare_per_min"]["passed"]),
    ),
    (
      "平台吞吐(用例/分)",
      f"≥{THROUGHPUT_GUARDRAIL_PER_MIN:.0f}",
      f"{guards['throughput_platform_projected_per_min']['value']:.1f}（外推）",
      verdict(guards["throughput_platform_projected_per_min"]["passed"]),
    ),
    (
      "平台开销比",
      f"≤{OVERHEAD_RATIO_GUARDRAIL}",
      f"{guards['platform_overhead_ratio']['value']:.2f}",
      verdict(guards["platform_overhead_ratio"]["passed"]),
    ),
  ]
  for name in ("create_case", "record_execution", "finish_execution"):
    item = guards["write_p95"][name]
    rows.append(
      (
        f"{name} P95(ms)",
        f"≤{WRITE_P95_GUARDRAIL_MS:.0f}",
        f"{item['value_ms']:.2f}",
        verdict(item["passed"]),
      )
    )
  print_table("实验 A 摘要", rows)
  rule = guards["improvement_rule"]
  print(
    f"改进规则: B={rule['baseline_b_seconds']}s | 靶值={rule['target_seconds']}s | "
    f"回归报警线={rule['alarm_seconds']}s"
  )


def _print_experiment_b(payload: dict[str, Any]) -> None:
  """
  打印实验 B 摘要表

  参数:
    payload (dict[str, Any]): run_experiment_b 的产物

  返回:
    无
  """
  guards = payload["guards"]
  measured = guards["e2e_measured_sample_seconds"]
  projected = guards["e2e_projected_full_seconds"]
  rows = [
    (
      f"{measured['cases']}条端到端(s)",
      f"≤{SYNTH_E2E_GUARDRAIL_SECONDS:.0f}",
      f"{measured['value']:.1f}",
      verdict(measured["passed"]),
    ),
    (
      f"{projected['cases']}条外推(s)",
      f"≤{SYNTH_E2E_GUARDRAIL_SECONDS:.0f}",
      f"{projected['value']:.1f}",
      verdict(projected["passed"]),
    ),
    (
      "扩增集构成",
      "mock 100%",
      "mock 100%",
      verdict(True),
    ),
    (
      "合成用例残留",
      "0",
      str(payload["cleanup"]["remaining_synth"]),
      verdict(payload["cleanup"]["restored"]),
    ),
  ]
  print_table("实验 B 摘要", rows)
  print(f"数据集声明: {guards['composition_declared']}")


if __name__ == "__main__":
  sys.exit(main())
