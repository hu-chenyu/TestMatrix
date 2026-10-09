"""
存量用例 source_ref 回填脚本（Day47，真实执行器 Phase-1 Day3）

用途:
    Day46 已把 test_cases.source_ref 列落地，但迁移只补 None、**不猜值**，
    故存量用例的 source_ref 全为 NULL。本脚本按 docs/real_executor_integration_design.md
    第 5.3 节的三档策略把可自动判定的部分回填，其余明确标记为待人工处理。

三档策略（判据与设计文档 5.3 一一对应）:
    A 自动提取档（auto_extract）
        description/name 中含 `.py` 形态的路径（含 `::类名::函数名` 后缀）。
        正则提取后**必须校验文件在项目根下真实存在**才写入——这是防
        "配了路径跑不了"最有效的一道闸：写进去一个不存在的路径，比留空
        更坏（留空会明确报"不可被 pytest 执行"，不存在的路径会报
        "file or directory not found"，排障方向完全不同）。
        文件不存在则跳过并打 WARNING，计入 skipped_file_not_found。

    B 人工补录档（manual）
        A 档未命中、且不属于 C 档的用例。source_ref 保持 NULL，
        输出待补录清单交录入人处理。

    C 模拟执行专用档（simulation_only）
        用例类型为 api/接口/模拟/mock 一类（无对应 pytest 脚本文件的
        业务用例）。source_ref 保持 NULL，空值语义 = **不可被 pytest
        执行、走模拟降级**，不是数据缺失。

    **判定顺序是 A → C → B**：含 `.py` 路径的用例一律先尝试自动提取，
    只有在没有可提取路径时才去判 C 档。否则一条"接口用例的验证脚本在
    name 里写了路径"的用例会因类型是 api 被误判成"模拟专用"，而它其实
    恰恰是唯一能被真实执行的那条。

硬约束（与 ADR-001 决策③对齐，改动前务必确认没破坏）:
    1. **幂等**: 只处理 source_ref IS NULL 的行，且 UPDATE 语句自带
       `source_ref IS NULL` 条件。重复运行第二次 updated=0，不会覆盖
       已回填的值（含人工补录过的值）。
    2. **只写 source_ref 一列**: UPDATE 语句只列 source_ref，
       不触碰 case_id/name/description/module/priority/status 等任何
       其它字段，也不改 updated_at（ORM 的 onupdate 不参与，因为走的是
       text() 裸 SQL）。
    3. **不回落 case_id**: 空值保持为空。"该用例不可被 pytest 执行"
       是这条列存在的语义，回填出一个猜测值等于抹平语义。

运行方式:
    # 先跑迁移确保列存在，再回填
    .venv\\Scripts\\python.exe -m src.db.migration
    # 预演（只输出清单，不写库）——推荐先跑这个
    .venv\\Scripts\\python.exe -m src.scripts.backfill_source_ref --dry-run
    # 正式回填
    .venv\\Scripts\\python.exe -m src.scripts.backfill_source_ref
    # 分批回填（每次只处理前 N 条 NULL 用例）
    .venv\\Scripts\\python.exe -m src.scripts.backfill_source_ref --limit 50
    # 指定项目根（校验文件存在性时的基准目录）
    .venv\\Scripts\\python.exe -m src.scripts.backfill_source_ref --project-root .

    直接脚本方式（不带 -m）同样可用，文件内已把项目根插入 sys.path。

注意（并发安全）:
    本脚本会真实写库。**严禁与全量 pytest 并发运行**——pytest 的
    --alluredir 固定指向 output/allure_results，两个进程会互相删/占该
    目录导致 INTERNALERROR（见项目已知坑记录）。
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Any, cast

# 项目根目录（src/scripts/ 的上两级）必须在 import src.* 之前确定并加入
# sys.path，保证直接脚本方式（不带 -m）与模块方式两种都能导入业务模块
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import CursorResult, inspect, text  # noqa: E402 (需先补 sys.path)
from src.common.logger import LogManager  # noqa: E402
from src.core.case_manager import (  # noqa: E402
    MAX_SOURCE_REF_LENGTH,
    SOURCE_REF_PATTERN,
)
from src.db.db_session import DatabaseSession  # noqa: E402
from src.db.models import TestCase as CaseModel  # noqa: E402

logger = LogManager.get_logger()

# 三档判定结果的取值（单一事实来源，报表与测试都按这三个字面量比对）
TIER_AUTO = "auto_extract"
TIER_MANUAL = "manual"
TIER_SIMULATION = "simulation_only"

# C 档（模拟执行专用）关键词：小写匹配，命中任一即判该档。
# case_type 走枚举精确比对（api/chip），module/name/description 走关键词
# 子串匹配——存量数据的"接口/模拟/mock"写法五花八门（实测有"接口"、
# "模拟"、"mock" 等），精确枚举会漏掉一大半，漏判的直接后果是这些
# 用例被报成"待人工补录"，人工去给一条本来就没有 .py 的用例找路径。
SIMULATION_KEYWORDS = ("api", "接口", "模拟", "mock")

# 模拟执行专用档的 case_type 精确取值（与 routes/cases.py 的
# VALID_CASE_TYPES 对齐：api=HTTP接口，chip=串口/Telnet协议。chip 是真实
# 硬件执行，不属本档）
SIMULATION_CASE_TYPES = ("api",)

# 从自由文本中提取候选路径的正则（比 SOURCE_REF_PATTERN 宽）：
# SOURCE_REF_PATTERN 是**整串**匹配（用于校验），这里的任务是在一段
# 自然语言里"捞出"看起来像路径的片段，故用 finditer 逐个候选再送进
# SOURCE_REF_PATTERN 复验。候选不能跨越空白/中文标点，否则会把
# "验证 tests/a.py 通过"整句都吞进来。
_CANDIDATE_PATTERN = re.compile(r"[\w./\\-]+\.py(?:::\w+)*")


def extract_source_ref(text_content: str | None) -> str | None:
    """
    从自由文本中提取第一个合法的 source_ref（内部函数）

    与 SOURCE_REF_PATTERN 的分工: 后者是整串校验（用于拒绝"invalid
    path!!"这类输入），本函数负责在散文里**定位**候选。因此这里用
    宽松的候选正则逐个 finditer，再用严格的 SOURCE_REF_PATTERN 复验，
    两条正则各司其职。

    参数:
        text_content (str | None): 待检索文本（description 或 name）

    返回:
        str | None: 第一个通过复验的候选路径；无合法候选时为 None

    异常:
        无
    """
    if not text_content:
        return None
    for match in _CANDIDATE_PATTERN.finditer(str(text_content)):
        # P3-2（Day47-fix）: Windows 风格反斜杠路径归一为 POSIX 正斜杠。
        # 候选正则刻意含 `\`（存量用例的描述里两种写法都有），而
        # SOURCE_REF_PATTERN 不含 `\` —— 不归一的话 `tests\foo.py` 会被
        # 复验判为非法并**静默跳过**，该用例随后被报成"待人工补录"，
        # 而它明明写了一个可用路径。归一后 `tests\foo.py` → `tests/foo.py`
        # 即可通过复验，且 pytest 本身也接受正斜杠路径。
        candidate = match.group(0).replace("\\", "/")
        if SOURCE_REF_PATTERN.match(candidate):
            return candidate
    return None


def file_part(source_ref: str) -> str:
    """
    取出 source_ref 中的文件路径部分（去掉 ::函数名 后缀）

    参数:
        source_ref (str): 形如 tests/x.py 或 tests/x.py::TestC::test_y

    返回:
        str: 文件路径部分（tests/x.py）
    """
    return source_ref.split("::")[0]


def resolve_file_exists(source_ref: str, project_root: Path) -> bool:
    """
    校验 source_ref 指向的文件在项目根下真实存在

    为什么要做存在性校验: 回填值会直接被拼进 pytest 子进程命令
    （PytestRunner.build_command），一个不存在的路径会让该用例以
    "file or directory not found" 退出码 4 收场，且报错完全指不到
    "路径配错了"。留空则会明确报"该用例不可被 pytest 执行"——两种
    错误的排查方向截然不同，所以宁可留空并点名，不可猜着写。

    同时拒绝越出项目根的路径（../ 前缀），它与"文件不存在"同义处理。

    参数:
        source_ref (str): 待校验的 source_ref
        project_root (Path): 项目根目录（相对路径的基准）

    返回:
        bool: 文件真实存在且位于项目根内时为 True

    异常:
        无（路径非法或不存在都返回 False，由调用方计入跳过统计）
    """
    relative = file_part(source_ref)
    if not relative:
        return False
    # Path.resolve() 后判断 is_relative_to 项目根，挡住 ../ 越界
    try:
        target = (project_root / relative).resolve()
        root = project_root.resolve()
    except OSError:
        return False
    if not target.is_relative_to(root):
        return False
    return target.is_file()


def classify_case(case: dict[str, Any]) -> str:
    """
    判定单条用例应归入哪一档（内部函数）

    判定顺序 A → C → B（理由见模块 docstring 的"判定顺序"段）。

    参数:
        case (dict[str, Any]): 用例行映射（至少含 name/description/
                               case_type/module 字段，缺项按空串处理）

    返回:
        str: TIER_AUTO / TIER_SIMULATION / TIER_MANUAL 三者之一
    """
    # A 档：description/name 中含 .py 形态路径
    haystack = f"{case.get('description') or ''} {case.get('name') or ''}"
    if extract_source_ref(haystack) is not None:
        return TIER_AUTO

    # C 档：api/接口/模拟/mock 类用例（无对应 pytest 脚本）
    case_type = str(case.get("case_type") or "").strip().lower()
    if case_type in SIMULATION_CASE_TYPES:
        return TIER_SIMULATION
    # C 档关键词扫描覆盖 module/name/description 三个字段
    # （P3-1 Day47-fix: 原实现只扫 module/name，与上方常量注释里写的
    # "module/name/description" 不一致）。补上 description 的理由不只是
    # 注释对齐——存量用例常把接口性质写在描述里（实测 dev 库大量
    # description 形如"标签: smoke, api"），只扫 name/module 会把这些
    # 本该判 C 档的用例报成"待人工补录"，人工去给一条根本没有 .py 的
    # 接口用例找路径。两档都不写库，但报表归类会误导补录方向。
    lowered = " ".join(
        str(case.get(field) or "").lower()
        for field in ("module", "name", "description")
    )
    if any(keyword in lowered for keyword in SIMULATION_KEYWORDS):
        return TIER_SIMULATION

    # B 档：其余，交人工补录
    return TIER_MANUAL


def backfill_source_ref(
    project_root: Path,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """
    执行存量 source_ref 回填（核心逻辑）

    流程:
        1. 校验 source_ref 列存在（不存在即报错并提示先跑迁移）
        2. 查询 source_ref IS NULL 的用例（按 id 升序，保证多次运行
           的处理顺序稳定，是幂等性的一部分）
        3. 逐条按三档策略判定；A 档额外校验文件存在
        4. 仅对"提取成功且文件存在"的行执行单列 UPDATE

    参数:
        project_root (Path): 项目根目录（文件存在性校验基准）
        dry_run (bool): True 时只统计与输出清单、不写库，默认 False
        limit (int | None): 只处理前 N 条 NULL 用例（分批回填用），
                            None 表示全量

    返回:
        dict[str, Any]: 统计字典，含 total / auto_extracted /
            skipped_file_not_found / pending_manual / simulation_only /
            updated / to_backfill / pending_list / not_found_list

    异常:
        RuntimeError: test_cases 表不存在 source_ref 列时抛出
            （提示先执行 Day46 的迁移脚本）
        SQLAlchemyError: 数据库操作异常时向上抛出（由 main 负责回滚
            与错误提示；session_scope 上下文管理器已自动回滚）
    """
    engine = DatabaseSession.get_engine()
    columns = {
        str(col["name"]) for col in inspect(engine).get_columns("test_cases")
    }
    if "source_ref" not in columns:
        raise RuntimeError(
            "test_cases 表不存在 source_ref 列，请先执行迁移: "
            ".venv\\Scripts\\python.exe -m src.db.migration"
        )

    stats: dict[str, Any] = {
        "total": 0,
        "auto_extracted": 0,
        "skipped_file_not_found": 0,
        "pending_manual": 0,
        "simulation_only": 0,
        "updated": 0,
        # 将回填的清单（dry-run 时是全部，非 dry-run 时是实际写入的）
        "to_backfill": [],
        # 待人工补录清单
        "pending_list": [],
        # 提取到路径但文件不存在、必须人工核对的清单
        "not_found_list": [],
    }

    with DatabaseSession.session_scope() as session:
        query = (
            session.query(CaseModel)
            .filter(CaseModel.source_ref.is_(None))
            .order_by(CaseModel.id)
        )
        if limit is not None:
            query = query.limit(limit)
        rows = query.all()
        stats["total"] = len(rows)

        for row in rows:
            case = {
                "case_id": row.case_id,
                "name": row.name,
                "module": row.module,
                "case_type": row.case_type,
                "description": row.description,
            }
            tier = classify_case(case)
            case_id = str(row.case_id)

            if tier == TIER_SIMULATION:
                # C 档：空值即语义（不可被 pytest 执行），不写库
                stats["simulation_only"] += 1
                continue

            if tier == TIER_MANUAL:
                stats["pending_manual"] += 1
                stats["pending_list"].append(case_id)
                logger.info(f"待人工补录 source_ref | 用例: {case_id} | {row.name}")
                continue

            # A 档：提取 + 存在性校验
            haystack = f"{row.description or ''} {row.name or ''}"
            candidate = extract_source_ref(haystack)
            stats["auto_extracted"] += 1
            # 长度兜底: 提取值来自自由文本，理论上可能超长。SQLite 不实现
            # VARCHAR 长度约束（Day46 实测），库层不会拦，写进去就是脏数据
            if candidate is None or len(candidate) > MAX_SOURCE_REF_LENGTH:
                stats["skipped_file_not_found"] += 1
                stats["not_found_list"].append(
                    {"case_id": case_id, "source_ref": candidate}
                )
                logger.warning(
                    f"source_ref 缺失或超长，跳过回填 | 用例: {case_id} | "
                    f"提取到: {candidate!r}"
                )
                continue
            if not resolve_file_exists(candidate, project_root):
                stats["skipped_file_not_found"] += 1
                stats["not_found_list"].append(
                    {"case_id": case_id, "source_ref": candidate}
                )
                logger.warning(
                    f"source_ref 文件不存在，跳过回填（多半是 case_id 命名"
                    f"不规范）| 用例: {case_id} | 提取到: {candidate!r}"
                )
                continue

            stats["to_backfill"].append({"case_id": case_id, "source_ref": candidate})
            if dry_run:
                logger.info(
                    f"[dry-run] 将回填 source_ref | 用例: {case_id} | {candidate}"
                )
                continue

            # 只写 source_ref 一列；WHERE 带 source_ref IS NULL 保证
            # 并发安全与幂等（即使本行在读取后被别人填了也不会被覆盖）
            #
            # rowcount 只能从 CursorResult 读：SQLAlchemy 2.0 里
            # Session.execute 的静态返回类型是 Result（无 rowcount），
            # 但 UPDATE 实际返回的是 CursorResult。此处显式收窄类型，
            # 而不是 getattr 兜底——后者在类型漂移时会静默返回 None
            # 并把 updated 悄悄记成 0（假绿）。
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    text(
                        "UPDATE test_cases SET source_ref = :ref "
                        "WHERE case_id = :case_id AND source_ref IS NULL"
                    ),
                    {"ref": candidate, "case_id": case_id},
                ),
            )
            stats["updated"] += result.rowcount or 0
            logger.info(f"source_ref 已回填 | 用例: {case_id} | {candidate}")

    return stats


def format_report(stats: dict[str, Any], dry_run: bool) -> str:
    """
    把统计字典渲染为可读报表（内部函数）

    参数:
        stats (dict[str, Any]): backfill_source_ref 的返回值
        dry_run (bool): 是否为预演模式（决定标题与"将回填"的措辞）

    返回:
        str: 多行报表文本
    """
    lines = [
        "=" * 60,
        "source_ref 存量回填报表（dry-run 预演）" if dry_run else "source_ref 存量回填报表",
        "=" * 60,
        f"待处理 NULL 用例总数 : {stats['total']}",
        f"A 自动提取命中        : {stats['auto_extracted']}",
        f"  其中文件不存在跳过  : {stats['skipped_file_not_found']}",
        f"B 待人工补录          : {stats['pending_manual']}",
        f"C 模拟执行专用        : {stats['simulation_only']}",
    ]
    if dry_run:
        lines.append(f"将回填（未写库）      : {len(stats['to_backfill'])}")
    else:
        lines.append(f"实际写入库条数        : {stats['updated']}")

    if stats["to_backfill"]:
        lines.append("-" * 60)
        lines.append("回填清单:")
        for item in stats["to_backfill"]:
            lines.append(f"  {item['case_id']} -> {item['source_ref']}")
    if stats["not_found_list"]:
        lines.append("-" * 60)
        lines.append("文件不存在（需人工核对，多半是命名不规范）:")
        for item in stats["not_found_list"]:
            lines.append(f"  {item['case_id']} -> {item['source_ref']!r}")
    if stats["pending_list"]:
        lines.append("-" * 60)
        lines.append(f"待人工补录清单（共 {len(stats['pending_list'])} 条）:")
        for case_id in stats["pending_list"]:
            lines.append(f"  {case_id}")
    if stats["simulation_only"]:
        lines.append("-" * 60)
        lines.append(
            f"模拟执行专用: {stats['simulation_only']} 条"
            f"（空值语义=不可被 pytest 执行，非数据缺失）"
        )
    lines.append("=" * 60)
    return "\n".join(lines)


def main() -> None:
    """
    命令行入口

    参数:
        无（从命令行读取 --dry-run / --limit / --project-root）

    返回:
        None

    异常:
        RuntimeError: source_ref 列不存在时打印错误并以退出码 1 结束
        SQLAlchemyError: 数据库异常时打印错误并以退出码 1 结束
            （session_scope 已自动回滚，不会留下半回填状态）
    """
    parser = argparse.ArgumentParser(
        prog="python -m src.scripts.backfill_source_ref",
        description="存量用例 source_ref 三档回填脚本（幂等、只写 source_ref 一列）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出将回填的清单，不实际写库（推荐首次先跑这个）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只处理前 N 条 source_ref 为 NULL 的用例（分批回填用）",
    )
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
        help="项目根目录（source_ref 文件存在性校验的基准），默认项目根",
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须为正整数")

    project_root = Path(args.project_root).resolve()
    logger.info(
        f"source_ref 回填开始 | dry_run: {args.dry_run} | limit: {args.limit} | "
        f"项目根: {project_root}"
    )

    try:
        stats = backfill_source_ref(
            project_root=project_root,
            dry_run=args.dry_run,
            limit=args.limit,
        )
    except Exception as exc:  # noqa: BLE001 CLI 顶层兜底，不甩 traceback
        # CLI 入口不能把 traceback 甩给使用者。异常消息已按类型区分：
        # RuntimeError = "列不存在，先跑迁移"这类可操作的前置条件问题；
        # 其它异常 = 数据库/文件系统故障，一并如实报出类型名。
        logger.error(f"source_ref 回填失败: {type(exc).__name__}: {exc}")
        raise SystemExit(1) from exc

    # CLI 边界报表走 stdout（与项目既有 CLI 脚本一致，便于 CI 日志直读）；
    # 业务代码内部一律用 loguru，不用 print
    print(format_report(stats, args.dry_run))
    if args.dry_run:
        logger.info("dry-run 模式：未写库。确认清单无误后去掉 --dry-run 正式执行")
    else:
        logger.info(
            f"source_ref 回填完成 | 实际更新: {stats['updated']} 条"
            f"（重复执行第二次应为 0，即幂等）"
        )


if __name__ == "__main__":
    main()