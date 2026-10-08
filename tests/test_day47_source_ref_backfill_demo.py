"""
source_ref 改造 Day2 测试（Day47：存量回填 + 录入侧三侧放开 + build_command 改造）

测试范围（30 条）:
    1. 校验函数（6 条）: 合法路径/函数级路径/未配置值归一/非法格式/
       超长/上级目录/非字符串类型
    2. 回填分档（5 条）: A 档优先于 C 档、C 档关键词、B 档、函数级提取
    3. 回填执行（6 条）: 三档落库、文件不存在跳过、幂等、只写一列、
       dry-run、--limit
    4. 录入侧（9 条）: YAML 有/无 source_ref、YAML 非法整批拒绝、
       Excel 有/无 source_ref 列、创建 API 可选与非法、更新 API 可改与非法
    5. build_command（4 条）: source_ref 优先、空值抛 ValueError、
       run_one 降级为 error、script_path 过渡兼容

数据隔离设计:
    - 数据库: monkeypatch 把 TM_DB_SQLITE_PATH 指向 tmp_path 下独立库，
      每个用例前后 DatabaseSession.reset()（Windows 文件锁）
    - 回填脚本的文件存在性校验: fake_project_root fixture 在 tmp_path 下
      自建目录并造真实 .py 文件，**绝不依赖仓库既有文件**——否则将来
      那个文件被改名/删除，本用例会从"回填成功"静默变成"跳过"，
      而测试依然通过（断言只看计数分布的自洽性）
    - 录入侧 YAML/Excel: tmp_path 现场构造，不依赖 tests/testdata 存量
    - 零真实网络/硬件依赖

别名导入约定: ORM 模型类 TestCase 以 models.TestCase 方式引用，不直接
import TestCase——后者会被 pytest 的 python_classes=Test* 启发式当成测试
类收集并抛 PytestCollectionWarning，破坏 0 warning 门禁（见 7.50）。
"""

from collections.abc import Iterator
from pathlib import Path

import allure
import pytest
import yaml
from flask.testing import FlaskClient
from openpyxl import Workbook
from sqlalchemy import text
from src.common.source_ref import SOURCE_REF_PATTERN as common_source_ref_pattern
from src.core.case_manager import (
    MAX_SOURCE_REF_LENGTH,
    SOURCE_REF_PATTERN,
    CaseDataLoadError,
    CaseManager,
    _case_context,
    _normalize_clear_marker,
    validate_source_ref,
)
from src.core.executors import PytestRunner
from src.db import models
from src.db.db_session import DatabaseSession
from src.scripts.backfill_source_ref import (
    TIER_AUTO,
    TIER_MANUAL,
    TIER_SIMULATION,
    backfill_source_ref,
    classify_case,
    extract_source_ref,
    file_part,
    format_report,
    main,
    resolve_file_exists,
)
from src.web import create_app

# 项目根目录（隔离守卫测试要断言"引擎不指向真实库"，需要该路径）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ===========================================================================
# 造数与读数辅助
# ===========================================================================
def _seed_case(case_id: str, **fields: object) -> None:
    """
    写入单条用例（source_ref 默认留空，模拟存量状态）

    参数:
        case_id (str): 业务编号
        **fields (object): 其余列值（name/module/case_type/description 等）

    返回:
        None
    """
    with DatabaseSession.session_scope() as session:
        session.add(models.TestCase(case_id=case_id, **fields))


def _read_case(case_id: str) -> dict:
    """
    用裸 SQL 读出整行（含全部列），用于逐字段比对

    刻意不走 ORM："只写 source_ref 一列"这条断言要看的是**整行**是否
    原样，ORM 只投影模型声明的列，看不到未声明列的变化。

    参数:
        case_id (str): 业务编号

    返回:
        dict: 该行的全部列字典（读不到时返回空字典）
    """
    with DatabaseSession.get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT * FROM test_cases WHERE case_id = :cid"),
            {"cid": case_id},
        ).mappings().first()
    return dict(row) if row is not None else {}


def _write_yaml(path: Path, cases: list[dict]) -> Path:
    """
    把用例列表写成 YAML 数据文件并返回路径

    参数:
        path (Path): 目标文件路径
        cases (list[dict]): 用例字典列表

    返回:
        Path: 写入的路径
    """
    path.write_text(
        yaml.safe_dump(cases, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return path


def _write_excel(path: Path, headers: list[str], rows: list[list]) -> Path:
    """
    按给定表头与数据行写出 Excel 文件并返回路径

    参数:
        path (Path): 目标文件路径
        headers (list[str]): 表头字段名列表
        rows (list[list]): 数据行列表

    返回:
        Path: 写入的路径
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()
    return path


# ===========================================================================
# 夹具
# ===========================================================================
@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """
    临时 SQLite 库夹具（建好表，前后 reset 引擎防 Windows 文件锁）

    参数:
        tmp_path (Path): pytest 临时目录
        monkeypatch (pytest.MonkeyPatch): 环境变量补丁工具

    返回:
        Iterator[Path]: yield 临时库文件路径
    """
    db_path = tmp_path / "day47.db"
    monkeypatch.setenv("TM_DB_TYPE", "sqlite")
    monkeypatch.setenv("TM_DB_SQLITE_PATH", str(db_path))
    DatabaseSession.reset()
    DatabaseSession.init_db()
    yield db_path
    DatabaseSession.reset()


@pytest.fixture()
def fake_project_root(tmp_path: Path) -> Path:
    """
    回填脚本用的假项目根（内含真实 .py 文件）

    参数:
        tmp_path (Path): pytest 临时目录

    返回:
        Path: 假项目根目录（tests/demo.py 与 tests/demo2.py 真实存在）
    """
    root = tmp_path / "fake_project"
    tests_dir = root / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "demo.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (tests_dir / "demo2.py").write_text(
        "def test_ok2():\n    assert True\n", encoding="utf-8"
    )
    return root


@pytest.fixture()
def crud_client(temp_db: Path) -> Iterator[FlaskClient]:
    """
    创建/更新 API 测试客户端（复用 temp_db 临时库）

    参数:
        temp_db (Path): 临时库路径（仅确保夹具顺序）

    返回:
        Iterator[FlaskClient]: yield Flask 测试客户端
    """
    yield create_app("test").test_client()
    DatabaseSession.reset()


# ===========================================================================
# 1. source_ref 校验函数
# ===========================================================================
@allure.feature("Day47-source_ref改造Day2")
@allure.story("source_ref格式校验")
class TestSourceRefValidation:
    """validate_source_ref：YAML/Excel/API 三侧共用的单一校验函数"""

    @allure.story("合法路径与函数级路径通过")
    def test_valid_paths_accepted(self):
        """纯文件路径与带 ::Class::method 的函数级路径都应原样通过"""
        assert validate_source_ref("tests/demo.py") == "tests/demo.py"
        assert (
            validate_source_ref("tests/demo.py::TestBar::test_baz")
            == "tests/demo.py::TestBar::test_baz"
        )

    @allure.story("未配置值统一归一为 None")
    def test_none_and_blank_normalized(self):
        """None/空串/纯空格都表示"未配置执行目标"，归一为 None"""
        assert validate_source_ref(None) is None
        assert validate_source_ref("") is None
        assert validate_source_ref("   ") is None
        assert validate_source_ref("  tests/demo.py  ") == "tests/demo.py"

    @allure.story("格式非法明确拒绝")
    def test_invalid_format_rejected(self):
        """含空格/缺 .py 后缀/含反斜杠的值必须报错且消息含字段名"""
        for bad in ["invalid path!!", "tests/demo", "tests\\demo.py"]:
            with pytest.raises(ValueError) as excinfo:
                validate_source_ref(bad, context="用例TM-X-0001: ")
            assert "source_ref" in str(excinfo.value), (
                f"错误消息必须含字段名，实际: {excinfo.value}"
            )

    @allure.story("超过 512 字符拒绝")
    def test_too_long_rejected(self):
        """超长值必须拒绝——SQLite 不实现 VARCHAR 长度约束，库层不拦"""
        too_long = "tests/" + ("a" * (MAX_SOURCE_REF_LENGTH - 5)) + ".py"
        assert len(too_long) > MAX_SOURCE_REF_LENGTH
        with pytest.raises(ValueError) as excinfo:
            validate_source_ref(too_long)
        assert "超长" in str(excinfo.value)

    @allure.story("禁止上级目录 ..")
    def test_parent_dir_rejected(self):
        """source_ref 会拼进 pytest 子进程命令，放行 .. 等于让路径越出项目根"""
        with pytest.raises(ValueError) as excinfo:
            validate_source_ref("../outside/demo.py")
        assert ".." in str(excinfo.value)

    @allure.story("非字符串类型拒绝")
    def test_non_string_rejected(self):
        """Excel 数值单元格会被 openpyxl 存成 int/float，必须显式拒绝"""
        with pytest.raises(ValueError):
            validate_source_ref(12345)


# ===========================================================================
# 2. 回填脚本三档分类
# ===========================================================================
@allure.feature("Day47-source_ref改造Day2")
@allure.story("存量回填三档策略")
@pytest.mark.usefixtures("temp_db")
class TestBackfillClassification:
    """classify_case / extract_source_ref 的分档与提取行为"""

    @allure.story("A 档：文本含 .py 路径")
    def test_auto_extract_tier(self):
        """description/name 含 .py 形态路径即判 A 档，且能提取出来"""
        case = {
            "case_id": "TM-A-0001",
            "name": "登录校验",
            "module": "用户中心",
            "case_type": "api",
            "description": "验证 tests/demo.py 覆盖登录流程",
        }
        assert classify_case(case) == TIER_AUTO
        assert extract_source_ref(case["description"]) == "tests/demo.py"

    @allure.story("A 档优先于 C 档：api 类型但有路径仍判 A")
    def test_auto_tier_wins_over_simulation(self):
        """
        判定顺序必须是 A → C → B。

        反例后果: 一条 case_type=api 的接口用例，若其验证脚本路径写在
        name 里，若先判 C 档就会被标成"模拟执行专用"而留空——而它恰恰
        是这批数据里唯一能被真实执行的那条，判错等于丢掉唯一可执行目标。
        """
        case = {
            "case_id": "TM-A-0002",
            "name": "接口校验 tests/demo.py",
            "module": "用户中心",
            "case_type": "api",
            "description": "",
        }
        assert classify_case(case) == TIER_AUTO, (
            "含 .py 路径的 api 用例必须优先判 A 档"
        )

    @allure.story("C 档：api/接口/模拟 类用例")
    def test_simulation_only_tier(self):
        """api 类型、以及 module/name 含接口/模拟/mock 的用例判 C 档"""
        assert classify_case(
            {
                "case_id": "TM-C-0001",
                "name": "用户查询",
                "module": "用户管理",
                "case_type": "api",
                "description": "标签: smoke",
            }
        ) == TIER_SIMULATION
        assert classify_case(
            {
                "case_id": "TM-C-0002",
                "name": "模拟下单",
                "module": "交易",
                "case_type": "chip",
                "description": "",
            }
        ) == TIER_SIMULATION, "名称含'模拟'应判 C 档"

    @allure.story("B 档：其余用例待人工补录")
    def test_manual_tier(self):
        """无 .py 路径、非接口类的用例判 B 档（待人工补录）"""
        assert classify_case(
            {
                "case_id": "TM-B-0001",
                "name": "板卡上电自检",
                "module": "硬件",
                "case_type": "chip",
                "description": "上电后读取寄存器",
            }
        ) == TIER_MANUAL

    @allure.story("从 name 中提取函数级路径")
    def test_extract_function_level_ref(self):
        """name 里的 ::Class::method 形态也应被完整提取"""
        assert (
            extract_source_ref("回归 tests/demo.py::TestBar::test_baz")
            == "tests/demo.py::TestBar::test_baz"
        )
        assert extract_source_ref("没有路径的描述") is None

    @allure.story("P3-1 description 含 api 关键词判 C 档")
    def test_simulation_tier_matches_description_keyword(self):
        """
        C 档关键词必须覆盖 description（Day47-fix P3-1）。

        原实现只扫 module/name，与常量注释写的
        "module/name/description" 不一致。存量用例常把接口性质写在
        描述里（实测 dev 库大量 description 形如"标签: smoke, api"），
        只扫 name/module 会把这些本该判 C 档的用例报成"待人工补录"，
        人工去给一条根本没有 .py 的接口用例找路径。
        """
        case = {
            "case_id": "TM-C-0003",
            "name": "查询返回结构",  # name/module 都不含关键词
            "module": "用户管理",
            "case_type": "chip",  # 枚举也不含 api
            "description": "标签: smoke, api",  # 只有 description 含
        }
        assert classify_case(case) == TIER_SIMULATION, (
            "description 含 api 关键词必须判 C 档"
        )

    @allure.story("P3-2 Windows 反斜杠路径归一为 POSIX")
    def test_extract_normalizes_backslash(self):
        """
        `tests\\foo.py` 必须被归一为 `tests/foo.py` 而不是静默跳过
        （Day47-fix P3-2）。

        原实现候选正则含 `\\` 但复验正则不含，Windows 风格写法复验
        不过即被丢弃，该用例随后被报成"待人工补录"——而它明明写了
        一个可用路径。静默跳过比报错更难发现。
        """
        assert (
            extract_source_ref(r"覆盖 tests\demo.py 验证登录")
            == "tests/demo.py"
        ), "反斜杠必须归一为正斜杠"
        assert (
            extract_source_ref(r"回归 tests\demo.py::TestBar::test_baz")
            == "tests/demo.py::TestBar::test_baz"
        ), "函数级后缀也要一并保留并归一"


# ===========================================================================
# 3. 回填脚本执行行为
# ===========================================================================
@allure.feature("Day47-source_ref改造Day2")
@allure.story("回填脚本执行")
@pytest.mark.usefixtures("temp_db")
class TestBackfillExecution:
    """回填主流程：分档落库、幂等、只写一列、dry-run、limit"""

    @allure.story("三档分别落到正确结果")
    def test_three_tiers_executed(
        self, temp_db: Path, fake_project_root: Path
    ) -> None:
        """A 档回填成功 / C 档与 B 档保持 NULL 且分类计数正确"""
        _seed_case(
            "TM-EXE-A1",
            name="登录校验",
            module="用户中心",
            case_type="chip",
            description="验证 tests/demo.py 覆盖登录",
        )
        _seed_case(
            "TM-EXE-C1",
            name="用户查询",
            module="用户管理",
            case_type="api",
            description="标签: smoke",
        )
        _seed_case(
            "TM-EXE-B1",
            name="上电自检",
            module="硬件",
            case_type="chip",
            description="读寄存器",
        )

        stats = backfill_source_ref(project_root=fake_project_root)

        assert stats["total"] == 3
        assert stats["auto_extracted"] == 1
        assert stats["simulation_only"] == 1
        assert stats["pending_manual"] == 1
        assert stats["updated"] == 1, "只有 A 档且文件存在时才写库"
        assert _read_case("TM-EXE-A1")["source_ref"] == "tests/demo.py"
        assert _read_case("TM-EXE-C1")["source_ref"] is None, "C 档必须保持 NULL"
        assert _read_case("TM-EXE-B1")["source_ref"] is None, "B 档必须保持 NULL"

    @allure.story("A 档文件不存在则跳过并计入统计")
    def test_auto_tier_file_not_found_skipped(
        self, temp_db: Path, fake_project_root: Path
    ) -> None:
        """
        提取到路径但文件不存在 → 不写库 + 计入 not_found。

        反例后果: 写进去后 pytest 会报 file or directory not found，
        报错完全指不到"路径配错了"，而留空会明确报"不可被 pytest 执行"。
        """
        _seed_case(
            "TM-EXE-MISS",
            name="缺失路径",
            module="用户中心",
            case_type="chip",
            description="验证 tests/not_exist.py 覆盖缺失",
        )

        stats = backfill_source_ref(project_root=fake_project_root)

        assert stats["auto_extracted"] == 1
        assert stats["skipped_file_not_found"] == 1
        assert stats["updated"] == 0
        assert _read_case("TM-EXE-MISS")["source_ref"] is None

    @allure.story("幂等：第二次运行 updated=0")
    def test_backfill_is_idempotent(
        self, temp_db: Path, fake_project_root: Path
    ) -> None:
        """
        连续两次回填，第二次必须 0 写入且已回填值不被改写。

        同时验证"已有人工补录值不被覆盖"：第三条用例先手工填一个
        非空 source_ref，回填脚本只处理 NULL 行，绝不能把它冲掉。
        """
        _seed_case(
            "TM-IDEM-1",
            name="登录",
            module="用户中心",
            case_type="chip",
            description="验证 tests/demo.py",
        )
        _seed_case(
            "TM-IDEM-2",
            name="下单",
            module="交易",
            case_type="chip",
            description="验证 tests/demo2.py",
        )
        _seed_case(
            "TM-IDEM-MANUAL",
            name="人工补录",
            module="硬件",
            case_type="chip",
            description="人工填写",
            source_ref="tests/demo.py",
        )

        first = backfill_source_ref(project_root=fake_project_root)
        assert first["updated"] == 2

        second = backfill_source_ref(project_root=fake_project_root)
        assert second["updated"] == 0, "第二次运行不得重复写入"
        assert second["total"] == 0, "已回填的非 NULL 行不再进入处理范围"
        assert _read_case("TM-IDEM-MANUAL")["source_ref"] == "tests/demo.py", (
            "人工补录值不得被脚本覆盖"
        )

    @allure.story("只写 source_ref 一列")
    def test_only_source_ref_column_written(
        self, temp_db: Path, fake_project_root: Path
    ) -> None:
        """回填前后除 source_ref 外整行完全一致（含 updated_at）"""
        _seed_case(
            "TM-ONLY-1",
            name="登录校验",
            module="用户中心",
            priority="P1",
            case_type="chip",
            status="active",
            description="验证 tests/demo.py 覆盖登录",
            creator="legacy",
        )
        before = _read_case("TM-ONLY-1")

        stats = backfill_source_ref(project_root=fake_project_root)
        assert stats["updated"] == 1

        after = _read_case("TM-ONLY-1")
        assert after["source_ref"] == "tests/demo.py"
        for column, value in before.items():
            if column == "source_ref":
                continue
            assert after[column] == value, (
                f"回填只允许写 source_ref，但列 {column} 发生了变化: "
                f"{value!r} -> {after[column]!r}"
            )

    @allure.story("dry-run 只统计不写库")
    def test_dry_run_does_not_write(
        self, temp_db: Path, fake_project_root: Path
    ) -> None:
        """dry-run 必须给出完整清单且库内一行未改"""
        _seed_case(
            "TM-DRY-1",
            name="登录",
            module="用户中心",
            case_type="chip",
            description="验证 tests/demo.py",
        )

        stats = backfill_source_ref(project_root=fake_project_root, dry_run=True)

        assert stats["to_backfill"], "dry-run 必须输出将回填的清单"
        assert stats["updated"] == 0, "dry-run 不得写库"
        assert _read_case("TM-DRY-1")["source_ref"] is None

    @allure.story("--limit 只处理前 N 条")
    def test_limit_processes_subset(
        self, temp_db: Path, fake_project_root: Path
    ) -> None:
        """分批回填：limit=1 时只处理 1 条，其余留待下一批"""
        _seed_case(
            "TM-LIMIT-1",
            name="用例1",
            module="用户中心",
            case_type="chip",
            description="验证 tests/demo.py",
        )
        _seed_case(
            "TM-LIMIT-2",
            name="用例2",
            module="用户中心",
            case_type="chip",
            description="验证 tests/demo2.py",
        )
        _seed_case(
            "TM-LIMIT-3",
            name="用例3",
            module="用户中心",
            case_type="chip",
            description="验证 tests/demo2.py",
        )

        stats = backfill_source_ref(project_root=fake_project_root, limit=1)

        assert stats["total"] == 1, "limit 必须限制处理条数"
        assert stats["updated"] == 1
        assert _read_case("TM-LIMIT-2")["source_ref"] is None, (
            "未在 limit 范围内的用例不得被回填"
        )


# ===========================================================================
# 4. 录入侧：YAML / Excel / API
# ===========================================================================
@allure.feature("Day47-source_ref改造Day2")
@allure.story("用例录入侧三侧放开")
@pytest.mark.usefixtures("temp_db")
class TestRecordSideSourceRef:
    """YAML/Excel 导入与创建/更新 API 的 source_ref 可选键与格式校验"""

    @allure.story("YAML 含 source_ref 导入后落库")
    def test_yaml_with_source_ref_persisted(self, tmp_path: Path) -> None:
        """YAML 写了 source_ref 时按该值入库"""
        path = _write_yaml(
            tmp_path / "with_ref.yaml",
            [
                {
                    "case_id": "TM-YML-0001",
                    "name": "登录校验",
                    "module": "用户中心",
                    "priority": "P1",
                    "source_ref": "tests/demo.py",
                },
                {
                    "case_id": "TM-YML-0002",
                    "name": "下单校验",
                    "module": "交易",
                    "priority": "P2",
                    "source_ref": "tests/demo.py::TestTrade::test_order",
                },
            ],
        )

        result = CaseManager.sync_cases_from_file(path)

        assert result["inserted"] == 2
        assert _read_case("TM-YML-0001")["source_ref"] == "tests/demo.py"
        assert (
            _read_case("TM-YML-0002")["source_ref"]
            == "tests/demo.py::TestTrade::test_order"
        ), "函数级 source_ref 应原样落库"

    @allure.story("YAML 不含 source_ref 时为 None（存量兼容）")
    def test_yaml_without_source_ref_is_none(self, tmp_path: Path) -> None:
        """存量 YAML 零改动继续可用：不写该键即落 None"""
        path = _write_yaml(
            tmp_path / "legacy.yaml",
            [
                {
                    "case_id": "TM-YML-LEGACY",
                    "name": "存量用例",
                    "module": "用户中心",
                    "priority": "P2",
                }
            ],
        )

        CaseManager.sync_cases_from_file(path)

        assert _read_case("TM-YML-LEGACY")["source_ref"] is None

    @allure.story("YAML 中 source_ref 格式非法整批拒绝")
    def test_yaml_invalid_source_ref_rejected(self, tmp_path: Path) -> None:
        """非法值必须让导入失败并报明确错误（消息含字段名），且不半截入库"""
        path = _write_yaml(
            tmp_path / "bad_ref.yaml",
            [
                {
                    "case_id": "TM-YML-BAD",
                    "name": "非法用例",
                    "module": "用户中心",
                    "priority": "P1",
                    "source_ref": "invalid path!!",
                }
            ],
        )

        with pytest.raises(CaseDataLoadError) as excinfo:
            CaseManager.sync_cases_from_file(path)

        assert "source_ref" in str(excinfo.value), (
            f"错误消息必须含字段名，实际: {excinfo.value}"
        )
        assert _read_case("TM-YML-BAD") == {}, (
            "校验失败时整批不入库，不得留下半截数据"
        )

    @allure.story("Excel 存量表头无 source_ref 列继续可用")
    def test_excel_without_source_ref_column(self, tmp_path: Path) -> None:
        """存量 Excel 零改动继续可用：不加列表头即落 None"""
        path = _write_excel(
            tmp_path / "legacy.xlsx",
            ["case_id", "name", "module", "priority"],
            [["TM-XLS-LEGACY", "存量Excel用例", "用户中心", "P2"]],
        )

        result = CaseManager.sync_cases_from_file(path)

        assert result["inserted"] == 1
        assert _read_case("TM-XLS-LEGACY")["source_ref"] is None

    @allure.story("Excel 含 source_ref 列按该值入库")
    def test_excel_with_source_ref_column(self, tmp_path: Path) -> None:
        """Excel 新增 source_ref 列表头后按该值入库"""
        path = _write_excel(
            tmp_path / "with_ref.xlsx",
            ["case_id", "name", "module", "priority", "source_ref"],
            [["TM-XLS-REF", "Excel用例", "用户中心", "P1", "tests/demo.py"]],
        )

        CaseManager.sync_cases_from_file(path)

        assert _read_case("TM-XLS-REF")["source_ref"] == "tests/demo.py"

    @allure.story("创建 API 可选 source_ref")
    def test_create_api_source_ref(self, crud_client: FlaskClient) -> None:
        """创建接口不传 source_ref 时为 None、传合法值时写入"""
        response = crud_client.post(
            "/api/cases/", json={"case_id": "TM-API-0001", "name": "接口用例"}
        )
        assert response.status_code == 201
        assert _read_case("TM-API-0001")["source_ref"] is None, (
            "不传即 None，存量客户端行为不变"
        )

        response = crud_client.post(
            "/api/cases/",
            json={
                "case_id": "TM-API-0002",
                "name": "接口用例2",
                "source_ref": "tests/demo.py",
            },
        )
        assert response.status_code == 201
        assert _read_case("TM-API-0002")["source_ref"] == "tests/demo.py"

    @allure.story("创建 API 非法 source_ref 返回 400")
    def test_create_api_invalid_source_ref(self, crud_client: FlaskClient) -> None:
        """非法值必须 400 且 message 含字段名"""
        response = crud_client.post(
            "/api/cases/",
            json={
                "case_id": "TM-API-BAD",
                "name": "非法用例",
                "source_ref": "invalid path!!",
            },
        )

        assert response.status_code == 400
        assert "source_ref" in response.get_data(as_text=True)

    @allure.story("更新 API 可改 source_ref")
    def test_update_api_source_ref(self, crud_client: FlaskClient) -> None:
        """更新接口可补录与清空 source_ref（空串归一为 None）"""
        crud_client.post(
            "/api/cases/", json={"case_id": "TM-UPD-0001", "name": "待补录用例"}
        )

        response = crud_client.put(
            "/api/cases/TM-UPD-0001", json={"source_ref": "tests/demo.py"}
        )
        assert response.status_code == 200
        assert _read_case("TM-UPD-0001")["source_ref"] == "tests/demo.py"

        response = crud_client.put(
            "/api/cases/TM-UPD-0001", json={"source_ref": ""}
        )
        assert response.status_code == 200
        assert _read_case("TM-UPD-0001")["source_ref"] is None, (
            "显式传空串应清空执行目标（撤销错误配置）"
        )

    @allure.story("更新 API 非法 source_ref 返回 400")
    def test_update_api_invalid_source_ref(self, crud_client: FlaskClient) -> None:
        """更新接口同样校验格式，非法值 400 且不落库"""
        crud_client.post(
            "/api/cases/", json={"case_id": "TM-UPD-BAD", "name": "待更新用例"}
        )

        response = crud_client.put(
            "/api/cases/TM-UPD-BAD", json={"source_ref": "tests/demo.txt"}
        )

        assert response.status_code == 400
        assert _read_case("TM-UPD-BAD")["source_ref"] is None

    @allure.story("P3-4 Excel 用 __CLEAR__ 显式清空 source_ref")
    def test_excel_clear_marker_clears_source_ref(self, tmp_path: Path) -> None:
        """
        Excel 通道必须也能表达"清空执行目标"（Day47-fix P3-4）。

        DataDriver 只把非 None 单元格放进字典，故 Excel 空单元格与
        "没有这一列"解析后完全一样、无法表达清空；YAML 可写
        `source_ref:`、API 可传 null，唯独 Excel 做不到。补一个显式
        标记值才补齐三侧口径。
        """
        # 先用 YAML 建一条带 source_ref 的用例
        yaml_path = _write_yaml(
            tmp_path / "seed.yaml",
            [
                {
                    "case_id": "TM-CLR-0001",
                    "name": "待清空用例",
                    "module": "用户中心",
                    "priority": "P1",
                    "source_ref": "tests/demo.py",
                }
            ],
        )
        CaseManager.sync_cases_from_file(yaml_path)
        assert _read_case("TM-CLR-0001")["source_ref"] == "tests/demo.py"

        # 再用 Excel 的 __CLEAR__ 标记清空
        excel_path = _write_excel(
            tmp_path / "clear.xlsx",
            ["case_id", "name", "module", "priority", "source_ref"],
            [["TM-CLR-0001", "待清空用例", "用户中心", "P1", "__CLEAR__"]],
        )
        result = CaseManager.sync_cases_from_file(excel_path)

        assert result["updated"] == 1
        assert _read_case("TM-CLR-0001")["source_ref"] is None, (
            "__CLEAR__ 必须把 source_ref 清成 NULL"
        )

    @allure.story("P3-4 标记值大小写与空白容错")
    def test_clear_marker_tolerates_case_and_space(self) -> None:
        """Excel 单元格常带首尾空白、大小写也可能被改动，归一时一并容错"""
        assert _normalize_clear_marker("__CLEAR__") is None
        assert _normalize_clear_marker("  __clear__  ") is None
        assert _normalize_clear_marker("tests/demo.py") == "tests/demo.py"
        assert _normalize_clear_marker(None) is None

    @allure.story("P3-5 校验失败消息含 case_id 与用例名")
    def test_invalid_source_ref_message_has_case_id_and_name(
        self, tmp_path: Path
    ) -> None:
        """
        报错必须同时点出编号与用例名（Day47-fix P3-5）。

        只给 case_id 时，收到报错的人得回数据文件逐行找"哪个编号
        写错了"，而导入场景里人往往只记得用例名。Excel 的真实行号在
        DataDriver 归一化后已不可得，case_id + name 是可得的最强组合。
        """
        path = _write_yaml(
            tmp_path / "bad_named.yaml",
            [
                {
                    "case_id": "TM-NAMED-0001",
                    "name": "登录失败用例",
                    "module": "用户中心",
                    "priority": "P1",
                    "source_ref": "invalid path!!",
                }
            ],
        )

        with pytest.raises(CaseDataLoadError) as excinfo:
            CaseManager.sync_cases_from_file(path)

        message = str(excinfo.value)
        assert "TM-NAMED-0001" in message, "消息必须含 case_id"
        assert "登录失败用例" in message, "消息必须含用例名"

    @allure.story("P3-5 无 name 时退化为仅 case_id")
    def test_context_falls_back_without_name(self) -> None:
        """name 为空时前缀退化为只含编号，不得抛错或留出多余括号"""
        assert _case_context({"case_id": "TM-1", "name": "登录"}) == "用例 TM-1（登录）: "
        assert _case_context({"case_id": "TM-1", "name": ""}) == "用例 TM-1: "
        assert _case_context({"case_id": "TM-1"}) == "用例 TM-1: "


# ===========================================================================
# 5. build_command 改造
# ===========================================================================
@allure.feature("Day47-source_ref改造Day2")
@allure.story("PytestRunner执行目标来源")
class TestPytestRunnerSourceRef:
    """build_command: source_ref 优先 + 空值显式报错 + 绝不回落 case_id"""

    @allure.story("source_ref 优先且排在 -- 之后")
    def test_source_ref_used(self):
        """有 source_ref 时用它作为执行目标，仍排在选项终止符之后"""
        command = PytestRunner().build_command(
            {"case_id": "TM-BC-0001", "source_ref": "tests/demo.py"}
        )
        assert command[-1] == "tests/demo.py"
        assert command.index("--") < command.index("tests/demo.py")
        assert "TM-BC-0001" not in command, "case_id 不得出现在命令里"

    @allure.story("source_ref 与 script_path 都为空时抛 ValueError")
    def test_empty_source_ref_raises(self):
        """
        绝不回落到 case_id：为空即语义"不可被 pytest 执行"。

        反例后果: 回落 case_id 后 pytest 报 file or directory not found
        并以退出码 4 收场，每条用例落 error，排障看到的是"路径错了"
        而非"这条用例本就不该走 pytest"。
        """
        with pytest.raises(ValueError) as excinfo:
            PytestRunner().build_command({"case_id": "TM-BC-0002"})

        message = str(excinfo.value)
        assert "source_ref" in message, f"异常消息必须点名字段: {message}"
        assert "TM-BC-0002" in message, "异常消息必须指明是哪条用例"

    @allure.story("run_one 把该异常降级为 error 结果")
    def test_run_one_maps_missing_source_ref_to_error(self):
        """编排层逐条 run_one，异常必须降级为单条 error 而非上抛整批"""
        result = PytestRunner().run_one({"case_id": "TM-BC-0003"})

        assert result.result == "error"
        assert "source_ref" in (result.error_message or "")

    @allure.story("P3-3 校验实现已下沉到 common 且仍从 case_manager 再导出")
    def test_validate_source_ref_reexported_from_common(self) -> None:
        """
        case_manager 必须**再导出**同一函数对象，而不是另抄一份实现。

        Day47-fix 把实现移到 src/common/source_ref.py（执行侧也要用，而
        case_manager 反向依赖 executors，就地定义会循环导入）。若这里改成
        各自实现一份，规则迟早会漂移成"录入侧放行、执行侧拒绝"——同一条
        数据在不同链路表现不一致，最难排查。
        """
        from src.common.source_ref import validate_source_ref as common_impl

        assert validate_source_ref is common_impl, (
            "case_manager 必须再导出 common 层的同一实现，不得另抄一份"
        )
        assert SOURCE_REF_PATTERN is common_source_ref_pattern, (
            "SOURCE_REF_PATTERN 也必须是同一个对象"
        )
        assert common_source_ref_pattern.match("tests/a.py"), "共用正则应可用"

    @allure.story("Day48 收口：script_path 过渡兼容分支已移除")
    def test_script_path_transition_fallback(self):
        """
        Day47 留的 script_path 过渡兜底在 Day48 随回填收口移除。

        断言口径随之反转: 此前要求"该分支仍能产出可执行命令"，是因为
        回填未完成、存量调用方需要兜底；回填收口后保留它反而有害——它会
        把"库里的 source_ref 还是空的"这一**待补录事实**掩盖成一次看似
        成功的执行，排障时再没人知道哪些用例其实没配执行目标。

        故此处断言：只带 script_path 的用例必须显式失败，且消息指向
        source_ref 与回填命令。
        """
        with pytest.raises(ValueError) as excinfo:
            PytestRunner().build_command(
                {"case_id": "TM-BC-0004", "script_path": "tests/demo.py"}
            )

        message = str(excinfo.value)
        assert "source_ref" in message, f"异常必须指向唯一合法字段: {message}"
        assert "TM-BC-0004" in message, "异常消息必须指明是哪条用例"
        assert "backfill_source_ref" in message, (
            "空值是待补录状态，消息必须给出回填入口而不是让人猜"
        )

    @allure.story("P3-3 执行侧拒绝 .. 越界路径")
    def test_build_command_rejects_parent_dir(self):
        """
        库中非法值不得进入 pytest 命令（Day47-fix 纵深防御）。

        反例后果: `../secret/x.py` 原样拼进命令等于让这条用例的测试
        目标跑到项目根之外，而调用方看不出任何异常。
        """
        with pytest.raises(ValueError) as excinfo:
            PytestRunner().build_command(
                {"case_id": "TM-BC-0005", "source_ref": "../secret/pytest.py"}
            )

        message = str(excinfo.value)
        assert "source_ref" in message
        assert "TM-BC-0005" in message, "异常消息必须指明是哪条用例"

    @allure.story("P3-3 执行侧拒绝含空格的路径")
    def test_build_command_rejects_malformed(self):
        """格式非法的库值同样在执行侧被拒（录入侧是第一道，这里是第二道）"""
        with pytest.raises(ValueError):
            PytestRunner().build_command(
                {"case_id": "TM-BC-0006", "source_ref": "tests/demo file.py"}
            )

    @allure.story("P3-3 run_one 把非法 source_ref 降级为 error")
    def test_run_one_maps_invalid_source_ref_to_error(self):
        """非法值不得让整批判 failed，必须降级为单条 error"""
        result = PytestRunner().run_one(
            {"case_id": "TM-BC-0007", "source_ref": "../secret/pytest.py"}
        )

        assert result.result == "error"
        assert "source_ref" in (result.error_message or "")

    @allure.story("P3-3 合法 source_ref 仍不受影响")
    def test_build_command_accepts_valid_after_hardening(self):
        """加校验后合法路径照常可用（纵深防御不能误伤正常数据）"""
        command = PytestRunner().build_command(
            {"case_id": "TM-BC-0008", "source_ref": "tests/demo.py::TestC::test_d"}
        )
        assert command[-1] == "tests/demo.py::TestC::test_d"


# ===========================================================================
# 6. 回填脚本的辅助函数与 CLI
# ===========================================================================
@allure.feature("Day47-source_ref改造Day2")
@allure.story("回填脚本CLI与辅助函数")
@pytest.mark.usefixtures("temp_db")
class TestBackfillCli:
    """文件存在性判定、报表渲染与命令行入口"""

    @allure.story("文件存在性判定")
    def test_resolve_file_exists(self, fake_project_root: Path) -> None:
        """存在→True；不存在/越出项目根/空路径→False"""
        assert resolve_file_exists("tests/demo.py", fake_project_root) is True
        assert resolve_file_exists("tests/demo.py::TestX::test_y", fake_project_root) is (
            True
        ), "函数级路径只校验 :: 之前的文件部分"
        assert resolve_file_exists("tests/nope.py", fake_project_root) is False
        assert resolve_file_exists("../escape.py", fake_project_root) is False, (
            "越出项目根的路径必须判为不可用"
        )
        assert resolve_file_exists("", fake_project_root) is False, (
            "空路径不得被当成项目根本身"
        )

    @allure.story("取文件部分")
    def test_file_part(self) -> None:
        """去掉 ::函数名 后缀只留文件路径"""
        assert file_part("tests/x.py::TestC::test_y") == "tests/x.py"
        assert file_part("tests/x.py") == "tests/x.py"

    @allure.story("空文本无候选")
    def test_extract_from_empty_text(self) -> None:
        """空串与 None 都直接返回 None（不进入正则扫描）"""
        assert extract_source_ref("") is None
        assert extract_source_ref(None) is None

    @allure.story("提取值超长时跳过回填")
    def test_over_length_candidate_skipped(
        self, temp_db: Path, fake_project_root: Path
    ) -> None:
        """
        超长候选不得写库。

        SQLite 不实现 VARCHAR 长度约束（Day46 实测），库层不会拦，
        写进去就是一条没人发现的脏数据——而它随后会被拼进 pytest 命令。
        """
        long_path = "tests/" + ("a" * (MAX_SOURCE_REF_LENGTH + 10)) + ".py"
        _seed_case(
            "TM-LEN-1",
            name="超长路径",
            module="硬件",
            case_type="chip",
            description=f"验证 {long_path} 覆盖用例",
        )

        stats = backfill_source_ref(project_root=fake_project_root)

        assert stats["updated"] == 0
        assert stats["skipped_file_not_found"] == 1
        assert _read_case("TM-LEN-1")["source_ref"] is None

    @allure.story("报表渲染含三档与清单")
    def test_format_report_contains_three_tiers(
        self, temp_db: Path, fake_project_root: Path
    ) -> None:
        """渲染出的报表必须点名三档并列出回填/待补录清单"""
        _seed_case(
            "TM-RPT-1",
            name="登录",
            module="用户中心",
            case_type="chip",
            description="验证 tests/demo.py",
        )
        _seed_case(
            "TM-RPT-2",
            name="用户查询",
            module="用户管理",
            case_type="api",
            description="",
        )
        _seed_case(
            "TM-RPT-3",
            name="上电自检",
            module="硬件",
            case_type="chip",
            description="读寄存器",
        )
        _seed_case(
            "TM-RPT-4",
            name="缺失",
            module="硬件",
            case_type="chip",
            description="验证 tests/gone.py",
        )

        report = format_report(
            backfill_source_ref(project_root=fake_project_root), dry_run=True
        )

        assert "A 自动提取命中" in report
        assert "B 待人工补录" in report
        assert "C 模拟执行专用" in report
        assert "dry-run" in report, "预演模式标题必须标明 dry-run"
        assert "TM-RPT-1" in report, "回填清单必须点名具体用例"
        assert "TM-RPT-3" in report, "待补录清单必须点名具体用例"
        assert "TM-RPT-4" in report, "文件不存在清单必须点名具体用例"

    @allure.story("非 dry-run 报表显示实际写入数")
    def test_format_report_non_dry_run(self, fake_project_root: Path) -> None:
        """正式模式标题不含 dry-run，且显示实际写入条数"""
        stats = {
            "total": 0,
            "auto_extracted": 0,
            "skipped_file_not_found": 0,
            "pending_manual": 0,
            "simulation_only": 0,
            "updated": 0,
            "to_backfill": [],
            "pending_list": [],
            "not_found_list": [],
        }
        report = format_report(stats, dry_run=False)

        assert "dry-run" not in report
        assert "实际写入库条数" in report

    @allure.story("CLI --dry-run 正常退出且不写库")
    def test_cli_dry_run(
        self,
        temp_db: Path,
        fake_project_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """命令行预演模式跑通并把清单打到 stdout"""
        _seed_case(
            "TM-CLI-1",
            name="登录",
            module="用户中心",
            case_type="chip",
            description="验证 tests/demo.py",
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "backfill_source_ref",
                "--dry-run",
                "--project-root",
                str(fake_project_root),
            ],
        )

        main()

        assert "TM-CLI-1" in capsys.readouterr().out
        assert _read_case("TM-CLI-1")["source_ref"] is None, "预演不得写库"

    @allure.story("CLI --limit 只处理前 N 条")
    def test_cli_limit(
        self,
        temp_db: Path,
        fake_project_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """命令行分批回填：limit=1 时只写一条"""
        _seed_case(
            "TM-CLI-2",
            name="登录",
            module="用户中心",
            case_type="chip",
            description="验证 tests/demo.py",
        )
        _seed_case(
            "TM-CLI-3",
            name="下单",
            module="交易",
            case_type="chip",
            description="验证 tests/demo2.py",
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "backfill_source_ref",
                "--limit",
                "1",
                "--project-root",
                str(fake_project_root),
            ],
        )

        main()

        assert _read_case("TM-CLI-2")["source_ref"] == "tests/demo.py"
        assert _read_case("TM-CLI-3")["source_ref"] is None, (
            "limit 之外的用例不得被写"
        )

    @allure.story("CLI 非法 --limit 拒绝执行")
    def test_cli_rejects_non_positive_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--limit 0 或负数必须被 argparse 拒绝（SystemExit 2）"""
        monkeypatch.setattr(
            "sys.argv", ["backfill_source_ref", "--limit", "0"]
        )
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 2, "参数非法应退出码 2"

    @allure.story("source_ref 列不存在时 CLI 报错退出")
    def test_cli_missing_column_exits(
        self, temp_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        缺列必须以退出码 1 收场并提示先跑迁移，不能抛裸 traceback。

        构造方式: 把 models 元数据里该列摘掉后重建表，得到"迁移前"结构
        （与 Day46 迁移脚本处理的是同一种库形态）。
        """
        from sqlalchemy import text as sql_text

        DatabaseSession.reset()
        with DatabaseSession.get_engine().begin() as conn:
            conn.execute(sql_text("ALTER TABLE test_cases DROP COLUMN source_ref"))
        monkeypatch.setattr("sys.argv", ["backfill_source_ref", "--dry-run"])

        with pytest.raises(SystemExit) as excinfo:
            main()

        assert excinfo.value.code == 1, "缺列属前置条件未满足，退出码应为 1"


# ===========================================================================
# 7. 默认库隔离守卫（Day47-fix P2-1）
# ===========================================================================
@allure.feature("Day47-fix测试库隔离")
@allure.story("默认库隔离守卫")
class TestDefaultDbIsolationGuard:
    """
    守卫测试：断言 conftest 的隔离在**本用例自身**身上生效

    为什么要有这条: P2-1 的缺陷是"测试把数据写进项目真实库
    output/testmatrix.db"，而这类缺陷**没有任何测试会失败**——
    全量照样绿，只是真实库被污染。本类断言当前用例看到的引擎确实指向
    临时库；一旦有人把守卫删掉或改回用例级，这里立刻变红。

    注意本类**不能**断言真实库文件不变（那需要跨进程比对，成本高且
    易 flaky），断言的是"当前进程的引擎指向哪里"这一真正的不变量。
    """

    @allure.story("引擎指向的库不是项目真实库")
    def test_engine_not_pointing_to_project_db(self) -> None:
        """当前用例的 DatabaseSession 引擎 URL 不得落在 output/testmatrix.db"""
        url = str(DatabaseSession.get_engine().url)
        project_db = (PROJECT_ROOT / "output" / "testmatrix.db").as_posix()

        assert project_db not in url.replace("\\", "/"), (
            f"引擎仍指向项目真实库: {url}"
        )

    @allure.story("env_manager.get 替身不得屏蔽数据库配置键")
    def test_env_get_stub_must_pass_through_db_keys(self) -> None:
        """
        用 patch 整体替换 env_manager.get 的替身，必须放行 DB 配置键。

        这正是 P2-1 的第二个根因：替身对表外的键返回 default，而
        TM_DB_SQLITE_PATH 的 default 恰是真实库路径——屏蔽后隔离环境
        变量完全失效。放在 router 测试文件里的 `_mock_get` 已按此放行，
        本条断言把它钉住，防止后续有人"简化"回去。
        """
        from tests.test_notification_router_demo import DB_CONFIG_KEYS

        assert "TM_DB_SQLITE_PATH" in DB_CONFIG_KEYS
        assert "TM_DB_TYPE" in DB_CONFIG_KEYS

    @allure.story("路由测试的 _mock_get 对 DB 键走真实实现")
    def test_router_mock_get_delegates_db_keys(self) -> None:
        """实际调用替身，确认 DB 键返回隔离后的临时路径而非真实库默认值"""
        from tests.test_notification_router_demo import _mock_get

        stub = _mock_get({"TM_NOTIFY_MAX_RETRIES": "3"})
        # 非 DB 键：走替身表
        assert stub("TM_NOTIFY_MAX_RETRIES", "0") == "3"
        # DB 键：必须放行真实实现（拿到 conftest 设的临时路径）
        resolved = stub("TM_DB_SQLITE_PATH", "output/testmatrix.db")
        assert resolved != "output/testmatrix.db", (
            "DB 配置键被替身屏蔽会回落真实库默认值（P2-1 根因）"
        )
        assert "testmatrix.db" not in resolved, (
            f"隔离后的临时路径不应指向真实库: {resolved}"
        )