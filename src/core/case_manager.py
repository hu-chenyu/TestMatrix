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

后续规划（不在本日范围）:
    - 批量调度执行: 串联create->select->execute->record->finish完整自动化闭环

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
"""

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from sqlalchemy.exc import SQLAlchemyError

from src.common.logger import LogManager
from src.core.data_driver import DataDriver, DataDriverError
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
