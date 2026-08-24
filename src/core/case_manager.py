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

后续规划（不在本日范围）:
    - 分级执行调度: 按smoke/regression标记与优先级圈定执行范围
    - 批量调度执行: 触发测试执行并回写test_executions表

使用示例:
    from src.core.case_manager import CaseManager, generate_execution_id

    execution_id = generate_execution_id()
    sync_result = CaseManager.sync_cases_from_file(
        "testdata/yaml/api_user_query_matrix.yaml"
    )
    cases = CaseManager.list_cases(module="用户管理", priority=["P0", "P1"])
"""

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from sqlalchemy.exc import SQLAlchemyError

from src.common.logger import LogManager
from src.core.data_driver import DataDriver, DataDriverError
from src.db.db_session import DatabaseSession
from src.db.models import TestCase

logger = LogManager.get_logger()

# 优先级排序权重: 数值越小越靠前（P0最高），未知优先级排最后
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# 芯片板卡用例的路径特征关键词（路径命中任意词即判定为chip类型，统一小写匹配）
CHIP_PATH_KEYWORDS = ("chip", "serial", "telnet")

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
