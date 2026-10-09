"""
用例调度与管理模块

功能（第二阶段Day1交付）:
    - 批次号生成 generate_execution_id: 格式RUN-YYYYMMDD-HHMMSS-xxxx，
      xxxx为uuid4前4位hex，进程内防碰撞保证唯一
    - 用例加载入库 sync_cases_from_file: 调DataDriver统一入口加载YAML/Excel数据，
      upsert到test_cases表（case_id存在则更新，不存在则插入），
      case_type按文件路径自动推断（含chip/serial/telnet为chip，否则为api）
    - 用例查询 list_cases: 支持module/priority/status/case_type多维度筛选，
      返回dict列表，按priority升序（P0→P3）再按case_id升序排列

功能（第二阶段Day2交付）:
    - 创建执行批次 create_execution: 校验trigger合法值并生成批次号，
      批次元信息（触发方式/执行人/环境/备注）全量记录日志
    - 筛选待执行用例 select_cases_for_execution: 复用list_cases查询active用例，
      支持module/priority/tags（description标签解析+交集匹配）筛选
    - 记录单条执行结果 record_execution: 单用例执行明细写入test_executions表，
      result合法性、failed/error必填error_message强校验
    - 完成批次汇总 finish_execution: 按批次聚合统计total/passed/failed/error/
      skipped/pass_rate，upsert到defect_statistics表

功能（第二阶段Day3交付）:
    - 批量执行 run_batch: 串联sync→create→select→execute→record→finish
      完整调度链路，支持dry_run只加载筛选不执行
    - 命令行入口 main: argparse解析--file/--priority/--module/
      --tags/--trigger/--dry-run/--notify参数，可直接python -m src.core.case_manager运行
    - 模拟执行器 _simulate_execute: 模拟单条用例执行（后续Web平台接入
      真实pytest执行时替换此方法）

功能（第二阶段Day15交付）:
    - 批次统计适配 build_notification_statistics: 批次DB执行记录转
      AllureResult列表（状态映射error→broken、耗时秒→毫秒、
      test_cases批量补全module/priority），复用ReportStatistics.aggregate
    - 批次自动通知 notify_execution_result: 统计模型交NotificationRouter
      推送（旁路铁律: 通知异常仅记error日志，不影响执行主流程）
    - run_batch新增notify参数（默认False零回归），CLI同步支持--notify

功能（第三阶段Day19交付）:
    - 分页用例查询 list_cases_paged: limit/offset在数据库层完成分页，
      支持keyword关键字三字段模糊搜索（case_id/name/description，
      LIKE不区分大小写），返回items/total/page/page_size/total_pages
      分页结构；与list_cases并存（后者为内部链路保留，保持向后兼容）

功能（第三阶段Day20交付）:
    - 用例详情查询 get_case: 按业务编号查询单条用例，不存在返回None
      （由路由层负责转NotFoundError，核心层不感知HTTP语义）
    - 用例创建 create_case: case_id查重（已存在抛CaseManagerError）后
      插入，priority统一转大写入库（与list_cases查询口径对齐）
    - 用例更新 update_case: 只更新传入字段、未传字段保持不变，case_id
      业务编号不可变，updated_at由模型onupdate=func.now()自动刷新
    - 用例删除 delete_case: 按业务编号物理删除，不存在抛CaseManagerError

功能（第三阶段Day24交付）:
    - 启动执行批次 start_execution: 筛选用例+生成批次号+写批次
      元信息行（pending状态），返回批次号/用例数/用例列表
    - 批次后台执行编排 _execute_batch_async: 批次状态机
      pending→running→finished/failed，逐用例经执行器抽象层
      run_one执行并立即落明细，完成后聚合汇总并冗余统计到批次行；
      线程目标函数，任何异常置failed绝不向调用线程抛出
    - 批次状态查询 get_execution_status: 查批次元信息表返回状态
      字典（含冗余统计与时间字段），不存在返回None

功能（第三阶段Day25交付）:
    - 执行事件埋点 _publish_execution_event /
      _close_execution_channel: _execute_batch_async关键节点
      （batch_start/case_finished/batch_finished/batch_failed）
      向事件通道发布事件，批次终态后关闭并清理通道；埋点全程
      try/except兜底只记日志，日志通道故障绝不影响真实执行

功能（第三阶段Day31交付）:
    - 缓存失效埋点: create_case/update_case/delete_case/
      sync_cases_from_file成功后失效用例列表缓存；
      _execute_batch_async的finished/failed收尾处失效报告统计
      缓存。失效调用统一走cache_client，Redis故障静默降级，
      业务代码零try/except

使用示例:
    from src.core.case_manager import CaseManager, generate_execution_id

    execution_id = generate_execution_id()
    sync_result = CaseManager.sync_cases_from_file(
        "testdata/yaml/api_user_query_matrix.yaml"
    )
    cases = CaseManager.list_cases(module="用户管理", priority=["P0", "P1"])

    # 执行调度链路
    execution_id = CaseManager.create_execution(trigger="ci", executor="jenkins")
    cases = CaseManager.select_cases_for_execution(priority="P0")
    CaseManager.record_execution(execution_id, "TM-0001", "登录校验", "passed",
                                 start_time, end_time, 0.5)
    summary = CaseManager.finish_execution(execution_id)

    # 批量执行与命令行入口
    summary = run_batch("testdata/yaml/api_user_query_matrix.yaml", dry_run=True)
    # 命令行: python -m src.core.case_manager -f testdata/yaml/xxx.yaml --dry-run
"""

import argparse
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from src.common.logger import LogManager

# Day47-fix P3-3: source_ref 校验实现已下沉到 src/common/source_ref.py
# ——执行侧（executors.PytestRunner.build_command）也要用同一份校验，而
# case_manager 反向依赖 executors，就地定义会构成**循环导入**。
# 此处保留再导出，对外 API 面（case_manager.SOURCE_REF_PATTERN /
# MAX_SOURCE_REF_LENGTH / validate_source_ref）零变化，既有引用与测试
# 不必改（符合 6.33 幂等导出约定）。用 `X as X` 冗余别名声明"有意再导出"，
# 否则 ruff 会把本模块内未直接使用的名字当 F401 删掉（该项目发生过一次）。
from src.common.source_ref import MAX_SOURCE_REF_LENGTH as MAX_SOURCE_REF_LENGTH
from src.common.source_ref import SOURCE_REF_PATTERN as SOURCE_REF_PATTERN
from src.common.source_ref import validate_source_ref
from src.common.time_utils import local_to_utc_iso, to_utc_iso
from src.core.cache import cache_client
from src.core.data_driver import DataDriver, DataDriverError
from src.core.event_bus import ExecutionEvent, close_channel, get_channel
from src.core.executors import get_executor
from src.core.notification import NotificationRouter
from src.core.report_analyzer import (
    AllureResult,
    ReportStatistics,
    StatisticsResult,
)
from src.core.task_queue import task_queue_client
from src.db.db_session import DatabaseSession
from src.db.models import (
    DefectStatistic,
    TestCase,
    TestExecution,
    TestExecutionBatch,
)

logger = LogManager.get_logger()

# 优先级排序权重: 数值越小越靠前（P0最高），未知优先级排最后
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# 分页查询每页最大条数（防止单页拉取全表，Web列表API分页上限）
MAX_PAGE_SIZE = 100

# 分页页码上界（Day44 P2-07）：SQL 的 OFFSET 是 (page-1)*page_size，
# page 无上界时一个构造请求即可让数据库扫描并丢弃近乎整表的数据；SQLite 下
# offset 超出 int64 还会抛 OverflowError，被包装成 SQLAlchemyError 后冒泡成
# 500 —— 一个本该 400 的参数错误变成了服务端故障。取 10000 的依据：配合
# MAX_PAGE_SIZE=100 即 offset 上限约 100 万行，已远超任何列表的合理翻页深度，
# 同时远低于 int64 溢出阈值。本常量是 page 上界的单一事实来源，Web 层
# src/web/pagination.py 从此处 import，避免两条路径的上界口径分叉。
MAX_PAGE = 10000

# 串口/Telnet协议用例的路径特征关键词（路径命中任意词即判定为chip类型，统一小写匹配）
CHIP_PATH_KEYWORDS = ("chip", "serial", "telnet")

# 执行批次触发方式合法值
VALID_TRIGGERS = ("manual", "cli", "web", "ci")

# 单条执行结果合法值
VALID_RESULTS = ("passed", "failed", "error", "skipped")

# description字段中标签暂存格式的前缀（与_build_description写入格式对齐）
TAGS_PREFIX = "标签:"

# 进程内已生成批次号集合: 防止同秒内uuid4前4位hex碰撞
# （16bit空间100次生成理论碰撞概率约7%），重试机制保证进程内绝对唯一
_generated_execution_ids: set = set()


class CaseManagerError(Exception):
    """
    用例调度管理统一异常类

    风格与common层HttpClientError保持一致: 封装用例管理各环节异常，
    携带操作上下文（context字典: 操作名/文件路径等），便于问题快速定位。

    属性:
        context (dict): 异常发生时的操作上下文信息
    """

    def __init__(self, message: str, context: dict | None = None):
        """
        初始化异常

        参数:
            message (str): 异常描述信息
            context (dict | None): 操作上下文（如操作名/文件路径），默认空字典

        返回:
            无
        """
        self.context = context or {}
        super().__init__(message)


# 批次号去重集合的容量上限（超限即清空，理由见 generate_execution_id）
# 取1万：按每天1000批次计可覆盖约10天，远超同秒碰撞的时间窗，
# 同时把常驻内存钉死在约1MB量级
MAX_TRACKED_EXECUTION_IDS = 10_000

# --------------------------------------------------------------------------
# 用例可更新字段白名单（update_case 纵深防御）
# --------------------------------------------------------------------------
# 为什么需要: update_case 原先对 payload 逐字段裸 setattr。HTTP 路径上
# CaseUpdateSchema 配 unknown=EXCLUDE 已把未知字段剥掉，所以今天从接口
# 传不进来；但 update_case 是公开类方法，任何内部调用方（CLI、脚本、
# 后续新代码）传错字段名时，setattr 只会在 ORM 实例上挂一个不落库的
# 临时属性，调用方拿到 200 + 原样数据，毫无察觉地丢掉这次更新。
# 白名单让"传了个不存在的字段"从静默丢弃变成一条可检索的 warning。
#
# 与不可变字段的分工: id/case_id/created_at 走 _IMMUTABLE_CASE_FIELDS
# 静默剔除（前端编辑表单会回传 case_id，属兼容而非错误）；不在白名单
# 里的其它字段记 warning——"没这个字段"和"这个字段不能改"是两回事。
UPDATABLE_CASE_FIELDS = frozenset(
    {
        "name",
        "module",
        "priority",
        "case_type",
        "status",
        "description",
        "creator",
        # source_ref（Day47）: pytest 执行目标，允许人工补录/修正。
        # 归入白名单而非不可变字段——它是**数据**（指向哪个测试文件），
        # 不是工作流属性；回填脚本只是首次填值的自动化，人工补录是
        # 同一件事的另一半，必须留出通道。
        "source_ref",
    }
)

# 业务编号/主键/创建时间不随更新变化（静默剔除，兼容前端回传完整对象）
_IMMUTABLE_CASE_FIELDS = ("id", "case_id", "created_at")

# 表 test_cases 上承载"业务编号唯一"语义的列名。
# SQLite 的唯一约束违例消息形如
#   UNIQUE constraint failed: test_cases.case_id
# PostgreSQL 形如 duplicate key value violates unique constraint
#   "uq_test_cases_case_id"（含列名）；MySQL 形如 Duplicate entry
#   'TM-0001' for key 'test_cases.case_id'。三者都含列名，据此判定。
_UNIQUE_CASE_ID_COLUMN = "case_id"


def _is_unique_case_id_violation(exc: IntegrityError) -> bool:
    """
    判断 IntegrityError 是否为"业务编号唯一约束"违例（v3 修复 V2-P2-4）

    背景: IntegrityError 覆盖全部完整性违例。NOT NULL / CHECK / 外键违例
    此前被一律翻译成"用例编号已存在"的 409，真因（某个必填字段是 None）
    被彻底掩盖，排障方向被带偏。

    判定依据: 各方言的唯一约束违例消息都含被约束的列名（test_cases.case_id
    或约束名 uq_test_cases_case_id）。这里只匹配**列名**这一稳定特征，
    不去匹配 "UNIQUE"/"duplicate" 等方言相关措辞——那些措辞各库不同
    且会随版本变化。

    参数:
        exc (IntegrityError): 捕获到的完整性异常

    返回:
        bool: True 表示是业务编号唯一约束违例（可翻译为 409）；
              False 表示是其它完整性违例（应回落通用错误）
    """
    message = str(exc.orig) if getattr(exc, "orig", None) else str(exc)
    return _UNIQUE_CASE_ID_COLUMN in message


def _safe_invalidate(operation: str, invalidate: Any) -> None:
    """
    缓存失效的统一兜底包装（v3 修复 V2-P3-8）

    为什么需要: 批次收尾路径上的 invalidate_reports 早已各自加了
    try/except（理由是"在外层 except 内裸抛会被当执行失败"，甚至会杀死
    daemon 线程），而 4 处 invalidate_cases_list 仍是裸调。口径不一致
    意味着：缓存层万一抛异常，用例已创建/更新/删除成功却返回 500，
    批量导入也会整批失败——缓存是旁路能力，绝不能反过来阻断业务。

    参数:
        operation (str): 操作名，仅用于日志定位（如 create_case）
        invalidate (Any): 无参可调用对象（通常是 cache_client 的方法）

    返回:
        None
    """
    try:
        invalidate()
    except Exception as cache_exc:  # noqa: BLE001 缓存故障不得阻断业务
        logger.warning(
            f"缓存失效异常已忽略（不影响{operation}结果） | "
            f"操作: {operation} | {type(cache_exc).__name__}: {cache_exc}"
        )


def _invalidate_case_related_caches(operation: str) -> None:
    """
    用例写操作后的**统一**缓存失效（Day45 全量审查第 3 批 P1-4）

    为什么必须同时清两个前缀：报告类统计**硬依赖 test_cases 表**——
        - report_analyzer.py `get_module_distribution`：
          `TestExecution LEFT OUTER JOIN TestCase` 后按 `TestCase.module` 分组
        - `get_priority_distribution`：同构，按 `TestCase.priority` 分组
        - `get_quality_metrics`：以 `TestCase.status == "active"` 计数作分母
    而 module / priority / status 三者全在可更新白名单内。修复前
    `invalidate_cases_list` 有 4 处调用而 `invalidate_reports` 仅 2 处
    （都在 `_execute_batch_async` 内），于是**改用例的 module/priority/status
    或删用例后，报告统计在 TTL（默认 300s）内持续返回与库不一致的聚合值**。
    聚合指标出错比明细出错更难察觉（用户看到"模块通过率 87%"，库里口径已变）。

    `delete_case` 是物理删除，删除后用例数必须下降，同样依赖本函数。

    参数:
        operation (str): 操作名，仅用于日志定位（create/update/delete/批量导入）

    返回:
        None
    """
    _safe_invalidate(operation, cache_client.invalidate_cases_list)
    _safe_invalidate(operation, cache_client.invalidate_reports)


# --------------------------------------------------------------------------
# 业务语义异常子类（路由层异常翻译的类型锚点）
# --------------------------------------------------------------------------
# 为什么需要: 路由层原先靠 `if "不存在" not in str(exc)` 这类**子串匹配**决定
# 转 404 还是原样抛 500。子串匹配是脆的——任何 DB 异常消息里恰好含"不存在"
# （例如"表 test_cases 不存在"、"database does not exist"）都会被误判成
# 业务 404，真实故障被静默吞掉。改为按异常类型分流后，判定依据是代码里
# 显式 raise 的语义，与错误文案彻底解耦。
#
# 向后兼容: 三个子类都继承 CaseManagerError，任何 `except CaseManagerError`
# 的既有捕获点行为不变；核心层保持 HTTP 无关（不引入 404/409 等 HTTP 概念），
# 异常翻译仍集中在路由层，只是锚点从"文案"换成"类型"。
class CaseNotFoundError(CaseManagerError):
    """资源不存在语义（用例/批次查不到）→ 路由层转 NotFoundError(404)"""


class CaseConflictError(CaseManagerError):
    """唯一性冲突语义（编号已存在）→ 路由层转 ConflictError(409)"""


class CaseDataLoadError(CaseManagerError):
    """数据文件加载/解析失败语义 → 路由层转 ValidationError(400)"""


class NoCasesSelectedError(CaseManagerError):
    """筛选无命中语义（无可执行用例）→ 路由层转 ValidationError(400)"""


def generate_execution_id() -> str:
    """
    生成测试执行批次号

    格式: RUN-YYYYMMDD-HHMMSS-xxxx，xxxx为uuid4前4位hex小写
    示例: RUN-20260824-213000-a1b2

    唯一性保障: 维护进程内已生成集合，碰撞时自动重新生成
    （同秒批量生成场景下的理论碰撞概率由约7%降为0）。

    参数:
        无

    返回:
        str: 进程内唯一的执行批次号

    异常:
        无
    """
    while True:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        short_uuid = uuid.uuid4().hex[:4]
        execution_id = f"RUN-{timestamp}-{short_uuid}"
        if execution_id not in _generated_execution_ids:
            _generated_execution_ids.add(execution_id)
            # 有界化: 集合只增不减会让长跑进程的内存单调增长（按每天1000
            # 批次计，一年36万条）。超上限时清空——批次号自带时间戳前缀，
            # 距今超过一天的编号不可能再被生成，清空导致的碰撞概率
            # 等同于"同秒+uuid前4位撞车"，本就是极小概率事件
            if len(_generated_execution_ids) > MAX_TRACKED_EXECUTION_IDS:
                _generated_execution_ids.clear()
                _generated_execution_ids.add(execution_id)
                logger.debug(
                    f"批次号去重集合超上限已重置 | 上限: {MAX_TRACKED_EXECUTION_IDS}"
                )
            logger.debug(f"执行批次号已生成 | {execution_id}")
            return execution_id
        # 极小概率事件: 同秒+uuid前4位撞车，重新生成
        logger.debug(f"批次号碰撞，自动重生成 | {execution_id}")


def _escape_like(value: str) -> str:
    r"""
    转义 SQL LIKE 模式中的元字符（Day44 P2-12）

    LIKE 的元字符 `%`（任意长度）与 `_`（单字符）出现在用户输入里会被
    当作通配符：搜索 "%" 生成的模式是 "%%%"，语义从"包含百分号"退化为
    "匹配任意"，返回全表数据——结果与用户意图完全相反。
    先把反斜杠本身也转义（否则用户输入 `a\%` 时反斜杠被 LIKE 当转义符
    吃掉，导致后续字符的转义失效），再转义两个元字符。

    参数:
        value (str): 用户原始输入（调用方负责 strip 与 lower）

    返回:
        str: 可安全嵌入 LIKE 模式的可搜索文本
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
# Excel 单元格显式"清空"标记值（Day47-fix P3-4）
#
# 为什么需要它: DataDriver 只把**非 None** 单元格放进用例字典，
# 而 Excel 的空单元格与"没有这一列"在解析后长得一模一样（都不在字典里）。
# 于是 Excel 通道无法表达"把这条用例的 source_ref 清掉"——
# YAML 可以写 `source_ref:`（解析出 None）、API 可以传 null，
# 唯独 Excel 做不到。用一个显式标记值补上这个能力。
#
# 为什么标记值只对 source_ref 生效、而不是通用机制:
# source_ref 是当前唯一需要"清空"语义的字段（其余字段空值等价于
# "用默认值"，清空没有意义）。做成通用机制会要求 DataDriver 逐字段
# 判断并改变"键是否存在"的语义，影响面远超本条修复。
SOURCE_REF_CLEAR_MARKER = "__CLEAR__"


def _normalize_clear_marker(value: object) -> object:
    """
    把 Excel 显式清空标记值归一为 None（内部函数，Day47-fix P3-4）

    参数:
        value (object): 原始单元格值（None / 字符串 / 其它类型）

    返回:
        object: 等于标记值（忽略大小写与首尾空白）时返回 None；
                其余原样返回

    异常:
        无
    """
    if isinstance(value, str) and value.strip().upper() == SOURCE_REF_CLEAR_MARKER:
        return None
    return value


def _case_context(case: dict[str, object]) -> str:
    """
    构造 source_ref 校验失败的用例定位前缀（内部函数，Day47-fix P3-5）

    为什么带上 name: 只给 case_id 时，收到报错的人得回数据文件里
    逐行找"哪个 case_id 写错了"；而导入场景里人往往只记得用例名。
    Excel 的真实行号在 DataDriver 归一化后已不可得（case dict 里不留
    `_row_number`），故 case_id + name 是当前可得的最强定位组合。

    参数:
        case (dict): DataDriver 归一化后的单条用例数据

    返回:
        str: 形如 "用例 TM-0001（登录校验）: " 的前缀；
             name 缺失或为空时退化为 "用例 TM-0001: "
    """
    case_id = str(case.get("case_id", "")).strip()
    case_name = str(case.get("name") or "").strip()
    if case_name:
        return f"用例 {case_id}（{case_name}）: "
    return f"用例 {case_id}: "


class CaseManager:
    """
    用例调度与管理器

    承接DataDriver（数据加载）与DatabaseSession（持久化）之间的调度层，
    负责用例元信息入库与多维度查询，为后续执行调度提供数据基础。
    全部方法为类方法，无需实例化。
    """

    # ------------------------------------------------------------------
    # 用例加载入库
    # ------------------------------------------------------------------
    @classmethod
    def sync_cases_from_file(
        cls,
        file_path: str | Path,
        sheet_name: str | None = None,
        creator: str = "admin",
    ) -> dict:
        """
        从数据文件加载用例并upsert入库

        执行流程:
            1. 调DataDriver.load_cases统一入口加载并校验数据（后缀自动识别）
            2. 按文件路径推断case_type（含chip/serial/telnet为chip，否则为api）
            3. 逐条upsert到test_cases表: case_id存在则更新业务字段，
               不存在则插入新记录（更新时保留原记录的creator与status，
               二者为工作流属性，不随数据文件同步覆盖）
            4. source_ref 为**可选键**（Day47）: YAML 写 source_ref 键、
               Excel 写 source_ref 列表头即为该用例配置 pytest 执行目标；
               文件里没有这一列/键时不动库里的既有值（存量文件零改动继续
               可用，也不会把回填脚本已回填的 source_ref 冲掉）

        参数:
            file_path (str | Path): 数据文件路径（YAML/Excel，相对路径支持项目根兜底）
            sheet_name (str | None): Excel的sheet名称，仅Excel文件生效，默认None
            creator (str): 用例创建人（仅首次插入时写入），默认"admin"

        返回:
            dict: {"total": 加载总数, "inserted": 新增数, "updated": 更新数}

        异常:
            CaseManagerError: 文件路径为空 / 数据加载失败（DataDriverError包装） /
                              数据库操作异常（SQLAlchemyError包装）时抛出，
                              context携带operation与file_path定位信息
        """
        if not file_path or not str(file_path).strip():
            raise CaseManagerError(
                "用例数据文件路径不能为空",
                context={
                    "operation": "sync_cases_from_file",
                    "file_path": str(file_path),
                },
            )

        # 1. 数据加载（DataDriver内部完成格式识别/字段校验/规范化）
        try:
            cases = DataDriver.load_cases(file_path, sheet_name=sheet_name)
        except DataDriverError as exc:
            logger.error(f"用例数据加载失败 | 文件: {file_path} | {exc}")
            raise CaseDataLoadError(
                f"用例数据加载失败: {exc}",
                context={"operation": "load_cases", "file_path": str(file_path)},
            ) from exc

        # 1.5 source_ref 格式校验（Day47）
        # 必须在入库前整批校验完：DataDriver 只做必填字段与枚举校验，
        # source_ref 是新增的可选字段，由本层统一把关（YAML/Excel 同一入口，
        # 校验一次覆盖两种格式）。任一条不合法即整批拒绝并指出是哪一条，
        # 避免"前 30 条已入库、第 31 条报错"的半截状态。
        source_ref_map: dict[str, str | None] = {}
        try:
            for case in cases:
                raw_ref = case.get("source_ref")
                if raw_ref is None and "source_ref" not in case:
                    # 键不存在 = 该文件未配置此列，保持"不写"语义
                    continue
                source_ref_map[str(case["case_id"])] = validate_source_ref(
                    _normalize_clear_marker(raw_ref),
                    context=_case_context(case),
                )
        except ValueError as exc:
            logger.error(f"source_ref 校验失败 | 文件: {file_path} | {exc}")
            raise CaseDataLoadError(
                str(exc),
                context={
                    "operation": "validate_source_ref",
                    "file_path": str(file_path),
                },
            ) from exc

        # 2. 用例类型推断
        case_type = cls._infer_case_type(file_path)

        # 3. 逐条upsert入库（session_scope自动提交/回滚/关闭）
        inserted = 0
        updated = 0
        try:
            with DatabaseSession.session_scope() as session:
                # 一次性把本批 case_id 全部查出来建内存映射（Day44 P2-13）。
                # 修复前是循环内逐条 .filter_by().first()，N 条用例 = N 次
                # SELECT + N 次 INSERT；虽有 case_id 唯一索引兜底使单次查找
                # 不慢，但数百至数千条导入仍产生同量级往返（典型数百毫秒
                # 到数秒）。改成一次 IN 查询后，SELECT 从 N 次降到 1 次，
                # 写路径不变（仍逐条 add 交给 session 统一 flush）。
                #
                # 同一文件内重复 case_id 的处理：后写入者覆盖先写入者且被计入
                # updated。新建对象会即时登记回 existing_map（见循环内注释），
                # 因此重复项命中的是内存对象而非再插一条，行为与修复前
                # 依赖 autoflush 的逐条查询完全一致。
                wanted_ids = {str(case["case_id"]) for case in cases}
                existing_map: dict[str, Any] = {}
                if wanted_ids:
                    for row in (
                        session.query(TestCase)
                        .filter(TestCase.case_id.in_(wanted_ids))
                        .all()
                    ):
                        existing_map[row.case_id] = row
                for case in cases:
                    existing = existing_map.get(str(case["case_id"]))
                    case_key = str(case["case_id"])
                    # 该文件是否显式提供了 source_ref（区分"没这一列"
                    # 与"写了空值"：前者不写库以保住既有回填值）
                    has_source_ref = case_key in source_ref_map
                    if existing is not None:
                        # 已存在: 更新业务字段，保留creator与status
                        existing.name = case["name"]
                        existing.module = case["module"]
                        existing.priority = case["priority"]
                        existing.case_type = case_type
                        existing.description = cls._build_description(case)
                        if has_source_ref:
                            existing.source_ref = source_ref_map[case_key]
                        updated += 1
                    else:
                        new_row = TestCase(
                            case_id=case_key,
                            name=case["name"],
                            module=case["module"],
                            priority=case["priority"],
                            case_type=case_type,
                            status="active",
                            description=cls._build_description(case),
                            creator=creator,
                            # 文件未提供该键时为 None（模型默认值），
                            # 语义 = 该用例不可被 pytest 执行
                            source_ref=source_ref_map.get(case_key),
                        )
                        session.add(new_row)
                        # 关键：把新建对象也登记回映射。批量查询发生在循环
                        # 之前，不再依赖 SQLAlchemy 的隐式 autoflush 让
                        # "下一次查询能看见上一次刚插入的行"——若不登记，
                        # 同一文件内重复的 case_id 会被当成两条新记录各插一次，
                        # flush 时撞唯一约束，整批导入回滚。
                        # 登记后重复项走更新分支，与修复前的逐条查询行为一致。
                        existing_map[str(case["case_id"])] = new_row
                        inserted += 1
        except SQLAlchemyError as exc:
            logger.error(f"用例入库数据库异常 | 文件: {file_path} | {exc}")
            raise CaseManagerError(
                f"用例入库数据库异常: {exc}",
                context={"operation": "upsert", "file_path": str(file_path)},
            ) from exc

        result = {"total": len(cases), "inserted": inserted, "updated": updated}
        logger.info(
            f"用例同步入库完成 | 文件: {Path(file_path).name} | "
            f"case_type: {case_type} | 总数: {result['total']} | "
            f"新增: {inserted} | 更新: {updated}"
        )
        # Day31: 用例数据变更后失效列表缓存（统一走_safe_invalidate，
        # 缓存故障不影响导入主流程）
        _invalidate_case_related_caches("批量导入")
        return result

    # ------------------------------------------------------------------
    # 用例查询
    # ------------------------------------------------------------------
    @classmethod
    def list_cases(
        cls,
        module: str | list | None = None,
        priority: str | list | None = None,
        status: str | None = "active",
        case_type: str | None = None,
    ) -> list:
        """
        多维度用例查询

        筛选规则:
            - module    精确匹配（str或list任一命中）
            - priority  精确匹配、忽略大小写统一大写（str或list任一命中）
            - status    精确匹配（active/disabled），传None查全部状态
            - case_type 精确匹配（api/chip），传None不过滤

        排序规则:
            priority升序（P0→P3，未知优先级排最后） -> case_id升序

        参数:
            module (str | list | None): 模块筛选值，默认None不过滤
            priority (str | list | None): 优先级筛选值，默认None不过滤
            status (str | None): 用例状态，默认"active"
            case_type (str | None): 用例类型筛选值，默认None不过滤

        返回:
            list[dict]: 命中用例的字典列表（含全部模型字段，时间为ISO格式字符串）

        异常:
            CaseManagerError: 数据库查询异常时抛出（context携带operation定位）
        """
        try:
            session = DatabaseSession.get_session()
            try:
                query = session.query(TestCase)

                # module筛选（空值/空列表视为不过滤该维度）
                if module is not None:
                    module_list = cls._normalize_values(module, "module")
                    if module_list:
                        query = query.filter(TestCase.module.in_(module_list))

                # priority筛选（统一大写后匹配，与入库规范化格式对齐）
                if priority is not None:
                    priority_list = [
                        str(item).strip().upper()
                        for item in cls._normalize_values(priority, "priority")
                    ]
                    if priority_list:
                        query = query.filter(TestCase.priority.in_(priority_list))

                # status筛选（默认active，显式传None查全部状态）
                if status is not None and str(status).strip():
                    query = query.filter(TestCase.status == str(status).strip())

                # case_type筛选
                if case_type is not None and str(case_type).strip():
                    query = query.filter(
                        TestCase.case_type == str(case_type).strip()
                    )

                rows = query.all()
            finally:
                session.close()
        except SQLAlchemyError as exc:
            logger.error(f"用例查询数据库异常 | {exc}")
            raise CaseManagerError(
                f"用例查询数据库异常: {exc}",
                context={"operation": "list_cases"},
            ) from exc

        result = [cls._to_dict(row) for row in rows]
        # Python层排序: priority权重升序（P0→P3），同优先级按case_id升序
        result.sort(
            key=lambda case: (
                PRIORITY_ORDER.get(case["priority"], len(PRIORITY_ORDER)),
                case["case_id"],
            )
        )
        logger.info(f"用例查询完成 | 命中: {len(result)}条")
        return result

    # ------------------------------------------------------------------
    # 用例分页查询（第三阶段Day19，Web列表API专用）
    # ------------------------------------------------------------------
    @classmethod
    def list_cases_paged(
        cls,
        module: str | list | None = None,
        priority: str | list | None = None,
        case_type: str | None = None,
        status: str | None = "active",
        keyword: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """
        多维度分页用例查询（Web用例列表API专用）

        与list_cases的关系:
            - list_cases返回全量命中列表，供执行调度等内部链路使用，
              签名与行为保持不变（向后兼容）
            - 本方法面向Web分页场景，limit/offset在数据库层完成分页，
              避免大表全量加载到内存后切片的开销

        筛选规则（与list_cases口径一致）:
            - module    精确匹配（str或list任一命中）
            - priority  精确匹配、统一大写（str或list任一命中）
            - status    精确匹配（active/disabled），传None查全部状态
            - case_type 精确匹配（api/chip），传None不过滤
            - keyword   模糊匹配case_id/name/description三字段，
                        SQL LIKE实现，不区分大小写

        排序规则（与list_cases一致）:
            priority权重升序（P0→P3，未知优先级排最后） -> case_id升序；
            priority权重经SQL CASE表达式在数据库层映射（复用
            PRIORITY_ORDER常量），保证跨页顺序稳定

        参数:
            module (str | list | None): 模块筛选值，默认None不过滤
            priority (str | list | None): 优先级筛选值，默认None不过滤
            case_type (str | None): 用例类型（api/chip），默认None不过滤
            status (str | None): 用例状态，默认"active"，传None查全部
            keyword (str | None): 关键字（模糊匹配case_id/name/
                                  description三字段），默认None不过滤
            page (int): 页码，从1开始，默认1
            page_size (int): 每页条数，1到MAX_PAGE_SIZE，默认20

        返回:
            dict: {"items": 当前页用例列表(list[dict]),
                   "total": 命中总数（过滤后、分页前）,
                   "page": 当前页码,
                   "page_size": 每页条数,
                   "total_pages": 总页数，ceil(total/page_size)}

        异常:
            CaseManagerError: 分页参数非法 / 数据库查询异常时抛出，
                              context携带operation定位信息
        """
        # 分页参数防御校验（bool是int子类需显式排除；非法limit/offset
        # 会在数据库层直接报错，此处提前拦截给出业务友好提示）
        if not isinstance(page, int) or isinstance(page, bool) or not (1 <= page <= MAX_PAGE):
            raise CaseManagerError(
                f"page必须为1到{MAX_PAGE}之间的整数: {page!r}",
                context={"operation": "list_cases_paged", "page": page},
            )
        if (
            not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or page_size < 1
            or page_size > MAX_PAGE_SIZE
        ):
            raise CaseManagerError(
                f"page_size必须在1到{MAX_PAGE_SIZE}之间: {page_size!r}",
                context={
                    "operation": "list_cases_paged",
                    "page_size": page_size,
                },
            )

        try:
            session = DatabaseSession.get_session()
            try:
                query = session.query(TestCase)

                # module筛选（与list_cases口径一致）
                if module is not None:
                    module_list = cls._normalize_values(module, "module")
                    if module_list:
                        query = query.filter(TestCase.module.in_(module_list))

                # priority筛选（统一大写后匹配，与入库规范化格式对齐）
                if priority is not None:
                    priority_list = [
                        str(item).strip().upper()
                        for item in cls._normalize_values(priority, "priority")
                    ]
                    if priority_list:
                        query = query.filter(
                            TestCase.priority.in_(priority_list)
                        )

                # status筛选（默认active，显式传None查全部状态）
                if status is not None and str(status).strip():
                    query = query.filter(
                        TestCase.status == str(status).strip()
                    )

                # case_type筛选
                if case_type is not None and str(case_type).strip():
                    query = query.filter(
                        TestCase.case_type == str(case_type).strip()
                    )

                # keyword模糊搜索: case_id/name/description三字段任一命中，
                # 统一转小写实现跨库（SQLite/MySQL）不区分大小写匹配
                if keyword is not None and str(keyword).strip():
                    # LIKE 通配符转义（Day44 P2-12）：用户搜索框输入的
                    # "%" 与 "_" 是 LIKE 的元字符，不转义时 pattern 变成
                    # "%%%" 或 "%_%"，语义从"包含该字面串"退化为
                    # "匹配任意"——搜 '%' 会返回全表，与用户意图完全相反。
                    # escape="\\" 让反斜杠成为转义符，跨 SQLite/MySQL 一致。
                    escaped_keyword = _escape_like(str(keyword).strip().lower())
                    pattern = f"%{escaped_keyword}%"
                    query = query.filter(
                        or_(
                            func.lower(TestCase.case_id).like(pattern, escape="\\"),
                            func.lower(TestCase.name).like(pattern, escape="\\"),
                            func.lower(TestCase.description).like(
                                pattern, escape="\\"
                            ),
                        )
                    )

                # 命中总数（过滤后、分页前，独立count查询）
                total = query.count()

                # 排序: priority权重CASE表达式（复用PRIORITY_ORDER，与
                # list_cases的Python层排序口径一致）+ case_id升序
                priority_weight = case(
                    *[
                        (TestCase.priority == name, weight)
                        for name, weight in PRIORITY_ORDER.items()
                    ],
                    else_=len(PRIORITY_ORDER),
                )
                # 数据库层分页: limit/offset，不将全表拉入内存切片
                rows = (
                    query.order_by(priority_weight, TestCase.case_id.asc())
                    .limit(page_size)
                    .offset((page - 1) * page_size)
                    .all()
                )
            finally:
                session.close()
        except SQLAlchemyError as exc:
            logger.error(f"用例分页查询数据库异常 | {exc}")
            raise CaseManagerError(
                f"用例分页查询数据库异常: {exc}",
                context={"operation": "list_cases_paged"},
            ) from exc

        # 总页数: 整数运算实现向上取整（避免浮点精度问题），0条时为0页
        total_pages = (total + page_size - 1) // page_size
        result = {
            "items": [cls._to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
        logger.info(
            f"用例分页查询完成 | 命中: {total}条 | 页: {page}/{total_pages} | "
            f"每页: {page_size}"
        )
        return result

    # ------------------------------------------------------------------
    # 用例CRUD（第三阶段Day20，Web用例管理API专用）
    # ------------------------------------------------------------------
    @classmethod
    def get_case(cls, case_id: str) -> dict | None:
        """
        按业务编号查询单条用例

        查询规则:
            - 按业务编号case_id精确匹配（非自增主键id）
            - 不存在时返回None，由路由层负责转NotFoundError，
              核心层不感知HTTP语义

        参数:
            case_id (str): 业务用例编号，如TM-API-0001

        返回:
            dict | None: 命中时返回含全部字段的用例字典（时间为ISO
                         格式字符串）；未命中返回None

        异常:
            CaseManagerError: 数据库查询异常时抛出（context携带
                              operation与case_id定位信息）
        """
        # strip 归一化（Day44 P3-12）：原先只判空、查询用原值，
        # 传入 " TM-0001 " 会让守卫放行但按带空格值查库 → 未命中 →
        # 报"用例不存在"，而数据确实存在。
        case_id = str(case_id).strip()
        try:
            with DatabaseSession.session_scope() as session:
                row = (
                    session.query(TestCase).filter_by(case_id=case_id).first()
                )
                return cls._to_dict(row) if row is not None else None
        except SQLAlchemyError as exc:
            logger.error(f"用例详情查询数据库异常 | 用例: {case_id} | {exc}")
            raise CaseManagerError(
                f"用例详情查询数据库异常: {exc}",
                context={"operation": "get_case", "case_id": case_id},
            ) from exc

    @classmethod
    def create_case(cls, data: dict) -> dict:
        """
        创建用例

        执行流程:
            1. 防御校验case_id/name非空（路由层Schema已校验，两层各自兜底）
            2. priority统一转大写后入库（与list_cases查询口径对齐）
            3. case_id查重: 已存在则抛CaseManagerError（由路由层转
               ConflictError，防唯一约束在数据库层裸抛）
            4. 插入TestCase实例，flush+refresh取回库端生成字段
               （自增id/created_at/updated_at），提交后返回_to_dict结果
            5. source_ref 归一化（Day47）: 复用 core 层单一校验函数，
               空值归一为None（"不可被 pytest 执行"），非法值抛
               CaseManagerError——路由层 Schema 已先拦一道，此处是
               给内部调用方的兜底，与 case_id/name 的两层校验同口径

        参数:
            data (dict): 已校验的字段字典（case_id/name必填，
                         module/priority/case_type/status/description/
                         creator/source_ref可选，缺省走模型默认值）

        返回:
            dict: 新建用例的完整字段字典（含库端生成的
                  id/created_at/updated_at）

        异常:
            CaseManagerError: case_id或name为空 / case_id已存在 /
                              数据库操作异常时抛出，context携带
                              operation与case_id定位信息
        """
        # 防御校验: 必填字段非空（路由层Schema已拦截，此处兜底）
        case_id_value = str(data.get("case_id") or "").strip()
        name_value = str(data.get("name") or "").strip()
        if not case_id_value:
            raise CaseManagerError(
                "用例编号不能为空",
                context={"operation": "create_case"},
            )
        if not name_value:
            raise CaseManagerError(
                "用例名称不能为空",
                context={"operation": "create_case", "case_id": case_id_value},
            )

        # source_ref归一化（Day47，路由层Schema之外的第二道闸）
        try:
            source_ref_value = validate_source_ref(
                data.get("source_ref"), context=""
            )
        except ValueError as exc:
            raise CaseManagerError(
                str(exc),
                context={"operation": "create_case", "case_id": case_id_value},
            ) from exc

        # priority统一大写（与list_cases/list_cases_paged查询口径对齐）
        payload = dict(data)
        payload["case_id"] = case_id_value
        payload["name"] = name_value
        payload["priority"] = str(payload.get("priority", "P2")).strip().upper()

        try:
            with DatabaseSession.session_scope() as session:
                # 查重: 唯一冲突提前转为业务异常（防数据库层裸抛IntegrityError）
                existing = (
                    session.query(TestCase)
                    .filter_by(case_id=case_id_value)
                    .first()
                )
                if existing is not None:
                    raise CaseConflictError(
                        "用例编号已存在",
                        context={
                            "operation": "create_case",
                            "case_id": case_id_value,
                        },
                    )

                case = TestCase(
                    case_id=case_id_value,
                    name=name_value,
                    module=payload.get("module", "default"),
                    priority=payload["priority"],
                    case_type=payload.get("case_type", "api"),
                    status=payload.get("status", "active"),
                    description=payload.get("description", ""),
                    creator=payload.get("creator", "admin"),
                    # 归一化后落库（None = 未配置执行目标）
                    source_ref=source_ref_value,
                )
                session.add(case)
                # flush触发INSERT，refresh取回库端生成字段
                # （自增id/created_at），保证_to_dict字段完整
                session.flush()
                session.refresh(case)
                result = cls._to_dict(case)
        except IntegrityError as exc:
            # 并发窗口: 上面的"先查重再插入"是 check-then-act，两个并发
            # POST 同编号时双方查重都未命中，由DB唯一约束在flush阶段拦截。
            # 修复前走通用 SQLAlchemyError 分支 → 客户端收 500，且 str(exc)
            # 含完整 INSERT 语句与全部列值（进日志、进 TESTING 响应体）。
            # 识别为唯一性冲突并转成与串行路径相同的业务语义。
            #
            # 但 IntegrityError 覆盖**全部**完整性违例，不止唯一约束
            # （v3 修复 V2-P2-2/P2-4）: 把所有 IntegrityError 一律翻译成
            # "用例编号已存在"的 409，会掩盖真因（CHECK 违例、外键违例、
            # 将来加的约束），排障方向被带偏。故此处只对**唯一约束**判定。
            #
            # 可达性说明（v3 实测更正审查报告的表述）: 审查报告以
            # "module 传 None -> NOT NULL 违例"举例，但这条路**当前不可达**
            # ——test_cases 的每个 nullable=False 列都带 Python 侧 default=，
            # 实测显式传 module=None 时 SQLAlchemy 会套用 default="default"
            # 落库，根本不产生 NOT NULL 违例。本收窄的价值因此是
            # 防御性的（覆盖 CHECK/外键/未来新增约束），而非修一个当前
            # 可触发的缺陷；判定逻辑本身由
            # test_v3_robustness_demo.py 直接注入异常验证。
            if not _is_unique_case_id_violation(exc):
                # 完整异常（含 INSERT 语句与全部绑定列值）只进日志，
                # 不进异常 message：路由只翻译 CaseConflictError，泛型
                # CaseManagerError 会原样上抛到全局 Exception 处理器，
                # 而该处理器在 TESTING 下 `return error(str(exc), 500)`
                # 会把 SQL 与列值回显进响应体（v5 审查 V3-P3-6）
                logger.error(
                    f"用例创建完整性违例（非唯一约束）| 用例: {case_id_value} | "
                    f"{type(exc).__name__}: {exc}"
                )
                raise CaseManagerError(
                    "用例创建完整性违例（检查字段是否为空/超长），"
                    "详情见服务端日志",
                    context={
                        "operation": "create_case",
                        "case_id": case_id_value,
                    },
                ) from exc
            logger.warning(
                f"用例创建并发编号冲突（唯一约束拦截）| 用例: {case_id_value}"
            )
            raise CaseConflictError(
                "用例编号已存在",
                context={"operation": "create_case", "case_id": case_id_value},
            ) from exc
        except SQLAlchemyError as exc:
            logger.error(
                f"用例创建数据库异常 | 用例: {case_id_value} | {exc}"
            )
            raise CaseManagerError(
                f"用例创建数据库异常: {exc}",
                context={"operation": "create_case", "case_id": case_id_value},
            ) from exc

        logger.info(
            f"用例已创建 | 编号: {case_id_value} | 名称: {name_value} | "
            f"优先级: {payload['priority']}"
        )
        # Day31: 创建成功后失效用例列表缓存（统一_safe_invalidate兜底）
        _invalidate_case_related_caches("用例创建")
        return result

    @classmethod
    def update_case(cls, case_id: str, data: dict) -> dict:
        """
        按业务编号更新用例（只更新传入字段）

        更新规则:
            - 未传字段保持原值不变（逐字段setattr，不做全量覆盖）
            - case_id业务编号不可变（id/case_id/created_at防御性剔除，
              路由层UpdateSchema同样不含case_id，两层保障）
            - 仅接受 UPDATABLE_CASE_FIELDS 白名单内的字段；白名单外
              的字段记 warning 后忽略（不落库、不报错），详见该常量
              处的说明。HTTP 路径上路由层已用 unknown=EXCLUDE 先剥掉，
              这里是给内部调用方的第二道闸
            - priority若传入则统一转大写（与查询口径对齐）
            - source_ref若传入则统一走 core 层校验函数归一化（Day47），
              空串/None 归一为"未配置执行目标"
            - updated_at由模型onupdate=func.now()自动刷新，
              无需手动设置

        参数:
            case_id (str): 业务用例编号（URL路径参数定位目标用例）
            data (dict): 待更新字段字典（name/module/priority/case_type/
                         status/description/creator/source_ref任意子集）

        返回:
            dict: 更新后用例的完整字段字典

        异常:
            CaseManagerError: case_id为空 / 用例不存在 / 数据库操作
                              异常时抛出，context携带operation与
                              case_id定位信息
        """
        if not case_id or not str(case_id).strip():
            raise CaseManagerError(
                "用例编号不能为空",
                context={"operation": "update_case"},
            )

        # 防御性剔除不可变字段（业务编号/主键/创建时间不随更新变化）
        payload = dict(data)
        for immutable_field in _IMMUTABLE_CASE_FIELDS:
            payload.pop(immutable_field, None)
        # 白名单外的字段不落库。HTTP 路径上 CaseUpdateSchema 已用
        # unknown=EXCLUDE 剥掉它们，此处是给内部调用方的第二道闸：
        # 没有它，未知字段会被 setattr 静默丢弃且无任何痕迹
        unknown_fields = sorted(set(payload) - UPDATABLE_CASE_FIELDS)
        if unknown_fields:
            logger.warning(
                f"用例更新含未知字段，已忽略（该字段不是TestCase列）| "
                f"用例: {case_id} | 字段: {unknown_fields}"
            )
            for unknown_field in unknown_fields:
                payload.pop(unknown_field, None)
        # priority统一大写（与list_cases/list_cases_paged查询口径对齐）
        if "priority" in payload:
            payload["priority"] = str(payload["priority"]).strip().upper()
        # source_ref归一化（Day47，与 create_case 同一校验函数）
        # 显式传 null/空串归一为 None = "清空执行目标"，这是补录时的
        # 正常动作（此前配错了路径需要撤销），故照写而非忽略
        if "source_ref" in payload:
            try:
                payload["source_ref"] = validate_source_ref(
                    payload["source_ref"], context=""
                )
            except ValueError as exc:
                raise CaseManagerError(
                    str(exc),
                    context={"operation": "update_case", "case_id": case_id},
                ) from exc

        # strip 归一化（Day44 P3-12，与 get_case/delete_case 同口径）
        case_id = str(case_id).strip()

        try:
            with DatabaseSession.session_scope() as session:
                row = (
                    session.query(TestCase).filter_by(case_id=case_id).first()
                )
                if row is None:
                    raise CaseNotFoundError(
                        "用例不存在",
                        context={"operation": "update_case", "case_id": case_id},
                    )
                for field, value in payload.items():
                    setattr(row, field, value)
                # flush触发UPDATE（onupdate自动刷新updated_at），
                # refresh取回库端最新值
                session.flush()
                session.refresh(row)
                result = cls._to_dict(row)
        except SQLAlchemyError as exc:
            logger.error(f"用例更新数据库异常 | 用例: {case_id} | {exc}")
            raise CaseManagerError(
                f"用例更新数据库异常: {exc}",
                context={"operation": "update_case", "case_id": case_id},
            ) from exc

        logger.info(
            f"用例已更新 | 编号: {case_id} | 更新字段: {sorted(payload.keys())}"
        )
        # Day31: 更新成功后失效用例列表缓存（统一_safe_invalidate兜底）
        _invalidate_case_related_caches("用例更新")
        return result

    @classmethod
    def delete_case(cls, case_id: str) -> bool:
        """
        按业务编号物理删除用例

        删除规则:
            - 物理删除（DELETE行级删除，非status=disabled软删除）
            - 不存在时抛CaseManagerError（由路由层转NotFoundError）

        参数:
            case_id (str): 业务用例编号

        返回:
            bool: 删除成功返回True（不存在时抛异常，不返回False）

        异常:
            CaseManagerError: case_id为空 / 用例不存在 / 数据库操作
                              异常时抛出，context携带operation与
                              case_id定位信息
        """
        if not case_id or not str(case_id).strip():
            raise CaseManagerError(
                "用例编号不能为空",
                context={"operation": "delete_case"},
            )

        # strip 归一化（Day44 P3-12，与 get_case/update_case 同口径）
        case_id = str(case_id).strip()

        try:
            with DatabaseSession.session_scope() as session:
                row = (
                    session.query(TestCase).filter_by(case_id=case_id).first()
                )
                if row is None:
                    raise CaseNotFoundError(
                        "用例不存在",
                        context={"operation": "delete_case", "case_id": case_id},
                    )
                session.delete(row)
        except SQLAlchemyError as exc:
            logger.error(f"用例删除数据库异常 | 用例: {case_id} | {exc}")
            raise CaseManagerError(
                f"用例删除数据库异常: {exc}",
                context={"operation": "delete_case", "case_id": case_id},
            ) from exc

        logger.info(f"用例已删除 | 编号: {case_id}")
        # Day31: 删除成功后失效用例列表缓存（统一_safe_invalidate兜底）
        _invalidate_case_related_caches("用例删除")
        return True

    # ------------------------------------------------------------------
    # 执行调度（第二阶段Day2）
    # ------------------------------------------------------------------
    @classmethod
    def create_execution(
        cls,
        trigger: str = "manual",
        executor: str = "local",
        environment: str = "dev",
        remark: str | None = None,
    ) -> str:
        """
        创建测试执行批次

        校验trigger合法值后生成批次号，批次元信息（触发方式/执行人/
        环境/备注）全量记录日志，作为批次生命周期的起点。

        参数:
            trigger (str): 触发方式，可选manual/cli/web/ci，默认"manual"
            executor (str): 执行人（人工姓名或CI标识，如jenkins），默认"local"
            environment (str): 执行环境（dev/test/prod），默认"dev"
            remark (str | None): 批次备注（如回归范围说明），默认None

        返回:
            str: 进程内唯一的执行批次号，格式RUN-YYYYMMDD-HHMMSS-xxxx

        异常:
            CaseManagerError: trigger为空或不在合法值集合（manual/cli/web/ci）时抛出，
                              context携带operation与入参值
        """
        if not trigger or trigger not in VALID_TRIGGERS:
            raise CaseManagerError(
                f"触发方式非法: {trigger!r}，合法取值: {list(VALID_TRIGGERS)}",
                context={"operation": "create_execution", "trigger": trigger},
            )

        execution_id = generate_execution_id()
        logger.info(
            f"执行批次已创建 | 批次号: {execution_id} | 触发方式: {trigger} | "
            f"执行人: {executor} | 环境: {environment} | 备注: {remark or '-'}"
        )
        return execution_id

    @classmethod
    def select_cases_for_execution(
        cls,
        module: str | list | None = None,
        priority: str | list | None = None,
        tags: str | list | None = None,
        case_type: str = "api",
    ) -> list:
        """
        筛选待执行用例（执行调度专用）

        执行流程:
            1. 复用list_cases查询status="active"且case_type匹配的用例
               （已按priority升序+case_id升序排列，不重复实现查询逻辑）
            2. tags非None时逐条解析description中的"标签: xxx,xxx"暂存格式，
               与筛选tags取交集，无交集的用例剔除（保持原排序不变）

        参数:
            module (str | list | None): 模块筛选值，默认None不过滤
            priority (str | list | None): 优先级筛选值，默认None不过滤
            tags (str | list | None): 标签筛选值（任一命中即保留），默认None不过滤
            case_type (str): 用例类型（api/chip），默认"api"

        返回:
            list[dict]: 命中筛选条件的待执行用例列表（priority升序再case_id升序）

        异常:
            无（底层list_cases的数据库异常已包装为CaseManagerError向上抛出）
        """
        # 复用list_cases: active状态 + case_type + module/priority多维度筛选
        cases = cls.list_cases(
            module=module, priority=priority, status="active", case_type=case_type
        )

        # tags维度: description暂存格式解析后交集匹配
        if tags is not None:
            tag_list = cls._normalize_values(tags, "tags")
            if tag_list:
                cases = [
                    case for case in cases
                    if set(cls._parse_tags_from_description(case["description"]))
                    & set(tag_list)
                ]
            logger.info(
                f"待执行用例标签筛选 | 筛选标签: {tag_list or '-'} | "
                f"筛选后剩余: {len(cases)}条"
            )

        logger.info(
            f"待执行用例筛选完成 | case_type: {case_type} | 命中: {len(cases)}条"
        )
        return cases

    @staticmethod
    def _normalize_batch_id(execution_id: object, operation: str) -> str:
        """
        批次号归一（所有批次号入口的统一口径）

        修复前只有 `record_execution` 做 strip，其余入口各写各的：
        `finish_execution` / `get_execution_detail` / `get_execution_status`
        只校验"非空白"却**仍用带空白的原值**去 filter_by，
        `_update_batch_status` / `build_notification_statistics` 连校验都没有。
        于是 `" TM-0001 "` 能通过空值守卫，却按带空格值查询——明细按干净
        号落库、状态更新按带空格号查不到（只 warning 返回 None，静默丢失），
        最后 `finish_execution` 抛"批次不存在"、整批判 failed。
        根因与 7.45 同源：同一口径散落多处即多处漂移。

        参数:
            execution_id (object): 原始批次号（允许非 str 入参）
            operation (str): 调用方操作名，写入异常 context 便于定位

        返回:
            str: strip 后的批次号；空值已在上一行被拒

        异常:
            CaseManagerError: 批次号为空或全是空白时抛出（统一转 400）
        """
        normalized = str(execution_id or "").strip()
        if not normalized:
            raise CaseManagerError(
                "执行批次号不能为空",
                context={"operation": operation},
            )
        return normalized

    @classmethod
    def record_execution(
        cls,
        execution_id: str,
        case_id: str,
        case_name: str,
        result: str,
        start_time: datetime,
        end_time: datetime,
        duration: float,
        error_message: str | None = None,
        environment: str | None = None,
        executor: str | None = None,
    ) -> None:
        """
        记录单条用例执行结果

        校验result合法性及error_message必填规则后，将单用例执行明细
        写入test_executions表（session_scope自动提交/回滚/关闭）。

        参数:
            execution_id (str): 执行批次号（由create_execution生成）
            case_id (str): 业务用例编号
            case_name (str): 用例名称（冗余存储，防止用例表变更影响历史记录）
            result (str): 执行结果，可选passed/failed/error/skipped
            start_time (datetime): 用例开始执行时间
            end_time (datetime): 用例结束执行时间
            duration (float): 执行耗时（秒，支持亚秒精度）
            error_message (str | None): 失败/错误的异常信息，
                                        result为failed/error时必填
            environment (str | None): 执行环境（dev/test/prod）。缺省时
                                     从批次行读取该批次的真实环境；批次行
                                     也不存在时回落到模型默认值
                                     （Day44 P2-03：修复前明细恒为 dev/local，
                                     与批次元数据矛盾——以 prod 触发时批次
                                     显示 prod 而每条明细显示 dev）
            executor (str | None): 执行器类型（simulated/pytest 等），
                                   缺省处理口径同 environment

        返回:
            None

        异常:
            CaseManagerError: result非法 / 批次号或用例编号为空 /
                              failed/error时error_message缺失 /
                              数据库操作异常时抛出，context携带operation定位
        """
        # 批次号与用例编号基础校验
        # strip 归一化（Day44 P3-12）：原先只判空、查询/写入用原值，
        # 传入 " TM-0001 " 会让守卫放行但按带空格值查询/落库——写进去的
        # 批次号带空格，后续按干净批次号聚合时查不到明细。
        # 批次号归一走统一口径（Day45 第 4 批 P3-1）：守卫与落库值必须
        # 是同一个字符串，否则明细按干净号写、后续按带空格号查不到
        execution_id = cls._normalize_batch_id(execution_id, "record_execution")
        if not case_id or not str(case_id).strip():
            raise CaseManagerError(
                "用例编号不能为空",
                context={"operation": "record_execution", "execution_id": execution_id},
            )
        case_id = str(case_id).strip()

        # result合法性校验
        if result not in VALID_RESULTS:
            raise CaseManagerError(
                f"执行结果非法: {result!r}，合法取值: {list(VALID_RESULTS)}",
                context={
                    "operation": "record_execution",
                    "execution_id": execution_id,
                    "case_id": case_id,
                    "result": result,
                },
            )

        # failed/error时error_message必填（非空字符串）
        if result in ("failed", "error"):
            if not error_message or not str(error_message).strip():
                raise CaseManagerError(
                    f"执行结果为'{result}'时error_message必填",
                    context={
                        "operation": "record_execution",
                        "execution_id": execution_id,
                        "case_id": case_id,
                        "result": result,
                    },
                )

        try:
            with DatabaseSession.session_scope() as session:
                # 明细去重（Day45 全量审查 问题2 第二道防线）：同一
                # (execution_id, case_id) 只允许一条明细。
                #
                # 幂等抢占是首道防线，但它是"尽力而为"——抢占本身故障时
                # 会按放行处理（保护机制不可用时不能让执行链路停摆）。
                # 没有第二道防线时，一次抢占失效就会让明细双写，
                # finish_execution 按 execution_id 聚合**全部**明细，
                # 计数翻倍、通过率被污染，且这种污染在库表里不可逆。
                # 放在写入前查一次，代价是一行 SELECT，换来明细不可重复。
                existing = (
                    session.query(TestExecution.id)
                    .filter_by(execution_id=execution_id, case_id=case_id)
                    .first()
                )
                if existing is not None:
                    logger.warning(
                        f"执行明细已存在，跳过重复写入（防同一批次双跑污染明细） | "
                        f"批次: {execution_id} | 用例: {case_id} | "
                        f"结果: {result} | 既有明细id: {existing.id}"
                    )
                    return
                # environment/executor 缺省时从批次行取真实值（Day44 P2-03）。
                # 批次行也不存在（历史/CLI 直调路径）则留 None，交给 ORM 的
                # 列默认值，与修复前的行为保持一致，不制造新的数据缺口。
                resolved_env = environment
                resolved_executor = executor
                if resolved_env is None or resolved_executor is None:
                    batch_row = (
                        session.query(TestExecutionBatch)
                        .filter_by(execution_id=execution_id)
                        .first()
                    )
                    if batch_row is not None:
                        if resolved_env is None:
                            resolved_env = batch_row.environment
                        if resolved_executor is None:
                            resolved_executor = batch_row.executor
                row_kwargs: dict[str, Any] = {
                    "execution_id": execution_id,
                    "case_id": case_id,
                    "case_name": case_name,
                    "result": result,
                    "start_time": start_time,
                    "end_time": end_time,
                    "duration": duration,
                    "error_message": error_message,
                }
                if resolved_env is not None:
                    row_kwargs["environment"] = resolved_env
                if resolved_executor is not None:
                    row_kwargs["executor"] = resolved_executor
                session.add(TestExecution(**row_kwargs))
        except SQLAlchemyError as exc:
            logger.error(
                f"执行结果入库数据库异常 | 批次: {execution_id} | "
                f"用例: {case_id} | {exc}"
            )
            raise CaseManagerError(
                f"执行结果入库数据库异常: {exc}",
                context={
                    "operation": "record_execution",
                    "execution_id": execution_id,
                    "case_id": case_id,
                },
            ) from exc

        logger.info(
            f"执行结果已入库 | 批次: {execution_id} | 用例: {case_id} | "
            f"结果: {result} | 耗时: {duration:.3f}s"
        )

    @classmethod
    def _aggregate_execution_results(
        cls, execution_id: str, operation: str
    ) -> dict[str, Any] | None:
        """
        聚合批次执行明细并 upsert 到 defect_statistics（内部方法）

        提取自 `finish_execution`（Day45 第 4 批 P2-1）。修复前聚合逻辑
        只长在"正常完成"路径里，failed 分支仅改状态不聚合，于是
        `defect_statistics` 缺行——而 `list_executions_paged` 只读该表，
        结果是**批次从列表彻底消失**，而 `get_execution_status` 仍显示
        failed + 计数全 0。同一套聚合口径散在两条路径上，就是这种
        "一条路径补齐、另一条路径漏掉"的温床（7.45 同源）。

        与 `finish_execution` 的差别只有一处：**无记录时返回 None 而不是
        抛错**。finished 路径据此报"批次不存在"，failed 路径据此降级为
        "无明细可聚合"（异常可能发生在第一条用例执行之前）。

        参数:
            execution_id (str): 执行批次号（调用方须已归一）
            operation (str): 操作名，写入异常 context 与日志

        返回:
            dict[str, Any] | None: {"execution_id", "total", "passed", "failed",
            "error", "skipped", "pass_rate"}；无任何执行记录时返回 None

        异常:
            CaseManagerError: 数据库操作异常时抛出（context 携带 operation）
        """
        try:
            with DatabaseSession.session_scope() as session:
                records = (
                    session.query(TestExecution)
                    .filter_by(execution_id=execution_id)
                    .all()
                )
                if not records:
                    return None

                # 结果计数聚合
                # total 恒 > 0：上一步已对空 records 返回 None，故除零
                # 不变量由该 early-return 保证。此处直接相除即可（Day44 P3-08：
                # 原先写的是 `if total else 0.0`，else 分支永不可达，会误导
                # 后续维护者以为空批次能走到这里、并掩盖"空批次已在上一行
                # 被拒"这一真实契约）。
                total = len(records)
                passed = sum(1 for r in records if r.result == "passed")
                failed = sum(1 for r in records if r.result == "failed")
                error = sum(1 for r in records if r.result == "error")
                skipped = sum(1 for r in records if r.result == "skipped")
                pass_rate = round(passed / total, 4)

                # upsert到defect_statistics（execution_id唯一键）
                statistic = (
                    session.query(DefectStatistic)
                    .filter_by(execution_id=execution_id)
                    .first()
                )
                if statistic is not None:
                    statistic.total_cases = total
                    statistic.passed = passed
                    statistic.failed = failed
                    statistic.error = error
                    statistic.skipped = skipped
                    statistic.pass_rate = pass_rate
                else:
                    session.add(
                        DefectStatistic(
                            execution_id=execution_id,
                            total_cases=total,
                            passed=passed,
                            failed=failed,
                            error=error,
                            skipped=skipped,
                            pass_rate=pass_rate,
                        )
                    )
        except SQLAlchemyError as exc:
            logger.error(f"批次汇总数据库异常 | 批次: {execution_id} | {exc}")
            raise CaseManagerError(
                f"批次汇总数据库异常: {exc}",
                context={"operation": operation, "execution_id": execution_id},
            ) from exc

        summary = {
            "execution_id": execution_id,
            "total": total,
            "passed": passed,
            "failed": failed,
            "error": error,
            "skipped": skipped,
            "pass_rate": pass_rate,
        }
        logger.info(
            f"执行批次汇总完成 | 批次: {execution_id} | 总数: {total} | "
            f"通过: {passed} | 失败: {failed} | 错误: {error} | "
            f"跳过: {skipped} | 通过率: {pass_rate:.2%}"
        )
        return summary

    @classmethod
    def finish_execution(cls, execution_id: str) -> dict[str, Any]:
        """
        完成执行批次并生成汇总统计

        执行流程:
            1. 批次号归一（strip + 空值拒绝）
            2. 委托 `_aggregate_execution_results` 完成查询/聚合/upsert
            3. 无任何执行记录视为批次不存在，抛CaseNotFoundError
            4. 返回统计字典

        参数:
            execution_id (str): 执行批次号

        返回:
            dict: {"execution_id", "total", "passed", "failed", "error",
                   "skipped", "pass_rate"}

        异常:
            CaseManagerError: 批次号为空 / 批次不存在（无执行记录） /
                              数据库操作异常时抛出，context携带operation定位
        """
        execution_id = cls._normalize_batch_id(execution_id, "finish_execution")
        summary = cls._aggregate_execution_results(execution_id, "finish_execution")
        if summary is None:
            raise CaseNotFoundError(
                f"执行批次不存在或无任何执行记录: {execution_id}",
                context={
                    "operation": "finish_execution",
                    "execution_id": execution_id,
                },
            )
        return summary

    # ------------------------------------------------------------------
    # 执行记录查询（第三阶段Day22，Web执行记录查询API专用）
    # ------------------------------------------------------------------
    @classmethod
    def list_executions_paged(cls, page: int = 1, page_size: int = 20) -> dict:
        """
        执行批次分页查询（Web执行批次列表API专用）

        数据口径:
            只返回已完成批次（finish_execution后才有defect_statistics
            汇总记录），未finish的执行中批次不在本接口范围

        排序规则:
            created_at倒序（最新完成批次在前）+ execution_id倒序，
            双字段排序保证同秒完成的批次跨页次序稳定

        状态字段:
            每个 item 附 status（来自 test_execution_batches 的真实状态）。
            两步非原子写入（先汇总行后批次行）失败时会留下"有汇总行但
            批次行非终态"的孤儿批次，前端据此显示真实状态而非恒定
            "已完成"；批次行缺失（Day24 建表前历史批次）时兜底
            "finished"——有汇总行即视为已完成，是本接口的既有不变量

        参数:
            page (int): 页码，从1开始，默认1
            page_size (int): 每页条数，1到MAX_PAGE_SIZE，默认20

        返回:
            dict: {"items": 当前页批次汇总列表(list[dict], 每项含
                   execution_id/total_cases/passed/failed/error/skipped/
                   pass_rate/created_at/status),
                   "total": 已完成批次总数,
                   "page": 当前页码,
                   "page_size": 每页条数,
                   "total_pages": 总页数，ceil(total/page_size)}

        异常:
            CaseManagerError: 分页参数非法 / 数据库查询异常时抛出，
                              context携带operation定位信息
        """
        # 分页参数防御校验（口径与list_cases_paged一致: bool是int子类
        # 需显式排除，非法limit/offset提前拦截给出业务友好提示；
        # page 上界见 MAX_PAGE，Day44 P2-07）
        if not isinstance(page, int) or isinstance(page, bool) or not (1 <= page <= MAX_PAGE):
            raise CaseManagerError(
                f"page必须为1到{MAX_PAGE}之间的整数: {page!r}",
                context={"operation": "list_executions_paged", "page": page},
            )
        if (
            not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or page_size < 1
            or page_size > MAX_PAGE_SIZE
        ):
            raise CaseManagerError(
                f"page_size必须在1到{MAX_PAGE_SIZE}之间: {page_size!r}",
                context={
                    "operation": "list_executions_paged",
                    "page_size": page_size,
                },
            )

        # 批次真实状态映射（Day44 收尾）: 列表口径虽然只查 defect_statistics
        # （有汇总行即视为已完成），但核心层 finish_execution（写汇总行）与
        # _update_batch_status（更新批次行）是**两步非原子**——前者成功后
        # 后者失败会留下"有汇总行但批次行非终态"的孤儿批次。列表项附真实
        # status，前端状态列不再硬编码"已完成"。
        status_map: dict[str, str] = {}
        try:
            session = DatabaseSession.get_session()
            try:
                # 已完成批次总数（独立count查询，空表时为0不报错）
                total = session.query(DefectStatistic).count()

                # 数据库层分页: created_at倒序（最新完成批次在前）+
                # execution_id倒序做次级排序，保证同秒批次跨页次序稳定
                rows = (
                    session.query(DefectStatistic)
                    .order_by(
                        DefectStatistic.created_at.desc(),
                        DefectStatistic.execution_id.desc(),
                    )
                    .limit(page_size)
                    .offset((page - 1) * page_size)
                    .all()
                )

                # 批量取本页批次的真实状态（一次 in_ 查询，不逐行查库）。
                # 空列表不查：SQLAlchemy 对空 IN 子句的方言支持不一致
                execution_ids = [row.execution_id for row in rows]
                if execution_ids:
                    batch_rows = (
                        session.query(TestExecutionBatch)
                        .filter(
                            TestExecutionBatch.execution_id.in_(execution_ids)
                        )
                        .all()
                    )
                    status_map = {
                        batch.execution_id: batch.status
                        for batch in batch_rows
                    }
            finally:
                session.close()
        except SQLAlchemyError as exc:
            logger.error(f"执行批次分页查询数据库异常 | {exc}")
            raise CaseManagerError(
                f"执行批次分页查询数据库异常: {exc}",
                context={"operation": "list_executions_paged"},
            ) from exc

        # 总页数: 整数运算实现向上取整（与list_cases_paged口径一致），
        # 0条时为0页
        total_pages = (total + page_size - 1) // page_size
        # 批次行缺失时按"有汇总行即已完成"兜底（Day24 建表前的历史批次）
        items = []
        for row in rows:
            item = cls._execution_summary_to_dict(row)
            item["status"] = status_map.get(row.execution_id, "finished")
            items.append(item)
        result = {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }
        logger.info(
            f"执行批次分页查询完成 | 命中: {total}个批次 | "
            f"页: {page}/{total_pages} | 每页: {page_size}"
        )
        return result

    @classmethod
    def get_execution_detail(cls, execution_id: str) -> dict | None:
        """
        查询执行批次详情（汇总统计 + 单用例执行明细）

        执行流程:
            1. 查defect_statistics汇总记录，不存在返回None
               （路由层据此转404；未finish的执行中批次无汇总记录）
            2. 查test_executions该批次全部明细，按id升序
               （与record_execution写入顺序一致，即执行先后顺序）
            3. 汇总与明细组装为summary/items双层数据结构返回

        参数:
            execution_id (str): 执行批次号

        返回:
            dict | None: {"summary": 批次汇总字典,
                          "items": 单用例明细列表(list[dict])}；
                         批次不存在（无汇总记录）时返回None

        异常:
            CaseManagerError: 批次号为空 / 数据库查询异常时抛出，
                              context携带operation定位信息
        """
        # 批次号基础校验（空字符串/空白串直接拒绝）+ 归一
        execution_id = cls._normalize_batch_id(
            execution_id, "get_execution_detail"
        )

        try:
            session = DatabaseSession.get_session()
            try:
                # 先查汇总记录（批次完成的标志），无记录即批次不存在
                summary_row = (
                    session.query(DefectStatistic)
                    .filter_by(execution_id=execution_id)
                    .first()
                )
                if summary_row is None:
                    logger.debug(
                        f"执行批次不存在（无汇总记录）| 批次: {execution_id}"
                    )
                    return None

                # 再查明细: 按id升序保持执行先后顺序
                records = (
                    session.query(TestExecution)
                    .filter_by(execution_id=execution_id)
                    .order_by(TestExecution.id.asc())
                    .all()
                )
            finally:
                session.close()
        except SQLAlchemyError as exc:
            logger.error(
                f"执行批次详情查询数据库异常 | 批次: {execution_id} | {exc}"
            )
            raise CaseManagerError(
                f"执行批次详情查询数据库异常: {exc}",
                context={
                    "operation": "get_execution_detail",
                    "execution_id": execution_id,
                },
            ) from exc

        result = {
            "summary": cls._execution_summary_to_dict(summary_row),
            "items": [
                cls._execution_record_to_dict(record) for record in records
            ],
        }
        logger.info(
            f"执行批次详情查询完成 | 批次: {execution_id} | "
            f"明细: {len(records)}条"
        )
        return result

    @staticmethod
    def _execution_summary_to_dict(row: DefectStatistic) -> dict:
        """
        DefectStatistic模型行转字典（内部方法）

        参数:
            row (DefectStatistic): 批次汇总统计模型实例

        返回:
            dict: 批次汇总字段字典（created_at转ISO格式字符串，
                  便于JSON序列化）

        异常:
            无
        """
        return {
            "execution_id": row.execution_id,
            "total_cases": row.total_cases,
            "passed": row.passed,
            "failed": row.failed,
            "error": row.error,
            "skipped": row.skipped,
            "pass_rate": row.pass_rate,
            "created_at": to_utc_iso(row.created_at),
        }

    @staticmethod
    def _execution_record_to_dict(row: TestExecution) -> dict:
        """
        TestExecution模型行转字典（内部方法）

        参数:
            row (TestExecution): 单用例执行明细模型实例

        返回:
            dict: 单用例明细全量字段字典；start_time/end_time/
                  error_message可空字段原样保留None（不转空串，
                  保持与库内一致的空值语义，error_message即失败
                  堆栈原样透传不截断）；datetime字段转ISO格式
                  字符串便于JSON序列化

        异常:
            无
        """
        return {
            "id": row.id,
            "execution_id": row.execution_id,
            "case_id": row.case_id,
            "case_name": row.case_name,
            "result": row.result,
            "start_time": local_to_utc_iso(row.start_time),
            "end_time": local_to_utc_iso(row.end_time),
            "duration": row.duration,
            "error_message": row.error_message,
            "environment": row.environment,
            "executor": row.executor,
            "created_at": to_utc_iso(row.created_at),
        }

    # ------------------------------------------------------------------
    # 异步执行编排（第三阶段Day24，Web触发执行API专用）
    # ------------------------------------------------------------------
    @classmethod
    def start_execution(
        cls,
        trigger: str,
        executor_name: str = "local",
        environment: str = "dev",
        remark: str | None = None,
        module: str | list | None = None,
        priority: str | list | None = None,
        tags: str | list | None = None,
        case_type: str = "api",
    ) -> dict:
        """
        启动执行批次（筛选用例 + 建批次行，不执行）

        执行流程:
            1. select_cases_for_execution筛选待执行用例（active状态 +
               多维过滤），空列表抛CaseManagerError（路由层转400）
            2. create_execution生成批次号（trigger合法性校验内置）
            3. 写test_execution_batches行: status="pending" +
               total_cases=用例数（批次元信息持久化，进程重启后
               状态仍可查询；内存字典方案重启即丢，不采用）

        参数:
            trigger (str): 触发方式，可选manual/cli/web/ci
            executor_name (str): 执行人（人工姓名或CI标识），默认"local"
            environment (str): 执行环境（dev/test/prod），默认"dev"
            remark (str | None): 批次备注，默认None
            module (str | list | None): 模块筛选值，默认None不过滤
            priority (str | list | None): 优先级筛选值，默认None不过滤
            tags (str | list | None): 标签筛选值，默认None不过滤
            case_type (str): 用例类型（api/chip），默认"api"

        返回:
            dict: {"execution_id": 批次号, "total_cases": 用例数,
                   "status": "pending", "cases": 用例字典列表}；
                   cases供后台线程执行使用（纯字典无ORM对象，
                   跨线程传递安全），路由层不序列化给客户端

        异常:
            CaseManagerError: 无符合条件的用例 / trigger非法 /
                              批次元信息落库数据库异常时抛出，
                              context携带operation定位
        """
        # 1. 筛选待执行用例（空列表直接拒绝，无意义的空批次不落库）
        cases = cls.select_cases_for_execution(
            module=module, priority=priority, tags=tags, case_type=case_type
        )
        if not cases:
            raise NoCasesSelectedError(
                "无符合条件的用例可执行",
                context={
                    "operation": "start_execution",
                    "module": module,
                    "priority": priority,
                    "case_type": case_type,
                },
            )

        # 2. 生成批次号（trigger合法性由create_execution内置校验）
        execution_id = cls.create_execution(
            trigger=trigger,
            executor=executor_name,
            environment=environment,
            remark=remark,
        )

        # 3. 批次元信息落库（状态机起点pending）
        try:
            with DatabaseSession.session_scope() as session:
                session.add(
                    TestExecutionBatch(
                        execution_id=execution_id,
                        trigger=trigger,
                        executor=executor_name,
                        environment=environment,
                        remark=remark,
                        status="pending",
                        total_cases=len(cases),
                    )
                )
        except SQLAlchemyError as exc:
            logger.error(
                f"批次元信息入库数据库异常 | 批次: {execution_id} | {exc}"
            )
            raise CaseManagerError(
                f"批次元信息入库数据库异常: {exc}",
                context={
                    "operation": "start_execution",
                    "execution_id": execution_id,
                },
            ) from exc

        logger.info(
            f"执行批次已启动 | 批次: {execution_id} | "
            f"状态: pending | 用例数: {len(cases)}"
        )
        return {
            "execution_id": execution_id,
            "total_cases": len(cases),
            "status": "pending",
            "cases": cases,
        }

    @classmethod
    def _publish_execution_event(
        cls, execution_id: str, event_type: str, data: dict
    ) -> None:
        """
        发布执行事件到批次事件通道（内部方法，SSE埋点专用）

        event_bus.publish自身已保证绝不抛异常，本层再包一层
        try/except双保险（含get_channel建通道环节）: 日志通道任何
        故障只记error日志，绝不影响真实执行主流程。

        参数:
            execution_id (str): 执行批次号
            event_type (str): 事件类型，合法取值见
                              event_bus.VALID_EVENT_TYPES
            data (dict): 事件载荷

        返回:
            无

        异常:
            无（全部内部消化，只记error日志）
        """
        # 通道注册表以批次号为键，不归一会与 DB 侧分叉出两个通道
        execution_id = str(execution_id or "").strip()
        try:
            channel = get_channel(execution_id, create=True)
            if channel is None:
                return
            channel.publish(
                ExecutionEvent(event_type=event_type, data=data)
            )
        except Exception as exc:
            # 兜底铁律: 事件发布任何故障只记日志，不影响执行主流程
            logger.error(
                f"执行事件发布异常（不影响执行主流程）| "
                f"批次: {execution_id} | 类型: {event_type} | {exc}"
            )

    @classmethod
    def _close_execution_channel(
        cls, execution_id: str, reason: str
    ) -> None:
        """
        关闭并清理批次事件通道（内部方法，批次终态后调用）

        与_publish_execution_event同口径兜底吞异常: 通道清理故障
        只记error日志，绝不影响真实执行主流程。

        参数:
            execution_id (str): 执行批次号
            reason (str): 关闭原因（"finished"/"failed"）

        返回:
            无

        异常:
            无（全部内部消化，只记error日志）
        """
        execution_id = str(execution_id or "").strip()
        try:
            close_channel(execution_id, reason=reason)
        except Exception as exc:
            logger.error(
                f"事件通道关闭异常（不影响执行主流程）| "
                f"批次: {execution_id} | 原因: {reason} | {exc}"
            )

    @classmethod
    def _execute_batch_async(
        cls,
        execution_id: str,
        cases: list,
        executor_kind: str | None = None,
        notification_router: NotificationRouter | None = None,
    ) -> None:
        """
        批次后台执行编排（daemon线程目标函数，也可同步调用）

        状态机流转:
            pending → running → finished（正常完成）
                              → failed（任何环节异常）

        执行流程:
            1. 批次行置running + started_at
            2. 经工厂get_executor取执行器（编排只依赖BaseExecutor
               抽象契约，具体执行器可插拔替换），逐用例run_one
               并立即record_execution落明细（含start/end/duration）
            3. 全部完成调finish_execution聚合写defect_statistics
            4. 批次行置finished + 冗余各结果计数/通过率 + finished_at
            5. 全程try/except兜底: 任何异常→批次行置failed +
               error_message，绝不向调用线程抛异常（守护线程内
               未捕获异常会静默杀死线程并污染进程状态）
            6. 事件埋点（Day25）: 关键节点向事件通道发布
               batch_start/case_finished/batch_finished/batch_failed
               事件（SSE流式接口消费），批次终态后关闭并清理通道；
               埋点全程兜底，日志通道故障绝不影响真实执行
            7. 批次终态且通道关闭后（Day27）: 旁路调用
               notify_execution_result推送邮件/企微通知（异常双层
               兜底不影响主流程；通知含指数退避重试可能耗时数秒，
               置于通道关闭之后确保SSE客户端先收到终态事件）
            8. 报告统计缓存失效（Day31）: finished分支在finish落库
               与批次状态更新成功后、failed分支在failed状态收尾后，
               调cache_client.invalidate_reports清五类报告缓存，
               缓存故障静默降级不影响执行主流程

        线程安全说明: 所有数据库操作均各自新建会话
        （record_execution/finish_execution走session_scope，
        状态更新同口径），绝不借用请求线程的session——
        SQLAlchemy Session非线程安全，跨线程复用会话会造成
        连接状态错乱。

        参数:
            execution_id (str): 执行批次号
            cases (list[dict]): 待执行用例字典列表
                                （start_execution筛选结果）
            executor_kind (str | None): 执行器类型（simulated/pytest），
                                        None时工厂读TM_EXECUTOR环境变量
            notification_router (NotificationRouter | None): 通知路由器
                                        （仅测试注入用；Web触发路径不传，
                                        None时notify_execution_result内部
                                        默认实例化，策略由TM_NOTIFY_STRATEGY
                                        环境变量控制，与CLI默认行为一致）

        返回:
            None

        异常:
            无（全部异常内部消化为批次failed状态）
        """
        try:
            # 幂等抢占（Day45 全量审查 问题2）：enqueue 的 lpush 可能在
            # Redis 已生效、但应答因 socket_timeout 超时抛错，此时路由会
            # 用同一 execution_id 起裸线程兜底，而消息仍在队列里等 worker
            # 消费——两条链路同时执行同一批次，明细双写、计数翻倍、
            # 通过率被污染。以批次号做 SETNX 抢占，后到者直接放弃。
            #
            # 导入方向说明：task_queue 只在 run() 内**延迟** import
            # case_manager，模块级不反向依赖，故本模块顶层 import 它
            # 不构成循环依赖。
            #
            # 队列未启用时 task_queue_client.claim() 恒返回 True
            # （后端不可用时只有裸线程一条路径，不存在双跑），
            # 因此本段对默认链路零影响。
            if not task_queue_client.claim(execution_id):
                logger.warning(
                    f"批次已被其他执行链路抢占，本次放弃（防同一批次双跑） | "
                    f"批次: {execution_id}"
                )
                return

            # a. 置running + 记录批次开始时间
            # 顺带取回批次元信息（Day44 热修 P2-1）：_update_batch_status
            # 本来就要 SELECT 这一行，返回值里多带了 environment/executor，
            # 下面逐条明细直接透传，record_execution 就不必为每条用例再查一次。
            batch_meta = cls._update_batch_status(
                execution_id, status="running", started_at=datetime.now()
            ) or {}
            batch_environment = batch_meta.get("environment")
            batch_executor = batch_meta.get("executor")
            logger.info(
                f"批次开始执行 | 批次: {execution_id} | 用例数: {len(cases)} | "
                f"执行器: {executor_kind or '(TM_EXECUTOR环境变量)'} | "
                f"环境: {batch_environment}"
            )
            # 事件埋点: 批次开始（executor_kind为None表示读TM_EXECUTOR
            # 环境变量的默认执行器，载荷原样透传null）
            cls._publish_execution_event(
                execution_id,
                "batch_start",
                {
                    "total_cases": len(cases),
                    "executor_kind": executor_kind,
                },
            )

            # b. 经工厂取执行器后逐用例执行并立即落明细
            #    （单用例异常同样走兜底转批次failed，当前粒度为批次级）
            executor = get_executor(executor_kind)
            for case in cases:
                case_start = datetime.now()
                exec_result = executor.run_one(case)
                case_end = datetime.now()
                cls.record_execution(
                    execution_id=execution_id,
                    case_id=str(case.get("case_id", "")),
                    case_name=str(case.get("name", "")),
                    result=exec_result.result,
                    start_time=case_start,
                    end_time=case_end,
                    duration=exec_result.duration,
                    error_message=exec_result.error_message,
                    # 整批不变的两个字段从批次行一次性取到后透传，
                    # 避免 record_execution 内部逐条查批次行（Day44 热修 P2-1）
                    environment=batch_environment,
                    executor=batch_executor,
                )
                # 事件埋点: 单条用例执行完成（与明细表同口径字段）
                cls._publish_execution_event(
                    execution_id,
                    "case_finished",
                    {
                        "case_id": str(case.get("case_id", "")),
                        "case_name": str(case.get("name", "")),
                        "result": exec_result.result,
                        "duration": exec_result.duration,
                        "error_message": exec_result.error_message,
                    },
                )

            # c. 聚合汇总写defect_statistics
            summary = cls.finish_execution(execution_id)

            # d. 置finished + 冗余统计（批次状态查询免聚合直查）
            cls._update_batch_status(
                execution_id,
                status="finished",
                passed=summary["passed"],
                failed=summary["failed"],
                error=summary["error"],
                skipped=summary["skipped"],
                pass_rate=summary["pass_rate"],
                finished_at=datetime.now(),
            )
            logger.info(
                f"批次执行完成 | 批次: {execution_id} | "
                f"通过: {summary['passed']}/{summary['total']} | "
                f"通过率: {summary['pass_rate']:.2%}"
            )
            # Day31: finish_execution汇总落库且批次finished状态更新
            # 成功后失效报告统计缓存（五类聚合结果已变化）
            # 独立try隔离: 缓存是旁路能力，其异常绝不能被外层
            # except接盘后当"执行失败"处理——否则一条100%通过的批次
            # 会被改写成 status="failed"（缓存层已自身兜底，此处为
            # 纵深防御，契约见 6.1 通知旁路铁律）
            try:
                cache_client.invalidate_reports()
            except Exception as cache_exc:
                logger.warning(
                    f"报告统计缓存失效异常已忽略（不影响批次结果） | "
                    f"批次: {execution_id} | "
                    f"{type(cache_exc).__name__}: {cache_exc}"
                )
            # 事件埋点: 批次正常完成 → 发布终态事件并关闭清理通道
            cls._publish_execution_event(
                execution_id,
                "batch_finished",
                {
                    "total": summary["total"],
                    "passed": summary["passed"],
                    "failed": summary["failed"],
                    "error": summary["error"],
                    "skipped": summary["skipped"],
                    "pass_rate": summary["pass_rate"],
                },
            )
            cls._close_execution_channel(execution_id, reason="finished")
            # 7. 通道关闭后旁路推送批次通知（SSE终态事件先送达
            #    客户端；通知含指数退避重试可能耗时数秒，不能阻塞
            #    订阅方收终态事件）；外层try兜底（口径同run_batch
            #    第8步）: 通知异常仅记error日志，绝不向上抛
            try:
                cls.notify_execution_result(
                    execution_id, router=notification_router
                )
            except Exception as notify_exc:
                logger.error(
                    f"批次自动通知异常已捕获（不影响主流程） | "
                    f"批次: {execution_id} | "
                    f"{type(notify_exc).__name__}: {notify_exc}"
                )
        except Exception as exc:
            # e. 任何异常: 批次置failed + error_message，不向调用线程抛出
            logger.error(
                f"批次执行异常，批次置failed | 批次: {execution_id} | "
                f"{type(exc).__name__}: {exc}"
            )
            error_summary = f"{type(exc).__name__}: {exc}"
            # 失败批次同样要聚合（Day45 第 4 批 P2-1）
            #
            # 修复前本分支只 `_update_batch_status(status="failed")` 改状态，
            # 不写 defect_statistics。而 `list_executions_paged` 只读该表，
            # 于是"第 k 条用例抛异常"的批次——前 k-1 条明细已落库、统计完全
            # 可算——在批次列表里**彻底不可见**，`get_execution_status` 却显示
            # failed + 计数全 0。排障时表现为"批次凭空消失"。
            #
            # 异常可能发生在第一条用例执行之前（records 为空），此时无明细
            # 可聚合，属正常降级：记 warning 后继续收尾，不影响终态落库。
            failed_summary: dict[str, Any] | None = None
            try:
                failed_summary = cls._aggregate_execution_results(
                    execution_id, "aggregate_failed_batch"
                )
            except Exception as agg_exc:
                # 聚合失败不得吞掉 failed 状态本身：本行位于外层 except 块内，
                # 裸抛会杀死 daemon 线程并连带跳过终态事件与失败通知
                logger.error(
                    f"失败批次聚合异常（不影响failed状态落库） | "
                    f"批次: {execution_id} | "
                    f"{type(agg_exc).__name__}: {agg_exc}"
                )
            if failed_summary is None:
                logger.warning(
                    f"失败批次无执行明细可聚合，不写汇总行 | 批次: {execution_id}"
                )
            # 冗余统计回写批次行：get_execution_status 直查批次行，
            # 不聚合，故不写这里它仍显示 failed + 全 0
            stat_fields: dict[str, Any] = {}
            if failed_summary is not None:
                stat_fields = {
                    "passed": failed_summary["passed"],
                    "failed": failed_summary["failed"],
                    "error": failed_summary["error"],
                    "skipped": failed_summary["skipped"],
                    "pass_rate": failed_summary["pass_rate"],
                }
            try:
                cls._update_batch_status(
                    execution_id,
                    status="failed",
                    error_message=error_summary,
                    finished_at=datetime.now(),
                    **stat_fields,
                )
            except Exception as mark_exc:
                # failed状态落库也失败（如库不可用）: 仅记日志，
                # 双重异常下保证线程安全退出不崩进程
                logger.error(
                    f"批次failed状态落库异常 | 批次: {execution_id} | "
                    f"{mark_exc}"
                )
            # Day31: 批次终态（failed）收尾处同样失效报告统计缓存，
            # 与finished分支口径一致（部分明细可能已落库，统计聚合
            # 存在可见性变化）
            # 独立try隔离: 本行位于外层except块内部，**此处已无任何
            # try可接盘**，裸抛会一路冒到线程目标函数外、杀死daemon
            # 线程，并连带跳过下面的 batch_failed 事件发布、事件通道
            # 关闭与失败通知——订阅该批次的SSE客户端将永远收不到终态
            try:
                cache_client.invalidate_reports()
            except Exception as cache_exc:
                logger.warning(
                    f"报告统计缓存失效异常已忽略（不影响批次收尾） | "
                    f"批次: {execution_id} | "
                    f"{type(cache_exc).__name__}: {cache_exc}"
                )
            # 事件埋点: 批次异常失败 → 发布终态事件并关闭清理通道
            # （置于状态落库之后，即使落库失败也向订阅方发终态事件）
            cls._publish_execution_event(
                execution_id,
                "batch_failed",
                {"error_message": error_summary},
            )
            cls._close_execution_channel(execution_id, reason="failed")
            # failed批次同样旁路推送通知: strategy=failed_only时是否
            # 实际发送由Router内部裁决，接入层不判断；except分支内
            # 的通知调用必须自带try兜底——此处已无外层try可接盘，
            # 裸抛异常会杀死daemon线程
            try:
                cls.notify_execution_result(
                    execution_id, router=notification_router
                )
            except Exception as notify_exc:
                logger.error(
                    f"批次自动通知异常已捕获（不影响主流程） | "
                    f"批次: {execution_id} | "
                    f"{type(notify_exc).__name__}: {notify_exc}"
                )

    @classmethod
    def get_execution_status(cls, execution_id: str) -> dict | None:
        """
        查询执行批次状态（Web批次状态查询API专用）

        数据口径:
            直查test_execution_batches批次元信息行（含finish后冗余的
            各结果计数与通过率），无需聚合明细表；执行中批次
            （running/pending）同样可查，冗余统计字段为初始值0。

        参数:
            execution_id (str): 执行批次号

        返回:
            dict | None: {"execution_id", "status", "total_cases",
                         "passed", "failed", "error", "skipped",
                         "pass_rate", "error_message", "started_at",
                         "finished_at", "created_at"}；
                         datetime字段转ISO格式字符串（未完成时
                         started_at/finished_at为None，冗余统计
                         字段为初始值）；批次不存在返回None
                         （路由层转404）

        异常:
            CaseManagerError: 批次号为空 / 数据库查询异常时抛出，
                              context携带operation定位
        """
        # 批次号基础校验（空字符串/空白串直接拒绝）+ 归一
        execution_id = cls._normalize_batch_id(
            execution_id, "get_execution_status"
        )

        try:
            session = DatabaseSession.get_session()
            try:
                batch_row = (
                    session.query(TestExecutionBatch)
                    .filter_by(execution_id=execution_id)
                    .first()
                )
            finally:
                session.close()
        except SQLAlchemyError as exc:
            logger.error(
                f"批次状态查询数据库异常 | 批次: {execution_id} | {exc}"
            )
            raise CaseManagerError(
                f"批次状态查询数据库异常: {exc}",
                context={
                    "operation": "get_execution_status",
                    "execution_id": execution_id,
                },
            ) from exc

        if batch_row is None:
            logger.debug(f"批次不存在（无批次行）| 批次: {execution_id}")
            return None

        return {
            "execution_id": batch_row.execution_id,
            "status": batch_row.status,
            "total_cases": batch_row.total_cases,
            "passed": batch_row.passed,
            "failed": batch_row.failed,
            "error": batch_row.error,
            "skipped": batch_row.skipped,
            "pass_rate": batch_row.pass_rate,
            "error_message": batch_row.error_message,
            "started_at": local_to_utc_iso(batch_row.started_at),
            "finished_at": local_to_utc_iso(batch_row.finished_at),
            "created_at": to_utc_iso(batch_row.created_at),
        }

    @classmethod
    def _update_batch_status(
        cls,
        execution_id: str,
        status: str,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        passed: int | None = None,
        failed: int | None = None,
        error: int | None = None,
        skipped: int | None = None,
        pass_rate: float | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        """
        更新批次状态与冗余统计（内部方法）

        仅更新显式传入（非None）的字段，其余字段保持不变；
        批次行不存在时记warning后静默返回（防御性容错，
        不阻断调用链——明细写入仍可正常完成）。

        参数:
            execution_id (str): 执行批次号
            status (str): 目标状态 pending/running/finished/failed
            started_at (datetime | None): 批次开始时间，None不更新
            finished_at (datetime | None): 批次完成时间，None不更新
            passed (int | None): 通过数，None不更新
            failed (int | None): 失败数，None不更新
            error (int | None): 错误数，None不更新
            skipped (int | None): 跳过数，None不更新
            pass_rate (float | None): 通过率，None不更新
            error_message (str | None): 批次级异常信息，None不更新

        返回:
            dict[str, Any] | None: 本次读到的批次元信息
                {"environment": ..., "executor": ...}；
                批次行不存在时为 None（调用方需自行决定兜底）

        异常:
            CaseManagerError: 数据库操作异常时抛出
                              （由调用方决定兜底策略）
        """
        meta: dict[str, Any] | None = None
        # 归一：带空白的批次号此前查不到批次行，只 warning 返回 None——
        # 状态更新被静默丢弃，而明细已按干净号落库，两边就此分叉
        execution_id = str(execution_id or "").strip()
        try:
            with DatabaseSession.session_scope() as session:
                batch_row = (
                    session.query(TestExecutionBatch)
                    .filter_by(execution_id=execution_id)
                    .first()
                )
                if batch_row is None:
                    logger.warning(
                        f"批次行不存在，状态更新跳过 | 批次: {execution_id} | "
                        f"目标状态: {status}"
                    )
                    return None
                # 顺带回传两个"整批不变"的字段（Day44 热修 P2-1）：本方法本来
                # 就 SELECT 了这一行，多取两个标量零额外成本。调用方
                # （_execute_batch_async）据此把值透传给每条明细的
                # record_execution，避免它为每条用例再查一次批次行
                # （修复前 20 条用例多出 22 次查询，放大 2.1x）。
                # 必须在 session 关闭前取值。
                meta = {
                    "environment": batch_row.environment,
                    "executor": batch_row.executor,
                }
                batch_row.status = status
                if started_at is not None:
                    batch_row.started_at = started_at
                if finished_at is not None:
                    batch_row.finished_at = finished_at
                if passed is not None:
                    batch_row.passed = passed
                if failed is not None:
                    batch_row.failed = failed
                if error is not None:
                    batch_row.error = error
                if skipped is not None:
                    batch_row.skipped = skipped
                if pass_rate is not None:
                    batch_row.pass_rate = pass_rate
                if error_message is not None:
                    batch_row.error_message = error_message
        except SQLAlchemyError as exc:
            logger.error(
                f"批次状态更新数据库异常 | 批次: {execution_id} | "
                f"目标状态: {status} | {exc}"
            )
            raise CaseManagerError(
                f"批次状态更新数据库异常: {exc}",
                context={
                    "operation": "_update_batch_status",
                    "execution_id": execution_id,
                    "status": status,
                },
            ) from exc
        return meta

    # ------------------------------------------------------------------
    # 批次完成通知集成（第二阶段Day15）
    # ------------------------------------------------------------------
    @classmethod
    def build_notification_statistics(
        cls, execution_id: str
    ) -> StatisticsResult | None:
        """
        将批次DB执行记录适配为统计模型（Day15适配器）

        执行流程:
            1. 查询该execution_id全部test_executions记录，无记录返回None
            2. 一次性查询关联test_cases建立case_id→(module, priority)字典
               （批量查询防N+1；缺失用例落unknown不报错）
            3. 逐条转AllureResult（状态映射: DB error→Allure broken，
               即Day8入库映射的逆过程；耗时秒→毫秒: start=0, stop=duration×1000）
            4. 复用ReportStatistics.aggregate产出StatisticsResult
               （aggregate是唯一统计入口，P95/分组/失败明细口径只有一份）

        参数:
            execution_id (str): 执行批次号

        返回:
            StatisticsResult | None: 批次统计结果；批次无记录时返回None

        异常:
            无（数据库异常由session记录后向上抛出，调用方notify_execution_result消化）
        """
        session = DatabaseSession.get_session()
        # 归一但不抛：本方法契约是"异常: 无"，空白号与"无记录"同义返回 None
        execution_id = str(execution_id or "").strip()
        try:
            records = (
                session.query(TestExecution)
                .filter_by(execution_id=execution_id)
                .all()
            )
            if not records:
                logger.debug(f"批次无执行记录，跳过统计构建 | 批次: {execution_id}")
                return None

            # 批量查关联用例（防N+1）: case_id→(module, priority)
            case_ids = [record.case_id for record in records]
            case_rows = (
                session.query(TestCase)
                .filter(TestCase.case_id.in_(case_ids))
                .all()
            )
            case_info = {
                row.case_id: (row.module, row.priority) for row in case_rows
            }

            # DB四态→Allure四态映射（Day8入库映射的逆过程:
            # 入库时failed=纯failed、error=broken，此处error还原为broken）
            status_mapping = {"passed": "passed", "failed": "failed",
                              "error": "broken", "skipped": "skipped"}
            allure_results = []
            for record in records:
                module, priority = case_info.get(
                    record.case_id, ("unknown", "unknown")
                )
                allure_status = status_mapping.get(record.result, "unknown")
                allure_results.append(
                    AllureResult(
                        uuid=record.case_id,
                        name=record.case_name,
                        full_name=record.case_id,
                        status=allure_status,
                        description="",
                        start=0,
                        stop=int(round(record.duration * 1000)),  # 秒→毫秒
                        history_id=record.case_id,
                        labels={
                            "feature": [module or "unknown"],
                            "severity": [priority or "unknown"],
                        },
                        parameters=[],
                        status_details=(
                            {"message": record.error_message or "", "trace": ""}
                            if allure_status in ("failed", "broken")
                            else None
                        ),
                    )
                )
        finally:
            session.close()

        stat = ReportStatistics.aggregate(allure_results)
        logger.info(
            f"批次统计模型构建完成 | 批次: {execution_id} | "
            f"总数: {stat.total} | 通过率: {stat.pass_rate:.2%}"
        )
        return stat

    @classmethod
    def notify_execution_result(
        cls,
        execution_id: str,
        router: NotificationRouter | None = None,
        strategy: str | None = None,
    ) -> dict:
        """
        批次完成自动通知（Day15集成入口，通知为旁路能力）

        执行流程:
            1. build_notification_statistics构建统计模型，无记录返回{}
            2. router为None时默认实例化NotificationRouter
            3. router.notify推送（发送策略/重试/死信均由Router管理）

        旁路铁律: 任何异常（建统计失败/路由失败/发送失败）只记error日志
        并返回{}，绝不向上抛——执行主流程不被通知影响。

        参数:
            execution_id (str): 执行批次号
            router (NotificationRouter | None): 通知路由器（测试可注入）
            strategy (str | None): 覆盖通知策略（None用Router实例策略）

        返回:
            dict: 各渠道发送结果（如{"email": True, "wechat": False}）；
                  任何异常时返回{}

        异常:
            无（全部内部消化）
        """
        try:
            stat = cls.build_notification_statistics(execution_id)
            if stat is None:
                logger.info(
                    f"批次无执行记录，跳过通知 | 批次: {execution_id}"
                )
                return {}
            if router is None:
                router = NotificationRouter()
            return router.notify(stat, execution_id, strategy)
        except Exception as exc:
            logger.error(
                f"批次通知异常已捕获（不影响主流程） | 批次: {execution_id} | "
                f"{type(exc).__name__}: {exc}"
            )
            return {}

    # ------------------------------------------------------------------
    # 批量执行与命令行（第二阶段Day3）
    # ------------------------------------------------------------------
    @staticmethod
    def _simulate_execute(case: dict) -> tuple[str, str | None, float]:
        """
        模拟执行器（内部工具方法）

        模拟单条用例执行过程（后续Web平台接入真实pytest执行时替换此方法）:
            1. time.sleep(0.01)模拟用例执行耗时
            2. 结果规则: case_id末尾数字为偶数→passed，奇数→failed
               （failed时error_message为固定模拟文案）

        参数:
            case (dict): 待执行用例字典（含case_id/name等字段）

        返回:
            tuple: (result: str, error_message: Optional[str], duration: float)
                   result为执行结果passed/failed，error_message为失败信息
                   （通过时为None），duration为模拟执行耗时（秒）

        异常:
            无
        """
        start_time = time.perf_counter()
        time.sleep(0.01)  # 模拟用例执行耗时
        duration = time.perf_counter() - start_time

        # case_id末尾数字奇偶决定结果（无数字时视为偶数→passed）
        tail_digits = [ch for ch in str(case.get("case_id", "")) if ch.isdigit()]
        tail_number = int(tail_digits[-1]) if tail_digits else 0
        if tail_number % 2 == 0:
            logger.debug(f"模拟执行通过 | 用例: {case.get('case_id')}")
            return "passed", None, duration

        error_message = "模拟执行失败: 断言不通过"
        logger.debug(f"模拟执行失败 | 用例: {case.get('case_id')} | {error_message}")
        return "failed", error_message, duration

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------
    @staticmethod
    def _infer_case_type(file_path: str | Path) -> str:
        """
        按文件路径推断用例类型（内部方法）

        规则: **只对文件名**（统一小写、反斜杠归一为斜杠）包含
        chip/serial/telnet 任意关键词即判定为chip（串口/Telnet协议），
        否则为api（HTTP接口）。

        为什么只看文件名: 原实现对**整条绝对路径**做子串匹配，而路径里
        包含操作系统账户名与所有父目录。部署机账户名一旦含关键词
        （如 telnet_lab、serial.wang），该用户上传的**所有**文件都会被
        误判为 chip，连纯 API 用例也会被标成协议类型——进而
        case_type="api" 的执行批次永远筛不到它们。

        参数:
            file_path (str | Path): 数据文件路径

        返回:
            str: "chip"或"api"

        异常:
            无
        """
        # 同时覆盖正斜杠文件名与反斜杠文件名
        normalized = str(file_path).lower().replace("\\", "/")
        file_name = normalized.rsplit("/", 1)[-1]
        return (
            "chip"
            if any(keyword in file_name for keyword in CHIP_PATH_KEYWORDS)
            else "api"
        )

    @staticmethod
    def _build_description(case: dict) -> str:
        """
        构建用例描述字段（内部方法）

        test_cases表暂无独立tags列，数据中的tags标签列表序列化为
        描述文本暂存；数据自带description字段时优先使用原描述。

        参数:
            case (dict): DataDriver规范化后的单条用例数据

        返回:
            str: 描述文本（无可用信息时为空字符串）

        异常:
            无
        """
        custom_desc = str(case.get("description") or "").strip()
        tags = case.get("tags") or []
        tag_line = f"标签: {', '.join(tags)}" if tags else ""
        # 描述与标签**拼接**而非二选一（v3 修复 V2-P2-3）
        #
        # 修复前 custom_desc 非空即 return，tags 分支永不执行——一整行
        # 用例的标签被静默丢弃，随后按标签筛选恒不命中。而 v1 引入
        # _normalize_scalar 之后，Excel 里的 description=2024 从"非 str
        # 被拒"变成合法字符串 "2024"，正好命中该分支，触发面被扩大。
        # "取描述"与"丢标签"本不该是同一个决定。
        if custom_desc and tag_line:
            return f"{custom_desc}\n{tag_line}"
        if custom_desc:
            return custom_desc
        return tag_line

    @staticmethod
    def _parse_tags_from_description(description: str | None) -> list:
        """
        从description字段解析标签列表（内部方法）

        解析规则:
            description中含"标签: xxx,yyy"行（由_build_description写入，
            拼接后可能位于自定义描述之后）时，提取该行冒号后内容按逗号分割
            并strip；不含该行（纯自定义描述/空值）返回空列表（视为无标签）。

        参数:
            description (str | None): 用例描述文本

        返回:
            list: 解析出的标签列表（无标签时为空列表）

        异常:
            无
        """
        if not description:
            return []
        # 逐行查找标签行（v3 修复 V2-P2-3）: 描述与标签拼接后标签行不再
        # 一定在首位，仍用整串 startswith 会让拼接后的标签永远解析不出来
        for line in str(description).splitlines():
            stripped = line.strip()
            if not stripped.startswith(TAGS_PREFIX):
                continue
            tag_text = stripped[len(TAGS_PREFIX):].strip()
            return [tag.strip() for tag in tag_text.split(",") if tag.strip()]
        return []

    @staticmethod
    def _normalize_values(value: str | list, dim_name: str) -> list:
        """
        筛选维度值归一化为列表（内部方法）

        参数:
            value (str | list): 单值或列表
            dim_name (str): 维度名称（用于空值/非法类型警告日志）

        返回:
            list: 归一化列表（剔除空白项；空列表表示不过滤该维度）

        异常:
            无
        """
        if isinstance(value, str):
            stripped = value.strip()
            return [stripped] if stripped else []
        if isinstance(value, list):
            if not value:
                logger.warning(f"筛选维度'{dim_name}'传入了空列表，视为不过滤该维度")
            return [str(item).strip() for item in value if str(item).strip()]
        logger.warning(
            f"筛选维度'{dim_name}'类型非法: {type(value).__name__}，视为不过滤该维度"
        )
        return []

    @staticmethod
    def _to_dict(row: TestCase) -> dict:
        """
        TestCase模型行转字典（内部方法）

        参数:
            row (TestCase): 数据库模型实例

        返回:
            dict: 含全部字段的字典（datetime转为ISO格式字符串，便于序列化）

        异常:
            无

        为什么 source_ref 必须在这里（Day48 补 Day47 前置缺口）:
            本方法是"模型行 → 用例字典"的**唯一**出口，GET /api/cases/
            列表、详情、创建响应，以及执行调度用的
            select_cases_for_execution → _execute_batch_async 逐条 run_one
            的 case 字典，**全部**经由它产出。Day47 已经在写入侧落库、
            在执行侧按 source_ref 拼命令，但本方法没把这个字段放进字典，
            于是执行器读到的永远是空值，build_command 每条都抛
            "source_ref 为空" 并降级为 error——真实执行链路功能性不可用。
            键缺失比值为 None 更危险，故此处无条件输出该键（未配置时序列化为
            JSON null），不做"有值才带"的裁剪。
        """
        return {
            "id": row.id,
            "case_id": row.case_id,
            "name": row.name,
            "module": row.module,
            "priority": row.priority,
            "case_type": row.case_type,
            "status": row.status,
            "description": row.description,
            # pytest 可执行目标（None = 该用例不可被 pytest 执行）
            "source_ref": row.source_ref,
            "creator": row.creator,
            "created_at": to_utc_iso(row.created_at),
            "updated_at": to_utc_iso(row.updated_at),
        }


def run_batch(
    file_path: str | Path,
    sheet_name: str | None = None,
    priority: str | list | None = None,
    module: str | list | None = None,
    tags: str | list | None = None,
    trigger: str = "cli",
    dry_run: bool = False,
    notify: bool = False,
) -> dict:
    """
    批量执行完整调度链路（模块级函数）

    串联完整调度链路:
        1. sync_cases_from_file 用例入库
        2. create_execution 创建批次（trigger透传，executor固定"cli"）
        3. select_cases_for_execution 筛选待执行用例
        4. dry_run=True: 打印待执行用例列表（case_id/name/module/priority），
           返回 {"dry_run": True, "count": N}，不产生执行记录
        5. dry_run=False: 逐条调_simulate_execute模拟执行并record_execution入库
        6. finish_execution 汇总统计并写入defect_statistics
        7. 控制台打印汇总报告（总数/通过/失败/错误/跳过/通过率）

    参数:
        file_path (str | Path): 数据文件路径（YAML/Excel）
        sheet_name (str | None): Excel的sheet名称，默认None
        priority (str | list | None): 优先级筛选值，默认None不过滤
        module (str | list | None): 模块筛选值，默认None不过滤
        tags (str | list | None): 标签筛选值，默认None不过滤
        trigger (str): 触发方式（manual/cli/web/ci），默认"cli"
        dry_run (bool): 只加载筛选不执行，默认False

    返回:
        dict: dry_run时返回{"dry_run": True, "count": N}；
              正常执行返回finish_execution的统计字典
              （{"execution_id", "total", "passed", "failed", "error",
                "skipped", "pass_rate"}）

    异常:
        CaseManagerError: 数据加载失败/数据库异常等链路任一环节失败时向上抛出
    """
    logger.info(
        f"批量执行启动 | 文件: {file_path} | dry_run: {dry_run} | "
        f"筛选: priority={priority or '-'} & module={module or '-'} & "
        f"tags={tags or '-'}"
    )

    # 1. 用例入库
    sync_result = CaseManager.sync_cases_from_file(file_path, sheet_name=sheet_name)
    logger.info(
        f"用例入库完成 | 总数: {sync_result['total']} | "
        f"新增: {sync_result['inserted']} | 更新: {sync_result['updated']}"
    )

    # 2. 创建执行批次
    execution_id = CaseManager.create_execution(trigger=trigger, executor="cli")

    # 3. 筛选待执行用例
    #    case_type 必须与上一步 sync_cases_from_file 的推断口径一致,
    #    否则 chip 数据文件刚被标成 chip、这里却按默认值 api 筛,
    #    结果必然命中0条, 并抛出误导性的"批次不存在"
    inferred_case_type = CaseManager._infer_case_type(file_path)
    selected_cases = CaseManager.select_cases_for_execution(
        module=module, priority=priority, tags=tags,
        case_type=inferred_case_type,
    )

    # 4. dry_run: 只打印待执行列表即返回
    if dry_run:
        print(f"\n===== 待执行用例列表（共{len(selected_cases)}条）=====")
        for case in selected_cases:
            print(
                f"  [{case['priority']}] {case['case_id']} | "
                f"{case['module']} | {case['name']}"
            )
        print("=" * 46)
        logger.info(f"dry_run模式 | 待执行用例: {len(selected_cases)}条，未产生执行记录")
        return {"dry_run": True, "count": len(selected_cases)}

    # 5. 逐条模拟执行并记录结果
    for case in selected_cases:
        result, error_message, duration = CaseManager._simulate_execute(case)
        CaseManager.record_execution(
            execution_id=execution_id,
            case_id=case["case_id"],
            case_name=case["name"],
            result=result,
            start_time=datetime.now(),
            end_time=datetime.now(),
            duration=duration,
            error_message=error_message,
        )

    # 6. 批次汇总统计
    summary = CaseManager.finish_execution(execution_id)

    # 6.1 报告统计缓存失效（Day45 第 3 批 P1-4）：本 CLI 路径直接调
    #     record_execution + finish_execution，**不经过 _execute_batch_async**，
    #     而报告缓存失效此前只挂在 _execute_batch_async 内部 → 走 CLI 执行后
    #     Web 报告页在 TTL 内一直是旧值。新明细已落库，聚合口径已变，必须失效。
    _safe_invalidate("CLI批次执行", cache_client.invalidate_reports)

    # 7. 控制台汇总报告
    print(f"\n===== 批量执行汇总报告 | 批次号: {execution_id} =====")
    print(f"  用例总数: {summary['total']}")
    print(f"  通过: {summary['passed']} | 失败: {summary['failed']} | "
          f"错误: {summary['error']} | 跳过: {summary['skipped']}")
    print(f"  通过率: {summary['pass_rate']:.2%}")
    print("=" * 52)

    # 8. 批次完成自动推送（旁路: 通知异常仅记error日志，不影响主流程返回）
    if notify:
        try:
            CaseManager.notify_execution_result(execution_id)
        except Exception as exc:
            logger.error(
                f"批次自动通知异常已捕获（不影响主流程） | 批次: {execution_id} | "
                f"{type(exc).__name__}: {exc}"
            )

    return summary


def main() -> None:
    """
    命令行入口

    参数说明:
        --file / -f         数据文件路径（YAML/Excel），必填
        --sheet / -s        Excel sheet名称，默认None
        --priority / -p     优先级筛选，可多次传入（如-p P0 -p P1），默认None
        --module / -m       模块筛选，可多次传入，默认None
        --tags / -t         标签筛选，可多次传入，默认None
        --trigger           触发方式（manual/cli/web/ci），默认cli
        --dry-run           只加载筛选不执行，打印待执行用例列表后退出

    参数:
        无（从sys.argv解析）

    返回:
        None

    异常:
        SystemExit: --file缺失或文件不存在时sys.exit(1)；
                    run_batch链路异常时打印错误并以退出码1退出
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.core.case_manager",
        description="TestMatrix用例批量执行命令行入口",
    )
    parser.add_argument(
        "--file", "-f", required=True,
        help="数据文件路径（YAML/Excel），必填",
    )
    parser.add_argument(
        "--sheet", "-s", default=None,
        help="Excel sheet名称，默认None（仅Excel文件生效）",
    )
    parser.add_argument(
        "--priority", "-p", action="append", default=None,
        help="优先级筛选，可多次传入（如 -p P0 -p P1）",
    )
    parser.add_argument(
        "--module", "-m", action="append", default=None,
        help="模块筛选，可多次传入",
    )
    parser.add_argument(
        "--tags", "-t", action="append", default=None,
        help="标签筛选，可多次传入",
    )
    parser.add_argument(
        "--trigger", default="cli",
        choices=list(VALID_TRIGGERS),
        help="触发方式（manual/cli/web/ci），默认cli",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="只加载筛选不执行，打印待执行用例列表后退出",
    )
    parser.add_argument(
        "--notify", action="store_true", default=False,
        help="执行完成后推送邮件/企微通知（需同时开启对应渠道开关）",
    )
    args = parser.parse_args()

    # --file存在性校验（不存在打印错误并退出码1）
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"错误: 数据文件不存在: {args.file}")
        sys.exit(1)

    # 调用run_batch执行完整链路（链路异常时打印错误并以退出码1退出）
    try:
        run_batch(
            file_path=file_path,
            sheet_name=args.sheet,
            priority=args.priority,
            module=args.module,
            tags=args.tags,
            trigger=args.trigger,
            dry_run=args.dry_run,
            notify=args.notify,
        )
    except CaseManagerError as exc:
        print(f"错误: 批量执行失败: {exc}")
        logger.error(f"命令行批量执行失败 | {exc} | context: {exc.context}")
        sys.exit(1)


if __name__ == "__main__":
    main()
