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
from typing import Optional, Tuple, Union

from sqlalchemy.exc import SQLAlchemyError

from src.common.logger import LogManager
from src.core.data_driver import DataDriver, DataDriverError
from src.core.notification import NotificationRouter
from src.core.report_analyzer import (
    AllureResult,
    ReportStatistics,
    StatisticsResult,
)
from src.db.db_session import DatabaseSession
from src.db.models import DefectStatistic, TestCase, TestExecution

logger = LogManager.get_logger()

# 优先级排序权重: 数值越小越靠前（P0最高），未知优先级排最后
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# 芯片板卡用例的路径特征关键词（路径命中任意词即判定为chip类型，统一小写匹配）
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

    def __init__(self, message: str, context: Optional[dict] = None):
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
            logger.debug(f"执行批次号已生成 | {execution_id}")
            return execution_id
        # 极小概率事件: 同秒+uuid前4位撞车，重新生成
        logger.debug(f"批次号碰撞，自动重生成 | {execution_id}")


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
        file_path: Union[str, Path],
        sheet_name: Optional[str] = None,
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
            raise CaseManagerError(
                f"用例数据加载失败: {exc}",
                context={"operation": "load_cases", "file_path": str(file_path)},
            ) from exc

        # 2. 用例类型推断
        case_type = cls._infer_case_type(file_path)

        # 3. 逐条upsert入库（session_scope自动提交/回滚/关闭）
        inserted = 0
        updated = 0
        try:
            with DatabaseSession.session_scope() as session:
                for case in cases:
                    existing = (
                        session.query(TestCase)
                        .filter_by(case_id=case["case_id"])
                        .first()
                    )
                    if existing is not None:
                        # 已存在: 更新业务字段，保留creator与status
                        existing.name = case["name"]
                        existing.module = case["module"]
                        existing.priority = case["priority"]
                        existing.case_type = case_type
                        existing.description = cls._build_description(case)
                        updated += 1
                    else:
                        session.add(
                            TestCase(
                                case_id=case["case_id"],
                                name=case["name"],
                                module=case["module"],
                                priority=case["priority"],
                                case_type=case_type,
                                status="active",
                                description=cls._build_description(case),
                                creator=creator,
                            )
                        )
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
        return result

    # ------------------------------------------------------------------
    # 用例查询
    # ------------------------------------------------------------------
    @classmethod
    def list_cases(
        cls,
        module: Optional[Union[str, list]] = None,
        priority: Optional[Union[str, list]] = None,
        status: Optional[str] = "active",
        case_type: Optional[str] = None,
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
    # 执行调度（第二阶段Day2）
    # ------------------------------------------------------------------
    @classmethod
    def create_execution(
        cls,
        trigger: str = "manual",
        executor: str = "local",
        environment: str = "dev",
        remark: Optional[str] = None,
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
        module: Optional[Union[str, list]] = None,
        priority: Optional[Union[str, list]] = None,
        tags: Optional[Union[str, list]] = None,
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
        error_message: Optional[str] = None,
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

        返回:
            None

        异常:
            CaseManagerError: result非法 / 批次号或用例编号为空 /
                              failed/error时error_message缺失 /
                              数据库操作异常时抛出，context携带operation定位
        """
        # 批次号与用例编号基础校验
        if not execution_id or not str(execution_id).strip():
            raise CaseManagerError(
                "执行批次号不能为空",
                context={"operation": "record_execution", "case_id": case_id},
            )
        if not case_id or not str(case_id).strip():
            raise CaseManagerError(
                "用例编号不能为空",
                context={"operation": "record_execution", "execution_id": execution_id},
            )

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
                session.add(
                    TestExecution(
                        execution_id=execution_id,
                        case_id=case_id,
                        case_name=case_name,
                        result=result,
                        start_time=start_time,
                        end_time=end_time,
                        duration=duration,
                        error_message=error_message,
                    )
                )
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
    def finish_execution(cls, execution_id: str) -> dict:
        """
        完成执行批次并生成汇总统计

        执行流程:
            1. 查询该execution_id下全部test_executions记录
            2. 无任何记录视为批次不存在，抛CaseManagerError
            3. 统计total/passed/failed/error/skipped及通过率
               （pass_rate=passed/total，保留4位小数）
            4. upsert到defect_statistics表: execution_id存在则更新指标，
               不存在则插入（重复finish同一批次时指标幂等刷新）
            5. 返回统计字典

        参数:
            execution_id (str): 执行批次号

        返回:
            dict: {"execution_id", "total", "passed", "failed", "error",
                   "skipped", "pass_rate"}

        异常:
            CaseManagerError: 批次号为空 / 批次不存在（无执行记录） /
                              数据库操作异常时抛出，context携带operation定位
        """
        if not execution_id or not str(execution_id).strip():
            raise CaseManagerError(
                "执行批次号不能为空",
                context={"operation": "finish_execution"},
            )

        try:
            with DatabaseSession.session_scope() as session:
                records = (
                    session.query(TestExecution)
                    .filter_by(execution_id=execution_id)
                    .all()
                )
                if not records:
                    raise CaseManagerError(
                        f"执行批次不存在或无任何执行记录: {execution_id}",
                        context={
                            "operation": "finish_execution",
                            "execution_id": execution_id,
                        },
                    )

                # 结果计数聚合
                total = len(records)
                passed = sum(1 for r in records if r.result == "passed")
                failed = sum(1 for r in records if r.result == "failed")
                error = sum(1 for r in records if r.result == "error")
                skipped = sum(1 for r in records if r.result == "skipped")
                pass_rate = round(passed / total, 4) if total else 0.0

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
                context={"operation": "finish_execution", "execution_id": execution_id},
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

    # ------------------------------------------------------------------
    # 批次完成通知集成（第二阶段Day15）
    # ------------------------------------------------------------------
    @classmethod
    def build_notification_statistics(
        cls, execution_id: str
    ) -> Optional[StatisticsResult]:
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
        router: Optional[NotificationRouter] = None,
        strategy: Optional[str] = None,
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
    def _simulate_execute(case: dict) -> Tuple[str, Optional[str], float]:
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
    def _infer_case_type(file_path: Union[str, Path]) -> str:
        """
        按文件路径推断用例类型（内部方法）

        规则: 路径（统一小写、反斜杠归一为斜杠）包含chip/serial/telnet
        任意关键词即判定为chip（芯片板卡），否则为api（HTTP接口）。

        参数:
            file_path (str | Path): 数据文件路径

        返回:
            str: "chip"或"api"

        异常:
            无
        """
        path_text = str(file_path).lower().replace("\\", "/")
        return "chip" if any(keyword in path_text for keyword in CHIP_PATH_KEYWORDS) else "api"

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
        if custom_desc:
            return custom_desc
        tags = case.get("tags", [])
        return f"标签: {', '.join(tags)}" if tags else ""

    @staticmethod
    def _parse_tags_from_description(description: Optional[str]) -> list:
        """
        从description字段解析标签列表（内部方法）

        解析规则:
            description为"标签: xxx,yyy"格式（由_build_description写入）时，
            提取冒号后内容按逗号分割并strip；其他格式（自定义描述/空值）
            返回空列表（视为无标签）。

        参数:
            description (str | None): 用例描述文本

        返回:
            list: 解析出的标签列表（无标签时为空列表）

        异常:
            无
        """
        if not description:
            return []
        text = str(description).strip()
        if not text.startswith(TAGS_PREFIX):
            return []
        tag_text = text[len(TAGS_PREFIX):].strip()
        return [tag.strip() for tag in tag_text.split(",") if tag.strip()]

    @staticmethod
    def _normalize_values(value: Union[str, list], dim_name: str) -> list:
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
            "creator": row.creator,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


def run_batch(
    file_path: Union[str, Path],
    sheet_name: Optional[str] = None,
    priority: Optional[Union[str, list]] = None,
    module: Optional[Union[str, list]] = None,
    tags: Optional[Union[str, list]] = None,
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
    selected_cases = CaseManager.select_cases_for_execution(
        module=module, priority=priority, tags=tags
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
